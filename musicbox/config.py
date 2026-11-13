"""Configuration loading."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("~/.config/musicbox/config.toml")


@dataclass
class Config:
    music_dir: Path = Path("/mnt/music")
    cards_file: Path = Path("~/.config/musicbox/cards.json")
    volume: int = 100
    audio_device: str = ""
    misses_to_remove: int = 3
    read_timeout: float = 0.2

    def __post_init__(self) -> None:
        self.music_dir = Path(self.music_dir).expanduser()
        self.cards_file = Path(self.cards_file).expanduser()


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Load config from `path`, $MUSICBOX_CONFIG, or the default location.

    A missing file at the default location just means "use the defaults";
    a missing file that was asked for explicitly is an error.
    """
    explicit = path or os.environ.get("MUSICBOX_CONFIG")
    config_path = Path(explicit or DEFAULT_CONFIG_PATH).expanduser()
    if not config_path.exists():
        if explicit:
            raise FileNotFoundError(f"config file not found: {config_path}")
        return Config()
    with config_path.open("rb") as f:
        data = tomllib.load(f)
    known = {f.name for f in fields(Config)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown config keys in {config_path}: {sorted(unknown)}")
    return Config(**data)
