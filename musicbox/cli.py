"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import signal
import sys

from .box import MusicBox
from .cards import CardStore
from .config import load_config
from .player import Player
from .reader import Reader


def cmd_run(args, config, cards) -> int:
    # systemd stops us with SIGTERM; turn it into a normal exit so the
    # `finally` in MusicBox.run() silences the player.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    box = MusicBox(
        reader=Reader(timeout=config.read_timeout),
        player=Player(volume=config.volume, audio_device=config.audio_device),
        cards=cards,
        misses_to_remove=config.misses_to_remove,
    )
    logging.info("music box ready: %d cards, music in %s", len(cards.all()), config.music_dir)
    box.run()
    return 0


def cmd_assign(args, config, cards) -> int:
    # Validate every path before asking for any taps.
    tracks = [cards.relative(t) for t in args.tracks]
    reader = Reader(timeout=config.read_timeout)
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
    reader = Reader(timeout=config.read_timeout)
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
