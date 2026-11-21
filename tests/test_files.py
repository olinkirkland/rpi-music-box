"""The file manager: safe paths, uploads, renames and deletes that keep cards in step."""

import io
import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from musicbox.cards import CardStore
from musicbox.events import EventBus
from musicbox.files import FileError, FileManager, clean_filename
from musicbox.taplog import TapLog
from musicbox.web import start_web


@pytest.fixture
def fm(tmp_path):
    music = tmp_path / "music"
    (music / "Story").mkdir(parents=True)
    (music / "Story" / "1.mp3").write_bytes(b"one")
    (music / "Story" / "2.mp3").write_bytes(b"two")
    (music / "song.mp3").write_bytes(b"song")
    (music / ".hidden").write_text("x")
    (tmp_path / "secret.txt").write_text("outside")
    cards = CardStore(tmp_path / "cards.json", music)
    cards.assign("aa11", [music / "Story" / "1.mp3", music / "Story" / "2.mp3"])
    cards.assign("bb22", music / "song.mp3")
    # /tmp on the Pi is a small RAM disk; the real 500 MB reserve is tested below.
    return FileManager(music, cards, keep_free=1024), cards, tmp_path


@pytest.mark.parametrize("bad", ["../secret.txt", "Story/../../secret.txt", ".hidden", "Story/./1.mp3",
                                 "a\x00b"])
def test_paths_cannot_leave_the_music_folder(fm, bad):
    manager, cards, tmp = fm
    with pytest.raises(FileError):
        manager.resolve(bad)


def test_symlinks_out_are_refused_and_hidden_from_listing(fm):
    manager, cards, tmp = fm
    os.symlink(tmp / "secret.txt", manager.root / "link.mp3")
    with pytest.raises(FileError):
        manager.resolve("link.mp3")
    names = [f["name"] for f in manager.list("")["files"]]
    assert "link.mp3" not in names and ".hidden" not in names


def test_listing(fm):
    manager, cards, tmp = fm
    root = manager.list("")
    assert [f["name"] for f in root["folders"]] == ["Story"]
    assert root["folders"][0]["used_by"] == ["aa11"] and root["folders"][0]["items"] == 2
    assert root["files"] == [{"name": "song.mp3", "path": "song.mp3", "size": 4,
                              "modified": root["files"][0]["modified"], "kind": "audio", "used_by": ["bb22"]}]
    story = manager.list("Story")
    assert [c["path"] for c in story["crumbs"]] == ["", "Story"] and story["disk"]["free"] > 0


def test_upload(fm):
    manager, cards, tmp = fm
    assert manager.upload("Story", "3.mp3", io.BytesIO(b"three"), 5) == {"path": "Story/3.mp3", "replaced": False}
    assert (manager.root / "Story" / "3.mp3").read_bytes() == b"three"
    with pytest.raises(FileError) as e:
        manager.upload("Story", "3.mp3", io.BytesIO(b"new"), 3)
    assert e.value.code == 409
    assert manager.upload("Story", "3.mp3", io.BytesIO(b"new"), 3, overwrite=True)["replaced"]
    for name, code in (("run.sh", 415), ("evil.py", 415), ("../x.mp3", None), (".x.mp3", 400)):
        with pytest.raises(FileError) as e:
            manager.upload("", name, io.BytesIO(b"x"), 1)
        if code:
            assert e.value.code == code


def test_upload_refused_when_the_card_is_nearly_full(fm):
    manager, cards, tmp = fm
    manager.keep_free = 10 ** 15  # more than any disk has
    with pytest.raises(FileError) as e:
        manager.upload("", "big.mp3", io.BytesIO(b"x"), 1)
    assert e.value.code == 507 and not (manager.root / "big.mp3").exists()


def test_upload_that_stops_early_leaves_nothing(fm):
    manager, cards, tmp = fm
    with pytest.raises(FileError):
        manager.upload("", "cut.mp3", io.BytesIO(b"abc"), 10)
    assert not (manager.root / "cut.mp3").exists()
    assert not [p for p in manager.root.iterdir() if p.name.startswith(".upload-")]


