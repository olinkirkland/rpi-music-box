"""Next/previous, shuffle and repeat: the player's playlist commands and the box's rules."""

import json
import random
import socket
import threading

import pytest

from musicbox.box import MusicBox
from musicbox.cards import CardStore
from musicbox.events import EventBus
from musicbox.player import ControlError, Player

from test_box import FakeReader
from test_player import FakeMixer, FakeProc


# --- Player <-> a fake mpv with a real playlist --------------------------------

class PlaylistMpv:
    """Just enough of mpv's JSON IPC to model its playlist."""

    def __init__(self, path, playlist):
        self.playlist = list(playlist)
        self.pos = 1
        self.time_pos = 10.0
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen()
        threading.Thread(target=self._serve, daemon=True).start()

    def answer(self, cmd):
        name = cmd[0]
        if name == "get_property":
            return {"playlist-pos": self.pos, "playlist-count": len(self.playlist),
                    "time-pos": self.time_pos}.get(cmd[1])
        if name == "playlist-next":
            self.pos += 1
            self.time_pos = 0.0
        elif name == "playlist-prev":
            self.pos -= 1
            self.time_pos = 0.0
        elif name == "playlist-remove":
            del self.playlist[cmd[1]]
        elif name == "loadfile":
            assert cmd[2] == "append"
            self.playlist.append(cmd[1])
        elif name == "seek":
            self.time_pos = float(cmd[1])
        return None

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                if buf:
                    req = json.loads(buf.split(b"\n")[0])
                    data = self.answer(req["command"])
                    conn.sendall(json.dumps({"data": data, "error": "success",
                                             "request_id": req["request_id"]}).encode() + b"\n")


@pytest.fixture
def mpv(tmp_path, monkeypatch):
    FakeProc.started = []
    monkeypatch.setattr("musicbox.player.subprocess.Popen", FakeProc)
    player = Player(command=["mpv"], chime=tmp_path / "chime.wav", chime_seconds=2.0,
                    mixer=FakeMixer(), ipc_dir=tmp_path)
    player.play([tmp_path / "a.mp3", tmp_path / "b.mp3", tmp_path / "c.mp3"])
    fake = PlaylistMpv(player.ipc_path, ["chime.wav", "a.mp3", "b.mp3", "c.mp3"])
    yield player, fake
    fake.server.close()


def test_next_and_previous(mpv):
    player, fake = mpv
    assert player.next_track() and fake.pos == 2
    fake.time_pos = 1.0
    assert player.previous_track() == 0 and fake.pos == 1  # back a track
    fake.time_pos = 8.0
    assert player.previous_track() == 0 and fake.pos == 1 and fake.time_pos == 0  # restart it
    fake.time_pos = 1.0
    assert player.previous_track() == 0 and fake.pos == 1  # never back into the chime
    fake.pos = 3
    assert player.next_track() is False  # nothing after the last


def test_replace_and_append_upcoming(mpv, tmp_path):
    player, fake = mpv
    player.replace_upcoming([tmp_path / "c.mp3", tmp_path / "x.mp3"])
    assert [p.split("/")[-1] for p in fake.playlist] == ["chime.wav", "a.mp3", "c.mp3", "x.mp3"]
    player.append_tracks([tmp_path / "a.mp3"])
    assert fake.playlist[-1].endswith("a.mp3")


def test_no_skipping_during_the_chime(mpv):
    player, fake = mpv
    fake.pos = 0
    with pytest.raises(ControlError, match="chime"):
        player.next_track()
    assert player.playlist_state() is None


# --- the box's queue rules ------------------------------------------------------

class QueuePlayer:
    """Models mpv's playlist after the chime; reports it like Player does."""
    chime_seconds = 0.0
    controllable = True

    def __init__(self):
        self.queue = []  # file names, as queued
        self.pos = 0
        self.playing = False
        self.paused = False

    def play(self, tracks):
        tracks = tracks if isinstance(tracks, list) else [tracks]
        self.queue = [t.name for t in tracks]
        self.pos = 0
        self.playing = True
        return True

    def stop(self):
        self.playing = False

    def cue(self):
        pass

    def check(self):
        pass

    def playlist_state(self):
        return (self.pos, 0.0) if self.playing else None

    def next_track(self):
        if self.pos >= len(self.queue) - 1:
            return False
        self.pos += 1
        return True

    def previous_track(self):
        self.pos = max(0, self.pos - 1)
        return self.pos

    def replace_upcoming(self, tracks):
        self.queue = self.queue[: self.pos + 1] + [t.name for t in tracks]

    def append_tracks(self, tracks):
        self.queue += [t.name for t in tracks]

    def pause(self):
        self.paused = True
        return 0.0

    def resume(self):
        return True

    def seek(self, s):
        return s


