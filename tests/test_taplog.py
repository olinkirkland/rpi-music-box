from musicbox.box import MusicBox
from musicbox.cards import CardStore
from musicbox.taplog import TapLog

from test_box import FakePlayer, FakeReader


def test_record_and_recent_newest_first(tmp_path):
    taps = TapLog(tmp_path / "data" / "taps.db")
    taps.record("aa", "a.mp3", "played", when=100.0)
    taps.record("zz", None, "unknown", when=200.0)
    rows = taps.recent()
    assert [(r["uid"], r["track"], r["result"], r["time"]) for r in rows] == [
        ("zz", None, "unknown", 200.0),
        ("aa", "a.mp3", "played", 100.0),
    ]
    assert len(taps.recent(limit=1)) == 1


def test_log_survives_reopen(tmp_path):
    TapLog(tmp_path / "taps.db").record("aa", "a.mp3", "played")
    assert len(TapLog(tmp_path / "taps.db").recent()) == 1


def test_box_records_each_placement(tmp_path):
    music = tmp_path / "music"
    (music / "album").mkdir(parents=True)
    (music / "album" / "a.mp3").write_bytes(b"")
    (music / "gone.mp3").write_bytes(b"")
    cards = CardStore(tmp_path / "cards.json", music)
    cards.assign("aa", music / "album" / "a.mp3")
    cards.assign("gg", music / "gone.mp3")
    (music / "gone.mp3").unlink()
    taps = TapLog(tmp_path / "taps.db")
    reads = [None, "aa", "aa", "aa", None, None, None, "zz", "gg"]
    box = MusicBox(FakeReader(reads), FakePlayer(), cards, taplog=taps)
    for _ in reads:
        box.tick()
    rows = [(r["uid"], r["track"], r["result"]) for r in reversed(taps.recent())]
    assert rows == [
        ("aa", "album/a.mp3", "played"),
        ("zz", None, "unknown"),
        ("gg", "gone.mp3", "missing"),
    ]


def test_logging_failure_does_not_stop_playback(tmp_path):
    class BrokenLog:
        def record(self, *a):
            raise OSError("disk full")

    music = tmp_path / "music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"")
    cards = CardStore(tmp_path / "cards.json", music)
    cards.assign("aa", music / "a.mp3")
    player = FakePlayer()
    box = MusicBox(FakeReader([None, "aa"]), player, cards, taplog=BrokenLog())
    box.tick()
    box.tick()
    assert player.events == [("play", "a.mp3")]
