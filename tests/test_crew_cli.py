"""Behavioral tests for the crew CLI (common_room/crew.py).

This file is written before common_room/crew.py exists, per house discipline: the
expected RED here is ImportError on `import common_room.crew as crew`, not a test
failure. Once crew.py exists these exercise it end to end against a real room built
by common_room.activation.start_room, in-process via crew.main(argv), never a real
subprocess and never a real model or network endpoint beyond the room's own loopback
HTTP server.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # lets `python tests/test_crew_cli.py` run from a tree root (evidence.py does)

import contextlib, io, json, os, unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock

from tests.helpers import case_dir
import common_room.crew as crew

CONFIG = {
    'version': 1,
    'actors': [
        {'id': 'crew-worker-a', 'role': 'worker', 'label': 'Worker A', 'session': 'fixture-a'},
        {'id': 'crew-worker-b', 'role': 'worker', 'label': 'Worker B', 'session': 'fixture-b'},
        {'id': 'crew-reviewer', 'role': 'reviewer', 'label': 'Reviewer', 'session': 'fixture-r'},
    ],
    'authorities': [
        {'id': 'crew-scope-one', 'members': ['keeper', 'crew-worker-a', 'crew-worker-b', 'crew-reviewer'],
         'scope': 'First fictional crew scope'},
        {'id': 'crew-scope-two', 'members': ['keeper', 'crew-worker-a', 'crew-worker-b', 'crew-reviewer'],
         'scope': 'Second fictional crew scope'},
    ],
}
ALL_SEATS = ['crew-worker-a', 'crew-worker-b', 'crew-reviewer']


class CrewCLIBase(unittest.TestCase):
    def start_room(self, root=None, selected=None):
        from common_room.activation import start_room
        room = start_room(root or case_dir('crew-cli'), enrollment=CONFIG, selected=selected or ALL_SEATS)
        self.addCleanup(room.close)
        return room

    def run_cli(self, room, actor, *args, json_mode=True, extra=()):
        argv = list(extra) + ['--credential', str(room.root / (actor + '-credential.json'))]
        if json_mode:
            argv.append('--json')
        argv += list(args)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = crew.main(argv)
        return code, out.getvalue(), err.getvalue()

    def as_cli(self, room, actor, *args, json_mode=True):
        """--as is gated by COMMON_ROOM_SEAT (M8): a real seat's launcher sets this
        before invoking crew, so tests that exercise a seat's OWN --as set it too.
        --as keeper additionally needs an interactive terminal; mocked here since
        the test runner's own stdin is never a tty."""
        argv = ['--as', actor, '--room-root', str(room.root)]
        if json_mode:
            argv.append('--json')
        argv += list(args)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {'COMMON_ROOM_SEAT': actor}))
            if actor == 'keeper':
                stack.enter_context(mock.patch('sys.stdin', new=SimpleNamespace(isatty=lambda: True)))
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            code = crew.main(argv)
        return code, out.getvalue(), err.getvalue()

    def intake(self, room, scope='crew-scope-one', to='crew-worker-a', title='Fixture task',
               body='Do the fixture thing.', acceptance='Looks right when reviewed.'):
        code, out, err = self.run_cli(room, 'keeper', 'intake', '--scope', scope, '--to', to,
                                       '--title', title, '--body', body, '--acceptance', acceptance)
        self.assertEqual(code, 0, err)
        return json.loads(out)['task_id']

    def write_report(self, text='Fixture report body.\n'):
        path = case_dir('crew-cli-report') / 'report.md'
        path.write_text(text, encoding='utf-8')
        return path

    def last_error_code(self, err):
        """crew.py prints 'request-id: ...' to stderr before every write, so a
        refusal's 'code: message' line is no longer necessarily the first line."""
        lines = [l for l in err.strip().splitlines() if l]
        self.assertTrue(lines, 'expected an error line on stderr')
        return lines[-1]


