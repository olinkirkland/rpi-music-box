from pathlib import Path

from musicbox.box import MusicBox
from musicbox.cards import CardStore


class FakeReader:
    def __init__(self, reads):
        self.reads = list(reads)

    def read(self):
        return self.reads.pop(0)


class FakePlayer:
    def __init__(self):
        self.events = []

    def play(self, track):
        self.events.append(("play", Path(track).name))

    def stop(self):
        self.events.append(("stop",))


def make_box(tmp_path, reads, misses_to_remove=3):
    music = tmp_path / "music"
    music.mkdir()
    for name in ("a.mp3", "b.mp3"):
        (music / name).write_bytes(b"")
    cards = CardStore(tmp_path / "cards.json", music)
    cards.assign("aa", music / "a.mp3")
    cards.assign("bb", music / "b.mp3")
    player = FakePlayer()
    box = MusicBox(FakeReader(reads), player, cards, misses_to_remove)
    for _ in reads:
        box.tick()
    return box, player, music


def test_place_plays_and_remove_stops(tmp_path):
    _, player, _ = make_box(tmp_path, ["aa", "aa", None, None, None])
    assert player.events == [("play", "a.mp3"), ("stop",)]


def test_brief_read_misses_do_not_interrupt(tmp_path):
    box, player, _ = make_box(tmp_path, ["aa", None, None, "aa", None, None, "aa"])
    assert player.events == [("play", "a.mp3")]
    assert box.current_uid == "aa"


def test_same_card_again_after_removal_restarts(tmp_path):
    _, player, _ = make_box(tmp_path, ["aa", None, None, None, "aa"])
    assert player.events == [("play", "a.mp3"), ("stop",), ("play", "a.mp3")]


def test_swapping_cards_switches_track(tmp_path):
    _, player, _ = make_box(tmp_path, ["aa", "bb"])
    assert player.events == [("play", "a.mp3"), ("play", "b.mp3")]


def test_unknown_card_stops_playback(tmp_path):
    _, player, _ = make_box(tmp_path, ["aa", "zz"])
    assert player.events == [("play", "a.mp3"), ("stop",)]


def test_missing_file_does_not_play(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"")
    cards = CardStore(tmp_path / "cards.json", music)
    cards.assign("aa", music / "a.mp3")
    (music / "a.mp3").unlink()
    player = FakePlayer()
    MusicBox(FakeReader(["aa"]), player, cards).tick()
    assert ("play", "a.mp3") not in player.events
