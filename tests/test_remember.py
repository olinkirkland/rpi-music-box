"""Remember playback: saving where a card left off, and resuming there."""

import json
import random
from urllib.request import Request, urlopen

import pytest

from musicbox.box import MusicBox
from musicbox.cards import CardStore
from musicbox.events import EventBus
from musicbox.positions import RESUME_BACK, PositionStore, resume_point
from musicbox.taplog import TapLog
from musicbox.web import start_web

from test_box import FakeReader

NAMES = ["a.mp3", "b.mp3", "c.mp3"]


def test_store_save_get_forget_rename(tmp_path):
    store = PositionStore(tmp_path / "p.json")
    assert store.get("aa") is None
    store.save("aa", "x.mp3", 61.27)
    assert store.get("aa")["position"] == 61.3 and store.get("aa")["track"] == "x.mp3"
    store.rename("aa", "bb")
    assert store.get("aa") is None and store.get("bb")
    store.forget("bb")
    assert store.get("bb") is None
    (tmp_path / "p.json").write_text("not json")
    assert store.get("bb") is None  # a damaged file means "start over", not a crash


def test_resume_point_goes_back_a_little():
    assert resume_point(100) == 100 - RESUME_BACK
    assert resume_point(2) == 0


class StartPlayer:
    chime_seconds = 2.0
    controllable = False
    playing = True

    def __init__(self):
        self.calls = []

    def play(self, tracks, start=0.0):
        tracks = tracks if isinstance(tracks, list) else [tracks]
        self.calls.append(([t.name for t in tracks], start))
        self.playing = True
        return True

    def stop(self):
        self.playing = False

    def cue(self):
        pass

    def check(self):
        pass


@pytest.fixture
def setup(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    for n in NAMES:
        (music / n).write_bytes(b"")
    cards = CardStore(tmp_path / "cards.json", music)
    cards.assign("aa11", [music / n for n in NAMES], remember_playback=True)
    store = PositionStore(tmp_path / "positions.json")
    clock = {"t": 1000.0}
    reader = FakeReader([None])
    player = StartPlayer()
    box = MusicBox(reader, player, cards, positions=store, clock=lambda: clock["t"],
                   duration_of=lambda p: 300.0, rng=random.Random(2))
    box.tick()

    def feed(*uids):
        reader.reads.extend(uids)
        for _ in uids:
            box.tick()

    return box, player, cards, store, clock, feed


def test_lifting_saves_and_the_next_tap_resumes_a_bit_back(setup):
    box, player, cards, store, clock, feed = setup
    feed("aa11")
    assert player.calls[-1] == (NAMES, 0.0)
    box.now["part"] = 1  # now on b.mp3 ...
    box.now["song_starts_at"] = clock["t"] - 80.0  # ... 80 s in
    feed(None, None, None)  # lifted
    assert store.get("aa11")["track"] == "b.mp3" and store.get("aa11")["position"] == 80.0
    feed("aa11")
    assert player.calls[-1] == (["b.mp3", "c.mp3"], 80.0 - RESUME_BACK)
    assert box.now["resumed_at"] == 80.0 - RESUME_BACK
    # The page's clock shows the resumed point, not 0:00, once the chime is over.
    clock["t"] += 2.0
    assert clock["t"] - box.now["song_starts_at"] == pytest.approx(80.0 - RESUME_BACK)


def test_nothing_saved_during_the_chime(setup):
    box, player, cards, store, clock, feed = setup
    feed("aa11", None, None, None)  # lifted before the chime ended
    assert store.get("aa11") is None


def test_finishing_every_track_starts_over_next_time(setup):
    box, player, cards, store, clock, feed = setup
    store.save("aa11", "c.mp3", 250.0)
    feed("aa11")
    player.playing = False  # the last track ends with the card still on
    feed("aa11")
    assert box.now["state"] == "finished" and store.get("aa11") is None


def test_cards_without_the_flag_always_start_over(setup):
    box, player, cards, store, clock, feed = setup
    cards.set_settings("aa11", remember_playback=False)
    store.save("aa11", "c.mp3", 120.0)
    feed("aa11")
    assert player.calls[-1] == (NAMES, 0.0)


def test_a_track_no_longer_on_the_card_starts_over(setup):
    box, player, cards, store, clock, feed = setup
    store.save("aa11", "gone.mp3", 120.0)
    feed("aa11")
    assert player.calls[-1] == (NAMES, 0.0)


def test_saved_every_15_seconds_while_playing(setup, monkeypatch):
    box, player, cards, store, clock, feed = setup
    mono = {"t": 0.0}
    monkeypatch.setattr("musicbox.box.time.monotonic", lambda: mono["t"])
    feed("aa11")
    clock["t"] += 32.0  # 30 s into the first track
    mono["t"] += 16.0
    feed("aa11")
    assert store.get("aa11")["track"] == "a.mp3" and store.get("aa11")["position"] == 30.0


def test_api_flag_forget_and_rename(setup, tmp_path):
    box, player, cards, store, clock, feed = setup
    srv = start_web(TapLog(tmp_path / "t.db"), cards, EventBus(), host="127.0.0.1", port=0, controller=box)
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def send(method, path, body):
        req = Request(base + path, data=json.dumps(body).encode(), method=method,
                      headers={"Content-Type": "application/json"})
        with urlopen(req) as r:
            return json.load(r)

    try:
        store.save("aa11", "b.mp3", 50.0)
        send("POST", "/api/cards/aa11/rename", {"uid": "bb22"})
        assert store.get("bb22") and store.get("aa11") is None
        with urlopen(base + "/api/cards") as r:
            assert json.load(r)[0]["remember_playback"] is True
        send("PUT", "/api/cards/bb22", {"tracks": NAMES, "remember_playback": False})
        assert store.get("bb22") is None and cards.settings("bb22")["remember_playback"] is False
    finally:
        srv.shutdown()
        srv.server_close()


def test_stop_for_power_saves_the_position_and_stops(setup):
    box, player, cards, store, clock, feed = setup
    feed("aa11")
    box.now["song_starts_at"] = clock["t"] - 42.0
    box.stop_for_power()
    assert store.get("aa11")["position"] == 42.0
    assert not player.playing and box.now["state"] == "finished"
