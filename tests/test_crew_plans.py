"""Behavioral tests for the orchestrator-plan additions to the crew CLI:
plan-check, plan-approve, decide-file, questions, and cancel (common_room/crew.py).

Written before those subcommands exist. crew.py itself already exists (the
2026-09-23 crew build), so the expected RED here is an assertion failure or a
crew.CrewError-free crash inside main() (an unrecognized subcommand exits 2
from argparse, or an AttributeError from a missing helper), not ImportError.

Exercises the CLI in-process via crew.main(argv) against a real room built by
common_room.activation.start_room, never a real subprocess and never a real
model or network endpoint beyond the room's own loopback HTTP server.
"""
import contextlib, hashlib, io, json, unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.helpers import case_dir
from common_room.store import FIELDS
import common_room.crew as crew

CONFIG = {
    'version': 1,
    'actors': [
        {'id': 'plan-worker-a', 'role': 'worker', 'label': 'Worker A', 'session': 'fixture-a'},
        {'id': 'plan-worker-b', 'role': 'worker', 'label': 'Worker B', 'session': 'fixture-b'},
        {'id': 'plan-reviewer', 'role': 'reviewer', 'label': 'Reviewer', 'session': 'fixture-r'},
    ],
    'authorities': [
        {'id': 'plan-scope-one', 'members': ['keeper', 'plan-worker-a', 'plan-worker-b', 'plan-reviewer'],
         'scope': 'Fixture plan scope one'},
    ],
}
ALL_SEATS = ['plan-worker-a', 'plan-worker-b', 'plan-reviewer']


def make_plan(plan_id='plan-one', scope='plan-scope-one', title='Fixture plan', tasks=None):
    if tasks is None:
        tasks = [
            {'key': 't1', 'to': 'plan-worker-a', 'title': 'Task one', 'body': 'Do thing one.',
             'acceptance': 'Thing one looks right.'},
            {'key': 't2', 'to': 'plan-worker-b', 'title': 'Task two', 'body': 'Do thing two.',
             'acceptance': 'Thing two looks right.'},
        ]
    return {'protocol': 'crew-plan/1', 'plan_id': plan_id, 'title': title, 'scope': scope, 'tasks': tasks}


class PlanCLIBase(unittest.TestCase):
    def start_room(self, root=None, selected=None, enrollment=None):
        from common_room.activation import start_room
        room = start_room(root or case_dir('plan-cli'), enrollment=enrollment or CONFIG,
                           selected=selected or ALL_SEATS)
        self.addCleanup(room.close)
        return room

    def run_cli(self, room, actor, *args, json_mode=True, stdin=None):
        argv = ['--credential', str(room.root / (actor + '-credential.json'))]
        if json_mode:
            argv.append('--json')
        argv += list(args)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            if stdin is not None:
                stack.enter_context(mock.patch('sys.stdin', new=stdin))
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            code = crew.main(argv)
        return code, out.getvalue(), err.getvalue()

    def no_tty(self):
        return SimpleNamespace(isatty=lambda: False)

    def tty_answer(self, answer):
        return SimpleNamespace(isatty=lambda: True, readline=lambda: answer)

    def last_error_code(self, err):
        lines = [l for l in err.strip().splitlines() if l]
        self.assertTrue(lines, 'expected an error line on stderr')
        return lines[-1]

    def write_plan(self, plan, name='plan.json'):
        path = case_dir('plan-files') / name
        path.write_text(json.dumps(plan), encoding='utf-8')
        return path

    def write_decisions(self, decisions, name='decisions.json'):
        path = case_dir('decide-files') / name
        path.write_text(json.dumps(decisions), encoding='utf-8')
        return path

    def intake(self, room, scope='plan-scope-one', to='plan-worker-a', title='Fixture task',
               body='Do the fixture thing.', acceptance='Looks right when reviewed.'):
        code, out, err = self.run_cli(room, 'keeper', 'intake', '--scope', scope, '--to', to,
                                       '--title', title, '--body', body, '--acceptance', acceptance)
        self.assertEqual(code, 0, err)
        return json.loads(out)['task_id']

    def make_waiting_task(self, room, worker='plan-worker-a', question='Which format?'):
        task_id = self.intake(room, to=worker)
        code, out, err = self.run_cli(room, worker, 'ack', task_id)
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli(room, worker, 'claim', task_id, '--seconds', '300')
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli(room, worker, 'ask', task_id, '--question', question)
        self.assertEqual(code, 0, err)
        return task_id

    def make_two_strike_task(self, room, worker='plan-worker-a'):
        """A task with two recorded failures: status waiting_decision, the
        failure budget exhausted, only Shane's `reinstate` can move it again."""
        task_id = self.intake(room, to=worker)
        self.assertEqual(self.run_cli(room, worker, 'ack', task_id)[0], 0)
        self.assertEqual(self.run_cli(room, worker, 'claim', task_id, '--seconds', '300')[0], 0)
        self.assertEqual(self.run_cli(room, worker, 'fail', task_id, '--reason', 'First failure.')[0], 0)
        self.assertEqual(self.run_cli(room, worker, 'claim', task_id, '--seconds', '300')[0], 0)
        self.assertEqual(self.run_cli(room, worker, 'fail', task_id, '--reason', 'Second failure.')[0], 0)
        return task_id


