"""Custom card names: stored with the card only, never in the files."""

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from musicbox.box import MusicBox
from musicbox.cards import CardStore, clean_name
from musicbox.events import EventBus
from musicbox.taplog import TapLog
from musicbox.web import start_web

from test_box import FakePlayer, FakeReader


@pytest.fixture
def cards(tmp_path):
    music = tmp_path / "music"
    (music / "Story").mkdir(parents=True)
    (music / "Story" / "1.mp3").write_bytes(b"")
    (music / "a.mp3").write_bytes(b"")
    return CardStore(tmp_path / "cards.json", music)


def test_old_plain_file_still_loads(cards):
    cards.cards_file.write_text(json.dumps({"aa11": "a.mp3"}))
    assert cards.all() == {"aa11": "a.mp3"}
    assert cards.custom_name("aa11") is None


def test_name_is_stored_only_in_the_card_entry(cards):
    before = (cards.music_dir / "a.mp3").read_bytes()
    cards.assign("aa11", cards.music_dir / "Story", name="  William   Tell ")
    cards.assign("bb22", cards.music_dir / "a.mp3")
    raw = json.loads(cards.cards_file.read_text())
    assert raw == {"aa11": {"tracks": ["Story/1.mp3"], "name": "William Tell"}, "bb22": "a.mp3"}
    assert cards.all() == {"aa11": "Story/1.mp3", "bb22": "a.mp3"}
    assert (cards.music_dir / "a.mp3").read_bytes() == before


def test_name_kept_for_same_track_dropped_for_another(cards):
    cards.assign("aa11", cards.music_dir / "Story", name="William Tell")
    cards.assign("aa11", cards.music_dir / "Story")
    assert cards.custom_name("aa11") == "William Tell"
    cards.assign("aa11", cards.music_dir / "a.mp3")
    assert cards.custom_name("aa11") is None
    cards.assign("aa11", cards.music_dir / "Story", name="New")
    cards.assign("aa11", cards.music_dir / "Story", name="")
    assert cards.custom_name("aa11") is None


def test_set_name_and_rename_keep_the_track(cards):
    cards.assign("aa11", cards.music_dir / "Story")
    assert cards.set_name("aa11", "Bedtime") == "Bedtime"
    cards.rename("aa11", "cc33")
    assert cards.entries() == {"cc33": {"tracks": ["Story/1.mp3"], "name": "Bedtime"}}
    cards.set_name("cc33", None)
    assert cards.entries() == {"cc33": {"tracks": ["Story/1.mp3"]}}
    with pytest.raises(KeyError):
        cards.set_name("zz99", "x")


@pytest.mark.parametrize("bad", [5, ["x"], "a" * 81, "bad\x00name"])
def test_clean_name_rejects(bad):
    with pytest.raises(ValueError):
        clean_name(bad)


def test_clean_name_tidies():
    assert clean_name("  Snow\tWhite \n") == "Snow White"
    assert clean_name("   ") is None and clean_name(None) is None


def test_now_playing_uses_the_custom_name(cards):
    cards.assign("aa11", cards.music_dir / "Story", name="William Tell")
    box = MusicBox(FakeReader([None, "aa11"]), FakePlayer(), cards, events=EventBus(),
                   duration_of=lambda p: 10.0)
    box.tick()
    box.tick()
    assert box.now["name"] == "William Tell"
    for _ in range(300):  # the tag lookup thread must not overwrite it
        if box.now.get("duration") == 10.0:
            break
        __import__("time").sleep(0.01)
    assert box.now["name"] == "William Tell"


@pytest.fixture
def api(tmp_path, cards):
    srv = start_web(TapLog(tmp_path / "t.db"), cards, EventBus(), host="127.0.0.1", port=0)
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def put(base, uid, body):
    req = Request(f"{base}/api/cards/{uid}", data=json.dumps(body).encode(), method="PUT",
                  headers={"Content-Type": "application/json"})
    try:
        with urlopen(req) as r:
            return r.status, json.load(r)
    except HTTPError as e:
        return e.code, json.load(e)


def test_api_sets_and_lists_custom_names(api, cards):
    code, data = put(api, "aa11", {"tracks": ["Story/1.mp3"], "name": "William Tell"})
    assert code == 200 and data["name"] == "William Tell" and data["tracks"] == ["Story/1.mp3"]
    with urlopen(api + "/api/cards") as r:
        (card,) = json.load(r)
    assert (card["name"], card["custom_name"], card["tag_name"]) == ("William Tell", "William Tell", "1")
    assert put(api, "aa11", {"tracks": ["Story/1.mp3"], "name": ""})[1]["name"] is None
    assert put(api, "aa11", {"tracks": ["Story/1.mp3"], "name": "x" * 81})[0] == 400
    assert put(api, "aa11", {"tracks": ["Story/1.mp3"], "name": 7})[0] == 400
