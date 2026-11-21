"""Assign modal support: silent assigning mode, and renaming a card's ID."""

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from musicbox import box as box_module
from musicbox.box import MusicBox
from musicbox.cards import CardStore
from musicbox.events import EventBus
from musicbox.taplog import TapLog
from musicbox.web import start_web

from test_box import FakePlayer, FakeReader


@pytest.fixture
def setup(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"")
    (music / "b.mp3").write_bytes(b"")
    cards = CardStore(tmp_path / "cards.json", music)
    cards.assign("aa11", music / "a.mp3")
    bus = EventBus()
    q = bus.subscribe()
    reader = FakeReader([None])
    player = FakePlayer()
    box = MusicBox(reader, player, cards, taplog=TapLog(tmp_path / "t.db"), events=bus)
    box.tick()  # boot with an empty reader

    def feed(*uids):
        reader.reads.extend(uids)
        for _ in uids:
            box.tick()

    return box, player, cards, bus, q, feed


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_taps_are_read_silently_while_assigning(setup):
    box, player, cards, bus, q, feed = setup
    box.start_assigning()
    feed("aa11")  # an assigned card
    feed(None, None, None, "bb22")  # lifted, then a new one
    assert [e for e in player.events if e[0] == "play"] == []
    assert ("cue",) not in player.events  # not even the chime
    taps = [d for e, d in drain(q) if e == "assign_tap"]
    assert taps == [{"uid": "aa11", "track": "a.mp3", "name": "a"},
                    {"uid": "bb22", "track": None, "name": None}]
    assert box.now == {"state": "assigning", "uid": "bb22"}


def test_card_on_reader_after_assigning_stays_silent(setup):
    box, player, cards, bus, q, feed = setup
    box.start_assigning()
    feed("aa11")
    box.stop_assigning()
    assert box.now == {"state": "waiting", "uid": "aa11"}
    feed("aa11", "aa11")
    assert player.events == [("stop",)]  # still nothing played
    feed(None, None, None, "aa11")  # lift and place again: now it plays
    assert ("play", "a.mp3") in player.events


def test_assigning_times_out_by_itself(setup, monkeypatch):
    box, player, cards, bus, q, feed = setup
    t = {"now": 1000.0}
    monkeypatch.setattr(box_module.time, "monotonic", lambda: t["now"])
    box.start_assigning()
    assert box.assigning
    t["now"] += box_module.ASSIGN_LEASE + 1
    assert not box.assigning
    feed("aa11")
    assert ("play", "a.mp3") in player.events


def test_assign_taps_are_logged(setup):
    box, player, cards, bus, q, feed = setup
    box.start_assigning()
    feed("cc33")
    assert box.taplog.recent()[0]["result"] == "assigning"


def test_rename_keeps_the_track(tmp_path):
    music = tmp_path / "m"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"")
    (music / "b.mp3").write_bytes(b"")
    cards = CardStore(tmp_path / "c.json", music)
    cards.assign("aa11", music / "a.mp3")
    cards.assign("bb22", music / "b.mp3")
    assert cards.rename("aa11", "cc33") == ["a.mp3"]
    assert cards.all() == {"cc33": "a.mp3", "bb22": "b.mp3"}
    with pytest.raises(ValueError):
        cards.rename("cc33", "bb22")
    with pytest.raises(KeyError):
        cards.rename("zz99", "dd44")


@pytest.fixture
def api(setup, tmp_path):
    box, player, cards, bus, q, feed = setup
    srv = start_web(box.taplog, cards, bus, host="127.0.0.1", port=0, controller=box)
    yield box, cards, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def post(base, path, body, headers=None):
    req = Request(base + path, data=json.dumps(body).encode(), method="POST",
                  headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urlopen(req) as r:
            return r.status, json.load(r)
    except HTTPError as e:
        return e.code, json.load(e)


def test_assign_mode_endpoints(api):
    box, cards, base = api
    assert post(base, "/api/assign/start", {}) == (200, {"active": True})
    assert box.assigning
    assert post(base, "/api/assign/stop", {}) == (200, {"active": False})
    assert not box.assigning
    assert post(base, "/api/assign/start", {}, {"Origin": "http://evil.example"})[0] == 403
    assert not box.assigning


def test_rename_endpoint(api):
    box, cards, base = api
    code, data = post(base, "/api/cards/AA11/rename", {"uid": " 04CC33 "})
    assert code == 200 and data == {"uid": "04cc33", "tracks": ["a.mp3"], "track": "a.mp3"}
    assert cards.all() == {"04cc33": "a.mp3"}
    cards.assign("bb22", cards.music_dir / "b.mp3")
    assert post(base, "/api/cards/04cc33/rename", {"uid": "bb22"})[0] == 409
    assert post(base, "/api/cards/04cc33/rename", {"uid": "not hex"})[0] == 400
    assert post(base, "/api/cards/dead99/rename", {"uid": "beef00"})[0] == 404
    assert post(base, "/api/cards/04cc33/rename", {"uid": "ee55"}, {"Origin": "http://evil.example"})[0] == 403
    assert cards.all() == {"04cc33": "a.mp3", "bb22": "b.mp3"}
