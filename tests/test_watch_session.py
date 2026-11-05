"""`crew watch` holds ONE logged-in session for its whole life (task watch-session, plan turnons-2026-10-03).

Before: every poll (every 20 s) called client.connect(), a fresh /api/login, and the old session was never logged
out. A session lives up to 900 s after its last use and board.py refuses every login once 256 are live, so about
seven watching seats filled the room's table and locked every seat, and Shane's board, out (findings #1230, #1232,
#1233, ruling #1235).

Now: a watch connects once and reuses that session on every poll; it reconnects only when the room answers
unauthenticated (session expired, keys rotated, room restarted), re-reading this seat's credential file; it logs the
old session out before replacing it and logs out on exit (normal, Ctrl+C or error); a one-shot crew command logs out
at exit too. A failing logout never changes an exit code. board.py's 256 cap and 900 s window are untouched.

Everything runs against the suite's own in-process room (common_room.activation.start_room, loopback only), with fake
clocks and sleeps; the live sessions are counted straight from room.server.sessions.

RED note: on the base commit the 8-watcher test fails with the table growing by 8 every round (232 live sessions
after 29 rounds) and a one-shot command leaves 1 behind.
"""
import sys
from pathlib import Path
if __name__ == '__main__':          # runnable as a script too (tools/evidence.py runs `python tests/<file> .`)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import io, json, threading, unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import common_room.crew as crew
import common_room.crew_client as crew_client
from common_room.participant import Participant
from tests.helpers import case_dir

WATCHERS = ['watcher-%d' % n for n in range(1, 9)]
CONFIG = {
    'version': 1,
    'actors': [{'id': w, 'role': 'worker', 'label': 'Watcher', 'session': 'fixture-' + w} for w in WATCHERS]
              + [{'id': 'watch-reviewer', 'role': 'reviewer', 'label': 'Reviewer', 'session': 'fixture-r'}],
    'authorities': [{'id': 'watch-scope', 'members': ['keeper'] + WATCHERS + ['watch-reviewer'],
                     'scope': 'Fictional watch scope'}],
}
POLLS = 30


class FakeClock:
    """monotonic() and sleep() that move the same fake time; `hook(n)` runs inside the nth sleep, i.e. 'while the seat waited'."""
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


class WatchSessionBase(unittest.TestCase):
    def start_room(self, selected=None):
        from common_room.activation import start_room
        room = start_room(case_dir('watch-session'), enrollment=CONFIG, selected=selected or WATCHERS)
        self.addCleanup(room.close)
        return room

    def path(self, room, seat):
        return str(room.root / (seat + '-credential.json'))

    def live(self, room):
        with room.server.lock:
            return sorted(v[0].id for v in room.server.sessions.values())

    def watch_args(self, room, seat, polls=POLLS):
        # polls at t = 0, 20, ... : (polls - 1) sleeps of WATCH_POLL_SECONDS, then WAIT
        return crew.build_parser().parse_args(['--credential', self.path(room, seat), 'watch', '--max-wait',
                                               str((polls - 1) * crew.WATCH_POLL_SECONDS)])

    def run_watch(self, room, seat, hook=None, polls=POLLS, client=None):
        client = client or crew.CrewClient(self.path(room, seat))
        clock = FakeClock(hook)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = crew.run_watch(client, self.watch_args(room, seat, polls), sleep=clock.sleep, monotonic=clock.monotonic)
        self.assertEqual(err.getvalue(), '')
        return code, out.getvalue(), clock

    def counting_logins(self):
        logins = []
        real = Participant.login

        def login(participant):
            logins.append(participant.token)
            return real(participant)
        patcher = mock.patch.object(Participant, 'login', login)
        patcher.start()
        self.addCleanup(patcher.stop)
        return logins


