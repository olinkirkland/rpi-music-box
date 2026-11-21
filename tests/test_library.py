"""Folder cards, tag names, cover art, and following a folder's parts."""

import json
import shutil
import subprocess
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from musicbox.box import MusicBox
from musicbox.cards import CardStore
from musicbox.events import EventBus
from musicbox.media import cover_art, describe, track_info
from musicbox.taplog import TapLog
from musicbox.web import library, start_web

from test_box import FakeReader

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture
def lib(tmp_path):
    music = tmp_path / "music"
    story = music / "stories" / "Snow White"
    story.mkdir(parents=True)
    for name in ("Part 10.mp3", "Part 2.mp3", "Part 1.mp3", ".hidden.mp3", "notes.txt"):
        (story / name).write_bytes(b"")
    (music / "song.mp3").write_bytes(b"")
    (music / "empty").mkdir()
    return CardStore(tmp_path / "cards.json", music)


def test_folders_and_natural_order(lib):
    assert lib.folders() == ["stories/Snow White"]
    files = lib.files_for(lib.music_dir / "stories/Snow White")
    assert [f.name for f in files] == ["Part 1.mp3", "Part 2.mp3", "Part 10.mp3"]
    assert lib.files_for(lib.music_dir / "song.mp3") == [lib.music_dir / "song.mp3"]
    assert lib.files_for(lib.music_dir / "empty") == []
    # Cards play files only now; folders are not offered.
    assert "stories/Snow White" not in lib.playable() and "song.mp3" in lib.playable()


def test_assigning_a_folder_adds_its_files_in_order(lib):
    assert lib.assign("aa", lib.music_dir / "stories/Snow White") == [
        "stories/Snow White/Part 1.mp3", "stories/Snow White/Part 2.mp3", "stories/Snow White/Part 10.mp3"]
    with pytest.raises(FileNotFoundError):
        lib.assign("bb", lib.music_dir / "empty")


def test_old_folder_cards_load_as_track_lists_and_migrate(lib):
    lib.cards_file.write_text(json.dumps({"aa": "stories/Snow White",
                                          "bb": {"track": "song.mp3", "name": "Mine"}}))
    assert lib.entries() == {
        "aa": {"tracks": ["stories/Snow White/Part 1.mp3", "stories/Snow White/Part 2.mp3",
                          "stories/Snow White/Part 10.mp3"]},
        "bb": {"tracks": ["song.mp3"], "name": "Mine"}}
    assert lib.migrate() is True
    assert json.loads(lib.cards_file.read_text()) == {
        "aa": {"tracks": ["stories/Snow White/Part 1.mp3", "stories/Snow White/Part 2.mp3",
                          "stories/Snow White/Part 10.mp3"]},
        "bb": {"tracks": ["song.mp3"], "name": "Mine"}}
    assert lib.migrate() is False  # nothing left to convert


def test_several_tracks_in_any_order(lib):
    m = lib.music_dir
    assert lib.assign("aa", [m / "song.mp3", m / "stories/Snow White/Part 2.mp3"]) == [
        "song.mp3", "stories/Snow White/Part 2.mp3"]
    assert lib.tracks_for("aa") == [m / "song.mp3", m / "stories/Snow White/Part 2.mp3"]
    with pytest.raises(ValueError):
        lib.assign("bb", [])


def test_names_fall_back_to_file_and_folder_names(lib):
    folder = lib.music_dir / "stories/Snow White"
    assert describe(folder, lib.files_for(folder)) == {"name": "Snow White", "artist": None, "parts": 3}
    song = lib.music_dir / "song.mp3"
    assert describe(song, [song])["name"] == "song"


def make_tagged_mp3(path, title, album, artist, with_cover):
    """A one-second silent MP3 with tags (and optionally a cover picture). Never played."""
    run = lambda cmd: subprocess.run(["ffmpeg", "-y", "-v", "error", *cmd], check=True, capture_output=True)
    cmd = ["-f", "lavfi", "-t", "1", "-i", "anullsrc=r=8000:cl=mono"]
    if with_cover:
        picture = path.with_suffix(".cover.jpg")
        run(["-f", "lavfi", "-i", "color=c=blue:s=32x32", "-frames:v", "1", str(picture)])
        cmd += ["-i", str(picture), "-map", "0:a", "-map", "1:v", "-c:v", "copy",
                "-disposition:v", "attached_pic"]
    cmd += ["-c:a", "libmp3lame", "-metadata", f"title={title}", "-metadata", f"album={album}",
            "-metadata", f"artist={artist}", "-id3v2_version", "3", str(path)]
    run(cmd)
    if with_cover:
        picture.unlink()


