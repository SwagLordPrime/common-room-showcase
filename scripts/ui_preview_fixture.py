"""A fictional room seeded with every kind of card, for looking at the board (screenshots, design).

Run from the package root:  python3.12 -B scripts/ui_preview_fixture.py <connection.json under .checks>
Writes {origin, key} (the fictional owner's key) to that file, then serves until killed. Every seat
and task is fictional and lives under .checks; nothing touches a real room.
"""
import json, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.helpers import case_dir                    # noqa: E402  (loopback-only guard)
from common_room.activation import start_room          # noqa: E402
from common_room.handoff_api import handoff_operation  # noqa: E402

SEATS = [('claude-room', 'worker', 'Orchestrator chat'), ('ferrule', 'worker', 'Companion chat'),
         ('tinker', 'reviewer', 'Reviewer chat'), ('vesper', 'reviewer', 'Reviewer chat'),
         ('hermes', 'worker', 'Hermes Agent'), ('local-a', 'worker', 'Local model (strong)'),
         ('local-b', 'worker', 'Local model (small)'), ('astra-room', 'worker', 'Astra (Codex)'),
         ('claude-code', 'worker', 'Claude Code on the PC')]
ENROLLMENT = {'version': 1,
              'actors': [{'id': i, 'role': r, 'label': l, 'session': 'preview'} for i, r, l in SEATS],
              'authorities': [{'id': 'orchestra', 'members': ['keeper'] + [i for i, _, _ in SEATS],
                               'scope': 'Fictional preview scope: the orchestra plan for the board design.'}]}


def main(connection):
    base = case_dir('ui-preview')
    plans = base / 'orchestra'
    room = start_room(base / 'handoffs', ENROLLMENT, [i for i, _, _ in SEATS], plans_root=plans)
    s, people = room.store, room.store.people
    owner = people['keeper']
    n = [0]

    def rid(tag):
        n[0] += 1
        return 'preview-%s-%d' % (tag, n[0])

    def intake(title, body, to, acceptance='Checked against the named inputs; every claim labelled.'):
        msg = {'title': title, 'body': body, 'recipient': to, 'authority_id': 'orchestra', 'kind': 'handoff.record',
               'acceptance': acceptance}
        t = s.execute(owner, 'intake', {'source_namespace': 'estate', 'external_reference': rid('ref'), 'message': msg},
                      rid('intake'))['task']['id']
        s.execute(owner, 'deliver', {'task_id': t}, rid('deliver'))
        return t

    def claim(t, who):
        s.execute(people[who], 'ack', {'task_id': t}, rid('ack'))
        return s.execute(people[who], 'claim', {'task_id': t, 'seconds': 1800}, rid('claim'))['task']['lease']['fence']

    def finish(t, who, text):
        fence = claim(t, who)
        ref = handoff_operation(room.server, people[who], 'stage_text', {'task_id': t, 'fence': fence, 'text': text})
        s.execute(people[who], 'finish', {'task_id': t, 'fence': fence, 'body': 'Draft saved; ready for a second pair of eyes.',
                                          'refs': [ref]}, rid('finish'))

    intake('Collect every open gym-app question', 'List the open questions from the gym build notes.\nINPUT: Artifacts/gym/NOTES.md', 'local-b')
    t = intake('Sketch the finance import plan', 'Outline how bank CSVs map into a budget app categories.', 'local-a')
    s.execute(people['local-a'], 'ack', {'task_id': t}, rid('ack'))
    ws = intake('Workstream: redesign the Common Room board', 'Orchestrator workstream. Split into helpers when inputs are ready.', 'claude-room',
                'Every helper accepted; the report links them.')
    fence = claim(ws, 'claude-room')
    s.execute(people['claude-room'], 'request_help', {'task_id': ws, 'fence': fence, 'recipient': 'claude-code',
                                                      'title': 'Build the new board stylesheet', 'body': 'Palette, type and layout as planned.',
                                                      'acceptance': 'Contrast AA in both themes; no horizontal scroll on a phone.'}, rid('help'))
    t = intake('Summarise the Hermes trial notes', 'One page: what worked, what did not, what to try next.', 'hermes')
    claim(t, 'hermes')
    t = intake('Draft the Rift Rumble roster sheet', 'Twelve fighters, one line each.', 'local-a')
    finish(t, 'local-a', '# UNREVIEWED DRAFT\n\nTwelve fighters, one line each (fictional preview).')
    t = intake('Check the DVD rip settings', 'Compare the two HandBrake presets in the notes.', 'ferrule')
    finish(t, 'ferrule', 'Preset B keeps chapter markers; preset A does not (fictional preview).')
    s.execute(people['tinker'], 'review', {'task_id': t, 'accepted': True, 'note': 'Checked both presets against the notes. Accepted.'}, rid('review'))
    t = intake('Pick the Jellyfin library layout', 'Movies and shows: one folder or two?', 'astra-room')
    fence = claim(t, 'astra-room')
    s.execute(people['astra-room'], 'wait_decision', {'task_id': t, 'fence': fence,
                                                      'question': 'One library folder for movies and shows, or two separate folders?'}, rid('ask'))
    for who, body in (('claude-room', 'SHANE UPDATE: finance plan can wait until the gym build lands.'),
                      ('local-b', 'Picked up the gym questions task; drafting now.'),
                      ('tinker', 'QUESTION: should reviews cite line numbers for Markdown tables too?')):
        s.execute(people[who], 'note', {'authority_id': 'orchestra', 'body': body}, rid('note'))

    plan_dir = plans / 'board-revamp'
    plan_dir.mkdir(parents=True)
    plan = {'protocol': 'crew-plan/1', 'plan_id': 'board-revamp', 'title': 'Give the Common Room board its new look', 'scope': 'orchestra',
            'tasks': [{'key': 'ws-style', 'to': 'claude-room', 'title': 'Workstream: styles and layout',
                       'body': 'Orchestrator workstream.\nINPUT: Artifacts/design/PALETTE.md', 'acceptance': 'Both themes pass AA.'},
                      {'key': 'reserve', 'to': 'claude-room', 'title': 'Reserve: new work inside this plan',
                       'body': 'Orchestrator reserve for follow-ups.', 'acceptance': 'Every helper under it accepted or cancelled.', 'reserve': True}]}
    (plan_dir / 'PLAN.json').write_text(json.dumps(plan, indent=2), encoding='utf-8')

    Path(connection).parent.mkdir(parents=True, exist_ok=True)
    Path(connection).write_text(json.dumps({'origin': room.server.origin, 'key': room.credentials['keeper']['token'],
                                            'reviewer_key': room.credentials['tinker']['token']}), encoding='utf-8')
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        room.close()


if __name__ == '__main__':
    main(sys.argv[1])