class CrewCLITests(CrewCLIBase):
    def test_whoami_and_scoped_tasks(self):
        room = self.start_room()
        code, out, err = self.run_cli(room, 'crew-worker-a', 'whoami')
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data['actor'], 'crew-worker-a')
        self.assertEqual(data['role'], 'worker')
        self.assertEqual({s['id'] for s in data['scopes']}, {'crew-scope-one', 'crew-scope-two'})
        self.assertIn('server_time', data)

        task_id = self.intake(room)
        code, out, err = self.run_cli(room, 'crew-worker-a', 'tasks')
        self.assertEqual(code, 0, err)
        ids = [t['id'] for t in json.loads(out)['tasks']]
        self.assertIn(task_id, ids)

        code, out, err = self.run_cli(room, 'crew-worker-b', 'tasks')
        self.assertEqual(code, 0, err)
        self.assertNotIn(task_id, [t['id'] for t in json.loads(out)['tasks']])

    def test_owner_intake_worker_submit_reviewer_accept_end_to_end(self):
        room = self.start_room()
        task_id = self.intake(room)

        code, out, err = self.run_cli(room, 'crew-worker-a', 'ack', task_id)
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli(room, 'crew-worker-a', 'claim', task_id, '--seconds', '300')
        self.assertEqual(code, 0, err)

        report = self.write_report('Fixture end-to-end report.\n')
        code, out, err = self.run_cli(room, 'crew-worker-a', 'submit', task_id,
                                       '--file', str(report), '--summary', 'Did the fixture thing.')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['status'], 'ready_for_review')

        code, out, err = self.run_cli(room, 'crew-reviewer', 'review-queue')
        self.assertEqual(code, 0, err)
        self.assertIn(task_id, [t['id'] for t in json.loads(out)['tasks']])

        code, out, err = self.run_cli(room, 'crew-reviewer', 'review', task_id, '--accept', '--note', 'Looks correct.')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['status'], 'done')

        code, out, err = self.run_cli(room, 'keeper', 'show', task_id)
        self.assertEqual(code, 0, err)
        shown = json.loads(out)
        self.assertEqual(shown['task']['status'], 'done')
        kinds = {h['kind'] for h in shown['history']}
        self.assertIn('task_finish', kinds)
        self.assertIn('task_review', kinds)

    def test_rerun_after_lost_response_is_idempotent(self):
        room = self.start_room()
        task_id = self.intake(room)
        self.run_cli(room, 'crew-worker-a', 'ack', task_id)

        code1, out1, err1 = self.run_cli(room, 'crew-worker-a', 'claim', task_id, '--seconds', '300')
        code2, out2, err2 = self.run_cli(room, 'crew-worker-a', 'claim', task_id, '--seconds', '300')
        self.assertEqual((code1, code2), (0, 0), (err1, err2))

        report = self.write_report('Idempotent submit body.\n')
        code3, out3, err3 = self.run_cli(room, 'crew-worker-a', 'submit', task_id,
                                          '--file', str(report), '--summary', 'Submitted once.')
        code4, out4, err4 = self.run_cli(room, 'crew-worker-a', 'submit', task_id,
                                          '--file', str(report), '--summary', 'Submitted once.')
        self.assertEqual((code3, code4), (0, 0), (err3, err4))

        events, _, _ = room.store._snapshot(room.store.people['keeper'])
        claims = [e for e in events if e['task_id'] == task_id and e['kind'] == 'task_claim']
        finishes = [e for e in events if e['task_id'] == task_id and e['kind'] == 'task_finish']
        self.assertEqual(len(claims), 1, claims)
        self.assertEqual(len(finishes), 1, finishes)

    def test_delegate_creates_helper_and_parent_waits_for_it(self):
        room = self.start_room()
        task_id = self.intake(room)
        self.run_cli(room, 'crew-worker-a', 'ack', task_id)
        self.run_cli(room, 'crew-worker-a', 'claim', task_id, '--seconds', '300')

        code, out, err = self.run_cli(room, 'crew-worker-a', 'delegate', task_id, '--to', 'crew-worker-b',
                                       '--title', 'Helper piece', '--body', 'Handle the sub-piece.',
                                       '--acceptance', 'Sub-piece looks right.')
        self.assertEqual(code, 0, err)
        child_id = json.loads(out)['child_task_id']
        self.assertNotEqual(child_id, task_id)

        report = self.write_report('Parent report while helper pending.\n')
        code, out, err = self.run_cli(room, 'crew-worker-a', 'submit', task_id,
                                       '--file', str(report), '--summary', 'Parent done early.')
        self.assertEqual(code, 1)
        self.assertTrue(err.strip())

        self.run_cli(room, 'crew-worker-b', 'ack', child_id)
        self.run_cli(room, 'crew-worker-b', 'claim', child_id, '--seconds', '300')
        child_report = self.write_report('Helper report.\n')
        self.run_cli(room, 'crew-worker-b', 'submit', child_id, '--file', str(child_report), '--summary', 'Helper done.')
        code, out, err = self.run_cli(room, 'crew-reviewer', 'review', child_id, '--accept', '--note', 'Helper accepted.')
        self.assertEqual(code, 0, err)

        code, out, err = self.run_cli(room, 'crew-worker-a', 'submit', task_id,
                                       '--file', str(report), '--summary', 'Parent done now.')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['status'], 'ready_for_review')

    def test_ask_then_owner_decides(self):
        room = self.start_room()
        task_id = self.intake(room)
        self.run_cli(room, 'crew-worker-a', 'ack', task_id)
        self.run_cli(room, 'crew-worker-a', 'claim', task_id, '--seconds', '300')

        code, out, err = self.run_cli(room, 'crew-worker-a', 'ask', task_id, '--question', 'Which format do you want?')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['status'], 'waiting_decision')

        code, out, err = self.run_cli(room, 'crew-worker-a', 'tasks', '--all')
        self.assertEqual(code, 0, err)
        row = next(t for t in json.loads(out)['tasks'] if t['id'] == task_id)
        self.assertTrue(row['needs_shane'])

        code, out, err = self.run_cli(room, 'keeper', 'decide', task_id, '--response', 'Use markdown.')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['status'], 'delivered')

    def test_reject_records_strike_and_needs_shane(self):
        room = self.start_room()
        task_id = self.intake(room)
        self.run_cli(room, 'crew-worker-a', 'ack', task_id)
        self.run_cli(room, 'crew-worker-a', 'claim', task_id, '--seconds', '300')
        report = self.write_report('First attempt.\n')
        self.run_cli(room, 'crew-worker-a', 'submit', task_id, '--file', str(report), '--summary', 'First try.')

        code, out, err = self.run_cli(room, 'crew-reviewer', 'review', task_id, '--reject', '--note', 'Not close enough.')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['status'], 'waiting_decision')

        code, out, err = self.run_cli(room, 'keeper', 'show', task_id)
        shown = json.loads(out)
        self.assertEqual(shown['task']['failed_attempts'], 1)
        self.assertTrue(shown['task']['needs_shane'])

    def test_worker_cannot_review_own_work_and_non_owner_cannot_intake(self):
        room = self.start_room()
        task_id = self.intake(room)
        self.run_cli(room, 'crew-worker-a', 'ack', task_id)
        self.run_cli(room, 'crew-worker-a', 'claim', task_id, '--seconds', '300')
        report = self.write_report('Self-review attempt.\n')
        self.run_cli(room, 'crew-worker-a', 'submit', task_id, '--file', str(report), '--summary', 'Done by me.')

        code, out, err = self.run_cli(room, 'crew-worker-a', 'review', task_id, '--accept', '--note', 'I accept my own work.')
        self.assertEqual(code, 1)
        self.assertTrue(self.last_error_code(err).startswith('self_review'), err)

        code, out, err = self.run_cli(room, 'crew-worker-a', 'intake', '--scope', 'crew-scope-one',
                                       '--to', 'crew-worker-b', '--title', 'Not mine', '--body', 'Should refuse.',
                                       '--acceptance', 'N/A')
        self.assertEqual(code, 1)
        self.assertTrue(self.last_error_code(err).startswith('forbidden'), err)

    def test_text_limits_checked_before_sending(self):
        room = self.start_room()

        def unchanged(call):
            before, _, _ = room.store._snapshot(room.store.people['keeper'])
            code, out, err = call()
            after, _, _ = room.store._snapshot(room.store.people['keeper'])
            self.assertEqual(code, 1)
            self.assertTrue(err.startswith('invalid'), err)
            self.assertEqual(len(before), len(after), 'a locally-refused write must never reach the room')

        unchanged(lambda: self.run_cli(room, 'keeper', 'note', 'crew-scope-one', 'x' * 4001))
        unchanged(lambda: self.run_cli(room, 'keeper', 'intake', '--scope', 'crew-scope-one', '--to', 'crew-worker-a',
                                        '--title', 'x' * 121, '--body', 'fine', '--acceptance', 'fine'))
        unchanged(lambda: self.run_cli(room, 'keeper', 'intake', '--scope', 'crew-scope-one', '--to', 'crew-worker-a',
                                        '--title', 'fine', '--body', 'fine', '--acceptance', 'x' * 2001))

        task_id = self.intake(room)
        self.run_cli(room, 'crew-worker-a', 'ack', task_id)
        self.run_cli(room, 'crew-worker-a', 'claim', task_id, '--seconds', '300')
        report = self.write_report('Fine body.\n')
        unchanged(lambda: self.run_cli(room, 'crew-worker-a', 'submit', task_id, '--file', str(report),
                                        '--summary', 'x' * 4001))
        unchanged(lambda: self.run_cli(room, 'crew-worker-a', 'ask', task_id, '--question', 'x' * 2001))
        unchanged(lambda: self.run_cli(room, 'crew-reviewer', 'review', task_id, '--accept', '--note', 'x' * 2001))
        unchanged(lambda: self.run_cli(room, 'crew-worker-a', 'delegate', task_id, '--to', 'crew-worker-b',
                                        '--title', 'x' * 121, '--body', 'fine', '--acceptance', 'fine'))

    def test_token_never_printed(self):
        room = self.start_room()
        tokens = []
        for actor in ['keeper'] + ALL_SEATS:
            config = json.loads((room.root / (actor + '-credential.json')).read_text())
            tokens.append(config['token'])

        transcript = []
        task_id = self.intake(room)
        transcript.append(self._collect(room, 'crew-worker-a', 'whoami'))
        transcript.append(self._collect(room, 'crew-worker-a', 'tasks', '--all'))
        transcript.append(self._collect(room, 'crew-worker-a', 'ack', task_id))
        transcript.append(self._collect(room, 'crew-worker-a', 'claim', task_id, '--seconds', '300'))
        transcript.append(self._collect(room, 'crew-worker-a', 'show', task_id))
        report = self.write_report('Token safety report.\n')
        transcript.append(self._collect(room, 'crew-worker-a', 'submit', task_id, '--file', str(report), '--summary', 'Done.'))
        transcript.append(self._collect(room, 'crew-reviewer', 'review-queue'))
        transcript.append(self._collect(room, 'crew-reviewer', 'read', task_id, '--ref', '1'))
        transcript.append(self._collect(room, 'crew-reviewer', 'review', task_id, '--accept', '--note', 'Fine.'))
        transcript.append(self._collect(room, 'keeper', 'note', 'crew-scope-one', 'A lounge note.'))
        transcript.append(self._collect(room, 'keeper', 'notes'))
        transcript.append(self._collect(room, 'keeper', 'intake', '--scope', 'crew-scope-one', '--to', 'crew-worker-a',
                                         '--title', 'Second task', '--body', 'body', '--acceptance', 'acc'))
        # A refusal path too (worker attempting an owner-only action): its error text must stay clean.
        transcript.append(self._collect(room, 'crew-worker-a', 'intake', '--scope', 'crew-scope-one',
                                         '--to', 'crew-worker-b', '--title', 'Not mine', '--body', 'b',
                                         '--acceptance', 'a'))
        blob = '\n'.join(transcript)
        for token in tokens:
            self.assertNotIn(token, blob)

    def _collect(self, room, actor, *args):
        code, out, err = self.run_cli(room, actor, *args)
        return out + '\n' + err

    def test_json_output_is_parseable(self):
        room = self.start_room()
        task_id = self.intake(room)
        for actor, args in [
            ('crew-worker-a', ('whoami',)),
            ('crew-worker-a', ('tasks', '--all')),
            ('crew-reviewer', ('review-queue',)),
            ('keeper', ('show', task_id)),
            ('keeper', ('notes',)),
        ]:
            code, out, err = self.run_cli(room, actor, *args)
            self.assertEqual(code, 0, err)
            parsed = json.loads(out)
            self.assertIsInstance(parsed, dict)

    def test_prefix_resolution_and_ambiguity(self):
        room = self.start_room()
        task_id = self.intake(room)
        prefix = task_id[:8]
        code, out, err = self.run_cli(room, 'keeper', 'show', prefix)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['task']['id'], task_id)

        state = {'tasks': [{'id': 'tabcdef0111111111111111111111111'},
                            {'id': 'tabcdef0222222222222222222222222'}]}
        with self.assertRaises(crew.CrewError) as ctx:
            crew.resolve_task_id(state, 'tabcdef0')
        self.assertEqual(ctx.exception.code, 'ambiguous')

        with self.assertRaises(crew.CrewError) as ctx:
            crew.resolve_task_id(state, 'tabcd')
        self.assertEqual(ctx.exception.code, 'invalid')

        with self.assertRaises(crew.CrewError) as ctx:
            crew.resolve_task_id(state, 'tzzzzzz')
        self.assertEqual(ctx.exception.code, 'forbidden')

    def test_as_actor_resolves_inside_room_root_only(self):
        room = self.start_room()
        code, out, err = self.as_cli(room, 'crew-worker-a', 'whoami')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['actor'], 'crew-worker-a')

        code, out, err = self.run_cli(room, 'keeper', 'whoami')
        self.assertEqual(code, 0, err)

        code, out, err = self.as_cli(room, 'keeper', 'whoami')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['actor'], 'keeper')

        argv = ['--as', '../keeper', '--room-root', str(room.root), '--json', 'whoami']
        out2, err2 = io.StringIO(), io.StringIO()
        with redirect_stdout(out2), redirect_stderr(err2):
            code2 = crew.main(argv)
        self.assertEqual(code2, 1)
        self.assertTrue(err2.getvalue().strip())

    def test_survives_key_rotation(self):
        room = self.start_room()
        code, out, err = self.run_cli(room, 'crew-worker-a', 'whoami')
        self.assertEqual(code, 0, err)
        room.rotate('crew-worker-a')
        code, out, err = self.run_cli(room, 'crew-worker-a', 'whoami')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['actor'], 'crew-worker-a')