class OneSessionPerWatcherTests(WatchSessionBase):
    def test_eight_watchers_over_thirty_polls_hold_at_most_eight_live_sessions(self):
        room = self.start_room()
        rounds = POLLS - 1
        arrived, released = threading.Barrier(len(WATCHERS) + 1), threading.Barrier(len(WATCHERS) + 1)
        codes, failures, samples = {}, [], []

        def watcher(seat):
            clock = FakeClock()
            args = self.watch_args(room, seat)

            def sleep(seconds):
                clock.sleep(seconds)
                arrived.wait(timeout=60)        # every watcher has just polled: the main thread counts the table
                released.wait(timeout=60)
            try:
                codes[seat] = crew.run_watch(crew.CrewClient(self.path(room, seat)), args, sleep=sleep, monotonic=clock.monotonic)
            except Exception as exc:            # a broken barrier or a refused poll must fail the test, not hang it
                failures.append((seat, repr(exc)))
                arrived.abort()
                released.abort()

        threads = [threading.Thread(target=watcher, args=(seat,), daemon=True) for seat in WATCHERS]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            for thread in threads:
                thread.start()
            try:
                for _ in range(rounds):
                    arrived.wait(timeout=60)
                    samples.append(self.live(room))
                    released.wait(timeout=60)
            except threading.BrokenBarrierError:
                pass
            for thread in threads:
                thread.join(60)
        self.assertEqual(failures, [])
        self.assertEqual(len(samples), rounds)
        self.assertEqual(codes, {seat: 3 for seat in WATCHERS})                    # every watch ended in WAIT
        self.assertLessEqual(max(len(s) for s in samples), len(WATCHERS), [len(s) for s in samples])
        self.assertTrue(all(s == sorted(WATCHERS) for s in samples), samples[-1])  # exactly one per watcher, every round
        self.assertEqual(self.live(room), [], 'every watch logs out when it ends')

    def test_a_watch_logs_in_once_for_all_its_polls(self):
        room = self.start_room(['watcher-1'])
        logins = self.counting_logins()
        sizes = []
        code, out, clock = self.run_watch(room, 'watcher-1', hook=lambda n: sizes.append(len(self.live(room))))
        self.assertEqual((code, out), (3, 'WAIT\n'))
        self.assertEqual(len(clock.sleeps), POLLS - 1)
        self.assertEqual(len(logins), 1)
        self.assertEqual(set(sizes), {1})

    def test_the_credential_is_read_once_for_a_whole_watch(self):
        room = self.start_room(['watcher-1'])
        real, calls = crew_client.load_credential, []

        def counting(path, root=crew_client.ROOT):
            calls.append(str(path))
            return real(path, root)
        with mock.patch.object(crew_client, 'load_credential', counting):
            code, out, clock = self.run_watch(room, 'watcher-1')
        self.assertEqual(code, 3)
        self.assertEqual(len(calls), 1)


