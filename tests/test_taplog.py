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


def test_search_pages_newest_first_and_matches_literally(tmp_path):
    taps = TapLog(tmp_path / "taps.db")
    for i in range(250):
        taps.record(f"{i:04x}aa", "Story/100%_off.mp3" if i == 7 else f"t{i}.mp3",
                    "unknown" if i % 50 == 0 else "played")
    rows, total = taps.search("", limit=100, offset=0)
    assert total == 250 and len(rows) == 100 and rows[0]["uid"] == "00f9aa"
    rows, total = taps.search("", limit=100, offset=200)
    assert len(rows) == 50 and rows[-1]["uid"] == "0000aa"
    assert taps.search("100%_")[1] == 1  # % and _ are plain characters here
    assert taps.search("unknown")[1] == 5
    assert taps.search("00f9")[1] == 1


def test_search_endpoint(tmp_path):
    import json
    from urllib.request import urlopen

    from musicbox.cards import CardStore
    from musicbox.web import start_web

    taps = TapLog(tmp_path / "taps.db")
    for i in range(130):
        taps.record(f"{i:04x}bb", "a.mp3", "played")
    (tmp_path / "m").mkdir()
    srv = start_web(taps, CardStore(tmp_path / "c.json", tmp_path / "m"), host="127.0.0.1", port=0)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with urlopen(base + "/api/taps/search?page=2") as r:
            data = json.load(r)
        assert (data["total"], data["page"], data["pages"], len(data["rows"])) == (130, 2, 2, 30)
        with urlopen(base + "/api/taps/search?q=0081&page=abc") as r:
            data = json.load(r)
        assert data["total"] == 1 and data["page"] == 1
    finally:
        srv.shutdown()
        srv.server_close()
