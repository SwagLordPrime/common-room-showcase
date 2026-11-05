"""crew/tick.py: the wake-up check against a real room and a running relay.

Written before crew/tick.py and crew/schedules/*.json were finished (house discipline);
the expected RED is these tests failing against a missing or incomplete tool, never a
crash while importing the test module itself.
"""
import ast, hashlib, importlib.util, io, json, os, re, shutil, subprocess, sys, threading, time, unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

from tests.helpers import case_dir
from tests.test_relay import RelayTestCase as RelayTests
from common_room.relay import Relay

ROOT = Path(__file__).resolve().parents[1]
TICK_PATH = ROOT / 'crew' / 'tick.py'
SCHEDULES_DIR = ROOT / 'crew' / 'schedules'

SPEC = importlib.util.spec_from_file_location('tick', TICK_PATH)
tick = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tick)


CONFIG = {
    'version': 1,
    'actors': [
        {'id': 'orch', 'role': 'worker', 'label': 'Fictional orchestrator', 'session': 'fixture-orch'},
        {'id': 'helper', 'role': 'worker', 'label': 'Fictional helper', 'session': 'fixture-helper'},
        {'id': 'other', 'role': 'worker', 'label': 'Fictional bystander', 'session': 'fixture-other'},
        {'id': 'judge', 'role': 'reviewer', 'label': 'Fictional reviewer', 'session': 'fixture-judge'},
    ],
    'authorities': [
        {'id': 'band', 'members': ['keeper', 'orch', 'helper', 'other', 'judge'], 'scope': 'Fictional orchestra'},
    ],
}


