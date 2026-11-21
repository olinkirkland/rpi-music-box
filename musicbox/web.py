"""A small web page: the card tap log, and editing which card plays which song.

Runs in a background thread of `musicbox run`, using only the standard
library.

    GET    /                  the page
    GET    /api/taps          tap log, newest first (?limit=N)
    GET    /api/cards         [{uid, track, missing}]
    GET    /api/tracks        audio files in music_dir
    PUT    /api/cards/<uid>   {"track": "<path relative to music_dir>"}
    DELETE /api/cards/<uid>
"""

from __future__ import annotations

import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

log = logging.getLogger(__name__)

TITLE = "Olin's Music Box"
UID_RE = re.compile(r"^[0-9a-f]{4,32}$")
MAX_BODY = 16 * 1024

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root {
    --bg: #f6f3ee; --panel: #ffffff; --text: #1f1d1a; --muted: #6f6a62;
    --line: #e6e0d6; --accent: #b4532a; --accent-text: #ffffff; --field: #fbfaf7;
    --ok: #2f7d4f; --warn: #a86a00; --bad: #b3261e;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #171614; --panel: #201f1c; --text: #ece8e1; --muted: #a29c92;
      --line: #34322d; --accent: #e0855a; --accent-text: #1a1310; --field: #191816;
      --ok: #6cc28f; --warn: #e0b04f; --bad: #f08a80;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  main { max-width: 900px; margin: 0 auto; padding: 32px 16px 48px; }
  h1 { margin: 0 0 4px; font-size: 28px; letter-spacing: -0.01em; }
  h1 span { color: var(--accent); }
  .sub { margin: 0 0 24px; color: var(--muted); }
  .panel {
    background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
    overflow: hidden; margin-bottom: 24px;
  }
  .head {
    display: flex; justify-content: space-between; align-items: baseline; gap: 12px;
    padding: 14px 16px; border-bottom: 1px solid var(--line);
  }
  .head h2 { margin: 0; font-size: 16px; }
  .head small { color: var(--muted); }
  .scroll { max-height: 60vh; overflow: auto; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 9px 16px; border-bottom: 1px solid var(--line); vertical-align: middle; }
  th {
    position: sticky; top: 0; background: var(--panel); z-index: 1;
    font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted);
  }
  tbody tr:last-child td { border-bottom: none; }
  td.num { color: var(--muted); font-variant-numeric: tabular-nums; }
  td.time { white-space: nowrap; font-variant-numeric: tabular-nums; }
  td.track { word-break: break-word; }
  td.actions { text-align: right; white-space: nowrap; }
  code { font: 13px ui-monospace, SFMono-Regular, Menlo, monospace; }
  .tag { font-size: 12px; font-weight: 600; white-space: nowrap; }
  .tag.played { color: var(--ok); }
  .tag.unknown { color: var(--warn); }
  .tag.missing, .tag.blocked { color: var(--bad); }
  .empty { padding: 24px 16px; text-align: center; color: var(--muted); }
  select, input, button { font: inherit; color: inherit; }
  select, input {
    width: 100%; min-width: 0; padding: 6px 8px; border-radius: 8px;
    border: 1px solid var(--line); background: var(--field);
  }
  input { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }
  button {
    padding: 6px 12px; border-radius: 8px; border: 1px solid var(--line);
    background: transparent; cursor: pointer; white-space: nowrap;
  }
  button:hover { border-color: var(--muted); }
  button.primary { background: var(--accent); border-color: var(--accent); color: var(--accent-text); font-weight: 600; }
  button.danger { color: var(--bad); }
  button.link { border: none; padding: 2px 4px; color: var(--accent); font-weight: 600; }
  .add {
    display: grid; grid-template-columns: minmax(110px, 1fr) minmax(0, 2.5fr) auto; gap: 8px;
    padding: 12px 16px; border-top: 1px solid var(--line); align-items: center;
  }
  .hint { padding: 0 16px 12px; margin: 0; color: var(--muted); font-size: 13px; }
  .missing-note { color: var(--bad); font-size: 12px; font-weight: 600; margin-left: 6px; }
  #toast {
    position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%);
    background: var(--text); color: var(--bg); padding: 8px 14px; border-radius: 8px;
    font-size: 14px; opacity: 0; transition: opacity .2s; pointer-events: none;
  }
  #toast.show { opacity: 1; }
  #toast.error { background: var(--bad); color: #fff; }
  @media (max-width: 560px) {
    th, td { padding: 8px 10px; }
    .hide-sm { display: none; }
    .add { grid-template-columns: 1fr auto; }
    .add select { grid-column: 1 / -1; grid-row: 2; }
  }
