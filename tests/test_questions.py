"""A1 comms fix (task t0d912d8c35b7, 2026-10-24): a board question reaches the seat it is for.

A note whose first line starts `QUESTION FOR <seat>:` is delivered to that seat. It wakes that seat's `crew watch`
(a QUESTION line) or shows in that seat's relay-state, and no other seat's. It carries four separate marks:
delivered (the note itself), read (the seat's own watch or own relay state request picked it up), accepted (the seat
took it up) and checked (the asker confirmed it is settled). Everything runs in a scratch room under .checks with
keys the tests make; nothing here reaches the live room, a model or the network beyond loopback.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # lets `python tests/test_questions.py` run from a tree root

import json, os, shutil, subprocess, unittest

import common_room.crew as crew
from common_room.store import fold
from common_room.protocol import canonical
try:
    from common_room.store import fold_questions
except ImportError:          # a store from before A1 (the RED run on 356bbac): no questions, so the tests fail on behaviour
    def fold_questions(events):
        return {}
from tests.helpers import case_dir, ROOT
from tests.test_crew_watch import WatchBase, FakeClock

Q_CONFIG = {
    'version': 1,
    'actors': [
        {'id': 'q-worker-a', 'role': 'worker', 'label': 'Worker A', 'session': 'fixture-a'},
        {'id': 'q-worker-b', 'role': 'worker', 'label': 'Worker B', 'session': 'fixture-b'},
        {'id': 'q-reviewer', 'role': 'reviewer', 'label': 'Reviewer', 'session': 'fixture-r'},
        {'id': 'claude-room', 'role': 'worker', 'label': 'Orchestrator', 'session': 'fixture-o'},
        {'id': 'local-a', 'role': 'worker', 'label': 'Local model', 'session': 'fixture-l'},
        {'id': 'notify', 'role': 'observer', 'label': 'Phone push', 'session': 'fixture-n'},
    ],
    'authorities': [
        {'id': 'q-scope', 'members': ['keeper', 'q-worker-a', 'q-worker-b', 'q-reviewer', 'claude-room', 'local-a', 'notify'],
         'scope': 'Fictional question scope'},
        {'id': 'q-other', 'members': ['keeper', 'q-worker-a', 'q-reviewer'], 'scope': 'Fictional second scope'},
    ],
}
Q_SEATS = ['q-worker-a', 'q-worker-b', 'q-reviewer', 'claude-room', 'local-a', 'notify']
# The rollback tests are opt-in (A1R): they run 356bbac's own code from a checkout that only A1_BASELINE_TREE names
# (a worktree of its own, outside the task worktree; never the live tree). There is no default folder.
BASELINE_ENV = 'A1_BASELINE_TREE'
BASELINE_VALUE = os.environ.get(BASELINE_ENV, '')
BASELINE_TREE = Path(BASELINE_VALUE) if BASELINE_VALUE else None
BASELINE_COMMIT = '356bbac'
BASELINE_FULL = '356bbac1e85abe4520944c2655c4f41ef5348a58'
BASELINE_SKIP = 'A1 rollback tests are opt-in: set ' + BASELINE_ENV + ' to a 356bbac checkout to run them'


def baseline_opted_in(value):
    """Unset (read as '') or empty: not opted in, so RollbackTests skip. Any other value, spaces only included, is an opt-in."""
    return value != ''


def baseline_problem(value):
    """None when `value` names an absolute 356bbac checkout holding common_room/store.py, else what is wrong with it.
    Only called for a value that is set and not empty: an explicit opt-in that is wrong fails, it never skips."""
    folder = Path(value)
    if not folder.is_absolute():
        return 'is not an absolute path'
    if not (folder / 'common_room' / 'store.py').is_file():
        return 'holds no common_room/store.py'
    head = subprocess.run(['git', '-C', str(folder), 'rev-parse', 'HEAD'], capture_output=True, text=True,
                          env=dict(os.environ, GIT_OPTIONAL_LOCKS='0')).stdout.strip()
    if head != BASELINE_FULL:
        return 'has HEAD ' + (head or '(none)') + ', not ' + BASELINE_FULL
    return None


def baseline_failure(value, problem):
    return (BASELINE_ENV + ' is set to ' + repr(value) + ', which ' + problem + '. Unset ' + BASELINE_ENV
            + ' to skip the A1 rollback tests, or point it at a ' + BASELINE_FULL + ' checkout to run them.')


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class QuestionBase(WatchBase):
    def start_room(self, root=None, selected=None, clock=None, enrollment=Q_CONFIG):
        from common_room.activation import start_room
        room = start_room(root or case_dir('a1-questions'), enrollment=enrollment, selected=selected or Q_SEATS, clock=clock)
        self.addCleanup(room.close)
        return room

    def note(self, room, actor, body, scope='q-scope'):
        return self.cli_ok(room, actor, 'note', scope, body)

    def ask(self, room, asker, seat, text='Is the fixture ready?', scope='q-scope'):
        result = self.note(room, asker, 'QUESTION FOR ' + seat + ': ' + text, scope)
        self.assertIsNotNone(result.get('question'), result)
        return result['question']['seq']

    def quick_watch(self, room, actor, *flags):
        return self.watch(room, actor, '--max-wait', '45', *flags)

    def actor(self, room, actor_id):
        return room.store.people[actor_id]

    def questions_of(self, room, actor_id):
        from common_room.board import state_body
        return state_body(room.server, self.actor(room, actor_id)).get('questions', [])

    def asked_of(self, room, actor_id):
        from common_room.board import state_body
        return state_body(room.server, self.actor(room, actor_id)).get('asked', [])

    def events(self, room):
        return room.store._snapshot(self.actor(room, 'keeper'))[0]

    def mark(self, room, actor, mark, seq):
        return self.run_cli(room, actor, 'question', mark, str(seq))


class AddressingTests(QuestionBase):
    def test_a_question_to_a_crew_seat_wakes_that_seat_and_no_other(self):
        room = self.start_room()
        code, out, err = self.run_cli(room, 'q-worker-a', 'note', 'q-scope', 'QUESTION FOR q-worker-b: Is the build green?',
                                      json_mode=False)
        self.assertEqual(code, 0, err)
        self.assertIn('note posted to q-scope', out)
        self.assertIn('delivered to q-worker-b (not yet read', out)          # posted is not arrived: delivered, not read
        seq = self.questions_of(room, 'q-worker-b')[0]['seq']

        for other, flags in (('q-worker-a', ()), ('q-reviewer', ('--reviews',)), ('claude-room', ()), ('local-a', ())):
            with self.subTest(seat=other):
                code, out, _ = self.quick_watch(room, other, *flags)
                self.assertEqual((code, out.strip()), (3, 'WAIT'))
        code, out, _ = self.quick_watch(room, 'q-worker-b')
        self.assertEqual(code, 0)
        self.assertEqual(self.one_line(out), 'QUESTION #' + str(seq) + ' from q-worker-a: QUESTION FOR q-worker-b: Is the build green?')
        row = self.questions_of(room, 'q-worker-b')[0]
        self.assertEqual(row['state'], 'read')
        self.assertIsNotNone(row['marks']['read'])
        code, out, _ = self.quick_watch(room, 'q-worker-b')           # read now: it does not wake the seat again
        self.assertEqual((code, out.strip()), (3, 'WAIT'))

        # It stays on the board as a Lounge note for every member, exactly like any other note.
        from common_room.board import state_body
        for seat in ('q-worker-a', 'q-reviewer', 'notify'):
            notes = state_body(room.server, self.actor(room, seat))['notes']
            self.assertIn('QUESTION FOR q-worker-b: Is the build green?', [n['body'] for n in notes])
            self.assertEqual(set(notes[-1]), {'sender', 'authority_id', 'body', 'seq', 'timestamp', 'sha256'})
        # ...but it enters the addressed seat's inbox ('questions') only; the asker sees it in its own outbox ('asked').
        self.assertEqual([q['seq'] for q in self.asked_of(room, 'q-worker-a')], [seq])
        self.assertEqual(self.questions_of(room, 'q-worker-a'), [])
        self.assertEqual(self.asked_of(room, 'q-worker-b'), [])
        for seat in ('q-reviewer', 'claude-room', 'local-a', 'notify', 'keeper'):
            self.assertEqual((self.questions_of(room, seat), self.asked_of(room, seat)), ([], []), seat)

    def test_a_question_to_a_relay_seat_shows_in_its_relay_state_only_and_its_own_state_request_reads_it(self):
        from common_room.relay import Relay
        room = self.start_room()
        seq = self.ask(room, 'q-worker-a', 'claude-room', 'Which window tonight?')
        relay = Relay(room.root, ['claude-room', 'q-worker-b'], clock=room.server.clock, state_every=0)
        relay.process_once()                                          # the relay's own periodic write: it only holds it
        mine = json.loads((room.root / 'relay' / 'claude-room' / 'relay-state.json').read_text(encoding='utf-8'))
        other = json.loads((room.root / 'relay' / 'q-worker-b' / 'relay-state.json').read_text(encoding='utf-8'))
        self.assertEqual([(q['seq'], q['to'], q['state']) for q in mine['questions']], [(seq, 'claude-room', 'delivered')])
        self.assertIsNone(mine['questions'][0]['marks']['read'])
        self.assertEqual((other['questions'], other['asked']), ([], []))
        self.assertFalse(any(e['kind'] == 'question_marked' for e in self.events(room)))

        request = room.root / 'relay' / 'claude-room' / 'requests' / 'state-1.json'
        request.write_text(json.dumps({'request_id': 'state-1', 'kind': 'state'}), encoding='utf-8')
        relay.process_once()
        answer = json.loads((room.root / 'relay' / 'claude-room' / 'responses' / 'resp-state-1.json').read_text(encoding='utf-8'))
        self.assertTrue(answer['ok'], answer)
        self.assertEqual(answer['result'], {'written': True, 'questions_read': [seq]})
        mine = json.loads((room.root / 'relay' / 'claude-room' / 'relay-state.json').read_text(encoding='utf-8'))
        self.assertEqual(mine['questions'][0]['state'], 'read')
        marks = [e for e in self.events(room) if e['kind'] == 'question_marked']
        self.assertEqual([(e['actor'], e['payload']['mark']) for e in marks], [('claude-room', 'read')])
        # A crew seat's watch never wakes for a relay seat's question.
        code, out, _ = self.quick_watch(room, 'q-worker-b')
        self.assertEqual((code, out.strip()), (3, 'WAIT'))

    def test_a_relay_seat_records_accepted_through_its_own_folder(self):
        from common_room.relay import Relay
        room = self.start_room()
        seq = self.ask(room, 'q-worker-a', 'claude-room')
        relay = Relay(room.root, ['claude-room'], clock=room.server.clock, state_every=0)
        folder = room.root / 'relay' / 'claude-room'
        (folder / 'requests' / 's.json').write_text(json.dumps({'request_id': 's', 'kind': 'state'}), encoding='utf-8')
        relay.process_once()
        (folder / 'requests' / 'a.json').write_text(json.dumps({'request_id': 'a', 'kind': 'command', 'action': 'question_mark',
                                                                 'payload': {'note_seq': seq, 'mark': 'accepted'}}), encoding='utf-8')
        relay.process_once()
        answer = json.loads((folder / 'responses' / 'resp-a.json').read_text(encoding='utf-8'))
        self.assertTrue(answer['ok'], answer)
        self.assertEqual(answer['result']['question']['state'], 'accepted')


class MarkTests(QuestionBase):
    def test_the_four_marks_are_separate_and_move_in_order(self):
        room = self.start_room()
        posted = self.note(room, 'q-worker-a', 'QUESTION FOR q-worker-b: Can you check the pins?')
        seq = posted['question']['seq']
        self.assertEqual(posted['question'], {'seq': seq, 'to': 'q-worker-b', 'state': 'delivered'})

        def state():
            return [q for q in self.asked_of(room, 'q-worker-a') if q['seq'] == seq]

        self.assertEqual(state()[0]['marks']['read'], None)
        refusals = [('q-worker-b', 'accept', 'state'), ('q-worker-a', 'check', 'state'),   # out of order
                    ('q-worker-a', 'read', 'forbidden'), ('q-reviewer', 'read', 'forbidden'),  # not the addressed seat
                    ('notify', 'read', 'forbidden')]
        for actor, mark, code in refusals:
            with self.subTest(actor=actor, mark=mark):
                rc, _, err = self.mark(room, actor, mark, seq)
                self.assertNotEqual(rc, 0)
                self.assertIn(code, self.last_error_code(err))
        steps = [('q-worker-b', 'read', 'read'), ('q-worker-b', 'accept', 'accepted'), ('q-worker-a', 'check', 'checked')]
        wrong_at = {'accept': ('q-worker-a', 'forbidden'), 'check': ('q-worker-b', 'forbidden')}
        for actor, mark, reached in steps:
            if mark in wrong_at:
                rc, _, err = self.mark(room, wrong_at[mark][0], mark, seq)
                self.assertNotEqual(rc, 0)
                self.assertIn(wrong_at[mark][1], self.last_error_code(err))
            rc, out, err = self.mark(room, actor, mark, seq)
            self.assertEqual(rc, 0, err)
            self.assertEqual(json.loads(out)['question']['state'], reached)
            rc, _, err = self.run_cli(room, actor, 'question', mark, str(seq), '--request-id', 'again-' + mark)
            self.assertNotEqual(rc, 0)                                # each mark once
            self.assertIn('state', self.last_error_code(err))
        question = fold_questions(self.events(room))[seq]
        times = [question['marks'][m] for m in ('delivered', 'read', 'accepted', 'checked')]
        self.assertTrue(all(t is not None for t in times))
        self.assertEqual(times, sorted(times))
        self.assertEqual(state(), [])                                 # checked: settled, it leaves both seats' lists
        self.assertEqual(self.questions_of(room, 'q-worker-b'), [])

    def test_a_mark_on_a_plain_note_is_refused(self):
        room = self.start_room()
        self.note(room, 'q-worker-a', 'Just a lounge note.')
        seq = self.events(room)[-1]['seq']
        rc, _, err = self.mark(room, 'q-worker-b', 'read', seq)
        self.assertNotEqual(rc, 0)
        self.assertIn('forbidden', self.last_error_code(err))

    def test_a_room_restart_keeps_an_unread_question(self):
        root = case_dir('a1-restart')
        room = self.start_room(root)
        seq = self.ask(room, 'q-worker-a', 'q-worker-b', 'Still there after a restart?')
        room.close()
        again = self.start_room(root, enrollment=None)
        self.assertEqual([(q['seq'], q['state']) for q in self.questions_of(again, 'q-worker-b')], [(seq, 'delivered')])
        code, out, _ = self.quick_watch(again, 'q-worker-b')
        self.assertEqual(code, 0)
        self.assertTrue(self.one_line(out).startswith('QUESTION #' + str(seq) + ' from q-worker-a: '))

    def test_a_question_that_arrived_before_the_watch_started_still_wakes_it(self):
        room = self.start_room()
        seq = self.ask(room, 'q-worker-a', 'q-worker-b')
        code, out, _ = self.quick_watch(room, 'q-worker-b')
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith('QUESTION #' + str(seq)))

    def test_a_question_that_arrives_while_the_seat_waits_wakes_it(self):
        room = self.start_room()
        holder = {}

        def hook(n):
            if n == 1:
                holder['seq'] = self.ask(room, 'q-worker-a', 'q-worker-b', 'Arriving mid-wait')
        code, out, clock = self.watch(room, 'q-worker-b', hook=hook)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith('QUESTION #' + str(holder['seq'])))
        self.assertEqual(clock.sleeps, [crew.WATCH_POLL_SECONDS])

    def test_a_reply_starts_no_loop(self):
        room = self.start_room()
        seq = self.ask(room, 'q-worker-a', 'q-worker-b')
        self.assertEqual(self.quick_watch(room, 'q-worker-b')[0], 0)                  # read
        self.assertEqual(self.mark(room, 'q-worker-b', 'accept', seq)[0], 0)
        before = len(self.events(room))
        self.note(room, 'q-worker-b', 'answering #' + str(seq) + ': yes, the pins are checked.')
        self.assertEqual(len(self.events(room)), before + 1)                        # the reply is one note, nothing else
        self.assertNotIn('to', self.events(room)[-1]['payload'])                    # and is addressed to no one
        for seat in ('q-worker-a', 'q-worker-b'):
            code, out, _ = self.quick_watch(room, seat)
            self.assertEqual((code, out.strip()), (3, 'WAIT'), seat)
        self.assertEqual(self.mark(room, 'q-worker-a', 'check', seq)[0], 0)
        self.assertEqual(len(self.events(room)), before + 2)                        # the check is one mark, no note
        for seat in ('q-worker-a', 'q-worker-b'):
            code, out, _ = self.quick_watch(room, seat)
            self.assertEqual((code, out.strip()), (3, 'WAIT'), seat)
        kinds = [e['kind'] for e in self.events(room)[before:]]
        self.assertEqual(kinds, ['note', 'question_marked'])

    def test_a_refused_read_mark_is_not_returned_again_and_again(self):
        room = self.start_room()
        seq = self.ask(room, 'q-worker-a', 'q-worker-b')
        path = str(room.root / 'q-worker-b-credential.json')
        client = crew.CrewClient(path)
        calls = []

        def refuse(action, payload, request_id):
            calls.append(payload['note_seq'])
            raise crew.CrewError('held', 'This authority is paused or revoked.')
        client.command = refuse
        args = crew.build_parser().parse_args(['--credential', path, 'watch', '--max-wait', '90'])
        clock = FakeClock()
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            code = crew.run_watch(client, args, sleep=clock.sleep, monotonic=clock.monotonic)
        self.assertEqual((code, out.getvalue().strip()), (3, 'WAIT'))
        self.assertEqual(calls, [seq])                                               # tried once in this watch, not per poll
        self.assertEqual(self.questions_of(room, 'q-worker-b')[0]['state'], 'delivered')

    def test_a_question_comes_first_and_counts_the_rest(self):
        room = self.start_room()
        self.intake_q(room)
        seq = self.ask(room, 'q-worker-a', 'q-worker-b')
        code, out, _ = self.quick_watch(room, 'q-worker-b')
        self.assertEqual(code, 0)
        line = self.one_line(out)
        self.assertTrue(line.startswith('QUESTION #' + str(seq)) and line.endswith('(+1 more)'), line)
        code, out, _ = self.quick_watch(room, 'q-worker-b')
        self.assertEqual(code, 0)
        self.assertTrue(self.one_line(out).startswith('TASK '))

    def intake_q(self, room):
        code, out, err = self.run_cli(room, 'keeper', 'intake', '--scope', 'q-scope', '--to', 'q-worker-b',
                                      '--title', 'Fixture task', '--body', 'Do it.', '--acceptance', 'Looks right.')
        self.assertEqual(code, 0, err)
        return json.loads(out)['task_id']


class UnchangedTests(QuestionBase):
    def test_unaddressed_and_shane_notes_behave_as_before(self):
        room = self.start_room()
        bodies = ['QUESTION FOR SHANE: may I take the Tuesday slot?', 'QUESTION for claude-room: lowercase for',
                  'QUESTION FOR keeper: owner is not a seat', 'QUESTION FOR q-nobody: no such seat',
                  'QUESTION FOR q-worker-a: asking myself', 'QUESTION FOR Q-WORKER-B: seat names are lowercase',
                  'QUESTION: plain question', 'SHANE UPDATE: an update', 'A plain note.', 'Second line\nQUESTION FOR q-worker-b: not first']
        for body in bodies:
            code, out, err = self.run_cli(room, 'q-worker-a', 'note', 'q-scope', body, json_mode=False)
            self.assertEqual(code, 0, err)
            lines = out.strip().splitlines()
            self.assertEqual(lines[0], 'note posted to q-scope')
            if body.startswith(('QUESTION FOR SHANE', 'QUESTION:', 'SHANE', 'A plain', 'Second')):
                self.assertEqual(lines, ['note posted to q-scope'], body)                   # exactly as before
            else:
                self.assertTrue(lines[1].startswith('not delivered as a question'), body)
        # A seat outside the note's scope is not addressable there either.
        code, out, err = self.run_cli(room, 'q-worker-a', 'note', 'q-other', 'QUESTION FOR q-worker-b: wrong scope', json_mode=False)
        self.assertEqual(code, 0, err)
        self.assertIn('not delivered as a question', out)
        events = self.events(room)
        self.assertFalse(any('to' in e['payload'] for e in events if e['kind'] == 'note'))
        self.assertEqual(fold_questions(events), {})
        for seat in Q_SEATS:
            self.assertEqual(self.questions_of(room, seat), [], seat)
        # The Shane desk still sees its question.
        from common_room.board import state_body
        needs = state_body(room.server, self.actor(room, 'keeper'))['needs']
        self.assertIn('QUESTION from q-worker-a', ' '.join(i['summary'] for i in needs if i['kind'] == 'question'))

    def test_questions_to_local_and_notify_seats_are_stored_only(self):
        clock = Clock()
        room = self.start_room(clock=clock)
        local = self.ask(room, 'q-worker-a', 'local-a', 'Can you draft this?')
        push = self.ask(room, 'q-worker-a', 'notify', 'Push this?')
        rc, _, err = self.mark(room, 'notify', 'read', push)                            # an observer posts nothing
        self.assertNotEqual(rc, 0)
        self.assertIn('forbidden', self.last_error_code(err))
        clock.now += 3 * 3600                                                           # long past the 30-minute slow line
        from common_room.board import state_body
        for seat in ('keeper', 'claude-room', 'notify', 'local-a'):
            needs = state_body(room.server, self.actor(room, seat))['needs']
            self.assertFalse([i for i in needs if i['id'] in ('slow:note:' + str(local), 'slow:note:' + str(push))], seat)
        self.assertEqual({q['seq']: q['state'] for q in self.asked_of(room, 'q-worker-a')}, {local: 'delivered', push: 'delivered'})
        self.assertEqual([e['kind'] for e in self.events(room)].count('question_marked'), 0)

    def test_a_question_to_claude_room_still_goes_slow_for_shane_as_before(self):
        clock = Clock()
        room = self.start_room(clock=clock)
        seq = self.ask(room, 'q-worker-a', 'claude-room', 'Where is the plan?')
        clock.now += 31 * 60
        from common_room.board import state_body
        needs = state_body(room.server, self.actor(room, 'keeper'))['needs']
        self.assertIn('slow:note:' + str(seq), [i['id'] for i in needs])

    def test_an_old_watcher_state_without_questions_still_works(self):
        from tests.test_crew_watch import FakeClient, idle_state
        client = FakeClient(case_dir('a1-old-state'), [idle_state()])
        args = crew.build_parser().parse_args(['--credential', str(client.path), 'watch', '--max-wait', '45'])
        clock = FakeClock()
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(crew.run_watch(client, args, sleep=clock.sleep, monotonic=clock.monotonic), 3)


def _run_baseline(script, root):
    """Run `script` with the 356bbac checkout's own code (cwd and import path = that checkout); returns its JSON.
    The room stays where it is, as in a real rollback (the code changes, the room folder and its recorded artifact
    roots do not). start_room only takes a room under its own package's .checks, so the script points that one guard
    (handoff_room.ROOT / activation.ROOT) at this tree; no other line of the old code is touched."""
    env = dict(os.environ, PYTHONPATH=str(BASELINE_TREE), PYTHONDONTWRITEBYTECODE='1')
    env.pop('PYTHONIOENCODING', None)
    proc = subprocess.run([sys.executable, '-B', '-c', script, str(root), str(ROOT)], cwd=str(BASELINE_TREE), env=env,
                          capture_output=True, timeout=300)
    if proc.returncode != 0:
        raise AssertionError('baseline run failed: ' + proc.stderr.decode('utf-8', 'replace')[-3000:])
    return json.loads(proc.stdout.decode('utf-8').strip().splitlines()[-1])


BASELINE_SCRIPT = r'''
import json, sys, hashlib, subprocess
from pathlib import Path
import common_room.handoff_room as handoff_room, common_room.activation as activation
assert Path(activation.__file__).resolve().parents[1] == Path.cwd().resolve(), 'not the baseline checkout'
handoff_room.ROOT = activation.ROOT = Path(sys.argv[2])
from common_room.activation import start_room
from common_room.board import state_body
from common_room.participant import Participant
from common_room.protocol import canonical
from common_room.store import fold
root = Path(sys.argv[1])
head = subprocess.run(['git', 'rev-parse', '--short=7', 'HEAD'], capture_output=True, text=True).stdout.strip()
room = start_room(root, enrollment=None, selected=['q-worker-a', 'q-worker-b', 'q-reviewer', 'claude-room', 'local-a', 'notify'])
try:
    events = room.store._snapshot(room.store.people['keeper'])[0]
    authorities, tasks = fold(events)
    cred = json.loads((root / 'q-worker-b-credential.json').read_text(encoding='utf-8'))
    p = Participant(cred['origin'], cred['token']); p.login()
    state = p.state()
    import urllib.request
    status = json.loads(urllib.request.urlopen(cred['origin'] + '/api/status', timeout=10).read().decode('utf-8'))
    p.command('note', {'authority_id': 'q-scope', 'body': 'QUESTION FOR q-worker-a: written by the old code'}, 'old-note-1')
    p.command('note', {'authority_id': 'q-scope', 'body': 'plain note from the old code'}, 'old-note-2')
    after = room.store._snapshot(room.store.people['keeper'])[0]
    print(json.dumps({'head': head, 'fold': hashlib.sha256(canonical([authorities, tasks]).encode('utf-8')).hexdigest(),
                      'events': len(events), 'after': len(after), 'notes': [n['body'] for n in state['notes']],
                      'state_keys': sorted(state), 'status_seq': status['last_event_seq'], 'task_count': len(tasks)}))
finally:
    room.close()
'''


@unittest.skipUnless(baseline_opted_in(BASELINE_VALUE), BASELINE_SKIP)   # unset or empty: skip; reads no folder, runs no git
class RollbackTests(QuestionBase):
    def setUp(self):
        super().setUp()
        problem = baseline_problem(BASELINE_VALUE)   # set: it must be right, or each test fails here, before any work
        if problem:
            self.fail(baseline_failure(BASELINE_VALUE, problem))

    def test_baseline_checkout_is_356bbac(self):
        head = subprocess.run(['git', '-C', str(BASELINE_TREE), 'rev-parse', '--short=7', 'HEAD'],
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(head, BASELINE_COMMIT)

    def test_356bbac_starts_on_a_store_the_new_code_wrote_and_the_new_code_starts_on_its_writes(self):
        root = case_dir('a1-rb')
        (root / 'pre-switch-enrollment.json').write_text(canonical(Q_CONFIG), encoding='utf-8')
        room = self.start_room(root)
        task = self.intake_q_root(room)
        unread = self.ask(room, 'q-worker-a', 'q-worker-b', 'Unread across a rollback?')
        marked = self.ask(room, 'q-worker-b', 'q-worker-a', 'Accepted before the rollback?')
        self.assertEqual(self.mark(room, 'q-worker-a', 'read', marked)[0], 0)
        self.assertEqual(self.mark(room, 'q-worker-a', 'accept', marked)[0], 0)
        new_events = self.events(room)
        new_fold = canonical(list(fold(new_events)))
        room.close()

        # Rollback, in place: 356bbac's code starts on this same room folder, with the enrollment.json from before
        # the switch put back (cutover rollback restores that file).
        shutil.copyfile(root / 'pre-switch-enrollment.json', root / 'enrollment.json')
        old = _run_baseline(BASELINE_SCRIPT, root)
        self.assertEqual(old['head'], BASELINE_COMMIT)
        import hashlib
        self.assertEqual(old['fold'], hashlib.sha256(new_fold.encode('utf-8')).hexdigest())    # same work, same state
        self.assertEqual(old['events'], len(new_events))
        self.assertEqual(old['status_seq'], len(new_events))
        self.assertIn('QUESTION FOR q-worker-b: Unread across a rollback?', old['notes'])
        self.assertNotIn('questions', old['state_keys'])                                       # the old room has no list
        self.assertEqual(old['after'], len(new_events) + 2)

        # Forward again: the new code starts on the store 356bbac just wrote; the unread question and the marks hold,
        # and the old code's own QUESTION FOR note (written with no address) is never taken for a question.
        again = self.start_room(root, enrollment=None)
        rows = {q['seq']: q['state'] for q in self.asked_of(again, 'q-worker-a') + self.questions_of(again, 'q-worker-a')}
        self.assertEqual(rows, {unread: 'delivered', marked: 'accepted'})
        self.assertEqual(sorted(fold_questions(self.events(again))), [unread, marked])
        code, out, _ = self.quick_watch(again, 'q-worker-b')
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith('QUESTION #' + str(unread)))
        self.assertEqual(self.mark(again, 'q-worker-b', 'check', marked)[0], 0)
        self.assertIn(task, [t['id'] for t in again.store.list_tasks(self.actor(again, 'keeper'))])

    def test_the_new_code_starts_on_a_store_356bbac_made(self):
        root = case_dir('a1-made')
        (root / 'enrollment.json').write_text(canonical(Q_CONFIG), encoding='utf-8')
        old = _run_baseline(BASELINE_SCRIPT, root)                     # 356bbac makes the store, in place
        self.assertEqual(old['head'], BASELINE_COMMIT)
        room = self.start_room(root, enrollment=None)
        events = self.events(room)
        self.assertEqual(len(events), old['after'])
        self.assertEqual(fold_questions(events), {})                    # history written before A1 holds no questions
        for seat in Q_SEATS:
            self.assertEqual(self.questions_of(room, seat), [], seat)
        code, out, _ = self.quick_watch(room, 'q-worker-a')
        self.assertEqual((code, out.strip()), (3, 'WAIT'))
        seq = self.ask(room, 'q-worker-b', 'q-worker-a', 'New after the switch')
        code, out, _ = self.quick_watch(room, 'q-worker-a')
        self.assertTrue(out.startswith('QUESTION #' + str(seq)))

    def intake_q_root(self, room):
        code, out, err = self.run_cli(room, 'keeper', 'intake', '--scope', 'q-scope', '--to', 'q-worker-b',
                                      '--title', 'Fixture task', '--body', 'Do it.', '--acceptance', 'Looks right.')
        self.assertEqual(code, 0, err)
        return json.loads(out)['task_id']


class RollbackOptInTests(unittest.TestCase):
    """The opt-in check itself, with no checkout: these run and pass whether A1_BASELINE_TREE is set or not."""
    def test_unset_or_empty_skips_the_rollback_tests(self):
        self.assertFalse(baseline_opted_in(''))
        self.assertIn(BASELINE_ENV, BASELINE_SKIP)
        self.assertEqual(getattr(RollbackTests, '__unittest_skip__', False), not baseline_opted_in(BASELINE_VALUE))
        if not baseline_opted_in(BASELINE_VALUE):
            self.assertEqual(RollbackTests.__unittest_skip_why__, BASELINE_SKIP)

    def test_only_spaces_is_an_opt_in_and_fails(self):
        self.assertTrue(baseline_opted_in('   '))                                 # not a skip
        self.assertEqual(baseline_problem('   '), 'is not an absolute path')

    def test_a_relative_path_fails(self):
        self.assertEqual(baseline_problem('tests'), 'is not an absolute path')

    def test_a_missing_folder_fails(self):
        missing = case_dir('a1r-missing') / 'not-there'
        self.assertEqual(baseline_problem(str(missing)), 'holds no common_room/store.py')

    def test_a_checkout_at_another_head_fails_and_the_message_names_the_way_out(self):
        folder = case_dir('a1r-other-head')                     # under this worktree's .checks: git reports this head
        (folder / 'common_room').mkdir()
        (folder / 'common_room' / 'store.py').write_text('# fixture\n', encoding='utf-8')
        problem = baseline_problem(str(folder))
        self.assertTrue(problem.startswith('has HEAD '), problem)
        self.assertNotIn(BASELINE_FULL + ',', problem)
        message = baseline_failure(str(folder), problem)
        for part in (BASELINE_ENV, repr(str(folder)), problem, 'Unset ' + BASELINE_ENV, BASELINE_FULL + ' checkout'):
            self.assertIn(part, message)


if __name__ == '__main__':
    unittest.main(argv=sys.argv[:1])
