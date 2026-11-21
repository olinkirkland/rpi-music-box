"""The web page: what's playing, the card list, and the tap log.

Runs in a background thread of `musicbox run`, using only the standard
library. The page itself is static files in musicbox/static (Vue and Pico
CSS are vendored there, so there is no build step and no internet needed).

    GET    /                  the page
    GET    /static/<path>     page assets
    GET    /api/events        live updates (Server-Sent Events): now, tap, cards
    GET    /api/now           what's playing right now
    GET    /api/taps          tap log, newest first (?limit=N)
    GET    /api/cards         [{uid, track, missing}]
    GET    /api/tracks        audio files in music_dir
    PUT    /api/cards/<uid>   {"track": "<path relative to music_dir>"}
    DELETE /api/cards/<uid>
    POST   /api/player/pause   {}
    POST   /api/player/resume  {}
    POST   /api/player/seek    {"position": <seconds into the song>}
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


def make_handler(taplog, cards, events=None, controller=None):
    write_lock = threading.Lock()

    def now_state() -> dict:
        now = events.now if events is not None else {"state": "idle"}
        return {**now, "server_time": time.time()}

    class Handler(BaseHTTPRequestHandler):
        server_version = "musicbox"

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
            else:
                raise ApiError(404, "not found")

        def _put(self, url) -> None:
            uid = self._card_uid(url)
            self._check_write()
            body = self._read_json()
            track = body.get("track") if isinstance(body, dict) else None
            if not isinstance(track, str) or track not in cards.tracks():
                raise ApiError(400, "unknown song; pick one from the list")
            with write_lock:
                cards.assign(uid, cards.music_dir / track)
            log.info("web: card %s -> %s", uid, track)
            self._cards_changed()
            self._json(200, {"uid": uid, "track": track})

        def _delete(self, url) -> None:
            uid = self._card_uid(url)
            self._check_write()
            with write_lock:
                removed = cards.remove(uid)
            if not removed:
                raise ApiError(404, f"no such card: {uid}")
            log.info("web: card %s removed", uid)
            self._cards_changed()
            self._json(200, {"uid": uid})

        def _post(self, url) -> None:
            if url.path == "/api/volume":
                return self._post_volume()
            prefix = "/api/player/"
            if not url.path.startswith(prefix):
                raise ApiError(404, "not found")
            action = url.path[len(prefix):]
            if action not in ("pause", "resume", "seek"):
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
            try:
                now = controller.control(action, position)
            except ControlError as e:
                raise ApiError(409, str(e)) from None
            self._json(200, {**now, "server_time": time.time()})

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
            return [
                {"uid": uid, "track": track, "missing": not (cards.music_dir / track).is_file()}
                for uid, track in sorted(cards.all().items(), key=lambda kv: kv[1].lower())
            ]

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
              port: int = 80, controller=None) -> ThreadingHTTPServer:
    """Start serving in a daemon thread and return the server."""
    server = ThreadingHTTPServer((host, port), make_handler(taplog, cards, events, controller))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    log.info("web page at http://%s:%d/", host, server.server_address[1])
    return server
