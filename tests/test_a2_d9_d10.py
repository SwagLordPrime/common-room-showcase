"""A2 D9 + D10 (task tdfb5fc501, 2026-10-24; Shane's decisions of 2026-10-19).

D9 "Yes, retire it": local-b stops starting and gets no key. Shown by reading the launcher, never by running it.
D10 "Yes, undoable only": dot (Rune) may accept or reject undoable work only. Its power comes from a review stand-in
the room is given at start (crew/review-standins.json for the live room); the store enforces it on every review:
  - only a task marked undoable when it was created; an unmarked task (every pre-switch task) or a can't-undo task
    refuses dot;
  - only work one of the stand-in's builders built (Claude seats), never dot's own build;
  - the mark is set at creation only: by the owner at intake, or by a marker the stand-in names (claude-room) when it
    asks for a helper; never by the task's builder, never by dot, and no later command changes it;
  - dot stays a worker; no other worker gains review rights.
Rollback: 356bbac's store, run on a store this code wrote (a marked task and a stand-in review included), still
starts and gives dot no review power. Fictional rooms in .checks only; keys made here; no keeper vault.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # lets `python tests/test_a2_d9_d10.py` run from a tree root

import inspect, json, re, subprocess, types, unittest

from common_room import activation
from common_room.handoff_api import handoff_operation
from common_room.protocol import RoomError
from common_room.store import FIELDS, OPTIONAL_FIELDS
from tests.helpers import case_dir

TREE = Path(__file__).resolve().parents[1]
CONFIG = {'version': 1, 'actors': [
    {'id': 'cc', 'role': 'worker', 'label': 'Fictional Claude builder', 'session': 'fixture-cc'},
    {'id': 'room', 'role': 'worker', 'label': 'Fictional orchestrator', 'session': 'fixture-room'},
    {'id': 'dot', 'role': 'worker', 'label': 'Fictional dot', 'session': 'fixture-dot'},
    {'id': 'oa', 'role': 'worker', 'label': 'Fictional OpenAI builder', 'session': 'fixture-oa'},
    {'id': 'w2', 'role': 'worker', 'label': 'Fictional other worker', 'session': 'fixture-w2'},
    {'id': 'audit', 'role': 'reviewer', 'label': 'Fictional reviewer', 'session': 'fixture-audit'}],
    'authorities': [{'id': 'orch', 'members': ['keeper', 'cc', 'room', 'dot', 'oa', 'w2', 'audit'],
                     'scope': 'Fictional orchestra scope'}]}
STANDIN = {'actor': 'dot', 'stands_for': 'audit', 'authority': 'orch', 'builders': ['cc', 'room'], 'markers': ['room']}
SEATS = ['cc', 'room', 'dot', 'oa', 'w2', 'audit']


def with_standins(standins):
    """Looked up, not assumed: on 356bbac start_room has no stand-ins, and these tests must fail there on behaviour."""
    takes = 'review_standins' in inspect.signature(activation.start_room).parameters
    return {'review_standins': list(standins)} if takes else {}


class Room(unittest.TestCase):
    standins = (STANDIN,)

    def setUp(self):
        self.root = case_dir('a2-d10')
        self.room = activation.start_room(self.root, enrollment=CONFIG, selected=SEATS, **with_standins(self.standins))
        self.addCleanup(self.room.close)
        self.store = self.room.store
        self.n = 0

    def who(self, actor):
        return self.store.people[actor]

    def run_(self, actor, action, payload):
        self.n += 1
        return self.store.execute(self.who(actor), action, payload, 'a2-%d' % self.n)

    def intake(self, to='cc', mark=None, ref=None):
        self.n += 1
        payload = {'source_namespace': 'estate', 'external_reference': ref or 'ref-%d' % self.n,
                   'message': {'title': 'Fictional task', 'body': 'Do the fictional thing.', 'recipient': to,
                               'authority_id': 'orch', 'kind': 'handoff.record', 'acceptance': 'A fictional check.'}}
        if mark is not None:
            payload['undoable'] = mark
        tid = self.run_('keeper', 'intake', payload)['task']['id']
        self.run_('keeper', 'deliver', {'task_id': tid})
        return tid

    def build(self, tid, builder=None):
        builder = builder or self.store.task(self.who('keeper'), tid)['recipient']
        self.run_(builder, 'ack', {'task_id': tid})
        fence = self.run_(builder, 'claim', {'task_id': tid, 'seconds': 1800})['task']['lease']['fence']
        ref = handoff_operation(self.room.server, self.who(builder), 'stage_text', {'task_id': tid, 'fence': fence, 'text': 'Fictional result.'})
        self.run_(builder, 'finish', {'task_id': tid, 'fence': fence, 'body': 'Done.', 'refs': [{'path': ref['path'], 'sha256': ref['sha256']}]})
        return tid

    def review(self, actor, tid, accepted=True, **extra):
        return self.run_(actor, 'review', dict({'task_id': tid, 'accepted': accepted, 'note': 'A fictional review.'}, **extra))

    def refused(self, code, fn):
        with self.assertRaises(RoomError) as caught:
            fn()
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        return str(caught.exception)


class D9LauncherTests(unittest.TestCase):
    """Read the launcher; never run it (it stops every CRCREW window and writes the keeper key)."""

    def setUp(self):
        self.text = (TREE / 'Start Common Room Crew.cmd').read_text(encoding='utf-8')

    def test_local_b_gets_no_key(self):
        seats = re.search(r'^set "SEATS=([^"]*)"', self.text, re.M).group(1).split()
        self.assertNotIn('local-b', seats)
        self.assertIn('local-a', seats)

    def test_local_b_gets_no_window(self):
        starts = [l for l in self.text.splitlines() if l.lstrip().lower().startswith('start ')]
        self.assertFalse([l for l in starts if 'local-b' in l])
        self.assertTrue([l for l in starts if '"CRCREW local-a"' in l])

    def test_local_b_stays_enrolled_so_the_change_is_one_line_to_undo(self):
        shipped = json.loads((TREE / 'crew' / 'enrollment-crew-2026-09-24.json').read_text(encoding='utf-8'))
        self.assertIn('local-b', {a['id'] for a in shipped['actors']})


class D10ReviewTests(Room):
    def test_dot_accepts_an_undoable_task_a_claude_seat_built(self):
        tid = self.build(self.intake(mark=True))
        task = self.review('dot', tid)['task']
        self.assertEqual((task['status'], task['reviewer'], task['standin_for']), ('done', 'dot', 'audit'))

    def test_dot_rejects_an_undoable_task_and_the_strike_rules_hold(self):
        tid = self.build(self.intake(mark=True))
        task = self.review('dot', tid, accepted=False)['task']
        self.assertEqual((task['status'], task['failed_attempts'], task['standin_for']), ('waiting_decision', 1, 'audit'))
        tid2 = self.build(self.intake(mark=True))
        task = self.review('dot', tid2, accepted=False, kind='paperwork')['task']
        self.assertEqual((task['status'], task['failed_attempts'], task['paperwork_returns']), ('waiting_decision', 0, 1))

    def test_dot_is_refused_on_an_unmarked_task(self):
        tid = self.build(self.intake())
        self.assertIn('unmarked', self.refused('forbidden', lambda: self.review('dot', tid)))
        self.assertEqual(self.review('audit', tid)['task']['status'], 'done')   # the reviewer seat still can

    def test_dot_is_refused_on_a_cant_undo_task(self):
        tid = self.build(self.intake(mark=False))
        self.assertIn("can't-undo", self.refused('forbidden', lambda: self.review('dot', tid)))

    def test_dot_is_refused_on_its_own_build(self):
        tid = self.build(self.intake(to='dot', mark=True))
        self.refused('self_review', lambda: self.review('dot', tid))

    def test_dot_is_refused_on_work_a_seat_off_its_builders_list_built(self):
        tid = self.build(self.intake(to='oa', mark=True))
        self.assertIn('only on work built by', self.refused('forbidden', lambda: self.review('dot', tid)))

    def test_other_workers_still_cannot_review(self):
        tid = self.build(self.intake(mark=True))
        for actor in ('room', 'w2', 'oa'):
            with self.subTest(actor=actor):
                self.refused('forbidden', lambda: self.review(actor, tid))
        self.refused('self_review', lambda: self.review('cc', tid))

    def test_every_seat_can_see_the_grant_in_its_state(self):
        from common_room.board import state_body
        for seat in ('dot', 'cc', 'audit'):
            with self.subTest(seat=seat):
                scope = next(a for a in state_body(self.room.server, self.who(seat))['authorities'] if a['id'] == 'orch')
                self.assertEqual(scope.get('review_standins'), [STANDIN])

    def test_dot_still_works_as_a_worker(self):
        tid = self.build(self.intake(to='dot'))
        self.assertEqual(self.store.task(self.who('keeper'), tid)['status'], 'ready_for_review')
        self.assertEqual(self.review('audit', tid)['task']['status'], 'done')


class D10MarkTests(Room):
    def helper_parent(self, holder):
        tid = self.intake(to=holder)
        self.run_(holder, 'ack', {'task_id': tid})
        fence = self.run_(holder, 'claim', {'task_id': tid, 'seconds': 1800})['task']['lease']['fence']
        return tid, fence

    def help_(self, holder, tid, fence, to, mark):
        payload = {'task_id': tid, 'fence': fence, 'recipient': to, 'title': 'Fictional helper', 'body': 'Help.',
                   'acceptance': 'A fictional check.', 'undoable': mark}
        return self.run_(holder, 'request_help', payload)

    def test_the_owner_marks_at_intake(self):
        self.assertIs(self.store.task(self.who('keeper'), self.intake(mark=True))['undoable'], True)
        self.assertIs(self.store.task(self.who('keeper'), self.intake(mark=False))['undoable'], False)
        self.assertNotIn('undoable', self.store.task(self.who('keeper'), self.intake()))

    def test_the_named_marker_marks_a_helper_it_asks_for(self):
        tid, fence = self.helper_parent('room')
        child = self.help_('room', tid, fence, 'cc', True)['task']
        self.assertIs(child['undoable'], True)
        self.build(child['id'])
        self.assertEqual(self.review('dot', child['id'])['task']['standin_for'], 'audit')

    def test_neither_the_builder_nor_dot_can_set_a_mark(self):
        for holder in ('cc', 'dot'):
            with self.subTest(holder=holder):
                tid, fence = self.helper_parent(holder)
                self.assertIn('marker', self.refused('forbidden', lambda: self.help_(holder, tid, fence, 'w2', True)))
        self.refused('forbidden', lambda: self.run_('dot', 'intake', {'source_namespace': 'estate', 'external_reference': 'x',
            'message': {'title': 'T', 'body': 'B', 'recipient': 'cc', 'authority_id': 'orch', 'kind': 'handoff.record',
                        'acceptance': 'A'}, 'undoable': True}))

    def test_no_later_command_can_set_or_change_a_mark(self):
        self.assertEqual({a for a, f in OPTIONAL_FIELDS.items() if 'undoable' in f}, {'intake', 'request_help'})
        tid = self.intake(mark=False)
        self.run_('cc', 'ack', {'task_id': tid})
        lease = self.run_('cc', 'claim', {'task_id': tid, 'seconds': 1800})['task']['lease']
        for action, payload in (('renew', {'task_id': tid, 'fence': lease['fence'], 'seconds': 600}),
                                ('wait_decision', {'task_id': tid, 'fence': lease['fence'], 'question': 'Q?'}),
                                ('note', {'authority_id': 'orch', 'body': 'n'})):
            with self.subTest(action=action):
                self.refused('invalid', lambda: self.run_('cc', action, dict(payload, undoable=True)))
        self.refused('invalid', lambda: self.run_('keeper', 'resolve_decision', {'task_id': tid, 'body': 'b', 'undoable': True}))
        self.refused('invalid', lambda: self.run_('keeper', 'reinstate', {'task_id': tid, 'body': 'b', 'undoable': True}))
        self.assertIs(self.store.task(self.who('keeper'), tid)['undoable'], False)
        self.assertTrue(set(FIELDS) - {'intake', 'request_help'})

    def test_a_repeat_intake_with_another_mark_is_a_different_brief(self):
        self.intake(mark=False, ref='same')
        self.refused('idempotency', lambda: self.intake(mark=True, ref='same'))


class D10RelayTests(Room):
    def test_dot_reviews_through_its_own_relay_folder(self):
        from common_room.relay import Relay
        good = self.build(self.intake(mark=True))
        unmarked = self.build(self.intake())
        relay = Relay(self.room.root, ['dot'], clock=self.room.server.clock)
        requests = self.room.root / 'relay' / 'dot' / 'requests'
        for rid, tid in (('rev-good', good), ('rev-unmarked', unmarked)):
            (requests / (rid + '.json')).write_text(json.dumps({'request_id': rid, 'kind': 'command', 'action': 'review',
                'payload': {'task_id': tid, 'accepted': True, 'note': 'A fictional review.'}}), encoding='utf-8')
        relay.process_once()
        answer = lambda rid: json.loads((self.room.root / 'relay' / 'dot' / 'responses' / ('resp-' + rid + '.json')).read_text(encoding='utf-8'))
        ok = answer('rev-good')
        self.assertTrue(ok['ok'], ok)
        self.assertEqual((ok['result']['task']['status'], ok['result']['task']['standin_for']), ('done', 'audit'))
        no = answer('rev-unmarked')
        self.assertFalse(no['ok'])
        self.assertEqual(no.get('error'), 'forbidden')


class StandinConfigTests(unittest.TestCase):
    def people(self, config=CONFIG):
        from common_room.protocol import Actor
        return dict({'keeper': Actor('keeper', 'owner')}, **{a['id']: Actor(a['id'], a['role']) for a in config['actors']})

    def test_bad_stand_ins_are_refused_at_start(self):
        bad = [dict(STANDIN, actor='audit'), dict(STANDIN, stands_for='cc'), dict(STANDIN, builders=['dot']),
               dict(STANDIN, markers=['dot']), dict(STANDIN, authority='nowhere'), dict(STANDIN, builders=[]),
               dict(STANDIN, extra=1)]
        for entry in bad:
            with self.subTest(entry=entry):
                with self.assertRaises(ValueError):
                    activation.check_standins([entry], CONFIG, self.people())
        with self.assertRaises(ValueError):
            activation.check_standins([STANDIN, STANDIN], CONFIG, self.people())

    def test_the_shipped_file_names_dot_for_astra_audit_on_claude_built_work(self):
        entries = activation.load_standins(TREE / 'crew' / 'review-standins.json')
        shipped = activation.validate_enrollment(json.loads((TREE / 'crew' / 'enrollment-crew-2026-09-24.json').read_text(encoding='utf-8')))
        people = self.people(shipped)
        checked = activation.check_standins(entries, shipped, people)
        self.assertEqual([(e['actor'], e['stands_for'], e['authority']) for e in checked], [('dot', 'astra-audit', 'orchestra')])
        self.assertEqual(sorted(checked[0]['builders']), ['claude-code', 'claude-code-b', 'claude-room', 'ferrule'])
        self.assertEqual(checked[0]['markers'], ['claude-room'])

    def test_a_missing_file_means_no_stand_in(self):
        self.assertEqual(activation.load_standins(case_dir('a2-none') / 'review-standins.json'), [])


class PreSwitchEnrollmentTests(unittest.TestCase):
    def test_the_new_code_starts_on_a_pre_switch_enrollment_and_dot_has_power_only_with_the_stand_in(self):
        root = case_dir('a2-preswitch')
        activation.start_room(root, enrollment=CONFIG, selected=SEATS).close()   # writes enrollment.json, as 356bbac would
        written = json.loads((root / 'enrollment.json').read_text(encoding='utf-8'))
        self.assertNotIn('review_standins', written)                           # the enrollment schema is unchanged
        for standins, power in (((), False), ((STANDIN,), True)):
            room = activation.start_room(root, None, SEATS, **with_standins(standins))
            try:
                self.assertEqual(bool(getattr(room.store, 'standins', None)), power)
            finally:
                room.close()


class RollbackStoreTests(Room):
    def test_356bbacs_store_opens_a_store_this_code_wrote_and_gives_dot_no_review_power(self):
        done = self.build(self.intake(mark=True))
        self.review('dot', done)                                   # a stand-in review is in the log
        pending = self.build(self.intake(mark=True))               # a marked task waits for review
        shown = subprocess.run(['git', '-C', str(TREE), 'show', '356bbac:common_room/store.py'], capture_output=True, text=True,
                               encoding='utf-8')
        if shown.returncode != 0:
            self.skipTest('356bbac is not reachable through git here')
        old = types.ModuleType('common_room._store_356bbac')
        old.__package__ = 'common_room'
        exec(compile(shown.stdout, '356bbac:common_room/store.py', 'exec'), old.__dict__)
        self.room.close()
        people = dict(self.store.people)
        roots = dict(self.store.roots)
        store = old.Store(self.root / 'room.sqlite', people, roots)
        keeper = people['keeper']
        self.assertEqual(store.task(keeper, done)['status'], 'done')
        self.assertIs(store.task(keeper, pending)['undoable'], True)
        with self.assertRaises(RoomError) as caught:
            store.execute(people['dot'], 'review', {'task_id': pending, 'accepted': True, 'note': 'n'}, 'old-dot-review')
        self.assertEqual(caught.exception.code, 'forbidden')
        self.assertEqual(store.execute(people['audit'], 'review', {'task_id': pending, 'accepted': True, 'note': 'n'},
                                       'old-audit-review')['task']['status'], 'done')


if __name__ == '__main__':
    unittest.main(argv=sys.argv[:1])    # evidence.py passes the tree root as an argument; it names no test
