"""RED-first tests for common_room.keepalive: restart-with-backoff, quick-refusal budget,
termination forwarding, and the new crew/autostart*.cmd files (existence and shape only --
never executed, per house rules)."""
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import case_dir

ROOT = Path(__file__).resolve().parents[1]

from common_room import keepalive


class FakeClock:
    """A mutable logical clock: advance() moves it forward; calling it reads the current time."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeProcess:
    """Stands in for subprocess.Popen: .wait() advances the shared clock by run_seconds (as if
    the child had actually run that long) then returns the given exit code. Never really spawns
    anything."""

    _next_pid = [1000]

    def __init__(self, clock, exit_code, run_seconds=0.0):
        self._clock = clock
        self._exit_code = exit_code
        self._run_seconds = run_seconds
        self.pid = FakeProcess._next_pid[0]
        FakeProcess._next_pid[0] += 1
        self.terminated = False
        self.killed = False

    def wait(self, timeout=None):
        self._clock.advance(self._run_seconds)
        return self._exit_code

    def poll(self):
        return self._exit_code

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class ScriptedFakeProcess:
    """A more faithful stand-in for subprocess.Popen than FakeProcess: wait(timeout) really
    behaves like a blocking wait with a timeout -- it raises subprocess.TimeoutExpired while the
    process has not exited, and only returns once it has, exactly as the real thing does. This
    is needed to test terminate_current()/_wait_for_exit() themselves (not just the run() loop),
    where the mutants for "kill after grace" and "stop polling" live. Time is a FakeClock so
    these tests are instant; wait() also takes a tiny real sleep (capped at 20ms) so a caller
    polling it in a background thread does not spin hot while waiting for a real-world event
    such as request_stop() from the main thread."""

    _next_pid = [5000]

    def __init__(self, clock, exit_code=0, run_seconds=None, ignore_terminate=False, kill_exit_code=-9):
        self._clock = clock
        self._exit_code = exit_code
        self._deadline = None if run_seconds is None else clock() + run_seconds
        self.ignore_terminate = ignore_terminate
        self.kill_exit_code = kill_exit_code
        self.pid = ScriptedFakeProcess._next_pid[0]
        ScriptedFakeProcess._next_pid[0] += 1
        self.terminated = False
        self.killed = False
        self._exited_code = None

    def _check_natural_exit(self):
        if self._exited_code is None and self._deadline is not None and self._clock() >= self._deadline:
            self._exited_code = self._exit_code

    def poll(self):
        self._check_natural_exit()
        return self._exited_code

    def terminate(self):
        self.terminated = True
        if not self.ignore_terminate and self._exited_code is None:
            self._exited_code = self._exit_code

    def kill(self):
        self.killed = True
        if self._exited_code is None:
            self._exited_code = self.kill_exit_code

    def wait(self, timeout=None):
        self._check_natural_exit()
        if self._exited_code is not None:
            return self._exited_code
        if timeout is None:
            # A real, un-killed process would block here forever; fail loudly and immediately
            # instead of actually hanging the test suite.
            raise AssertionError('wait() with no timeout would block forever: the process was '
                                 'never terminated or killed')
        time.sleep(min(timeout, 0.02))
        self._clock.advance(timeout)
        self._check_natural_exit()
        if self._exited_code is not None:
            return self._exited_code
        raise subprocess.TimeoutExpired(cmd='fake', timeout=timeout)


def make_keepalive(clock, spawns, sleep_calls, **kwargs):
    """spawns: an iterable of FakeProcess (or a callable list) consumed one per restart.
    sleep_calls: a list that records every delay passed to sleep()."""
    spawns = iter(spawns)

    def fake_spawn(argv):
        try:
            return next(spawns)
        except StopIteration:
            raise AssertionError('keepalive tried to spawn more children than the test expected: ' + repr(argv))

    def fake_sleep(seconds):
        sleep_calls.append(seconds)

    return keepalive.Keepalive('unit-test', ['fake', 'argv'], clock=clock, sleep=fake_sleep,
                                spawn=fake_spawn, **kwargs)


class BackoffTest(unittest.TestCase):
    def test_restart_after_crash_uses_growing_backoff(self):
        clock = FakeClock()
        sleeps = []
        procs = [FakeProcess(clock, 1, run_seconds=0.0) for _ in range(5)]
        ka = make_keepalive(clock, procs, sleeps)

        def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 4:
                ka.request_stop()

        ka.sleep = fake_sleep
        code = ka.run()
        self.assertEqual(code, 0)
        self.assertEqual(sleeps, [5, 15, 30, 60])

    def test_backoff_resets_after_a_long_run(self):
        clock = FakeClock()
        sleeps = []
        # Three quick crashes (backoff climbs to 5, 15, 30), then a child that runs past
        # reset_after (600s) before crashing: the next delay must drop back to 5, not 60.
        procs = [
            FakeProcess(clock, 1, run_seconds=0.0),
            FakeProcess(clock, 1, run_seconds=0.0),
            FakeProcess(clock, 1, run_seconds=0.0),
            FakeProcess(clock, 1, run_seconds=700.0),
            FakeProcess(clock, 1, run_seconds=0.0),
        ]
        ka = make_keepalive(clock, procs, sleeps, reset_after=600.0)

        def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 5:
                ka.request_stop()

        ka.sleep = fake_sleep
        code = ka.run()
        self.assertEqual(code, 0)
        self.assertEqual(sleeps, [5, 15, 30, 5, 15])


class QuickRefusalTest(unittest.TestCase):
    def test_three_quick_exit_two_refusals_stop_keepalive_and_no_fourth_restart(self):
        clock = FakeClock()
        sleeps = []
        spawn_calls = []

        def fake_spawn(argv):
            spawn_calls.append(argv)
            if len(spawn_calls) > 3:
                raise AssertionError('a fourth restart was attempted after three quick refusals')
            # Each "refusal" exits 2 almost immediately (well under the 30s quick window).
            return FakeProcess(clock, 2, run_seconds=1.0)

        ka = keepalive.Keepalive('unit-test', ['fake', 'argv'], clock=clock,
                                  sleep=lambda seconds: sleeps.append(seconds), spawn=fake_spawn,
                                  max_quick_refusals=3, quick_seconds=30.0)
        code = ka.run()
        self.assertEqual(code, 2)
        self.assertEqual(len(spawn_calls), 3)
        # A normal backoff sleep happens between attempt 1->2 and 2->3; giving up after the 3rd
        # returns immediately, with no further sleep and no 4th spawn.
        self.assertEqual(sleeps, [5, 15])

    def test_non_quick_exit_two_is_restarted_not_treated_as_refusal(self):
        clock = FakeClock()
        sleeps = []
        # Each exit-2 here runs for 40s first -- longer than the 30s quick window -- so it must
        # never count toward the quick-refusal budget, however many times it happens.
        procs = [FakeProcess(clock, 2, run_seconds=40.0) for _ in range(4)]
        ka = make_keepalive(clock, procs, sleeps, max_quick_refusals=3, quick_seconds=30.0)

        def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 4:
                ka.request_stop()

        ka.sleep = fake_sleep
        code = ka.run()
        self.assertEqual(code, 0)
        self.assertEqual(len(sleeps), 4)


class ExitZeroTest(unittest.TestCase):
    def test_exit_zero_is_logged_and_restarted(self):
        clock = FakeClock()
        sleeps = []
        events = []
        procs = [FakeProcess(clock, 0, run_seconds=0.0), FakeProcess(clock, 1, run_seconds=0.0)]
        ka = make_keepalive(clock, procs, sleeps, log=lambda **fields: events.append(fields))

        def fake_sleep(seconds):
            sleeps.append(seconds)
            ka.request_stop()

        ka.sleep = fake_sleep
        code = ka.run()
        self.assertEqual(code, 0)
        exit_events = [e for e in events if e.get('event') == 'exited']
        self.assertTrue(any(e.get('exit_code') == 0 for e in exit_events),
                         'an exit-0 child must still be logged: ' + repr(events))


class JsonLoggingTest(unittest.TestCase):
    def test_each_log_line_is_one_json_object_with_name(self):
        clock = FakeClock()
        sleeps = []
        lines = []
        procs = [FakeProcess(clock, 1, run_seconds=0.0)]
        ka = make_keepalive(clock, procs, sleeps, log=None)
        captured = []
        ka._sink = lambda **fields: captured.append(fields)

        def fake_sleep(seconds):
            sleeps.append(seconds)
            ka.request_stop()

        ka.sleep = fake_sleep
        ka.run()
        self.assertTrue(captured)
        for fields in captured:
            self.assertEqual(fields.get('name'), 'unit-test')
            json.dumps(fields)  # must always be JSON-serializable


class TerminationForwardedTest(unittest.TestCase):
    """The one real-process test: keepalive is launched for real, with a real child that just
    sleeps; SIGTERM to keepalive must reach the child too.

    M6 fix: the child command here must NOT repeat sys.executable -- common_room.keepalive.main()
    already builds the child argv as [sys.executable] + <everything after '-->'], so an extra
    sys.executable here made the real child argv `python3 python3 -c '...'`: the interpreter
    tried to run its own path as Python source and crashed in milliseconds with "source code
    cannot contain null bytes", well before SIGTERM was ever sent. That made every assertion
    below pass (or flakily pass) for the wrong reason -- the child was already gone, or about to
    be, regardless of whether keepalive forwards signals at all. See
    evidence/autonomy-2026-09-24/fix-cite/RED.txt for the old (buggy) argv's captured output and
    a run against a keepalive with its signal handlers disabled, proving this fixed version does
    fail when keepalive ignores the signal, unlike the old one."""

    def test_sigterm_stops_keepalive_and_its_child(self):
        proc = subprocess.Popen(
            [sys.executable, '-X', 'utf8', '-B', '-m', 'common_room.keepalive',
             '--name', 'kt-test', '--',
             '-c', 'import time; time.sleep(60)'],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
            # Windows: a process cannot catch TerminateProcess, so the stop keepalive can act on there is
            # Ctrl+Break (SIGBREAK), which reaches a whole process group: keepalive and its child.
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0,
        )
        child_pid = None
        try:
            q = queue.Queue()

            def reader():
                for line in iter(proc.stdout.readline, ''):
                    q.put(line)
                q.put(None)

            t = threading.Thread(target=reader, daemon=True)
            t.start()
            deadline = time.time() + 15
            while time.time() < deadline and child_pid is None:
                remaining = max(0.1, deadline - time.time())
                try:
                    line = q.get(timeout=remaining)
                except queue.Empty:
                    break
                if line is None:
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get('event') == 'started' and event.get('name') == 'kt-test':
                    child_pid = event.get('pid')
            self.assertIsNotNone(child_pid, 'keepalive never reported a started child in time')
            self.assertTrue(_pid_alive(child_pid), 'the child should be alive before SIGTERM')

            proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == 'nt' else signal.SIGTERM)
            exit_code = proc.wait(timeout=15)
            self.assertEqual(exit_code, 0)
            deadline = time.time() + 5
            while time.time() < deadline and _pid_alive(child_pid):
                time.sleep(0.1)
            self.assertFalse(_pid_alive(child_pid), 'the child should be gone once keepalive stopped')
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if proc.stdout is not None:
                proc.stdout.close()
            if child_pid is not None and _pid_alive(child_pid):
                try:
                    os.kill(child_pid, getattr(signal, 'SIGKILL', signal.SIGTERM))   # Windows: TerminateProcess
                except OSError:
                    pass


def _pid_alive(pid):
    if os.name == 'nt':                 # os.kill(pid, 0) there would send Ctrl+C, not probe the process
        import ctypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)):
                return True
            return code.value == 259                                # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class KillAfterGraceTest(unittest.TestCase):
    """K01 (VERIFY7B mutant survivor): terminate_current() must escalate to kill() once a child
    outlives terminate_grace after being asked to stop (a child that ignores SIGTERM)."""

    def test_terminate_current_kills_a_child_that_ignores_terminate(self):
        clock = FakeClock()
        proc = ScriptedFakeProcess(clock, exit_code=0, ignore_terminate=True, kill_exit_code=-9)
        ka = keepalive.Keepalive('unit-test', ['fake', 'argv'], clock=clock,
                                  sleep=lambda s: None, spawn=lambda argv: proc, terminate_grace=1.0)
        ka._proc = proc
        ka.terminate_current()
        self.assertTrue(proc.terminated, 'terminate() must be tried first')
        self.assertTrue(proc.killed, 'kill() must be called once the grace period elapses')

    def test_terminate_current_does_not_kill_a_child_that_stops_on_terminate(self):
        """Companion check: a child that DOES stop on terminate() must never be kill()ed."""
        clock = FakeClock()
        proc = ScriptedFakeProcess(clock, exit_code=0, ignore_terminate=False)
        ka = keepalive.Keepalive('unit-test', ['fake', 'argv'], clock=clock,
                                  sleep=lambda s: None, spawn=lambda argv: proc, terminate_grace=1.0)
        ka._proc = proc
        ka.terminate_current()
        self.assertTrue(proc.terminated)
        self.assertFalse(proc.killed)


class StopPollingTest(unittest.TestCase):
    """K04 (VERIFY7B mutant survivor): _wait_for_exit's polling loop must notice request_stop()
    while the child is still running, not only once the child exits on its own."""

    def test_wait_for_exit_notices_a_stop_request_while_the_child_still_runs(self):
        clock = FakeClock()
        proc = ScriptedFakeProcess(clock, exit_code=0, run_seconds=None)  # never exits by itself
        ka = keepalive.Keepalive('unit-test', ['fake', 'argv'], clock=clock,
                                  sleep=lambda s: None, spawn=lambda argv: proc, terminate_grace=1.0)
        result = {}

        def call():
            result['code'] = ka._wait_for_exit(proc)

        t = threading.Thread(target=call, daemon=True)
        t.start()
        time.sleep(0.1)
        self.assertTrue(t.is_alive(), 'the child looks like it is still running before stop is requested')
        ka.request_stop()
        t.join(timeout=3)
        self.assertFalse(t.is_alive(), '_wait_for_exit must notice a stop request instead of polling forever')
        self.assertTrue(proc.terminated)
        self.assertEqual(result.get('code'), proc._exited_code)


class SigbreakRegistrationTest(unittest.TestCase):
    """K02 (VERIFY7B mutant survivor): SIGBREAK only exists on Windows, so it cannot be sent in
    this Linux test environment. Instead this tests main()'s handler-registration logic itself,
    with signal.SIGBREAK patched in (simulating Windows) or absent (simulating POSIX)."""

    def _registered_signals(self, sigbreak_value):
        calls = []

        def fake_signal(sig, handler):
            calls.append(sig)

        with mock.patch.object(keepalive.signal, 'signal', side_effect=fake_signal), \
             mock.patch.object(keepalive.signal, 'SIGBREAK', sigbreak_value, create=True), \
             mock.patch.object(keepalive.Keepalive, 'run', return_value=0):
            code = keepalive.main(['--name', 'sigbreak-test', '--', '-c', 'pass'])
        self.assertEqual(code, 0)
        return calls

    def test_sigbreak_is_registered_when_present_windows_style(self):
        fake_sigbreak = 21  # a stand-in integer; only presence/absence on the signal module matters
        calls = self._registered_signals(fake_sigbreak)
        self.assertIn(fake_sigbreak, calls)
        self.assertIn(signal.SIGINT, calls)
        self.assertIn(signal.SIGTERM, calls)

    def test_sigbreak_is_not_registered_when_absent_posix_style(self):
        calls = self._registered_signals(None)
        self.assertNotIn(None, calls)
        self.assertEqual(sorted(calls), sorted([signal.SIGINT, signal.SIGTERM]))


class SplitArgvTest(unittest.TestCase):
    """K03 (VERIFY7B mutant survivor): _split_argv must split on the FIRST '--' only, so a '--'
    that is part of the child's own command line is left alone rather than being mistaken for
    keepalive's own separator (e.g. via index() being swapped for rindex() or similar)."""

    def test_splits_on_the_first_double_dash_even_when_the_child_argv_has_another(self):
        argv = ['--name', 'x', '--', 'child.py', '--', 'extra']
        own, child = keepalive._split_argv(argv)
        self.assertEqual(own, ['--name', 'x'])
        self.assertEqual(child, ['child.py', '--', 'extra'])

    def test_no_double_dash_puts_everything_in_own_argv(self):
        own, child = keepalive._split_argv(['--name', 'x'])
        self.assertEqual(own, ['--name', 'x'])
        self.assertEqual(child, [])