# ---------------------------------------------------------------------------
# plan-check
# ---------------------------------------------------------------------------

class PlanCheckTests(PlanCLIBase):
    def test_valid_plan_is_ok(self):
        room = self.start_room()
        path = self.write_plan(make_plan())
        code, out, err = self.run_cli(room, 'plan-worker-a', 'plan-check', str(path))
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertTrue(data['ok'])
        self.assertEqual(data['plan_id'], 'plan-one')

    def test_any_seat_can_run_plan_check(self):
        room = self.start_room()
        path = self.write_plan(make_plan())
        code, out, err = self.run_cli(room, 'plan-reviewer', 'plan-check', str(path))
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)['ok'])

    def test_unknown_top_level_field_refused(self):
        room = self.start_room()
        plan = make_plan()
        plan['extra'] = 'nope'
        path = self.write_plan(plan)
        code, out, err = self.run_cli(room, 'keeper', 'plan-check', str(path))
        self.assertEqual(code, 1)
        self.assertTrue(self.last_error_code(err).startswith('invalid'), err)

    def test_unknown_task_field_refused(self):
        room = self.start_room()
        plan = make_plan(tasks=[{'key': 't1', 'to': 'plan-worker-a', 'title': 'T', 'body': 'b',
                                  'acceptance': 'a', 'extra': 'nope'}])
        path = self.write_plan(plan)
        code, out, err = self.run_cli(room, 'keeper', 'plan-check', str(path))
        self.assertEqual(code, 1)
        self.assertTrue(self.last_error_code(err).startswith('invalid'), err)

    def test_duplicate_json_key_refused(self):
        room = self.start_room()
        path = case_dir('plan-files') / 'dup.json'
        path.write_text(
            '{"protocol":"crew-plan/1","protocol":"crew-plan/1","plan_id":"p1","title":"T",'
            '"scope":"plan-scope-one","tasks":[]}', encoding='utf-8')
        code, out, err = self.run_cli(room, 'keeper', 'plan-check', str(path))
        self.assertEqual(code, 1)
        self.assertTrue(self.last_error_code(err).startswith('invalid'), err)

    def test_duplicate_task_key_refused(self):
        room = self.start_room()
        plan = make_plan(tasks=[
            {'key': 'dup', 'to': 'plan-worker-a', 'title': 'A', 'body': 'a', 'acceptance': 'a'},
            {'key': 'dup', 'to': 'plan-worker-b', 'title': 'B', 'body': 'b', 'acceptance': 'b'},
        ])
        path = self.write_plan(plan)
        code, out, err = self.run_cli(room, 'keeper', 'plan-check', str(path))
        self.assertEqual(code, 1)
        self.assertTrue(self.last_error_code(err).startswith('invalid'), err)

    def test_oversize_title_refused(self):
        room = self.start_room()
        plan = make_plan(tasks=[{'key': 't1', 'to': 'plan-worker-a', 'title': 'x' * 121,
                                  'body': 'b', 'acceptance': 'a'}])
        path = self.write_plan(plan)
        code, out, err = self.run_cli(room, 'keeper', 'plan-check', str(path))
        self.assertEqual(code, 1)
        self.assertTrue(self.last_error_code(err).startswith('invalid'), err)

    def test_oversize_body_refused(self):
        room = self.start_room()
        plan = make_plan(tasks=[{'key': 't1', 'to': 'plan-worker-a', 'title': 'T',
                                  'body': 'x' * 4001, 'acceptance': 'a'}])
        path = self.write_plan(plan)
        code, out, err = self.run_cli(room, 'keeper', 'plan-check', str(path))
        self.assertEqual(code, 1)
        self.assertTrue(self.last_error_code(err).startswith('invalid'), err)

    def test_oversize_acceptance_refused(self):
        room = self.start_room()
        plan = make_plan(tasks=[{'key': 't1', 'to': 'plan-worker-a', 'title': 'T',
                                  'body': 'b', 'acceptance': 'x' * 2001}])
        path = self.write_plan(plan)
        code, out, err = self.run_cli(room, 'keeper', 'plan-check', str(path))
        self.assertEqual(code, 1)
        self.assertTrue(self.last_error_code(err).startswith('invalid'), err)

    def test_too_many_tasks_refused(self):
        room = self.start_room()
        tasks = [{'key': 't' + str(i), 'to': 'plan-worker-a', 'title': 'T', 'body': 'b', 'acceptance': 'a'}
                 for i in range(61)]
        path = self.write_plan(make_plan(tasks=tasks))
        code, out, err = self.run_cli(room, 'keeper', 'plan-check', str(path))
        self.assertEqual(code, 1)
        self.assertTrue(self.last_error_code(err).startswith('invalid'), err)

    def test_sixty_tasks_is_accepted(self):
        room = self.start_room()
        tasks = [{'key': 't' + str(i), 'to': 'plan-worker-a', 'title': 'T', 'body': 'b', 'acceptance': 'a'}
                 for i in range(60)]
        path = self.write_plan(make_plan(tasks=tasks))
        code, out, err = self.run_cli(room, 'keeper', 'plan-check', str(path))
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)['ok'])

    def test_non_worker_to_reported_as_problem(self):
        room = self.start_room()
        plan = make_plan(tasks=[{'key': 't1', 'to': 'plan-reviewer', 'title': 'T', 'body': 'b', 'acceptance': 'a'}])
        path = self.write_plan(plan)
        code, out, err = self.run_cli(room, 'keeper', 'plan-check', str(path))
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertFalse(data['ok'])
        self.assertTrue(any('plan-reviewer' in p for p in data['problems']), data['problems'])

    def test_to_not_a_member_of_scope_reported_as_problem(self):
        room = self.start_room()
        plan = make_plan(tasks=[{'key': 't1', 'to': 'nonexistent-seat', 'title': 'T', 'body': 'b', 'acceptance': 'a'}])
        path = self.write_plan(plan)
        code, out, err = self.run_cli(room, 'keeper', 'plan-check', str(path))
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertFalse(data['ok'])

    def test_unknown_scope_reported_as_problem(self):
        room = self.start_room()
        plan = make_plan(scope='no-such-scope')
        path = self.write_plan(plan)
        code, out, err = self.run_cli(room, 'keeper', 'plan-check', str(path))
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertFalse(data['ok'])
        self.assertTrue(any('no-such-scope' in p for p in data['problems']), data['problems'])

    def test_bad_plan_id_charset_refused(self):
        room = self.start_room()
        plan = make_plan(plan_id='Not_Valid!')
        path = self.write_plan(plan)
        code, out, err = self.run_cli(room, 'keeper', 'plan-check', str(path))
        self.assertEqual(code, 1)
        self.assertTrue(self.last_error_code(err).startswith('invalid'), err)


