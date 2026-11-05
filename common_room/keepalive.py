"""Keep one crew process alive: restart it whenever it exits, with backoff, and stop trying
after repeated quick refusals.

    python -m common_room.keepalive --name <label> [--max-quick-refusals 3] [--quick-seconds 30]
        [--reset-after 600] -- <python args...>

Runs ``[sys.executable] + <python args...>`` as the child (for example
``-X utf8 -B -m common_room.relay --actors ...``), with the child's stdout and stderr left
attached to keepalive's own (so a minimized "CRCREW ..." console window still shows whatever the
room, relay or worker itself prints). Whenever the child exits -- for any reason, including exit
code 0, since none of the room's --renew loop or a worker's --loop are ever meant to stop on their
own -- keepalive restarts it after a delay. The delay follows a fixed backoff ladder in seconds
(5, 15, 30, 60, 120, 300, capped at 300) that resets back to the start once a child has stayed up
for at least --reset-after seconds.

A child that exits with code 2 within --quick-seconds of being started is treated as a refusal --
bad configuration, another relay already holding this room, or a worker seat lock already held by
another process (see common_room.relay.main and the "deferred" exit code in
common_room.ollama_worker.EXIT_CODES). Restarting a genuine refusal on a tight loop would just
spin; after --max-quick-refusals such refusals in an unbroken row, keepalive gives up, prints one
line explaining why, and exits 2 itself without restarting again. A slow exit 2 (the child ran for
a while first) is not a refusal for this purpose and is restarted normally, resetting the count.

On Ctrl+C, SIGTERM, or (Windows only) SIGBREAK, keepalive asks its current child to stop, waits up
to ten seconds, kills it if it has not, and then exits. It never reads or parses a line of the
child's output -- only its exit code and how long it ran matter here. Every event keepalive itself
notices is written as one JSON object per line on stdout: at least {"name", "event"}, plus "pid",
"exit_code" and "next_delay" where they apply.

The Keepalive class takes its clock, sleep and spawn functions as constructor arguments so tests
never need a real subprocess, a real clock or a real sleep.
"""
import argparse
import json
import signal
import subprocess
import sys
import time

BACKOFF_STEPS = (5, 15, 30, 60, 120, 300)
DEFAULT_TERMINATE_GRACE = 10.0
DEFAULT_MAX_QUICK_REFUSALS = 3
DEFAULT_QUICK_SECONDS = 30.0
DEFAULT_RESET_AFTER = 600.0