class TickTestCase(unittest.TestCase):
    intake_task = RelayTests.intake_task

    def start_band(self, root=None):
        from common_room.activation import start_room
        room = start_room(root or case_dir('tick'), enrollment=CONFIG,
                           selected=('orch', 'helper', 'other', 'judge'))
        self.addCleanup(room.close)
        return room

    def running_relay(self, room, actors):
        relay = Relay(room.root, actors)
        relay._log = lambda *a, **k: None
        stop = threading.Event()

        def loop():
            while not stop.is_set():
                relay.process_once()
                stop.wait(0.15)
        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        self.addCleanup(lambda: (stop.set(), thread.join(3)))
        return relay

    def folder(self, room, actor):
        return str(room.root / 'relay' / actor)

    def run_tick(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = tick.main(list(argv))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 2
        return code, out.getvalue(), err.getvalue()

    def check(self, folder, role, holder=None, timeout=5, max_age=0):
        argv = [folder, 'check', '--role', role, '--timeout', str(timeout), '--max-age', str(max_age)]
        if holder is not None:
            argv += ['--holder', holder]
        return self.run_tick(*argv)

    # -- room fixtures --------------------------------------------------

    def deliver_root_to_orch(self, room, ref='root-1', **over):
        owner = room.store.people['keeper']
        msg = dict(title='Workstream', body='Root task body', acceptance='Done',
                   recipient='orch', authority_id='band', kind='handoff.record')
        msg.update(over)
        task = room.store.execute(owner, 'intake', {'source_namespace': 'estate', 'external_reference': ref,
                                   'message': msg}, 'intake-' + ref)['task']['id']
        room.store.execute(owner, 'deliver', {'task_id': task}, 'deliver-' + ref)
        return task

    def make_helper_waiting(self, room, ref='ws1'):
        """orch claims a root task and delegates a helper to `helper`, who then asks a
        question (wait_decision). Returns (root_task_id, child_task_id)."""
        root = self.deliver_root_to_orch(room, ref=ref)
        orch = room.store.people['orch']
        room.store.execute(orch, 'ack', {'task_id': root}, 'ack-' + ref)
        fence = room.store.execute(orch, 'claim', {'task_id': root, 'seconds': 1800}, 'claim-' + ref)['task']['fence']
        child = room.store.execute(orch, 'request_help', {'task_id': root, 'fence': fence, 'recipient': 'helper',
                                    'title': 'Piece', 'body': 'Piece body', 'acceptance': 'Piece done'},
                                    'help-' + ref)['task']['id']
        helper = room.store.people['helper']
        room.store.execute(helper, 'ack', {'task_id': child}, 'hack-' + ref)
        hf = room.store.execute(helper, 'claim', {'task_id': child, 'seconds': 1800}, 'hclaim-' + ref)['task']['fence']
        room.store.execute(helper, 'wait_decision', {'task_id': child, 'fence': hf, 'question': 'Which file?'},
                            'hq-' + ref)
        return root, child

    def fail_twice(self, room, child, ref='ws1'):
        helper = room.store.people['helper']
        room.store.execute(helper, 'ack', {'task_id': child}, 'reack1-' + ref)
        hf = room.store.execute(helper, 'claim', {'task_id': child, 'seconds': 1800}, 'reclaim1-' + ref)['task']['fence']
        room.store.execute(helper, 'fail', {'task_id': child, 'fence': hf, 'body': 'first try failed'}, 'fail1-' + ref)
        hf2 = room.store.execute(helper, 'claim', {'task_id': child, 'seconds': 1800}, 'reclaim2-' + ref)['task']['fence']
        room.store.execute(helper, 'fail', {'task_id': child, 'fence': hf2, 'body': 'second try failed'}, 'fail2-' + ref)

    def post_note(self, room, actor, body, ref='n1'):
        person = room.store.people[actor]
        room.store.execute(person, 'note', {'authority_id': 'band', 'body': body}, 'note-' + ref)


class IdleAndFreshnessTests(TickTestCase):
    def test_idle_with_fresh_state_sends_no_request_and_exits_3(self):
        room = self.start_band()
        self.running_relay(room, ['orch'])
        folder = self.folder(room, 'orch')
        code, out, err = self.check(folder, 'orchestrator', max_age=120)
        self.assertEqual(code, 3, err)
        self.assertEqual(list((Path(folder) / 'requests').iterdir()), [])
        archived_before = set((Path(folder) / 'archive').iterdir())
        code2, out2, err2 = self.check(folder, 'orchestrator', max_age=120)
        self.assertEqual(code2, 3, err2)
        self.assertEqual(list((Path(folder) / 'requests').iterdir()), [])
        self.assertEqual(set((Path(folder) / 'archive').iterdir()), archived_before,
                          'a fresh relay-state.json must not trigger a second relay round trip')

    def test_stale_state_sends_exactly_one_request(self):
        room = self.start_band()
        self.running_relay(room, ['orch'])
        folder = self.folder(room, 'orch')
        self.check(folder, 'orchestrator', max_age=120)
        archived_first = list((Path(folder) / 'archive').iterdir())
        self.assertEqual(len(archived_first), 1)
        state_path = Path(folder) / 'relay-state.json'
        state = json.loads(state_path.read_text(encoding='utf-8'))
        state['relay_written_at'] = time.time() - 10000
        state_path.write_text(json.dumps(state), encoding='utf-8')
        code, out, err = self.check(folder, 'orchestrator', max_age=1)
        self.assertIn(code, (0, 3), err)
        archived_second = list((Path(folder) / 'archive').iterdir())
        self.assertEqual(len(archived_second), 2, 'exactly one new request must have been sent and archived')

    def test_relay_down_exits_5_within_a_short_timeout(self):
        room = self.start_band()
        folder = self.folder(room, 'orch')
        os.makedirs(os.path.join(folder, 'requests'), exist_ok=True)
        os.makedirs(os.path.join(folder, 'responses'), exist_ok=True)
        os.makedirs(os.path.join(folder, 'archive'), exist_ok=True)
        started = time.time()
        code, out, err = self.check(folder, 'orchestrator', timeout=1, max_age=0)
        elapsed = time.time() - started
        self.assertEqual(code, 5, 'relay trouble is exit 5, never 2 (a missing tick.py or argparse error is 2)')
        self.assertLess(elapsed, 8, 'must not wait anywhere near the 90s default')
        result = json.loads(out)
        self.assertEqual(result['error'], 'relay_trouble')
        self.assertIn('no answer', result['reason'])
        self.assertNotIn('work', result, 'relay trouble never reports work or idle')


def plain_folder(seat='orch'):
    """A bare relay folder (requests/, responses/, archive/) with no room and no relay behind it."""
    folder = case_dir('tick') / 'relay' / seat
    for sub in ('requests', 'responses', 'archive'):
        (folder / sub).mkdir(parents=True)
    return str(folder)


def write_state(folder, written_at, notes=(), tasks=(), actor='orch'):
    Path(folder, tick.STATE_NAME).write_text(json.dumps({
        'relay_written_at': written_at, 'server_time': written_at, 'relay_actor': actor,
        'actor': {'id': actor, 'role': 'worker'}, 'tasks': list(tasks), 'notes': list(notes)}), encoding='utf-8')


class RelayTroubleTests(TickTestCase):
    """VERIFY7B M2: a failed or refused refresh, or a clock-skewed relay-state.json, is relay trouble
    (exit 5 with a reason), never "Idle.". A negative age (relay clock ahead) is stale, not fresh."""

    def trouble(self, folder, max_age=1, **kw):
        code, out, err = self.check(folder, 'orchestrator', timeout=2, max_age=max_age, **kw)
        self.assertEqual(code, 5, out + err)
        result = json.loads(out)
        self.assertEqual(result['ok'], False)
        self.assertEqual(result['error'], 'relay_trouble')
        self.assertNotIn('work', result)
        return result['reason']

    def test_refused_state_request_is_relay_trouble_not_idle(self):
        folder = plain_folder()
        write_state(folder, time.time() - 600)
        with mock.patch.object(tick.relay_ask, 'send', return_value={'ok': False, 'error': 'unauthenticated'}) as send:
            reason = self.trouble(folder)
        self.assertEqual(send.call_count, 1)
        self.assertIn('refused', reason)
        self.assertIn('unauthenticated', reason)

    def test_state_not_newer_after_refresh_is_relay_trouble(self):
        # A relay clock 30 s ahead: the old file's age is negative (stale, so refresh), the relay
        # says ok but the file is the same one. Only the "newer than before" rule can catch this:
        # its age (-30 s) is inside [-max_age, max_age].
        folder = plain_folder()
        write_state(folder, time.time() + 30)
        with mock.patch.object(tick.relay_ask, 'send', return_value={'ok': True, 'result': {'written': True}}):
            reason = self.trouble(folder, max_age=120)
        self.assertIn('no newer', reason)

    def test_state_missing_after_refresh_is_relay_trouble(self):
        folder = plain_folder()
        with mock.patch.object(tick.relay_ask, 'send', return_value={'ok': True, 'result': {'written': True}}):
            reason = self.trouble(folder)
        self.assertIn('missing', reason)

    def test_negative_age_is_stale_and_refreshes(self):
        folder = plain_folder()
        write_state(folder, time.time() + 30)            # relay clock 30 s ahead: a negative age is not "fresh"
        calls = []

        def honest_send(f, request, timeout=90.0):
            calls.append(request['kind'])
            write_state(f, time.time() + 30.5)           # rewritten, by the same (30 s ahead) clock
            return {'ok': True, 'result': {'written': True}}
        with mock.patch.object(tick.relay_ask, 'send', honest_send):
            code, out, err = self.check(folder, 'orchestrator', timeout=2, max_age=120)
        self.assertEqual(calls, ['state'], 'a negative age must trigger exactly one refresh')
        self.assertEqual(code, 3, out)

    def test_relay_clock_ahead_after_refresh_is_relay_trouble(self):
        folder = plain_folder()

        def skewed_send(f, request, timeout=90.0):
            write_state(f, time.time() + 7200)
            return {'ok': True, 'result': {'written': True}}
        with mock.patch.object(tick.relay_ask, 'send', skewed_send):
            reason = self.trouble(folder, max_age=120)
        self.assertIn('clock', reason)

    def test_relay_clock_behind_after_refresh_is_relay_trouble(self):
        folder = plain_folder()

        def skewed_send(f, request, timeout=90.0):
            write_state(f, time.time() - 7200)
            return {'ok': True, 'result': {'written': True}}
        with mock.patch.object(tick.relay_ask, 'send', skewed_send):
            reason = self.trouble(folder, max_age=120)
        self.assertIn('clock', reason)

    def test_state_unreadable_when_read_is_relay_trouble(self):
        """VERIFY7B T08 (its exact mutant): the state read after the freshness decision can still
        find no usable file (a torn or vanished write). That is relay trouble, never a crash."""
        folder = plain_folder()
        with mock.patch.object(tick, '_state_stamp', return_value=time.time()):     # looks fresh: no refresh
            reason = self.trouble(folder, max_age=120)
        self.assertIn('unreadable', reason)

    def test_relay_folder_unusable_is_relay_trouble(self):
        folder = plain_folder()
        with mock.patch.object(tick.relay_ask, 'send', side_effect=PermissionError('requests/ is read-only')):
            reason = self.trouble(folder)
        self.assertIn('could not be used', reason)

    def test_no_relay_folder_is_relay_trouble(self):
        folder = str(case_dir('tick') / 'relay' / 'orch')
        os.makedirs(folder)
        reason = self.trouble(folder)
        self.assertIn('no relay folder', reason)

    def test_room_down_relay_up_is_relay_trouble(self):
        room = self.start_band()
        self.running_relay(room, ['orch'])
        folder = self.folder(room, 'orch')
        self.assertEqual(self.check(folder, 'orchestrator', max_age=120)[0], 3)
        room.close()
        time.sleep(1.2)
        # 60 s, not 20: on Windows each connection to a closed loopback port takes about two seconds to be
        # refused, and the relay's client tries three times before it answers (the real default is 90 s).
        code, out, err = self.check(folder, 'orchestrator', timeout=60, max_age=1)
        self.assertEqual(code, 5, out + err)
        self.assertIn('refused', json.loads(out)['reason'])

    def test_as_a_process_under_python310_trouble_is_5_and_a_missing_tick_is_not(self):
        python = shutil.which('python3.10')
        if python is None:
            self.skipTest('python3.10 is not installed here')
        folder = plain_folder()
        proc = subprocess.run([python, '-B', str(TICK_PATH), folder, 'check', '--role', 'orchestrator',
                               '--timeout', '1', '--max-age', '0'], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 5, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['error'], 'relay_trouble')
        missing = subprocess.run([python, '-B', str(TICK_PATH) + '.missing', folder, 'check', '--role', 'orchestrator'],
                                 capture_output=True, text=True, timeout=60)
        typo = subprocess.run([python, '-B', str(TICK_PATH), folder, 'check', '--role', 'orchestrater'],
                              capture_output=True, text=True, timeout=60)
        self.assertNotIn(missing.returncode, (0, 3, 4, 5))
        self.assertNotIn(typo.returncode, (0, 3, 4, 5))
        lock = subprocess.run([python, '-B', str(TICK_PATH), folder, 'lock', '--holder', 'auto'],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(lock.returncode, 0, lock.stdout + lock.stderr)
        self.assertTrue(json.loads(lock.stdout)['holder'].startswith('orch-'))


class OrchestratorWorkKindTests(TickTestCase):
    def test_sees_own_delivered_task(self):
        room = self.start_band()
        self.running_relay(room, ['orch'])
        folder = self.folder(room, 'orch')
        task = self.deliver_root_to_orch(room)
        code, out, err = self.check(folder, 'orchestrator')
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        kinds = {(i['kind'], i['task_id']) for i in result['items']}
        self.assertIn(('task', task), kinds)
        self.assertTrue(result['work'])

    def test_sees_own_acknowledged_task(self):
        room = self.start_band()
        self.running_relay(room, ['orch'])
        folder = self.folder(room, 'orch')
        task = self.deliver_root_to_orch(room)
        room.store.execute(room.store.people['orch'], 'ack', {'task_id': task}, 'ack-1')
        code, out, err = self.check(folder, 'orchestrator')
        self.assertEqual(code, 0, err)
        kinds = {(i['kind'], i['task_id']) for i in json.loads(out)['items']}
        self.assertIn(('task', task), kinds)

    def test_sees_own_claimed_task(self):
        room = self.start_band()
        self.running_relay(room, ['orch'])
        folder = self.folder(room, 'orch')
        task = self.deliver_root_to_orch(room)
        orch = room.store.people['orch']
        room.store.execute(orch, 'ack', {'task_id': task}, 'ack-1')
        room.store.execute(orch, 'claim', {'task_id': task, 'seconds': 1800}, 'claim-1')
        code, out, err = self.check(folder, 'orchestrator')
        self.assertEqual(code, 0, err)
        kinds = {(i['kind'], i['task_id']) for i in json.loads(out)['items']}
        self.assertIn(('task', task), kinds)

    def test_own_helper_waiting_decision_is_answerable_work_not_a_question(self):
        room = self.start_band()
        self.running_relay(room, ['orch'])
        folder = self.folder(room, 'orch')
        root, child = self.make_helper_waiting(room)
        code, out, err = self.check(folder, 'orchestrator')
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        kinds = {(i['kind'], i['task_id']) for i in result['items']}
        self.assertIn(('helper_decision', child), kinds)
        self.assertNotIn(child, [q.get('task_id') for q in result['questions_for_shane']])

    def test_root_task_waiting_decision_is_a_question_i_cannot_answer(self):
        room = self.start_band()
        self.running_relay(room, ['orch'])
        folder = self.folder(room, 'orch')
        task = self.deliver_root_to_orch(room)
        orch = room.store.people['orch']
        room.store.execute(orch, 'ack', {'task_id': task}, 'ack-1')
        fence = room.store.execute(orch, 'claim', {'task_id': task, 'seconds': 1800}, 'claim-1')['task']['fence']
        room.store.execute(orch, 'wait_decision', {'task_id': task, 'fence': fence, 'question': 'Which way?'}, 'wd-1')
        code, out, err = self.check(folder, 'orchestrator')
        result = json.loads(out)
        qtasks = [q['task_id'] for q in result['questions_for_shane']]
        self.assertIn(task, qtasks)
        self.assertNotIn(('task', task), {(i['kind'], i['task_id']) for i in result['items']},
                          'a task I cannot act on is not "work" for me')

    def test_helper_with_two_strikes_is_a_question_even_though_i_sent_it(self):
        room = self.start_band()
        self.running_relay(room, ['orch'])
        folder = self.folder(room, 'orch')
        root, child = self.make_helper_waiting(room)
        # Answer the first question so it can be claimed and failed again.
        orch = room.store.people['orch']
        room.store.execute(orch, 'resolve_decision', {'task_id': child, 'body': 'Use notes.md'}, 'answer-1')
        self.fail_twice(room, child)
        code, out, err = self.check(folder, 'orchestrator')
        result = json.loads(out)
        qtasks = [q['task_id'] for q in result['questions_for_shane']]
        self.assertIn(child, qtasks)
        self.assertNotIn(('helper_decision', child), {(i['kind'], i['task_id']) for i in result['items']})

    def test_question_note_is_an_item_and_a_question(self):
        room = self.start_band()
        self.running_relay(room, ['orch'])
        folder = self.folder(room, 'orch')
        self.post_note(room, 'helper', 'QUESTION: which file is authoritative?')
        code, out, err = self.check(folder, 'orchestrator')
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertTrue(any(i['kind'] == 'note' for i in result['items']))
        self.assertTrue(any('which file is authoritative' in q['question'] for q in result['questions_for_shane']))

    def test_shane_update_note_is_an_item_but_never_a_question(self):
        room = self.start_band()
        self.running_relay(room, ['orch'])
        folder = self.folder(room, 'orch')
        self.post_note(room, 'helper', 'SHANE UPDATE: ship the blue version')
        code, out, err = self.check(folder, 'orchestrator')
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertTrue(any(i['kind'] == 'note' for i in result['items']))
        self.assertEqual(result['questions_for_shane'], [])

    def test_seen_note_seq_hides_only_notes_up_to_that_point(self):
        # `seen` only decides what wakes the seat; the QUESTION stays on Shane's list (M4).
        room = self.start_band()
        self.running_relay(room, ['orch'])
        folder = self.folder(room, 'orch')
        self.post_note(room, 'helper', 'QUESTION: first one', ref='n1')
        _, out, err = self.check(folder, 'orchestrator')
        first = json.loads(out)
        seq1 = next(q['note_seq'] for q in first['questions_for_shane'])
        code, out, err = self.run_tick(folder, 'seen', '--note-seq', str(seq1))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.run_tick(folder, 'notified', '--digest', first['questions_digest'])[0], 0)
        code, out, err = self.check(folder, 'orchestrator')
        self.assertEqual(code, 3, out)
        result = json.loads(out)
        self.assertEqual(result['items'], [])
        self.assertEqual([q.get('note_seq') for q in result['questions_for_shane']], [seq1])
        self.post_note(room, 'helper', 'QUESTION: second one', ref='n2')
        code, out, err = self.check(folder, 'orchestrator')
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual([i['title'] for i in result['items'] if i['kind'] == 'note'], ['QUESTION: second one'])
        self.assertTrue(any('second one' in q['question'] for q in result['questions_for_shane']))
        self.assertTrue(result['new_questions'])

    def test_a_plain_lounge_note_is_neither_work_nor_a_question(self):
        room = self.start_band()
        self.running_relay(room, ['orch'])
        folder = self.folder(room, 'orch')
        self.post_note(room, 'helper', 'Lunch is at noon; nothing for anyone to do.', ref='plain')
        code, out, err = self.check(folder, 'orchestrator')
        self.assertEqual(code, 3, out)
        result = json.loads(out)
        self.assertEqual(result['items'], [])
        self.assertEqual(result['questions_for_shane'], [])


class OrchestratorQuietWhileWaitingTests(TickTestCase):
    """E2E3 finding: a reserve task (or any workstream only waiting on helpers) must not keep the
    hourly check busy forever. After `seen --all` it is quiet; any helper status change wakes it."""
    def test_reserve_task_is_quiet_after_seen_all(self):
        room = self.start_band()
        self.running_relay(room, ['orch'])
        folder = self.folder(room, 'orch')
        task = self.deliver_root_to_orch(room, title='Reserve: new work inside this plan')
        room.store.execute(room.store.people['orch'], 'ack', {'task_id': task}, 'ack-r')
        code, out, err = self.check(folder, 'orchestrator')
        self.assertEqual(code, 0, err)              # never seen: work once
        code, out, err = self.run_tick(folder, 'seen', '--all')
        self.assertEqual(code, 0, err)
        code, out, err = self.check(folder, 'orchestrator')
        self.assertEqual(code, 3, out)              # only waiting: idle
        self.assertEqual(json.loads(out)['items'], [])

    def test_workstream_waiting_on_a_helper_is_quiet_until_the_helper_moves(self):
        room = self.start_band()
        self.running_relay(room, ['orch'])
        folder = self.folder(room, 'orch')
        root = self.deliver_root_to_orch(room, ref='ws9')
        orch = room.store.people['orch']
        room.store.execute(orch, 'ack', {'task_id': root}, 'ack-ws9')
        fence = room.store.execute(orch, 'claim', {'task_id': root, 'seconds': 1800}, 'claim-ws9')['task']['fence']
        child = room.store.execute(orch, 'request_help', {'task_id': root, 'fence': fence, 'recipient': 'helper',
                                    'title': 'Piece', 'body': 'Piece body', 'acceptance': 'Piece done'},
                                   'help-ws9')['task']['id']
        self.check(folder, 'orchestrator')
        self.assertEqual(self.run_tick(folder, 'seen', '--all')[0], 0)
        code, out, err = self.check(folder, 'orchestrator')
        self.assertEqual(code, 3, out)
        room.store.execute(room.store.people['helper'], 'ack', {'task_id': child}, 'hack-ws9')
        code, out, err = self.check(folder, 'orchestrator')
        self.assertEqual(code, 0, out)
        items = json.loads(out)['items']
        self.assertTrue(any(i['task_id'] == root and child in i['detail'] for i in items), items)

    def test_seen_all_also_records_flagged_notes(self):
        room = self.start_band()
        self.running_relay(room, ['orch'])
        folder = self.folder(room, 'orch')
        self.post_note(room, 'helper', 'SHANE UPDATE: use the blue one', ref='n9')
        self.assertEqual(self.check(folder, 'orchestrator')[0], 0)
        self.assertEqual(self.run_tick(folder, 'seen', '--all')[0], 0)
        self.assertEqual(self.check(folder, 'orchestrator')[0], 3)

    def test_a_new_delivered_task_is_work_even_after_seen_all(self):
        room = self.start_band()
        self.running_relay(room, ['orch'])
        folder = self.folder(room, 'orch')
        self.check(folder, 'orchestrator')
        self.assertEqual(self.run_tick(folder, 'seen', '--all')[0], 0)
        task = self.deliver_root_to_orch(room, ref='late')
        code, out, err = self.check(folder, 'orchestrator')
        self.assertEqual(code, 0, out)
        self.assertIn(task, [i['task_id'] for i in json.loads(out)['items']])


    def test_a_delivered_task_stays_work_even_if_seen_all_recorded_it(self):
        room = self.start_band()
        self.running_relay(room, ['orch'])
        folder = self.folder(room, 'orch')
        task = self.deliver_root_to_orch(room, ref='unacked')
        self.check(folder, 'orchestrator')
        self.assertEqual(self.run_tick(folder, 'seen', '--all')[0], 0)
        code, out, err = self.check(folder, 'orchestrator')
        self.assertEqual(code, 0, out)       # nobody has acknowledged it yet: still the seat's to do
        self.assertIn(task, [i['task_id'] for i in json.loads(out)['items']])

    def test_helper_question_is_quiet_after_seen_all_until_it_changes(self):
        """VERIFY7B M3: an own helper's open question follows the seen rule too: after `seen --all`
        it no longer wakes the seat every hour (the orchestrator escalated it via QUESTIONS.md),
        and it wakes it again when that helper's status or its question text changes."""
        room = self.start_band()
        self.running_relay(room, ['orch'])
        folder = self.folder(room, 'orch')
        root, child = self.make_helper_waiting(room, ref='hq')
        code, out, err = self.check(folder, 'orchestrator')
        self.assertEqual(code, 0, out)
        self.assertIn(('helper_decision', child), {(i['kind'], i['task_id']) for i in json.loads(out)['items']})
        self.assertEqual(self.run_tick(folder, 'seen', '--all')[0], 0)
        for _ in range(2):                                    # two "hourly" checks: quiet both times
            code, out, err = self.check(folder, 'orchestrator')
            self.assertEqual(code, 3, out)
            self.assertEqual(json.loads(out)['items'], [])
        # Same status (waiting_decision) again, different question: wakes.
        orch, helper = room.store.people['orch'], room.store.people['helper']
        room.store.execute(orch, 'resolve_decision', {'task_id': child, 'body': 'Use notes.md'}, 'ans-hq')
        room.store.execute(helper, 'ack', {'task_id': child}, 'reack-hq')
        hf = room.store.execute(helper, 'claim', {'task_id': child, 'seconds': 1800}, 'reclaim-hq')['task']['fence']
        room.store.execute(helper, 'wait_decision', {'task_id': child, 'fence': hf, 'question': 'Which folder, then?'},
                           'hq2-hq')
        code, out, err = self.check(folder, 'orchestrator')
        self.assertEqual(code, 0, out)
        items = json.loads(out)['items']
        self.assertEqual([(i['kind'], i['task_id'], i['detail']) for i in items],
                         [('helper_decision', child, 'Which folder, then?')])

    def test_helper_question_status_change_wakes_after_seen_all(self):
        room = self.start_band()
        self.running_relay(room, ['orch'])
        folder = self.folder(room, 'orch')
        root, child = self.make_helper_waiting(room, ref='hs')
        self.check(folder, 'orchestrator')
        self.assertEqual(self.run_tick(folder, 'seen', '--all')[0], 0)
        self.assertEqual(self.check(folder, 'orchestrator')[0], 3)
        room.store.execute(room.store.people['orch'], 'resolve_decision', {'task_id': child, 'body': 'Use a.md'}, 'ans-hs')
        code, out, err = self.check(folder, 'orchestrator')
        self.assertEqual(code, 0, out)       # the helper moved (waiting_decision -> delivered)


class QuestionsForShaneTests(TickTestCase):
    """VERIFY7B M4: `seen --all` never changes questions_for_shane or its digest. A QUESTION note
    stays on Shane's list until `answered --note-seq N` records it answered."""

    def root_question_and_note(self, room, ref='q1'):
        task = self.deliver_root_to_orch(room, ref=ref)
        orch = room.store.people['orch']
        room.store.execute(orch, 'ack', {'task_id': task}, 'ack-' + ref)
        fence = room.store.execute(orch, 'claim', {'task_id': task, 'seconds': 1800}, 'claim-' + ref)['task']['fence']
        room.store.execute(orch, 'wait_decision', {'task_id': task, 'fence': fence, 'question': 'Which way?'}, 'wd-' + ref)
        self.post_note(room, 'helper', 'QUESTION for Shane: may I use the red folder?', ref=ref + 'n')
        return task

    def test_seen_all_changes_neither_the_questions_nor_the_digest(self):
        room = self.start_band()
        self.running_relay(room, ['orch'])
        folder = self.folder(room, 'orch')
        self.root_question_and_note(room)
        code1, out1, _ = self.check(folder, 'orchestrator')
        first = json.loads(out1)
        self.assertEqual(code1, 0)
        self.assertEqual(len(first['questions_for_shane']), 2)
        self.assertTrue(first['new_questions'])
        self.assertEqual(self.run_tick(folder, 'notified', '--digest', first['questions_digest'])[0], 0)
        self.assertEqual(self.run_tick(folder, 'seen', '--all')[0], 0)
        code2, out2, _ = self.check(folder, 'orchestrator')
        second = json.loads(out2)
        self.assertEqual(code2, 3, out2)
        self.assertFalse(second['new_questions'])
        self.assertEqual(second['questions_for_shane'], first['questions_for_shane'])
        self.assertEqual(second['questions_digest'], first['questions_digest'])

    def test_question_note_stays_listed_until_answered(self):
        room = self.start_band()
        self.running_relay(room, ['orch'])
        folder = self.folder(room, 'orch')
        self.post_note(room, 'helper', 'QUESTION: may I delete the draft?', ref='qa')
        _, out, _ = self.check(folder, 'orchestrator')
        seq = json.loads(out)['questions_for_shane'][0]['note_seq']
        self.assertEqual(self.run_tick(folder, 'seen', '--all')[0], 0)
        self.assertEqual(self.run_tick(folder, 'seen', '--note-seq', str(seq + 5))[0], 0)
        _, out, _ = self.check(folder, 'orchestrator')
        self.assertEqual([q.get('note_seq') for q in json.loads(out)['questions_for_shane']], [seq])
        code, out, err = self.run_tick(folder, 'answered', '--note-seq', str(seq))
        self.assertEqual(code, 0, out + err)
        state = json.loads(Path(folder, tick.TICK_STATE_NAME).read_text(encoding='utf-8'))
        self.assertEqual(state['answered_note_seqs'], [seq])
        _, out, _ = self.check(folder, 'orchestrator')
        self.assertEqual(json.loads(out)['questions_for_shane'], [])
        code, out, err = self.run_tick(folder, 'questions')
        self.assertIn('no open questions', out)

    def test_answered_forgets_notes_below_the_window_and_flags_an_unknown_number(self):
        folder = plain_folder()
        write_state(folder, time.time(), notes=[{'seq': s, 'sender': 'helper', 'body': 'QUESTION: q%d' % s}
                                                for s in (50, 51, 52)])
        Path(folder, tick.TICK_STATE_NAME).write_text(json.dumps({'answered_note_seqs': [3, 51]}), encoding='utf-8')
        code, out, err = self.run_tick(folder, 'answered', '--note-seq', '52')
        self.assertEqual(code, 0, out + err)
        result = json.loads(out)
        self.assertEqual(result['answered_note_seqs'], [51, 52], 'seq 3 scrolled out of the window: forgotten')
        self.assertTrue(result['note_found'])
        self.assertNotIn('warning', result)
        self.assertEqual([q['note_seq'] for q in tick.compute_orchestrator(
            json.loads(Path(folder, tick.STATE_NAME).read_text(encoding='utf-8')),
            json.loads(Path(folder, tick.TICK_STATE_NAME).read_text(encoding='utf-8')))[1]], [50])
        code, out, err = self.run_tick(folder, 'answered', '--note-seq', '51000')
        result = json.loads(out)
        self.assertFalse(result['note_found'])
        self.assertIn('not a QUESTION note', result['warning'])

    def test_answering_a_notified_question_does_not_ping_again(self):
        room = self.start_band()
        self.running_relay(room, ['orch'])
        folder = self.folder(room, 'orch')
        self.root_question_and_note(room, ref='q2')
        first = json.loads(self.check(folder, 'orchestrator')[1])
        note_seq = next(q['note_seq'] for q in first['questions_for_shane'] if 'note_seq' in q)
        self.assertEqual(self.run_tick(folder, 'notified', '--digest', first['questions_digest'])[0], 0)
        self.assertEqual(self.run_tick(folder, 'answered', '--note-seq', str(note_seq))[0], 0)
        self.assertEqual(self.run_tick(folder, 'seen', '--all')[0], 0)
        code, out, _ = self.check(folder, 'orchestrator')
        result = json.loads(out)
        self.assertEqual(code, 3, out)
        self.assertFalse(result['new_questions'], 'one question fewer is not a new batch for Shane')
        self.assertEqual([q.get('task_id') is not None for q in result['questions_for_shane']], [True])

class ReviewerQueueTests(TickTestCase):
    def _finish_task(self, room, builder_actor, ref='r1'):
        task = self.deliver_root_to_orch(room, ref=ref, recipient=builder_actor)
        builder = room.store.people[builder_actor]
        room.store.execute(builder, 'ack', {'task_id': task}, 'back-' + ref)
        fence = room.store.execute(builder, 'claim', {'task_id': task, 'seconds': 1800}, 'bclaim-' + ref)['task']['fence']
        from common_room.handoff_api import handoff_operation
        staged = handoff_operation(room.server, builder, 'stage_text', {'task_id': task, 'fence': fence, 'text': 'Body'})
        room.store.execute(builder, 'finish', {'task_id': task, 'fence': fence, 'body': 'Done', 'refs': [staged]},
                            'bfinish-' + ref)
        return task

    def test_reviewer_sees_ready_for_review_not_built_by_self(self):
        room = self.start_band()
        task = self._finish_task(room, 'helper')
        self.running_relay(room, ['judge'])
        folder = self.folder(room, 'judge')
        code, out, err = self.check(folder, 'reviewer')
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result['role'], 'reviewer')
        task_ids = {i['task_id'] for i in result['items']}
        self.assertIn(task, task_ids)
        self.assertEqual(result['questions_for_shane'], [])
        self.assertFalse(result['new_questions'])

    def test_reviewer_never_lists_its_own_build(self):
        room = self.start_band()
        # 'helper' is a plain worker seat; checking AS helper with --role reviewer exercises
        # the "builder != me" filter directly, without needing a real self-review attempt.
        task = self._finish_task(room, 'helper')
        self.running_relay(room, ['helper'])
        folder = self.folder(room, 'helper')
        code, out, err = self.check(folder, 'reviewer')
        self.assertEqual(code, 3, err)
        self.assertEqual(json.loads(out)['items'], [])

    def test_orchestrator_own_queue_and_reviewer_queue_do_not_leak_into_each_other(self):
        room = self.start_band()
        task = self.deliver_root_to_orch(room)
        review_task = self._finish_task(room, 'helper', ref='r2')
        self.running_relay(room, ['orch'])
        folder = self.folder(room, 'orch')
        code, out, err = self.check(folder, 'orchestrator')
        result = json.loads(out)
        self.assertFalse(any(i['kind'] == 'review' for i in result['items']))
        self.assertTrue(any(i['task_id'] == task for i in result['items']))


