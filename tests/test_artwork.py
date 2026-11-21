"""Per-card artwork: uploads, MusicBrainz search (faked, no internet), card upkeep."""

import io
import json
import shutil
import subprocess
import urllib.error
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from musicbox import artwork
from musicbox.artwork import ArtStore, ArtworkError, MusicBrainz, to_jpeg
from musicbox.cards import CardStore
from musicbox.events import EventBus
from musicbox.taplog import TapLog
from musicbox.web import start_web

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
MBID = "9162580e-5df4-32de-80cc-f45a8d8a9b1d"
MBID2 = "4b65927f-9611-48b2-a9da-61c72055683a"


def image(fmt="png", size="40x30"):
    """A small test image made by ffmpeg (fixture data only)."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c=blue:s={size}",
                        "-frames:v", "1", "-f", "image2pipe", "-c:v", {"png": "png", "jpg": "mjpeg"}[fmt], "pipe:1"],
                       capture_output=True, check=True)
    return r.stdout


def test_to_jpeg_reencodes_and_shrinks():
    out = to_jpeg(image("png", "1600x900"))
    assert out[:2] == b"\xff\xd8"
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height",
                            "-of", "csv=p=0", "-"], input=out, capture_output=True).stdout.decode().strip()
    assert probe == "800,450"


@pytest.mark.parametrize("data", [b"", b"ID3\x03\x00 not an image", b"#EXTM3U\nfile:///etc/passwd\n",
                                  b"\x89PNG\r\n\x1a\n broken"])
def test_to_jpeg_refuses_non_images(data):
    with pytest.raises(ArtworkError):
        to_jpeg(data)


def test_art_store_save_rename_remove(tmp_path):
    store = ArtStore(tmp_path / "art")
    assert store.version("aa11") is None
    v = store.save("aa11", image())
    assert v and store.path("aa11").read_bytes()[:2] == b"\xff\xd8"
    store.rename("aa11", "bb22")
    assert store.version("aa11") is None and store.version("bb22")
    assert store.remove("bb22") and not store.remove("bb22")


class FakeResponse(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeInternet:
    """Answers MusicBrainz and Cover Art Archive requests from a script."""

    def __init__(self, searches, covers):
        self.searches = list(searches)  # list of (status, json-able) answers, in order
        self.covers = covers  # {mbid: image bytes}
        self.urls = []
        self.agents = set()

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        self.agents.add(req.get_header("User-agent"))
        if "musicbrainz.org" in url:
            status, body = self.searches.pop(0)
            return FakeResponse(status, json.dumps(body).encode())
        mbid = url.split("/release-group/")[1].split("/")[0]
        if mbid in self.covers:
            return FakeResponse(200, self.covers[mbid])
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)


def groups(*ids):
    return {"release-groups": [{"id": i, "title": "Abbey Road", "first-release-date": "1969-09-26",
                                "artist-credit": [{"name": "The Beatles"}]} for i in ids]}


def test_search_keeps_only_results_with_covers(tmp_path, monkeypatch):
    monkeypatch.setattr(artwork.time, "sleep", lambda s: None)
    net = FakeInternet([(200, groups(MBID, MBID2))], {MBID: image("jpg")})
    mb = MusicBrainz(tmp_path / "mb", opener=net)
    assert mb.search("Abbey Road", "Beatles") == [
        {"id": MBID, "title": "Abbey Road", "artist": "The Beatles", "year": "1969", "type": None}]
    assert net.agents == {artwork.USER_AGENT}
    assert "artist%3A%28Beatles%29" in net.urls[0]
    assert mb.thumbnail(MBID).exists() and mb.thumbnail(MBID2) is None


def test_search_retries_when_busy_and_drops_the_artist(tmp_path, monkeypatch):
    monkeypatch.setattr(artwork.time, "sleep", lambda s: None)
    busy = (200, {"error": "The MusicBrainz web server is currently busy."})
    net = FakeInternet([busy, (200, groups()), (200, groups(MBID))], {MBID: image("jpg")})
    mb = MusicBrainz(tmp_path / "mb", opener=net)
    assert [r["id"] for r in mb.search("Abbey Road", "Nobody")] == [MBID]
    assert "artist" not in net.urls[2]  # second try without the artist


def test_search_errors_are_readable(tmp_path, monkeypatch):
    monkeypatch.setattr(artwork.time, "sleep", lambda s: None)
    busy = (503, {})
    with pytest.raises(ArtworkError, match="busy"):
        MusicBrainz(tmp_path, opener=FakeInternet([busy] * 3, {})).search("x")

    def offline(req, timeout=None):
        raise urllib.error.URLError("Name or service not known")

    with pytest.raises(ArtworkError, match="internet"):
        MusicBrainz(tmp_path, opener=offline).search("x")
    with pytest.raises(ArtworkError):
        MusicBrainz(tmp_path, opener=offline).search("   ")


def test_query_special_characters_are_escaped():
    assert MusicBrainz._escape('AC/DC: "Back" (in) Black!') == 'AC\\/DC\\: \\"Back\\" \\(in\\) Black\\!'


# --- web API ------------------------------------------------------------------

@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(artwork.time, "sleep", lambda s: None)
    music = tmp_path / "music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"")
    (music / "b.mp3").write_bytes(b"")
    cards = CardStore(tmp_path / "cards.json", music)
    cards.assign("aa11", music / "a.mp3")
    store = ArtStore(tmp_path / "art")
    net = FakeInternet([(200, groups(MBID))], {MBID: image("jpg", "500x500")})
    srv = start_web(TapLog(tmp_path / "t.db"), cards, EventBus(), host="127.0.0.1", port=0,
                    art=store, musicbrainz=MusicBrainz(tmp_path / "mb", opener=net))
    yield f"http://127.0.0.1:{srv.server_address[1]}", cards, store
    srv.shutdown()
    srv.server_close()


def send(base, method, path, data=None, ctype="application/json", headers=None):
    body = json.dumps(data).encode() if ctype == "application/json" and data is not None else data
    req = Request(base + path, data=body, method=method, headers={"Content-Type": ctype, **(headers or {})})
    try:
        with urlopen(req) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if r.headers.get_content_type() == "application/json" else raw)
    except HTTPError as e:
        return e.code, json.load(e)


def test_upload_show_and_remove_card_art(api):
    base, cards, store = api
    code, data = send(base, "PUT", "/api/cards/aa11/art", image(), "image/png")
    assert code == 200 and data["art_version"]
    code, raw = send(base, "GET", "/api/cards/aa11/art", ctype="text/plain")
    assert code == 200 and raw[:2] == b"\xff\xd8"
    _, listing = send(base, "GET", "/api/cards", ctype="text/plain")
    assert listing[0]["art_version"] == data["art_version"]
    assert send(base, "DELETE", "/api/cards/aa11/art")[1] == {"uid": "aa11", "removed": True}
    assert send(base, "GET", "/api/cards/aa11/art", ctype="text/plain")[0] == 404


def test_upload_refusals(api):
    base, cards, store = api
    assert send(base, "PUT", "/api/cards/aa11/art", b"not an image", "image/png")[0] == 400
    assert send(base, "PUT", "/api/cards/aa11/art", image(), "text/plain")[0] == 415
    assert send(base, "PUT", "/api/cards/ffff/art", image(), "image/png")[0] == 404  # no such card
    assert send(base, "PUT", "/api/cards/aa11/art", image(), "image/png",
                {"Origin": "http://evil.example"})[0] == 403
    assert store.version("aa11") is None


def test_search_thumb_and_pick_from_musicbrainz(api):
    base, cards, store = api
    code, data = send(base, "GET", "/api/artwork/search?q=Abbey%20Road&artist=Beatles", ctype="text/plain")
    assert code == 200 and [r["id"] for r in data["results"]] == [MBID]
    code, raw = send(base, "GET", f"/api/artwork/thumb/{MBID}", ctype="text/plain")
    assert code == 200 and raw[:2] == b"\xff\xd8"
    assert send(base, "GET", "/api/artwork/thumb/../../etc", ctype="text/plain")[0] == 404
    assert send(base, "POST", "/api/cards/aa11/art/musicbrainz", {"id": MBID})[0] == 200
    assert store.version("aa11")
    assert send(base, "POST", "/api/cards/aa11/art/musicbrainz", {"id": "not-an-id"})[0] == 400


def test_card_upkeep_moves_or_drops_art(api):
    base, cards, store = api
    store.save("aa11", image())
    send(base, "POST", "/api/cards/aa11/rename", {"uid": "cc33"})
    assert store.version("cc33") and store.version("aa11") is None
    send(base, "PUT", "/api/cards/cc33", {"track": "a.mp3"})  # same track: kept
    assert store.version("cc33")
    send(base, "PUT", "/api/cards/cc33", {"track": "b.mp3"})  # different track: dropped
    assert store.version("cc33") is None
    store.save("cc33", image())
    send(base, "DELETE", "/api/cards/cc33")
    assert store.version("cc33") is None