class Keepalive(object):
    """Runs one child command over and over. See the module docstring for the policy."""

    def __init__(self, name, argv, max_quick_refusals=DEFAULT_MAX_QUICK_REFUSALS,
                 quick_seconds=DEFAULT_QUICK_SECONDS, reset_after=DEFAULT_RESET_AFTER,
                 terminate_grace=DEFAULT_TERMINATE_GRACE, clock=time.time, sleep=None, spawn=None,
                 log=None):
        if not argv:
            raise ValueError('Nothing to run: pass the child command after --.')
        if type(max_quick_refusals) is not int or max_quick_refusals < 1:
            raise ValueError('--max-quick-refusals must be a whole number of at least 1.')
        self.name = name
        self.argv = list(argv)
        self.max_quick_refusals = max_quick_refusals
        self.quick_seconds = float(quick_seconds)
        self.reset_after = float(reset_after)
        self.terminate_grace = float(terminate_grace)
        self.clock = clock
        self.sleep = sleep if sleep is not None else self._default_sleep
        self.spawn = spawn if spawn is not None else (lambda argv: subprocess.Popen(argv))
        self._sink = log if log is not None else self._print_line
        self._stopping = False
        self._proc = None

    # ---- logging: _log always adds "name" first, then hands every field to the sink ------
    def _print_line(self, **fields):
        print(json.dumps(fields, ensure_ascii=True), flush=True)

    def _log(self, **fields):
        payload = {'name': self.name}
        payload.update(fields)
        self._sink(**payload)

    # ---- stopping ------------------------------------------------------------------------
    def request_stop(self):
        """Safe to call from a signal handler: only ever sets a flag. The loop notices it on its
        next check (at most a couple hundred milliseconds later while a child is running, or
        within half a second while backing off) and does the actual terminating itself, never
        from inside the handler."""
        self._stopping = True

    def terminate_current(self):
        """Ask the currently-running child (if any) to stop, then wait up to terminate_grace
        seconds, then kill it. Safe to call more than once, and safe to call when no child is
        running."""
        proc = self._proc
        if proc is not None:
            self._terminate_and_wait(proc)

    def _terminate_and_wait(self, proc):
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=self.terminate_grace)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
        return proc.wait()

    def _wait_for_exit(self, proc):
        """Poll in short slices (never a single indefinite blocking wait) so a stop request set
        from a signal handler is noticed promptly no matter which instant the signal lands in."""
        while True:
            try:
                return proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                if self._stopping:
                    return self._terminate_and_wait(proc)

    # ---- the loop ------------------------------------------------------------------------
    def run(self):
        """Runs until request_stop() has been called (typically from a signal handler) or the
        quick-refusal budget runs out. Returns the process exit code keepalive itself should use:
        0 for a clean stop, 2 for "gave up after repeated refusals"."""
        backoff_index = 0
        quick_refusals = 0
        while not self._stopping:
            started_at = self.clock()
            proc = self.spawn(self.argv)
            self._proc = proc
            pid = getattr(proc, 'pid', None)
            self._log(event='started', pid=pid)
            exit_code = self._wait_for_exit(proc)
            self._proc = None
            if self._stopping:
                self._log(event='stopped', pid=pid, exit_code=exit_code)
                return 0
            ran_for = self.clock() - started_at
            quick_refusal = exit_code == 2 and ran_for <= self.quick_seconds
            quick_refusals = quick_refusals + 1 if quick_refusal else 0
            self._log(event='exited', pid=pid, exit_code=exit_code, ran_for=ran_for,
                       quick_refusal=quick_refusal)
            if quick_refusal and quick_refusals >= self.max_quick_refusals:
                message = ('%d quick exit-2 refusals in a row (each within %ss of starting); '
                           'not restarting again.' % (quick_refusals, _fmt(self.quick_seconds)))
                self._log(event='giving_up', pid=pid, exit_code=exit_code, message=message)
                print('common_room.keepalive[%s]: %s' % (self.name, message), file=sys.stderr)
                return 2
            if ran_for >= self.reset_after:
                backoff_index = 0
            delay = BACKOFF_STEPS[min(backoff_index, len(BACKOFF_STEPS) - 1)]
            backoff_index += 1
            self._log(event='restarting', pid=pid, next_delay=delay)
            self.sleep(delay)
        return 0

    # ---- default sleep: real time, but checked often enough to notice a stop request quickly
    def _default_sleep(self, seconds):
        remaining = float(seconds)
        while remaining > 0 and not self._stopping:
            step = remaining if remaining < 0.5 else 0.5
            time.sleep(step)
            remaining -= step


def _fmt(value):
    return ('%g' % value)


def _split_argv(argv):
    """Everything before the first "--" is keepalive's own flags; everything after is the child
    command. Done by hand (not via argparse) so the child's own flags -- which may themselves
    include "-h" or repeats of keepalive's own option names -- are never touched."""
    if '--' in argv:
        i = argv.index('--')
        return argv[:i], argv[i + 1:]
    return argv, []


def build_parser():
    parser = argparse.ArgumentParser(prog='python -m common_room.keepalive',
                                      description='Restart a crew child process with backoff; '
                                                   'stop after repeated quick config refusals.')
    parser.add_argument('--name', required=True, help='A short label for this child, used in every log line')
    parser.add_argument('--max-quick-refusals', type=int, default=DEFAULT_MAX_QUICK_REFUSALS,
                         help='Consecutive quick exit-2s before keepalive gives up (default %(default)s)')
    parser.add_argument('--quick-seconds', type=float, default=DEFAULT_QUICK_SECONDS,
                         help='An exit 2 this soon after starting counts as a refusal (default %(default)s)')
    parser.add_argument('--reset-after', type=float, default=DEFAULT_RESET_AFTER,
                         help='A child that ran at least this long resets the backoff ladder (default %(default)s)')
    return parser


def main(argv=None):
    raw = sys.argv[1:] if argv is None else list(argv)
    own_argv, child_argv = _split_argv(raw)
    parser = build_parser()
    args = parser.parse_args(own_argv)
    if not child_argv:
        parser.error('Nothing to run: pass the child command after --.')
    command = [sys.executable] + child_argv
    keepalive = Keepalive(args.name, command, max_quick_refusals=args.max_quick_refusals,
                           quick_seconds=args.quick_seconds, reset_after=args.reset_after)

    def _handle_signal(signum, frame):
        # Only ever set a flag here: see Keepalive.request_stop for why the actual terminating
        # happens in the main loop instead of inside the handler.
        keepalive.request_stop()

    signals = [signal.SIGINT, signal.SIGTERM]
    sigbreak = getattr(signal, 'SIGBREAK', None)  # Windows only
    if sigbreak is not None:
        signals.append(sigbreak)
    for sig in signals:
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            pass  # not available on this platform/thread; best effort

    return keepalive.run()


if __name__ == '__main__':
    sys.exit(main())
