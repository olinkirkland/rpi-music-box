"""Command line entry point."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import socket
import re
import signal
import sys
import threading
import wave
from pathlib import Path

from .artwork import ArtStore, MusicBrainz
from .box import MusicBox
from .chime import duration as chime_duration
from .chime import prepare_chime
from .cards import CardStore
from .config import DEFAULT_CONFIG_PATH, load_config
from .files import FileManager
from .positions import PositionStore
from .wifi import WifiClient, WifiWatcher
from .events import EventBus
from .player import Mixer, Player
from .reader import Reader
from .taplog import TapLog
from .web import library, start_web


def _card_name(audio_device: str) -> str:
    """ALSA card name from an mpv device like alsa/plughw:CARD=Foo,DEV=0."""
    m = re.search(r"CARD=([^,]+)", audio_device)
    return m.group(1) if m else ""


# The sound effects that ship with the app.
SOUNDS_DIR = Path(__file__).resolve().parent / "sounds"


def _where_things_live(args, config, art_dir: Path, cache_dir: Path) -> dict:
    """Paths and commands for troubleshooting on the Pi itself."""
    config_file = Path(args.config or os.environ.get("MUSICBOX_CONFIG") or DEFAULT_CONFIG_PATH).expanduser()
    host = socket.gethostname()
    user = getpass.getuser()
    return {
        "music_dir": str(config.music_dir),
        "config_file": str(config_file),
        "cards_file": str(config.cards_file),
        "taps_file": str(config.taps_file),
        "art_dir": str(art_dir),
        "cache_dir": str(cache_dir),
        "app_dir": str(Path(__file__).resolve().parent.parent),
        "ssh": f"ssh {user}@{host.lower()}.local",
        "copy_files": f"scp <files> {user}@{host.lower()}.local:{config.music_dir}/",
        "logs": "journalctl -u musicbox -f",
        "restart": "sudo systemctl restart musicbox",
    }


def cmd_run(args, config, cards) -> int:
    # systemd stops us with SIGTERM; turn it into a normal exit so the
    # `finally` in MusicBox.run() silences the player.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    taplog = TapLog(config.taps_file)
    events = EventBus()
    cache = Path("~/.cache/musicbox").expanduser()

    def load_sound(setting: str, bundled: str, what: str):
        """(cached WAV at chime_volume, its length) for one sound, or (None, 0)."""
        if setting.strip().lower() == "off":
            return None, 0.0
        source = Path(setting).expanduser() if setting.strip() else SOUNDS_DIR / bundled
        try:
            path = prepare_chime(cache, config.chime_volume, source)
            return path, chime_duration(path)
        except (OSError, EOFError, wave.Error) as e:
            logging.error("%s sound unusable (%s): %s", what, source, e)
            return None, 0.0

    chime, chime_seconds = load_sound(config.chime_file, "chime-add-card.mp3", "card-placed")
    if chime is None:
        logging.error("no card-placed chime, so nothing will play (a song only plays after the chime)")
    sounds = {}
    for name, setting, bundled in (("remove", config.remove_chime_file, "chime-remove-card.mp3"),
                                   ("assign", config.assign_chime_file, "chime-assign.mp3")):
        path, seconds = load_sound(setting, bundled, name)
        if path is not None:
            sounds[name] = (path, seconds)
    mixer = None
    if config.mixer_volume >= 0:
        mixer = Mixer(_card_name(config.audio_device), config.mixer_volume)
    box = MusicBox(
        reader=Reader(timeout=config.read_timeout, serial_port=config.serial_port),
        player=Player(volume=config.volume, audio_device=config.audio_device,
                      chime=chime, chime_seconds=chime_seconds, sounds=sounds,
                      play_seconds=config.play_seconds, mixer=mixer,
                      ipc_dir=Path("~/.cache/musicbox").expanduser()),
        cards=cards,
        misses_to_remove=config.misses_to_remove,
        taplog=taplog,
        events=events,
        positions=PositionStore(Path("~/.local/share/musicbox/positions.json").expanduser()),
    )
    if config.web_enabled:
        try:
            wifi = WifiWatcher(WifiClient(), events)
            wifi.start()
            art_dir = Path("~/.local/share/musicbox/art").expanduser()
            cache_dir = Path("~/.cache/musicbox").expanduser()
            start_web(taplog, cards, events, port=config.web_port, controller=box,
                      cover_dir=cache_dir / "covers", art=ArtStore(art_dir),
                      musicbrainz=MusicBrainz(cache_dir / "musicbrainz"),
                      files=FileManager(config.music_dir, cards),
                      paths=_where_things_live(args, config, art_dir, cache_dir), wifi=wifi)
            # Read every file's tags once in the background, so the first
            # page load doesn't wait for ffprobe on the whole library.
            threading.Thread(target=library, args=(cards,), name="warm-tags", daemon=True).start()
        except OSError as e:
            # Never let the web page keep the music from working.
            logging.error("web page disabled: cannot listen on port %d (%s)", config.web_port, e)
    if cards.migrate():
        logging.info("cards.json: saved folder cards as track lists")
    box.publish_volume()  # starts at mixer_volume; web changes last until restart
    logging.info("music box ready: %d cards, music in %s", len(cards.all()), config.music_dir)
    box.run()
    return 0


def cmd_assign(args, config, cards) -> int:
    # Validate every path before asking for any taps.
    tracks = [cards.relative(t) for t in args.tracks]
    reader = Reader(timeout=config.read_timeout, serial_port=config.serial_port)
    for track in tracks:
        print(f"Tap a card for: {track}")
        uid = reader.wait_for_card()
        previous = cards.all().get(uid)
        cards.assign(uid, config.music_dir / track)
        note = f" (was: {previous})" if previous and previous != track else ""
        print(f"  {uid} -> {track}{note}")
        if track != tracks[-1]:
            print("  Remove the card.")
            reader.wait_for_removal()
    return 0


def cmd_scan(args, config, cards) -> int:
    reader = Reader(timeout=config.read_timeout, serial_port=config.serial_port)
    print("Tap cards to see their UIDs. Ctrl-C to quit.")
    while True:
        uid = reader.wait_for_card()
        print(f"{uid}  {cards.all().get(uid, '(unassigned)')}")
        reader.wait_for_removal()


def cmd_list(args, config, cards) -> int:
    for uid, entry in sorted(cards.entries().items(), key=lambda kv: kv[1]["tracks"][0]):
        tracks = entry["tracks"]
        missing = sum(not (config.music_dir / t).is_file() for t in tracks)
        flags = [f'"{entry["name"]}"'] if entry.get("name") else []
        flags += [f for f in ("shuffle", "repeat") if entry.get(f)]
        if missing:
            flags.append(f"[{missing} FILE{'S' if missing > 1 else ''} MISSING]")
        extra = ("  " + "  ".join(flags)) if flags else ""
        print(f"{uid}  {tracks[0]}" + (f"  (+{len(tracks) - 1} more)" if len(tracks) > 1 else "") + extra)
    return 0


def cmd_remove(args, config, cards) -> int:
    if not cards.remove(args.uid.lower()):
        print(f"no such card: {args.uid}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="musicbox", description="Tap an NFC card, hear a song.")
    parser.add_argument("--config", help="path to config.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="run the music box (what the service does)").set_defaults(func=cmd_run)
    p = sub.add_parser("assign", help="assign cards to tracks, one tap per track")
    p.add_argument("tracks", nargs="+", metavar="TRACK")
    p.set_defaults(func=cmd_assign)
    sub.add_parser("scan", help="print the UID of each card tapped").set_defaults(func=cmd_scan)
    sub.add_parser("list", help="list assigned cards").set_defaults(func=cmd_list)
    p = sub.add_parser("remove", help="unassign a card")
    p.add_argument("uid")
    p.set_defaults(func=cmd_remove)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    try:
        config = load_config(args.config)
        cards = CardStore(config.cards_file, config.music_dir)
        return args.func(args, config, cards)
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
