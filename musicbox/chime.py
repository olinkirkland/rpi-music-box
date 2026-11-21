"""The tap chime: your own sound file, turned down to `chime_volume`.

The file is converted once with ffmpeg into a cached WAV at that loudness,
so the player can play it before each track. There is no built-in sound:
without a usable chime file, nothing plays.
"""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path


def prepare_chime(cache_dir: Path, volume: int, source: Path) -> Path:
    """Return a cached WAV of `source` scaled to `volume` (0-100)."""
    volume = max(0, min(int(volume), 100))
    st = source.stat()  # FileNotFoundError (an OSError) if it is gone
    # Name by size and mtime so editing the file makes a fresh copy.
    path = cache_dir / f"chime-{source.stem}-{st.st_size}-{st.st_mtime_ns}-{volume}.wav"
    if path.exists():
        return path
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.wav")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(source),
             "-af", f"volume={volume / 100:.2f}", "-vn", str(tmp)],
            check=True, capture_output=True, text=True, timeout=30,
        )
    except subprocess.CalledProcessError as e:
        tmp.unlink(missing_ok=True)
        raise OSError(f"ffmpeg could not read {source}: {e.stderr.strip()}") from None
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        raise OSError(f"ffmpeg timed out reading {source}") from None
    tmp.replace(path)
    return path


def duration(path: Path) -> float:
    """Length of a WAV file in seconds."""
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()
