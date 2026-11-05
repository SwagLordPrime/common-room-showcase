"""Before the room starts: does it need the shipped enrollment, and would the room accept it?
(seat-dot, plan turnons-2026-10-03, 2026-10-05.)

"Start Common Room Crew.cmd" runs this from the tree root before it starts the room:

    python -X utf8 -B crew/enroll_check.py SEAT [SEAT ...]

It reads only the two NON-secret enrollment files (the shipped crew/enrollment-crew-2026-09-24.json and the
room's .runtime/handoffs/enrollment.json), never a credential, and writes nothing. It checks with the room's
own rule (common_room.activation.merge_enrollment): seats are only appended, and a scope may only gain
members, so a seat added to the shipped file reaches the running room at its next start, and nothing else.

Exit codes (one plain line on stdout each time):
  0  nothing to add: the room already has the shipped enrollment, and every seat in SEATS is enrolled
  3  pass the shipped enrollment (--enrollment): first run, or the shipped file only adds seats or members
  4  the room has the older 09-23 "orchestra" scope (ORCHESTRA_2026-09-24.md, "If the launcher stops")
  2  stop: the room would refuse it (a seat in SEATS is enrolled nowhere, or the shipped file changes or
     drops a seat or scope the room already has). Nothing was started or changed.
"""
import json, sys
from pathlib import Path

TREE = Path(__file__).resolve().parents[1]
SHIPPED = Path('crew') / 'enrollment-crew-2026-09-24.json'
LIVE = Path('.runtime') / 'handoffs' / 'enrollment.json'


def decide(live, shipped, seats):
    """(exit code, line) for a live record (None when the room has none yet), the shipped record and SEATS."""
    if str(TREE) not in sys.path:
        sys.path.insert(0, str(TREE))
    from common_room.activation import merge_enrollment, validate_enrollment
    try:
        shipped = validate_enrollment(shipped)
    except ValueError as exc:
        return 2, 'The shipped enrollment ' + str(SHIPPED) + ' is not valid (' + str(exc) + '). Nothing was started.'
    known = {row['id'] for row in shipped['actors']}
    missing = [seat for seat in seats if seat not in known]
    if missing:
        return 2, ('SEATS names ' + ', '.join(missing) + ', which the shipped enrollment does not enroll. '
                   'Nothing was started.')
    if live is None:
        return 3, 'First run: adding the shipped enrollment.'
    try:
        live = validate_enrollment(live)
    except ValueError as exc:
        return 2, 'The room\'s enrollment.json is not valid (' + str(exc) + '). Nothing was started.'
    if live == shipped:
        return 0, 'Enrollment up to date.'
    try:
        merge_enrollment(live, shipped)
    except ValueError:
        # The room keeps an enrollment of its own (the orchestra-b fix, or an explicit --enrollment run).
        enrolled = {row['id'] for row in live['actors']}
        if all(seat in enrolled for seat in seats):
            return 0, 'This room keeps its own enrollment (it differs from the shipped file); starting it unchanged.'
        if 'orchestra' in {row['id'] for row in live['authorities']} and 'claude-code-b' not in enrolled:
            return 4, 'This room already has an older "orchestra" scope without the claude-code seats.'
        return 2, ('SEATS names ' + ', '.join(seat for seat in seats if seat not in enrolled) + ', which this room does '
                   'not enroll, and the shipped enrollment would change or drop a seat or scope this room already '
                   'has (the room only ever adds them). Nothing was started.')
    added = sorted({row['id'] for row in shipped['actors']} - {row['id'] for row in live['actors']})
    return 3, 'Adding to the room at this start: ' + (', '.join(added) if added else 'new scope members') + '.'


def main(argv=None):
    seats = sys.argv[1:] if argv is None else list(argv)
    try:
        shipped = json.loads((TREE / SHIPPED).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        print('The shipped enrollment ' + str(SHIPPED) + ' could not be read (' + type(exc).__name__ + '). Nothing was started.')
        return 2
    try:
        live = json.loads((TREE / LIVE).read_text(encoding='utf-8'))
    except FileNotFoundError:
        live = None
    except (OSError, ValueError) as exc:
        print('The room\'s enrollment.json could not be read (' + type(exc).__name__ + '). Nothing was started.')
        return 2
    code, line = decide(live, shipped, seats)
    print(line)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
