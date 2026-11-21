"""The card -> track mapping, stored as a small JSON file.

Keys are card UIDs as lowercase hex ("04a1b2c3"), values are track paths
relative to the music directory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wav"}


class CardStore:
    def __init__(self, cards_file: Path, music_dir: Path) -> None:
        self.cards_file = cards_file
        self.music_dir = music_dir
        self._mtime: float | None = None
        self._cards: dict[str, str] = {}

    def _load(self) -> dict[str, str]:
        """Reload if the file changed, so `assign` works while the box runs."""
        try:
            mtime = self.cards_file.stat().st_mtime
        except FileNotFoundError:
            self._mtime, self._cards = None, {}
            return self._cards
        if mtime != self._mtime:
            self._cards = json.loads(self.cards_file.read_text())
            self._mtime = mtime
        return self._cards

    def all(self) -> dict[str, str]:
        return dict(self._load())

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
        return sorted(found, key=str.lower)

    def track_for(self, uid: str) -> Path | None:
        """Absolute path of the track assigned to `uid`, if any."""
        rel = self._load().get(uid)
        return self.music_dir / rel if rel else None

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
        if not path.is_file():
            raise FileNotFoundError(f"no such track: {path}")
        return rel.as_posix()

    def assign(self, uid: str, track: str | os.PathLike) -> str:
        rel = self.relative(track)
        cards = self.all()
        cards[uid] = rel
        self._save(cards)
        return rel

    def remove(self, uid: str) -> bool:
        cards = self.all()
        if uid not in cards:
            return False
        del cards[uid]
        self._save(cards)
        return True

    def _save(self, cards: dict[str, str]) -> None:
        self.cards_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cards_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(cards, indent=2, sort_keys=True) + "\n")
        tmp.replace(self.cards_file)  # atomic: survives a power cut mid-write
        self._mtime = None
