"""S0 F1-F5 (task t8dc54e2cb, 2026-10-15): readers that broke when S0's bounded state dropped task_renew rows from
history and clipped the text of quiet tasks (done or cancelled, no event for more than 3 days).

Written before the fix; on 736a57ed:
  F1  both model workers numbered renew request ids by counting task_renew rows in /api/state history. S0 drops those
      rows, so every renew at a fence reused id -1, the store replayed it, and the lease ran out mid-job.
  F2  plans.check_previous_approval_conflicts compared clipped body/acceptance with the plan file: a false conflict.
  F3  plans.room_confirms compared a clipped history detail with the answer: an applied answer was not recognised.
  F4  needs._cited lost a #seq citation past character 200 of a clipped detail: an answered QUESTION came back.
  F5  ollama_worker.owner_named_files read the root's INPUT lines from a clipped body: helper inputs were refused.
Fictional rooms only, on a fake clock; no model, no network.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # lets `python tests/test_s0_f1_fix.py` run from a tree root

import inspect, tempfile, types, unittest

from common_room import agent_worker, board, crew_client, needs, ollama_worker, plans
from common_room.crew_client import CrewClient, CrewError

# Looked up, not imported: on 736a57ed these do not exist yet, and the tests must fail there on behaviour, not on import.
RenewIds = getattr(crew_client, 'RenewIds', lambda: None)
from common_room.protocol import Actor
from common_room.store import Store

DAY = 86400.0


class Clock:
    def __init__(self, t):
        self.now = float(t)

    def __call__(self):
        return self.now


class Room:
    """A fictional handoff room: keeper (owner), one worker, one reviewer."""

    def __init__(self, worker='local-a'):
        self.tmp = tempfile.TemporaryDirectory()
        art = Path(self.tmp.name) / 'art'
        art.mkdir()
        self.people = {a.id: a for a in [Actor('keeper', 'owner'), Actor(worker, 'worker'), Actor('rev', 'reviewer')]}
        self.worker = self.people[worker]
        self.clock = Clock(1792000000.0)
        self.store = Store(Path(self.tmp.name) / 'room.sqlite', self.people, {'fixture': art}, clock=self.clock)
        self.n = 0
        self.call('keeper', 'grant_handoff', {'id': 'orchestra', 'members': ['keeper', worker, 'rev'],
                                             'capabilities': ['handoff.record'], 'root_id': 'fixture',
                                             'scope': 'Fictional F1 room', 'source_namespaces': ['estate', 'room-helper']})
        self.server = board.make_server(self.store, {'tok': self.worker}, clock=self.clock)
        self.sent = []

    def close(self):
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, actor, action, payload, rid=None):
        self.n += 1
        self.clock.now += 1
        return self.store.execute(self.people[actor], action, payload, rid or 'f1-%d' % self.n)

    def task(self, body='Fictional body.', acceptance='Fictional acceptance.', ref='ext-1', namespace='estate',
             title='Fictional task', claim=True):
        msg = {'title': title, 'body': body, 'recipient': self.worker.id, 'authority_id': 'orchestra',
               'kind': 'handoff.record', 'acceptance': acceptance}
        tid = self.call('keeper', 'intake', {'source_namespace': namespace, 'external_reference': ref, 'message': msg})['task']['id']
        self.call('keeper', 'deliver', {'task_id': tid})
        self.call(self.worker.id, 'ack', {'task_id': tid})
        fence = self.call(self.worker.id, 'claim', {'task_id': tid, 'seconds': 1800})['task']['lease']['fence'] if claim else None
        return tid, fence

    def state(self):
        return board.state_body(self.server, self.worker)

    def client(self, flaky=None):
        room = self

        class Client:
            task_in = staticmethod(CrewClient.task_in)

            def state(self):
                return room.state()

            def command(self, action, payload, request_id):
                room.sent.append(request_id)
                if flaky is not None and flaky(action, request_id):
                    raise CrewError('unavailable', 'Local room unavailable (fixture blip).')
                return room.store.execute(room.worker, action, payload, request_id)

            def handoff(self, operation, payload):
                from common_room.handoff_api import handoff_operation
                return handoff_operation(room.server, room.worker, operation, payload)

        return Client()

    def until(self, tid):
        return self.store.task(self.worker, tid)['lease']['until']

    def renew_events(self, tid):
        events, _, _ = self.store._snapshot(self.people['keeper'])
        return [e for e in events if e['task_id'] == tid and e['kind'] == 'task_renew']


def ollama_self(client, actor):
    return types.SimpleNamespace(client=client, actor=actor, timeout=2700, renew_ids=RenewIds(), _log=lambda *a, **k: None)


def agent_self(client, actor):
    return types.SimpleNamespace(client=client, actor=actor, renew_below=agent_worker.RENEW_BELOW, renew_ids=RenewIds(),
                                 _log=lambda *a, **k: None)


class F1RenewTests(unittest.TestCase):
    """Renewals keep extending the lease, as they did when history still carried task_renew rows (d1122c8)."""

    def setUp(self):
        self.room = Room()
        self.addCleanup(self.room.close)

    def drive(self, method, make_self, step, steps):
        tid, fence = self.room.task()
        me = make_self(self.room.client(), self.room.worker.id)
        for _ in range(steps):
            method(me, tid, fence)                      # raises CrewError('lease') once the lease has run out
            self.assertGreater(self.room.until(tid) - self.room.clock.now, 0)
            self.room.clock.now += step
        return tid

    def test_ollama_worker_keeps_its_lease_over_many_renewals(self):
        tid = self.drive(ollama_worker.Worker._ensure_lease, ollama_self, 600, 6)
        self.assertEqual(len(self.room.renew_events(tid)), 6)
        self.assertEqual(len(set(self.room.sent)), 6, 'every renew must carry a fresh request id')

    def test_agent_worker_keeps_its_lease_over_many_renewals(self):
        tid = self.drive(agent_worker.Runner._keep_lease, agent_self, 700, 8)
        renews = self.room.renew_events(tid)
        self.assertGreaterEqual(len(renews), 3)
        self.assertEqual(len(set(self.room.sent)), len(self.room.sent), 'every renew must carry a fresh request id')
        ends = [e['payload']['changes']['lease']['until'] for e in renews]
        self.assertEqual(ends, sorted(ends))
        self.assertEqual(len(set(ends)), len(ends))

    def test_renewals_in_the_same_room_second_still_extend_later(self):
        # the room clamps its clock to the last event, so two renewals can share one timestamp and one lease end;
        # an id keyed to the lease end would then repeat and be replayed for good
        tid, fence = self.room.task()
        me = ollama_self(self.room.client(), self.room.worker.id)
        frozen = self.room.clock.now
        for _ in range(2):
            ollama_worker.Worker._ensure_lease(me, tid, fence)
            self.room.clock.now = frozen
        for _ in range(4):
            self.room.clock.now += 600
            ollama_worker.Worker._ensure_lease(me, tid, fence)
            self.assertEqual(self.room.until(tid), self.room.clock.now + 1800)
        self.assertEqual(len(set(self.room.sent)), 6)

    def test_a_retry_after_a_lost_reply_reuses_its_own_id_and_is_answered_from_the_receipt(self):
        for method, make_self in ((ollama_worker.Worker._ensure_lease, ollama_self), (agent_worker.Runner._keep_lease, agent_self)):
            with self.subTest(worker=method.__qualname__):
                tid, fence = self.room.task(ref='ext-retry-' + method.__name__.strip('_'))
                self.room.clock.now += 1300                   # both workers renew from here
                blips = []
                flaky = lambda action, rid: action == 'renew' and not blips and not blips.append(rid)
                me = make_self(self.room.client(flaky=flaky), self.room.worker.id)
                with self.assertRaises(CrewError):
                    method(me, tid, fence)                    # the reply is lost before the room saw it
                method(me, tid, fence)                        # the worker loop tries again
                self.assertEqual(self.room.sent[-1], blips[0], 'the retry must reuse the lost request id')
                self.assertEqual(len(self.room.renew_events(tid)), 1)
                # and a reply lost AFTER the room applied it: the same id again is answered from the receipt
                before = self.room.until(tid)
                answer = self.room.store.execute(self.room.worker, 'renew', {'task_id': tid, 'fence': fence, 'seconds': 1800},
                                                 blips[0])
                self.assertEqual(answer['task']['lease']['until'], before)
                self.assertEqual(len(self.room.renew_events(tid)), 1)
                self.room.sent.clear()


def quiet(room, tid, actor='rev'):
    """Finish and accept a claimed task, then let it go quiet (no event for more than 3 days)."""
    task = room.store.task(room.worker, tid)
    room.call(room.worker.id, 'finish', {'task_id': tid, 'fence': task['lease']['fence'], 'body': 'Fictional result.',
                                        'refs': [ref(room)]})
    room.call(actor, 'review', {'task_id': tid, 'accepted': True, 'note': 'Fictional review.'})
    room.clock.now += 3 * DAY + 60


def ref(room):
    import hashlib
    content = b'A fictional F1 artifact.\n'
    (Path(room.tmp.name) / 'art' / 'r.txt').write_bytes(content)
    return {'path': 'r.txt', 'sha256': hashlib.sha256(content).hexdigest()}


class F2PlanConflictTests(unittest.TestCase):
    def setUp(self):
        self.room = Room()
        self.addCleanup(self.room.close)

    def plan(self, body, acceptance):
        return {'plan_id': 'plan-f2', 'scope': 'orchestra',
                'tasks': [{'key': 'one', 'title': 'Fictional task', 'body': body, 'acceptance': acceptance, 'to': 'local-a'}]}

    def test_an_unchanged_plan_is_not_a_conflict_when_its_task_is_clipped(self):
        body, acc = 'B' * 900 + ' end', 'A' * 700 + ' end'
        tid, _ = self.room.task(body=body, acceptance=acc, ref='plan-f2-one')
        quiet(self.room, tid)
        state = self.room.state()
        self.assertTrue(next(t for t in state['tasks'] if t['id'] == tid).get('clipped'))
        self.assertEqual(plans.check_previous_approval_conflicts(self.plan(body, acc), state), [])

    def test_an_edited_plan_is_still_a_conflict_when_its_task_is_clipped(self):
        tid, _ = self.room.task(body='B' * 900, acceptance='A' * 700, ref='plan-f2-one')
        quiet(self.room, tid)
        state = self.room.state()
        self.assertEqual(plans.check_previous_approval_conflicts(self.plan('C' * 900, 'A' * 700), state), ['one'])


class F3RoomConfirmsTests(unittest.TestCase):
    def setUp(self):
        self.room = Room()
        self.addCleanup(self.room.close)

    def decided(self):
        tid, fence = self.room.task()
        self.room.call(self.room.worker.id, 'wait_decision', {'task_id': tid, 'fence': fence, 'question': 'Fictional?'})
        return tid

    def event(self, tid, kind):
        return next(h for h in self.room.state()['history'] if h['task_id'] == tid and h['kind'] == kind)

    def test_a_long_resolve_answer_is_still_confirmed_once_its_task_is_quiet(self):
        tid = self.decided()
        answer = 'R' * 900 + ' fictional answer'
        self.room.call('keeper', 'resolve_decision', {'task_id': tid, 'body': answer})
        seq = self.event(tid, 'task_resolve_decision')['seq']
        self.room.call(self.room.worker.id, 'ack', {'task_id': tid})
        self.room.call(self.room.worker.id, 'claim', {'task_id': tid, 'seconds': 1800})
        quiet(self.room, tid)
        earlier = {'seq': seq, 'task': tid, 'action': 'resolve'}
        self.assertTrue(plans.room_confirms(self.room.state(), earlier, answer))
        self.assertFalse(plans.room_confirms(self.room.state(), earlier, 'S' * 900))

    def test_a_long_reinstate_reason_is_still_confirmed_once_its_task_is_quiet(self):
        tid, fence = self.room.task()
        for _ in range(2):
            self.room.call(self.room.worker.id, 'fail', {'task_id': tid, 'fence': fence, 'body': 'Fictional failure.'})
            if self.room.store.task(self.room.worker, tid)['status'] == 'acknowledged':
                fence = self.room.call(self.room.worker.id, 'claim', {'task_id': tid, 'seconds': 1800})['task']['lease']['fence']
        reason = 'Q' * 900 + ' fictional reason'
        self.room.call('keeper', 'reinstate', {'task_id': tid, 'body': reason})
        seq = self.event(tid, 'task_reinstate')['seq']
        self.room.call(self.room.worker.id, 'ack', {'task_id': tid})
        self.room.call(self.room.worker.id, 'claim', {'task_id': tid, 'seconds': 1800})
        quiet(self.room, tid)
        earlier = {'seq': seq, 'task': tid, 'action': 'reinstate'}
        self.assertTrue(plans.room_confirms(self.room.state(), earlier, reason))
        self.assertFalse(plans.room_confirms(self.room.state(), earlier, 'P' * 900))


class F4NeedsCitationTests(unittest.TestCase):
    def setUp(self):
        self.room = Room()
        self.addCleanup(self.room.close)

    def test_a_question_answered_past_character_200_stays_answered_once_the_task_is_quiet(self):
        self.room.call(self.room.worker.id, 'note', {'authority_id': 'orchestra', 'body': 'QUESTION keeper: is this fictional?'})
        qseq = max(h['seq'] for h in self.room.state()['notes'])
        self.room.call(self.room.worker.id, 'note', {'authority_id': 'orchestra', 'body': 'QUESTION keeper: an unanswered control?'})
        control = max(h['seq'] for h in self.room.state()['notes'])
        tid, fence = self.room.task()
        self.room.call(self.room.worker.id, 'wait_decision', {'task_id': tid, 'fence': fence, 'question': 'Fictional?'})
        self.room.call('keeper', 'resolve_decision', {'task_id': tid, 'body': 'W' * 300 + ' answers #%d' % qseq})
        self.room.call(self.room.worker.id, 'ack', {'task_id': tid})
        self.room.call(self.room.worker.id, 'claim', {'task_id': tid, 'seconds': 1800})
        quiet(self.room, tid)
        state = self.room.state()
        ids = [item['id'] for item in state['needs']]
        self.assertIn('question:%d' % control, ids, 'the uncited control question must still be listed')
        self.assertNotIn('question:%d' % qseq, ids)
        crew_items, _ = __import__('common_room.crew', fromlist=['cmd_needs']).cmd_needs(None, state, None)
        self.assertNotIn('question:%d' % qseq, [i['id'] for i in crew_items])
        self.assertNotIn('question:%d' % qseq, [i['id'] for i in getattr(needs, 'room_needs', needs.needs_shane)(state)])


class F5HelperInputsTests(unittest.TestCase):
    def test_owner_input_lines_past_character_300_still_grant_files_when_the_root_is_clipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root_dir = Path(tmp) / 'r'
            root_dir.mkdir()
            (root_dir / 'late.txt').write_text('late\n', encoding='utf-8')
            roots = [root_dir]
            body = 'Fictional root. ' + 'x' * 400 + '\nINPUT: r/late.txt\n'
            full = {'id': 'troot', 'body': body, 'intake': {'source_namespace': 'estate'}}
            clipped = dict(full, body=body[:300], clipped=True)
            helper = {'id': 'thelp', 'parent_id': 'troot', 'intake': {'source_namespace': 'room-helper'}, 'body': 'help'}
            tasks = {'troot': clipped, 'thelp': helper}
            want = ollama_worker.owner_named_files(helper, {'troot': full, 'thelp': helper}, roots)[0][0]
            self.assertTrue(want, 'the fixture must name a file through the full root body')
            takes_fetch = 'fetch' in inspect.signature(ollama_worker.owner_named_files).parameters
            got, why = ollama_worker.owner_named_files(helper, tasks, roots, **({'fetch': lambda tid: full} if takes_fetch else {}))
            self.assertIsNone(why)
            self.assertEqual(got[0], want)


if __name__ == '__main__':
    unittest.main(argv=sys.argv[:1])    # evidence.py passes the tree root as an argument; it names no test
