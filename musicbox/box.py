"""The core behaviour: card on -> play, card off -> stop.

The box also keeps a "now playing" state and publishes it, plus each tap,
to the web page through an optional EventBus, and takes pause / resume /
seek requests from the web page (see `control`).
"""

from __future__ import annotations

import logging
import threading
import time

from .media import display_name, track_duration
from .player import ControlError
from .reader import READ_ERROR

log = logging.getLogger(__name__)

VOLUME_STEPS = (0, 20, 40, 60, 80, 100)


class MusicBox:
    def __init__(self, reader, player, cards, misses_to_remove: int = 3,
                 taplog=None, events=None, clock=time.time,
                 duration_of=track_duration) -> None:
        self.reader = reader
        self.player = player
        self.cards = cards
        self.taplog = taplog
        self.events = events
        self.clock = clock
        self.duration_of = duration_of
        self.misses_to_remove = misses_to_remove
        self.current_uid: str | None = None
        self.now: dict = {"state": "idle"}
        self._misses = 0
        self._started = False
        self._tap_seq = 0  # bumped on every tap or lift, so late duration lookups are ignored
        # Guards `now`, current_uid and the player against the duration
        # thread and web requests (pause/resume/seek).
        self._now_lock = threading.RLock()

    def tick(self) -> None:
        """Poll the reader once and react."""
        with self._now_lock:
            self.player.check()
            if self.now["state"] in ("playing", "paused") and not self.player.playing:
                # The song ended (or was stopped by a safety check) with the
                # card still on the reader.
                self._set_now({**self.now, "state": "finished"})

        uid = self.reader.read()  # outside the lock: it waits on the reader
        with self._now_lock:
            self._react(uid)

    def _react(self, uid) -> None:
        if uid == READ_ERROR:
            # The reader failed; that is not a card being lifted or placed.
            # Keep the current state, so a card lying on the reader through a
            # glitch doesn't replay its song when reading recovers.
            return

        if not self._started:
            # Only a tap plays. A card already on the reader when the box
            # starts (or restarts after a crash) is noted but stays silent.
            self._started = True
            if uid is not None:
                log.info("card %s already on the reader at start; lift and replace it to play", uid)
                self.current_uid = uid
                self._set_now({"state": "waiting", "uid": uid})
                return

        if uid is None:
            # A card lying on the reader occasionally fails a read, so only
            # treat it as removed after several misses in a row.
            if self.current_uid is not None:
                self._misses += 1
                if self._misses >= self.misses_to_remove:
                    log.info("card %s removed", self.current_uid)
                    self.player.stop()
                    self.current_uid = None
                    self._tap_seq += 1
                    self._set_now({"state": "idle"})
            return

        self._misses = 0
        if uid == self.current_uid:
            return  # Same card still there. If its song ended, stay silent.

        self.current_uid = uid
        self._tap_seq += 1
        track = self.cards.track_for(uid)
        if track is None:
            log.warning("unknown card %s (assign it with: musicbox assign <file>)", uid)
            self._record(uid, None, "unknown")
            self.player.cue()
            self._set_now({"state": "unknown", "uid": uid})
        elif not track.is_file():
            log.error("card %s -> %s, but that file is missing (drive not mounted?)", uid, track)
            rel = self._record(uid, track, "missing")
            self.player.cue()
            self._set_now({"state": "missing", "uid": uid, "track": rel, "name": display_name(rel)})
        else:
            log.info("card %s placed", uid)
            played = self.player.play(track)
            rel = self._record(uid, track, "played" if played is not False else "blocked")
            info = {"uid": uid, "track": rel, "name": display_name(rel)}
            if played is False:
                self._set_now({"state": "blocked", **info})
            else:
                chime = getattr(self.player, "chime_seconds", 0.0)
                self._set_now({"state": "playing", **info, "duration": None,
                               "song_starts_at": self.clock() + chime,
                               "controls": bool(getattr(self.player, "controllable", False))})
                self._look_up_duration(track)

    # --- volume (from the web page) -------------------------------------------

    def volume_state(self) -> dict:
        level = getattr(self.player, "volume", None)
        return {"level": level, "steps": list(VOLUME_STEPS), "available": level is not None}

    def publish_volume(self) -> None:
        if self.events is not None:
            self.events.publish("volume", self.volume_state())

    def set_volume(self, level: int) -> dict:
        """Select one of VOLUME_STEPS. Never starts a sound."""
        if level not in VOLUME_STEPS:
            raise ControlError("volume must be one of " + ", ".join(f"{v}%" for v in VOLUME_STEPS))
        with self._now_lock:
            applied = self.player.set_volume(level)
            log.info("web: volume set to %d%%%s", level, "" if applied else
                     " (not confirmed on the speaker yet; checked again before the next sound)")
            if not applied and self.now["state"] in ("playing", "paused") and not self.player.playing:
                # set_volume stopped playback because the level couldn't be confirmed.
                self._set_now({**self.now, "state": "blocked"})
            self.publish_volume()
            return {**self.volume_state(), "applied": applied}

    # --- pause / resume / seek (from the web page) ----------------------------

    def control(self, action: str, position: float | None = None) -> dict:
        """Pause, resume or seek the current song; return the new state.

        Only the song from the current tap, while its card is still on the
        reader. The web page can never start a song or replay one that ended.
        Raises ControlError with a reason the page can show.
        """
        with self._now_lock:
            now = self.now
            if (now["state"] not in ("playing", "paused") or now.get("uid") is None
                    or now.get("uid") != self.current_uid or not self.player.playing):
                raise ControlError("nothing from a card on the reader is playing")
            if not now.get("controls"):
                raise ControlError("pause and seek are not available with this setup")
            base = {k: v for k, v in now.items() if k not in ("song_starts_at", "position")}

            if action == "pause":
                if now["state"] == "paused":
                    return now
                pos = self.player.pause()
                log.info("web: paused %s at %.1f s", now.get("track"), pos)
                self._set_now({**base, "state": "paused", "position": pos})
            elif action == "resume":
                if now["state"] == "playing":
                    return now
                if not self.player.resume():
                    raise ControlError("not resumed: the volume couldn't be confirmed at a safe level")
                log.info("web: resumed %s", now.get("track"))
                self._set_now({**base, "state": "playing",
                               "song_starts_at": self.clock() - now.get("position", 0.0)})
            elif action == "seek":
                if position is None:
                    raise ControlError("no position given")
                pos = self.player.seek(position)
                log.info("web: moved %s to %.1f s", now.get("track"), pos)
                if now["state"] == "paused":
                    self._set_now({**base, "state": "paused", "position": pos})
                else:
                    self._set_now({**base, "state": "playing", "song_starts_at": self.clock() - pos})
            else:
                raise ControlError(f"unknown action: {action}")
            return self.now

    def _set_now(self, now: dict) -> None:
        with self._now_lock:
            self.now = now
            if self.events is not None:
                self.events.publish("now", now)

    def _look_up_duration(self, track) -> None:
        """Fill in the song's length without delaying the music."""
        tap_seq = self._tap_seq

        def work() -> None:
            seconds = self.duration_of(track)
            if seconds is None:
                return
            with self._now_lock:
                # Only if it's still the same tap; otherwise it's stale.
                if tap_seq == self._tap_seq and self.now["state"] in ("playing", "paused"):
                    self._set_now({**self.now, "duration": seconds})

        threading.Thread(target=work, name="duration", daemon=True).start()

    def _record(self, uid: str, track, result: str) -> str | None:
        """Log the tap; return the track relative to the music directory."""
        rel = None
        if track is not None:
            try:
                rel = track.relative_to(self.cards.music_dir).as_posix()
            except ValueError:
                rel = str(track)
        if self.taplog is None:
            return rel
        try:
            row = self.taplog.record(uid, rel, result)
        except Exception:
            # A logging failure (full disk, locked db) must never stop the music.
            log.exception("could not record tap for card %s", uid)
            return rel
        if self.events is not None and row:
            self.events.publish("tap", row)
        return rel

    def run(self) -> None:
        try:
            while True:
                self.tick()
        finally:
            self.player.stop()
