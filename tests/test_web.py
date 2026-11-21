import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from musicbox.cards import CardStore
from musicbox.taplog import TapLog
from musicbox.web import start_web


@pytest.fixture
def cards(tmp_path):
    music = tmp_path / "music"
    (music / "album").mkdir(parents=True)
    for name in ("b.mp3", "album/a.MP3", "notes.txt", ".hidden.mp3"):
        (music / name).write_bytes(b"")
    return CardStore(tmp_path / "cards.json", music)


@pytest.fixture
def server(tmp_path, cards):
    taps = TapLog(tmp_path / "taps.db")
    srv = start_web(taps, cards, host="127.0.0.1", port=0)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield taps, base
    srv.shutdown()
    srv.server_close()


def test_page_has_title(server):
    _, base = server
    with urlopen(base + "/") as r:
        assert r.headers["Content-Type"].startswith("text/html")
        assert "Olin's Music Box" in r.read().decode()


def test_api_returns_taps_newest_first(server):
    taps, base = server
    taps.record("aa", "a.mp3", "played", when=1.0)
    taps.record("bb", None, "unknown", when=2.0)
    with urlopen(base + "/api/taps") as r:
        data = json.load(r)
    assert [t["uid"] for t in data] == ["bb", "aa"]
    with urlopen(base + "/api/taps?limit=1") as r:
        assert len(json.load(r)) == 1


def test_unknown_path_is_404(server):
    _, base = server
    with pytest.raises(HTTPError) as e:
        urlopen(base + "/nope")
    assert e.value.code == 404


def call(base, method, path, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    h = {"Content-Type": "application/json"} if body is not None else {}
    h.update(headers or {})
    try:
        with urlopen(Request(base + path, data=data, method=method, headers=h)) as r:
            return r.status, json.load(r)
    except HTTPError as e:
        return e.code, json.load(e)


def test_tracks_lists_audio_files_only(server):
    _, base = server
    assert call(base, "GET", "/api/tracks") == (200, ["album/a.MP3", "b.mp3"])


def test_assign_change_and_remove_card(server, cards):
    _, base = server
    assert call(base, "PUT", "/api/cards/04AABB", {"track": "b.mp3"})[0] == 200
    assert cards.all() == {"04aabb": "b.mp3"}
    call(base, "PUT", "/api/cards/04aabb", {"track": "album/a.MP3"})
    assert call(base, "GET", "/api/cards") == (
        200, [{"uid": "04aabb", "track": "album/a.MP3", "missing": False}])
    assert call(base, "DELETE", "/api/cards/04aabb")[0] == 200
    assert cards.all() == {}
    assert call(base, "DELETE", "/api/cards/04aabb")[0] == 404


def test_card_list_flags_missing_files(server, cards):
    _, base = server
    cards.assign("aa11", cards.music_dir / "b.mp3")
    (cards.music_dir / "b.mp3").unlink()
    assert call(base, "GET", "/api/cards")[1][0]["missing"] is True


@pytest.mark.parametrize("track", ["../cards.json", "notes.txt", "/etc/passwd", None, 5])
def test_rejects_tracks_not_in_library(server, cards, track):
    _, base = server
    assert call(base, "PUT", "/api/cards/aa11", {"track": track})[0] == 400
    assert cards.all() == {}


def test_rejects_bad_card_ids(server):
    _, base = server
    assert call(base, "PUT", "/api/cards/not-hex", {"track": "b.mp3"})[0] == 400


def test_rejects_cross_site_writes(server, cards):
    _, base = server
    code, _ = call(base, "PUT", "/api/cards/aa11", {"track": "b.mp3"},
                   headers={"Origin": "http://evil.example"})
    assert code == 403
    assert cards.all() == {}


def test_rejects_non_json_body(server):
    _, base = server
    req = Request(base + "/api/cards/aa11", data=b"track=b.mp3", method="PUT",
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    with pytest.raises(HTTPError) as e:
        urlopen(req)
    assert e.value.code == 415
