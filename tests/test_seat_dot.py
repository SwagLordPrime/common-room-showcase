"""seat-dot (plan turnons-2026-10-03, job 7; claude-room ruling #1136, approved by Shane 2026-10-05).

Shane's ChatGPT dot joins as seat `dot`, a worker in the `orchestra` scope. Scopes are immutable except for one
thing: an existing scope may GAIN members, appended at room start from the shipped enrollment, never changed,
reordered or removed. Nothing a seat sends can reach that path. The dot has worker rights and no keeper action,
and a STOP note from the dot is not a stop."""
import json, sys, types, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))      # lets `python tests/test_seat_dot.py` run from a tree root (tools/evidence.py does)

from tests.helpers import case_dir
from common_room.crew_client import CrewClient, CrewError, new_request_id
from common_room.protocol import RoomError, canonical

FULL = json.loads((ROOT / 'crew' / 'enrollment-crew-2026-09-24.json').read_text(encoding='utf-8'))
LATER = ('notify', 'duet')    # seats appended after dot by later jobs (notify, 2026-10-05; duet, 2026-10-30)


def as_of_seat_dot(enrollment):
    """The shipped file as seat-dot left it: later seats taken out again (they are appended after dot)."""
    return dict(enrollment, actors=[a for a in enrollment['actors'] if a['id'] not in LATER],
                authorities=[dict(a, members=[m for m in a['members'] if m not in LATER]) for a in enrollment['authorities']])


SHIPPED = as_of_seat_dot(FULL)
SEATS = ['claude-room', 'ferrule', 'tinker', 'vesper', 'crew-audit', 'hermes', 'local-a', 'local-b',
         'astra-room', 'astra-audit', 'claude-code', 'claude-code-b']
LIVE_ORCHESTRA = ['keeper', 'claude-room', 'astra-room', 'ferrule', 'claude-code', 'claude-code-b', 'hermes',
                  'local-a', 'local-b', 'tinker', 'vesper', 'crew-audit', 'astra-audit']


def without_dot(enrollment):
    """The enrollment as Shane's live room has it today (claude-room VERIFIED, #1136): no dot anywhere."""
    return dict(enrollment, actors=[a for a in enrollment['actors'] if a['id'] != 'dot'],
                authorities=[dict(a, members=[m for m in a['members'] if m != 'dot']) for a in enrollment['authorities']])


def dot_enrolled_nowhere(enrollment):
    """The dot as a registered seat that belongs to no scope yet (so the store can be asked to append it)."""
    return dict(without_dot(enrollment), actors=enrollment['actors'])


def orchestra(enrollment):
    return next(a for a in enrollment['authorities'] if a['id'] == 'orchestra')


def start(root, enrollment, seats):
    from common_room.activation import start_room
    return start_room(root, enrollment=enrollment, selected=seats)


class ShippedEnrollmentTests(unittest.TestCase):
    def test_dot_is_a_worker_appended_to_orchestra_and_nothing_else_changed(self):
        dot = [a for a in SHIPPED['actors'] if a['id'] == 'dot']
        self.assertEqual(len(dot), 1)
        self.assertEqual(dot[0]['role'], 'worker')
        self.assertEqual(SHIPPED['actors'][-1]['id'], 'dot')                       # appended last
        self.assertEqual(orchestra(SHIPPED)['members'], LIVE_ORCHESTRA + ['dot'])  # appended last, order kept
        self.assertEqual([a['id'] for a in SHIPPED['authorities'] if 'dot' in a['members']], ['orchestra'])

    def test_the_rest_is_byte_for_byte_the_live_file(self):
        # claude-room VERIFIED (#1136) the live enrollment.json is byte-identical to the 09-24 shipped file, sha256
        # prefix 4d6a7443ff40e8cb. Taking dot back out must give exactly those bytes (compact JSON, as shipped).
        import hashlib
        raw = json.dumps(without_dot(SHIPPED), separators=(',', ':'), ensure_ascii=False).encode('utf-8')
        self.assertTrue(hashlib.sha256(raw).hexdigest().startswith('4d6a7443ff40e8cb'))

    def test_the_shipped_file_passes_the_rooms_own_checks(self):
        from common_room.activation import merge_enrollment, validate_enrollment
        validate_enrollment(SHIPPED)                                          # sizes, ids, 16 KiB cap
        self.assertEqual(merge_enrollment(without_dot(SHIPPED), SHIPPED), json.loads(canonical(SHIPPED)))


