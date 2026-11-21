import json
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from musicbox.taplog import TapLog
from musicbox.web import start_web


@pytest.fixture
def server(tmp_path):
    taps = TapLog(tmp_path / "taps.db")
    srv = start_web(taps, host="127.0.0.1", port=0)
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
