"""A small read-only web page showing the card tap log.

Runs in a background thread of `musicbox run`, using only the standard
library. GET / serves the page, GET /api/taps serves the log as JSON.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)

TITLE = "Olin's Music Box"

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root {
    --bg: #f6f3ee; --panel: #ffffff; --text: #1f1d1a; --muted: #6f6a62;
    --line: #e6e0d6; --accent: #b4532a;
    --ok: #2f7d4f; --warn: #a86a00; --bad: #b3261e;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #171614; --panel: #201f1c; --text: #ece8e1; --muted: #a29c92;
      --line: #34322d; --accent: #e0855a;
      --ok: #6cc28f; --warn: #e0b04f; --bad: #f08a80;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  main { max-width: 860px; margin: 0 auto; padding: 32px 16px 48px; }
  h1 { margin: 0 0 4px; font-size: 28px; letter-spacing: -0.01em; }
  h1 span { color: var(--accent); }
  .sub { margin: 0 0 24px; color: var(--muted); }
  .panel {
    background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
    overflow: hidden;
  }
  .head {
    display: flex; justify-content: space-between; align-items: baseline;
    padding: 14px 16px; border-bottom: 1px solid var(--line);
  }
  .head h2 { margin: 0; font-size: 16px; }
  .head small { color: var(--muted); }
  .scroll { max-height: 70vh; overflow: auto; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 9px 16px; border-bottom: 1px solid var(--line); }
  th {
    position: sticky; top: 0; background: var(--panel); z-index: 1;
    font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted);
  }
  tr:last-child td { border-bottom: none; }
  td.num { color: var(--muted); font-variant-numeric: tabular-nums; }
  td.time { white-space: nowrap; font-variant-numeric: tabular-nums; }
  code { font: 13px ui-monospace, SFMono-Regular, Menlo, monospace; }
  td.track { word-break: break-word; }
  .tag { font-size: 12px; font-weight: 600; white-space: nowrap; }
  .tag.played { color: var(--ok); }
  .tag.unknown { color: var(--warn); }
  .tag.missing { color: var(--bad); }
  .empty { padding: 32px 16px; text-align: center; color: var(--muted); }
  @media (max-width: 560px) {
    th, td { padding: 8px 10px; }
    .hide-sm { display: none; }
  }
</style>
</head>
<body>
<main>
  <h1>&#9835; <span>__TITLE__</span></h1>
  <p class="sub">Tap a card, hear a song.</p>
  <section class="panel">
    <div class="head">
      <h2>Card taps</h2>
      <small id="status">Loading&hellip;</small>
    </div>
    <div class="scroll">
      <table>
        <thead><tr>
          <th class="hide-sm">#</th><th>Date</th><th>Card ID</th><th>Track</th><th>Result</th>
        </tr></thead>
        <tbody id="rows"></tbody>
      </table>
      <div id="empty" class="empty" hidden>No cards tapped yet.</div>
    </div>
  </section>
</main>
<script>
const LABELS = { played: "played", unknown: "unknown card", missing: "file missing" };
const fmt = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium" });
let lastId = null;

function cell(row, text, cls) {
  const td = row.insertCell();
  if (cls) td.className = cls;
  td.textContent = text;
  return td;
}

function render(taps) {
  const body = document.getElementById("rows");
  body.replaceChildren();
  for (const t of taps) {
    const tr = body.insertRow();
    cell(tr, t.id, "num hide-sm");
    cell(tr, fmt.format(new Date(t.time * 1000)), "time");
    const id = tr.insertCell();
    const code = document.createElement("code");
    code.textContent = t.uid;
    id.appendChild(code);
    cell(tr, t.track || "\\u2014", "track");
    const res = tr.insertCell();
    const tag = document.createElement("span");
    tag.className = "tag " + t.result;
    tag.textContent = LABELS[t.result] || t.result;
    res.appendChild(tag);
  }
  document.getElementById("empty").hidden = taps.length > 0;
}

async function refresh() {
  const status = document.getElementById("status");
  try {
    const res = await fetch("api/taps", { cache: "no-store" });
    if (!res.ok) throw new Error(res.status);
    const taps = await res.json();
    const newest = taps.length ? taps[0].id : 0;
    if (newest !== lastId) { render(taps); lastId = newest; }
    status.textContent = taps.length + " shown \\u00b7 updated " +
      new Date().toLocaleTimeString();
  } catch (e) {
    status.textContent = "Can't reach the music box";
  }
}

refresh();
setInterval(refresh, 2500);
</script>
</body>
</html>
""".replace("__TITLE__", TITLE)


def make_handler(taplog):
    class Handler(BaseHTTPRequestHandler):
        server_version = "musicbox"

        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path == "/":
                self._send(200, "text/html; charset=utf-8", PAGE.encode())
            elif url.path == "/api/taps":
                try:
                    limit = int(parse_qs(url.query).get("limit", ["500"])[0])
                except ValueError:
                    limit = 500
                limit = max(1, min(limit, 5000))
                body = json.dumps(taplog.recent(limit)).encode()
                self._send(200, "application/json", body)
            else:
                self._send(404, "text/plain; charset=utf-8", b"not found\n")

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


def start_web(taplog, host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:
    """Start serving in a daemon thread and return the server."""
    server = ThreadingHTTPServer((host, port), make_handler(taplog))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    log.info("web page at http://%s:%d/", host, server.server_address[1])
    return server