class MergeRuleTests(unittest.TestCase):
    """Members may only grow: same scope text, old members first, in order. Everything else is still refused."""
    def setUp(self):
        from common_room.activation import merge_enrollment
        self.merge = merge_enrollment
        self.old = without_dot(SHIPPED)

    def changed(self, **row):
        new = json.loads(json.dumps(SHIPPED))
        orchestra(new).update(row)
        return new

    def refused(self, new):
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.merge(self.old, new)

    def test_appended_members_are_accepted(self):
        self.merge(self.old, SHIPPED)

    def test_a_member_put_first_or_in_the_middle_is_refused(self):
        self.refused(self.changed(members=['dot'] + LIVE_ORCHESTRA))
        self.refused(self.changed(members=LIVE_ORCHESTRA[:3] + ['dot'] + LIVE_ORCHESTRA[3:]))

    def test_reordering_or_removing_a_member_is_refused(self):
        self.refused(self.changed(members=list(reversed(LIVE_ORCHESTRA)) + ['dot']))
        self.refused(self.changed(members=[m for m in LIVE_ORCHESTRA if m != 'ferrule'] + ['dot']))
        self.refused(self.changed(members=[m for m in LIVE_ORCHESTRA if m != 'ferrule']))

    def test_a_changed_scope_text_is_refused_even_with_appended_members(self):
        self.refused(self.changed(scope=orchestra(SHIPPED)['scope'] + ' More.'))

    def test_dropping_a_scope_or_a_seat_is_still_refused(self):
        new = dict(SHIPPED, authorities=[a for a in SHIPPED['authorities'] if a['id'] != 'gym-build'])
        self.refused(new)
        old = dot_enrolled_nowhere(SHIPPED)                  # the dot is in no scope here, so dropping it is valid JSON
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.merge(old, without_dot(SHIPPED))

    def test_a_changed_seat_is_still_refused(self):
        new = json.loads(json.dumps(SHIPPED))
        next(a for a in new['actors'] if a['id'] == 'ferrule')['role'] = 'reviewer'
        self.refused(new)

    def test_the_same_record_still_merges(self):
        self.merge(SHIPPED, SHIPPED)


