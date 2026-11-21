import shutil
import struct
import wave

import pytest

from musicbox.chime import duration, prepare_chime

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def write_wav(path, seconds=1.0, amplitude=30000):
    # A test fixture file only; the tests never play it.
    n = int(8000 * seconds)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(struct.pack(f"<{n}h", *([amplitude, -amplitude] * (n // 2))))
    return path


def peak(path):
    with wave.open(str(path)) as w:
        frames = w.readframes(w.getnframes())
    return max(abs(v) for (v,) in struct.iter_unpack("<h", frames))


def test_chime_file_is_scaled_down_and_cached(tmp_path):
    src = write_wav(tmp_path / "chime.wav")
    out = prepare_chime(tmp_path / "cache", 25, src)
    assert out.parent == tmp_path / "cache"
    assert 0 < peak(out) < peak(src) * 0.3
    assert abs(duration(out) - 1.0) < 0.05
    assert prepare_chime(tmp_path / "cache", 25, src) == out


def test_unusable_chime_file_raises_oserror(tmp_path):
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"not audio")
    with pytest.raises(OSError):
        prepare_chime(tmp_path, 25, bad)
    with pytest.raises(OSError):
        prepare_chime(tmp_path, 25, tmp_path / "missing.mp3")
    assert not list(tmp_path.glob("*.tmp*"))
