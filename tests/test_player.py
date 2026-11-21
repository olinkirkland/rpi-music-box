import subprocess

import pytest

from musicbox.player import Mixer, Player


class FakeProc:
    started = []

    def __init__(self, cmd, **kw):
        FakeProc.started.append(cmd)
        self.running = True

    def poll(self):
        return None if self.running else 0

    def terminate(self):
        self.running = False

    def wait(self, timeout=None):
        return 0


class FakeMixer:
    def __init__(self, ok=True, level=10, limit=10):
        self.ok, self.level, self.limit = ok, level, limit
        self.sets = 0

    def set_and_verify(self):
        self.sets += 1
        return self.ok

    def current(self):
        return self.level


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def fake_popen(monkeypatch):
    FakeProc.started = []
    monkeypatch.setattr("musicbox.player.subprocess.Popen", FakeProc)


def make(tmp_path, **kw):
    kw.setdefault("chime", tmp_path / "chime.wav")
    kw.setdefault("chime_seconds", 2.0)
    return Player(command=["mpv"], **kw)


def test_chime_plays_before_the_track(tmp_path):
    p = make(tmp_path)
    assert p.play(tmp_path / "song.mp3") is True
    p.cue()
    assert FakeProc.started == [
        ["mpv", "--", str(tmp_path / "chime.wav"), str(tmp_path / "song.mp3")],
        ["mpv", "--", str(tmp_path / "chime.wav")],
    ]


def test_no_chime_means_no_song(tmp_path):
    p = make(tmp_path, chime=None)
    assert p.play(tmp_path / "song.mp3") is False
    p.cue()
    assert FakeProc.started == []


def test_nothing_plays_if_volume_not_confirmed(tmp_path):
    mixer = FakeMixer(ok=False)
    p = make(tmp_path, mixer=mixer)
    assert p.play(tmp_path / "song.mp3") is False
    p.cue()
    assert FakeProc.started == [] and mixer.sets == 2


def test_volume_is_set_before_every_sound(tmp_path):
    mixer = FakeMixer()
    p = make(tmp_path, mixer=mixer)
    p.play(tmp_path / "song.mp3")
    p.cue()
    assert mixer.sets == 2 and len(FakeProc.started) == 2


@pytest.mark.parametrize("level", [11, 100, None])
def test_playback_killed_if_volume_rises(tmp_path, level):
    clock, mixer = Clock(), FakeMixer()
    p = make(tmp_path, mixer=mixer, clock=clock)
    p.play(tmp_path / "song.mp3")
    mixer.level = level
    clock.t = 1.0
    p.check()
    assert not p.playing


def test_hard_stop_after_chime_plus_play_seconds(tmp_path):
    clock = Clock()
    p = make(tmp_path, play_seconds=5, clock=clock)
    p.play(tmp_path / "song.mp3")
    clock.t = 2.0 + 5 + 1.0
    p.check()
    assert p.playing
    clock.t = 2.0 + 5 + 1.6
    p.check()
    assert not p.playing


def test_mpv_command_limits_length_and_volume():
    p = Player(audio_device="alsa/x", play_seconds=5, volume=250)
    assert "--length=5" in p.command
    assert "--volume=100" in p.command and "--volume-max=100" in p.command


def test_mixer_reads_back_levels(monkeypatch):
    outputs = {"sset": "", "sget": "  Front Left: Playback 15 [10%] [-26dB] [on]\n"
                                    "  Front Right: Playback 15 [10%] [-26dB] [on]\n"}
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        verb = "sset" if "sset" in cmd else "sget"
        return subprocess.CompletedProcess(cmd, 0, outputs[verb], "")

    monkeypatch.setattr("musicbox.player.subprocess.run", run)
    m = Mixer("USB", 10)
    assert m.set_and_verify() is True
    assert calls[0] == ["amixer", "-c", "USB", "-q", "sset", "PCM", "10%"]
    outputs["sget"] = "  Front Left: Playback 147 [100%] [on]\n"
    assert m.set_and_verify() is False


def test_mixer_fails_closed_without_card_or_amixer(monkeypatch):
    assert Mixer("", 10).set_and_verify() is False
    monkeypatch.setattr("musicbox.player.subprocess.run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "no card"))
    assert Mixer("USB", 10).set_and_verify() is False
