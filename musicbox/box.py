"""The core behaviour: card on -> play, card off -> stop.

The box also keeps a "now playing" state and publishes it, plus each tap,
to the web page through an optional EventBus, and takes pause / resume /
seek / next / previous / shuffle / repeat requests from the web page (see
`control`).

The play queue: a card's tracks play in the card's order, or shuffled if the
card's Shuffle is on. One pass through all of them is a "round". With
Repeat on, the next round is queued while the last track of a round plays
(a fresh shuffle each round, never starting with the track that just ended).
Turning Shuffle on mid-way plays all the *other* tracks next, freshly
shuffled; turning it off continues with the tracks after the current one, in
the card's order.
"""

from __future__ import annotations

import logging
import random
import threading
import time

from .media import describe_tracks, display_name, part_title, track_duration
from .positions import resume_point
from .player import ControlError
from .reader import READ_ERROR

log = logging.getLogger(__name__)

VOLUME_STEPS = (0, 20, 40, 60, 80, 100)
ASSIGN_LEASE = 20.0  # seconds assigning mode lasts unless the page renews it


class MusicBox:
    def __init__(self, reader, player, cards, misses_to_remove: int = 3,
                 taplog=None, events=None, clock=time.time,
                 duration_of=track_duration, rng=None, positions=None) -> None:
        self.reader = reader
        self.player = player
        self.positions = positions  # where Remember-playback cards left off
        self._remember = False  # the current tap's card has Remember playback on
        self._next_position_save = 0.0
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
        self._files: list = []  # the current tap's tracks, in the card's order
        # The play queue, as loaded in mpv after the chime: indexes into _files.
        self._queue: list[int] = []
        self._qpos = 0  # which queue entry is playing
        self._round_starts: list[int] = [0]  # queue indexes where each round begins
        self._shuffle = False
        self._repeat = False
        self.rng = rng or random.Random()
        self._assign_until = 0.0  # time.monotonic() until which taps are read, not played
        self._next_part_check = 0.0
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
                if self._remember and self.positions is not None and self.current_uid:
                    self.positions.forget(self.current_uid)
            elif self.now["state"] == "playing" and self._files:
                self._follow_queue()
                if self._remember and time.monotonic() >= self._next_position_save:
                    self._next_position_save = time.monotonic() + 15.0
                    self._save_position()

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
                    self._save_position()
                    self.player.stop()
                    self._effect("remove")
                    self.current_uid = None
                    self._files = []
                    self._queue = []
                    self._tap_seq += 1
                    self._set_now({"state": "idle"})
            return

        self._misses = 0
        if uid == self.current_uid:
            return  # Same card still there. If its song ended, stay silent.

        self.current_uid = uid
        self._tap_seq += 1
        tracks = self.cards.tracks_for(uid)
        track = tracks[0] if tracks else None
        if self.assigning:
            self._read_for_assigning(uid, tracks)
            return
        if track is None:
            log.warning("unknown card %s (assign it with: musicbox assign <file>)", uid)
            self._files = []
            self._record(uid, None, "unknown")
            self.player.cue()
            self._set_now({"state": "unknown", "uid": uid})
            return
        files = [t for t in tracks if t.is_file()]  # skip any that have gone missing
        if not files:
            log.error("card %s -> %s, but that file is missing (drive not mounted?)", uid, track)
            self._files = []
            rel = self._record(uid, track, "missing")
            self.player.cue()
            self._set_now({"state": "missing", "uid": uid, "track": rel,
                           "name": self._custom_name(uid) or display_name(rel)})
            return
        log.info("card %s placed", uid)
        settings = self.cards.settings(uid) if hasattr(self.cards, "settings") else {}
        self._shuffle = bool(settings.get("shuffle"))
        self._repeat = bool(settings.get("repeat"))
        self._remember = bool(settings.get("remember_playback")) and self.positions is not None
        order, start = self._round_order(len(files), last=None), 0.0
        resume = self._resume_for(uid, files) if self._remember else None
        if resume is not None:
            # Carry on: that track first, a few seconds back, then the rest of
            # the round (in order, or a fresh shuffle of all the others).
            index, start = resume
            if self._shuffle:
                rest = [i for i in range(len(files)) if i != index]
                self.rng.shuffle(rest)
                order = [index, *rest]
            else:
                order = list(range(index, len(files)))
            log.info("card %s resumes at track %d, %.0f s", uid, index + 1, start)
        ordered = [files[i] for i in order]
        played = self.player.play(ordered if len(ordered) > 1 else ordered[0], start=start) if start \
            else self.player.play(ordered if len(ordered) > 1 else ordered[0])
        rel = self._record(uid, track, "played" if played is not False else "blocked")
        # File names for now; tag names and lengths follow from a background
        # thread, so reading them never delays the music.
        custom = self._custom_name(uid)
        first = files[order[0]]
        info = {"uid": uid, "track": rel,
                "name": custom or (files[0].stem if len(files) == 1 else f"{files[0].stem} + {len(files) - 1} more"),
                "artist": None, "parts": len(files), "part": order[0], "track_no": 1,
                "part_title": first.stem, "cover": files[0].relative_to(self.cards.music_dir).as_posix(),
                "shuffle": self._shuffle, "repeat": self._repeat,
                "remember_playback": self._remember, "resumed_at": start or None}
        if played is False:
            self._files = []
            self._queue = []
            self._set_now({"state": "blocked", **info})
            return
        self._files = files
        self._queue = order
        self._qpos = 0
        self._round_starts = [0]
        self._next_part_check = time.monotonic() + 1.0
        chime = getattr(self.player, "chime_seconds", 0.0)
        self._next_position_save = time.monotonic() + 15.0
        self._set_now({"state": "playing", **info, "duration": None,
                       "song_starts_at": self.clock() + chime - start,
                       "controls": bool(getattr(self.player, "controllable", False))})
        self._look_up_info(files, custom)

    # --- assigning cards (from the web page) ----------------------------------

    @property
    def assigning(self) -> bool:
        return time.monotonic() < self._assign_until

    def start_assigning(self) -> dict:
        """Read taps silently for the Assign modal, for ASSIGN_LEASE seconds.

        The page calls this again every few seconds while the modal is open,
        so if the page goes away the box returns to normal by itself.
        """
        with self._now_lock:
            if not self.assigning:
                log.info("web: assigning mode on (taps are read and chime; tracks are not played)")
            self._assign_until = time.monotonic() + ASSIGN_LEASE
            return {"active": True}

    def stop_assigning(self) -> dict:
        with self._now_lock:
            if self.assigning:
                log.info("web: assigning mode off")
            self._assign_until = 0.0
            if self.now["state"] == "assigning":
                # The card may still be on the reader; it stays silent until
                # it is lifted and placed again, like at start-up.
                self._set_now({"state": "waiting", "uid": self.now.get("uid")})
            return {"active": False}

    def _read_for_assigning(self, uid: str, tracks) -> None:
        """A tap while the Assign modal is open: report it, and play the assign
        chime (never the card's tracks)."""
        self.player.stop()  # a quick card swap may not have registered a lift
        self._effect("assign")
        self._files = []
        rel = self._record(uid, tracks[0] if tracks else None, "assigning")
        name = self._custom_name(uid)
        if tracks and not name:
            files = [t for t in tracks if t.is_file()]
            name = describe_tracks(files)["name"] if files else display_name(rel)
        log.info("card %s read for assigning (currently %s)", uid, rel or "unassigned")
        if self.events is not None:
            self.events.publish("assign_tap", {"uid": uid, "track": rel, "name": name})
        self._set_now({"state": "assigning", "uid": uid})

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

    def control(self, action: str, position: float | None = None, value=None) -> dict:
        """Pause, resume, seek, next, previous, shuffle or repeat; return the
        new state. (`value` is the on/off for shuffle and repeat.)

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
            elif action == "next":
                if not self.player.next_track():
                    if self._repeat:
                        self._queue_next_round()  # just in case it isn't queued yet
                        if not self.player.next_track():
                            raise ControlError("the next round isn't ready yet; try again")
                    else:
                        log.info("web: next past the last track; stopping")
                        self.player.stop()
                        self._set_now({**base, "state": "finished"})
                        if self._remember and self.positions is not None:
                            self.positions.forget(self.current_uid)  # all played: start over next time
                        return self.now
                self._moved_to(self._qpos + 1, 0.0)
                log.info("web: next track")
            elif action == "previous":
                self._moved_to(self.player.previous_track(), 0.0)
                log.info("web: previous track")
            elif action == "shuffle":
                self._set_shuffle(bool(value))
            elif action == "repeat":
                self._set_repeat(bool(value))
            else:
                raise ControlError(f"unknown action: {action}")
            return self.now

    # --- the play queue ------------------------------------------------------

    def _round_order(self, count: int, last: int | None) -> list[int]:
        """One round of track indexes: the card's order, or a fresh shuffle
        that doesn't start with `last` (the track that just played)."""
        order = list(range(count))
        if self._shuffle and count > 1:
            self.rng.shuffle(order)
            if order[0] == last:
                order[0], order[1] = order[1], order[0]
        return order

    def _round_start(self) -> int:
        return max(s for s in self._round_starts if s <= self._qpos)

    def _moved_to(self, qpos: int, position: float) -> None:
        """mpv is now on queue entry `qpos`, `position` seconds in."""
        if not 0 <= qpos < len(self._queue):
            return
        self._qpos = qpos
        index = self._queue[qpos]
        update = {"part": index, "part_title": part_title(self._files[index]),
                  "duration": self.duration_of(self._files[index]),
                  "track_no": qpos - self._round_start() + 1}
        base = {k: v for k, v in self.now.items() if k not in ("song_starts_at", "position")}
        if self.now["state"] == "paused":
            self._set_now({**base, **update, "position": position})
        else:
            self._set_now({**base, **update, "song_starts_at": self.clock() - position})

    def _queue_next_round(self) -> None:
        """With Repeat on: queue the next round after the last queued track."""
        if not (self._repeat and self._files and self._qpos == len(self._queue) - 1):
            return
        order = self._round_order(len(self._files), last=self._queue[-1])
        try:
            self.player.append_tracks([self._files[i] for i in order])
        except ControlError as e:
            log.warning("couldn't queue the next round: %s", e)
            return
        self._round_starts.append(len(self._queue))
        self._queue += order
        log.info("repeat: queued round %d", len(self._round_starts))

    def _follow_queue(self) -> None:
        """Notice when mpv moves to the next track; queue the next round."""
        now = time.monotonic()
        if now < self._next_part_check or not getattr(self.player, "controllable", False):
            return
        self._next_part_check = now + 1.0
        state = self.player.playlist_state()
        if state is None:  # still the chime, or the player is busy
            return
        qpos, position = state
        if qpos != self._qpos:
            index = self._queue[qpos] if 0 <= qpos < len(self._queue) else None
            if index is not None:
                log.info("now playing track %d of %d: %s", index + 1, len(self._files), self._files[index])
            self._moved_to(qpos, position)
        self._queue_next_round()

    def _require_track_playing(self) -> int:
        """The card index of the track playing; refuses during the chime."""
        state = self.player.playlist_state() if hasattr(self.player, "playlist_state") else None
        if state is None:
            raise ControlError("wait for the chime to finish")
        if state[0] != self._qpos:
            self._moved_to(state[0], state[1])
        return self._queue[self._qpos]

    def _save_setting(self, **flag) -> None:
        if self.current_uid and hasattr(self.cards, "set_settings"):
            try:
                self.cards.set_settings(self.current_uid, **flag)
            except KeyError:
                return  # the card was removed meanwhile; the playback still changes
            if self.events is not None:
                self.events.publish("cards", {})  # pages show the card's new setting

    def _set_shuffle(self, on: bool) -> None:
        if on == self._shuffle:
            return
        current = self._require_track_playing() if len(self._files) > 1 else None
        self._shuffle = on
        self._save_setting(shuffle=on)
        if current is not None:
            if on:  # everything else next, freshly shuffled
                rest = [i for i in range(len(self._files)) if i != current]
                self.rng.shuffle(rest)
            else:  # on in the card's order from here
                rest = list(range(current + 1, len(self._files)))
            self.player.replace_upcoming([self._files[i] for i in rest])
            self._queue = self._queue[: self._qpos + 1] + rest
            # This is now the round: shuffled, it begins here; in order, the
            # track number follows the card.
            self._round_starts = [s for s in self._round_starts if s <= self._qpos]
            if on:
                self._round_starts.append(self._qpos)
            else:
                self._round_starts.append(self._qpos - current)
            self._queue_next_round()
        log.info("web: shuffle %s", "on" if on else "off")
        self._set_now({**self.now, "shuffle": on,
                       "track_no": self._qpos - self._round_start() + 1})

    def _set_repeat(self, on: bool) -> None:
        if on == self._repeat:
            return
        self._repeat = on
        self._save_setting(repeat=on)
        if not on:
            # Drop a next round that was already queued.
            round_end = max(len(self._queue) - 1, self._qpos)
            later = [s for s in self._round_starts if s > self._qpos]
            if later:
                round_end = min(later) - 1
            if round_end < len(self._queue) - 1:
                keep = self._queue[self._qpos + 1: round_end + 1]
                self.player.replace_upcoming([self._files[i] for i in keep])
                self._queue = self._queue[: round_end + 1]
                self._round_starts = [s for s in self._round_starts if s <= round_end]
        else:
            self._queue_next_round()
        log.info("web: repeat %s", "on" if on else "off")
        self._set_now({**self.now, "repeat": on})

    # --- Remember playback ------------------------------------------------------

    def _resume_for(self, uid: str, files) -> tuple[int, float] | None:
        """(track index, seconds) to resume this card at, or None to start over."""
        saved = self.positions.get(uid)
        if not saved:
            return None
        rels = [f.relative_to(self.cards.music_dir).as_posix() for f in files]
        if saved["track"] not in rels:
            return None  # the card's tracks changed since: start over
        return rels.index(saved["track"]), resume_point(saved["position"])

    def _save_position(self) -> None:
        """Remember where the current card is (Remember playback cards only)."""
        if not (self._remember and self.current_uid and self._files and self.positions is not None):
            return
        now = self.now
        if now.get("state") == "paused":
            position = float(now.get("position") or 0.0)
        elif now.get("state") == "playing" and now.get("song_starts_at") is not None:
            position = self.clock() - now["song_starts_at"]
            if position < 0:
                return  # still the chime: nothing new to remember
        else:
            return
        part = now.get("part", 0)
        if not 0 <= part < len(self._files):
            return
        track = self._files[part].relative_to(self.cards.music_dir).as_posix()
        try:
            self.positions.save(self.current_uid, track, position)
        except OSError as e:
            log.warning("couldn't remember the position of card %s: %s", self.current_uid, e)

    def stop_for_power(self) -> None:
        """Before a restart or shutdown from the page: stop the music and
        remember where a Remember-playback card was."""
        with self._now_lock:
            self._save_position()
            self.player.stop()
            if self.now["state"] in ("playing", "paused"):
                self._set_now({**self.now, "state": "finished"})

    def _effect(self, name: str) -> None:
        """A short sound effect, if the player has one by that name."""
        play = getattr(self.player, "sound", None)
        if play is not None:
            play(name)

    def _custom_name(self, uid: str) -> str | None:
        get = getattr(self.cards, "custom_name", None)
        return get(uid) if get else None

    def _set_now(self, now: dict) -> None:
        with self._now_lock:
            self.now = now
            if self.events is not None:
                self.events.publish("now", now)

    def _look_up_info(self, files, custom_name: str | None = None) -> None:
        """Fill in tag names and the current part's length without delaying the music."""
        tap_seq = self._tap_seq

        def work() -> None:
            shown = describe_tracks(files)
            titles = [part_title(f) for f in files] if len(files) > 1 else None
            durations = {0: self.duration_of(files[0])}
            with self._now_lock:
                # Only if it's still the same tap; otherwise it's stale.
                if tap_seq != self._tap_seq or self.now["state"] not in ("playing", "paused"):
                    return
                part = self.now.get("part", 0)
                if part not in durations:
                    durations[part] = self.duration_of(files[part])
                update = {"name": custom_name or shown["name"], "artist": shown["artist"],
                          "duration": durations[part]}
                if titles:
                    update["part_title"] = titles[part]
                self._set_now({**self.now, **update})

        threading.Thread(target=work, name="track-info", daemon=True).start()

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