</style>
</head>
<body>
<main>
  <h1>&#9835; <span>__TITLE__</span></h1>
  <p class="sub">Tap a card, hear a song.</p>

  <section class="panel">
    <div class="head">
      <h2>Cards</h2>
      <small id="cards-status"></small>
    </div>
    <table>
      <thead><tr><th>Card ID</th><th>Song</th><th></th></tr></thead>
      <tbody id="cards"></tbody>
    </table>
    <div id="cards-empty" class="empty" hidden>No cards assigned yet.</div>
    <form class="add" id="add-form">
      <input id="add-uid" placeholder="card ID" autocomplete="off" spellcheck="false" required>
      <select id="add-track" required></select>
      <button class="primary" type="submit">Assign</button>
    </form>
    <p class="hint">Tap a new card on the box, then press <b>Assign</b> next to it in the log below.</p>
  </section>

  <section class="panel">
    <div class="head">
      <h2>Card taps</h2>
      <small id="status">Loading&hellip;</small>
    </div>
    <div class="scroll">
      <table>
        <thead><tr>
          <th class="hide-sm">#</th><th>Date</th><th>Card ID</th><th>Track</th><th>Result</th><th></th>
        </tr></thead>
        <tbody id="rows"></tbody>
      </table>
      <div id="empty" class="empty" hidden>No cards tapped yet.</div>
    </div>
  </section>
</main>
<div id="toast"></div>
<script>
const LABELS = { played: "played", unknown: "unknown card", missing: "file missing", blocked: "not played (safety check)" };
const fmt = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium" });
let lastTapId = null, tracks = [], cards = [];

function el(tag, props = {}, ...kids) {
  const e = Object.assign(document.createElement(tag), props);
  e.append(...kids);
  return e;
}

let toastTimer;
function toast(msg, error = false) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "show" + (error ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = ""), 2500);
}

async function api(method, path, body) {
  const opts = { method, cache: "no-store", headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "HTTP " + res.status);
  return data;
}

function trackSelect(selected, placeholder) {
  const s = el("select");
  if (placeholder) s.append(el("option", { value: "", textContent: placeholder, disabled: true, selected: !selected }));
  const names = [...tracks];
  if (selected && !names.includes(selected)) names.unshift(selected);  // file gone, keep showing it
  for (const name of names) s.append(el("option", { value: name, textContent: name, selected: name === selected }));
  return s;
}

function renderCards() {
  const body = document.getElementById("cards");
  body.replaceChildren();
  for (const c of cards) {
    const select = trackSelect(c.track);
    select.addEventListener("change", () => assign(c.uid, select.value));
    const songCell = el("td", {}, select);
    if (c.missing) songCell.append(el("span", { className: "missing-note", textContent: "file missing" }));
    const remove = el("button", { className: "danger", type: "button", textContent: "Remove" });
    remove.addEventListener("click", () => removeCard(c.uid));
    body.append(el("tr", {},
      el("td", {}, el("code", { textContent: c.uid })),
      songCell,
      el("td", { className: "actions" }, remove)));
  }
  document.getElementById("cards-empty").hidden = cards.length > 0;
  document.getElementById("cards-status").textContent =
    cards.length + (cards.length === 1 ? " card" : " cards") + " \\u00b7 " + tracks.length + " songs";
  const add = document.getElementById("add-track");
  const keep = add.value;
  add.replaceWith(Object.assign(trackSelect(keep, tracks.length ? "Choose a song\\u2026" : "No songs found"), { id: "add-track", required: true }));
}

async function loadCards() {
  try {
    [tracks, cards] = await Promise.all([api("GET", "api/tracks"), api("GET", "api/cards")]);
    renderCards();
  } catch (e) { toast("Couldn't load cards: " + e.message, true); }
}

async function assign(uid, track) {
  try {
    await api("PUT", "api/cards/" + encodeURIComponent(uid), { track });
    toast("Card " + uid + " \\u2192 " + track);
  } catch (e) { toast(e.message, true); }
  loadCards();
}