class CrewNotesScopeTests(CrewCLIBase):
    """Added by root 2026-09-23 with the board change that reports each note's scope."""
    def test_notes_scope_filter_applies(self):
        room = self.start_room()
        for scope, body in (('crew-scope-one', 'note for one'), ('crew-scope-two', 'note for two')):
            code, out, err = self.run_cli(room, 'crew-worker-a', 'note', scope, body)
            self.assertEqual(code, 0, err)
        code, out, err = self.run_cli(room, 'crew-worker-b', 'notes', '--scope', 'crew-scope-two')
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertNotIn('warning', result)
        self.assertEqual([n['body'] for n in result['notes']], ['note for two'])
        self.assertEqual({n['authority_id'] for n in result['notes']}, {'crew-scope-two'})


class CrewFixPassTests(CrewCLIBase):
    """2026-09-23 second attempt, findings from INDEPENDENT_VERIFICATION_2026-09-23.md
    section 6 (M3, M4, M7, M8, M9c, M9e) plus the coverage gaps named as untested in
    the first attempt's final report (renew, --body-file, notes --last, the default
    --room-root, non-owner decide/reinstate refusal)."""

    # ---- M3: a receipt replay must never be reported as today's real status ----
    def test_ack_after_reset_is_a_real_second_ack(self):
        room = self.start_room()
        task_id = self.intake(room)
        code, out, err = self.run_cli(room, 'crew-worker-a', 'ack', task_id)
        self.assertEqual(code, 0, err)
        self.run_cli(room, 'crew-worker-a', 'claim', task_id, '--seconds', '300')
        code, out, err = self.run_cli(room, 'crew-worker-a', 'ask', task_id, '--question', 'Which way?')
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli(room, 'keeper', 'decide', task_id, '--response', 'Either way, go ahead.')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['status'], 'delivered')

        # Same plain "ack TASK" invocation as the very first one above. With a
        # fence-less default id this used to be answered from the FIRST ack's
        # stored receipt, printing "acknowledged" while the real status was still
        # "delivered" (PROBE_IDEMPOTENCY.txt: "CLI ack after re-delivery actually
        # acknowledges").
        code, out, err = self.run_cli(room, 'crew-worker-a', 'ack', task_id)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['status'], 'acknowledged')

        events, _, _ = room.store._snapshot(room.store.people['keeper'])
        acks = [e for e in events if e['task_id'] == task_id and e['kind'] == 'task_ack']
        self.assertEqual(len(acks), 2, acks)

    # ---- C-M3b: ack must report a FRESH status, never a replayed receipt's ----
    def test_ack_reused_request_id_after_redelivery_reports_the_real_status(self):
        room = self.start_room()
        task_id = self.intake(room)
        reused_id = 'ack-fixed-redelivery-1'
        code, out, err = self.run_cli(room, 'crew-worker-a', 'ack', task_id, '--request-id', reused_id)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['status'], 'acknowledged')

        self.run_cli(room, 'crew-worker-a', 'claim', task_id, '--seconds', '300')
        code, out, err = self.run_cli(room, 'crew-worker-a', 'ask', task_id, '--question', 'Which way?')
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli(room, 'keeper', 'decide', task_id, '--response', 'Go ahead.')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['status'], 'delivered')

        # Resend the FIRST ack's exact request id. The room answers it from
        # its own stored receipt (status "acknowledged", as of that very
        # first ack) and records no new ack event -- the CLI must still
        # report today's real status (delivered), never that stale receipt.
        code, out, err = self.run_cli(room, 'crew-worker-a', 'ack', task_id, '--request-id', reused_id)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['status'], 'delivered')

        code, out, err = self.run_cli(room, 'crew-worker-a', 'ack', task_id, '--request-id', reused_id, json_mode=False)
        self.assertEqual(code, 0, err)
        self.assertIn('status: delivered', out)
        self.assertNotIn('status: acknowledged', out)

        events, _, _ = room.store._snapshot(room.store.people['keeper'])
        acks = [e for e in events if e['task_id'] == task_id and e['kind'] == 'task_ack']
        self.assertEqual(len(acks), 1, acks)

    # ---- M4: content-derived default ids so a crash-and-retry never duplicates ----
    def test_intake_rerun_with_identical_args_is_idempotent(self):
        room = self.start_room()
        kwargs = dict(scope='crew-scope-one', to='crew-worker-a', title='Repeatable task',
                      body='Same body both times.', acceptance='Same acceptance both times.')
        first = self.intake(room, **kwargs)
        second = self.intake(room, **kwargs)
        self.assertEqual(first, second)
        events, _, _ = room.store._snapshot(room.store.people['keeper'])
        created = [e for e in events if e['kind'] == 'task_created' and e['task_id'] == first]
        delivered = [e for e in events if e['kind'] == 'task_deliver' and e['task_id'] == first]
        self.assertEqual(len(created), 1, created)
        self.assertEqual(len(delivered), 1, delivered)

    # ---- N10: a rerun must say the task already exists, not "created" again ----
    def test_intake_rerun_reports_already_exists_not_created(self):
        room = self.start_room()
        args = ('intake', '--scope', 'crew-scope-one', '--to', 'crew-worker-a',
                '--title', 'Repeatable task N10', '--body', 'Same body both times.',
                '--acceptance', 'Same acceptance both times.')

        code, out, err = self.run_cli(room, 'keeper', *args)
        self.assertEqual(code, 0, err)
        first = json.loads(out)
        self.assertFalse(first['already_existed'])

        code, out, err = self.run_cli(room, 'keeper', *args)
        self.assertEqual(code, 0, err)
        second = json.loads(out)
        self.assertEqual(second['task_id'], first['task_id'])
        self.assertTrue(second['already_existed'])

        code, out, err = self.run_cli(room, 'keeper', *args, json_mode=False)
        self.assertEqual(code, 0, err)
        self.assertIn('already exists', out)
        self.assertNotIn('created and delivered', out)

    def test_delegate_rerun_with_identical_args_is_idempotent(self):
        room = self.start_room()
        task_id = self.intake(room)
        self.run_cli(room, 'crew-worker-a', 'ack', task_id)
        self.run_cli(room, 'crew-worker-a', 'claim', task_id, '--seconds', '300')
        args = ('delegate', task_id, '--to', 'crew-worker-b', '--title', 'Helper piece',
                '--body', 'Handle it.', '--acceptance', 'Looks right.')
        code1, out1, err1 = self.run_cli(room, 'crew-worker-a', *args)
        code2, out2, err2 = self.run_cli(room, 'crew-worker-a', *args)
        self.assertEqual((code1, code2), (0, 0), (err1, err2))
        self.assertEqual(json.loads(out1)['child_task_id'], json.loads(out2)['child_task_id'])
        _, _, tasks = room.store._snapshot(room.store.people['keeper'])
        children = [t for t in tasks.values() if t.get('parent_id') == task_id]
        self.assertEqual(len(children), 1, children)

    def test_request_id_printed_before_sending(self):
        room = self.start_room()
        code, out, err = self.run_cli(room, 'keeper', 'note', 'crew-scope-one', 'A note.')
        self.assertEqual(code, 0, err)
        self.assertTrue(any(l.startswith('request-id: ') for l in err.strip().splitlines()), err)

    # ---- M7: a cp1252 stdout must never crash the CLI ----
    def test_cp1252_stdout_does_not_crash(self):
        room = self.start_room()
        cp1252_out = io.TextIOWrapper(io.BytesIO(), encoding='cp1252', newline='')
        cp1252_err = io.TextIOWrapper(io.BytesIO(), encoding='cp1252', newline='')
        argv = ['--credential', str(room.root / 'keeper-credential.json'),
                'note', 'crew-scope-one', 'Arrow: → done.']
        with mock.patch('sys.stdout', cp1252_out), mock.patch('sys.stderr', cp1252_err):
            code = crew.main(argv)
            cp1252_out.flush(); cp1252_err.flush()
        self.assertEqual(code, 0)

        argv2 = ['--credential', str(room.root / 'keeper-credential.json'), 'notes']
        cp1252_out2 = io.TextIOWrapper(io.BytesIO(), encoding='cp1252', newline='')
        with mock.patch('sys.stdout', cp1252_out2):
            code2 = crew.main(argv2)
            cp1252_out2.flush()
        self.assertEqual(code2, 0)
        printed = cp1252_out2.buffer.getvalue().decode('utf-8', errors='backslashreplace')
        self.assertIn('Arrow:', printed)

    # ---- M8: --as is a speed bump, gated by COMMON_ROOM_SEAT (and a tty for keeper) ----
    def test_as_without_matching_seat_env_var_is_refused(self):
        room = self.start_room()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('COMMON_ROOM_SEAT', None)
            argv = ['--as', 'crew-worker-a', '--room-root', str(room.root), '--json', 'whoami']
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = crew.main(argv)
        self.assertEqual(code, 1)
        self.assertTrue(self.last_error_code(err.getvalue()).startswith('config'), err.getvalue())

    def test_as_with_wrong_seat_env_var_is_refused(self):
        room = self.start_room()
        with mock.patch.dict(os.environ, {'COMMON_ROOM_SEAT': 'crew-worker-b'}):
            argv = ['--as', 'crew-worker-a', '--room-root', str(room.root), '--json', 'whoami']
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = crew.main(argv)
        self.assertEqual(code, 1)
        self.assertTrue(self.last_error_code(err.getvalue()).startswith('config'), err.getvalue())

    def test_as_keeper_requires_interactive_stdin(self):
        room = self.start_room()
        with mock.patch.dict(os.environ, {'COMMON_ROOM_SEAT': 'keeper'}):
            with mock.patch('sys.stdin', new=SimpleNamespace(isatty=lambda: False)):
                argv = ['--as', 'keeper', '--room-root', str(room.root), '--json', 'whoami']
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = crew.main(argv)
            self.assertEqual(code, 1)
            self.assertTrue(self.last_error_code(err.getvalue()).startswith('config'), err.getvalue())

            with mock.patch('sys.stdin', new=SimpleNamespace(isatty=lambda: True)):
                argv = ['--as', 'keeper', '--room-root', str(room.root), '--json', 'whoami']
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = crew.main(argv)
            self.assertEqual(code, 0, err.getvalue())
            self.assertEqual(json.loads(out.getvalue())['actor'], 'keeper')

    def test_as_own_seat_still_works_with_env_var_set(self):
        room = self.start_room()
        with mock.patch.dict(os.environ, {'COMMON_ROOM_SEAT': 'crew-worker-a'}):
            argv = ['--as', 'crew-worker-a', '--room-root', str(room.root), '--json', 'whoami']
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = crew.main(argv)
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())['actor'], 'crew-worker-a')

    # ---- M9(c): text mode must be scanned for tokens too (kills mutant j2) ----
    def test_token_never_printed_text_mode(self):
        room = self.start_room()
        tokens = [json.loads((room.root / (a + '-credential.json')).read_text())['token']
                  for a in ['keeper'] + ALL_SEATS]
        task_id = self.intake(room)
        transcript = []
        for actor, args in [
            ('crew-worker-a', ('whoami',)),
            ('crew-worker-a', ('tasks', '--all')),
            ('crew-worker-a', ('show', task_id)),
            ('keeper', ('notes',)),
        ]:
            code, out, err = self.run_cli(room, actor, *args, json_mode=False)
            transcript.append(out + '\n' + err)
        blob = '\n'.join(transcript)
        for token in tokens:
            self.assertNotIn(token, blob)

    # ---- M9(e): a CLI test that dies if crew_client's re-read-on-rotation is removed ----
    def test_client_session_persists_across_rotation(self):
        room = self.start_room()
        parser = crew.build_parser()
        cred = room.root / 'crew-worker-a-credential.json'
        client = crew.CrewClient(cred)

        args1 = parser.parse_args(['--credential', str(cred), '--json', 'whoami'])
        out1 = io.StringIO()
        with redirect_stdout(out1):
            code1 = crew.run_with_client(client, args1)
        self.assertEqual(code1, 0)
        self.assertEqual(json.loads(out1.getvalue())['actor'], 'crew-worker-a')

        room.rotate('crew-worker-a')

        args2 = parser.parse_args(['--credential', str(cred), '--json', 'whoami'])
        out2 = io.StringIO()
        with redirect_stdout(out2):
            code2 = crew.run_with_client(client, args2)
        self.assertEqual(code2, 0)
        self.assertEqual(json.loads(out2.getvalue())['actor'], 'crew-worker-a')

    # ---- previously-listed untested coverage ----
    def test_renew_extends_lease(self):
        room = self.start_room()
        task_id = self.intake(room)
        self.run_cli(room, 'crew-worker-a', 'ack', task_id)
        code, out, err = self.run_cli(room, 'crew-worker-a', 'claim', task_id, '--seconds', '60')
        self.assertEqual(code, 0, err)
        first_until = json.loads(out)['lease']['until']
        code, out, err = self.run_cli(room, 'crew-worker-a', 'renew', task_id, '--seconds', '1800')
        self.assertEqual(code, 0, err)
        self.assertGreater(json.loads(out)['lease']['until'], first_until)

    def test_body_file_for_intake_and_delegate(self):
        room = self.start_room()
        body_path = case_dir('crew-cli-body') / 'body.txt'
        body_path.write_text('Body from a file.', encoding='utf-8')
        code, out, err = self.run_cli(room, 'keeper', 'intake', '--scope', 'crew-scope-one',
                                       '--to', 'crew-worker-a', '--title', 'From file',
                                       '--body-file', str(body_path), '--acceptance', 'Fine.')
        self.assertEqual(code, 0, err)
        task_id = json.loads(out)['task_id']

        self.run_cli(room, 'crew-worker-a', 'ack', task_id)
        self.run_cli(room, 'crew-worker-a', 'claim', task_id, '--seconds', '300')
        delegate_body = case_dir('crew-cli-body') / 'delegate_body.txt'
        delegate_body.write_text('Helper body from a file.', encoding='utf-8')
        code, out, err = self.run_cli(room, 'crew-worker-a', 'delegate', task_id, '--to', 'crew-worker-b',
                                       '--title', 'Helper via file', '--body-file', str(delegate_body),
                                       '--acceptance', 'Fine.')
        self.assertEqual(code, 0, err)

    def test_notes_last_limits_results(self):
        room = self.start_room()
        for i in range(5):
            code, out, err = self.run_cli(room, 'keeper', 'note', 'crew-scope-one', 'Note number %d.' % i)
            self.assertEqual(code, 0, err)
        code, out, err = self.run_cli(room, 'keeper', 'notes', '--last', '2')
        self.assertEqual(code, 0, err)
        notes = json.loads(out)['notes']
        self.assertEqual(len(notes), 2)
        self.assertEqual(notes[-1]['body'], 'Note number 4.')

    def test_default_room_root_matches_package_convention(self):
        parser = crew.build_parser()
        args = parser.parse_args(['--credential', '/tmp/x', 'whoami'])
        self.assertEqual(Path(args.room_root), crew.ROOT / '.runtime' / 'handoffs')
        self.assertEqual(crew.DEFAULT_ROOM_ROOT, crew.ROOT / '.runtime' / 'handoffs')

    def test_non_owner_cannot_decide_or_reinstate(self):
        room = self.start_room()
        task_id = self.intake(room)
        self.run_cli(room, 'crew-worker-a', 'ack', task_id)
        self.run_cli(room, 'crew-worker-a', 'claim', task_id, '--seconds', '300')
        self.run_cli(room, 'crew-worker-a', 'ask', task_id, '--question', 'Which way?')

        code, out, err = self.run_cli(room, 'crew-worker-a', 'decide', task_id, '--response', 'Not mine to answer.')
        self.assertEqual(code, 1)
        self.assertTrue(self.last_error_code(err).startswith('forbidden'), err)

        code, out, err = self.run_cli(room, 'crew-reviewer', 'reinstate', task_id, '--reason', 'Not mine either.')
        self.assertEqual(code, 1)
        self.assertTrue(self.last_error_code(err).startswith('forbidden'), err)