# ---------------------------------------------------------------------------
# plan-approve
# ---------------------------------------------------------------------------

class PlanApproveTests(PlanCLIBase):
    def test_refuses_without_tty(self):
        room = self.start_room()
        path = self.write_plan(make_plan())
        code, out, err = self.run_cli(room, 'keeper', 'plan-approve', str(path), stdin=self.no_tty())
        self.assertEqual(code, 1)
        self.assertTrue(self.last_error_code(err).startswith('config'), err)
        events, _, _ = room.store._snapshot(room.store.people['keeper'])
        self.assertEqual([e for e in events if e['kind'] == 'task_created'], [])

    def test_refuses_without_typed_yes(self):
        room = self.start_room()
        path = self.write_plan(make_plan())
        code, out, err = self.run_cli(room, 'keeper', 'plan-approve', str(path), stdin=self.tty_answer('nope\n'))
        self.assertEqual(code, 1)
        self.assertTrue(self.last_error_code(err).startswith('cancelled'), err)
        events, _, _ = room.store._snapshot(room.store.people['keeper'])
        self.assertEqual([e for e in events if e['kind'] == 'task_created'], [])

    def test_non_owner_refused_before_any_write(self):
        room = self.start_room()
        path = self.write_plan(make_plan())
        code, out, err = self.run_cli(room, 'plan-worker-a', 'plan-approve', str(path), stdin=self.tty_answer('yes\n'))
        self.assertEqual(code, 1)
        self.assertTrue(self.last_error_code(err).startswith('forbidden'), err)
        events, _, _ = room.store._snapshot(room.store.people['keeper'])
        self.assertEqual([e for e in events if e['kind'] == 'task_created'], [])

    def test_non_owner_refused_before_the_confirm_gate_is_even_reached(self):  # P5
        # P5: the CLI's own owner-role check must fire BEFORE anything that
        # needs an interactive terminal. With no stdin fixture that is a tty,
        # if the role check were ever removed (the room would still refuse
        # the actual intake, but only after trying to confirm) this would
        # instead surface as a 'config' (no-tty) error, not 'forbidden'.
        room = self.start_room()
        path = self.write_plan(make_plan())
        code, out, err = self.run_cli(room, 'plan-worker-a', 'plan-approve', str(path), stdin=self.no_tty())
        self.assertEqual(code, 1)
        self.assertTrue(self.last_error_code(err).startswith('forbidden'), err)
        events, _, _ = room.store._snapshot(room.store.people['keeper'])
        self.assertEqual([e for e in events if e['kind'] == 'task_created'], [])

    def test_invalid_plan_refused_before_confirmation(self):
        room = self.start_room()
        plan = make_plan(scope='no-such-scope')
        path = self.write_plan(plan)
        # No stdin fixture at all: if this reached the confirm step it would
        # crash on sys.stdin.isatty(); it must be refused before that.
        code, out, err = self.run_cli(room, 'keeper', 'plan-approve', str(path))
        self.assertEqual(code, 1)
        self.assertTrue(self.last_error_code(err).startswith('invalid'), err)

    def test_creates_and_delivers_each_task_once_and_writes_receipt(self):
        room = self.start_room()
        plan = make_plan()
        path = self.write_plan(plan)
        code, out, err = self.run_cli(room, 'keeper', 'plan-approve', str(path), stdin=self.tty_answer('yes\n'))
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data['plan_id'], 'plan-one')
        self.assertEqual(set(data['tasks']), {'t1', 't2'})
        for info in data['tasks'].values():
            self.assertEqual(info['status'], 'delivered')

        receipt_path = path.with_name(path.stem + '.receipt.json')
        self.assertTrue(receipt_path.exists())
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        self.assertEqual(receipt['plan_id'], 'plan-one')
        self.assertEqual(receipt['scope'], 'plan-scope-one')
        self.assertIn('approved_at', receipt)
        self.assertEqual(set(receipt['tasks']), {'t1', 't2'})
        self.assertEqual(receipt['tasks']['t1']['task_id'], data['tasks']['t1']['task_id'])

        events, _, _ = room.store._snapshot(room.store.people['keeper'])
        self.assertEqual(len([e for e in events if e['kind'] == 'task_created']), 2)
        self.assertEqual(len([e for e in events if e['kind'] == 'task_deliver']), 2)

        # Each task actually reached its named worker.
        code, out, err = self.run_cli(room, 'plan-worker-a', 'tasks', '--all')
        self.assertEqual(code, 0, err)
        self.assertIn(data['tasks']['t1']['task_id'], [t['id'] for t in json.loads(out)['tasks']])

    def test_rerun_is_idempotent_and_reports_already_existed(self):
        room = self.start_room()
        plan = make_plan()
        path = self.write_plan(plan)
        code1, out1, err1 = self.run_cli(room, 'keeper', 'plan-approve', str(path), stdin=self.tty_answer('yes\n'))
        self.assertEqual(code1, 0, err1)
        first_ids = {k: v['task_id'] for k, v in json.loads(out1)['tasks'].items()}

        code2, out2, err2 = self.run_cli(room, 'keeper', 'plan-approve', str(path), stdin=self.tty_answer('yes\n'))
        self.assertEqual(code2, 0, err2)
        second_ids = {k: v['task_id'] for k, v in json.loads(out2)['tasks'].items()}
        self.assertEqual(first_ids, second_ids)

        events, _, _ = room.store._snapshot(room.store.people['keeper'])
        self.assertEqual(len([e for e in events if e['kind'] == 'task_created']), 2)
        self.assertEqual(len([e for e in events if e['kind'] == 'task_deliver']), 2)

        code3, out3, err3 = self.run_cli(room, 'keeper', 'plan-approve', str(path), json_mode=False,
                                          stdin=self.tty_answer('yes\n'))
        self.assertEqual(code3, 0, err3)
        self.assertIn('already existed', out3)

    def test_receipt_and_output_never_contain_a_token(self):
        room = self.start_room()
        tokens = [json.loads((room.root / (a + '-credential.json')).read_text(encoding='utf-8'))['token']
                  for a in ['keeper'] + ALL_SEATS]
        path = self.write_plan(make_plan())
        code, out, err = self.run_cli(room, 'keeper', 'plan-approve', str(path), stdin=self.tty_answer('yes\n'))
        self.assertEqual(code, 0, err)
        receipt_path = path.with_name(path.stem + '.receipt.json')
        blob = out + '\n' + err + '\n' + receipt_path.read_text(encoding='utf-8')
        for token in tokens:
            self.assertNotIn(token, blob)

    def test_confirm_shows_file_hash_task_count_every_input_line_and_full_acceptance(self):  # F6
        room = self.start_room()
        body = ('Do the thing.\n'
                'INPUT: Artifacts/foo.md#L1-L10\n'
                'Also check this one.\n'
                'INPUT: Lab Notes/bar.md\n'
                'Not an input line.\n')
        acceptance = 'A' * 150 + ' -- the whole thing, not a preview.'
        plan = make_plan(tasks=[{'key': 't1', 'to': 'plan-worker-a', 'title': 'Task one',
                                  'body': body, 'acceptance': acceptance}])
        path = self.write_plan(plan)
        expected_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        code, out, err = self.run_cli(room, 'keeper', 'plan-approve', str(path), stdin=self.tty_answer('yes\n'))
        self.assertEqual(code, 0, err)
        self.assertIn(expected_sha256, err)
        self.assertIn('1 task', err)
        self.assertIn('[t1]', err)
        self.assertIn('plan-worker-a', err)
        self.assertIn('Task one', err)
        self.assertIn('INPUT: Artifacts/foo.md#L1-L10', err)
        self.assertIn('INPUT: Lab Notes/bar.md', err)
        self.assertIn(acceptance, err)

    def test_editing_an_already_approved_task_is_refused_before_confirm_naming_the_key(self):  # F14
        room = self.start_room()
        plan = make_plan()
        path = self.write_plan(plan)
        code, out, err = self.run_cli(room, 'keeper', 'plan-approve', str(path), stdin=self.tty_answer('yes\n'))
        self.assertEqual(code, 0, err)
        first_ids = {k: v['task_id'] for k, v in json.loads(out)['tasks'].items()}

        edited = make_plan(tasks=[
            {'key': 't1', 'to': 'plan-worker-a', 'title': 'Task one EDITED', 'body': 'Do thing one.',
             'acceptance': 'Thing one looks right.'},
            plan['tasks'][1],
        ])
        path2 = self.write_plan(edited, name='plan-edited.json')
        # No stdin fixture: if the conflict were only caught deep inside the
        # per-task room call (or not at all before the confirm), this would
        # instead surface as a 'config' no-tty error -- catching it here
        # proves it is refused BEFORE the confirm is ever reached.
        code, out, err = self.run_cli(room, 'keeper', 'plan-approve', str(path2))
        self.assertEqual(code, 1)
        self.assertTrue(self.last_error_code(err).startswith('invalid'), err)
        self.assertIn('t1', err)
        self.assertIn('already approved with different content', err)
        self.assertIn('new key', err)
        self.assertIn('new plan_id', err)

        # Nothing new was created; t2 (unchanged) was never touched either.
        events, _, _ = room.store._snapshot(room.store.people['keeper'])
        self.assertEqual(len([e for e in events if e['kind'] == 'task_created']), 2)

        # The ORIGINAL, unedited plan file re-runs fine -- idempotent re-runs
        # of unchanged tasks are never blocked by this check.
        code, out, err = self.run_cli(room, 'keeper', 'plan-approve', str(path), stdin=self.tty_answer('yes\n'))
        self.assertEqual(code, 0, err)
        second_ids = {k: v['task_id'] for k, v in json.loads(out)['tasks'].items()}
        self.assertEqual(first_ids, second_ids)


