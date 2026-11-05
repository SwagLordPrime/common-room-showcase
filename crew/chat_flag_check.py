"""Check the A3 chat flag file with the same parser the room runs at start (common_room.chat.chat_pair).

    py -3 -B crew/chat_flag_check.py [<path>]      default: crew/chat-pair.json in this tree

Prints one line and exits: `CHAT FLAG: ON` (0), `CHAT FLAG: OFF` (0, the file is absent or says "off"), or
`CHAT FLAG: BAD: <reason>` (2: the room would refuse to start on it). It reads the file and nothing else; it
writes nothing and reaches no room. For the yes-#4 sheet: check the file before the restart window. The undo is
to remove crew/chat-pair.json and restart.
"""
import sys
from pathlib import Path

TREE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TREE))

from common_room.chat import FLAG_PATH, ChatFlagError, chat_pair   # noqa: E402


def main(argv):
    if len(argv) > 1:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    path = Path(argv[0]) if argv else TREE / FLAG_PATH
    try:
        pair = chat_pair(path)
    except ChatFlagError as exc:
        print('CHAT FLAG: BAD: ' + str(exc))
        return 2
    print('CHAT FLAG: ON' if pair is not None else 'CHAT FLAG: OFF')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