NAMES = ["a.mp3", "b.mp3", "c.mp3", "d.mp3", "e.mp3"]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    music = tmp_path / "music"
    music.mkdir()
    for n in NAMES:
        (music / n).write_bytes(b"")
    cards = CardStore(tmp_path / "cards.json", music)
    cards.assign("aa11", [music / n for n in NAMES])
    player = QueuePlayer()
    reader = FakeReader([None, "aa11"])
    box = MusicBox(reader, player, cards, events=EventBus(), duration_of=lambda p: 60.0,
                   rng=random.Random(4))
    clock = iter(range(10**6, 10**9, 10))  # 10 s per call: the 1 s throttle never holds back
    monkeypatch.setattr("musicbox.box.time.monotonic", lambda: float(next(clock)))

    def tick(n=1):
        for _ in range(n):
            reader.reads.append("aa11")
            box.tick()

    box.tick()
    box.tick()  # tapped: playing a.mp3
    return box, player, cards, tick


def playing(box):
    return box._files[box.now["part"]].name


def test_plays_in_card_order_then_stops(setup):
    box, player, cards, tick = setup
    assert player.queue == NAMES and playing(box) == "a.mp3"
    player.pos = 4
    tick()
    assert playing(box) == "e.mp3" and box.now["track_no"] == 5
    player.playing = False  # last track ends
    tick()
    assert box.now["state"] == "finished"


def test_next_and_previous_follow_the_queue(setup):
    box, player, cards, tick = setup
    box.control("next")
    assert playing(box) == "b.mp3" and box.now["track_no"] == 2
    box.control("previous")
    assert playing(box) == "a.mp3"
    player.pos = 4
    tick()
    box.control("next")  # past the last, no repeat: stop
    assert box.now["state"] == "finished" and not player.playing


def test_shuffle_on_mid_way_plays_all_others_next(setup):
    box, player, cards, tick = setup
    box.control("next")  # on b
    box.control("shuffle", value=True)
    upcoming = player.queue[player.pos + 1:]
    assert sorted(upcoming) == ["a.mp3", "c.mp3", "d.mp3", "e.mp3"]  # including a, already heard
    assert box.now["shuffle"] is True and box.now["track_no"] == 1
    assert cards.settings("aa11")["shuffle"] is True  # saved on the card


def test_shuffle_off_continues_in_card_order(setup):
    box, player, cards, tick = setup
    box.control("shuffle", value=True)
    player.pos = 2
    tick()
    current = playing(box)
    box.control("shuffle", value=False)
    after = NAMES[NAMES.index(current) + 1:]
    assert player.queue[player.pos + 1:] == after
    assert box.now["track_no"] == NAMES.index(current) + 1


def test_repeat_queues_a_new_round_at_the_last_track(setup):
    box, player, cards, tick = setup
    box.control("repeat", value=True)
    assert cards.settings("aa11")["repeat"] is True
    player.pos = 4
    tick()
    assert player.queue == NAMES + NAMES  # next round queued in card order
    box.control("next")
    assert playing(box) == "a.mp3" and box.now["track_no"] == 1


def test_repeat_with_shuffle_reshuffles_each_round(setup):
    box, player, cards, tick = setup
    box.control("shuffle", value=True)
    box.control("repeat", value=True)
    rounds = []
    for _ in range(6):
        player.pos = len(player.queue) - 1
        tick()
        rounds.append(player.queue[-5:])
    for prev, nxt in zip(rounds, rounds[1:]):
        assert sorted(nxt) == sorted(NAMES)  # every track once per round
        assert nxt[0] != prev[-1]  # never the same track twice in a row
    assert len({tuple(r) for r in rounds}) > 1  # a fresh order each round


def test_repeat_off_drops_the_queued_round(setup):
    box, player, cards, tick = setup
    box.control("repeat", value=True)
    player.pos = 4
    tick()
    assert len(player.queue) == 10
    box.control("repeat", value=False)
    assert player.queue == NAMES and cards.settings("aa11")["repeat"] is False


def test_saved_settings_apply_on_the_next_tap(tmp_path, monkeypatch):
    music = tmp_path / "music"
    music.mkdir()
    for n in NAMES:
        (music / n).write_bytes(b"")
    cards = CardStore(tmp_path / "cards.json", music)
    cards.assign("aa11", [music / n for n in NAMES], shuffle=True, repeat=True)
    player = QueuePlayer()
    box = MusicBox(FakeReader([None, "aa11"]), player, cards, rng=random.Random(1),
                   duration_of=lambda p: 1.0)
    box.tick()
    box.tick()
    assert sorted(player.queue) == NAMES and player.queue != NAMES  # shuffled from the start
    assert box.now["shuffle"] and box.now["repeat"]


def test_controls_never_start_sound_after_a_lift(setup):
    box, player, cards, tick = setup
    box.reader.reads.extend([None, None, None])
    for _ in range(3):
        box.tick()
    assert box.now == {"state": "idle"}
    for action in ("next", "previous", "shuffle", "repeat"):
        with pytest.raises(ControlError):
            box.control(action, value=True)
    assert not player.playing