class RunningRoomGainsTheSeatTests(unittest.TestCase):
    """The path at the next start: the room as it stands (no dot), then a start with the shipped enrollment."""
    def setUp(self):
        self.root = case_dir('seat-dot-room')
        before = start(self.root, without_dot(SHIPPED), SEATS)
        keeper = before.store.people['keeper']
        msg = {'title': 'Old workstream', 'body': 'Posted before the dot joined.', 'acceptance': 'Reviewed.',
               'recipient': 'claude-room', 'authority_id': 'orchestra', 'kind': 'handoff.record'}
        self.old_task = before.store.execute(keeper, 'intake', {'source_namespace': 'estate', 'external_reference': 'old-1',
                                                                'message': msg}, 'intake-old-1')['task']['id']
        before.store.execute(keeper, 'deliver', {'task_id': self.old_task}, 'deliver-old-1')
        CrewClient(before.root / 'claude-room-credential.json').command(
            'note', {'authority_id': 'orchestra', 'body': 'A note from before the dot.'}, 'note-old-1')
        self.events_before = self.events(before)
        before.close()
        self.room = start(self.root, SHIPPED, SEATS + ['dot'])
        self.addCleanup(self.room.close)

    @staticmethod
    def events(room):
        from contextlib import closing
        from common_room.store import read_events
        with closing(room.store._connect()) as conn:
            return read_events(conn)

    def test_orchestra_gains_dot_by_one_appended_event(self):
        grants = self.room.store._snapshot(self.room.store.people['keeper'])[1]
        self.assertEqual(grants['orchestra']['members'], LIVE_ORCHESTRA + ['dot'])
        after = self.events(self.room)
        self.assertEqual(after[:len(self.events_before)], self.events_before)       # history untouched
        new = after[len(self.events_before):]
        self.assertEqual([(e['kind'], e['actor'], e['payload']) for e in new],
                         [('authority_members_added', 'keeper', {'authority_id': 'orchestra', 'members': ['dot']})])
        for other in ('crew-coordination', 'local-crew', 'activation'):
            self.assertNotIn('dot', grants[other]['members'])
        saved = json.loads((self.root / 'enrollment.json').read_text(encoding='utf-8'))
        self.assertEqual(orchestra(saved)['members'], LIVE_ORCHESTRA + ['dot'])

    def test_a_restart_adds_nothing_more(self):
        count = len(self.events(self.room))
        self.room.close()
        again = start(self.root, SHIPPED, SEATS + ['dot'])
        self.addCleanup(again.close)
        self.assertEqual(len(self.events(again)), count)
        again.close()
        plain = start(self.root, None, SEATS + ['dot'])                  # later starts read the saved enrollment.json
        self.addCleanup(plain.close)
        self.assertEqual(len(self.events(plain)), count)

    def test_dot_gets_a_key_and_sees_the_orchestra_work_from_before(self):
        dot = CrewClient(self.room.root / 'dot-credential.json')
        state = dot.state()
        self.assertEqual(state['actor']['id'], 'dot')
        self.assertIn(self.old_task, [t['id'] for t in state['tasks']])
        self.assertIn('A note from before the dot.', [n['body'] for n in state['notes']])

    def test_dot_can_be_given_a_helper_and_work_it(self):
        keeper = self.room.store.people['keeper']
        orch = CrewClient(self.room.root / 'claude-room-credential.json')
        orch.command('ack', {'task_id': self.old_task}, 'ack-old-1')
        fence = orch.command('claim', {'task_id': self.old_task, 'seconds': 1800}, 'claim-old-1')['task']['fence']
        helper = orch.command('request_help', {'task_id': self.old_task, 'fence': fence, 'recipient': 'dot',
                                               'title': 'Watch the plan', 'body': 'One-line synopsis.\nDetails.',
                                               'acceptance': 'Notes posted.'}, 'help-dot-1')['task']
        dot = CrewClient(self.room.root / 'dot-credential.json')
        dot.command('ack', {'task_id': helper['id']}, 'dot-ack-1')
        claimed = dot.command('claim', {'task_id': helper['id'], 'seconds': 1800}, 'dot-claim-1')['task']
        self.assertEqual((claimed['status'], claimed['lease']['holder']), ('claimed', 'dot'))
        dot.command('note', {'authority_id': 'orchestra', 'body': 'WATCHDOG: nothing stuck.'}, 'dot-note-1')
        grand = dot.command('request_help', {'task_id': helper['id'], 'fence': claimed['fence'], 'recipient': 'ferrule',
                                             'title': 'Piece', 'body': 'Do it.', 'acceptance': 'Done.'}, 'dot-help-1')['task']
        self.assertEqual((grand['recipient'], grand['sender']), ('ferrule', 'dot'))
        with self.assertRaises(CrewError):                       # the dot never reviews: it is a worker
            dot.command('review', {'task_id': helper['id'], 'accepted': True, 'note': 'Fine.'}, 'dot-review-1')
        self.assertIsNotNone(keeper)

    def test_dot_has_no_keeper_action(self):
        dot = CrewClient(self.room.root / 'dot-credential.json')
        msg = {'title': 'x', 'body': 'x', 'acceptance': 'x', 'recipient': 'dot', 'authority_id': 'orchestra',
               'kind': 'handoff.record'}
        refused = {
            'intake': {'source_namespace': 'estate', 'external_reference': 'dot-root', 'message': msg},
            'deliver': {'task_id': self.old_task}, 'assign': {'task_id': self.old_task, 'recipient': 'dot'},
            'reinstate': {'task_id': self.old_task, 'body': 'x'}, 'resolve_decision': {'task_id': self.old_task, 'body': 'x'},
            'cancel': {'task_id': self.old_task, 'body': 'x'},
            'pause': {'authority_id': 'orchestra'}, 'resume': {'authority_id': 'orchestra'},
            'revoke': {'authority_id': 'orchestra'}, 'stop_all': {'reason': 'x'}, 'resume_all': {},
            'grant_handoff': {'id': 'dot-own', 'members': ['keeper', 'dot'], 'capabilities': ['handoff.record'],
                              'root_id': 'orchestra', 'scope': 'x', 'source_namespaces': ['estate']},
        }
        for action, payload in refused.items():
            with self.subTest(action=action):
                with self.assertRaises(CrewError) as raised:
                    dot.command(action, payload, new_request_id('dot'))
                # the store refuses (forbidden/state); a grant never reaches it: the room's API answers 'error',
                # 'Unsupported participant operation.' (MEASURED 2026-10-05)
                self.assertIn(raised.exception.code, ('forbidden', 'state', 'error'))
        grants = self.room.store._snapshot(self.room.store.people['keeper'])[1]
        self.assertFalse(grants['orchestra']['paused'])
        self.assertNotIn('dot-own', grants)
        self.assertFalse((self.room.root / 'STOP').exists())

    def test_no_seat_command_reaches_the_member_add(self):
        dot = CrewClient(self.room.root / 'dot-credential.json')
        for action in ('add_members', 'authority_members_added'):
            with self.subTest(action=action):
                with self.assertRaises(CrewError):
                    dot.command(action, {'authority_id': 'local-crew', 'members': ['dot']}, new_request_id('dot'))
        keeper = self.room.store.people['keeper']
        with self.assertRaisesRegex(RoomError, 'Unsupported command'):   # not even the keeper, through a command
            self.room.store.execute(keeper, 'add_members', {'authority_id': 'local-crew', 'members': ['dot']}, 'k-add-1')
        worker = self.room.store.people['ferrule']
        with self.assertRaisesRegex(RoomError, 'owner'):
            self.room.store.add_members(worker, 'local-crew', ['dot'], 'w-add-1')


