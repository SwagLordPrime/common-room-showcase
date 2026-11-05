"""The fix for "the room has the 09-23 orchestra scope" (see ORCHESTRA_2026-09-24.md, "If the launcher stops").

Grants are immutable, so an older `orchestra` scope cannot be changed. This writes
`.runtime/handoffs/enrollment-crew-orchestra-b.json`: the room's live enrollment.json unchanged, plus
the claude-code seats and a new scope `orchestra-b` (the 09-24 orchestra members and wording) appended.

    python3 crew/make_orchestra_b.py <tree>

It reads only the two NON-secret enrollment files, never a credential, and refuses to overwrite an
existing output. Then Shane starts the crew once with the line it prints. Python 3.8+, stdlib only.
"""
import json, sys
from pathlib import Path

SHIPPED = Path('crew') / 'enrollment-crew-2026-09-24.json'
LIVE = Path('.runtime') / 'handoffs' / 'enrollment.json'
OUTPUT = Path('.runtime') / 'handoffs' / 'enrollment-crew-orchestra-b.json'
NEW_SCOPE = 'orchestra-b'
WINDOWS_TREE = r'D:\Work Area\Artifacts\common-room\tree'


def build(live, shipped):
    """The live record with the claude-code seats and `orchestra-b` appended; every existing row unchanged."""
    if type(live) is not dict or type(shipped) is not dict:
        raise ValueError('Both enrollment files must be JSON objects.')
    authority_ids = [row.get('id') for row in live.get('authorities', [])]
    if 'orchestra' not in authority_ids:
        raise ValueError('The live room has no "orchestra" scope; this fix is not needed. Double-click the launcher.')
    if NEW_SCOPE in authority_ids:
        raise ValueError('The live room already has "' + NEW_SCOPE + '"; nothing to do.')
    source = [row for row in shipped.get('authorities', []) if row.get('id') == 'orchestra']
    if len(source) != 1:
        raise ValueError(str(SHIPPED) + ' has no single "orchestra" scope to copy.')
    result = json.loads(json.dumps(live))
    have = {row.get('id') for row in result['actors']}
    for seat in ('claude-code', 'claude-code-b'):
        if seat not in have:
            rows = [row for row in shipped.get('actors', []) if row.get('id') == seat]
            if len(rows) != 1:
                raise ValueError(str(SHIPPED) + ' has no single "' + seat + '" seat to copy.')
            result['actors'].append(rows[0])
    scope = dict(source[0])
    scope['id'] = NEW_SCOPE
    result['authorities'].append(scope)
    return result


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        sys.stderr.write('usage: python3 make_orchestra_b.py <tree>\n')
        return 2
    tree = Path(args[0])
    out = tree / OUTPUT
    if out.exists():
        sys.stderr.write(str(out) + ' already exists; not overwriting.\n')
        return 2
    try:
        live = json.loads((tree / LIVE).read_text(encoding='utf-8'))
        shipped = json.loads((tree / SHIPPED).read_text(encoding='utf-8'))
        result = build(live, shipped)
    except (OSError, ValueError) as exc:
        sys.stderr.write(str(exc) + '\n')
        return 2
    out.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print('Wrote ' + str(out))
    print('Give Shane this line for a Command Prompt (once; after that he double-clicks as usual):')
    print('    cd /d "' + WINDOWS_TREE + '" && "Start Common Room Crew.cmd" --enrollment .runtime\\handoffs\\'
          + OUTPUT.name)
    print('From then on use scope "' + NEW_SCOPE + '" in plans and --scope ' + NEW_SCOPE + ' on join cards.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