class DigestAndNotifiedTests(TickTestCase):
    def test_digest_is_stable_and_changes_only_when_questions_change(self):
        room = self.start_band()
        self.running_relay(room, ['orch'])
        folder = self.folder(room, 'orch')
        task = self.deliver_root_to_orch(room)
        orch = room.store.people['orch']
        room.store.execute(orch, 'ack', {'task_id': task}, 'a1')
        fence = room.store.execute(orch, 'claim', {'task_id': task, 'seconds': 1800}, 'c1')['task']['fence']
        room.store.execute(orch, 'wait_decision', {'task_id': task, 'fence': fence, 'question': 'Which way?'}, 'w1')
        _, out1, _ = self.check(folder, 'orchestrator', max_age=0)
        digest1 = json.loads(out1)['questions_digest']
        _, out2, _ = self.check(folder, 'orchestrator', max_age=0)
        digest2 = json.loads(out2)['questions_digest']
        self.assertEqual(digest1, digest2, 'nothing changed; the digest must not move')
        self.post_note(room, 'helper', 'QUESTION: a brand new one')
        _, out3, _ = self.check(folder, 'orchestrator', max_age=0)
        digest3 = json.loads(out3)['questions_digest']
        self.assertNotEqual(digest1, digest3)

    def test_notified_clears_new_questions_until_something_changes_again(self):
        room = self.start_band()
        self.running_relay(room, ['orch'])
        folder = self.folder(room, 'orch')
        self.post_note(room, 'helper', 'QUESTION: please decide', ref='n1')
        code, out, err = self.check(folder, 'orchestrator', max_age=0)
        result = json.loads(out)
        self.assertTrue(result['new_questions'])
        digest = result['questions_digest']
        code, out, err = self.run_tick(folder, 'notified', '--digest', digest)
        self.assertEqual(code, 0, err)
        code, out, err = self.check(folder, 'orchestrator', max_age=0)
        result = json.loads(out)
        self.assertFalse(result['new_questions'], 'the same question set was already notified')
        self.post_note(room, 'helper', 'QUESTION: and another', ref='n2')
        code, out, err = self.check(folder, 'orchestrator', max_age=0)
        self.assertTrue(json.loads(out)['new_questions'])


