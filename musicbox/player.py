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
- Resuming after a pause re-checks the volume first, exactly like starting.

Pause, resume and seek go through mpv's JSON control socket. Which of them
are allowed, and when, is decided by MusicBox (only the song from the
current tap, while its card is still on the reader).
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

CHECK_EVERY = 0.5  # seconds between volume checks while playing
GRACE = 1.5        # extra seconds allowed for mpv start-up before the hard stop
IPC_TIMEOUT = 1.0  # seconds to wait for mpv to answer a control command


class ControlError(Exception):
    """A pause/resume/seek request that can't be done; the message says why."""


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
        """Highest channel volume in percent (a muted channel counts as 0),
        or None if it can't be read."""
        out = self._amixer("sget", self.control) or ""
        levels = []
        for line in out.splitlines():
            m = re.search(r"\[(\d+)%\]", line)
            if m:
                levels.append(0 if "[off]" in line else int(m.group(1)))
        return max(levels) if levels else None

    def set_level(self, level: int) -> None:
        """Change the target level (0-100); takes effect on set_and_verify."""
        self.limit = max(0, min(int(level), 100))

    def set_and_verify(self) -> bool:
        """Set the volume to the limit; True only if it reads back at or below it.

        0% also mutes, since the lowest hardware step may not be silent.
        """
        switch = "mute" if self.limit == 0 else "unmute"
        if self._amixer("-q", "sset", self.control, f"{self.limit}%", switch) is None:
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
                 mixer: Mixer | None = None, clock=time.monotonic,
                 ipc_dir: Path | None = None) -> None:
        if command is None:
            volume = max(0, min(int(volume), 100))
            command = ["mpv", "--no-video", "--no-terminal",
                       f"--volume={volume}", "--volume-max=100", "--replaygain=no"]
            if audio_device:
                command.append(f"--audio-device={audio_device}")
            if play_seconds:
                # Per file: the chime is shorter, so only the track is cut.
                command.append(f"--length={play_seconds}")
        # mpv's control socket, for pause/resume/seek. None: no controls.
        self.ipc_path = ipc_dir / f"mpv-{os.getpid()}.sock" if ipc_dir else None
        if self.ipc_path is not None:
            command = [*command, f"--input-ipc-server={self.ipc_path}"]
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
        self._request_id = 0
        # The box thread starts/stops/checks; web threads pause/resume/seek.
        self._lock = threading.RLock()

    @property
    def controllable(self) -> bool:
        """Whether pause/resume/seek can work at all with this setup."""
        # With play_seconds set, a pause would fight the hard time limit.
        return self.ipc_path is not None and not self.play_seconds

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
        with self._lock:
            if self.mixer is not None and not self.mixer.set_and_verify():
                log.error("not playing anything: could not confirm the volume is at most %d%%",
                          self.mixer.limit)
                return False
            if self.ipc_path is not None:
                self.ipc_path.parent.mkdir(parents=True, exist_ok=True)
                self.ipc_path.unlink(missing_ok=True)  # a stale one from a killed mpv
            now = self.clock()
            self._deadline = now + seconds + GRACE if seconds is not None else None
            self._next_check = now + CHECK_EVERY
            self._proc = subprocess.Popen([*self.command, "--", *map(str, files)],
                                          stdin=subprocess.DEVNULL)
            return True

    # --- volume -------------------------------------------------------------

    @property
    def volume(self) -> int | None:
        """The selected hardware volume in percent, or None with no mixer."""
        return self.mixer.limit if self.mixer is not None else None

    def set_volume(self, level: int) -> bool:
        """Select a new volume. While something plays it is applied and read
        back at once; if that fails, playback stops. False if not applied now
        (it will be applied, and checked, before the next sound)."""
        if self.mixer is None:
            raise ControlError("volume control needs mixer_volume in the config")
        with self._lock:
            self.mixer.set_level(level)
            if not self.playing:
                return self.mixer.set_and_verify()
            if self.mixer.set_and_verify():
                return True
            log.error("could not confirm the new %d%% volume during playback; stopping it", level)
            self.stop()
            return False

    # --- pause / resume / seek ----------------------------------------------

    def pause(self) -> float:
        """Pause the song; return the position it paused at, in seconds."""
        with self._lock:
            self._require_song()
            self._ipc("set_property", "pause", True)
            return float(self._ipc("get_property", "time-pos") or 0.0)

    def resume(self) -> bool:
        """Resume after a pause. False (still paused) if the volume check fails."""
        with self._lock:
            self._require_song()
            if self.mixer is not None and not self.mixer.set_and_verify():
                log.error("not resuming: could not confirm the volume is at most %d%%",
                          self.mixer.limit)
                return False
            self._ipc("set_property", "pause", False)
            return True

    def seek(self, seconds: float) -> float:
        """Jump to `seconds` into the song; return where it landed."""
        with self._lock:
            self._require_song()
            self._ipc("seek", max(0.0, float(seconds)), "absolute")
            return float(self._ipc("get_property", "time-pos") or 0.0)

    def _require_song(self) -> None:
        if not self.controllable:
            raise ControlError("pause and seek are not available with this setup"
                               + (" (play_seconds is set)" if self.play_seconds else ""))
        if not self.playing:
            raise ControlError("nothing is playing")
        # The playlist is [chime, song]; index 1 is the song.
        if self._ipc("get_property", "playlist-pos") != 1:
            raise ControlError("wait for the chime to finish")

    def _ipc(self, *command):
        """Send one command to mpv's control socket and return its result."""
        deadline = time.monotonic() + IPC_TIMEOUT
        while True:  # mpv creates the socket a moment after it starts
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(IPC_TIMEOUT)
            try:
                sock.connect(str(self.ipc_path))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                sock.close()
                if time.monotonic() >= deadline:
                    raise ControlError("the player is not ready yet") from None
                time.sleep(0.05)
        self._request_id += 1
        request_id = self._request_id
        try:
            with sock:
                sock.sendall(json.dumps({"command": list(command),
                                         "request_id": request_id}).encode() + b"\n")
                buf = b""
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        raise ControlError("the player closed the connection")
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        msg = json.loads(line)
                        if msg.get("request_id") != request_id:
                            continue  # an event, or another answer
                        if msg.get("error") != "success":
                            raise ControlError(f"the player refused: {msg.get('error')}")
                        return msg.get("data")
        except (OSError, ValueError) as e:
            raise ControlError(f"could not talk to the player: {e}") from None

    # --- safety --------------------------------------------------------------

    def check(self) -> None:
        """Call often while running: enforces the time and volume limits."""
        with self._lock:
            self._check()

    def _check(self) -> None:
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
        with self._lock:
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
            if self.ipc_path is not None:
                self.ipc_path.unlink(missing_ok=True)

    @property
    def playing(self) -> bool:
        return self._proc is not None and self._proc.poll() is None