# ---------------------------------------------------------------------------
# decide-file
# ---------------------------------------------------------------------------

class DecideFileTests(PlanCLIBase):
    def test_refuses_without_tty(self):
        room = self.start_room()
        t1 = self.make_waiting_task(room)
        path = self.write_decisions({'protocol': 'crew-decisions/1',
                                      'decisions': [{'task': t1, 'action': 'resolve', 'text': 'Use markdown.'}]})
        code, out, err = self.run_cli(room, 'keeper', 'decide-file', str(path), stdin=self.no_tty())
        self.assertEqual(code, 1)
        self.assertTrue(self.last_error_code(err).startswith('config'), err)

    def test_refuses_without_typed_yes(self):
        room = self.start_room()
        t1 = self.make_waiting_task(room)
        path = self.write_decisions({'protocol': 'crew-decisions/1',
                                      'decisions': [{'task': t1, 'action': 'resolve', 'text': 'Use markdown.'}]})
        code, out, err = self.run_cli(room, 'keeper', 'decide-file', str(path), stdin=self.tty_answer('n\n'))
        self.assertEqual(code, 1)
        self.assertTrue(self.last_error_code(err).startswith('cancelled'), err)

    def test_applies_confirmed_entry(self):
        room = self.start_room()
        t1 = self.make_waiting_task(room, question='Which format?')
        path = self.write_decisions({'protocol': 'crew-decisions/1',
                                      'decisions': [{'task': t1, 'action': 'resolve', 'text': 'Use markdown.'}]})
        code, out, err = self.run_cli(room, 'keeper', 'decide-file', str(path), stdin=self.tty_answer('yes\n'))
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertTrue(data['ok'])
        self.assertEqual(data['results'][0]['task'], t1)
        self.assertEqual(data['results'][0]['status'], 'delivered')

    def test_continues_past_a_refusal_and_exits_1(self):
        room = self.start_room()
        t1 = self.make_waiting_task(room, 'plan-worker-a', 'Q1')
        t2 = self.make_waiting_task(room, 'plan-worker-b', 'Q2')
        code, out, err = self.run_cli(room, 'keeper', 'decide', t2, '--response', 'Already handled.')
        self.assertEqual(code, 0, err)  # t2 no longer waiting_decision

        path = self.write_decisions({'protocol': 'crew-decisions/1', 'decisions': [
            {'task': t1, 'action': 'resolve', 'text': 'Use markdown.'},
            {'task': t2, 'action': 'resolve', 'text': 'Too late.'},
        ]})
        code, out, err = self.run_cli(room, 'keeper', 'decide-file', str(path), stdin=self.tty_answer('yes\n'))
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertFalse(data['ok'])
        results = {r['task']: r for r in data['results']}
        self.assertTrue(results[t1]['ok'], results)
        self.assertFalse(results[t2]['ok'], results)

        # the confirmed entry that succeeded really did apply
        code, out, err = self.run_cli(room, 'keeper', 'show', t1)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['task']['status'], 'delivered')

    def test_unknown_protocol_refused(self):
        room = self.start_room()
        t1 = self.make_waiting_task(room)
        path = self.write_decisions({'protocol': 'crew-decisions/2',
                                      'decisions': [{'task': t1, 'action': 'resolve', 'text': 'x'}]})
        code, out, err = self.run_cli(room, 'keeper', 'decide-file', str(path), stdin=self.tty_answer('yes\n'))
        self.assertEqual(code, 1)
        self.assertTrue(self.last_error_code(err).startswith('invalid'), err)

    def test_too_many_entries_refused(self):
        room = self.start_room()
        path = self.write_decisions({'protocol': 'crew-decisions/1',
                                      'decisions': [{'task': 'x' * 8, 'action': 'resolve', 'text': 'x'}] * 41})
        code, out, err = self.run_cli(room, 'keeper', 'decide-file', str(path), stdin=self.tty_answer('yes\n'))
        self.assertEqual(code, 1)
        self.assertTrue(self.last_error_code(err).startswith('invalid'), err)

    def test_reinstate_action_clears_the_two_strike_stop(self):  # F11
        room = self.start_room()
        task_id = self.make_two_strike_task(room)
        path = self.write_decisions({'protocol': 'crew-decisions/1',
                                      'decisions': [{'task': task_id, 'action': 'reinstate',
                                                      'text': 'Lease lapsed; redoing it.'}]})
        code, out, err = self.run_cli(room, 'keeper', 'decide-file', str(path), stdin=self.tty_answer('yes\n'))
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertTrue(data['ok'], data)
        self.assertEqual(data['results'][0]['status'], 'delivered')

        code, out, err = self.run_cli(room, 'keeper', 'show', task_id)
        self.assertEqual(code, 0, err)
        task = json.loads(out)['task']
        self.assertEqual(task['failed_attempts'], 0)
        self.assertEqual(task['status'], 'delivered')

    def test_reinstate_by_a_non_owner_is_refused_by_the_room(self):  # F11
        room = self.start_room()
        task_id = self.make_two_strike_task(room)
        # decide-file itself has no --credential-per-entry concept; exercise the
        # room's own owner-only enforcement directly through the plumbed action.
        code, out, err = self.run_cli(room, 'plan-worker-a', 'reinstate', task_id, '--reason', 'Not mine to say.')
        self.assertEqual(code, 1)
        self.assertTrue(self.last_error_code(err).startswith('forbidden'), err)


