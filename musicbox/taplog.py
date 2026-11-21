"""A persistent log of card taps, stored in SQLite.

The box writes one row per card placed; the web page reads them. Each call
opens its own connection, so the reader loop and the web server threads
never share one.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS taps (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    time   REAL NOT NULL,   -- unix timestamp
    uid    TEXT NOT NULL,
    track  TEXT,            -- path relative to music_dir, NULL if unassigned
    result TEXT NOT NULL    -- 'played', 'blocked', 'unknown' or 'missing'
)
"""


class TapLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def record(self, uid: str, track: str | None, result: str,
               when: float | None = None) -> dict:
        """Store a tap and return it as a row, like recent() does."""
        when = time.time() if when is None else when
        db = self._connect()
        try:
            with db:
                cur = db.execute(
                    "INSERT INTO taps (time, uid, track, result) VALUES (?, ?, ?, ?)",
                    (when, uid, track, result),
                )
        finally:
            db.close()
        return {"id": cur.lastrowid, "time": when, "uid": uid, "track": track, "result": result}

    def recent(self, limit: int = 500) -> list[dict]:
        """Newest taps first."""
        db = self._connect()
        try:
            rows = db.execute(
                "SELECT id, time, uid, track, result FROM taps ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            db.close()
        return [dict(r) for r in rows]