class ReconnectTests(WatchSessionBase):
    def test_an_expired_session_reconnects_once_and_the_watch_keeps_going(self):
        room = self.start_room(['watcher-1'])
        logins = self.counting_logins()
        real, calls = crew_client.load_credential, []

        def counting(path, root=crew_client.ROOT):
            calls.append(str(path))
            return real(path, root)
        seen = {}

        def hook(n):
            if n == 5:                           # the room forgets every session while the seat waits
                with room.server.lock:
                    room.server.sessions.clear()
            if n == 6:                           # the poll after it has reconnected
                seen['after'] = self.live(room)
                seen['logins'] = len(logins)
        with mock.patch.object(crew_client, 'load_credential', counting):
            code, out, clock = self.run_watch(room, 'watcher-1', hook=hook)
        self.assertEqual((code, out), (3, 'WAIT\n'))
        self.assertEqual(len(clock.sleeps), POLLS - 1)                  # it kept polling to the end
        self.assertEqual(seen, {'after': ['watcher-1'], 'logins': 2})   # one reconnect, one live session
        self.assertEqual(len(logins), 2)
        self.assertEqual(len(calls), 2)                                 # the credential file was re-read for it

    def test_a_rotated_credential_is_picked_up_on_the_reconnect(self):
        room = self.start_room(['watcher-1'])
        old = json.loads((room.root / 'watcher-1-credential.json').read_text(encoding='utf-8'))['token']
        logins = self.counting_logins()
        seen = {}

        def hook(n):
            if n == 3:
                room.rotate('watcher-1')         # revokes the old key and writes the new credential file
            if n == 4:
                with room.server.lock:
                    seen['tokens'] = sorted(v[2] for v in room.server.sessions.values())
        code, out, clock = self.run_watch(room, 'watcher-1', hook=hook)
        new = json.loads((room.root / 'watcher-1-credential.json').read_text(encoding='utf-8'))['token']
        self.assertNotEqual(old, new)
        self.assertEqual((code, out), (3, 'WAIT\n'))
        self.assertEqual(logins, [old, new])                            # the second login used the re-read key
        self.assertEqual(seen['tokens'], [new])                         # and only the new session is alive

    def test_replacing_a_session_logs_the_old_one_out_first(self):
        room = self.start_room(['watcher-1'])
        client = crew.CrewClient(self.path(room, 'watcher-1'))
        for _ in range(3):
            client.connect()
            self.assertEqual(self.live(room), ['watcher-1'])
        client.logout()
        self.assertEqual(self.live(room), [])
        client.logout()                                                 # idempotent
        self.assertEqual(client.state()['server_time'] > 0, True)       # the next call simply connects again
        self.assertEqual(self.live(room), ['watcher-1'])
        client.logout()


class PollFailureTests(WatchSessionBase):
    """A poll keeps the timing it had: one try, then the watch loop's own back-off. CrewClient._call's three reconnect
    attempts with 2 s pauses (right for a write) stretched one poll against a down room from 2 s to 14 s (measured)."""
    def client_with_sleeps(self, room):
        client = crew.CrewClient(self.path(room, 'watcher-1'))
        sleeps = []
        client.sleep = sleeps.append
        return client, sleeps

    def test_a_room_that_is_down_is_raised_at_once_with_no_retry_and_no_new_login(self):
        room = self.start_room(['watcher-1'])
        logins = self.counting_logins()
        client, sleeps = self.client_with_sleeps(room)
        client.watch_state()                          # the watch holds its session
        room.close()                                  # the room goes down
        with self.assertRaises(crew_client.CrewError) as caught:
            client.watch_state()
        self.assertEqual(caught.exception.code, 'unavailable')
        self.assertEqual(sleeps, [])
        self.assertEqual(len(logins), 1)

    def test_unauthenticated_gets_exactly_one_reconnect_and_a_broken_credential_is_raised_at_once(self):
        room = self.start_room(['watcher-1'])
        client, sleeps = self.client_with_sleeps(room)
        client.watch_state()
        credential = room.root / 'watcher-1-credential.json'
        saved = credential.read_bytes()
        real, calls = crew_client.load_credential, []

        def counting(path, root=crew_client.ROOT):
            calls.append(str(path))
            return real(path, root)
        with room.server.lock:
            room.server.sessions.clear()              # the session is gone ...
        credential.unlink()                           # ... and the key file is missing while it is rewritten
        with mock.patch.object(crew_client, 'load_credential', counting):
            with self.assertRaises(crew_client.CrewError) as caught:
                client.watch_state()
        self.assertEqual(caught.exception.code, 'config')
        self.assertEqual(len(calls), 1)               # one reconnect attempt, no pauses
        self.assertEqual(sleeps, [])
        self.assertEqual(self.live(room), [])         # and nothing is left in the room's table
        credential.write_bytes(saved)                 # the file is back: the next poll just connects again
        self.assertGreater(client.watch_state()['server_time'], 0)
        self.assertEqual(self.live(room), ['watcher-1'])
        client.logout()


