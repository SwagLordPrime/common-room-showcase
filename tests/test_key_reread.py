"""A4 key re-read (task tc28bbe83bd0, 2026-10-24): seats carry on through a key renewal and are pointed to their join card.

A renewal is what activation.run_renewing does every cycle: close the room, then start it again on the same root and
port, which issues every seat a new key and the room a new instance id. These tests do exactly that to a scratch room
under .checks, with keys the tests make. 356bbac already re-reads each seat's own credential file on the room's next
401 (crew_client.CrewClient.connect), so a watch, a relay and a one-shot carry on; what A4 adds is checked here:
  * one note per seat after a renewal (state `room_renewed`, a `note:` line in crew output), cleared by the seat's
    first command, pointing it to `crew card <seat>`; no note at all without a renewal;
  * a relay command written while the room is restarting waits for it instead of being answered "unavailable";
  * a renewal alone wakes no watch, and a restart under three minutes is not ROOM DOWN, while a room that stays down
    still is;
  * no key text anywhere but the credential files;
  * the board no longer calls an idle seat with a good key "key inactive".
"""
import sys
from pathlib import Path
if __name__ == '__main__':          # runnable as a script too (tools/evidence.py runs `python tests/<file> .`)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import io, json, unittest
from contextlib import redirect_stderr, redirect_stdout

import common_room.crew as crew
from tests.helpers import case_dir

CONFIG = {
    'version': 1,
    'actors': [{'id': 'k-worker', 'role': 'worker', 'label': 'Worker', 'session': 'fixture-w'},
               {'id': 'k-relay', 'role': 'worker', 'label': 'Relay seat', 'session': 'fixture-r'},
               {'id': 'k-reviewer', 'role': 'reviewer', 'label': 'Reviewer', 'session': 'fixture-v'}],
    'authorities': [{'id': 'k-scope', 'members': ['keeper', 'k-worker', 'k-relay', 'k-reviewer'], 'scope': 'Fictional key scope'}],
}
SEATS = ['k-worker', 'k-relay', 'k-reviewer']


class FakeClock:
    """monotonic() and sleep() that move the same fake time; `hook(n)` runs inside the nth sleep."""
    def __init__(self, hook=None):
        self.now = 1000.0
        self.sleeps = []
        self.hook = hook

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if self.hook is not None:
            self.hook(len(self.sleeps), self)
        self.now += seconds