class StoreAddMembersTests(unittest.TestCase):
    def setUp(self):
        self.room = start(case_dir('seat-dot-store'), SHIPPED, SEATS + ['dot'])
        self.addCleanup(self.room.close)
        self.store = self.room.store
        self.keeper = self.store.people['keeper']

    def members(self, aid):
        return self.store._snapshot(self.keeper)[1][aid]['members']

    def test_append_then_repeat_from_the_receipt(self):
        before = list(self.members('local-crew'))
        first = self.store.add_members(self.keeper, 'local-crew', ['dot'], 'add-lc-1')
        self.assertEqual(self.members('local-crew'), before + ['dot'])
        self.assertEqual(self.store.add_members(self.keeper, 'local-crew', ['dot'], 'add-lc-1'), first)
        self.assertEqual(self.members('local-crew'), before + ['dot'])

    def test_refusals(self):
        cases = {
            'already a member': ('orchestra', ['dot']), 'the keeper': ('local-crew', ['keeper']),
            'unregistered': ('local-crew', ['nobody']), 'duplicate': ('local-crew', ['dot', 'dot']),
            'empty': ('local-crew', []), 'not a list': ('local-crew', 'dot'), 'no such scope': ('nowhere', ['dot']),
        }
        for name, (aid, members) in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(RoomError):
                    self.store.add_members(self.keeper, aid, members, new_request_id('add'))
        self.store.execute(self.keeper, 'revoke', {'authority_id': 'gym-build'}, 'revoke-gym')
        with self.assertRaisesRegex(RoomError, 'revoked'):
            self.store.add_members(self.keeper, 'gym-build', ['dot'], 'add-gym-1')
        with self.assertRaises(RoomError):
            self.store.add_members(self.keeper, 'local-crew', ['dot'], 'bad/id')

    def test_a_scope_cannot_pass_32_members(self):
        from common_room.protocol import Actor
        extra = ['x' + str(i) for i in range(40)]
        for name in extra:
            self.store.people[name] = Actor(name, 'worker')
        room_size = len(self.members('orchestra'))
        with self.assertRaisesRegex(RoomError, '32'):
            self.store.add_members(self.keeper, 'orchestra', extra[:33 - room_size], 'add-many')
        self.store.add_members(self.keeper, 'orchestra', extra[:32 - room_size], 'add-enough')
        self.assertEqual(len(self.members('orchestra')), 32)