# ---------------------------------------------------------------------------
# questions
# ---------------------------------------------------------------------------

class QuestionsTests(PlanCLIBase):
    def test_lists_waiting_tasks_and_question_and_shane_update_notes(self):
        room = self.start_room()
        t1 = self.make_waiting_task(room, 'plan-worker-a', 'Which format?')

        code, out, err = self.run_cli(room, 'keeper', 'note', 'plan-scope-one', 'QUESTION: what about X?')
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli(room, 'keeper', 'notes')
        self.assertEqual(code, 0, err)
        question_seq = json.loads(out)['notes'][-1]['seq']
        code, out, err = self.run_cli(room, 'keeper', 'note', 'plan-scope-one', 'Just chatting, not a question.')
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli(room, 'keeper', 'note', 'plan-scope-one', 'SHANE UPDATE: new priority.')
        self.assertEqual(code, 0, err)

        code, out, err = self.run_cli(room, 'keeper', 'questions')
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual([t['id'] for t in data['tasks']], [t1])
        self.assertFalse(data['tasks'][0]['is_helper'])
        self.assertEqual(data['tasks'][0]['answerable_by'], 'owner')
        self.assertEqual(data['tasks'][0]['question'], 'Which format?')

        bodies = [n['body'] for n in data['notes']]
        self.assertIn('QUESTION: what about X?', bodies)
        self.assertIn('SHANE UPDATE: new priority.', bodies)
        self.assertNotIn('Just chatting, not a question.', bodies)

        code, out, err = self.run_cli(room, 'keeper', 'questions', '--since', str(question_seq))
        self.assertEqual(code, 0, err)
        data2 = json.loads(out)
        bodies2 = [n['body'] for n in data2['notes']]
        self.assertNotIn('QUESTION: what about X?', bodies2)
        self.assertIn('SHANE UPDATE: new priority.', bodies2)

    def test_flags_helper_tasks_as_answerable_by_owner_or_sender(self):
        room = self.start_room()
        root_id = self.intake(room, to='plan-worker-a', title='Root', body='Root body.', acceptance='Root ok.')
        self.run_cli(room, 'plan-worker-a', 'ack', root_id)
        self.run_cli(room, 'plan-worker-a', 'claim', root_id, '--seconds', '300')
        code, out, err = self.run_cli(room, 'plan-worker-a', 'delegate', root_id, '--to', 'plan-worker-b',
                                       '--title', 'Helper', '--body', 'Helper body.', '--acceptance', 'Helper ok.')
        self.assertEqual(code, 0, err)
        helper_id = json.loads(out)['child_task_id']
        self.run_cli(room, 'plan-worker-b', 'ack', helper_id)
        self.run_cli(room, 'plan-worker-b', 'claim', helper_id, '--seconds', '300')
        code, out, err = self.run_cli(room, 'plan-worker-b', 'ask', helper_id, '--question', 'Helper question?')
        self.assertEqual(code, 0, err)

        code, out, err = self.run_cli(room, 'keeper', 'questions')
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        row = next(t for t in data['tasks'] if t['id'] == helper_id)
        self.assertTrue(row['is_helper'])
        self.assertEqual(row['answerable_by'], 'owner or plan-worker-a')

    def test_empty_board_returns_empty_lists(self):
        room = self.start_room()
        code, out, err = self.run_cli(room, 'keeper', 'questions')
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data['tasks'], [])
        self.assertEqual(data['notes'], [])

    def test_two_strike_root_task_flagged_stopped_not_plain_answerable(self):  # F11
        room = self.start_room()
        task_id = self.make_two_strike_task(room)
        code, out, err = self.run_cli(room, 'keeper', 'questions')
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        row = next(t for t in data['tasks'] if t['id'] == task_id)
        self.assertEqual(row['answerable_by'], 'stopped: only Shane can reinstate (use action reinstate)')
        self.assertNotEqual(row['answerable_by'], 'owner')

    def test_two_strike_helper_task_also_flagged_stopped_over_sender(self):  # F11
        room = self.start_room()
        root_id = self.intake(room, to='plan-worker-a', title='Root', body='Root body.', acceptance='Root ok.')
        self.run_cli(room, 'plan-worker-a', 'ack', root_id)
        self.run_cli(room, 'plan-worker-a', 'claim', root_id, '--seconds', '300')
        code, out, err = self.run_cli(room, 'plan-worker-a', 'delegate', root_id, '--to', 'plan-worker-b',
                                       '--title', 'Helper', '--body', 'Helper body.', '--acceptance', 'Helper ok.')
        self.assertEqual(code, 0, err)
        helper_id = json.loads(out)['child_task_id']
        self.run_cli(room, 'plan-worker-b', 'ack', helper_id)
        self.run_cli(room, 'plan-worker-b', 'claim', helper_id, '--seconds', '300')
        self.assertEqual(self.run_cli(room, 'plan-worker-b', 'fail', helper_id, '--reason', 'One.')[0], 0)
        self.run_cli(room, 'plan-worker-b', 'claim', helper_id, '--seconds', '300')
        self.assertEqual(self.run_cli(room, 'plan-worker-b', 'fail', helper_id, '--reason', 'Two.')[0], 0)

        code, out, err = self.run_cli(room, 'keeper', 'questions')
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        row = next(t for t in data['tasks'] if t['id'] == helper_id)
        self.assertTrue(row['is_helper'])
        self.assertEqual(row['answerable_by'], 'stopped: only Shane can reinstate (use action reinstate)')

    def test_notes_output_states_it_only_covers_the_last_100(self):  # F11
        room = self.start_room()
        code, out, err = self.run_cli(room, 'keeper', 'questions')
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertIn('last 100', data['notes_scope_note'])

        code, out, err = self.run_cli(room, 'keeper', 'questions', json_mode=False)
        self.assertEqual(code, 0, err)
        self.assertIn('last 100', out)


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------

