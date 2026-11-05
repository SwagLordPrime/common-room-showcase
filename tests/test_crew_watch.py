"""Behavioral tests for `crew watch` (common_room/crew.py: run_watch).

Written before the code, so the RED on the base commit is the missing `watch` subcommand
and `run_watch`, not a typo. Event cases run against a real room started in-process by
common_room.activation.start_room (loopback only); the clock and the sleeps are fake, so
a 9-minute watch runs in milliseconds. Nothing here calls a model or leaves loopback.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # lets `python tests/test_crew_watch.py` run from a tree root

import contextlib, io, json, unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import common_room.crew as crew
import common_room.crew_client as crew_client
from tests.helpers import case_dir
from tests.test_crew_cli import CrewCLIBase, CONFIG


class FakeClock:
    """monotonic() and sleep() that move the same fake time; `hook(n)` runs inside the nth sleep,
    i.e. 'while the seat was waiting' (another seat acts, a file appears)."""
    def __init__(self, hook=None):
        self.now = 1000.0
        self.sleeps = []
        self.hook = hook

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if self.hook is not None:
            self.hook(len(self.sleeps))
        self.now += seconds


class FakeClient:
    """Just enough CrewClient for run_watch: a credential path (for the STOP file), connect() and
    state(); `script` is a list of states or exceptions handed out one per state() call."""
    def __init__(self, folder, script, actor='crew-worker-a'):
        self.path = Path(folder) / (actor + '-credential.json')
        self.actor_id = actor
        self.script = list(script)
        self.connects = 0
        self.states = 0

    def connect(self):
        self.connects += 1
        return self.actor_id

    def state(self):
        self.states += 1
        item = self.script[min(self.states - 1, len(self.script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    def review_queue(self, state):
        return [t for t in state.get('tasks', []) if t['status'] == 'ready_for_review' and t.get('builder') != self.actor_id]


# The crew CLI test room plus the orchestrator seat, so a claude-room STOP note can be posted.
WATCH_CONFIG = {
    'version': 1,
    'actors': CONFIG['actors'] + [{'id': 'claude-room', 'role': 'worker', 'label': 'Orchestrator', 'session': 'fixture-o'}],
    'authorities': [dict(a, members=a['members'] + ['claude-room']) for a in CONFIG['authorities']],
}
WATCH_SEATS = ['crew-worker-a', 'crew-worker-b', 'crew-reviewer', 'claude-room']


def idle_state(notes=()):
    return {'server_time': 5000.0, 'tasks': [], 'notes': list(notes)}


class WatchBase(CrewCLIBase):
    def start_room(self, root=None, selected=None):
        from common_room.activation import start_room
        room = start_room(root or case_dir('crew-watch'), enrollment=WATCH_CONFIG, selected=selected or WATCH_SEATS)
        self.addCleanup(room.close)
        return room

    def watch(self, room, actor, *flags, hook=None):
        path = str(room.root / (actor + '-credential.json'))
        args = crew.build_parser().parse_args(['--credential', path, 'watch', *flags])
        client = crew.CrewClient(path)
        clock = FakeClock(hook)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = crew.run_watch(client, args, sleep=clock.sleep, monotonic=clock.monotonic)
        self.assertEqual(err.getvalue(), '')
        return code, out.getvalue(), clock

    def cli_ok(self, room, actor, *args):
        code, out, err = self.run_cli(room, actor, *args)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def one_line(self, text):
        self.assertTrue(text.endswith('\n') and text.count('\n') == 1, repr(text))
        self.assertLessEqual(len(text.strip()), 300)
        return text.strip()

    def worker_has_submitted(self, room, title='Fixture task'):
        task_id = self.intake(room, title=title)
        self.cli_ok(room, 'crew-worker-a', 'ack', task_id)
        self.cli_ok(room, 'crew-worker-a', 'claim', task_id, '--seconds', '600')
        self.cli_ok(room, 'crew-worker-a', 'submit', task_id, '--file', str(self.write_report()), '--summary', 'Done.')
        return task_id

    def token_of(self, room, actor):
        return json.loads((room.root / (actor + '-credential.json')).read_text(encoding='utf-8'))['token']


class WatchEventTests(WatchBase):
    def test_new_task_already_waiting_returns_at_once(self):
        room = self.start_room()
        task_id = self.intake(room, title='Build the thing')
        code, out, clock = self.watch(room, 'crew-worker-a')
        line = self.one_line(out)
        self.assertEqual(code, 0)
        self.assertTrue(line.startswith('TASK '), line)
        self.assertIn(task_id[:10], line)
        self.assertIn('Build the thing', line)
        self.assertEqual(clock.sleeps, [])

    def test_new_task_arriving_while_waiting_is_polled_every_20_seconds(self):
        room = self.start_room()
        seen = {}
        def hook(n):
            if n == 2:
                seen['id'] = self.intake(room, title='Late arrival')
        code, out, clock = self.watch(room, 'crew-worker-a', hook=hook)
        line = self.one_line(out)
        self.assertEqual(code, 0)
        self.assertTrue(line.startswith('TASK '), line)
        self.assertIn(seen['id'][:10], line)
        self.assertEqual(clock.sleeps, [20, 20])

    def test_other_seats_tasks_and_finished_work_do_not_wake_it(self):
        room = self.start_room()
        self.intake(room, to='crew-worker-b', title='Not yours')
        done = self.worker_has_submitted(room, title='Mine, already submitted')
        code, out, clock = self.watch(room, 'crew-worker-a', '--max-wait', '40')
        self.assertEqual((code, out.strip()), (3, 'WAIT'))

    def test_acknowledged_task_needs_a_claim(self):
        room = self.start_room()
        task_id = self.intake(room)
        self.cli_ok(room, 'crew-worker-a', 'ack', task_id)
        code, out, _ = self.watch(room, 'crew-worker-a')
        line = self.one_line(out)
        self.assertEqual(code, 0)
        self.assertTrue(line.startswith('TASK ') and 'acknowledged' in line, line)

    def test_answer_to_an_ask_wakes_the_asker(self):
        room = self.start_room()
        task_id = self.intake(room, title='Needs an answer')
        self.cli_ok(room, 'crew-worker-a', 'ack', task_id)
        self.cli_ok(room, 'crew-worker-a', 'claim', task_id, '--seconds', '600')
        self.cli_ok(room, 'crew-worker-a', 'ask', task_id, '--question', 'Which way?')
        def hook(n):
            if n == 1:
                self.cli_ok(room, 'keeper', 'decide', task_id, '--response', 'This way.')
        code, out, clock = self.watch(room, 'crew-worker-a', hook=hook)
        line = self.one_line(out)
        self.assertEqual(code, 0)
        self.assertTrue(line.startswith('ANSWER '), line)
        self.assertIn(task_id[:10], line)
        self.assertEqual(clock.sleeps, [20])

    def test_a_waiting_decision_alone_does_not_wake_the_asker(self):
        room = self.start_room()
        task_id = self.intake(room)
        self.cli_ok(room, 'crew-worker-a', 'ack', task_id)
        self.cli_ok(room, 'crew-worker-a', 'claim', task_id, '--seconds', '600')
        self.cli_ok(room, 'crew-worker-a', 'ask', task_id, '--question', 'Which way?')
        code, out, _ = self.watch(room, 'crew-worker-a', '--max-wait', '60')
        self.assertEqual((code, out.strip()), (3, 'WAIT'))

    def test_work_coming_back_from_review_wakes_the_builder(self):
        for accepted in (True, False):
            with self.subTest(accepted=accepted):
                room = self.start_room()
                task_id = self.worker_has_submitted(room, title='Reviewed work')
                def hook(n):
                    if n == 1:
                        self.cli_ok(room, 'crew-reviewer', 'review', task_id,
                                    '--accept' if accepted else '--reject', '--note', 'Checked.')
                code, out, clock = self.watch(room, 'crew-worker-a', hook=hook)
                line = self.one_line(out)
                self.assertEqual(code, 0)
                self.assertTrue(line.startswith('REVIEWED '), line)
                self.assertIn(task_id[:10], line)
                self.assertIn('accepted' if accepted else 'rejected', line)
                self.assertEqual(clock.sleeps, [20])

    # ---- DECIDE: a helper this seat sent is waiting on this seat's decision (level-triggered) ----

    def start_helper(self, room):
        """crew-worker-a holds a parent task and sends a helper to crew-worker-b, which claims it. Returns (parent, helper)."""
        parent = self.intake(room, title='Parent job')
        self.cli_ok(room, 'crew-worker-a', 'ack', parent)
        self.cli_ok(room, 'crew-worker-a', 'claim', parent, '--seconds', '900')
        helper = self.cli_ok(room, 'crew-worker-a', 'delegate', parent, '--to', 'crew-worker-b', '--title', 'Helper job',
                             '--body', 'Help.', '--acceptance', 'Helps.')['child_task_id']
        self.cli_ok(room, 'crew-worker-b', 'ack', helper)
        self.cli_ok(room, 'crew-worker-b', 'claim', helper, '--seconds', '900')
        return parent, helper

    def submit_and_reject(self, room, helper):
        self.cli_ok(room, 'crew-worker-b', 'submit', helper, '--file', str(self.write_report()), '--summary', 'Done.')
        self.cli_ok(room, 'crew-reviewer', 'review', helper, '--reject', '--note', 'Not good enough.')

    def test_a_helper_rejected_before_the_watch_started_wakes_its_sender_at_once(self):
        room = self.start_room()
        parent, helper = self.start_helper(room)
        self.submit_and_reject(room, helper)
        code, out, clock = self.watch(room, 'crew-worker-a')
        line = self.one_line(out)
        self.assertEqual(code, 0)
        self.assertTrue(line.startswith('DECIDE ' + helper[:10] + ' rejected: '), line)
        self.assertIn('Helper job', line)
        self.assertEqual(clock.sleeps, [])

    def test_a_helper_rejected_during_the_wait_wakes_its_sender(self):
        room = self.start_room()
        parent, helper = self.start_helper(room)
        def hook(n):
            if n == 1:
                self.submit_and_reject(room, helper)
        code, out, clock = self.watch(room, 'crew-worker-a', hook=hook)
        self.assertEqual(code, 0)
        self.assertTrue(self.one_line(out).startswith('DECIDE ' + helper[:10] + ' rejected: '), out)
        self.assertEqual(clock.sleeps, [20])

    def test_a_helper_that_asks_a_question_also_needs_its_senders_decision(self):
        room = self.start_room()
        parent, helper = self.start_helper(room)
        self.cli_ok(room, 'crew-worker-b', 'ask', helper, '--question', 'Which way?')
        code, out, _ = self.watch(room, 'crew-worker-a')
        line = self.one_line(out)
        self.assertEqual(code, 0)
        self.assertTrue(line.startswith('DECIDE ' + helper[:10] + ' question: '), line)

    def test_a_helper_at_two_strikes_is_the_owners_so_no_decide(self):
        room = self.start_room()
        parent, helper = self.start_helper(room)
        self.submit_and_reject(room, helper)
        self.cli_ok(room, 'crew-worker-a', 'decide', helper, '--response', 'Try again.')
        self.cli_ok(room, 'crew-worker-b', 'ack', helper)
        self.cli_ok(room, 'crew-worker-b', 'claim', helper, '--seconds', '900')
        self.submit_and_reject(room, helper)
        shown = self.cli_ok(room, 'crew-worker-a', 'show', helper)['task']
        self.assertEqual((shown['status'], shown['failed_attempts']), ('waiting_decision', 2))
        code, out, _ = self.watch(room, 'crew-worker-a', '--max-wait', '40')
        self.assertEqual((code, out.strip()), (3, 'WAIT'))

    def test_only_the_sender_is_woken_by_a_decide(self):
        room = self.start_room()
        parent, helper = self.start_helper(room)
        self.submit_and_reject(room, helper)
        code, out, _ = self.watch(room, 'crew-worker-b', '--max-wait', '40')
        self.assertEqual((code, out.strip()), (3, 'WAIT'))

    def test_a_decide_that_was_answered_stops_waking_the_sender_for_it(self):
        room = self.start_room()
        parent, helper = self.start_helper(room)
        self.submit_and_reject(room, helper)
        self.cli_ok(room, 'crew-worker-a', 'decide', helper, '--response', 'Try again.')
        code, out, _ = self.watch(room, 'crew-worker-a', '--max-wait', '40')
        self.assertEqual((code, out.strip()), (3, 'WAIT'))

    def test_review_waiting_wakes_a_reviewer_only_with_the_flag(self):
        room = self.start_room()
        task_id = self.worker_has_submitted(room, title='Please review me')
        code, out, clock = self.watch(room, 'crew-reviewer', '--reviews')
        line = self.one_line(out)
        self.assertEqual(code, 0)
        self.assertTrue(line.startswith('REVIEW '), line)
        self.assertIn(task_id[:10], line)
        self.assertEqual(clock.sleeps, [])
        code, out, _ = self.watch(room, 'crew-reviewer', '--max-wait', '40')
        self.assertEqual((code, out.strip()), (3, 'WAIT'))

    def test_review_arriving_while_a_reviewer_waits(self):
        room = self.start_room()
        seen = {}
        def hook(n):
            if n == 1:
                seen['id'] = self.worker_has_submitted(room, title='Arrives mid-wait')
        code, out, clock = self.watch(room, 'crew-reviewer', '--reviews', hook=hook)
        self.assertEqual(code, 0)
        self.assertIn(seen['id'][:10], self.one_line(out))

    def test_stop_note_after_the_watch_began_stops_it(self):
        room = self.start_room()
        def hook(n):
            if n == 2:
                self.cli_ok(room, 'keeper', 'note', 'crew-scope-one', 'STOP all work now, Shane is changing the plan.')
        code, out, clock = self.watch(room, 'crew-worker-a', hook=hook)
        line = self.one_line(out)
        self.assertEqual(code, 4)
        self.assertEqual(line, 'STOP STOP all work now, Shane is changing the plan.')
        self.assertEqual(clock.sleeps, [20, 20])

    def test_a_claude_room_stop_note_stops_it_too(self):
        room = self.start_room()
        def hook(n):
            if n == 1:
                self.cli_ok(room, 'claude-room', 'note', 'crew-scope-one', 'STOP the run, claude-room says so.')
        code, out, clock = self.watch(room, 'crew-worker-a', hook=hook)
        self.assertEqual((code, out.strip()), (4, 'STOP STOP the run, claude-room says so.'))

    def test_a_stop_note_from_a_worker_or_a_reviewer_is_ignored(self):
        room = self.start_room()
        def hook(n):
            if n == 1:
                self.cli_ok(room, 'crew-worker-b', 'note', 'crew-scope-one', 'STOP everything, says a worker.')
                self.cli_ok(room, 'crew-reviewer', 'note', 'crew-scope-one', 'STOP everything, says a reviewer.')
        code, out, clock = self.watch(room, 'crew-worker-a', '--max-wait', '60', hook=hook)
        self.assertEqual((code, out.strip()), (3, 'WAIT'))
        self.assertEqual(clock.sleeps, [20, 20, 20])

    def test_an_ignored_stop_note_does_not_hide_a_real_one_behind_it(self):
        room = self.start_room()
        def hook(n):
            if n == 1:
                self.cli_ok(room, 'crew-worker-b', 'note', 'crew-scope-one', 'STOP, a worker tries.')
            if n == 2:
                self.cli_ok(room, 'keeper', 'note', 'crew-scope-one', 'STOP, the owner means it.')
        code, out, clock = self.watch(room, 'crew-worker-a', hook=hook)
        self.assertEqual((code, out.strip()), (4, 'STOP STOP, the owner means it.'))

    def test_stop_prints_at_most_200_characters_of_the_note(self):
        room = self.start_room()
        def hook(n):
            self.cli_ok(room, 'keeper', 'note', 'crew-scope-one', 'STOP ' + 'x' * 3000)
        code, out, _ = self.watch(room, 'crew-worker-a', hook=hook)
        line = self.one_line(out)
        self.assertEqual(code, 4)
        self.assertTrue(line.startswith('STOP STOP xxx'))
        self.assertEqual(len(line), len('STOP ') + 200)

    def test_a_stop_note_from_before_the_watch_began_is_history(self):
        room = self.start_room()
        self.cli_ok(room, 'keeper', 'note', 'crew-scope-one', 'STOP yesterday, then carried on.')
        code, out, _ = self.watch(room, 'crew-worker-a', '--max-wait', '60')
        self.assertEqual((code, out.strip()), (3, 'WAIT'))

    def test_only_a_note_that_starts_with_the_word_stop_counts(self):
        room = self.start_room()
        def hook(n):
            if n == 1:
                self.cli_ok(room, 'keeper', 'note', 'crew-scope-one', 'Please do not STOP now.')
                self.cli_ok(room, 'keeper', 'note', 'crew-scope-one', 'STOPPED early, carry on.')
        code, out, _ = self.watch(room, 'crew-worker-a', '--max-wait', '60', hook=hook)
        self.assertEqual((code, out.strip()), (3, 'WAIT'))

    def test_stop_file_present_at_the_start(self):
        room = self.start_room()
        (room.root / 'STOP').write_text('Shane pressed the switch.\n' + 'y' * 500, encoding='utf-8')
        code, out, clock = self.watch(room, 'crew-worker-a')
        line = self.one_line(out)
        self.assertEqual(code, 4)
        self.assertTrue(line.startswith('STOP Shane pressed the switch.'), line)
        self.assertLessEqual(len(line), len('STOP ') + 200)
        self.assertEqual(clock.sleeps, [])

    def test_stop_file_appearing_while_waiting(self):
        room = self.start_room()
        def hook(n):
            if n == 2:
                (room.root / 'STOP').write_text('', encoding='utf-8')
        code, out, clock = self.watch(room, 'crew-worker-a', hook=hook)
        self.assertEqual((code, out.strip()), (4, 'STOP'))
        self.assertEqual(clock.sleeps, [20, 20])

    def test_stop_beats_a_waiting_task(self):
        room = self.start_room()
        self.intake(room)
        (room.root / 'STOP').write_text('halt', encoding='utf-8')
        code, out, _ = self.watch(room, 'crew-worker-a')
        self.assertEqual((code, out.strip()), (4, 'STOP halt'))

    def test_nothing_to_do_ends_in_wait_at_max_wait(self):
        room = self.start_room()
        code, out, clock = self.watch(room, 'crew-worker-a', '--max-wait', '60')
        self.assertEqual((code, out), (3, 'WAIT\n'))
        self.assertEqual(clock.sleeps, [20, 20, 20])

    def test_default_wait_is_540_and_the_cap_is_590(self):
        parse = crew.build_parser().parse_args
        base = ['--credential', 'x-credential.json', 'watch']
        self.assertEqual(parse(base).max_wait, 540)
        self.assertFalse(parse(base).reviews)
        self.assertTrue(parse(base + ['--reviews']).reviews)
        self.assertEqual(parse(base + ['--max-wait', '590']).max_wait, 590)
        for bad in ('591', '0', '-5', 'soon'):
            with self.subTest(bad=bad), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse(base + ['--max-wait', bad])

    def test_a_full_default_watch_spans_540_seconds(self):
        room = self.start_room()
        code, out, clock = self.watch(room, 'crew-worker-a')
        self.assertEqual(code, 3)
        self.assertEqual(sum(clock.sleeps), 540)
        self.assertTrue(all(s == 20 or s == 540 % 20 for s in clock.sleeps))

    def test_the_credential_is_read_once_per_watch_and_again_only_after_the_room_says_unauthenticated(self):
        # watch-session (ruling #1245): a watch holds ONE session, so one credential read for all its polls; the file is
        # read again only when the room answers unauthenticated (here: the room forgets every session mid-watch).
        room = self.start_room()
        real = crew_client.load_credential
        calls = []
        def counting(path, root=crew_client.ROOT):
            calls.append(str(path))
            return real(path, root)
        with mock.patch.object(crew_client, 'load_credential', counting):
            code, out, clock = self.watch(room, 'crew-worker-a', '--max-wait', '60')
        self.assertEqual(code, 3)
        self.assertGreaterEqual(len(clock.sleeps), 3)
        self.assertEqual(len(calls), 1)
        del calls[:]
        def forget(n):
            if n == 2:
                with room.server.lock:
                    room.server.sessions.clear()
        with mock.patch.object(crew_client, 'load_credential', counting):
            code, out, clock = self.watch(room, 'crew-worker-a', '--max-wait', '100', hook=forget)
        self.assertEqual(code, 3)
        self.assertGreaterEqual(len(clock.sleeps), 5)
        self.assertEqual(len(calls), 2)

    def test_output_never_carries_a_token(self):
        room = self.start_room()
        token = self.token_of(room, 'crew-worker-a')
        self.intake(room, title='Plain title')
        for hook, flags in ((None, ()), (None, ('--max-wait', '20'))):
            code, out, _ = self.watch(room, 'crew-worker-a', *flags, hook=hook)
            self.assertNotIn(token, out)
        (room.root / 'STOP').write_text('halt', encoding='utf-8')
        _, out, _ = self.watch(room, 'crew-worker-a')
        self.assertNotIn(token, out)

    def test_a_long_title_still_gives_one_short_line(self):
        room = self.start_room()
        self.intake(room, title=('long title ' * 11)[:120].strip(), body='b')
        code, out, _ = self.watch(room, 'crew-worker-a')
        line = self.one_line(out)
        self.assertEqual(code, 0)
        self.assertTrue(line.startswith('TASK '))

    def test_main_runs_watch_end_to_end(self):
        room = self.start_room()
        task_id = self.intake(room, title='Through main')
        code, out, err = self.run_cli(room, 'crew-worker-a', 'watch', json_mode=False)
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith('TASK ') and task_id[:10] in out, out)
        code, out, err = self.run_cli(room, 'crew-worker-a', 'watch', '--max-wait', '1', json_mode=False, extra=())
        self.assertEqual(code, 0)    # the task is still unclaimed, so it still needs the seat
        with mock.patch.object(crew, 'WATCH_POLL_SECONDS', 0.2):
            code, out, err = self.run_cli(room, 'crew-reviewer', 'watch', '--max-wait', '1', json_mode=False)
        self.assertEqual((code, out.strip()), (3, 'WAIT'))

    def test_json_mode_prints_one_object(self):
        room = self.start_room()
        task_id = self.intake(room)
        code, out, err = self.run_cli(room, 'crew-worker-a', 'watch')
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual((data['event'], data['exit_code']), ('TASK', 0))
        self.assertIn(task_id[:10], data['line'])

    def test_watch_is_listed_in_help(self):
        text = crew.build_parser().format_help()
        self.assertIn('watch', text)
        self.assertIn('--max-wait', crew.build_parser()._subparsers._group_actions[0].choices['watch'].format_help())


class WatchFailureTests(WatchBase):
    def folder(self):
        from tests.helpers import case_dir
        return case_dir('crew-watch-fake')

    def run_fake(self, client, *flags):
        args = crew.build_parser().parse_args(['--credential', str(client.path), 'watch', *flags])
        clock = FakeClock()
        out = io.StringIO()
        with redirect_stdout(out):
            code = crew.run_watch(client, args, sleep=clock.sleep, monotonic=clock.monotonic)
        return code, out.getvalue(), clock

    def test_room_down_after_three_minutes_of_failures(self):
        client = FakeClient(self.folder(), [crew_client.CrewError('unavailable', 'Local room unavailable.')])
        code, out, clock = self.run_fake(client)
        self.assertEqual((code, out), (5, 'ROOM DOWN\n'))
        elapsed = sum(clock.sleeps)
        self.assertGreaterEqual(elapsed, 180)
        self.assertLess(elapsed, 180 + 25)
        self.assertEqual(clock.sleeps[:4], [2, 4, 8, 16])           # backs off, then holds at the poll interval
        self.assertTrue(all(s <= 20 for s in clock.sleeps))

    def test_a_room_that_comes_back_is_not_down(self):
        down = crew_client.CrewError('unavailable', 'Local room unavailable.')
        client = FakeClient(self.folder(), [down, down, down, idle_state(), idle_state()])
        code, out, clock = self.run_fake(client, '--max-wait', '60')
        self.assertEqual((code, out), (3, 'WAIT\n'))

    def test_failures_must_be_unbroken_to_count(self):
        down = crew_client.CrewError('unavailable', 'Local room unavailable.')
        script = ([down] * 6 + [idle_state()]) * 12
        client = FakeClient(self.folder(), script)
        code, out, clock = self.run_fake(client, '--max-wait', '590')
        self.assertEqual(code, 3)

    def test_an_expired_key_or_missing_credential_is_retried_like_a_down_room(self):
        for code_name in ('unauthenticated', 'config'):
            with self.subTest(code_name):
                client = FakeClient(self.folder(), [crew_client.CrewError(code_name, 'try later')])
                code, out, _ = self.run_fake(client)
                self.assertEqual((code, out), (5, 'ROOM DOWN\n'))

    def test_a_credential_that_names_another_seat_is_not_retried(self):
        client = FakeClient(self.folder(), [crew_client.CrewError('identity', 'Credential identity mismatch.')])
        with self.assertRaises(crew_client.CrewError) as caught:
            self.run_fake(client)
        self.assertEqual(caught.exception.code, 'identity')
        self.assertEqual(client.states, 1)

    def test_stop_file_works_while_the_room_is_down(self):
        folder = self.folder()
        (folder / 'STOP').write_text('Room is being rebuilt.', encoding='utf-8')
        client = FakeClient(folder, [crew_client.CrewError('unavailable', 'Local room unavailable.')])
        code, out, clock = self.run_fake(client)
        self.assertEqual((code, out), (4, 'STOP Room is being rebuilt.\n'))
        self.assertEqual(client.states, 0)

    def test_the_loop_never_logs_in_by_itself(self):
        # watch-session (ruling #1245): the loop only polls. The client's own state() connects on first use and again
        # only after the room answers unauthenticated, so a poll adds no login and no credential read of its own.
        client = FakeClient(self.folder(), [idle_state()])
        code, out, clock = self.run_fake(client, '--max-wait', '60')
        self.assertEqual(code, 3)
        self.assertEqual(client.connects, 0)
        self.assertGreaterEqual(client.states, 3)

    def test_an_expired_lease_means_the_seat_must_claim_again(self):
        task = {'id': 't' + 'a' * 31, 'title': 'Lapsed', 'status': 'claimed', 'recipient': 'crew-worker-a',
                'builder': 'crew-worker-a', 'lease': {'holder': 'crew-worker-a', 'fence': 3, 'until': 4000.0}}
        client = FakeClient(self.folder(), [{'server_time': 5000.0, 'tasks': [task], 'notes': []}])
        code, out, _ = self.run_fake(client)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith('TASK ' + task['id'][:10]), out)

    def test_stop_notes_count_only_from_keeper_and_claude_room(self):
        quiet = idle_state(notes=[{'seq': 1, 'sender': 'keeper', 'body': 'earlier note'}])
        for sender, stops in (('keeper', True), ('claude-room', True), ('crew-worker-b', False),
                              ('crew-reviewer', False), ('hermes', False), ('astra-audit', False)):
            with self.subTest(sender=sender):
                loud = idle_state(notes=quiet['notes'] + [{'seq': 2, 'sender': sender, 'body': 'STOP now'}])
                client = FakeClient(self.folder(), [quiet, loud, loud, loud])
                code, out, clock = self.run_fake(client, '--max-wait', '60')
                self.assertEqual((code, out), (4, 'STOP STOP now\n') if stops else (3, 'WAIT\n'))

    def test_decide_needs_this_seat_as_sender_and_fewer_than_two_strikes(self):
        def task(sender, strikes, decision='Review requires an owner decision: nope'):
            return {'id': 't' + 'c' * 31, 'title': 'Helper', 'status': 'waiting_decision', 'recipient': 'crew-worker-b',
                    'sender': sender, 'builder': 'crew-worker-b', 'failed_attempts': strikes, 'decision': decision,
                    'lease': None}
        cases = ((task('crew-worker-a', 0), 'DECIDE'), (task('crew-worker-a', 1), 'DECIDE'),
                 (task('crew-worker-a', 2), 'WAIT'), (task('crew-worker-b', 1), 'WAIT'),
                 (task('crew-worker-a', 1, decision='A question?'), 'DECIDE'))
        for t, expected in cases:
            with self.subTest(sender=t['sender'], strikes=t['failed_attempts'], decision=t['decision']):
                client = FakeClient(self.folder(), [{'server_time': 5000.0, 'tasks': [t], 'notes': []}])
                code, out, _ = self.run_fake(client, '--max-wait', '20')
                self.assertEqual(out.split(' ')[0].strip(), expected)

    def test_a_lease_still_running_is_work_in_hand_not_new_work(self):
        task = {'id': 't' + 'b' * 31, 'title': 'Mine', 'status': 'claimed', 'recipient': 'crew-worker-a',
                'builder': 'crew-worker-a', 'lease': {'holder': 'crew-worker-a', 'fence': 3, 'until': 9000.0}}
        client = FakeClient(self.folder(), [{'server_time': 5000.0, 'tasks': [task], 'notes': []}])
        code, out, _ = self.run_fake(client, '--max-wait', '20')
        self.assertEqual((code, out), (3, 'WAIT\n'))


if __name__ == '__main__':
    unittest.main(argv=sys.argv[:1])    # evidence.py passes the tree root as an argument; it names no test