class BackoffSleepStopTest(unittest.TestCase):
    """K05 (VERIFY7B mutant survivor): the real _default_sleep must return promptly once
    request_stop() is called, rather than always sleeping the full requested duration."""

    def test_default_sleep_returns_promptly_once_stop_is_requested(self):
        ka = keepalive.Keepalive('unit-test', ['fake', 'argv'], clock=time.time)
        started = time.time()

        def sleeper():
            ka._default_sleep(5.0)

        t = threading.Thread(target=sleeper, daemon=True)
        t.start()
        time.sleep(0.1)
        ka.request_stop()
        t.join(timeout=3)
        elapsed = time.time() - started
        self.assertFalse(t.is_alive(), '_default_sleep must return once a stop is requested')
        self.assertLess(elapsed, 2.0, '_default_sleep did not stop promptly after request_stop()')


class CmdFilesTest(unittest.TestCase):
    """The launcher itself is never touched or run. These only read the new .cmd files this
    piece adds: crew/autostart.cmd, crew/autostart-install.cmd, crew/autostart-remove.cmd."""

    FILES = ['crew/autostart.cmd', 'crew/autostart-install.cmd', 'crew/autostart-remove.cmd']

    def _read(self, relpath):
        path = ROOT / relpath
        self.assertTrue(path.exists(), '%s must exist' % relpath)
        raw = path.read_bytes()
        return path, raw

    def test_files_exist_are_crlf_and_ascii(self):
        for relpath in self.FILES:
            path, raw = self._read(relpath)
            self.assertNotIn(b'\x00', raw)
            try:
                text = raw.decode('ascii')
            except UnicodeDecodeError as exc:
                self.fail('%s is not plain ASCII: %s' % (relpath, exc))
            lines = raw.split(b'\n')
            for i, line in enumerate(lines[:-1] if raw.endswith(b'\n') else lines):
                self.assertTrue(line.endswith(b'\r'),
                                 '%s line %d is not CRLF-terminated: %r' % (relpath, i + 1, line))
                self.assertNotIn(b'\r', line[:-1], '%s line %d has a stray CR' % (relpath, i + 1))

    def test_install_and_remove_reference_the_startup_folder(self):
        for relpath in ['crew/autostart-install.cmd', 'crew/autostart-remove.cmd']:
            _, raw = self._read(relpath)
            text = raw.decode('ascii')
            self.assertIn('Start Menu\\Programs\\Startup', text)

    def test_install_prompts_for_a_typed_yes(self):
        _, raw = self._read('crew/autostart-install.cmd')
        text = raw.decode('ascii').lower()
        self.assertIn('yes', text)
        self.assertIn('set /p', text)

    def test_remove_prompts_for_a_typed_yes(self):
        _, raw = self._read('crew/autostart-remove.cmd')
        text = raw.decode('ascii').lower()
        self.assertIn('yes', text)
        self.assertIn('set /p', text)

    def test_no_file_calls_schtasks(self):
        # The word may appear in an explanatory "rem" comment (contrasting a Startup entry with
        # a scheduled task); no line that could actually run must mention it.
        for relpath in self.FILES:
            _, raw = self._read(relpath)
            for line in raw.decode('ascii').splitlines():
                stripped = line.strip()
                if stripped[:3].lower() == 'rem':
                    continue
                self.assertNotIn('schtasks', stripped.lower(),
                                  '%s calls schtasks: %r' % (relpath, line))

    def test_autostart_sets_unattended_and_is_minimized(self):
        _, raw = self._read('crew/autostart.cmd')
        text = raw.decode('ascii')
        self.assertIn('CR_UNATTENDED=1', text)
        self.assertIn('/min', text)

    def test_no_startup_folder_or_schtasks_side_effect_is_possible_from_these_tests(self):
        """Every test in this class reaches the filesystem only through self._read(), which is
        Path.exists()/Path.read_bytes() and nothing else. Prove _read itself is inert (no
        subprocess, no write, no touching %APPDATA%, no schtasks) so running this whole class can
        never install or remove a real Startup entry, whatever the individual test bodies go on
        to assert about the bytes it returns."""
        import inspect
        source = inspect.getsource(CmdFilesTest._read)
        for banned in ('subprocess', 'os.system', 'schtasks', 'APPDATA', 'os.startfile', 'write'):
            self.assertNotIn(banned, source)


if __name__ == '__main__':
    unittest.main()
