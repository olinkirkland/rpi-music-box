"""Audio playback by running mpv as a child process.

Safety rules, enforced here rather than left to configuration:

- Nothing plays unless the sound card's hardware volume was just set to, and
  read back as, at most the configured limit (when a mixer is configured).
- While anything plays, the volume is re-checked every half second; if it is
  above the limit, or can't be read, playback is killed.
- A track only plays after the chime, in the same mpv process. Without a
  chime, a track does not play.
- A track is cut off after `play_seconds`, by mpv and, as a backstop, by
  killing mpv if it runs past its deadline.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

CHECK_EVERY = 0.5  # seconds between volume checks while playing
GRACE = 1.5        # extra seconds allowed for mpv start-up before the hard stop


class Mixer:
    """The sound card's own (hardware) PCM volume, via amixer."""

    def __init__(self, card: str, limit: int, control: str = "PCM") -> None:
        self.card = card
        self.limit = max(0, min(int(limit), 100))
        self.control = control

    def _amixer(self, *args: str) -> str | None:
        if not self.card:
            log.error("no sound card name to set the volume on (audio_device needs CARD=...)")
            return None
        try:
            r = subprocess.run(["amixer", "-c", self.card, *args],
                               capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.error("amixer failed on %s: %s", self.card, e)
            return None
        if r.returncode != 0:
            log.error("amixer failed on %s: %s", self.card,
                      r.stderr.strip() or f"exit {r.returncode}")
            return None
        return r.stdout

    def current(self) -> int | None:
        """Highest channel volume in percent, or None if it can't be read."""
        out = self._amixer("sget", self.control)
        levels = [int(n) for n in re.findall(r"\[(\d+)%\]", out or "")]
        return max(levels) if levels else None

    def set_and_verify(self) -> bool:
        """Set the volume to the limit; True only if it reads back at or below it."""
        if self._amixer("-q", "sset", self.control, f"{self.limit}%") is None:
            return False
        level = self.current()
        if level is None or level > self.limit:
            log.error("volume on %s reads %s%%, above the %d%% limit", self.card, level, self.limit)
            return False
        return True


class Player:
    def __init__(self, volume: int = 100, audio_device: str = "",
                 command: list[str] | None = None, chime: Path | None = None,
                 chime_seconds: float = 0.0, play_seconds: float = 0,
                 mixer: Mixer | None = None, clock=time.monotonic) -> None:
        if command is None:
            volume = max(0, min(int(volume), 100))
            command = ["mpv", "--no-video", "--no-terminal",
                       f"--volume={volume}", "--volume-max=100", "--replaygain=no"]
            if audio_device:
                command.append(f"--audio-device={audio_device}")
            if play_seconds:
                # Per file: the chime is shorter, so only the track is cut.
                command.append(f"--length={play_seconds}")
        self.command = command
        # Played just before each track. Both go to one mpv process, because
        # a USB sound card can only be opened by one program at a time.
        self.chime = chime
        self.chime_seconds = chime_seconds
        self.play_seconds = play_seconds
        self.mixer = mixer
        self.clock = clock
        self._proc: subprocess.Popen | None = None
        self._deadline: float | None = None
        self._next_check = 0.0

    def play(self, track: Path) -> bool:
        """Chime, then the track (cut off after play_seconds). False if refused."""
        self.stop()
        if self.chime is None:
            log.error("not playing %s: no chime to play first (check chime_file)", track)
            return False
        log.info("playing chime, then %s", track)
        limit = self.chime_seconds + self.play_seconds if self.play_seconds else None
        return self._start([self.chime, track], limit)

    def cue(self) -> None:
        """Stop, and play just the chime: a card was seen but has no song."""
        self.stop()
        if self.chime is not None:
            self._start([self.chime], self.chime_seconds)

    def _start(self, files: list, seconds: float | None) -> bool:
        if self.mixer is not None and not self.mixer.set_and_verify():
            log.error("not playing anything: could not confirm the volume is at most %d%%",
                      self.mixer.limit)
            return False
        now = self.clock()
        self._deadline = now + seconds + GRACE if seconds is not None else None
        self._next_check = now + CHECK_EVERY
        self._proc = subprocess.Popen([*self.command, "--", *map(str, files)],
                                      stdin=subprocess.DEVNULL)
        return True

    def check(self) -> None:
        """Call often while running: enforces the time and volume limits."""
        if not self.playing:
            return
        now = self.clock()
        if self._deadline is not None and now >= self._deadline:
            log.warning("playback ran past its time limit; stopping it")
            self.stop()
            return
        if self.mixer is not None and now >= self._next_check:
            self._next_check = now + CHECK_EVERY
            level = self.mixer.current()
            if level is None or level > self.mixer.limit:
                log.error("volume is %s%% (limit %d%%) during playback; stopping it",
                          level, self.mixer.limit)
                self.stop()

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        self._deadline = None
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
