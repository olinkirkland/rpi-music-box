"""Audio playback by running mpv as a child process."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


class Player:
    def __init__(self, volume: int = 100, audio_device: str = "",
                 command: list[str] | None = None) -> None:
        if command is None:
            command = ["mpv", "--no-video", "--no-terminal", f"--volume={volume}"]
            if audio_device:
                command.append(f"--audio-device={audio_device}")
        self.command = command
        self._proc: subprocess.Popen | None = None

    def play(self, track: Path) -> None:
        self.stop()
        log.info("playing %s", track)
        self._proc = subprocess.Popen([*self.command, "--", str(track)],
                                      stdin=subprocess.DEVNULL)

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.poll() is None:
            log.info("stopping playback")
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    @property
    def playing(self) -> bool:
        return self._proc is not None and self._proc.poll() is None