class ReplayTests(unittest.TestCase):
    """Rule changes must keep old history readable (RUN_RULES). fold() gained one new event kind; every old kind
    folds exactly as before, so a log without the new event gives the same states, and adding it changes only the
    members of that one scope."""
    def test_a_rich_history_folds_the_same_with_and_without_the_new_event(self):
        from contextlib import closing
        from common_room.store import fold, read_events
        room = start(case_dir('seat-dot-replay'), dot_enrolled_nowhere(SHIPPED), SEATS)
        self.addCleanup(room.close)
        store, keeper = room.store, room.store.people['keeper']
        msg = {'title': 'W', 'body': 'Root.', 'acceptance': 'A.', 'recipient': 'claude-room', 'authority_id': 'orchestra',
               'kind': 'handoff.record'}
        task = store.execute(keeper, 'intake', {'source_namespace': 'estate', 'external_reference': 'r-1', 'message': msg},
                             'r-intake')['task']['id']
        store.execute(keeper, 'deliver', {'task_id': task}, 'r-deliver')
        orch = CrewClient(room.root / 'claude-room-credential.json')
        orch.command('ack', {'task_id': task}, 'r-ack')
        fence = orch.command('claim', {'task_id': task, 'seconds': 1800}, 'r-claim')['task']['fence']
        orch.command('note', {'authority_id': 'orchestra', 'body': 'Working.'}, 'r-note')
        store.execute(keeper, 'pause', {'authority_id': 'local-crew'}, 'r-pause')
        store.execute(keeper, 'resume', {'authority_id': 'local-crew'}, 'r-resume')
        self.assertTrue(fence >= 1)
        with closing(store._connect()) as conn:
            old_log = read_events(conn)
        old_states = json.loads(canonical(fold(old_log)))
        store.add_members(keeper, 'orchestra', ['dot'], 'r-add')
        with closing(store._connect()) as conn:
            new_log = read_events(conn)
        self.assertEqual(new_log[:len(old_log)], old_log)
        self.assertEqual(json.loads(canonical(fold(old_log))), old_states)        # replay is stable
        authorities, tasks = json.loads(canonical(fold(new_log)))
        self.assertEqual(tasks, old_states[1])
        self.assertEqual(authorities['orchestra']['members'], old_states[0]['orchestra']['members'] + ['dot'])
        authorities['orchestra']['members'] = old_states[0]['orchestra']['members']
        self.assertEqual(authorities, old_states[0])


class StopTests(unittest.TestCase):
    """A STOP note from the dot is not a stop, for every reader of one: crew watch, the store's own stop record
    and the STOP file."""
    def test_crew_watch_ignores_a_stop_note_from_dot_and_obeys_claude_room(self):
        from common_room.crew import _watch_event, WATCH_STOP_SENDERS
        self.assertNotIn('dot', WATCH_STOP_SENDERS)
        client = types.SimpleNamespace(actor_id='ferrule')
        state = {'server_time': 100.0, 'tasks': [], 'notes': [{'seq': 1, 'sender': 'keeper', 'body': 'hello'}]}
        base = {'note_seq': None, 'status': {}}
        self.assertIsNone(_watch_event(client, state, False, base))
        state['notes'].append({'seq': 2, 'sender': 'dot', 'body': 'STOP: everyone stop now'})
        self.assertIsNone(_watch_event(client, state, False, base))
        state['notes'].append({'seq': 3, 'sender': 'claude-room', 'body': 'STOP: for real'})
        event = _watch_event(client, state, False, base)
        self.assertEqual((event[0], event[2]), ('STOP', 4))

    def test_a_stop_note_from_dot_pauses_nothing_and_writes_no_stop_file(self):
        room = start(case_dir('seat-dot-stop'), SHIPPED, SEATS + ['dot'])
        self.addCleanup(room.close)
        CrewClient(room.root / 'dot-credential.json').command(
            'note', {'authority_id': 'orchestra', 'body': 'STOP: the dot says stop'}, 'dot-stop-note')
        grants = room.store._snapshot(room.store.people['keeper'])[1]
        self.assertFalse(any(a['paused'] or 'room_stopped' in a for a in grants.values()))
        self.assertFalse((room.root / 'STOP').exists())


class StartHazardTests(unittest.TestCase):
    """The hazard enroll_check.py exists for: a seat in SEATS that the loaded enrollment lacks stops the room."""
    def test_selecting_dot_without_enrolling_it_refuses_the_start(self):
        root = case_dir('seat-dot-hazard')
        start(root, without_dot(SHIPPED), SEATS).close()
        from common_room.activation import start_room
        with self.assertRaisesRegex(ValueError, 'Select distinct registered'):
            start_room(root, selected=SEATS + ['dot'])


