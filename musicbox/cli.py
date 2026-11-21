"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import re
import signal
import sys
import wave
from pathlib import Path

from .box import MusicBox
from .chime import duration as chime_duration
from .chime import prepare_chime
from .cards import CardStore
from .config import load_config
from .events import EventBus
from .player import Mixer, Player
from .reader import Reader
from .taplog import TapLog
from .web import start_web


def _card_name(audio_device: str) -> str:
    """ALSA card name from an mpv device like alsa/plughw:CARD=Foo,DEV=0."""
    m = re.search(r"CARD=([^,]+)", audio_device)
    return m.group(1) if m else ""


def cmd_run(args, config, cards) -> int:
    # systemd stops us with SIGTERM; turn it into a normal exit so the
    # `finally` in MusicBox.run() silences the player.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    taplog = TapLog(config.taps_file)
    events = EventBus()
    chime, chime_seconds = None, 0.0
    if config.chime_file:
        try:
            chime = prepare_chime(Path("~/.cache/musicbox").expanduser(), config.chime_volume,
                                  Path(config.chime_file).expanduser())
            chime_seconds = chime_duration(chime)
        except (OSError, EOFError, wave.Error) as e:
            chime = None
            logging.error("chime unusable, so nothing will play: %s", e)
    else:
        logging.error("no chime_file set, so nothing will play (a song only plays after the chime)")
    mixer = None
    if config.mixer_volume >= 0:
        mixer = Mixer(_card_name(config.audio_device), config.mixer_volume)
    box = MusicBox(
        reader=Reader(timeout=config.read_timeout, serial_port=config.serial_port),
        player=Player(volume=config.volume, audio_device=config.audio_device,
                      chime=chime, chime_seconds=chime_seconds,
                      play_seconds=config.play_seconds, mixer=mixer,
                      ipc_dir=Path("~/.cache/musicbox").expanduser()),
        cards=cards,
        misses_to_remove=config.misses_to_remove,
        taplog=taplog,
        events=events,
    )
    if config.web_enabled:
        try:
            start_web(taplog, cards, events, port=config.web_port, controller=box)
        except OSError as e:
            # Never let the web page keep the music from working.
            logging.error("web page disabled: cannot listen on port %d (%s)", config.web_port, e)
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
    for uid, track in sorted(cards.all().items(), key=lambda kv: kv[1]):
        missing = "" if (config.music_dir / track).is_file() else "  [FILE MISSING]"
        print(f"{uid}  {track}{missing}")
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