class CancelWiringTests(PlanCLIBase):
    """Runs regardless of the parallel branch: on this branch the room itself
    does not yet support the 'cancel' action, so the CLI must fail cleanly
    rather than crash."""

    def test_cancel_reason_text_limit_checked_locally(self):
        room = self.start_room()
        task_id = self.intake(room)
        code, out, err = self.run_cli(room, 'keeper', 'cancel', task_id, '--reason', 'x' * 2001)
        self.assertEqual(code, 1)
        self.assertTrue(self.last_error_code(err).startswith('invalid'), err)
        events, _, _ = room.store._snapshot(room.store.people['keeper'])
        self.assertEqual([e for e in events if e['kind'] == 'task_cancel'], [])

    def test_cancel_subcommand_is_wired_even_before_store_support(self):
        # Participant.command's own action allowlist (not in this branch's
        # editable files) also has to learn 'cancel' before this can ever
        # reach the room, so on this branch it is refused cleanly rather than
        # crashing -- the CLI argument wiring, text-limit check and request-id
        # handling all still run correctly.
        room = self.start_room()
        task_id = self.intake(room)
        code, out, err = self.run_cli(room, 'keeper', 'cancel', task_id, '--reason', 'Testing wiring.')
        if 'cancel' in FIELDS:
            self.assertEqual(code, 0, err)
        else:
            self.assertEqual(code, 1)
            self.assertTrue(self.last_error_code(err), err)