class RenewalBase(unittest.TestCase):
    def setUp(self):
        self.root = case_dir('a4-renew')
        self.rooms = []
        self.room = self.start(CONFIG)

    def tearDown(self):
        for room in self.rooms:
            room.close()

    def start(self, enrollment=None, plans_root=None):
        from common_room.activation import start_room
        room = start_room(self.root, enrollment=enrollment, selected=SEATS, plans_root=plans_root)
        self.rooms.append(room)
        return room

    def renew(self, plans_root=None):
        """One renewal, as run_renewing does it: close, then start on the same root (same port, new keys)."""
        self.room.close()
        self.room = self.start(plans_root=plans_root)
        return self.room

    def cred(self, seat):
        return self.root / (seat + '-credential.json')

    def token(self, seat):
        return json.loads(self.cred(seat).read_text(encoding='utf-8'))['token']

    def cli(self, seat, *args, json_mode=False):
        out, err = io.StringIO(), io.StringIO()
        argv = ['--credential', str(self.cred(seat))] + (['--json'] if json_mode else []) + list(args)
        with redirect_stdout(out), redirect_stderr(err):
            code = crew.main(argv)
        self.outputs.append(out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def intake(self, title='Fixture task', to='k-worker', ref=None):
        args = ['intake', '--scope', 'k-scope', '--to', to, '--title', title, '--body', 'Do it.', '--acceptance', 'Right.']
        if ref:
            args += ['--ref', ref]
        code, out, err = self.cli('keeper', *args, json_mode=True)
        self.assertEqual(code, 0, err)
        return json.loads(out)['task_id']

    def watch(self, seat, max_wait, hook):
        args = crew.build_parser().parse_args(['--credential', str(self.cred(seat)), 'watch', '--max-wait', str(max_wait)])
        clock = FakeClock(hook)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = crew.run_watch(crew.CrewClient(str(self.cred(seat))), args, sleep=clock.sleep, monotonic=clock.monotonic)
        self.outputs.append(out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue(), clock

    def state(self, seat):
        from common_room.board import state_body
        return state_body(self.room.server, self.room.store.people[seat])

    def instance(self):
        return json.loads((self.root / 'connection.json').read_text(encoding='utf-8'))['instance_id']

    outputs = None

    def run(self, result=None):
        self.outputs = []
        return super().run(result)


class CarryOnTests(RenewalBase):
    def test_a_watch_spanning_a_renewal_carries_on_and_wakes_only_for_the_next_real_work(self):
        old_token, old_instance = self.token('k-worker'), self.instance()
        seen = {}

        def hook(n, clock):
            if n == 2:
                self.renew()
                seen['instance'] = self.instance()
            if n == 5:
                seen['task'] = self.intake('After the renewal')
        code, out, err, clock = self.watch('k-worker', 300, hook)
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(out, 'TASK ' + crew.short_id(seen['task']) + ' delivered: After the renewal\n')   # one line, as ever
        self.assertNotEqual(self.token('k-worker'), old_token)
        self.assertNotEqual(seen['instance'], old_instance)
        self.assertEqual(clock.sleeps, [crew.WATCH_POLL_SECONDS] * 5)       # no backoff, no extra wake for the renewal
        contact = {p['id']: p['contact'] for p in self.state('keeper')['participants']}['k-worker']
        self.assertNotIn('inactive', contact)

    def test_a_renewal_alone_wakes_no_one(self):
        def hook(n, clock):
            if n == 3:
                self.renew()
        code, out, err, clock = self.watch('k-worker', 200, hook)
        self.assertEqual((code, out, err), (3, 'WAIT\n', ''))

    def test_a_restart_under_three_minutes_is_not_room_down(self):
        down = {}

        def hook(n, clock):
            if n == 1:
                self.room.close()
                down['at'] = clock.now
            elif 'back' not in down and clock.now - down['at'] >= 170:
                self.room = self.start()
                down['back'] = clock.now
        code, out, err, clock = self.watch('k-worker', 400, hook)
        self.assertIn('back', down)
        self.assertEqual((code, out), (3, 'WAIT\n'))                          # carried on: no exit 5 for the gap

    def test_a_room_that_stays_down_is_still_room_down(self):
        def hook(n, clock):
            if n == 1:
                self.room.close()
        code, out, err, clock = self.watch('k-worker', 400, hook)
        self.assertEqual((code, out), (5, 'ROOM DOWN\n'))
        self.assertGreaterEqual(sum(clock.sleeps), crew.WATCH_DOWN_AFTER)


class RelayTests(RenewalBase):
    def relay(self):
        from common_room.relay import Relay
        relay = Relay(self.root, ['k-relay'], clock=self.relay_clock, state_every=0)
        relay.clients['k-relay'].sleep = lambda seconds: None     # its reconnect pauses, not real time
        return relay

    def relay_clock(self):
        return self.now

    def request(self, name, body):
        path = self.root / 'relay' / 'k-relay' / 'requests' / name
        path.write_text(json.dumps(body), encoding='utf-8')
        return path

    def response(self, request_id):
        path = self.root / 'relay' / 'k-relay' / 'responses' / ('resp-' + request_id + '.json')
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None

    def notes(self):
        events = self.room.store._snapshot(self.room.store.people['keeper'])[0]
        return [e['payload']['body'] for e in events if e['kind'] == 'note']

    def test_a_relay_round_trip_spanning_a_renewal_carries_on_with_no_resend(self):
        self.now = 5000.0
        relay = self.relay()
        relay.process_once()
        self.room.close()                                              # the renewal begins: the room is unreachable
        self.request('n1.json', {'request_id': 'n-1', 'kind': 'command', 'action': 'note',
                                 'payload': {'authority_id': 'k-scope', 'body': 'written during the renewal'}})
        relay.process_once()
        self.assertIsNone(self.response('n-1'))                        # not answered "unavailable": it waits
        self.now += 8
        self.room = self.start()                                       # new keys, new instance
        relay.process_once()
        answer = self.response('n-1')
        self.assertTrue(answer['ok'], answer)
        self.assertEqual(self.notes().count('written during the renewal'), 1)
        state = json.loads((self.root / 'relay' / 'k-relay' / 'relay-state.json').read_text(encoding='utf-8'))
        self.assertEqual(state['instance_id'], self.instance())

    def test_a_room_that_stays_down_is_answered_unavailable_after_the_window(self):
        from common_room.relay import RETRY_SECONDS
        self.now = 5000.0
        relay = self.relay()
        self.room.close()
        self.request('n2.json', {'request_id': 'n-2', 'kind': 'command', 'action': 'note',
                                 'payload': {'authority_id': 'k-scope', 'body': 'never arrives'}})
        relay.process_once()
        self.assertIsNone(self.response('n-2'))
        self.now += RETRY_SECONDS + 1
        relay.process_once()
        answer = self.response('n-2')
        self.assertEqual((answer['ok'], answer['error']), (False, 'unavailable'))

    def test_a_relay_seat_sees_the_note_in_relay_state_until_its_own_command(self):
        self.now = 5000.0
        self.renew()
        relay = self.relay()
        relay.process_once()
        state_path = self.root / 'relay' / 'k-relay' / 'relay-state.json'
        note = json.loads(state_path.read_text(encoding='utf-8'))['room_renewed']
        self.assertEqual(note['instance_id'], self.instance())
        self.assertEqual(note['card'], 'crew card k-relay')
        self.request('n3.json', {'request_id': 'n-3', 'kind': 'command', 'action': 'note',
                                 'payload': {'authority_id': 'k-scope', 'body': 'carrying on'}})
        relay.process_once()
        self.assertTrue(self.response('n-3')['ok'])
        self.assertNotIn('room_renewed', self.response('n-3')['result'])   # replies are byte-identical to receipts
        self.assertNotIn('room_renewed', json.loads(state_path.read_text(encoding='utf-8')))


class NoteTests(RenewalBase):
    def test_no_renewal_no_note_and_no_change(self):
        self.assertNotIn('room_renewed', self.state('k-worker'))
        code, out, err = self.cli('k-worker', 'tasks')
        self.assertEqual((code, out, err), (0, '(no tasks)\n', ''))
        code, out, err = self.cli('k-worker', 'tasks', json_mode=True)
        self.assertEqual((json.loads(out), err), ({'tasks': []}, ''))

    def test_the_seats_next_call_points_it_to_its_join_card_once(self):
        plans = case_dir('a4-plans')
        (plans / 'demo-plan').mkdir()
        self.room.server.plans_root = plans
        task = self.intake('Plan work', ref='demo-plan-k1')
        old_instance = self.instance()
        self.renew(plans_root=plans)
        note = self.state('k-worker')['room_renewed']
        self.assertEqual(note['previous_instance_id'], old_instance)
        self.assertEqual(note['instance_id'], self.instance())
        self.assertEqual(note['expires_at'], self.room.expiry)
        self.assertEqual((note['scopes'], note['plans'], note['card']), (['k-scope'], ['demo-plan'], 'crew card k-worker'))
        code, out, err = self.cli('k-worker', 'tasks')
        self.assertEqual(code, 0, err)
        self.assertNotIn('note:', out)                                      # stdout is unchanged; the note is on stderr
        lines = err.splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith('note: The room started a new key cycle (instance ' + self.instance()[:8]), lines)
        self.assertIn('Your current join card: crew card k-worker', lines[0])
        self.assertIn('plan demo-plan', lines[0])
        code, out, err = self.cli('k-worker', 'tasks')                      # read-only calls keep pointing to it
        self.assertIn('note: ', err)
        code, out, err = self.cli('k-worker', 'ack', task)                  # the seat's first command clears it
        self.assertEqual(code, 0, err)
        code, out, err = self.cli('k-worker', 'tasks')
        self.assertNotIn('note: ', out + err)
        self.assertNotIn('room_renewed', self.state('k-worker'))
        self.assertIn('room_renewed', self.state('k-reviewer'))             # every other seat still has its own note
        code, out, err = self.cli('k-worker', 'card', 'k-worker')           # and the card it points to renders
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith('SEAT: k-worker'))

    def test_the_watch_loop_a_356bbac_card_gives_runs_unchanged_after_a_renewal(self):
        self.renew()
        code, out, err, clock = self.watch('k-worker', 60, None)           # the cards' own loop line: crew ... watch
        self.assertEqual((code, out, err), (3, 'WAIT\n', ''))                 # the watch prints no note: one line, as ever

    def test_a_saved_read_is_byte_identical_after_a_renewal(self):
        task = self.intake('Report to read')
        self.assertEqual(self.cli('k-worker', 'ack', task)[0], 0)
        self.assertEqual(self.cli('k-worker', 'claim', task, '--seconds', '600')[0], 0)
        report = case_dir('a4-report') / 'report.md'
        report.write_text('Fixture report body.\n', encoding='utf-8')
        code, out, err = self.cli('k-worker', 'submit', task, '--file', str(report), '--summary', 'Done.', '--skip-gate', 'fixture report, no gate here')
        self.assertEqual(code, 0, err)
        before = self.cli('k-reviewer', 'read', task)
        self.renew()
        after = self.cli('k-reviewer', 'read', task)
        self.assertEqual((after[0], after[1]), (before[0], before[1]))     # stdout byte for byte
        self.assertIn('note: ', after[2])
        code, out, err = self.cli('k-reviewer', 'read', task, json_mode=True)
        self.assertEqual(json.loads(out), json.loads(self.cli('k-reviewer', 'read', task, json_mode=True)[1]))
        self.assertNotIn('room_renewed', json.loads(out))

    def test_claude_rooms_own_note_names_shane(self):
        from common_room.board import renewal_note
        from common_room.protocol import Actor
        self.renew()
        self.room.server.renewal_pending.add('claude-room')
        _, authorities, tasks = self.room.store._snapshot(self.room.store.people['keeper'])
        note = renewal_note(self.room.server, Actor('claude-room', 'worker'), authorities, tasks)
        self.assertIn('a relay seat asks Shane to run it', note['note'])
        self.assertIn('UTC', note['note'])

    def test_an_idle_seat_with_a_good_key_is_not_called_key_inactive(self):
        self.cli('k-worker', 'tasks')
        self.room.server.contacts['k-worker'] -= 120
        contact = {p['id']: p['contact'] for p in self.state('keeper')['participants']}['k-worker']
        self.assertEqual(contact, 'Credential contact stale; key active')


