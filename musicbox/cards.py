"""The card -> tracks mapping, stored as a small JSON file.

Keys are card UIDs as lowercase hex ("04a1b2c3"). A card plays an ordered
list of audio files (paths relative to the music directory). In the file:

    "04a1b2c3": "Song.mp3"                     one track, no settings
    "04d5e6f7": {"tracks": ["a.mp3", "b.mp3"],  several tracks and/or settings
                 "name": "Bedtime", "shuffle": false, "repeat": true}

A custom name only changes what the page shows; files are never touched.
Shuffle and repeat are saved per card.

Older files used {"track": <path>} and could point at a folder. Those load
as a list of the folder's files in name order; `migrate()` saves them in
the new form.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path


AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wav"}
MAX_NAME = 80
MAX_TRACKS = 500
_KEEP = object()  # assign(): leave this setting as it is


def clean_name(name) -> str | None:
    """A custom card name, tidied; None (no custom name) if empty."""
    if name is None:
        return None
    if not isinstance(name, str):
        raise ValueError("name must be text")
    name = " ".join(name.split())  # also drops tabs, newlines and other control whitespace
    if any(ord(ch) < 32 for ch in name):
        raise ValueError("name has control characters")
    if len(name) > MAX_NAME:
        raise ValueError(f"name is longer than {MAX_NAME} characters")
    return name or None


class CardStore:
    def __init__(self, cards_file: Path, music_dir: Path) -> None:
        self.cards_file = cards_file
        self.music_dir = music_dir
        self._mtime: float | None = None
        self._cards: dict[str, dict] = {}
        self._needs_migration = False

    # --- loading ---------------------------------------------------------------

    def _expand(self, rel: str) -> list[str]:
        """An old-style path: a folder becomes its files, in name order."""
        target = self.music_dir / rel
        if target.is_dir():
            files = self.files_for(target)
            if files:
                self._needs_migration = True
                return [f.relative_to(self.music_dir).as_posix() for f in files]
        return [rel]

    def _parse(self, value) -> dict | None:
        if isinstance(value, str):
            return {"tracks": self._expand(value)}
        if not isinstance(value, dict):
            return None
        if isinstance(value.get("tracks"), list):
            tracks = [t for t in value["tracks"] if isinstance(t, str)]
        elif isinstance(value.get("track"), str):
            self._needs_migration = True
            tracks = self._expand(value["track"])
        else:
            return None
        if not tracks:
            return None
        entry = {"tracks": tracks}
        if isinstance(value.get("name"), str) and value["name"].strip():
            entry["name"] = value["name"].strip()
        for flag in ("shuffle", "repeat"):
            if value.get(flag) is True:
                entry[flag] = True
        return entry

    def _load(self) -> dict[str, dict]:
        """Reload if the file changed, so `assign` works while the box runs."""
        try:
            mtime = self.cards_file.stat().st_mtime
        except FileNotFoundError:
            self._mtime, self._cards = None, {}
            return self._cards
        if mtime != self._mtime:
            raw = json.loads(self.cards_file.read_text())
            cards = {}
            for uid, value in raw.items():
                entry = self._parse(value)
                if entry is not None:
                    cards[uid] = entry
            self._cards, self._mtime = cards, mtime
        return self._cards

    def migrate(self) -> bool:
        """Save cards loaded in an older form (folder cards) in the new form."""
        self._mtime = None
        self._needs_migration = False
        cards = self.entries()
        if not self._needs_migration:
            return False
        self._save(cards)
        return True

    # --- reading ---------------------------------------------------------------

    def entries(self) -> dict[str, dict]:
        """{uid: {"tracks": [...], "name"?, "shuffle"?, "repeat"?}}."""
        return {uid: {**e, "tracks": list(e["tracks"])} for uid, e in self._load().items()}

    def all(self) -> dict[str, str]:
        """{uid: first track}; see entries() for everything."""
        return {uid: e["tracks"][0] for uid, e in self._load().items()}

    def tracks_for(self, uid: str) -> list[Path]:
        """Absolute paths of a card's tracks, in the card's order."""
        return [self.music_dir / t for t in self._load().get(uid, {}).get("tracks", [])]

    def track_for(self, uid: str) -> Path | None:
        """The card's first track, if any."""
        tracks = self.tracks_for(uid)
        return tracks[0] if tracks else None

    def custom_name(self, uid: str) -> str | None:
        return self._load().get(uid, {}).get("name")

    def settings(self, uid: str) -> dict:
        e = self._load().get(uid, {})
        return {"shuffle": bool(e.get("shuffle")), "repeat": bool(e.get("repeat"))}

    # --- the music folder --------------------------------------------------------

    @staticmethod
    def _natural(name: str):
        """Sort key so "Part 2" comes before "Part 10"."""
        return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]

    @staticmethod
    def _audio_in(folder: Path) -> list[Path]:
        try:
            entries = list(folder.iterdir())
        except OSError:
            return []
        return [p for p in entries
                if p.suffix.lower() in AUDIO_EXTENSIONS and p.is_file() and not p.name.startswith(".")]

    def folders(self) -> list[str]:
        """Folders under the music directory that directly hold audio files."""
        if not self.music_dir.is_dir():
            return []
        found = {
            Path(t).parent.as_posix() for t in self.tracks() if Path(t).parent != Path(".")
        }
        return sorted(found, key=self._natural)

    def playable(self) -> set[str]:
        """Every path a card may play: audio files."""
        return set(self.tracks())

    def files_for(self, target: Path) -> list[Path]:
        """A file itself, or a folder's audio files in order."""
        if target.is_dir():
            return sorted(self._audio_in(target), key=lambda p: self._natural(p.name))
        return [target] if target.is_file() else []

    def tracks(self) -> list[str]:
        """Every audio file under the music directory, relative to it, sorted."""
        if not self.music_dir.is_dir():
            return []
        found = (
            p.relative_to(self.music_dir).as_posix()
            for p in self.music_dir.rglob("*")
            if p.suffix.lower() in AUDIO_EXTENSIONS and p.is_file()
            and not any(part.startswith(".") for part in p.relative_to(self.music_dir).parts)
        )
        return sorted(found, key=self._natural)

    def relative(self, track: str | os.PathLike) -> str:
        """Normalise a track path to one relative to the music directory."""
        path = Path(track).expanduser()
        if not path.is_absolute():
            # Accept paths relative to either the cwd or the music dir.
            cwd_path = Path.cwd() / path
            path = cwd_path if cwd_path.exists() else self.music_dir / path
        path = path.resolve()
        try:
            rel = path.relative_to(self.music_dir.resolve())
        except ValueError:
            raise ValueError(f"{path} is not inside {self.music_dir}") from None
        if path.is_dir():
            if not self.files_for(path):
                raise FileNotFoundError(f"no audio files in folder: {path}")
        elif not path.is_file():
            raise FileNotFoundError(f"no such track: {path}")
        return rel.as_posix()

    def _relative_tracks(self, tracks) -> list[str]:
        """Paths (or one path) to card tracks; a folder adds its files in order."""
        items = [tracks] if isinstance(tracks, (str, os.PathLike)) else list(tracks)
        out: list[str] = []
        for item in items:
            rel = self.relative(item)
            target = self.music_dir / rel
            if target.is_dir():
                out += [f.relative_to(self.music_dir).as_posix() for f in self.files_for(target)]
            else:
                out.append(rel)
        if not out:
            raise ValueError("a card needs at least one track")
        if len(out) > MAX_TRACKS:
            raise ValueError(f"a card can have at most {MAX_TRACKS} tracks")
        return out

    # --- changing ----------------------------------------------------------------

    def assign(self, uid: str, tracks, name=_KEEP, shuffle=_KEEP, repeat=_KEEP) -> list[str]:
        """Set what `uid` plays: one path or a list, in order (a folder adds its
        files). `name`: a new custom name, None to clear it, or leave it out to
        keep it (different tracks drop it, since the old name would no longer
        fit). `shuffle`/`repeat`: new settings, or leave out to keep them."""
        rels = self._relative_tracks(tracks)
        cards = self.entries()
        old = cards.get(uid, {})
        entry: dict = {"tracks": rels}
        if name is _KEEP:
            if old.get("name") and old.get("tracks") == rels:
                entry["name"] = old["name"]
        else:
            cleaned = clean_name(name)
            if cleaned:
                entry["name"] = cleaned
        for flag, value in (("shuffle", shuffle), ("repeat", repeat)):
            on = old.get(flag, False) if value is _KEEP else bool(value)
            if on:
                entry[flag] = True
        cards[uid] = entry
        self._save(cards)
        return rels

    def set_name(self, uid: str, name) -> str | None:
        """Set or clear (None/"") a card's custom name, keeping its tracks."""
        cards = self.entries()
        if uid not in cards:
            raise KeyError(uid)
        cleaned = clean_name(name)
        cards[uid].pop("name", None)
        if cleaned:
            cards[uid]["name"] = cleaned
        self._save(cards)
        return cleaned

    def set_settings(self, uid: str, shuffle=None, repeat=None) -> dict:
        """Change a card's saved shuffle/repeat (None leaves one as it is)."""
        cards = self.entries()
        if uid not in cards:
            raise KeyError(uid)
        for flag, value in (("shuffle", shuffle), ("repeat", repeat)):
            if value is None:
                continue
            cards[uid].pop(flag, None)
            if value:
                cards[uid][flag] = True
        self._save(cards)
        return {"shuffle": bool(cards[uid].get("shuffle")), "repeat": bool(cards[uid].get("repeat"))}

    # --- keeping cards in step with the music folder (file manager) ---------------

    def users_of(self, rel: str) -> list[str]:
        """Cards that play `rel` (a file) or anything inside it (a folder)."""
        prefix = rel.rstrip("/") + "/"
        return sorted(uid for uid, e in self._load().items()
                      if any(t == rel or t.startswith(prefix) for t in e["tracks"]))

    def moved(self, old: str, new: str) -> list[str]:
        """A file or folder was renamed: point cards at its new path.
        Returns the cards that changed."""
        prefix = old.rstrip("/") + "/"
        cards, changed = self.entries(), []
        for uid, e in cards.items():
            tracks = [new + t[len(old):] if (t == old or t.startswith(prefix)) else t for t in e["tracks"]]
            if tracks != e["tracks"]:
                e["tracks"] = tracks
                changed.append(uid)
        if changed:
            self._save(cards)
        return sorted(changed)

    def deleted(self, rel: str) -> list[str]:
        """A file was deleted: take it out of cards' track lists. A card it
        was the only track of keeps it, so the card shows "file missing"
        rather than vanishing. Returns the cards that changed."""
        cards, changed = self.entries(), []
        for uid, e in cards.items():
            if rel in e["tracks"] and len(e["tracks"]) > 1:
                e["tracks"] = [t for t in e["tracks"] if t != rel]
                changed.append(uid)
        if changed:
            self._save(cards)
        return sorted(changed)

    def rename(self, old: str, new: str) -> list[str]:
        """Give a card a new ID, keeping its tracks, name and settings."""
        cards = self.entries()
        if old not in cards:
            raise KeyError(old)
        if new != old and new in cards:
            raise ValueError(f"card {new} is already assigned")
        cards[new] = cards.pop(old)
        self._save(cards)
        return cards[new]["tracks"]

    def remove(self, uid: str) -> bool:
        cards = self.entries()
        if uid not in cards:
            return False
        del cards[uid]
        self._save(cards)
        return True

    def _save(self, cards: dict[str, dict]) -> None:
        # A plain path for a one-track card with nothing else, so the file
        # stays easy to read and edit by hand.
        out = {}
        for uid, e in cards.items():
            simple = len(e["tracks"]) == 1 and not (e.get("name") or e.get("shuffle") or e.get("repeat"))
            out[uid] = e["tracks"][0] if simple else e
        self.cards_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cards_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
        tmp.replace(self.cards_file)  # atomic: survives a power cut mid-write
        self._mtime = None
