"""Per-card artwork, and finding artwork on MusicBrainz / Cover Art Archive.

A card's own artwork is stored as <art_dir>/<uid>.jpg. It never touches
the music files; it only replaces the cover shown for that card.

Every image, uploaded or downloaded, is re-encoded by ffmpeg into a plain
JPEG (at most MAX_SIDE pixels), so only a clean image file is ever stored.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger(__name__)

MAX_UPLOAD = 15 * 1024 * 1024
MAX_SIDE = 800
MBID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# MusicBrainz asks every client to identify itself with a name and a contact.
USER_AGENT = "rpi-music-box/0.1 ( https://github.com/olinkirkland/rpi-music-box )"
MB_SEARCH = "https://musicbrainz.org/ws/2/release-group/"
CAA_FRONT = "https://coverartarchive.org/release-group/{id}/front-{size}"


class ArtworkError(Exception):
    """Something the page can show: bad image, no internet, service busy."""


def _looks_like_image(data: bytes) -> bool:
    return (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"
            or data[:6] in (b"GIF87a", b"GIF89a") or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"))


def to_jpeg(data: bytes, max_side: int = MAX_SIDE) -> bytes:
    """Re-encode an uploaded/downloaded image as a JPEG, or raise ArtworkError."""
    if not data:
        raise ArtworkError("the image is empty")
    if len(data) > MAX_UPLOAD:
        raise ArtworkError("the image is larger than 15 MB")
    if not _looks_like_image(data):
        raise ArtworkError("that isn't a JPEG, PNG, GIF or WebP image")
    try:
        r = subprocess.run(
            # Only read from the pipe, only as a still image: ffmpeg can't be
            # led to open other files or URLs.
            ["ffmpeg", "-v", "error", "-protocol_whitelist", "pipe", "-f", "image2pipe",
             "-i", "pipe:0", "-frames:v", "1",
             "-vf", f"scale='min({max_side},iw)':'min({max_side},ih)':force_original_aspect_ratio=decrease",
             "-q:v", "3", "-f", "mjpeg", "pipe:1"],
            input=data, capture_output=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ArtworkError(f"couldn't process the image: {e}") from None
    if r.returncode != 0 or not r.stdout.startswith(b"\xff\xd8"):
        raise ArtworkError("couldn't read that image")
    return r.stdout


class ArtStore:
    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def path(self, uid: str) -> Path:
        return self.folder / f"{uid}.jpg"

    def version(self, uid: str) -> int | None:
        """Changes whenever the artwork does (for cache-busting URLs)."""
        try:
            return self.path(uid).stat().st_mtime_ns
        except OSError:
            return None

    def save(self, uid: str, data: bytes) -> int:
        """Store `data` (any supported image) as the card's artwork."""
        jpeg = to_jpeg(data)
        self.folder.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.folder, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(jpeg)
            os.replace(tmp, self.path(uid))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return self.version(uid)

    def remove(self, uid: str) -> bool:
        try:
            self.path(uid).unlink()
            return True
        except FileNotFoundError:
            return False

    def rename(self, old: str, new: str) -> None:
        if self.path(old).exists():
            os.replace(self.path(old), self.path(new))


class MusicBrainz:
    """Search release groups and fetch their art, within MusicBrainz's rules
    (one request a second, identified by USER_AGENT)."""

    def __init__(self, cache_dir: Path, opener=None) -> None:
        self.cache_dir = cache_dir
        self._open = opener or urllib.request.urlopen
        self._lock = threading.Lock()
        self._last = 0.0

    def _fetch(self, url: str, timeout: float = 15) -> tuple[int, bytes]:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with self._open(req, timeout=timeout) as r:
                return r.status, r.read(MAX_UPLOAD + 1)
        except urllib.error.HTTPError as e:
            return e.code, b""
        except (urllib.error.URLError, OSError) as e:
            raise ArtworkError("couldn't reach the internet from the box "
                               f"({getattr(e, 'reason', e)})") from None

    def _search_request(self, query: str, limit: int) -> list[dict]:
        url = MB_SEARCH + "?" + urllib.parse.urlencode({"query": query, "fmt": "json", "limit": limit})
        for attempt in range(3):
            with self._lock:  # at most one request a second
                wait = self._last + 1.1 - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                status, body = self._fetch(url)
                self._last = time.monotonic()
            try:
                data = json.loads(body or b"{}")
            except ValueError:
                data = {}
            if status == 200 and "release-groups" in data:
                return data["release-groups"]
            busy = status == 503 or "busy" in str(data.get("error", "")).lower()
            if not busy:
                raise ArtworkError(f"MusicBrainz answered with an error ({status})")
            time.sleep(1.5 * (attempt + 1))
        raise ArtworkError("MusicBrainz is busy right now; try again in a moment")

    @staticmethod
    def _escape(text: str) -> str:
        return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", " ".join(text.split()))

    def search(self, title: str, artist: str | None = None, limit: int = 12) -> list[dict]:
        """Release groups matching title (and artist, if given) that have art."""
        title = (title or "").strip()
        if not title:
            raise ArtworkError("type something to search for")
        query = f"releasegroup:({self._escape(title)})"
        groups = []
        if artist and artist.strip():
            groups = self._search_request(f"{query} AND artist:({self._escape(artist)})", limit)
        if not groups:
            groups = self._search_request(query, limit)
        results = [{
            "id": g["id"],
            "title": g.get("title", ""),
            "artist": "".join(a.get("name", "") + a.get("joinphrase", "") for a in g.get("artist-credit", [])),
            "year": (g.get("first-release-date") or "")[:4] or None,
            "type": g.get("primary-type"),
        } for g in groups if MBID_RE.match(g.get("id", ""))]
        # Keep only the ones with a cover, checking a few at a time.
        with ThreadPoolExecutor(max_workers=4) as pool:
            has_art = list(pool.map(lambda r: self.thumbnail(r["id"]) is not None, results))
        return [r for r, ok in zip(results, has_art) if ok]

    def thumbnail(self, mbid: str) -> Path | None:
        """A cached 250 px cover for a release group, or None if it has none."""
        if not MBID_RE.match(mbid):
            return None
        out = self.cache_dir / f"{mbid}-250.jpg"
        none = self.cache_dir / f"{mbid}-250.none"
        if out.exists():
            return out
        if none.exists() and time.time() - none.stat().st_mtime < 86400:
            return None
        try:
            status, body = self._fetch(CAA_FRONT.format(id=mbid, size=250), timeout=20)
        except ArtworkError:
            return None
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if status != 200:
            none.touch()  # no art; don't ask again for a day
            return None
        try:
            jpeg = to_jpeg(body, max_side=250)
        except ArtworkError:
            none.touch()
            return None
        fd, tmp = tempfile.mkstemp(dir=self.cache_dir, suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            f.write(jpeg)
        os.replace(tmp, out)
        return out

    def front(self, mbid: str) -> bytes:
        """The full (500 px) front cover, for saving as a card's artwork."""
        if not MBID_RE.match(mbid):
            raise ArtworkError("not a MusicBrainz ID")
        status, body = self._fetch(CAA_FRONT.format(id=mbid, size=500), timeout=30)
        if status != 200 or not body:
            raise ArtworkError("couldn't download that cover")
        return body