# ---------------------------------------------------------------------------
# 2026-10-01 (cr-upgrade-2026-10-01, crew-cli): the crew tool learns the new rules. These run against a real room
# started in-process, merged with core-stop (eacc128, which contains core-strikes 6ead0d5) so the store actions exist.
# ---------------------------------------------------------------------------
import datetime, time


class UpgradeCLIBase(CrewCLIBase):
    def ok(self, room, actor, *args):
        code, out, err = self.run_cli(room, actor, *args)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def refused(self, room, actor, *args):
        code, out, err = self.run_cli(room, actor, *args)
        self.assertEqual(code, 1, (out, err))
        self.assertEqual(out.strip(), '')
        return self.last_error_code(err)

    def task(self, room, task_id):
        return self.ok(room, 'keeper', 'show', task_id)['task']

    def worker_submits(self, room, task_id, first):
        if first:
            self.ok(room, 'crew-worker-a', 'ack', task_id)
        else:
            self.ok(room, 'crew-worker-a', 'ack', task_id)
        self.ok(room, 'crew-worker-a', 'claim', task_id, '--seconds', '600')
        self.ok(room, 'crew-worker-a', 'submit', task_id, '--file', str(self.write_report()), '--summary', 'Done.')

    def reject(self, room, task_id, *flags, note='Not right.'):
        return self.run_cli(room, 'crew-reviewer', 'review', task_id, '--reject', *flags, '--note', note)

    def rejected_once(self, room, paperwork=False):
        task_id = self.intake(room)
        self.worker_submits(room, task_id, True)
        code, out, err = self.reject(room, task_id, *(['--paperwork'] if paperwork else []))
        self.assertEqual(code, 0, err)
        return task_id


