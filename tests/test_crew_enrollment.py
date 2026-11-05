"""The shipped orchestra enrollment: it merges append-only and the orchestrator can delegate."""
import json, unittest
from pathlib import Path
from tests.helpers import case_dir
from common_room.crew_client import CrewClient, CrewError, new_request_id

ROOT = Path(__file__).resolve().parents[1]
ENROLLMENT = json.loads((ROOT / 'crew' / 'enrollment-crew-2026-09-24.json').read_text(encoding='utf-8'))
SEATS = ['claude-room', 'ferrule', 'tinker', 'vesper', 'crew-audit', 'hermes', 'local-a', 'local-b',
         'astra-room', 'astra-audit', 'claude-code', 'claude-code-b']


class OrchestraEnrollmentTests(unittest.TestCase):
    def start(self, root, enrollment):
        return self.start_with(root, enrollment, SEATS)

    def start_with(self, root, enrollment, seats):
        from common_room.activation import start_room
        room = start_room(root, enrollment=enrollment, selected=seats)
        self.addCleanup(room.close)
        return room

    def test_orchestra_is_appended_and_delegation_works(self):
        root = case_dir('crew-enrollment')
        before = dict(ENROLLMENT, authorities=[a for a in ENROLLMENT['authorities'] if a['id'] != 'orchestra'],
                      actors=[a for a in ENROLLMENT['actors'] if a['id'] not in ('claude-code', 'claude-code-b')])
        self.start_with(root, before, [x for x in SEATS if x not in ('claude-code', 'claude-code-b')]).close()   # as it stood before
        room = self.start(root, ENROLLMENT)                    # append-only merge must accept this
        keeper = room.store.people['keeper']
        grants = room.store._snapshot(keeper)[1]
        self.assertEqual(grants['orchestra']['members'],
                         ['keeper', 'claude-room', 'astra-room', 'ferrule', 'claude-code', 'claude-code-b', 'hermes',
                          'local-a', 'local-b', 'tinker', 'vesper', 'crew-audit', 'astra-audit', 'dot', 'notify', 'duet'])   # dot: seat-dot, notify: notify, 2026-10-05; duet: seat duet, 2026-10-30
        self.assertEqual({room.store.people[x].role for x in ('claude-code', 'claude-code-b', 'astra-room')}, {'worker'})
        self.assertEqual(room.store.people['astra-audit'].role, 'reviewer')
        msg = {'title': 'Workstream', 'body': 'Root task.\nINPUT: Artifacts/x.md', 'acceptance': 'Helpers reviewed.',
               'recipient': 'claude-room', 'authority_id': 'orchestra', 'kind': 'handoff.record'}
        task = room.store.execute(keeper, 'intake', {'source_namespace': 'estate', 'external_reference': 'ws-1',
                                                     'message': msg}, 'intake-ws-1')['task']['id']
        room.store.execute(keeper, 'deliver', {'task_id': task}, 'deliver-ws-1')
        orchestrator = CrewClient(room.root / 'claude-room-credential.json')
        orchestrator.command('ack', {'task_id': task}, 'ack-ws-1')
        fence = orchestrator.command('claim', {'task_id': task, 'seconds': 1800}, 'claim-ws-1')['task']['fence']
        helper = orchestrator.command('request_help', {'task_id': task, 'fence': fence, 'recipient': 'local-a',
                                                       'title': 'Piece one', 'body': 'Summarize.\nINPUT: Artifacts/x.md',
                                                       'acceptance': 'Cites x.md lines.'}, 'help-ws-1')['task']
        self.assertEqual((helper['recipient'], helper['status'], helper['parent_id']), ('local-a', 'delivered', task))
        cc = orchestrator.command('request_help', {'task_id': task, 'fence': fence, 'recipient': 'claude-code',
                                                   'title': 'Piece two', 'body': 'Build it.\nINPUT: Artifacts/x.md',
                                                   'acceptance': 'Tests pass.'}, 'help-ws-2')['task']
        self.assertEqual((cc['recipient'], cc['status']), ('claude-code', 'delivered'))
        with self.assertRaises(CrewError):                     # reviewers are never task recipients
            orchestrator.command('request_help', {'task_id': task, 'fence': fence, 'recipient': 'tinker',
                                                  'title': 'x', 'body': 'x', 'acceptance': 'x'}, new_request_id('h'))
        worker = CrewClient(room.root / 'local-a-credential.json')
        with self.assertRaises(CrewError):                     # workers never create root tasks
            worker.command('intake', {'source_namespace': 'estate', 'external_reference': 'ws-2', 'message': msg}, 'intake-ws-2')

    def test_pre_existing_rows_are_exactly_shanes_live_file(self):              # F16, 2026-09-24
        # sha256 of Shane's live runtime enrollment.json as read 2026-09-23 (canonical JSON; the
        # room writes it canonically). Removing the two new actors and the orchestra scope from the
        # shipped file must reproduce it byte for byte. (2026-10-05, seat-dot and notify: dot and notify are
        # removed too (2026-10-30: duet as well); each is a member only of orchestra, which this removes.)
        import hashlib
        from common_room.protocol import canonical
        base = dict(ENROLLMENT, actors=[a for a in ENROLLMENT['actors'] if a['id'] not in ('claude-code', 'claude-code-b', 'dot', 'notify', 'duet')],
                    authorities=[a for a in ENROLLMENT['authorities'] if a['id'] != 'orchestra'])
        self.assertEqual(hashlib.sha256(canonical(base).encode('utf-8')).hexdigest(),
                         'aa97aa3b4c641a3a552147c713b98bc18e6de78662a643eec7cb589a953e2ff7')