@unittest.skipUnless('cancel' in FIELDS, 'needs the store cancel action from the parallel branch')
class CancelBehaviorTests(PlanCLIBase):
    def test_owner_can_cancel_any_task(self):
        room = self.start_room()
        task_id = self.intake(room)
        code, out, err = self.run_cli(room, 'keeper', 'cancel', task_id, '--reason', 'No longer needed.')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['status'], 'cancelled')

    def test_helper_sender_can_cancel_its_own_helper_task(self):
        room = self.start_room()
        root_id = self.intake(room, to='plan-worker-a', title='Root', body='b', acceptance='a')
        self.run_cli(room, 'plan-worker-a', 'ack', root_id)
        self.run_cli(room, 'plan-worker-a', 'claim', root_id, '--seconds', '300')
        code, out, err = self.run_cli(room, 'plan-worker-a', 'delegate', root_id, '--to', 'plan-worker-b',
                                       '--title', 'Helper', '--body', 'b', '--acceptance', 'a')
        self.assertEqual(code, 0, err)
        helper_id = json.loads(out)['child_task_id']
        code, out, err = self.run_cli(room, 'plan-worker-a', 'cancel', helper_id, '--reason', 'Not needed anymore.')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['status'], 'cancelled')

    def test_non_owner_non_sender_cannot_cancel(self):
        room = self.start_room()
        task_id = self.intake(room)
        code, out, err = self.run_cli(room, 'plan-reviewer', 'cancel', task_id, '--reason', 'Not mine.')
        self.assertEqual(code, 1)
        self.assertTrue(self.last_error_code(err).startswith('forbidden'), err)

    def test_cancelled_task_leaves_the_default_task_list(self):              # root, 2026-09-24
        room = self.start_room()
        task_id = self.intake(room, to='plan-worker-a')
        self.assertEqual(self.run_cli(room, 'keeper', 'cancel', task_id, '--reason', 'Withdrawn.')[0], 0)
        code, out, err = self.run_cli(room, 'plan-worker-a', 'tasks')
        self.assertEqual(code, 0, err)
        self.assertNotIn(task_id, out)