@needs_ffmpeg
def test_tags_and_cover_art(tmp_path):
    folder = tmp_path / "Story"
    folder.mkdir()
    one, two = folder / "1.mp3", folder / "2.mp3"
    make_tagged_mp3(one, "Story - Side 1", "The Story", "A Narrator", with_cover=True)
    make_tagged_mp3(two, "Story - Side 2", "The Story", "A Narrator", with_cover=False)
    info = track_info(one)
    assert (info["title"], info["album"], info["artist"]) == ("Story - Side 1", "The Story", "A Narrator")
    assert 0.5 < info["duration"] < 1.5
    assert describe(folder, [one, two]) == {"name": "The Story", "artist": "A Narrator", "parts": 2}
    cache = tmp_path / "covers"
    art = cover_art(folder, [one, two], cache)
    assert art is not None and art.read_bytes()[:2] == b"\xff\xd8"  # a JPEG
    assert cover_art(folder, [one, two], cache) == art  # cached
    assert cover_art(two, [two], cache) is None
    assert cover_art(two, [two], cache) is None and list(cache.glob("*.none"))  # not retried
    (folder / "cover.png").write_bytes(b"png")
    assert cover_art(folder, [one, two], cache) == folder / "cover.png"


class PartsPlayer:
    """Plays a list of files; reports which one is playing like mpv would."""
    chime_seconds = 0.0
    controllable = True
    playing = True

    def __init__(self):
        self.played = None
        self.state = None

    def play(self, tracks):
        self.played = tracks
        return True

    def stop(self):
        pass

    def cue(self):
        pass

    def check(self):
        pass

    def playlist_state(self):
        return self.state


def test_folder_card_plays_every_part_and_follows_along(lib, monkeypatch):
    lib.assign("aa", lib.music_dir / "stories/Snow White")
    player = PartsPlayer()
    bus = EventBus()
    reader = FakeReader([None, "aa"])
    clock = {"t": 100.0}
    box = MusicBox(reader, player, lib, events=bus, clock=lambda: clock["t"],
                   duration_of=lambda p: 60.0)
    box.tick()
    box.tick()
    assert [p.name for p in player.played] == ["Part 1.mp3", "Part 2.mp3", "Part 10.mp3"]
    assert box.now["parts"] == 3 and box.now["part"] == 0
    assert box.now["cover"] == "stories/Snow White/Part 1.mp3"  # the first track's art

    monkeypatch.setattr("musicbox.box.time.monotonic", lambda: 1e9)  # past the 1 s throttle
    player.state = (1, 12.0)
    clock["t"] = 500.0
    reader.reads.append("aa")
    box.tick()
    assert box.now["part"] == 1 and box.now["part_title"] == "Part 2"
    assert box.now["song_starts_at"] == 500.0 - 12.0 and box.now["duration"] == 60.0


@pytest.fixture
def api(tmp_path, lib):
    srv = start_web(TapLog(tmp_path / "t.db"), lib, EventBus(), host="127.0.0.1", port=0,
                    cover_dir=tmp_path / "covers")
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_library_endpoint_lists_songs_only(api, lib):
    with urlopen(api + "/api/library") as r:
        items = json.load(r)
    assert {i["kind"] for i in items} == {"file"}
    assert [i["path"] for i in items] == lib.tracks()
    assert items == library(lib)


def test_cards_take_an_ordered_track_list_over_the_api(api, lib):
    from urllib.request import Request
    body = {"tracks": ["stories/Snow White/Part 2.mp3", "song.mp3"], "repeat": True}
    req = Request(api + "/api/cards/aa11", method="PUT", data=json.dumps(body).encode(),
                  headers={"Content-Type": "application/json"})
    with urlopen(req) as r:
        assert json.load(r)["tracks"] == body["tracks"]
    with urlopen(api + "/api/cards") as r:
        (card,) = json.load(r)
    assert card["tracks"] == body["tracks"] and card["kind"] == "list" and card["parts"] == 2
    assert card["repeat"] is True and card["shuffle"] is False
    for bad in ({"tracks": ["stories/Snow White"]}, {"tracks": []}, {"tracks": ["../x.mp3"]},
                {"tracks": ["song.mp3"], "shuffle": "yes"}):
        req = Request(api + "/api/cards/aa11", method="PUT", data=json.dumps(bad).encode(),
                      headers={"Content-Type": "application/json"})
        with pytest.raises(HTTPError) as e:
            urlopen(req)
        assert e.value.code == 400, bad


@pytest.mark.parametrize("path", ["", "../cards.json", "/etc/passwd", "empty", "nope.mp3",
                                  "stories/Snow White/notes.txt"])
def test_cover_endpoint_only_serves_library_items(api, path):
    from urllib.parse import quote
    with pytest.raises(HTTPError) as e:
        urlopen(api + "/api/cover?path=" + quote(path))
    assert e.value.code == 404