def test_rename_file_and_folder_update_cards(fm):
    manager, cards, tmp = fm
    assert manager.rename("song.mp3", "Song.mp3") == {"path": "Song.mp3", "cards": ["bb22"]}
    assert cards.entries()["bb22"]["tracks"] == ["Song.mp3"]
    # The extension never changes: typed or not, it's kept.
    assert manager.rename("Song.mp3", "Tune")["path"] == "Tune.mp3"
    assert manager.rename("Tune.mp3", "Song.MP3")["path"] == "Song.mp3"
    assert manager.rename("Song.mp3", "Song.wav")["path"] == "Song.wav.mp3"
    assert cards.entries()["bb22"]["tracks"] == ["Song.wav.mp3"]
    with pytest.raises(FileError):
        manager.rename("Song.wav.mp3", ".mp3")  # no name left
    manager.rename("Song.wav.mp3", "Song")
    assert manager.rename("Story", "Snow White")["cards"] == ["aa11"]
    assert cards.entries()["aa11"]["tracks"] == ["Snow White/1.mp3", "Snow White/2.mp3"]
    with pytest.raises(FileError) as e:
        manager.rename("Snow White", "Song.mp3")
    assert e.value.code == 409


def test_delete(fm):
    manager, cards, tmp = fm
    assert manager.delete("Story/1.mp3") == {"path": "Story/1.mp3", "cards": ["aa11"]}
    assert cards.entries()["aa11"]["tracks"] == ["Story/2.mp3"]
    manager.delete("song.mp3")  # its card's only track: the card keeps it and shows "missing"
    assert cards.entries()["bb22"]["tracks"] == ["song.mp3"]
    with pytest.raises(FileError) as e:
        manager.delete("Story")
    assert e.value.code == 409  # not empty
    manager.delete("Story/2.mp3")
    manager.delete("Story")
    assert not (manager.root / "Story").exists()
    with pytest.raises(FileError):
        manager.delete("")


def test_make_folder(fm):
    manager, cards, tmp = fm
    assert manager.make_folder("Story", "Extras") == "Story/Extras"
    with pytest.raises(FileError):
        manager.make_folder("", "Story")
    with pytest.raises(FileError):
        manager.make_folder("", "a/b")


@pytest.mark.parametrize("name", ["", " ", ".", "..", ".dot", "a/b", "a\\b", "x" * 201, "bad:name"])
def test_clean_filename_rejects(name):
    with pytest.raises(FileError):
        clean_filename(name)


# --- web API ----------------------------------------------------------------------

@pytest.fixture
def api(fm):
    manager, cards, tmp = fm
    srv = start_web(TapLog(tmp / "t.db"), cards, EventBus(), host="127.0.0.1", port=0,
                    files=manager, paths={"music_dir": str(manager.root)})
    yield f"http://127.0.0.1:{srv.server_address[1]}", manager, cards
    srv.shutdown()
    srv.server_close()


def call(base, method, path, body=None, ctype="application/json", headers=None):
    data = json.dumps(body).encode() if ctype == "application/json" and body is not None else body
    req = Request(base + path, data=data, method=method, headers={"Content-Type": ctype, **(headers or {})})
    try:
        with urlopen(req) as r:
            return r.status, json.load(r)
    except HTTPError as e:
        return e.code, json.load(e)


def test_files_api(api):
    base, manager, cards = api
    code, data = call(base, "GET", "/api/files?path=Story", ctype="text/plain")
    assert code == 200 and [f["name"] for f in data["files"]] == ["1.mp3", "2.mp3"]
    assert call(base, "GET", "/api/files?path=../", ctype="text/plain")[0] == 400
    assert call(base, "PUT", "/api/files/upload?path=Story&name=3.mp3", b"abc", "application/octet-stream")[0] == 200
    assert (manager.root / "Story" / "3.mp3").read_bytes() == b"abc"
    assert call(base, "PUT", "/api/files/upload?path=&name=x.mp3", b"abc", "text/plain")[0] == 415
    assert call(base, "POST", "/api/files/folder", {"path": "", "name": "New"})[1] == {"path": "New"}
    assert call(base, "POST", "/api/files/rename", {"path": "song.mp3", "name": "Tune.mp3"})[1]["cards"] == ["bb22"]
    assert call(base, "DELETE", "/api/files?path=New", ctype="text/plain")[0] == 200
    assert call(base, "GET", "/api/paths", ctype="text/plain")[1] == {"music_dir": str(manager.root)}


def test_files_api_refuses_other_websites(api):
    base, manager, cards = api
    evil = {"Origin": "http://evil.example"}
    assert call(base, "DELETE", "/api/files?path=song.mp3", ctype="text/plain", headers=evil)[0] == 403
    assert call(base, "PUT", "/api/files/upload?path=&name=x.mp3", b"abc", "application/octet-stream", evil)[0] == 403
    assert call(base, "POST", "/api/files/rename", {"path": "song.mp3", "name": "x.mp3"}, headers=evil)[0] == 403
    assert (manager.root / "song.mp3").exists() and not (manager.root / "x.mp3").exists()
