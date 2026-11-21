"""Volume buttons: the mixer, the player, the box rules, and the web API."""

import json
import subprocess
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from musicbox.box import VOLUME_STEPS, MusicBox
from musicbox.cards import CardStore
from musicbox.events import EventBus
from musicbox.player import ControlError, Mixer, Player
from musicbox.taplog import TapLog
from musicbox.web import start_web

from test_box import FakePlayer, FakeReader
from test_player import FakeMixer, FakeProc


class FakeAmixer:
    """Stands in for `amixer`: remembers the level and mute switch."""

    def __init__(self, level=40):
        self.level, self.muted, self.calls = level, False, []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if "sset" in cmd:
            self.level = int(cmd[cmd.index("sset") + 2].rstrip("%"))
            self.muted = "mute" in cmd
            return subprocess.CompletedProcess(cmd, 0, "", "")
        switch = "[off]" if self.muted else "[on]"
        out = "".join(f"  Front {side}: Playback 0 [{self.level}%] [-10dB] {switch}\n"
                      for side in ("Left", "Right"))
        return subprocess.CompletedProcess(cmd, 0, out, "")


@pytest.fixture
def amixer(monkeypatch):
    fake = FakeAmixer()
    monkeypatch.setattr("musicbox.player.subprocess.run", fake)
    return fake


def test_zero_percent_mutes_and_others_unmute(amixer):
    m = Mixer("USB", 40)
    m.set_level(0)
    assert m.set_and_verify() and amixer.muted and m.current() == 0
    m.set_level(60)
    assert m.set_and_verify() and not amixer.muted and m.current() == 60


def test_muted_channel_counts_as_zero(amixer):
    amixer.level, amixer.muted = 100, True
    assert Mixer("USB", 0).current() == 0


@pytest.fixture
def popen(monkeypatch):
    FakeProc.started = []
    monkeypatch.setattr("musicbox.player.subprocess.Popen", FakeProc)


def test_set_volume_applies_now_and_watchdog_uses_new_level(tmp_path, popen):
    mixer = FakeMixer(level=40, limit=40)
    p = Player(command=["mpv"], chime=tmp_path / "c.wav", mixer=mixer)
    p.play(tmp_path / "song.mp3")
    assert p.set_volume(20) is True and p.volume == 20 and p.playing
    mixer.level = 30  # something pushed it above the new level
    p._next_check = 0
    p.check()
    assert not p.playing


def test_set_volume_stops_playback_if_not_confirmed(tmp_path, popen):
    mixer = FakeMixer()
    p = Player(command=["mpv"], chime=tmp_path / "c.wav", mixer=mixer)
    p.play(tmp_path / "song.mp3")
    mixer.ok = False
    assert p.set_volume(80) is False
    assert not p.playing


def test_set_volume_needs_a_mixer():
    with pytest.raises(ControlError, match="mixer_volume"):
        Player(command=["mpv"]).set_volume(20)


class VolumePlayer(FakePlayer):
    def __init__(self):
        super().__init__()
        self.volume = 40

    def set_volume(self, level):
        self.volume = level
        return True


def make_box(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    bus = EventBus()
    box = MusicBox(FakeReader([]), VolumePlayer(), CardStore(tmp_path / "c.json", music),
                   events=bus)
    return box, bus


def test_box_volume_steps_and_event(tmp_path):
    box, bus = make_box(tmp_path)
    q = bus.subscribe()
    assert box.set_volume(100) == {"level": 100, "steps": list(VOLUME_STEPS),
                                   "available": True, "applied": True}
    assert q.get_nowait() == ("volume", {"level": 100, "steps": list(VOLUME_STEPS), "available": True})
    assert bus.latest("volume")["level"] == 100
    for bad in (10, 55, -20, 120):
        with pytest.raises(ControlError):
            box.set_volume(bad)
    assert box.player.volume == 100


def test_setting_volume_never_starts_a_sound(tmp_path):
    box, _ = make_box(tmp_path)
    for v in VOLUME_STEPS:
        box.set_volume(v)
    assert box.player.events == []


@pytest.fixture
def api(tmp_path):
    box, bus = make_box(tmp_path)
    box.publish_volume()
    srv = start_web(TapLog(tmp_path / "t.db"), box.cards, bus, host="127.0.0.1", port=0,
                    controller=box)
    yield box, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def post(base, body, headers=None):
    req = Request(base + "/api/volume", data=json.dumps(body).encode(), method="POST",
                  headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urlopen(req) as r:
            return r.status, json.load(r)
    except HTTPError as e:
        return e.code, json.load(e)


def test_volume_api(api):
    box, base = api
    with urlopen(base + "/api/volume") as r:
        assert json.load(r)["level"] == 40
    code, data = post(base, {"level": 60})
    assert code == 200 and data["level"] == 60 and box.player.volume == 60


@pytest.mark.parametrize("level", [None, "60", 60.0, True, 33, 140])
def test_volume_api_rejects_bad_levels(api, level):
    box, base = api
    assert post(base, {"level": level})[0] == 400
    assert box.player.volume == 40


def test_volume_api_refuses_cross_site(api):
    box, base = api
    assert post(base, {"level": 100}, {"Origin": "http://evil.example"})[0] == 403
    assert box.player.volume == 40