class LockTests(TickTestCase):
    def test_lock_busy_then_expired_then_own_extend(self):
        room = self.start_band()
        folder = self.folder(room, 'orch')
        os.makedirs(os.path.join(folder, 'requests'), exist_ok=True)
        result, code = tick.cmd_lock(folder, 'live-chat', 45, now=1000.0)
        self.assertEqual(code, 0, result)
        result2, code2 = tick.cmd_lock(folder, 'scheduled-orch', 45, now=1005.0)
        self.assertEqual(code2, 4, result2)
        self.assertEqual(result2['holder'], 'live-chat')
        # Expired: now moves past the first holder's 'until'.
        result3, code3 = tick.cmd_lock(folder, 'scheduled-orch', 45, now=1000.0 + 45 * 60 + 1)
        self.assertEqual(code3, 0, result3)
        # Own-extend: the same holder can renew.
        result4, code4 = tick.cmd_lock(folder, 'scheduled-orch', 60, now=1000.0 + 45 * 60 + 2)
        self.assertEqual(code4, 0, result4)
        self.assertEqual(result4['result']['holder'], 'scheduled-orch')

    def test_unlock_by_a_different_holder_is_refused_and_never_deletes(self):
        room = self.start_band()
        folder = self.folder(room, 'orch')
        os.makedirs(os.path.join(folder, 'requests'), exist_ok=True)
        tick.cmd_lock(folder, 'scheduled-orch', 45, now=2000.0)
        result, code = tick.cmd_unlock(folder, 'someone-else')
        self.assertEqual(code, 4, result)
        lock_path = Path(folder) / tick.LOCK_NAME
        self.assertTrue(lock_path.exists())
        self.assertEqual(json.loads(lock_path.read_text(encoding='utf-8'))['holder'], 'scheduled-orch')
        result2, code2 = tick.cmd_unlock(folder, 'scheduled-orch')
        self.assertEqual(code2, 0, result2)
        self.assertTrue(lock_path.exists(), 'unlock never deletes the file')
        released = json.loads(lock_path.read_text(encoding='utf-8'))
        self.assertTrue(released['released'])
        self.assertEqual(released['holder'], 'scheduled-orch')

    def test_check_refuses_before_any_relay_request_when_locked_by_another_holder(self):
        room = self.start_band()
        folder = self.folder(room, 'orch')
        os.makedirs(os.path.join(folder, 'requests'), exist_ok=True)
        os.makedirs(os.path.join(folder, 'responses'), exist_ok=True)
        os.makedirs(os.path.join(folder, 'archive'), exist_ok=True)
        tick.cmd_lock(folder, 'live-chat', 45)
        started = time.time()
        # No relay is running at all; if the lock check did not come first, this would
        # have to wait out the relay timeout instead of refusing immediately.
        code, out, err = self.check(folder, 'orchestrator', holder='scheduled-orch', timeout=30, max_age=0)
        elapsed = time.time() - started
        self.assertEqual(code, 4, err)
        self.assertLess(elapsed, 5)
        self.assertEqual(list(Path(folder, 'requests').iterdir()), [])
        self.assertEqual(json.loads(out)['holder'], 'live-chat')

    def test_lock_status_reports_state(self):
        room = self.start_band()
        folder = self.folder(room, 'orch')
        os.makedirs(folder, exist_ok=True)
        self.assertEqual(tick.cmd_lock_status(folder)['state'], 'absent')
        tick.cmd_lock(folder, 'scheduled-orch', 45)
        self.assertEqual(tick.cmd_lock_status(folder)['state'], 'held')

    def test_lock_auto_makes_a_unique_holder_and_unlock_needs_it_exactly(self):
        """VERIFY7B L3: every scheduled firing gets its own holder, so one run can neither pass
        another's lock nor release it."""
        folder = plain_folder('claude-room')
        code, out, err = self.run_tick(folder, 'lock', '--holder', 'auto', '--minutes', '45')
        self.assertEqual(code, 0, out + err)
        holder = json.loads(out)['holder']
        self.assertRegex(holder, r'^claude-room-\d{8}T\d{6}Z-[0-9a-f]{8}$')
        self.assertEqual(json.loads(Path(folder, tick.LOCK_NAME).read_text(encoding='utf-8'))['holder'], holder)
        code, out, err = self.run_tick(folder, 'lock', '--holder', 'auto')
        self.assertEqual(code, 4, out)                         # a second firing: busy, not "own"
        self.assertEqual(json.loads(out)['holder'], holder)
        self.assertEqual(self.check(folder, 'orchestrator')[0], 4)   # holder-less check: busy
        self.assertEqual(self.run_tick(folder, 'unlock', '--holder', 'auto')[0], 4)
        self.assertEqual(self.run_tick(folder, 'unlock', '--holder', 'claude-room')[0], 4)
        self.assertEqual(tick.cmd_lock_status(folder)['state'], 'held')
        code, out, err = self.run_tick(folder, 'lock', '--holder', holder, '--minutes', '45')
        self.assertEqual(code, 0, out)                         # the same holder renews
        code, out, err = self.run_tick(folder, 'unlock', '--holder', holder)
        self.assertEqual(code, 0, out)
        self.assertEqual(tick.cmd_lock_status(folder)['state'], 'released')

    def test_unlock_auto_is_refused_even_with_no_lock(self):
        folder = plain_folder()
        code, out, err = self.run_tick(folder, 'unlock', '--holder', 'auto')
        self.assertEqual(code, 4, out)
        self.assertIn('exact holder', json.loads(out)['message'])

    def test_auto_holders_made_in_the_same_second_differ(self):
        folder = plain_folder('tinker')
        made = {tick.auto_holder(folder, now=1790000000.0) for _ in range(50)}
        self.assertEqual(len(made), 50)
        self.assertTrue(all(h.startswith('tinker-20260921T141320Z-') for h in made), made)

    def test_lock_read_back_detects_a_concurrent_acquire(self):
        """VERIFY7B T02: another acquire landing between our write and our read-back wins; we
        must report busy (4), never think we hold the lock."""
        folder = plain_folder()
        real_read_lock = tick.read_lock

        def racing_read_lock(f):
            tick._write_in_place(os.path.join(f, tick.LOCK_NAME),
                                 {'holder': 'other-run', 'since': 1.0, 'until': time.time() + 600})
            return real_read_lock(f)
        with mock.patch.object(tick, 'read_lock', racing_read_lock):
            result, code = tick.cmd_lock(folder, 'this-run', 45)
        self.assertEqual(code, 4, result)
        self.assertEqual(result['holder'], 'other-run')


