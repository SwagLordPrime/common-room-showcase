#!/usr/bin/env python3
"""Print a paste-ready Common Room join message for one seat.

Usage:
    python3 crew/join_card.py SEAT [--tree PATH] [--plan PATH] [--scope ID] [--no-python | --python]

--no-python gives a relay seat the card that writes request files by hand
(for a chat that cannot run Python); --python the relay_ask.py helper card.
Without either, each seat kind keeps its default.

A thin script: it adds the room tree to sys.path, imports
common_room.join_cards from it (a pure-stdlib module with no other
common_room import), reads the tree's NON-secret
.runtime/handoffs/enrollment.json, and prints the rendered card. It runs
standalone in a Cowork Linux VM against the mounted tree, or anywhere else
Python 3 is available; it never reads any *-credential.json file.

`--tree` defaults to the folder above this crew/ directory (the room tree
root, which contains both crew/ and common_room/).
"""
import argparse
import json
import sys
from pathlib import Path


def _utf8_streams():
    """The card has a middle dot in its first line. A pipe on Windows defaults to the console code page (cp1252,
    cp437), so whoever reads our stdout would see byte 0xB7 where UTF-8 is expected; say UTF-8 explicitly, always."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if callable(reconfigure):
            try:
                reconfigure(encoding='utf-8', errors='replace')
            except (ValueError, OSError):
                pass


def main(argv=None):
    _utf8_streams()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('seat', help='The seat id to build a join card for.')
    parser.add_argument('--tree', type=Path, default=None,
                         help='Room tree root (default: the folder above this crew/ directory).')
    parser.add_argument('--plan', default=None, help='Plan file path to mention on the card.')
    parser.add_argument('--scope', default=None, help='Scope/authority id to mention on the card.')
    helper = parser.add_mutually_exclusive_group()
    helper.add_argument('--no-python', dest='python', action='store_false', default=None,
                        help='Relay seats: a card that writes the request files by hand, for a chat that cannot run Python.')
    helper.add_argument('--python', dest='python', action='store_true',
                        help='Relay seats: a card that uses the relay_ask.py helper.')
    args = parser.parse_args(argv)

    tree = args.tree.resolve() if args.tree is not None else Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(tree))
    try:
        from common_room import join_cards
    except ImportError as exc:
        print('Could not import common_room.join_cards from ' + str(tree) + ': ' + str(exc), file=sys.stderr)
        return 1

    enrollment_path = tree / '.runtime' / 'handoffs' / 'enrollment.json'
    try:
        raw = enrollment_path.read_text(encoding='utf-8')
    except OSError as exc:
        print('Could not read ' + str(enrollment_path) + ': ' + str(exc), file=sys.stderr)
        return 1
    try:
        enrollment = json.loads(raw)
    except ValueError as exc:
        print(str(enrollment_path) + ' is not valid JSON: ' + str(exc), file=sys.stderr)
        return 1

    tier = None   # A4: the seat's own tier label, when crew/seat-tiers.json names it
    tiers_path = tree / 'crew' / 'seat-tiers.json'
    try:
        tier = (json.loads(tiers_path.read_text(encoding='utf-8')).get('seat_tiers') or {}).get(args.seat)
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError) as exc:
        print('Could not read ' + str(tiers_path) + ': ' + str(exc), file=sys.stderr)
        return 1
    extra = {}   # A3: the chat flag, through the one decision function, on the tree this card is for
    try:
        from common_room import chat as chat_flag
    except ImportError:
        chat_flag = None
    if chat_flag is not None:
        try:
            pair = chat_flag.chat_pair(tree / chat_flag.FLAG_PATH)
        except chat_flag.ChatFlagError as exc:
            print('No card: ' + str(exc), file=sys.stderr)
            return 2
        if pair is not None:
            extra['chat'] = pair
    try:
        card = join_cards.render_card(args.seat, enrollment, tree=tree, plan_path=args.plan, scope=args.scope,
                                      python=args.python, tier=tier, **extra)
    except join_cards.JoinCardError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(card)
    return 0


if __name__ == '__main__':
    sys.exit(main())