class ReviewPaperworkTests(UpgradeCLIBase):
    def test_a_paperwork_rejection_sends_the_work_back_without_a_strike(self):
        room = self.start_room()
        task_id = self.intake(room)
        self.worker_submits(room, task_id, True)
        code, out, err = self.reject(room, task_id, '--paperwork')
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual((data['status'], data['review_kind'], data['paperwork_returns'], data['failed_attempts']),
                         ('waiting_decision', 'paperwork', 1, 0))
        shown = self.task(room, task_id)
        self.assertEqual((shown['failed_attempts'], shown['review_kind'], shown['paperwork_returns']), (0, 'paperwork', 1))

    def test_the_default_stays_a_substance_rejection_with_a_strike(self):
        room = self.start_room()
        task_id = self.intake(room)
        self.worker_submits(room, task_id, True)
        code, out, err = self.reject(room, task_id)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual((data['review_kind'], data['failed_attempts']), ('substance', 1))
        shown = self.task(room, task_id)
        self.assertNotIn('review_kind', shown, 'no kind is sent unless --paperwork is given')
        self.assertEqual(shown['failed_attempts'], 1)

    def test_the_third_paperwork_return_on_one_task_costs_a_strike(self):
        room = self.start_room()
        task_id = self.intake(room)
        self.worker_submits(room, task_id, True)
        strikes = []
        for returns in (1, 2, 3):
            code, out, err = self.reject(room, task_id, '--paperwork')
            self.assertEqual(code, 0, err)
            data = json.loads(out)
            strikes.append((data['paperwork_returns'], data['failed_attempts']))
            if returns < 3:
                self.ok(room, 'keeper', 'decide', task_id, '--response', 'Fix the report.')
                self.worker_submits(room, task_id, False)
        self.assertEqual(strikes, [(1, 0), (2, 0), (3, 1)])

    def test_the_text_says_which_kind_and_what_it_cost(self):
        room = self.start_room()
        task_id = self.intake(room)
        self.worker_submits(room, task_id, True)
        code, out, err = self.run_cli(room, 'crew-reviewer', 'review', task_id, '--reject', '--paperwork', '--note', 'Paperwork.',
                                       json_mode=False)
        self.assertEqual(code, 0, err)
        self.assertIn('paperwork', out.lower())
        self.assertIn('strikes: 0', out)

    def test_paperwork_goes_with_a_rejection_only_and_nothing_is_sent_otherwise(self):
        room = self.start_room()
        task_id = self.intake(room)
        self.worker_submits(room, task_id, True)
        code, out, err = self.run_cli(room, 'crew-reviewer', 'review', task_id, '--accept', '--paperwork', '--note', 'No.')
        self.assertEqual(code, 1)
        self.assertIn('--reject', err)
        self.assertEqual(self.task(room, task_id)['status'], 'ready_for_review')

    def test_the_room_still_refuses_what_it_refuses(self):
        room = self.start_room()
        task_id = self.intake(room)
        self.worker_submits(room, task_id, True)
        code, out, err = self.run_cli(room, 'crew-worker-a', 'review', task_id, '--reject', '--paperwork', '--note', 'Mine.')
        self.assertEqual(code, 1)
        self.assertTrue(self.last_error_code(err).startswith(('self_review', 'forbidden')), err)
        self.assertEqual(self.task(room, task_id)['status'], 'ready_for_review')