class LauncherTests(unittest.TestCase):
    """Start Common Room Crew.cmd: dot gets a key (SEATS) and a relay folder (RELAY_SEATS), every seat it starts
    is enrolled in the shipped file, and the join cards' copy of SEATS is the script's own line."""
    @staticmethod
    def seat_list(name):
        script = (ROOT / 'Start Common Room Crew.cmd').read_text(encoding='utf-8')
        import re
        lines = re.findall(r'^set "' + name + r'=([^"]*)"\s*$', script, re.M)
        assert len(lines) == 1, lines
        return lines[0].split()

    def test_dot_is_in_seats_and_relay_seats(self):
        # seat-truth (D9): local-b stays enrolled but the launcher gives it no key.
        self.assertEqual(self.seat_list('SEATS'), [s for s in SEATS if s != 'local-b'] + ['dot'] + list(LATER))
        self.assertIn('dot', self.seat_list('RELAY_SEATS'))

    def test_every_launched_seat_is_enrolled_in_the_shipped_file(self):
        enrolled = {a['id'] for a in FULL['actors']}           # the real file, later seats included
        self.assertEqual([s for s in self.seat_list('SEATS') if s not in enrolled], [])

    def test_join_cards_know_the_launcher_seats(self):
        from common_room.join_cards import LAUNCHER_DEFAULT_SEATS
        self.assertEqual(LAUNCHER_DEFAULT_SEATS, frozenset(self.seat_list('SEATS')))

    def test_the_start_script_runs_the_check_before_the_room(self):
        script = (ROOT / 'Start Common Room Crew.cmd').read_text(encoding='utf-8')
        self.assertLess(script.index('crew\\enroll_check.py %SEATS%'), script.index('start "CRCREW room"'))
        self.assertNotIn('findstr /c:"\\"claude-code-b\\""', script)


class EnrollCheckTests(unittest.TestCase):
    """crew/enroll_check.py, run by the Start script before the room starts."""
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('enroll_check', ROOT / 'crew' / 'enroll_check.py')
        self.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tool)
        self.seats = SEATS + ['dot']

    def test_shanes_room_today_gets_the_shipped_enrollment(self):
        code, line = self.tool.decide(without_dot(SHIPPED), SHIPPED, self.seats)
        self.assertEqual(code, 3)
        self.assertIn('dot', line)

    def test_after_that_start_nothing_more(self):
        self.assertEqual(self.tool.decide(json.loads(canonical(SHIPPED)), SHIPPED, self.seats)[0], 0)

    def test_first_run(self):
        self.assertEqual(self.tool.decide(None, SHIPPED, self.seats)[0], 3)

    def test_a_seat_in_seats_that_the_shipped_file_lacks_stops_it(self):
        code, line = self.tool.decide(without_dot(SHIPPED), SHIPPED, self.seats + ['ghost'])
        self.assertEqual(code, 2)
        self.assertIn('ghost', line)

    def test_the_old_0923_scope(self):
        live = dict(without_dot(SHIPPED), actors=[a for a in SHIPPED['actors'] if a['id'] not in ('claude-code', 'claude-code-b', 'dot')],
                    authorities=[a for a in SHIPPED['authorities'] if a['id'] != 'orchestra'] +
                    [{'id': 'orchestra', 'members': ['keeper', 'claude-room', 'tinker'], 'scope': 'Older 09-23 wording'}])
        self.assertEqual(self.tool.decide(live, SHIPPED, self.seats)[0], 4)

    def test_a_room_that_keeps_its_own_enrollment_still_starts_when_every_seat_is_enrolled(self):
        live = json.loads(json.dumps(SHIPPED))
        orchestra(live)['scope'] = 'A room with its own wording'
        self.assertEqual(self.tool.decide(live, SHIPPED, self.seats)[0], 0)
        live['actors'] = [a for a in live['actors'] if a['id'] != 'dot']
        orchestra(live)['members'].remove('dot')
        code, line = self.tool.decide(live, SHIPPED, self.seats)
        self.assertEqual(code, 2)
        self.assertIn('dot', line)

    def test_a_shipped_file_that_changes_a_row_stops_it(self):
        shipped = json.loads(json.dumps(SHIPPED))
        orchestra(shipped)['members'] = ['keeper', 'dot'] + LIVE_ORCHESTRA[1:]
        live = without_dot(SHIPPED)
        code, _ = self.tool.decide(live, shipped, self.seats)
        self.assertEqual(code, 2)


if __name__ == '__main__':
    unittest.main(argv=sys.argv[:1])    # evidence.py passes the tree root as an argument; it names no test