class SecretTests(RenewalBase):
    def test_no_key_text_anywhere_but_the_credential_files(self):
        from common_room.relay import Relay
        tokens = {seat: self.token(seat) for seat in SEATS + ['keeper']}
        relay = Relay(self.root, ['k-relay'], state_every=0)
        relay.clients['k-relay'].sleep = lambda seconds: None
        relay.process_once()
        task = self.intake('Secret check')
        self.renew()
        tokens.update({seat + '-new': self.token(seat) for seat in SEATS + ['keeper']})
        (self.root / 'relay' / 'k-relay' / 'requests' / 's.json').write_text(json.dumps({'request_id': 's', 'kind': 'state'}), encoding='utf-8')
        relay.process_once()
        self.cli('k-worker', 'tasks')
        self.cli('k-worker', 'tasks', json_mode=True)
        self.cli('k-worker', 'ack', task)
        self.watch('k-reviewer', 40, None)
        from common_room.protocol import canonical
        texts = list(self.outputs) + [canonical(self.state(seat)) for seat in SEATS + ['keeper']]
        allowed = {p.name for p in self.root.glob('*-credential.json')}
        for path in self.root.rglob('*'):
            if path.is_file() and path.name not in allowed and path.suffix in ('.json', '.jsonl', '.txt', '.md'):
                texts.append(path.read_text(encoding='utf-8', errors='replace'))
        events = self.room.store._snapshot(self.room.store.people['keeper'])[0]
        texts.append(canonical(events))
        for name, token in tokens.items():
            for text in texts:
                self.assertNotIn(token, text, name)
        self.assertEqual(len(set(tokens.values())), len(tokens))           # every renewal key really is new


if __name__ == '__main__':
    unittest.main(argv=sys.argv[:1])
