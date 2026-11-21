"""The core behaviour: card on -> play, card off -> stop.

The box also keeps a "now playing" state and publishes it, plus each tap,
to the web page through an optional EventBus.
"""

from __future__ import annotations

import logging
import threading
import time

from .media import display_name, track_duration
from .reader import READ_ERROR

log = logging.getLogger(__name__)


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
        self._play_id = 0  # bumped on every change, so late duration lookups are ignored
        self._now_lock = threading.RLock()  # the duration thread also updates `now`

    def tick(self) -> None:
        """Poll the reader once and react."""
        self.player.check()
        if self.now["state"] == "playing" and not self.player.playing:
            # The song ended (or was stopped by a safety check) with the card
            # still on the reader.
            self._set_now({**self.now, "state": "finished"})

        uid = self.reader.read()

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
                    self._set_now({"state": "idle"})
            return

        self._misses = 0
        if uid == self.current_uid:
            return  # Same card still there. If its song ended, stay silent.

        self.current_uid = uid
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
                               "song_starts_at": self.clock() + chime})
                self._look_up_duration(track)

    def _set_now(self, now: dict) -> None:
        with self._now_lock:
            self._play_id += 1
            self.now = now
            if self.events is not None:
                self.events.publish("now", now)

    def _look_up_duration(self, track) -> None:
        """Fill in the song's length without delaying the music."""
        play_id = self._play_id

        def work() -> None:
            seconds = self.duration_of(track)
            if seconds is None:
                return
            with self._now_lock:
                # Only if nothing has changed since; otherwise it's stale.
                if play_id == self._play_id and self.now["state"] == "playing":
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