class DecideRefundTests(UpgradeCLIBase):
    def test_a_refund_by_the_owner_gives_one_strike_back_and_returns_the_task(self):
        room = self.start_room()
        task_id = self.rejected_once(room)
        self.assertEqual(self.task(room, task_id)['failed_attempts'], 1)
        data = self.ok(room, 'keeper', 'decide', task_id, '--refund', '--response', 'The brief was wrong; not the builder.')
        self.assertEqual((data['status'], data['strikes_before'], data['strikes_after'], data['refund']), ('delivered', 1, 0, True))
        shown = self.task(room, task_id)
        self.assertEqual(shown['failed_attempts'], 0)
        self.assertEqual(shown['refunds'][0]['reason'], 'The brief was wrong; not the builder.')

    def test_without_the_flag_a_decision_refunds_nothing(self):
        room = self.start_room()
        task_id = self.rejected_once(room)
        data = self.ok(room, 'keeper', 'decide', task_id, '--response', 'Try again.')
        self.assertEqual((data['refund'], data['strikes_before'], data['strikes_after']), (False, 1, 1))

    def test_the_text_names_the_refund_and_the_strikes(self):
        room = self.start_room()
        task_id = self.rejected_once(room)
        code, out, err = self.run_cli(room, 'keeper', 'decide', task_id, '--refund', '--response', 'Brief error.', json_mode=False)
        self.assertEqual(code, 0, err)
        self.assertIn('refund', out)
        self.assertIn('strikes: 1 -> 0', out)

    def test_a_refund_with_nothing_waiting_is_refused_by_the_room(self):
        room = self.start_room()
        task_id = self.intake(room)
        code = self.refused(room, 'keeper', 'decide', task_id, '--refund', '--response', 'Nothing to refund.')
        self.assertTrue(code.startswith('state'), code)

    def helper_rejected(self, room, rejects):
        parent = self.intake(room)
        self.ok(room, 'crew-worker-a', 'ack', parent)
        self.ok(room, 'crew-worker-a', 'claim', parent, '--seconds', '900')
        helper = self.ok(room, 'crew-worker-a', 'delegate', parent, '--to', 'crew-worker-b', '--title', 'Helper',
                         '--body', 'Help.', '--acceptance', 'Helps.')['child_task_id']
        for n in range(rejects):
            if n:
                self.ok(room, 'crew-worker-a', 'decide', helper, '--response', 'Again.')
            self.ok(room, 'crew-worker-b', 'ack', helper)
            self.ok(room, 'crew-worker-b', 'claim', helper, '--seconds', '900')
            self.ok(room, 'crew-worker-b', 'submit', helper, '--file', str(self.write_report()), '--summary', 'Done.')
            code, out, err = self.reject(room, helper)
            self.assertEqual(code, 0, err)
        return helper

    def test_the_sender_of_a_helper_may_refund_it_below_two_strikes(self):
        room = self.start_room()
        helper = self.helper_rejected(room, 1)
        data = self.ok(room, 'crew-worker-a', 'decide', helper, '--refund', '--response', 'My brief was unclear.')
        self.assertEqual((data['strikes_before'], data['strikes_after']), (1, 0))

    def test_at_two_strikes_only_the_owner_may_refund_and_the_room_says_so(self):
        room = self.start_room()
        helper = self.helper_rejected(room, 2)
        self.assertEqual(self.task(room, helper)['failed_attempts'], 2)
        code, out, err = self.run_cli(room, 'crew-worker-a', 'decide', helper, '--refund', '--response', 'Please.')
        self.assertEqual(code, 1)
        self.assertIn('Only the owner may refund', err)
        self.assertEqual(self.task(room, helper)['failed_attempts'], 2)
        data = self.ok(room, 'keeper', 'decide', helper, '--refund', '--response', 'Owner refunds one strike.')
        self.assertEqual((data['strikes_before'], data['strikes_after']), (2, 1))

    def test_a_stranger_is_refused(self):
        room = self.start_room()
        task_id = self.rejected_once(room)
        code, out, err = self.run_cli(room, 'crew-worker-b', 'decide', task_id, '--refund', '--response', 'Not mine.')
        self.assertEqual(code, 1)
        self.assertEqual(self.task(room, task_id)['failed_attempts'], 1)