async function removeCard(uid) {
  if (!confirm("Remove card " + uid + "? It will stop playing anything.")) return;
  try {
    await api("DELETE", "api/cards/" + encodeURIComponent(uid));
    toast("Removed card " + uid);
  } catch (e) { toast(e.message, true); }
  loadCards();
}

document.getElementById("add-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const uid = document.getElementById("add-uid").value.trim().toLowerCase();
  const track = document.getElementById("add-track").value;
  if (!track) return toast("Choose a song first", true);
  await assign(uid, track);
  document.getElementById("add-uid").value = "";
});

function prefill(uid) {
  const input = document.getElementById("add-uid");
  input.value = uid;
  input.scrollIntoView({ behavior: "smooth", block: "center" });
  document.getElementById("add-track").focus();
}

function renderTaps(taps) {
  const body = document.getElementById("rows");
  body.replaceChildren();
  const known = new Set(cards.map((c) => c.uid));
  for (const t of taps) {
    const action = el("td", { className: "actions" });
    if (!known.has(t.uid)) {
      const b = el("button", { className: "link", type: "button", textContent: "Assign" });
      b.addEventListener("click", () => prefill(t.uid));
      action.append(b);
    }
    body.append(el("tr", {},
      el("td", { className: "num hide-sm", textContent: t.id }),
      el("td", { className: "time", textContent: fmt.format(new Date(t.time * 1000)) }),
      el("td", {}, el("code", { textContent: t.uid })),
      el("td", { className: "track", textContent: t.track || "\\u2014" }),
      el("td", {}, el("span", { className: "tag " + t.result, textContent: LABELS[t.result] || t.result })),
      action));
  }
  document.getElementById("empty").hidden = taps.length > 0;
}

let lastCardKey = "";
async function refresh() {
  const status = document.getElementById("status");
  try {
    const taps = await api("GET", "api/taps");
    const newest = taps.length ? taps[0].id : 0;
    const cardKey = cards.map((c) => c.uid).join();
    if (newest !== lastTapId || cardKey !== lastCardKey) {
      renderTaps(taps); lastTapId = newest; lastCardKey = cardKey;
    }
    status.textContent = taps.length + " shown \\u00b7 updated " + new Date().toLocaleTimeString();
  } catch (e) {
    status.textContent = "Can't reach the music box";
  }
}

loadCards().then(refresh);
setInterval(refresh, 2500);
</script>
</body>
</html>
""".replace("__TITLE__", TITLE)


class ApiError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def make_handler(taplog, cards):
    write_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "musicbox"

        # --- routing -------------------------------------------------------

        def do_GET(self) -> None:
            self._handle(self._get)

        def do_PUT(self) -> None:
            self._handle(self._put)

        def do_DELETE(self) -> None:
            self._handle(self._delete)

        def _handle(self, method) -> None:
            try:
                method(urlparse(self.path))
            except ApiError as e:
                self._json(e.code, {"error": str(e)})
            except Exception:
                log.exception("web request failed: %s %s", self.command, self.path)
                self._json(500, {"error": "internal error"})

        def _get(self, url) -> None:
            if url.path == "/":
                self._send(200, "text/html; charset=utf-8", PAGE.encode())
            elif url.path == "/api/taps":
                try:
                    limit = int(parse_qs(url.query).get("limit", ["500"])[0])
                except ValueError:
                    limit = 500
                self._json(200, taplog.recent(max(1, min(limit, 5000))))
            elif url.path == "/api/cards":
                self._json(200, self._card_list())
            elif url.path == "/api/tracks":
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
            self._json(200, {"uid": uid, "track": track})

        def _delete(self, url) -> None:
            uid = self._card_uid(url)
            self._check_write()
            with write_lock:
                removed = cards.remove(uid)
            if not removed:
                raise ApiError(404, f"no such card: {uid}")
            log.info("web: card %s removed", uid)
            self._json(200, {"uid": uid})

        # --- helpers -------------------------------------------------------

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

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args) -> None:
            # The page polls every few seconds; keep that out of the journal.
            log.debug("%s %s", self.address_string(), format % args)

    return Handler


def start_web(taplog, cards, host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:
    """Start serving in a daemon thread and return the server."""
    server = ThreadingHTTPServer((host, port), make_handler(taplog, cards))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    log.info("web page at http://%s:%d/", host, server.server_address[1])
    return server
