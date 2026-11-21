"""Sound effects: the bundled files, when the box plays them, and the player's checks."""

from pathlib import Path

import pytest

from musicbox.box import MusicBox
from musicbox.cards import CardStore
from musicbox.cli import SOUNDS_DIR
from musicbox.player import Player

from test_box import FakePlayer, FakeReader
from test_player import FakeMixer, FakeProc


def test_bundled_sounds_ship_with_the_app():
    for name in ("chime-add-card.mp3", "chime-remove-card.mp3", "chime-assign.mp3"):
        assert (SOUNDS_DIR / name).stat().st_size > 1000


class SoundPlayer(FakePlayer):
    def sound(self, name):
        self.events.append(("sound", name))
        return True


@pytest.fixture
def box(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"")
    cards = CardStore(tmp_path / "cards.json", music)
    cards.assign("aa11", music / "a.mp3")
    reader = FakeReader([None])
    b = MusicBox(reader, SoundPlayer(), cards, duration_of=lambda p: 1.0)
    b.tick()

    def feed(*uids):
        reader.reads.extend(uids)
        for _ in uids:
            b.tick()

    return b, feed


def test_lifting_a_card_plays_the_removal_chime_after_stopping(box):
    b, feed = box
    feed("aa11", None, None, None)
    assert b.player.events == [("play", "a.mp3"), ("stop",), ("sound", "remove")]


def test_an_unknown_card_lifted_also_chimes(box):
    b, feed = box
    feed("ffee", None, None, None)
    assert b.player.events[-1] == ("sound", "remove")


def test_assign_window_read_plays_the_assign_chime_not_the_tracks(box):
    b, feed = box
    b.start_assigning()
    feed("aa11")
    assert ("sound", "assign") in b.player.events
    assert not [e for e in b.player.events if e[0] == "play"]


def test_brief_misses_dont_chime(box):
    b, feed = box
    feed("aa11", None, None, "aa11")  # two misses then read again: not a lift
    assert ("sound", "remove") not in b.player.events


@pytest.fixture
def popen(monkeypatch):
    FakeProc.started = []
    monkeypatch.setattr("musicbox.player.subprocess.Popen", FakeProc)


def test_player_sound_plays_alone_with_the_volume_check(tmp_path, popen):
    mixer = FakeMixer()
    p = Player(command=["mpv"], chime=tmp_path / "c.wav", mixer=mixer,
               sounds={"remove": (tmp_path / "remove.wav", 0.8)})
    assert p.sound("remove") is True
    assert FakeProc.started == [["mpv", "--", str(tmp_path / "remove.wav")]] and mixer.sets == 1
    assert p.sound("assign") is False  # none configured
    mixer.ok = False
    assert p.sound("remove") is False  # volume not confirmed: silent
    assert len(FakeProc.started) == 1