# every authority the keeper owns: the two fixture scopes and the room's own `handoffs` authority
SCOPES = ['crew-scope-one', 'crew-scope-two', 'handoffs']


class StopAllTests(UpgradeCLIBase):
    def paused(self, room, actor='keeper'):
        code, out, err = self.run_cli(room, actor, 'whoami')       # any call; the state comes from the room itself
        self.assertEqual(code, 0, err)
        from common_room.crew_client import CrewClient
        client = CrewClient(room.root / (actor + '-credential.json'))
        return {a['id']: a['paused'] for a in client.state()['authorities']}

    def test_the_keeper_stops_everything_and_the_text_says_what_was_paused(self):
        room = self.start_room()
        data = self.ok(room, 'keeper', 'stop-all', '--reason', 'Testing the red button.')
        self.assertEqual(sorted(data['paused']), SCOPES)
        self.assertEqual(data['already_paused'], [])
        self.assertTrue(data['stopped'])
        self.assertEqual(self.paused(room), {k: True for k in SCOPES})
        code, out, err = self.run_cli(room, 'keeper', 'resume-all', json_mode=False)
        self.assertEqual(code, 0, err)
        self.assertIn('resumed', out)
        self.assertIn('crew-scope-one', out)

    def test_a_stopped_room_refuses_work_and_resume_all_lets_it_through(self):
        room = self.start_room()
        task_id = self.intake(room)
        self.ok(room, 'keeper', 'stop-all', '--reason', 'Hold everything.')
        code = self.refused(room, 'crew-worker-a', 'ack', task_id)
        self.assertTrue(code.startswith(('paused', 'state', 'forbidden')) or code, code)
        data = self.ok(room, 'keeper', 'resume-all')
        self.assertEqual(sorted(data['resumed']), SCOPES)
        self.assertFalse(data['stopped'])
        self.ok(room, 'crew-worker-a', 'ack', task_id)

    def test_stopping_twice_or_resuming_a_running_room_is_refused_by_the_room(self):
        room = self.start_room()
        self.assertTrue(self.refused(room, 'keeper', 'resume-all').startswith('state'))
        self.ok(room, 'keeper', 'stop-all', '--reason', 'First.')
        code, out, err = self.run_cli(room, 'keeper', 'stop-all', '--reason', 'Second.')
        self.assertEqual(code, 1)
        self.assertIn('already stopped', err)

    def test_only_the_keeper_may_stop_or_resume(self):
        room = self.start_room()
        for actor in ('crew-worker-a', 'crew-reviewer'):
            self.assertTrue(self.refused(room, actor, 'stop-all', '--reason', 'Not allowed.').startswith('forbidden'))
        self.assertEqual(self.paused(room), {k: False for k in SCOPES})
        self.ok(room, 'keeper', 'stop-all', '--reason', 'Real stop.')
        self.assertTrue(self.refused(room, 'crew-worker-a', 'resume-all').startswith('forbidden'))
        self.assertEqual(self.paused(room), {k: True for k in SCOPES})

    def test_a_reason_is_required_to_stop_and_optional_to_resume(self):
        room = self.start_room()
        code, out, err = self.run_cli(room, 'keeper', 'stop-all')
        self.assertEqual(code, 2)
        self.ok(room, 'keeper', 'stop-all', '--reason', 'Because.')
        data = self.ok(room, 'keeper', 'resume-all', '--reason', 'All clear.')
        self.assertFalse(data['stopped'])

    def test_an_authority_paused_by_hand_is_reported_as_already_paused(self):
        room = self.start_room()
        self.ok(room, 'keeper', 'stop-all', '--reason', 'One.')
        self.ok(room, 'keeper', 'resume-all')
        data = self.ok(room, 'keeper', 'stop-all', '--reason', 'Two.')
        self.assertEqual(data['already_paused'], [])

    def test_the_stop_note_stops_a_waiting_crew_watch(self):
        from tests.test_crew_watch import FakeClock
        room = self.start_room()
        path = str(room.root / 'crew-worker-a-credential.json')
        args = crew.build_parser().parse_args(['--credential', path, 'watch'])
        def hook(n):
            if n == 1:
                self.ok(room, 'keeper', 'stop-all', '--reason', 'Everyone stop.')
        clock = FakeClock(hook)
        out = io.StringIO()
        with redirect_stdout(out):
            code = crew.run_watch(crew.CrewClient(path), args, sleep=clock.sleep, monotonic=clock.monotonic)
        self.assertEqual(code, 4)
        line = out.getvalue()                       # the STOP file the room wrote is seen first, with its reason inside
        self.assertTrue(line.startswith('STOP '), line)
        self.assertIn('Everyone stop.', line)
        self.assertLessEqual(len(line.strip()), 300)

    def test_crew_shane_help_text_mentions_the_new_commands(self):
        text = (Path(__file__).resolve().parents[1] / 'crew-shane.cmd').read_text(encoding='ascii')
        for word in ('stop-all', 'resume-all', 'usage'):
            self.assertIn(word, text)


