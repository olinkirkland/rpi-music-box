"""Pause / resume / seek: the player's mpv socket, the box's rules, the web API."""

import json
import socket
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from musicbox.box import MusicBox
from musicbox.cards import CardStore
from musicbox.player import ControlError, Player
from musicbox.taplog import TapLog
from musicbox.web import start_web

from test_box import FakePlayer, FakeReader
from test_player import FakeMixer, FakeProc


# --- Player <-> a fake mpv control socket ------------------------------------

class FakeMpv:
    """Answers mpv JSON IPC commands on a Unix socket, like mpv does."""

    def __init__(self, path):
        self.props = {"pause": False, "time-pos": 12.5, "playlist-pos": 1}
        self.commands = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen()
        threading.Thread(target=self._serve, daemon=True).start()

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
                if not buf:
                    continue
                req = json.loads(buf.split(b"\n")[0])
                cmd = req["command"]
                self.commands.append(cmd)
                data = None
                if cmd[0] == "get_property":
                    data = self.props.get(cmd[1])
                elif cmd[0] == "set_property":
                    self.props[cmd[1]] = cmd[2]
                elif cmd[0] == "seek":
                    self.props["time-pos"] = float(cmd[1])
                # An unrelated event first, as mpv may send one.
                conn.sendall(b'{"event":"property-change"}\n' + json.dumps(
                    {"data": data, "error": "success", "request_id": req["request_id"]}).encode() + b"\n")

    def close(self):
        self.server.close()


@pytest.fixture
def mpv_player(tmp_path, monkeypatch):
    FakeProc.started = []
    monkeypatch.setattr("musicbox.player.subprocess.Popen", FakeProc)
    mixer = FakeMixer()
    player = Player(command=["mpv"], chime=tmp_path / "chime.wav", chime_seconds=2.0,
                    mixer=mixer, ipc_dir=tmp_path)
    player.play(tmp_path / "song.mp3")
    mpv = FakeMpv(player.ipc_path)  # mpv makes the socket once it has started
    yield player, mpv, mixer
    mpv.close()


def test_mpv_command_gets_a_control_socket(tmp_path):
    p = Player(ipc_dir=tmp_path)
    assert f"--input-ipc-server={p.ipc_path}" in p.command
    assert p.controllable
    assert not Player(ipc_dir=tmp_path, play_seconds=5).controllable
    assert not Player().controllable


def test_pause_resume_seek_over_the_socket(mpv_player):
    player, mpv, mixer = mpv_player
    assert player.pause() == 12.5
    assert mpv.props["pause"] is True
    sets_before = mixer.sets
    assert player.resume() is True
    assert mpv.props["pause"] is False
    assert mixer.sets == sets_before + 1  # volume re-checked before sound resumes
    assert player.seek(65) == 65.0
    assert ["seek", 65.0, "absolute"] in mpv.commands


def test_resume_refused_if_volume_not_confirmed(mpv_player):
    player, mpv, mixer = mpv_player
    player.pause()
    mixer.ok = False
    assert player.resume() is False
    assert mpv.props["pause"] is True  # still silent


def test_no_controls_during_the_chime(mpv_player):
    player, mpv, _ = mpv_player
    mpv.props["playlist-pos"] = 0
    for call in (player.pause, player.resume, lambda: player.seek(5)):
        with pytest.raises(ControlError, match="chime"):
            call()
    assert mpv.props["pause"] is False


def test_controls_fail_cleanly_when_player_not_ready(tmp_path, monkeypatch):
    monkeypatch.setattr("musicbox.player.IPC_TIMEOUT", 0.2)
    monkeypatch.setattr("musicbox.player.subprocess.Popen", FakeProc)
    player = Player(command=["mpv"], chime=tmp_path / "c.wav", ipc_dir=tmp_path)
    with pytest.raises(ControlError, match="nothing is playing"):
        player.pause()
    player.play(tmp_path / "song.mp3")  # running, but no socket appears
    with pytest.raises(ControlError, match="not ready"):
        player.pause()


# --- MusicBox rules ------------------------------------------------------------

class ControlPlayer(FakePlayer):
    controllable = True
    chime_seconds = 2.0

    def __init__(self):
        super().__init__()
        self.position = 0.0
        self.paused = False
        self.resume_ok = True

    def pause(self):
        self.paused = True
        self.events.append(("pause",))
        return self.position

    def resume(self):
        if not self.resume_ok:
            return False
        self.paused = False
        self.events.append(("resume",))
        return True

    def seek(self, seconds):
        self.position = seconds
        self.events.append(("seek", seconds))
        return seconds


def make_box(tmp_path, player=None):
    music = tmp_path / "music"
    music.mkdir()
    (music / "song.mp3").write_bytes(b"")
    cards = CardStore(tmp_path / "cards.json", music)
    cards.assign("aa", music / "song.mp3")
    player = player or ControlPlayer()
    t = {"now": 1000.0}
    reader = FakeReader([])
    box = MusicBox(reader, player, cards, clock=lambda: t["now"],
                   duration_of=lambda p: 200.0)

    def feed(*uids):
        reader.reads.extend(uids)
        for _ in uids:
            box.tick()

    feed(None, "aa")  # boot with an empty reader, then a tap
    return box, player, feed, t