class OrchestraBFixTests(unittest.TestCase):
    """N-i: a room that already carries an older `orchestra` scope gets `orchestra-b` appended instead."""
    def test_old_scope_room_merges_orchestra_b_and_keeps_every_existing_row(self):
        import importlib.util, io
        from contextlib import redirect_stdout
        from common_room.activation import start_room, merge_enrollment
        spec = importlib.util.spec_from_file_location('make_orchestra_b', ROOT / 'crew' / 'make_orchestra_b.py')
        tool = importlib.util.module_from_spec(spec); spec.loader.exec_module(tool)
        old_scope = {'id': 'orchestra', 'members': ['keeper', 'claude-room', 'tinker'], 'scope': 'Older 09-23 wording'}
        live = dict(ENROLLMENT, actors=[a for a in ENROLLMENT['actors'] if a['id'] not in ('claude-code', 'claude-code-b')],
                    authorities=[a for a in ENROLLMENT['authorities'] if a['id'] != 'orchestra'] + [old_scope])
        tree = case_dir('crew-orchestra-b')
        seats = [x for x in SEATS if x not in ('claude-code', 'claude-code-b')]
        start_room(tree / '.runtime' / 'handoffs', enrollment=live, selected=seats).close()
        (tree / 'crew').mkdir()
        (tree / 'crew' / 'enrollment-crew-2026-09-24.json').write_text(json.dumps(ENROLLMENT), encoding='utf-8')
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(tool.main([str(tree)]), 0)
        self.assertIn('--enrollment .runtime\\handoffs\\enrollment-crew-orchestra-b.json', out.getvalue())
        made = json.loads((tree / '.runtime' / 'handoffs' / 'enrollment-crew-orchestra-b.json').read_text(encoding='utf-8'))
        stored = json.loads((tree / '.runtime' / 'handoffs' / 'enrollment.json').read_text(encoding='utf-8'))
        merge_enrollment(stored, made)                     # append-only: accepted
        room = start_room(tree / '.runtime' / 'handoffs', enrollment=made, selected=SEATS)
        self.addCleanup(room.close)
        grants = room.store._snapshot(room.store.people['keeper'])[1]
        self.assertEqual(grants['orchestra']['members'], old_scope['members'])
        self.assertIn('claude-code-b', grants['orchestra-b']['members'])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(tool.main([str(tree)]), 2)   # never overwrites

    def test_not_needed_when_the_room_has_no_orchestra_scope(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('make_orchestra_b', ROOT / 'crew' / 'make_orchestra_b.py')
        tool = importlib.util.module_from_spec(spec); spec.loader.exec_module(tool)
        live = dict(ENROLLMENT, authorities=[a for a in ENROLLMENT['authorities'] if a['id'] != 'orchestra'])
        with self.assertRaises(ValueError):
            tool.build(live, ENROLLMENT)


if __name__ == '__main__':
    unittest.main()
