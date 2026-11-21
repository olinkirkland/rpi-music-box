"""The web page: what's playing, the card list, and the tap log.

Runs in a background thread of `musicbox run`, using only the standard
library. The page itself is static files in musicbox/static (Vue and Pico
CSS are vendored there, so there is no build step and no internet needed).

    GET    /                  the page
    GET    /static/<path>     page assets
    GET    /api/events        live updates (Server-Sent Events): now, tap, cards
    GET    /api/now           what's playing right now
    GET    /api/taps          tap log, newest first (?limit=N)
    GET    /api/taps/search   ?q=<text>&page=<n>: {rows, total, page, pages, per_page}
    GET    /api/cards         [{uid, track, missing}]
    GET    /api/tracks        audio files in music_dir
    GET    /api/library       [{path, kind: "file", name, artist, album, parts}]
    GET    /api/cover?path=P  cover art for a file or folder in the library
    PUT    /api/cards/<uid>   {"tracks": ["<path>", ...], "name"?, "shuffle"?, "repeat"?}
    DELETE /api/cards/<uid>
    POST   /api/player/pause   {}
    POST   /api/player/resume  {}
    POST   /api/player/seek    {"position": <seconds into the song>}
    POST   /api/player/next    {}
    POST   /api/player/previous  {}
    POST   /api/player/shuffle {"on": true | false}   (also saved on the card)
    POST   /api/player/repeat  {"on": true | false}   (also saved on the card)
    POST   /api/cards/<uid>/rename  {"uid": "<new card ID>"}
    GET    /api/cards/<uid>/art     the card's own artwork (JPEG)
    PUT    /api/cards/<uid>/art     upload artwork (body: the image, Content-Type image/*)
    POST   /api/cards/<uid>/art/musicbrainz  {"id": "<release group MBID>"}
    DELETE /api/cards/<uid>/art
    GET    /api/artwork/search?q=<title>&artist=<artist>   MusicBrainz covers
    GET    /api/artwork/thumb/<MBID>
    POST   /api/assign/start   read taps silently for the Assign modal (renew every few s)
    POST   /api/assign/stop
    GET    /api/files?path=P   a folder in the music folder: {folders, files, crumbs, disk}
    POST   /api/files/folder   {"path": <parent>, "name": <new folder>}
    PUT    /api/files/upload?path=<folder>&name=<file>[&overwrite=1]   body: the file
    POST   /api/files/rename   {"path": <file or folder>, "name": <new name>}
    DELETE /api/files?path=P   a file, or an empty folder
    GET    /api/paths          where things live on the Pi (for troubleshooting)
    GET    /api/volume         {level, steps, available}
    POST   /api/volume         {"level": 0 | 20 | 40 | 60 | 80 | 100}

Nothing here can start a sound: a song only ever starts from a card tap.
Pause/resume/seek only act on the song from the current tap while its
card is on the reader (MusicBox.control enforces that).
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .artwork import MBID_RE, MAX_UPLOAD, ArtworkError
from .cards import MAX_TRACKS, clean_name
from .files import FileError
from .media import cover_art, describe_tracks, track_info
from .player import ControlError

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
UID_RE = re.compile(r"^[0-9a-f]{4,32}$")
MAX_BODY = 16 * 1024
KEEPALIVE = 15  # seconds between SSE comments, so dead connections are noticed

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".md": "text/plain; charset=utf-8",
}


class ApiError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def library(cards) -> list[dict]:
    """Every audio file, with its display name (cards no longer use folders)."""
    out = []
    for rel in cards.tracks():
        path = cards.music_dir / rel
        info = track_info(path)
        out.append({"path": rel, "kind": "file", "name": info["title"] or path.stem,
                    "artist": info["artist"], "album": info["album"], "parts": 1})
    return out


def make_handler(taplog, cards, events=None, controller=None, cover_dir=None,
                 art=None, musicbrainz=None, files=None, paths=None):
    write_lock = threading.Lock()
    ART_SUB = "/art"

    def now_state() -> dict:
        now = events.now if events is not None else {"state": "idle"}
        return {**now, "server_time": time.time()}

    class Handler(BaseHTTPRequestHandler):
        server_version = "musicbox"
        # A stalled upload (or a client that went quiet) gives up instead of
        # holding a thread forever. The live-events stream only writes, and
        # sends a keep-alive every KEEPALIVE seconds, so it isn't affected.
        timeout = 120

        # --- routing -------------------------------------------------------

        def do_GET(self) -> None:
            self._handle(self._get)

        def do_PUT(self) -> None:
            self._handle(self._put)

        def do_DELETE(self) -> None:
            self._handle(self._delete)

        def do_POST(self) -> None:
            self._handle(self._post)

        def _handle(self, method) -> None:
            try:
                method(urlparse(self.path))
            except ApiError as e:
                self._json(e.code, {"error": str(e)})
            except FileError as e:
                self._json(e.code, {"error": str(e)})
            except (BrokenPipeError, ConnectionResetError):
                pass  # the browser went away
            except Exception:
                log.exception("web request failed: %s %s", self.command, self.path)
                self._json(500, {"error": "internal error"})

        def _get(self, url) -> None:
            path = url.path
            if path == "/":
                self._static("index.html")
            elif path.startswith("/static/"):
                self._static(unquote(path[len("/static/"):]))
            elif path == "/api/events":
                self._event_stream()
            elif path == "/api/now":
                self._json(200, now_state())
            elif path == "/api/volume":
                if controller is None:
                    raise ApiError(503, "volume control is not available")
                self._json(200, controller.volume_state())
            elif path == "/api/taps/search":
                query = parse_qs(url.query)
                try:
                    page = max(1, int(query.get("page", ["1"])[0]))
                except ValueError:
                    page = 1
                per_page = 100
                rows, total = taplog.search(query.get("q", [""])[0][:200], per_page, (page - 1) * per_page)
                pages = max(1, -(-total // per_page))
                self._json(200, {"rows": rows, "total": total, "page": page, "pages": pages,
                                 "per_page": per_page})
            elif path == "/api/taps":
                try:
                    limit = int(parse_qs(url.query).get("limit", ["500"])[0])
                except ValueError:
                    limit = 500
                self._json(200, taplog.recent(max(1, min(limit, 5000))))
            elif path == "/api/cards":
                self._json(200, self._card_list())
            elif path == "/api/tracks":
                self._json(200, cards.tracks())
            elif path == "/api/library":
                self._json(200, library(cards))
            elif path == "/api/cover":
                self._cover(parse_qs(url.query).get("path", [""])[0])
            elif path.startswith("/api/cards/") and path.endswith(ART_SUB):
                uid = self._card_uid(urlparse(path[: -len(ART_SUB)]))
                if art is None or not art.path(uid).is_file():
                    raise ApiError(404, "no artwork for this card")
                self._send(200, "image/jpeg", art.path(uid).read_bytes(), "private, max-age=31536000")
            elif path == "/api/files":
                self._json(200, self._files().list(parse_qs(url.query).get("path", [""])[0]))
            elif path == "/api/paths":
                self._json(200, paths or {})
            elif path == "/api/artwork/search":
                self._artwork_search(parse_qs(url.query))
            elif path.startswith("/api/artwork/thumb/"):
                mbid = path[len("/api/artwork/thumb/"):]
                thumb = musicbrainz.thumbnail(mbid) if musicbrainz and MBID_RE.match(mbid) else None
                if thumb is None:
                    raise ApiError(404, "no cover")
                self._send(200, "image/jpeg", thumb.read_bytes(), "private, max-age=86400")
            else:
                raise ApiError(404, "not found")

        def _put(self, url) -> None:
            if url.path.startswith("/api/cards/") and url.path.endswith(ART_SUB):
                return self._upload_art(url)
            if url.path == "/api/files/upload":
                return self._upload_file(url)
            uid = self._card_uid(url)
            self._check_write()
            body = self._read_json()
            if not isinstance(body, dict):
                raise ApiError(400, "expected a JSON object")
            tracks = body.get("tracks")
            if tracks is None and isinstance(body.get("track"), str):
                tracks = [body["track"]]  # the older one-track form
            known = cards.playable()
            if (not isinstance(tracks, list) or not tracks or len(tracks) > MAX_TRACKS
                    or not all(isinstance(t, str) and t in known for t in tracks)):
                raise ApiError(400, "unknown track; pick tracks from the list")
            # "name": a custom name, or ""/null to use the tags. Left out:
            # kept for the same tracks, dropped for different ones.
            kwargs = {}
            if "name" in body:
                try:
                    kwargs["name"] = clean_name(body["name"])
                except ValueError as e:
                    raise ApiError(400, str(e)) from None
            for flag in ("shuffle", "repeat"):
                if flag in body:
                    if not isinstance(body[flag], bool):
                        raise ApiError(400, f"{flag} must be true or false")
                    kwargs[flag] = body[flag]
            with write_lock:
                previous = cards.entries().get(uid, {}).get("tracks")
                cards.assign(uid, [cards.music_dir / t for t in tracks], **kwargs)
                if art is not None and previous is not None and previous != tracks:
                    art.remove(uid)  # the old tracks' artwork wouldn't fit
            name = cards.custom_name(uid)
            log.info("web: card %s -> %s%s", uid, tracks[0] if len(tracks) == 1 else f"{len(tracks)} tracks",
                     f' ("{name}")' if name else "")
            self._cards_changed()
            self._json(200, {"uid": uid, "tracks": tracks, "track": tracks[0], "name": name,
                             **cards.settings(uid)})

        def _delete(self, url) -> None:
            if url.path == "/api/files":
                self._check_write()
                with write_lock:
                    result = self._files().delete(parse_qs(url.query).get("path", [""])[0])
                log.info("web: deleted %s from the music folder", result["path"])
                self._cards_changed()
                return self._json(200, result)
            if url.path.startswith("/api/cards/") and url.path.endswith(ART_SUB):
                uid = self._art_card(url.path[: -len(ART_SUB)])
                self._check_write()
                removed = art.remove(uid)
                log.info("web: card %s artwork removed", uid)
                self._cards_changed()
                return self._json(200, {"uid": uid, "removed": removed})
            uid = self._card_uid(url)
            self._check_write()
            with write_lock:
                removed = cards.remove(uid)
            if not removed:
                raise ApiError(404, f"no such card: {uid}")
            if art is not None:
                art.remove(uid)  # a removed card's artwork goes too
            log.info("web: card %s removed", uid)
            self._cards_changed()
            self._json(200, {"uid": uid})

        def _post(self, url) -> None:
            if url.path == "/api/volume":
                return self._post_volume()
            if url.path in ("/api/files/folder", "/api/files/rename"):
                self._check_write()
                body = self._read_json()
                if not isinstance(body, dict):
                    raise ApiError(400, "expected a JSON object")
                with write_lock:
                    if url.path.endswith("folder"):
                        result = {"path": self._files().make_folder(body.get("path", ""), body.get("name"))}
                        log.info("web: new folder %s", result["path"])
                    else:
                        result = self._files().rename(body.get("path", ""), body.get("name"))
                        log.info("web: renamed %s -> %s", body.get("path"), result["path"])
                self._cards_changed()
                return self._json(200, result)
            if url.path in ("/api/assign/start", "/api/assign/stop"):
                self._check_write()
                self._read_json()
                if controller is None:
                    raise ApiError(503, "assigning is not available")
                start = url.path.endswith("start")
                self._json(200, controller.start_assigning() if start else controller.stop_assigning())
                return
            if url.path.startswith("/api/cards/") and url.path.endswith("/rename"):
                return self._rename(url)
            if url.path.startswith("/api/cards/") and url.path.endswith(ART_SUB + "/musicbrainz"):
                return self._art_from_musicbrainz(url)
            prefix = "/api/player/"
            if not url.path.startswith(prefix):
                raise ApiError(404, "not found")
            action = url.path[len(prefix):]
            if action not in ("pause", "resume", "seek", "next", "previous", "shuffle", "repeat"):
                raise ApiError(404, "not found")
            self._check_write()
            body = self._read_json()
            if controller is None:
                raise ApiError(503, "the player can't be controlled right now")
            position = None
            if action == "seek":
                position = body.get("position") if isinstance(body, dict) else None
                if isinstance(position, bool) or not isinstance(position, (int, float)) \
                        or not 0 <= position < 24 * 3600:
                    raise ApiError(400, "position must be a number of seconds")
            value = None
            if action in ("shuffle", "repeat"):
                value = body.get("on") if isinstance(body, dict) else None
                if not isinstance(value, bool):
                    raise ApiError(400, '"on" must be true or false')
            try:
                now = controller.control(action, position, value) if value is not None \
                    else controller.control(action, position)
            except ControlError as e:
                raise ApiError(409, str(e)) from None
            self._json(200, {**now, "server_time": time.time()})

        def _rename(self, url) -> None:
            old = urlparse(url.path[: -len("/rename")])
            uid = self._card_uid(old)
            self._check_write()
            body = self._read_json()
            new = body.get("uid") if isinstance(body, dict) else None
            new = new.strip().lower() if isinstance(new, str) else ""
            if not UID_RE.match(new):
                raise ApiError(400, "card ID must be 4-32 hex characters, like 04a1b2c3")
            with write_lock:
                try:
                    tracks = cards.rename(uid, new)
                except KeyError:
                    raise ApiError(404, f"no such card: {uid}") from None
                except ValueError as e:
                    raise ApiError(409, str(e)) from None
                if art is not None:
                    art.rename(uid, new)
            log.info("web: card %s renamed to %s", uid, new)
            self._cards_changed()
            self._json(200, {"uid": new, "tracks": tracks, "track": tracks[0]})

        # --- file manager ----------------------------------------------------

        def _files(self):
            if files is None:
                raise ApiError(503, "the file manager is not available")
            return files

        def _upload_file(self, url) -> None:
            manager = self._files()
            self._check_write()
            # Not a form or plain-text type: browsers then check with us before
            # another website could send one (a CORS preflight).
            ctype = self.headers.get_content_type()
            if not (ctype == "application/octet-stream" or ctype.startswith(("audio/", "image/"))):
                raise ApiError(415, "send the file itself (application/octet-stream)")
            query = parse_qs(url.query)
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise ApiError(400, "bad Content-Length") from None
            with write_lock:
                result = manager.upload(query.get("path", [""])[0], query.get("name", [""])[0],
                                        self.rfile, length, overwrite=query.get("overwrite", ["0"])[0] == "1")
            log.info("web: uploaded %s (%d bytes)", result["path"], length)
            self._cards_changed()
            self._json(200, result)

        # --- artwork -----------------------------------------------------------

        def _art_card(self, card_path: str) -> str:
            """The uid in /api/cards/<uid>, which must be an assigned card."""
            if art is None:
                raise ApiError(503, "artwork is not available")
            uid = self._card_uid(urlparse(card_path))
            if uid not in cards.all():
                raise ApiError(404, f"no such card: {uid}")
            return uid

        def _upload_art(self, url) -> None:
            uid = self._art_card(url.path[: -len(ART_SUB)])
            self._check_write()
            # An image content type also makes browsers check with us before
            # another website could send one (a CORS preflight).
            if not self.headers.get_content_type().startswith("image/"):
                raise ApiError(415, "expected an image")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise ApiError(400, "bad Content-Length") from None
            if length <= 0:
                raise ApiError(400, "the image is empty")
            if length > MAX_UPLOAD:
                raise ApiError(413, "the image is larger than 15 MB")
            data = self.rfile.read(length)
            try:
                version = art.save(uid, data)
            except ArtworkError as e:
                raise ApiError(400, str(e)) from None
            log.info("web: card %s artwork uploaded", uid)
            self._cards_changed()
            self._json(200, {"uid": uid, "art_version": version})

        def _art_from_musicbrainz(self, url) -> None:
            uid = self._art_card(url.path[: -len(ART_SUB + "/musicbrainz")])
            self._check_write()
            body = self._read_json()
            mbid = body.get("id") if isinstance(body, dict) else None
            if musicbrainz is None:
                raise ApiError(503, "online artwork search is not available")
            if not isinstance(mbid, str) or not MBID_RE.match(mbid):
                raise ApiError(400, "not a MusicBrainz ID")
            try:
                version = art.save(uid, musicbrainz.front(mbid))
            except ArtworkError as e:
                raise ApiError(502, str(e)) from None
            log.info("web: card %s artwork from MusicBrainz %s", uid, mbid)
            self._cards_changed()
            self._json(200, {"uid": uid, "art_version": version})

        def _artwork_search(self, query) -> None:
            if musicbrainz is None:
                raise ApiError(503, "online artwork search is not available")
            title = query.get("q", [""])[0][:200]
            artist = query.get("artist", [""])[0][:200] or None
            try:
                results = musicbrainz.search(title, artist)
            except ArtworkError as e:
                raise ApiError(502, str(e)) from None
            self._json(200, {"results": results})

        def _post_volume(self) -> None:
            self._check_write()
            body = self._read_json()
            if controller is None:
                raise ApiError(503, "volume control is not available")
            level = body.get("level") if isinstance(body, dict) else None
            if isinstance(level, bool) or not isinstance(level, int):
                raise ApiError(400, "level must be a whole number of percent")
            try:
                self._json(200, controller.set_volume(level))
            except ControlError as e:
                raise ApiError(400, str(e)) from None

        # --- live events ---------------------------------------------------

        def _event_stream(self) -> None:
            if events is None:
                raise ApiError(404, "live updates are not available")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            q = events.subscribe()
            try:
                # The current state first, so a page opened mid-song is right.
                self._sse("now", now_state())
                volume = events.latest("volume")
                if volume is not None:
                    self._sse("volume", volume)
                while True:
                    try:
                        event, data = q.get(timeout=KEEPALIVE)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    if event == "now":
                        data = {**data, "server_time": time.time()}
                    self._sse(event, data)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # the page was closed
            finally:
                events.unsubscribe(q)

        def _sse(self, event: str, data) -> None:
            self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()

        def _cards_changed(self) -> None:
            if events is not None:
                events.publish("cards", {})

        # --- helpers -------------------------------------------------------

        def _static(self, rel: str) -> None:
            root = STATIC_DIR.resolve()
            path = (root / rel).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ApiError(404, "not found")
            ctype = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
            # Vendored files have the version in their name, so they never change.
            cache = ("public, max-age=31536000, immutable"
                     if path.is_relative_to(root / "vendor") else "no-store")
            self._send(200, ctype, path.read_bytes(), cache)

        def _card_list(self) -> list[dict]:
            out = []
            for uid, entry in cards.entries().items():
                paths = [cards.music_dir / t for t in entry["tracks"]]
                files = [p for p in paths if p.is_file()]
                shown = describe_tracks(files) if files else {"name": paths[0].stem, "artist": None, "parts": 0}
                custom = entry.get("name")
                out.append({"uid": uid, "tracks": entry["tracks"], "track": entry["tracks"][0],
                            "missing": not files, "missing_count": len(paths) - len(files),
                            "kind": "file" if len(paths) == 1 else "list",
                            **shown, "parts": len(paths),
                            # name: what to show; tag_name: what the files say
                            "name": custom or shown["name"], "tag_name": shown["name"],
                            "custom_name": custom,
                            "shuffle": bool(entry.get("shuffle")), "repeat": bool(entry.get("repeat")),
                            "art_version": art.version(uid) if art is not None else None})
            return sorted(out, key=lambda c: c["name"].lower())

        def _cover(self, rel: str) -> None:
            # Only paths the library lists; never an arbitrary file.
            if cover_dir is None or rel not in cards.playable():
                raise ApiError(404, "not found")
            target = cards.music_dir / rel
            art = cover_art(target, cards.files_for(target), cover_dir)
            if art is None:
                raise ApiError(404, "no cover art")
            ctype = "image/png" if art.suffix.lower() == ".png" else "image/jpeg"
            self._send(200, ctype, art.read_bytes(), "private, max-age=3600")

        def _card_uid(self, url) -> str:
            prefix = "/api/cards/"
            if not url.path.startswith(prefix):
                raise ApiError(404, "not found")
            uid = unquote(url.path[len(prefix):]).strip().lower()
            if not UID_RE.match(uid):
                raise ApiError(400, "card ID must be 4-32 hex characters, like 04a1b2c3")
            return uid

        def _check_write(self) -> None:
            # Block other websites open in the same browser from editing cards:
            # a cross-site request carries an Origin that doesn't match our Host.
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                raise ApiError(403, "cross-site request refused")

        def _read_json(self):
            if self.headers.get_content_type() != "application/json":
                raise ApiError(415, "expected application/json")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise ApiError(400, "bad Content-Length") from None
            if length > MAX_BODY:
                raise ApiError(413, "request too large")
            try:
                return json.loads(self.rfile.read(length) or b"null")
            except ValueError:
                raise ApiError(400, "invalid JSON") from None

        def _json(self, code: int, data) -> None:
            self._send(code, "application/json", json.dumps(data).encode())

        def _send(self, code: int, ctype: str, body: bytes, cache: str = "no-store") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args) -> None:
            log.debug("%s %s", self.address_string(), format % args)

    return Handler


def start_web(taplog, cards, events=None, host: str = "0.0.0.0",
              port: int = 80, controller=None, cover_dir=None,
              art=None, musicbrainz=None, files=None, paths=None) -> ThreadingHTTPServer:
    """Start serving in a daemon thread and return the server."""
    server = ThreadingHTTPServer((host, port),
                                 make_handler(taplog, cards, events, controller, cover_dir,
                                              art, musicbrainz, files, paths))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    log.info("web page at http://%s:%d/", host, server.server_address[1])
    return server
