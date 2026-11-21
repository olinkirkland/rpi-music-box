"""Reading facts about audio files: length, tags, cover art. No playback here.

Everything goes through ffprobe/ffmpeg and is cached by path, size and
modification time, so an edited file is read again and nothing else is.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

COVER_NAMES = ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.png")


def _stat_key(path: Path) -> tuple[str, int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return str(path), st.st_size, st.st_mtime_ns


def track_info(path: Path) -> dict:
    """{"title", "artist", "album", "duration"} from the file's tags; any may be None."""
    key = _stat_key(path)
    if key is None:
        return {"title": None, "artist": None, "album": None, "duration": None}
    return dict(_probe(*key))


@lru_cache(maxsize=2048)
def _probe(path: str, size: int, mtime_ns: int) -> dict:
    # size and mtime are part of the cache key so an edited file is re-read.
    info = {"title": None, "artist": None, "album": None, "duration": None}
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path],
            capture_output=True, text=True, timeout=10,
        )
        fmt = json.loads(r.stdout or "{}").get("format", {})
    except (OSError, subprocess.TimeoutExpired, ValueError):
        log.debug("could not read %s", path)
        return info
    tags = {k.lower(): v.strip() for k, v in (fmt.get("tags") or {}).items()
            if isinstance(v, str) and v.strip()}
    info.update(title=tags.get("title"), artist=tags.get("artist") or tags.get("album_artist"),
                album=tags.get("album"))
    try:
        info["duration"] = float(fmt["duration"])
    except (KeyError, TypeError, ValueError):
        pass
    return info


def track_duration(path: Path) -> float | None:
    """Length of an audio file in seconds, or None if unknown."""
    return track_info(path)["duration"]


def display_name(track: str) -> str:
    """A readable name from a track path: file name without its extension."""
    return Path(track).stem


def describe(target: Path, files: list[Path]) -> dict:
    """How to show what a card plays: {"name", "artist", "parts"}.

    A single file: its title tag, else its file name. A folder: the album
    tag of its first file, else the folder's name.
    """
    first = track_info(files[0]) if files else {}
    if target.is_dir():
        name = first.get("album") or target.name
    else:
        name = first.get("title") or target.stem
    return {"name": name, "artist": first.get("artist"), "parts": len(files)}


def describe_tracks(files: list[Path]) -> dict:
    """How to show a card's tracks: {"name", "artist", "parts"}.

    One track: its title tag, else its file name. Several from one album:
    the album. Otherwise: the first title "+ N more".
    """
    if not files:
        return {"name": "", "artist": None, "parts": 0}
    infos = [track_info(f) for f in files]
    first = infos[0]
    first_title = first["title"] or files[0].stem
    if len(files) == 1:
        name = first_title
    elif first["album"] and all(i["album"] == first["album"] for i in infos):
        name = first["album"]
    else:
        name = f"{first_title} + {len(files) - 1} more"
    artists = {i["artist"] for i in infos if i["artist"]}
    artist = first["artist"] if len(artists) <= 1 else "Various artists"
    return {"name": name, "artist": artist, "parts": len(files)}


def part_title(path: Path) -> str:
    return track_info(path)["title"] or path.stem


def cover_art(target: Path, files: list[Path], cache_dir: Path) -> Path | None:
    """A JPEG or PNG of the cover for a file or folder, or None.

    A cover image file in the folder wins; otherwise the picture embedded in
    the first audio file is extracted once and cached.
    """
    folder = target if target.is_dir() else target.parent
    for name in COVER_NAMES:
        candidate = folder / name
        if candidate.is_file():
            return candidate
    if not files:
        return None
    key = _stat_key(files[0])
    if key is None:
        return None
    digest = hashlib.sha1(repr(key).encode()).hexdigest()
    out = cache_dir / f"{digest}.jpg"
    none_marker = cache_dir / f"{digest}.none"
    if out.exists():
        return out
    if none_marker.exists():
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=cache_dir, suffix=".tmp.jpg")
    os.close(fd)
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(files[0]), "-an", "-map", "0:v:0",
             "-frames:v", "1", "-vf", "scale='min(600,iw)':-2", tmp],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode != 0 or os.path.getsize(tmp) == 0:
            none_marker.touch()  # no embedded picture; don't try again
            return None
        os.replace(tmp, out)
        return out
    except (OSError, subprocess.TimeoutExpired):
        log.debug("could not extract cover art from %s", files[0])
        return None
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
