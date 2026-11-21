"""The core behaviour: card on -> play, card off -> stop."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class MusicBox:
    def __init__(self, reader, player, cards, misses_to_remove: int = 3,
                 taplog=None) -> None:
        self.reader = reader
        self.player = player
        self.cards = cards
        self.taplog = taplog
        self.misses_to_remove = misses_to_remove
        self.current_uid: str | None = None
        self._misses = 0

    def tick(self) -> None:
        """Poll the reader once and react."""
        uid = self.reader.read()

        if uid is None:
            # A card lying on the reader occasionally fails a read, so only
            # treat it as removed after several misses in a row.
            if self.current_uid is not None:
                self._misses += 1
                if self._misses >= self.misses_to_remove:
                    log.info("card %s removed", self.current_uid)
                    self.player.stop()
                    self.current_uid = None
            return

        self._misses = 0
        if uid == self.current_uid:
            return  # Same card still there. If its song ended, stay silent.

        self.current_uid = uid
        track = self.cards.track_for(uid)
        if track is None:
            log.warning("unknown card %s (assign it with: musicbox assign <file>)", uid)
            self._record(uid, None, "unknown")
            self.player.stop()
        elif not track.is_file():
            log.error("card %s -> %s, but that file is missing (drive not mounted?)", uid, track)
            self._record(uid, track, "missing")
            self.player.stop()
        else:
            log.info("card %s placed", uid)
            self._record(uid, track, "played")
            self.player.play(track)

    def _record(self, uid: str, track, result: str) -> None:
        if self.taplog is None:
            return
        rel = None
        if track is not None:
            try:
                rel = track.relative_to(self.cards.music_dir).as_posix()
            except ValueError:
                rel = str(track)
        try:
            self.taplog.record(uid, rel, result)
        except Exception:
            # A logging failure (full disk, locked db) must never stop the music.
            log.exception("could not record tap for card %s", uid)

    def run(self) -> None:
        try:
            while True:
                self.tick()
        finally:
            self.player.stop()