class LogoutOnExitTests(WatchSessionBase):
    def test_every_way_a_watch_can_end_logs_out(self):
        room = self.start_room(['watcher-1'])
        stop = room.root / 'STOP'
        cases = {
            'wait': (lambda: None, None, 3),
            'stop file': (lambda: stop.write_text('halt', encoding='utf-8'), 1, 4),
        }
        for name, (arm, at, want) in cases.items():
            with self.subTest(name):
                hook = (lambda n, arm=arm, at=at: arm() if n == at else None)
                code, out, clock = self.run_watch(room, 'watcher-1', hook=hook)
                self.assertEqual(code, want)
                self.assertEqual(self.live(room), [])
            if stop.exists():
                stop.unlink()

    def test_ctrl_c_logs_out_and_still_propagates(self):
        room = self.start_room(['watcher-1'])

        def hook(n):
            if n == 3:
                raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_watch(room, 'watcher-1', hook=hook)
        self.assertEqual(self.live(room), [])

    def test_an_error_ends_the_watch_logged_out(self):
        room = self.start_room(['watcher-1'])
        real, calls = Participant.state, []

        def state(participant):
            calls.append(1)
            if len(calls) == 3:                  # a refusal the watch does not retry (not down, not unauthenticated)
                raise ValueError('forbidden: A fixture refusal.')
            return real(participant)
        with mock.patch.object(Participant, 'state', state):
            with self.assertRaises(crew_client.CrewError) as caught:
                self.run_watch(room, 'watcher-1')
        self.assertEqual(caught.exception.code, 'forbidden')
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.live(room), [])

    def test_a_failing_logout_never_changes_the_exit_code(self):
        room = self.start_room(['watcher-1'])
        real = Participant._request

        def request(participant, path, payload=None):
            if path == '/api/logout':
                raise ValueError('Local room unavailable or response lost.')
            return real(participant, path, payload)
        with mock.patch.object(Participant, '_request', request):
            code, out, clock = self.run_watch(room, 'watcher-1')
        self.assertEqual((code, out), (3, 'WAIT\n'))

    def test_a_client_without_a_logout_still_runs_a_watch(self):
        class Bare:                              # the shape the older fake clients in test_crew_watch have
            actor_id = 'watcher-1'
            def __init__(self, path):
                self.path = Path(path)
            def state(self):
                return {'server_time': 5000.0, 'tasks': [], 'notes': []}
        room = self.start_room(['watcher-1'])
        code, out, clock = self.run_watch(room, 'watcher-1', client=Bare(self.path(room, 'watcher-1')))
        self.assertEqual((code, out), (3, 'WAIT\n'))


class OneShotTests(WatchSessionBase):
    def run_cli(self, room, seat, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = crew.main(['--credential', self.path(room, seat), '--json', *args])
        return code, out.getvalue(), err.getvalue()

    def test_a_one_shot_call_leaves_no_session_behind(self):
        room = self.start_room(['watcher-1', 'watcher-2'])
        for seat in ('watcher-1', 'watcher-2', 'watcher-1'):
            code, out, err = self.run_cli(room, seat, 'whoami')
            self.assertEqual(code, 0, err)
            self.assertEqual(self.live(room), [])
        code, out, err = self.run_cli(room, 'watcher-1', 'tasks')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.live(room), [])

    def test_a_failing_one_shot_also_logs_out(self):
        room = self.start_room(['watcher-1'])
        code, out, err = self.run_cli(room, 'watcher-1', 'show', 't' + '0' * 32)
        self.assertEqual(code, 1)
        self.assertEqual(self.live(room), [])

    def test_a_failing_logout_never_changes_a_one_shots_exit_code(self):
        room = self.start_room(['watcher-1'])
        real = Participant._request

        def request(participant, path, payload=None):
            if path == '/api/logout':
                raise ValueError('Local room unavailable or response lost.')
            return real(participant, path, payload)
        with mock.patch.object(Participant, '_request', request):
            code, out, err = self.run_cli(room, 'watcher-1', 'whoami')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['actor'], 'watcher-1')


if __name__ == '__main__':
    unittest.main(argv=sys.argv[:1])