# ---------------------------------------------------------------------------
# card
# ---------------------------------------------------------------------------

CARD_CONFIG = {
    'version': 1,
    'actors': [
        {'id': 'hermes', 'role': 'worker', 'label': 'Hermes Agent', 'session': 'fixture-hermes'},
    ],
    'authorities': [
        {'id': 'card-scope', 'members': ['keeper', 'hermes'], 'scope': 'Fixture card scope'},
    ],
}


class CrewCardTests(PlanCLIBase):
    def test_card_hermes_includes_scheduled_job_block_from_the_real_tree_root(self):  # F15
        # This room's root is a child of tests/helpers.case_dir, i.e.
        # <tree root>/.checks/<case>, which is the very same "handoffs folder
        # is two levels below the tree root" layout the real deployment uses
        # (<tree root>/.runtime/handoffs). The real HERMES_SEAT_2026-09-23.md
        # lives at that tree root, so this exercises the real file, not a
        # fixture copy: before the F15 fix, `crew card` passed the handoffs
        # folder itself as `tree`, so the guide was never found there.
        room = self.start_room(enrollment=CARD_CONFIG, selected=['hermes'])
        code, out, err = self.run_cli(room, 'keeper', 'card', 'hermes')
        self.assertEqual(code, 0, err)
        card = json.loads(out)['card']
        self.assertIn('You are the Common Room seat "hermes" on Shane\'s PC.', card)
        self.assertNotIn('could not be read from the tree root', card)


if __name__ == '__main__':
    unittest.main()