class UsageTests(UpgradeCLIBase):
    NOW = datetime.datetime.now(datetime.timezone.utc)

    def ts(self, days_ago=0):
        return (self.NOW - datetime.timedelta(days=days_ago)).strftime('%Y-%m-%dT%H:%M:%SZ')

    def local(self, seat='local-a', outcome='finished', seconds=10.0, pt=100, ot=50, days_ago=0, **more):
        row = {'ts': self.ts(days_ago), 'seat': seat, 'kind': 'local', 'task_id': 't0123abcd', 'model': 'gemma4:31b-it-qat',
               'seconds': seconds, 'prompt_tokens': pt, 'output_tokens': ot, 'outcome': outcome}
        row.update(more)
        return row

    def headless(self, seat='claude-code', outcome='finished', seconds=200.0, exit_code=0, days_ago=0):
        return {'ts': self.ts(days_ago), 'seat': seat, 'kind': 'headless', 'task_id': 't0123abcd', 'model': 'claude',
                'seconds': seconds, 'exit': exit_code, 'budget_cap_usd': 5.0, 'outcome': outcome}

    def put(self, room, seat, rows, raw=()):
        folder = room.root / 'usage'
        folder.mkdir(exist_ok=True)
        lines = [json.dumps(r, separators=(',', ':')) for r in rows] + list(raw)
        (folder / (seat + '.jsonl')).write_text('\n'.join(lines) + '\n', encoding='utf-8')

    def fixture(self, room):
        self.put(room, 'local-a', [self.local(), self.local(outcome='question', seconds=5.0, pt=40, ot=0),
                                   self.local(outcome='failed', seconds=7.5, pt=None, ot=None),
                                   self.local(outcome='skipped_quiet', seconds=0, pt=None, ot=None)])
        self.put(room, 'local-b', [self.local(seat='local-b', seconds=3.0)])
        self.put(room, 'claude-code', [self.headless(), self.headless(outcome='failed', seconds=30.0, exit_code=1)])

    def test_it_adds_up_each_seat(self):
        room = self.start_room()
        self.fixture(room)
        data = self.ok(room, 'crew-worker-a', 'usage')
        seats = data['seats']
        self.assertEqual(sorted(seats), ['claude-code', 'local-a', 'local-b'])
        a = seats['local-a']
        self.assertEqual((a['jobs'], a['seconds'], a['prompt_tokens'], a['output_tokens']), (4, 22.5, 140, 50))
        self.assertEqual(a['outcomes'], {'finished': 1, 'question': 1, 'failed': 1, 'skipped_quiet': 1})
        h = seats['claude-code']
        self.assertEqual((h['jobs'], h['seconds'], h['nonzero_exits']), (2, 230.0, 1))
        self.assertEqual(data['jobs'], 7)
        self.assertEqual(data['bad_lines'], 0)

    def test_the_text_has_one_line_per_seat_and_no_task_text(self):
        room = self.start_room()
        self.fixture(room)
        code, out, err = self.run_cli(room, 'crew-worker-a', 'usage', json_mode=False)
        self.assertEqual(code, 0, err)
        for seat in ('local-a', 'local-b', 'claude-code'):
            self.assertEqual(len([l for l in out.splitlines() if l.strip().startswith(seat)]), 1, out)
        self.assertNotIn('t0123abcd', out)
        self.assertIn('7 jobs', out)

    def test_seat_and_days_filters(self):
        room = self.start_room()
        self.fixture(room)
        self.put(room, 'local-b', [self.local(seat='local-b', days_ago=10), self.local(seat='local-b', days_ago=0, seconds=2.0)])
        only = self.ok(room, 'crew-worker-a', 'usage', '--seat', 'local-b')
        self.assertEqual(sorted(only['seats']), ['local-b'])
        recent = self.ok(room, 'crew-worker-a', 'usage', '--days', '1')
        self.assertEqual(recent['seats']['local-b']['jobs'], 1)
        self.assertEqual(recent['window_days'], 1)
        everything = self.ok(room, 'crew-worker-a', 'usage')
        self.assertEqual(everything['seats']['local-b']['jobs'], 2)

    def test_bad_lines_are_counted_and_listed_never_a_crash(self):
        room = self.start_room()
        good = self.local()
        bad = [
            'not json at all',
            '[1, 2, 3]',
            json.dumps(dict(good, seconds='ten')),
            json.dumps(dict(good, outcome='exploded')),
            json.dumps(dict(good, prompt='write me a poem about the secret plan')),     # a text field is never accepted
            json.dumps({k: v for k, v in good.items() if k != 'model'}),
            json.dumps(dict(good, seat='local-b')),                                      # wrong seat for this file
            json.dumps(dict(good, ts='yesterday')),
            '',
            '{"unterminated": ',
        ]
        self.put(room, 'local-a', [good], raw=bad)
        data = self.ok(room, 'crew-worker-a', 'usage')
        self.assertEqual(data['seats']['local-a']['jobs'], 1)
        self.assertEqual(data['bad_lines'], 9)                       # the blank line is neither a job nor bad
        self.assertTrue(all(p.startswith('local-a.jsonl line ') for p in data['problems']))
        self.assertNotIn('secret plan', json.dumps(data))
        code, out, err = self.run_cli(room, 'crew-worker-a', 'usage', json_mode=False)
        self.assertEqual(code, 0)
        self.assertIn('9 bad line', out)

    def test_a_missing_usage_folder_is_not_an_error(self):
        room = self.start_room()
        code, out, err = self.run_cli(room, 'crew-worker-a', 'usage', json_mode=False)
        self.assertEqual(code, 0, err)
        self.assertIn('no usage recorded yet', out)

    def test_a_file_over_16_mb_is_skipped_and_said_so(self):
        room = self.start_room()
        self.fixture(room)
        (room.root / 'usage' / 'local-b.jsonl').write_bytes(b'x' * (16 * 1024 * 1024 + 1))
        data = self.ok(room, 'crew-worker-a', 'usage')
        self.assertNotIn('local-b', data['seats'])
        self.assertTrue(any('over 16 MB' in p for p in data['problems']))
        self.assertEqual(data['seats']['local-a']['jobs'], 4)

    def test_it_only_reads(self):
        room = self.start_room()
        self.fixture(room)
        folder = room.root / 'usage'
        before = {p.name: (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes()) for p in folder.iterdir()}
        self.ok(room, 'crew-worker-a', 'usage')
        self.ok(room, 'crew-worker-a', 'usage', '--seat', 'local-a', '--days', '3')
        after = {p.name: (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes()) for p in folder.iterdir()}
        self.assertEqual(after, before)
        self.assertEqual(sorted(p.name for p in room.root.glob('usage*')), ['usage'])

    def test_it_needs_no_room(self):
        folder = case_dir('crew-usage-noroom')
        (folder / 'usage').mkdir()
        (folder / 'usage' / 'local-a.jsonl').write_text(json.dumps(self.local()) + '\n', encoding='utf-8')
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = crew.main(['--credential', str(folder / 'nobody-credential.json'), '--json', 'usage'])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())['jobs'], 1)

    def test_bad_options_are_refused(self):
        room = self.start_room()
        for args in (['--days', '0'], ['--days', '-3'], ['--days', 'soon'], ['--seat', '../x'], ['--seat', 'UPPER']):
            with self.subTest(args=args):
                code, out, err = self.run_cli(room, 'crew-worker-a', 'usage', *args)
                self.assertNotEqual(code, 0)


class QuestionsStoppedTextTests(UpgradeCLIBase):
    OLD = 'stopped: only Shane can reinstate (use action reinstate)'

    def stopped_task(self, room):
        task_id = self.intake(room)
        self.worker_submits(room, task_id, True)
        self.reject(room, task_id)
        self.ok(room, 'keeper', 'decide', task_id, '--response', 'Again.')
        self.worker_submits(room, task_id, False)
        code, out, err = self.reject(room, task_id)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.task(room, task_id)['failed_attempts'], 2)
        return task_id

    def test_a_stopped_task_says_what_is_true_now(self):
        room = self.start_room()
        task_id = self.stopped_task(room)
        data = self.ok(room, 'keeper', 'questions')
        row = next(r for r in data['tasks'] if r['id'] == task_id)
        self.assertEqual(row['answerable_by'], self.OLD)          # the field other tools and tests key on is unchanged
        note = row['stopped_note']
        for phrase in ('owner', 'reinstate', 'refund', 'sender cannot refund'):
            self.assertIn(phrase, note)

    def test_the_printed_line_carries_the_note(self):
        room = self.start_room()
        self.stopped_task(room)
        code, out, err = self.run_cli(room, 'keeper', 'questions', json_mode=False)
        self.assertEqual(code, 0, err)
        line = next(l for l in out.splitlines() if 'answerable by:' in l)
        self.assertIn(self.OLD, line)
        self.assertIn('refund', line)
        self.assertIn('sender cannot refund', line)

    def test_a_task_that_is_not_stopped_has_no_such_note(self):
        room = self.start_room()
        task_id = self.rejected_once(room)
        data = self.ok(room, 'keeper', 'questions')
        row = next(r for r in data['tasks'] if r['id'] == task_id)
        self.assertEqual(row['answerable_by'], 'owner')
        self.assertIsNone(row.get('stopped_note'))


if __name__ == '__main__':
    unittest.main(argv=sys.argv[:1])    # evidence.py passes the tree root as an argument; it names no test