class NeverRenamesOrDeletesTests(TickTestCase):
    """VERIFY7B T01: the docstring's "never deletes, never renames" claim, tested. With rename,
    replace, unlink, remove and rmdir all refused, every tick.py write path still works."""

    def test_every_write_path_works_with_rename_and_delete_refused(self):
        folder = plain_folder()
        now = time.time()
        write_state(folder, now, notes=[{'seq': 7, 'sender': 'helper', 'body': 'QUESTION: which one?'},
                                        {'seq': 8, 'sender': 'helper', 'body': 'SHANE UPDATE: blue'}])
        calls = []

        def refuse(name):
            def inner(*a, **k):
                calls.append(name)
                raise PermissionError(name + ' refused on this mount')
            return inner
        results = []
        with mock.patch('os.replace', refuse('replace')), mock.patch('os.rename', refuse('rename')), \
                mock.patch('os.unlink', refuse('unlink')), mock.patch('os.remove', refuse('remove')), \
                mock.patch('os.rmdir', refuse('rmdir')):
            code, out, err = self.run_tick(folder, 'lock', '--holder', 'auto')          # create
            h1 = json.loads(out)['holder']
            results.append(code)
            results.append(self.run_tick(folder, 'lock', '--holder', h1, '--minutes', '50')[0])   # renew in place
            results.append(self.run_tick(folder, 'unlock', '--holder', h1)[0])
            code, out, err = self.run_tick(folder, 'lock', '--holder', 'auto')          # over a released lock
            h2 = json.loads(out)['holder']
            results.append(code)
            results.append(self.run_tick(folder, 'unlock', '--holder', h2)[0])
            results.append(self.run_tick(folder, 'notified', '--digest', 'x' * 64)[0])  # create tick-state
            results.append(self.run_tick(folder, 'notified', '--digest', 'y')[0])       # shorter, in place
            results.append(self.run_tick(folder, 'seen', '--note-seq', '3')[0])
            results.append(self.run_tick(folder, 'seen', '--all')[0])
            results.append(self.run_tick(folder, 'answered', '--note-seq', '7')[0])
            results.append(self.check(folder, 'orchestrator', max_age=120)[0])
        self.assertEqual(calls, [])
        self.assertEqual(results, [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 3])
        lock = json.loads(Path(folder, tick.LOCK_NAME).read_text(encoding='utf-8'))
        self.assertEqual((lock['holder'], lock['released']), (h2, True))
        state = json.loads(Path(folder, tick.TICK_STATE_NAME).read_text(encoding='utf-8'))
        self.assertEqual(state['notified_digest'], 'y')
        self.assertEqual(state['seen_note_seq'], 8)
        self.assertEqual(state['answered_note_seqs'], [7])
        self.assertEqual(sorted(os.listdir(folder)),
                         ['archive', 'relay-state.json', 'requests', 'responses', 'session.lock', 'tick-state.json'])


