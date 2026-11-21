"""Where each card left off, for cards with Remember playback on.

A small JSON file, separate from cards.json because it's written often (every
~15 s while a remembered card plays, and when it's lifted):

    {"04a1b2c3": {"track": "Story/2.mp3", "position": 312.4, "saved": 1795...}}

"track" is the file that was playing (relative to the music folder) and
"position" the seconds into it. Resuming starts a little before that.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

RESUME_BACK = 5.0  # seconds to go back when resuming, to catch the last words again


class PositionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.warning("couldn't read %s; starting with no remembered positions", self.path)
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=1, sort_keys=True)
            os.replace(tmp, self.path)  # atomic: a power cut leaves the old or the new file
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def get(self, uid: str) -> dict | None:
        with self._lock:
            entry = self._read().get(uid)
        ok = (isinstance(entry, dict) and isinstance(entry.get("track"), str)
              and isinstance(entry.get("position"), (int, float)))
        return entry if ok else None

    def save(self, uid: str, track: str, position: float) -> None:
        with self._lock:
            data = self._read()
            data[uid] = {"track": track, "position": round(max(0.0, float(position)), 1),
                         "saved": round(time.time())}
            self._write(data)

    def forget(self, uid: str) -> None:
        with self._lock:
            data = self._read()
            if data.pop(uid, None) is not None:
                self._write(data)

    def rename(self, old: str, new: str) -> None:
        with self._lock:
            data = self._read()
            if old in data:
                data[new] = data.pop(old)
                self._write(data)


def resume_point(position: float) -> float:
    """Where to restart: RESUME_BACK seconds before `position`, not below 0."""
    return max(0.0, float(position) - RESUME_BACK)
