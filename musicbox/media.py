"""Reading facts about audio files (no playback here)."""

from __future__ import annotations

import logging
import subprocess
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)


def track_duration(path: Path) -> float | None:
    """Length of an audio file in seconds via ffprobe, or None if unknown."""
    try:
        st = path.stat()
    except OSError:
        return None
    return _probe(str(path), st.st_size, st.st_mtime_ns)


@lru_cache(maxsize=256)
def _probe(path: str, size: int, mtime_ns: int) -> float | None:
    # size and mtime are part of the cache key so an edited file is re-read.
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=10,
        )
        return float(r.stdout.strip()) if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        log.debug("could not read the duration of %s", path)
        return None


def display_name(track: str) -> str:
    """A readable name from a track path: file name without its extension."""
    return Path(track).stem