class Python38GrammarTests(unittest.TestCase):
    def test_tick_parses_as_python_3_8(self):
        source = TICK_PATH.read_text(encoding='utf-8')
        ast.parse(source, filename=str(TICK_PATH), feature_version=(3, 8))

    def test_tick_never_opens_a_credential_file_except_to_refuse(self):
        for lineno, line in enumerate(TICK_PATH.read_text(encoding='utf-8').splitlines(), 1):
            if '-credential.json' in line:
                self.assertIn('never', line.lower(), 'line %d: %r' % (lineno, line))


class SchedulesTests(unittest.TestCase):
    REQUIRED_KEYS = {'name', 'cron_expression', 'requires_local_device', 'notifications', 'prompt', 'status', 'turn_on'}
    FILES = ['orchestrator-claude-room.json', 'reviewer-tinker.json', 'reviewer-vesper.json']
    SEATS = {'orchestrator-claude-room.json': 'claude-room',
             'reviewer-tinker.json': 'tinker', 'reviewer-vesper.json': 'vesper'}

    def test_every_schedule_file_parses_with_exactly_the_required_keys(self):
        for name in self.FILES:
            path = SCHEDULES_DIR / name
            data = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(set(data), self.REQUIRED_KEYS, name)
            self.assertTrue(data['requires_local_device'] is True, name)
            self.assertTrue(data['notifications'].get('push') is True, name)
            self.assertIsInstance(data['prompt'], str)
            self.assertIsInstance(data['cron_expression'], str)

    def test_each_prompt_names_its_own_relay_folder_and_tick_and_idle_and_exit_codes(self):
        for name in self.FILES:
            data = json.loads((SCHEDULES_DIR / name).read_text(encoding='utf-8'))
            prompt = data['prompt']
            seat = self.SEATS[name]
            self.assertIn('relay/' + seat, prompt, name)
            self.assertIn('tick.py', prompt, name)
            self.assertIn('Idle.', prompt, name)
            self.assertIn('Relay not answering', prompt, name)
            self.assertIn('Another session is working as', prompt, name)

    def test_readme_exists_and_explains_turning_one_on_and_off(self):
        readme = (SCHEDULES_DIR / 'README.md').read_text(encoding='utf-8')
        self.assertIn('scheduled task', readme.lower())
        self.assertIn('not created', readme.lower())

    def prompts(self):
        return {name: json.loads((SCHEDULES_DIR / name).read_text(encoding='utf-8'))['prompt'] for name in self.FILES}

    def test_every_prompt_has_the_exit_table_with_5_for_the_relay_and_a_catch_all(self):
        for name, prompt in self.prompts().items():
            lines = prompt.splitlines()
            for code in ('0', '3', '4', '5'):
                self.assertEqual(len([l for l in lines if l.startswith('- Exit %s ' % code)]), 1, (name, code))
            self.assertNotIn('Exit 2', prompt, name)
            relay_line = next(l for l in lines if l.startswith('- Exit 5 '))
            self.assertIn('Relay not answering', relay_line, name)
            idle_line = next(l for l in lines if l.startswith('- Exit 3 '))
            self.assertIn('`Idle.`', idle_line, name)
            other = [l for l in lines if l.startswith('- Any other exit code')]
            self.assertEqual(len(other), 1, name)
            self.assertIn('`tick.py failed: ', other[0], name)
            self.assertIn('first 300 characters', other[0], name)
            self.assertIn('stop', other[0], name)

    def test_every_command_path_in_the_prompts_is_under_R_crew(self):
        for name, prompt in self.prompts().items():
            commands = re.findall(r'python3\s+(\S+)', prompt)
            self.assertGreaterEqual(len(commands), 4, name)
            for path in commands:
                self.assertRegex(path, r'^"\$R/crew/[\w-]+\.py"$', name)

    def test_prompts_lock_with_auto_keep_the_holder_and_check_without_one(self):
        for name, prompt in self.prompts().items():
            seat = self.SEATS[name]
            self.assertNotIn('scheduled-' + seat, prompt, 'no fixed label shared by every firing: ' + name)
            check_lines = [l for l in prompt.splitlines() if ' check --role ' in l]
            self.assertEqual(len(check_lines), 1, name)
            self.assertNotIn('--holder', check_lines[0], name)
            lock_lines = [l for l in prompt.splitlines() if re.search(r'"\s+lock\s', l)]
            self.assertEqual(len(lock_lines), 1, name)
            self.assertIn('lock --holder auto', lock_lines[0], name)
            unlock_lines = [l for l in prompt.splitlines() if ' unlock --holder ' in l]
            self.assertEqual(len(unlock_lines), 1, name)
            self.assertNotIn('auto', unlock_lines[0], name)
            self.assertIn('--holder <', unlock_lines[0], name)
            self.assertIn('Keep that exact', prompt, name)

    def test_reviewer_cite_check_command_ends_its_line(self):
        for name in ('reviewer-tinker.json', 'reviewer-vesper.json'):
            prompt = self.prompts()[name]
            lines = [l for l in prompt.splitlines() if 'cite_check.py' in l]
            self.assertEqual(len(lines), 1, name)
            self.assertTrue(lines[0].rstrip().endswith('--relay-answer -'), lines[0])

    def test_reviewer_prompt_explains_cite_checks_exit_2_and_not_checked_rows(self):
        """VERIFY8B N8: the reviewer prompts said a citation check failure is grounds to reject
        without saying that exit 2 (the check itself could not run) and a NOT CHECKED row are not
        grounds to reject by themselves -- only MISSING FILE, OUT OF RANGE and QUOTE NOT FOUND
        are."""
        for name in ('reviewer-tinker.json', 'reviewer-vesper.json'):
            prompt = self.prompts()[name]
            self.assertIn('exit 2', prompt, name)
            self.assertNotIn('Exit 2', prompt, name)  # never mistaken for tick.py check's own table
            self.assertIn('the check itself could not run', prompt, name)
            self.assertIn('NOT CHECKED', prompt, name)
            self.assertIn('MISSING FILE, OUT OF RANGE or QUOTE NOT FOUND', prompt, name)
            not_grounds = [l for l in prompt.split('. ') if 'not grounds to reject' in l or 'not by itself grounds to reject' in l]
            self.assertGreaterEqual(len(not_grounds), 2, (name, prompt))
            self.assertIn('is grounds to reject', prompt, name)

    def test_orchestrator_prompt_marks_answered_notes_and_seen_all_only_when_waiting(self):
        prompt = self.prompts()['orchestrator-claude-room.json']
        answered = [l for l in prompt.splitlines() if 'answered --note-seq' in l]
        self.assertEqual(len(answered), 1)
        self.assertIn('"$R/crew/tick.py" "$R/.runtime/handoffs/relay/claude-room" answered --note-seq <N>', answered[0])
        self.assertIn('QUESTION', answered[0])
        seen = [l for l in prompt.splitlines() if 'seen --all' in l]
        self.assertEqual(len(seen), 1)
        self.assertIn('only waiting', seen[0])
        self.assertIn('do NOT run it', seen[0])

    def test_readme_names_the_exit_codes_and_the_folder_the_creating_chat_needs(self):
        readme = (SCHEDULES_DIR / 'README.md').read_text(encoding='utf-8')
        for must in ('exit 5', 'tick.py failed: ', 'lock --holder auto', 'Work Area'):
            self.assertIn(must, readme)

    def _readme_table_rows(self):
        readme = (SCHEDULES_DIR / 'README.md').read_text(encoding='utf-8')
        return [l for l in readme.splitlines() if l.startswith('| ') and l.count('|') >= 3]

    def test_readme_exit_table_pins_exit_5_as_relay_trouble(self):
        """VERIFY8B TP08: a mutant renumbering this row (5 -> 2) is not caught by any test.
        Pin the row's own leading exit number, not just that the word 'relay trouble' occurs
        somewhere in the README."""
        rows = self._readme_table_rows()
        relay_rows = [r for r in rows if 'relay trouble' in r]
        self.assertEqual(len(relay_rows), 1)
        cells = [c.strip() for c in relay_rows[0].split('|')]
        self.assertEqual(cells[1], '5', relay_rows[0])
        five_rows = [r for r in rows if r.split('|')[1].strip() == '5']
        self.assertEqual(len(five_rows), 1, rows)
        self.assertIn('relay trouble', five_rows[0])

    def test_readme_leaves_behind_list_covers_the_exit_5_leftover_request_file(self):
        """VERIFY7B/VERIFY8B N7: the README's 'what it can leave behind' list named archive/ and
        responses/ but omitted that an exit-5 (no answer) firing leaves its own request file in
        requests/ permanently -- crew/relay_ask.py's send() only archives a request it gets an
        answer to inside its own wait; a request that times out is never tidied, even if the
        relay later answers it."""
        readme = (SCHEDULES_DIR / 'README.md').read_text(encoding='utf-8')
        start = readme.index('## Turning one off')
        end = readme.index('## What an empty run costs')
        section = readme[start:end]
        self.assertIn('archive/', section)
        self.assertIn('responses/', section)
        self.assertIn('exit-5', section)
        # The leftover-forever bullet names requests/ and a tick-*.json file specifically, not
        # just the already-documented archive/responses case.
        forever_bullets = [b for b in section.split('- ') if 'for good' in b]
        self.assertEqual(len(forever_bullets), 1, section)
        self.assertIn('requests/', forever_bullets[0])
        self.assertIn('tick-', forever_bullets[0])


if __name__ == '__main__':
    unittest.main()