def test_pause_resume_and_seek_update_now(tmp_path):
    box, player, feed, t = make_box(tmp_path)
    player.position = 30.0
    assert box.control("pause")["state"] == "paused"
    assert box.now["position"] == 30.0 and "song_starts_at" not in box.now
    assert box.control("seek", 90)["position"] == 90  # seek while paused stays paused
    assert box.now["state"] == "paused"
    t["now"] = 2000.0
    now = box.control("resume")
    assert now["state"] == "playing" and now["song_starts_at"] == 2000.0 - 90
    box.control("seek", 10)
    assert box.now["song_starts_at"] == 2000.0 - 10
    assert [e for e in player.events if e[0] != "play"] == [
        ("pause",), ("seek", 90), ("resume",), ("seek", 10)]


def test_web_can_never_start_or_replay_a_song(tmp_path):
    box, player, feed, _ = make_box(tmp_path)
    box.control("pause")
    feed(None, None, None)  # card lifted
    assert box.now == {"state": "idle"}
    for action in ("resume", "pause", "seek"):
        with pytest.raises(ControlError):
            box.control(action, 5)
    plays = [e for e in player.events if e[0] == "play"]
    assert len(plays) == 1


def test_no_resume_once_the_song_finished(tmp_path):
    box, player, feed, _ = make_box(tmp_path)
    player.playing = False  # song ended, card still on
    feed("aa")
    assert box.now["state"] == "finished"
    with pytest.raises(ControlError):
        box.control("resume")


def test_resume_blocked_by_volume_stays_paused(tmp_path):
    box, player, *_ = make_box(tmp_path)
    box.control("pause")
    player.resume_ok = False
    with pytest.raises(ControlError, match="volume"):
        box.control("resume")
    assert box.now["state"] == "paused"


def test_controls_off_when_player_cannot_do_them(tmp_path):
    box, *_ = make_box(tmp_path, player=FakePlayer())  # no controllable
    assert box.now["controls"] is False
    with pytest.raises(ControlError, match="not available"):
        box.control("pause")


def test_duration_still_arrives_after_a_pause(tmp_path):
    gate = threading.Event()
    box, player, *_ = make_box(tmp_path)
    box.now = {**box.now, "duration": None}

    def slow(p):
        gate.wait(2)
        return 123.0

    box.duration_of = slow
    song = tmp_path / "music" / "song.mp3"
    box._look_up_info([song])
    box.control("pause")
    gate.set()
    for _ in range(500):  # the info thread also runs ffprobe, slow on a Pi 3
        if box.now.get("duration") == 123.0:
            break
        threading.Event().wait(0.01)
    assert box.now["duration"] == 123.0 and box.now["state"] == "paused"


# --- web API ----------------------------------------------------------------------

class FakeController:
    def __init__(self):
        self.calls = []
        self.fail = None

    def control(self, action, position=None):
        self.calls.append((action, position))
        if self.fail:
            raise ControlError(self.fail)
        return {"state": "paused" if action == "pause" else "playing"}


@pytest.fixture
def api(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    ctl = FakeController()
    srv = start_web(TapLog(tmp_path / "t.db"), CardStore(tmp_path / "c.json", music),
                    host="127.0.0.1", port=0, controller=ctl)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield ctl, base
    srv.shutdown()
    srv.server_close()


def post(base, path, body, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    req = Request(base + path, data=json.dumps(body).encode(), method="POST", headers=h)
    try:
        with urlopen(req) as r:
            return r.status, json.load(r)
    except HTTPError as e:
        return e.code, json.load(e)


def test_player_endpoints(api):
    ctl, base = api
    code, data = post(base, "/api/player/pause", {})
    assert code == 200 and data["state"] == "paused" and "server_time" in data
    assert post(base, "/api/player/seek", {"position": 42.5})[0] == 200
    assert ctl.calls == [("pause", None), ("seek", 42.5)]


@pytest.mark.parametrize("position", [None, "10", -1, True, 10 ** 9])
def test_seek_rejects_bad_positions(api, position):
    ctl, base = api
    assert post(base, "/api/player/seek", {"position": position})[0] == 400
    assert ctl.calls == []


def test_player_endpoint_refusals(api):
    ctl, base = api
    ctl.fail = "nothing from a card on the reader is playing"
    code, data = post(base, "/api/player/resume", {})
    assert code == 409 and "card" in data["error"]
    ctl.fail = None
    ctl.calls.clear()
    assert post(base, "/api/player/nope", {})[0] == 404
    assert post(base, "/api/player/pause", {}, {"Origin": "http://evil.example"})[0] == 403
    req = Request(base + "/api/player/pause", data=b"", method="POST")
    with pytest.raises(HTTPError) as e:
        urlopen(req)
    assert e.value.code == 415
    assert ctl.calls == []
