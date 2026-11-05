"""Headless coding-agent runner for one Common Room worker seat (piece A of
BUILD_PLAN_COMMON_ROOM_AUTONOMY_2026-09-24.md).

One process acts as ONE worker seat (for example claude-code, claude-code-b or astra-room). It
works the room the way the local-model worker (ollama_worker.py) does: it reads the room as that
seat, acknowledges handoffs addressed to it, claims at most one, and has it done by a headless
coding agent - Claude Code (`claude -p`) or Codex (`codex exec`) - started from the command line
in the seat's config file. The agent never talks to the room: the runner writes TASK.md for it,
and afterwards turns exactly one of RESULT.md / QUESTION.md / BLOCKED.md into a finish (for a
SEPARATE reviewer) or a question for whoever assigned the task.

Laws kept here:
- Nothing starts by itself. This module acts only when someone runs it (--once or --loop), and the
  launcher keeps it out of its default seat list.
- All room access goes through CrewClient under this seat's own credential file. The agent's
  command line, environment and TASK.md never carry a key; COMMON_ROOM_SEAT is removed from the
  agent's environment so the crew CLI's --as guard does not open for it.
- Before any claim: the command must resolve on PATH, and the per-hour and rolling per-day run caps
  (counted in <room-runtime>/workers/<seat>-runs.json) must have room. Otherwise the task stays
  acknowledged, with a visible reason.
- A run has a hard timeout that kills the agent's whole process tree, and the lease is renewed
  while it runs. Timeouts, crashes, questions and blocks ask the owner and never strike. Only an
  explicit room refusal is an error, except the room's refusal of an outcome that holds a room key,
  which is recorded in the run folder (room-refusal.json) and asked about with that reason.
- Outcome files are read link-safely (no symbolic links, no hard links, regular files only), and
  text holding this seat's own key is never sent to the room.
- The agent never acts as another seat unnoticed: Claude seats deny the room's .runtime folder and every
  *-credential.json ({runtime_rule_root}); before and after each run the runner lists (names and lstat
  only) requests/ and archive/ of every relay seat folder and puts a WARNING on the finish or question
  when a file there is new or changed, or when it could not list them all.
- Stopping the runner (SIGTERM, SIGINT, SIGBREAK) kills the agent's tree first. Each run folder records
  the agent's pid and start time (agent.pid, written before the launch and completed right after it);
  while any run folder of a task holds a live agent, the runner defers that task instead of starting a
  second agent, even after a hard kill of the runner. A record without a usable start time holds a task
  back for at most timeout + 600 s.
- The runner's bookkeeping (seat lock, run times, status) lives in <room-runtime>/workers/ and
  never holds task content or keys.
"""
from pathlib import Path
import argparse, json, math, ntpath, os, posixpath, re, shutil, signal, stat, subprocess, sys, time

from . import usage
from .activation import read_json, safe_path, write_json
from .crew_client import ROOT, CrewClient, CrewError, RenewIds, check_credential_path, load_credential
from .ollama_worker import (STOP_REASON, stop_flag_present, BODY_LIMIT, QUESTION_LIMIT, SEAT_FILE, CentralTime, SeatLock, _iso, _print_line,
                            clean_text, seconds_text, short)
from .protocol import canonical, decode_request
from .relay import ACTOR_NAME as RELAY_SEAT         # the relay serves only folders named like a seat id

LEASE_SECONDS = 1800
RENEW_BELOW = 600                 # renew whenever less than this many seconds of the lease remain
SLICE_SECONDS = 15.0              # how often a running agent is looked at
MIN_TIMEOUT, MAX_TIMEOUT = 60, 14400
MAX_PER_HOUR, MAX_PER_DAY = 12, 48
REPORT_LIMIT, MIN_REPORT_LIMIT = 12000, 1000
ROOM_REQUEST_LIMIT = 16384
RESULT_READ_LIMIT = 1024 * 1024
NOTE_READ_LIMIT = 64 * 1024
AGENT_JSON_LIMIT = 4 * 1024 * 1024
OUTCOMES = ('RESULT.md', 'QUESTION.md', 'BLOCKED.md')
PLACEHOLDER = re.compile(r'\{([A-Za-z_][A-Za-z0-9_]*)\}')
PLACEHOLDERS = frozenset({'task_file', 'run_dir', 'workdir', 'runtime_rule_root'})
CONTROL_ANY = re.compile(r'[\x00-\x1f\x7f]')
# Characters a Windows .cmd shim (claude.cmd from npm) would reinterpret inside an argument.
SHIM_UNSAFE = re.compile(r'["%&|<>^\x00-\x1f]')
# Characters a gitignore-style permission rule (or the shim) would read as syntax, refused in {runtime_rule_root}.
RULE_UNSAFE = re.compile(r'[*?\[\]\\!#()"%&|<>^\x00-\x1f\x7f]')
AGENT_RECORD = 'agent.pid'
AGENT_RECORD_LIMIT = 4096
START_UNKNOWN = -1                # the process runs, but this platform cannot tell when it started
RECORD_MARGIN = 600               # a record with no usable start time counts for at most timeout + this many seconds
RECORD_SKEW = 60                  # ... and not at all when its file is dated more than this far ahead
REFUSAL_RECORD = 'room-refusal.json'
ROOM_KEY_REFUSAL = 'Room text may not contain a room key.'       # the room's own refusal text (board.KEY_REFUSAL)
RELAY_FOLDERS = ('requests', 'archive')
RELAY_SEAT_LIMIT = 64             # seat folders listed, counted after the seat-id filter
RELAY_FILE_LIMIT = 64 * 2000      # request and archive entries listed in all
SUBTYPE = re.compile(r'[a-z_]{1,40}')
NOTE_REASONS = frozenset({'executable_missing'})
TRANSIENT = frozenset({'unavailable', 'unauthenticated'})
EXIT_CODES = {'idle': 0, 'submitted': 0, 'asked': 0, 'deferred': 2, 'stopped': 2, 'error': 1}
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
SEAT_ENV = 'COMMON_ROOM_SEAT'
RETURN_KINDS = frozenset({'task_deliver', 'task_assign', 'task_resolve_decision', 'task_reinstate'})
ANSWER_KINDS = frozenset({'task_resolve_decision', 'task_reinstate'})
QUESTION_KINDS = frozenset({'task_wait_decision', 'task_review', 'task_fail', 'task_expired'})
REJECTION = 'Review requires an owner decision: '
NEXT_STEP = {
    'question': 'Answer it with resolve: the task comes back to this seat with your answer in its TASK.md.',
    'blocked': 'Resolve with guidance to run it again, or cancel the task.',
    'timeout': 'Look at the run folder (stdout.txt, stderr.txt), then resolve to run it again or cancel.',
    'crashed': 'Look at stderr.txt in the run folder, then resolve to run it again or cancel.',
    'nothing_written': 'Look at stdout.txt and stderr.txt in the run folder, then resolve to run it again or cancel.',
    'several_outcomes': 'Look at the run folder, then resolve to run it again or cancel.',
    'empty_outcome': 'Look at the run folder, then resolve to run it again or cancel.',
    'outcome_refused': 'Look at the run folder (the file must be a plain file), then resolve to run it again or cancel.',
    'key_in_outcome': 'Rotate this seat\'s key (room console: rotate SEAT), delete that file from the run folder, then '
                      'resolve or cancel.',
    'key_refused': 'Find whose room key that file in the run folder holds, rotate that key (room console: rotate '
                   '<seat>), delete the file, then resolve or cancel.',
    'interrupted': 'Check the run folder and that no agent is still running for it, then resolve to run it again.',
    'run_folder_refused': 'Check the runs folder is a plain, writable folder, then resolve to run it again.',
    'launch_failed': 'Check the command in this seat\'s crew/agent-*.json, then resolve to run it again.',
}


# -- Command line of the agent ------------------------------------------------------------------
def check_command(command):
    if type(command) is not list or not 1 <= len(command) <= 64:
        raise ValueError('command is a list of 1-64 strings: the program, then its arguments.')
    for index, arg in enumerate(command):
        if type(arg) is not str or not arg or len(arg) > 4096 or CONTROL_ANY.search(arg):
            raise ValueError('Each command item is non-empty text of at most 4096 characters, without control characters.')
        names = PLACEHOLDER.findall(arg)
        unknown = sorted(set(names) - PLACEHOLDERS)
        if unknown:
            raise ValueError('Unknown command placeholder {%s}; only {task_file}, {run_dir}, {workdir} and '
                             '{runtime_rule_root} are filled in.' % unknown[0])
        if index == 0 and names:
            raise ValueError('command[0] is the program itself and takes no placeholder.')
    if command[0].startswith('-'):
        raise ValueError('command[0] must name the program, not an option.')
    return list(command)


def fill(arg, values):
    return PLACEHOLDER.sub(lambda match: values[match[1]], arg)


def agent_environment(environ=None):
    """The runner's own environment minus COMMON_ROOM_SEAT (any letter case, as Windows would match it)."""
    source = os.environ if environ is None else environ
    return {key: value for key, value in source.items() if key.upper() != SEAT_ENV}


def launch_options(os_name=None):
    """A new process group (Windows) or session (POSIX), so the whole tree can be killed."""
    if (os_name or os.name) == 'nt':
        return {'creationflags': CREATE_NEW_PROCESS_GROUP}
    return {'start_new_session': True}


def taskkill_argv(pid, system_root=None):
    root = system_root or os.environ.get('SystemRoot') or 'C:\\Windows'
    return [ntpath.join(root, 'System32', 'taskkill.exe'), '/T', '/F', '/PID', str(int(pid))]


def kill_tree(proc, os_name=None, run=subprocess.run):
    """Kill the agent and everything it started in its group. Windows: taskkill /T /F /PID, then
    TerminateProcess as a fallback. POSIX: SIGKILL to the session's process group."""
    if (os_name or os.name) == 'nt':
        try:
            run(taskkill_argv(proc.pid), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=60, creationflags=CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            proc.kill()
        except OSError:
            pass
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class AgentJob:
    """Windows: a job object holding the agent and everything it starts, so ONE call ends the whole tree at once.
    taskkill /T has to walk the process tree first and takes about 6 s on this PC (measured), long enough for a
    short-lived agent to finish by itself before the kill lands (the known baseline failure of
    test_room_refusal_mid_run_kills_the_agent_and_is_an_error). Elsewhere, or when the job cannot be made or
    joined (handle None, error says why), kill_tree is used as before. The job never kills on close: only
    terminate() ends processes, so a runner that dies still leaves the agent as agent.pid records it."""

    PROCESS_TERMINATE, PROCESS_SET_QUOTA = 0x0001, 0x0100

    def __init__(self, proc, os_name=None):
        self.handle = None
        self.error = None
        if (os_name or os.name) != 'nt':
            return
        try:
            self._join(proc.pid)
        except Exception as exc:         # never stop a launch over the job: kill_tree still works
            self.handle = None
            self.error = '%s: %s' % (type(exc).__name__, str(exc)[:120])

    def _join(self, pid):
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32 = kernel32
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        process = kernel32.OpenProcess(self.PROCESS_TERMINATE | self.PROCESS_SET_QUOTA, False, pid)
        if not process:
            error = ctypes.WinError(ctypes.get_last_error())
            kernel32.CloseHandle(job)
            raise error
        joined = kernel32.AssignProcessToJobObject(job, process)
        error = ctypes.WinError(ctypes.get_last_error()) if not joined else None
        kernel32.CloseHandle(process)
        if error is not None:
            kernel32.CloseHandle(job)
            raise error
        self.handle = job

    def terminate(self):
        """End every process in the job. True when the job was asked to; False when there is no job to ask."""
        if self.handle is None:
            return False
        return bool(self._kernel32.TerminateJobObject(self.handle, 1))

    def close(self):
        handle, self.handle = self.handle, None
        if handle is not None:
            self._kernel32.CloseHandle(handle)


class Stopped(BaseException):
    """Raised by the runner's signal handler once it has killed the agent's process tree. A BaseException,
    like KeyboardInterrupt, so no `except Exception` on the way out swallows it."""

    def __init__(self, signum):
        super().__init__('stopped by signal %s' % signum)
        self.signum = int(signum)


def install_signal_handlers(runner, module=signal):
    """SIGTERM, SIGINT and (where it exists: Windows) SIGBREAK run runner._on_signal. Main thread only.
    Returns {signal number: previous handler}. A Windows TerminateProcess (what keepalive's terminate() is
    there) cannot be caught at all: see agent.pid for what still protects the task then."""
    previous = {}
    for name in ('SIGTERM', 'SIGINT', 'SIGBREAK'):
        number = getattr(module, name, None)
        if number is not None:
            previous[number] = module.signal(number, runner._on_signal)
    return previous


# -- Processes: is the agent still running? -----------------------------------------------------
def _linux_stat(pid):
    """{'state', 'ppid', 'start'} from /proc/<pid>/stat (start: clock ticks after boot), or None."""
    try:
        with open('/proc/%d/stat' % pid, 'rb') as handle:
            data = handle.read(4096)
    except OSError:
        return None
    try:
        fields = data.rsplit(b')', 1)[1].split()          # the command name in ( ) may hold spaces or ')'
        return {'state': fields[0].decode('ascii'), 'ppid': int(fields[1]), 'start': int(fields[19])}
    except (IndexError, ValueError, UnicodeDecodeError):
        return None


def _windows_process_start(pid, running_only=True):
    """Creation time (FILETIME ticks) of the running process with this pid, or None (none runs, it has
    exited, or it cannot be queried). running_only=False also answers for a process that has exited but whose
    process object still exists (the runner's own Popen handle keeps it, and its pid, until the runner closes it).
    Windows only, through ctypes; NOT run on Windows in this build."""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if running_only and (not kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                             or code.value != 259):                                             # STILL_ACTIVE
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return None
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        kernel32.CloseHandle(handle)


def process_start(pid, os_name=None):
    """When a process with this pid runs now: its start time (Linux: /proc; Windows: its creation time),
    START_UNKNOWN where it runs but the platform gives no start time (other POSIX), or None when no such
    process runs (a zombie has ended). Never os.kill on Windows, where that would terminate the process."""
    if type(pid) is not int or not 0 < pid < 2 ** 32:
        return None
    if (os_name or os.name) == 'nt':
        try:
            return _windows_process_start(pid)
        except (OSError, ValueError, AttributeError):
            return None
    info = _linux_stat(pid)
    if info is not None:
        return None if info['state'] in ('Z', 'X') else info['start']
    if os.path.isfile('/proc/self/stat'):
        return None                                           # Linux: no /proc entry, no process
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except OSError:
        return START_UNKNOWN
    return START_UNKNOWN


def launched_start(pid, os_name=None):
    """Start time of a process this runner has just started and NOT yet reaped, read right after Popen, so an agent
    that has already exited still gets one: Linux /proc keeps a zombie's entry (and its pid cannot be reused until the
    runner reaps it); Windows gives the creation time while the runner's Popen handle keeps the process object. None
    where the platform gives no start time (other POSIX) or the query fails."""
    if type(pid) is not int or not 0 < pid < 2 ** 32:
        return None
    if (os_name or os.name) == 'nt':
        try:
            return _windows_process_start(pid, running_only=False)
        except (OSError, ValueError, AttributeError):
            return None
    info = _linux_stat(pid)
    return None if info is None else info['start']


def linux_descendants(root_pid):
    """{pid: start} of every running process whose parent chain reaches root_pid, from /proc (Linux only;
    {} elsewhere). Seen while the agent runs, so a child that called setsid can still be found after the
    agent exits, when it has been reparented and no group or session leads to it."""
    try:
        names = os.listdir('/proc')
    except OSError:
        return {}
    table = {}
    for name in names:
        if name.isdigit():
            info = _linux_stat(int(name))
            if info is not None and info['state'] not in ('Z', 'X'):
                table[int(name)] = info
    found = {}
    for pid, info in table.items():
        parent, steps = info['ppid'], 0
        while parent != root_pid and parent in table and steps < 64:
            parent, steps = table[parent]['ppid'], steps + 1
        if parent == root_pid and pid != root_pid:
            found[pid] = info['start']
    return found


def read_agent_record(path):
    """(pid, start) from a run folder's agent.pid, read like an outcome file (plain, single-linked, no links
    followed). pid None is the record written just before the launch (no agent known yet); start None means no
    usable start time. Raises ValueError for anything this runner would not have written."""
    text, cut = read_outcome(path, AGENT_RECORD_LIMIT)
    if cut:
        raise ValueError('oversized agent record')
    try:
        value = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise ValueError('agent record is not JSON') from exc
    if type(value) is not dict or value.get('version') != 1:
        raise ValueError('not an agent record of this runner')
    pid, start = value.get('pid'), value.get('start')
    if pid is not None and (type(pid) is not int or not 1 < pid < 2 ** 32):
        raise ValueError('agent record pid out of range')
    if start is not None and (type(start) is not int or start < 0 or pid is None):
        raise ValueError('agent record start time invalid')
    return pid, start


# -- The room's relay folders (names only) and Claude Code path rules ---------------------------
def _plain_folder(path):
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not os.path.isjunction(path)


def relay_seat_label(name):
    """The seat a <room>/relay/<name> folder serves: the relay's seat-id pattern, compared without letter case
    because Windows opens relay\\Check-Reviewer as relay\\check-reviewer. None for any other name (the relay never
    serves it), so no folder name the agent made up reaches the room."""
    lowered = name.lower()
    return lowered if name.isascii() and RELAY_SEAT.fullmatch(lowered) else None


def relay_snapshot(room_root):
    """What the relay could execute, taken before and after each run: {'files': {seat folder: {(folder, name):
    (st_dev, st_ino, st_size, st_mtime_ns)}}, 'incomplete': bool} for requests/ and archive/ of every
    <room>/relay/<seat> whose name is a seat id and that is a plain folder. Names and lstat only: no file is opened,
    no link or junction is followed; names ending in .tmp are left out, as the relay ignores them. The caps
    (RELAY_SEAT_LIMIT seat folders, RELAY_FILE_LIMIT entries) apply after the seat-id filter; 'incomplete' is True
    when one was hit or a folder could not be listed, so the caller warns instead of staying silent."""
    relay = os.path.join(str(room_root), 'relay')
    snapshot = {'files': {}, 'incomplete': False}
    if not _plain_folder(relay):
        return snapshot
    seats = []
    try:
        with os.scandir(relay) as entries:
            for entry in entries:
                if (relay_seat_label(entry.name) is None or not entry.is_dir(follow_symlinks=False)
                        or entry.is_junction()):
                    continue
                if len(seats) >= RELAY_SEAT_LIMIT:
                    snapshot['incomplete'] = True
                    break
                seats.append(entry.name)
    except OSError:
        snapshot['incomplete'] = True
        return snapshot
    total = 0
    for seat in sorted(seats):
        files = snapshot['files'][seat] = {}
        for folder in RELAY_FOLDERS:
            path = os.path.join(relay, seat, folder)
            if not _plain_folder(path):
                continue
            try:
                with os.scandir(path) as entries:
                    for entry in entries:
                        if entry.name.endswith('.tmp'):
                            continue
                        if total >= RELAY_FILE_LIMIT:
                            snapshot['incomplete'] = True
                            return snapshot
                        try:
                            info = os.lstat(entry.path)
                        except OSError:
                            continue                      # gone since it was listed
                        files[(folder, entry.name)] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                        total += 1
            except OSError:
                snapshot['incomplete'] = True
    return snapshot


def relay_changes(before, after):
    """{'seats': {seat: count}, 'incomplete': bool}. Counted: every file in `after` that `before` did not hold at the
    same place (folder and name) with the same device, inode, size and modification time: a new name, a request
    written over an existing name, a file moved back into requests/. The relay executes each of these. Not counted:
    a file moved unchanged from requests/ into archive/, which is a chat tidying a request that was there before."""
    seats = {}
    for name, files in after['files'].items():
        old = before['files'].get(name, {})
        tidied = {key for place, key in old.items() if place[0] == 'requests' and place not in files}
        count = sum(1 for place, key in files.items()
                    if old.get(place) != key and not (place[0] == 'archive' and key in tidied))
        if count:
            label = relay_seat_label(name)
            seats[label] = seats.get(label, 0) + count
    return {'seats': seats, 'incomplete': bool(before['incomplete'] or after['incomplete'])}


def relay_warning(changes):
    changes = changes or {}
    counts, parts = changes.get('seats') or {}, []
    if counts:
        listed = sorted(counts)
        seats = ', '.join('%s (%d)' % (seat, counts[seat]) for seat in listed[:10])
        if len(listed) > 10:
            seats += ' and %d more folders' % (len(listed) - 10)
        parts.append('while the agent ran, %d new or changed request file(s) appeared in the relay requests or archive '
                     'folders of: %s. A chat on those seats may have written them, or the agent may have acted as '
                     'another seat through the relay.' % (sum(counts.values()), seats))
    if changes.get('incomplete'):
        parts.append('the runner could not list every relay folder (more than %d seat folders or %d request files, or '
                     'a folder it could not read), so requests written while the agent ran may be missing from this '
                     'check.' % (RELAY_SEAT_LIMIT, RELAY_FILE_LIMIT))
    if not parts:
        return ''
    return ('WARNING: %s Check the relay folders and the room history before accepting this work.\n\n'
            % ' Also, '.join(parts))


def runtime_rule_folder(credential, package_root):
    """The package's .runtime folder (.checks for fixtures) that holds this seat's credential: the room's keys,
    its relay request folders and the runners' own files are all under it."""
    credential = Path(credential).absolute()
    for name in ('.runtime', '.checks'):
        folder = Path(package_root).absolute() / name
        if credential.resolve().is_relative_to(folder.resolve()):
            return Path(os.path.normpath(str(folder)))
    raise ValueError('The credential is not inside the package .runtime or .checks folder.')


def claude_rule_path(folder, os_name=None):
    """A folder as an absolute path in a Claude Code permission rule: // then its POSIX form, a Windows
    D:\\x becoming //d/x (CLAIMED from Claude Code's docs). Refuses characters a rule or a .cmd shim would
    read as syntax."""
    text = str(folder)
    if (os_name or os.name) == 'nt':
        drive, rest = ntpath.splitdrive(ntpath.normpath(text))
        if not re.fullmatch(r'[A-Za-z]:', drive) or not rest.startswith('\\'):
            raise ValueError('Claude Code path rules need the room folder on a drive letter (D:\\...): %s' % text)
        value = '/' + drive[0].lower() + rest.replace('\\', '/')
    else:
        value = posixpath.normpath(text)
        if not value.startswith('/'):
            raise ValueError('The room folder path is not absolute: %s' % text)
    value = '//' + value.strip('/')
    if value == '//' or RULE_UNSAFE.search(value):
        raise ValueError('The room folder path cannot be written safely into a Claude Code rule (it is the root, or '
                         'holds one of * ? [ ] \\ ! # ( ) " % & | < > ^): ' + value)
    return value


# -- Outcome files and the report ---------------------------------------------------------------
class OutcomeRefused(ValueError):
    pass


OUTCOME_KINDS = {'RESULT.md': 'report', 'QUESTION.md': 'question', 'BLOCKED.md': 'blocked note'}


def room_key_refusal(exc):
    """True for the room's refusal of text that holds a room key (board.refuse_room_keys)."""
    return isinstance(exc, CrewError) and exc.code == 'forbidden' and ROOM_KEY_REFUSAL in str(exc)


def key_refused_text(name):
    return ("The agent's %s (%s) contained a room key and was refused by the room; nothing was sent."
            % (OUTCOME_KINDS.get(name, 'outcome file'), name))


def read_outcome(path, limit):
    """(text, truncated) from one outcome file the agent wrote. Only a regular file with one link
    is read, opened without following links, and at most `limit` bytes of it."""
    path = Path(path)
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise OutcomeRefused('could not be checked (%s)' % type(exc).__name__) from exc
    if stat.S_ISLNK(info.st_mode) or path.is_junction():
        raise OutcomeRefused('is a link (symbolic link or junction)')
    if not stat.S_ISREG(info.st_mode):
        raise OutcomeRefused('is not a regular file')
    if info.st_nlink != 1:
        raise OutcomeRefused('is hard-linked (link count %d)' % info.st_nlink)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    except OSError as exc:
        raise OutcomeRefused('could not be opened (%s)' % type(exc).__name__) from exc
    try:
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                or opened.st_nlink != 1):
            raise OutcomeRefused('changed while it was being checked')
        chunks, size = [], 0
        while size <= limit:
            chunk = os.read(descriptor, min(65536, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(descriptor)
    data = b''.join(chunks)
    text = data[:limit].decode('utf-8', 'replace')
    if text.startswith('\ufeff'):
        text = text[1:]
    return clean_text(text.replace('\r\n', '\n')), len(data) > limit


def agent_json_facts(data):
    """total_cost_usd and subtype, when stdout is one JSON object as `claude -p --output-format json` prints."""
    if not data or len(data) > AGENT_JSON_LIMIT:
        return {}
    try:
        value = json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return {}
    if type(value) is not dict:
        return {}
    facts = {}
    cost = value.get('total_cost_usd')
    if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
        facts['total_cost_usd'] = cost
    subtype = value.get('subtype')
    if type(subtype) is str and SUBTYPE.fullmatch(subtype):
        facts['subtype'] = subtype
    return facts


def fit_text(text, task_id, fence, limit=REPORT_LIMIT):
    """Returns (report, truncated): at most `limit` UTF-8 bytes, and its stage_text request fits the
    room's 16 KB limit; a cut is on a character boundary and says so."""
    def fits(value):
        if len(value.encode('utf-8')) > limit:
            return False
        envelope = {'operation': 'stage_text', 'payload': {'task_id': task_id, 'fence': fence, 'text': value}}
        return len(canonical(envelope).encode('utf-8')) <= ROOM_REQUEST_LIMIT
    if fits(text):
        return text, False
    marker = '\n\n[TRUNCATED by the runner at %d bytes]' % limit
    cut = text.encode('utf-8')[:max(0, limit - len(marker.encode('utf-8')))].decode('utf-8', 'ignore')
    if fits(cut + marker):
        return cut + marker, True
    marker += "\n[Cut further so the report's request fits the room's 16 KB limit.]"
    low, high = 0, len(cut)
    while low < high:
        middle = (low + high + 1) // 2
        if fits(cut[:middle] + marker):
            low = middle
        else:
            high = middle - 1
    return cut[:low] + marker, True


def returned_context(task, history):
    """What TASK.md carries when the task came back: the owner's answer and the question it
    answered (when the latest return to the seat was an answered decision), and the last review
    note (when the latest review was a rejection)."""
    events = sorted((e for e in history if e.get('task_id') == task['id']), key=lambda e: e.get('seq', 0))
    context = {'question': None, 'answer': None, 'answered_by': None, 'review_note': None}
    returns = [index for index, e in enumerate(events) if e.get('kind') in RETURN_KINDS]
    if returns and events[returns[-1]].get('kind') in ANSWER_KINDS:
        index = returns[-1]
        context['answer'] = task.get('decision_response') or events[index].get('detail') or None
        context['answered_by'] = events[index].get('actor')
        for event in reversed(events[:index]):
            if event.get('kind') in QUESTION_KINDS and event.get('detail'):
                context['question'] = event['detail']
                break
    reviews = [e for e in events if e.get('kind') == 'task_review']
    if reviews and str(reviews[-1].get('detail') or '').startswith(REJECTION) and task.get('review_note'):
        context['review_note'] = task['review_note']
    return context


def task_markdown(seat, task, fence, run_dir, workdir, context, report_limit):
    def block(value):
        return clean_text(str(value)).strip() or '(empty)'
    lines = ['# Common Room task for %s' % seat, '',
             'You are running headless for the Common Room worker seat %s. The runner that started you reports '
             'back to the room for this seat; you never do.' % seat, '',
             '- Seat: ' + seat, '- Task: ' + task['id'], '- Fence: %d' % fence, '- From: %s' % task.get('sender'),
             '- Title: ' + block(task['title']).replace('\n', ' '), '- Run folder: %s' % run_dir,
             '- Working folder: %s' % workdir, '',
             '## Body', '', block(task['body']), '',
             '## Acceptance', '', block(task['acceptance']), '']
    if context['answer']:
        lines += ['## Came back from a decision', '',
                  'Question: ' + block(context['question'] or 'not found in the room history (MISSING)'), '',
                  'Answer (from %s): %s' % (context['answered_by'] or 'the owner', block(context['answer'])), '']
    if context['review_note']:
        lines += ['## Last review note (a reviewer rejected the previous result)', '', block(context['review_note']), '']
    result, question, blocked = (run_dir / name for name in OUTCOMES)
    lines += ['## Rules', '',
              '1. Before anything else, read %s and %s and follow them.' % (workdir / 'AGENTS.md', workdir / 'CREW_BRIEF.md'),
              '2. Do only what this task asks.',
              '3. Never open, read, copy or print any file named *-credential.json, and nothing under any .runtime '
              'folder: they hold Common Room keys.',
              '4. Label every claim you write: MEASURED (you ran or counted it yourself), VERIFIED IN LOCAL SOURCE (you '
              'read it in a file; cite path:line), CLAIMED (a document or person says so; name the source), INFERENCE '
              '(your own reasoning), MISSING (you looked and could not find it). Cite files under the estate as '
              'Artifacts/... or Lab Notes/... with :N or #LN-LM, so the reviewer\'s citation check can verify them.',
              '5. Do not contact the Common Room yourself: no crew commands, relay files, board pages or requests to '
              'its address. The runner that started you does that for this seat.',
              '6. Save deliverables where the task says. If it names no place, save them in the run folder: %s' % run_dir
              + ' If you cannot write where the task asks, write it in the run folder and say where it should go.',
              '7. Finish by writing exactly one of these files, as a plain file (not a link), then stop:',
              '   - %s : the report a reviewer reads: what you did, where the deliverables are and how to check them. '
              'The room receives at most its first %d bytes.' % (result, report_limit),
              '   - %s : one question for whoever assigned the task, when you cannot go on without an answer.' % question,
              '   - %s : why the task cannot be done.' % blocked,
              '   Writing none of them, or more than one, sends nothing for review: the runner asks the owner instead.',
              '']
    return '\n'.join(lines)


# -- The runner ---------------------------------------------------------------------------------
def _check_folder(value, name, guarded, must_exist):
    if not isinstance(value, (str, Path)) or not str(value):
        raise ValueError('%s is a folder path.' % name)
    path = Path(os.path.normpath(str(Path(value).absolute())))
    if SHIM_UNSAFE.search(str(path)):
        raise ValueError('%s contains a character a Windows command shim could reinterpret (" %% & | < > ^ or a '
                         'control character): %s' % (name, path))
    if path.is_symlink() or path.is_junction():
        raise ValueError('%s must not be a link: %s' % (name, path))
    if must_exist and not path.is_dir():
        raise ValueError('%s must be an existing folder: %s' % (name, path))
    if os.path.lexists(path) and not path.is_dir():
        raise ValueError('%s exists and is not a folder: %s' % (name, path))
    resolved = path.resolve()
    for guard in guarded:
        guard = Path(guard).resolve()
        if resolved == guard or resolved.is_relative_to(guard):
            raise ValueError('%s must not be inside a room runtime folder (%s): the agent is never pointed at room '
                             'keys.' % (name, guard))
    return path


class Runner:
    """One headless-agent worker seat. run_once() is one wake-up; run_forever() is --loop."""

    def __init__(self, client, command, workdir, runs_dir, *, timeout=2700, max_runs_per_hour=2, max_runs_per_day=8,
                 report_limit=REPORT_LIMIT, clock=time.time, sleep=time.sleep, log=None, which=shutil.which,
                 renew_below=RENEW_BELOW, slice_seconds=SLICE_SECONDS, central=None):
        self.command = check_command(command)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT:
            raise ValueError('timeout is above 0 and at most %d seconds.' % MAX_TIMEOUT)
        for name, value, high in (('max_runs_per_hour', max_runs_per_hour, MAX_PER_HOUR),
                                  ('max_runs_per_day', max_runs_per_day, MAX_PER_DAY)):
            if type(value) is not int or not 1 <= value <= high:
                raise ValueError('%s is a whole number from 1 to %d.' % (name, high))
        if type(report_limit) is not int or not MIN_REPORT_LIMIT <= report_limit <= REPORT_LIMIT:
            raise ValueError('report_limit is a whole number of bytes from %d to %d.' % (MIN_REPORT_LIMIT, REPORT_LIMIT))
        if type(renew_below) not in (int, float) or not 0 <= renew_below < LEASE_SECONDS:
            raise ValueError('renew_below is 0 or more and below the %d-second lease.' % LEASE_SECONDS)
        if type(slice_seconds) not in (int, float) or not 0 < slice_seconds <= 60:
            raise ValueError('slice_seconds is above 0 and at most 60.')
        credential = getattr(client, 'path', None)
        seat = SEAT_FILE.fullmatch(Path(credential).name) if credential is not None else None
        if not seat:
            raise ValueError('The runner keeps its lock, run and status files beside its own seat credential; '
                             'give it a CrewClient for <seat>-credential.json.')
        package = getattr(client, 'root', ROOT)
        credential = check_credential_path(credential, package)
        self.runtime = credential.parent
        # {runtime_rule_root}: filled only when the command uses it (Claude seats' deny rules).
        self.rule_root = None
        if any('runtime_rule_root' in PLACEHOLDER.findall(arg) for arg in self.command):
            self.rule_root = claude_rule_path(runtime_rule_folder(credential, package))
        guarded = [self.runtime, Path(package) / '.runtime']
        self.workdir = _check_folder(workdir, 'workdir', guarded, must_exist=True)
        self.runs_dir = _check_folder(runs_dir, 'runs_dir', guarded, must_exist=False)
        self.client = client
        self.clock = clock
        self.sleep = sleep
        self.log = log or _print_line
        self.which = which
        self.timeout = timeout
        self.max_runs_per_hour = max_runs_per_hour
        self.max_runs_per_day = max_runs_per_day
        self.report_limit = report_limit
        self.renew_below = renew_below
        self.renew_ids = RenewIds()
        self.slice_seconds = slice_seconds
        self.central = central or CentralTime()
        self.command_name = Path(self.command[0]).name
        self.seat = seat[1]
        self.lock = None
        self.notes_posted = None
        self.actor = None
        self.last_run = None
        self.killed_on_stop = None       # pid of the agent running when a stop signal came (killed on the way out)
        self._proc = None                # the running agent, for the signal handler
        self._agent_job = None           # Windows job object holding the running agent's tree
        self._launching = False
        self._pending_signal = None
        self._descendants = {}

    # logging: one JSON line per event; never a token, credential content or task text
    def _log(self, event, **fields):
        record = {'ts': _iso(self.clock()), 'event': event, 'actor': self.actor}
        record.update(fields)
        self.log(json.dumps(record, ensure_ascii=True))

    # -- the seat's own runtime files: <room-runtime>/workers/ -----------------------------
    def _file(self, name):
        folder = safe_path(self.runtime, self.runtime / 'workers')
        folder.mkdir(exist_ok=True)
        folder = safe_path(self.runtime, folder)
        return safe_path(self.runtime, folder / name)

    def _take_seat(self):
        """None when this process now holds the seat; otherwise the cycle result to return."""
        try:
            path = self._file(self.seat + '.lock')
            lock = SeatLock(path)
            acquired = lock.acquire()
        except (OSError, ValueError) as exc:
            self._log('seat_lock_unavailable', message=short(exc))
            return {'status': 'error', 'code': 'seat_lock', 'reason': 'The seat lock could not be opened safely.'}
        if not acquired:
            self._log('seat_busy', message='Another worker process for seat %s holds %s; this process exits '
                                           'without acting.' % (self.seat, path))
            return {'status': 'deferred', 'reason': 'seat_busy'}
        self.lock = lock
        return None

    def _release_seat(self):
        lock, self.lock = self.lock, None
        if lock is not None and not lock.release():
            self._log('seat_lock_release_warning', message='Explicit unlock failed; closing the file released it.')

    def _run_times(self, now):
        """Run start times within the last 24 hours, kept in <seat>-runs.json so --once processes
        honor the caps too. None means the file is unusable: a full cap."""
        name = self.seat + '-runs.json'
        try:
            path = self._file(name)
        except (OSError, ValueError) as exc:
            self._log('run_state_unsafe', message=short(exc))
            return None
        try:
            value = read_json(path.parent, name, {'version': 1, 'runs': []})
            stamps = value.get('runs')
            if (value.get('version') != 1 or set(value) != {'version', 'runs'} or type(stamps) is not list
                    or len(stamps) > 4 * MAX_PER_DAY
                    or any(type(t) not in (int, float) or not math.isfinite(t) for t in stamps)):
                raise ValueError('unexpected run-state content')
        except (OSError, ValueError) as exc:
            self._log('run_state_corrupt', message=short(exc), action='counted as a full cap this cycle; file reset')
            try:
                write_json(path.parent, name, {'version': 1, 'runs': []})
            except (OSError, ValueError) as again:
                self._log('run_state_unwritable', message=short(again))
            return None
        return [t for t in stamps if abs(now - t) < 86400]

    def _cap_block(self, now):
        stamps = self._run_times(now)
        if stamps is None:
            return 'run_state_unusable'
        if len(stamps) >= self.max_runs_per_day:
            return 'day_cap'
        if sum(1 for t in stamps if abs(now - t) < 3600) >= self.max_runs_per_hour:
            return 'hour_cap'
        return None

    def _record_run(self, now):
        stamps = self._run_times(now)
        if stamps is None:
            return False
        name = self.seat + '-runs.json'
        try:
            write_json(self._file(name).parent, name, {'version': 1, 'runs': stamps + [round(now, 3)]})
        except (OSError, ValueError) as exc:
            self._log('run_state_unwritable', message=short(exc))
            return False
        return True

    def _status(self):
        try:
            path = self._file(self.seat + '-status.json')
            value = read_json(path.parent, path.name, {})
            return value if type(value) is dict else {}
        except (OSError, ValueError) as exc:
            self._log('status_unreadable', message=short(exc))
            return {}

    def _notes(self):
        if self.notes_posted is None:
            posted = self._status().get('notes_posted')
            self.notes_posted = [x for x in posted if type(x) is str][-50:] if type(posted) is list else []
        return self.notes_posted

    def _write_status(self, result):
        """<seat>-status.json: what Shane needs to see why a task sits; never tokens or task content."""
        now = self.clock()
        previous = self._status()
        last_run = self.last_run if self.last_run is not None else previous.get('last_run')
        status = {'version': 1, 'seat': self.seat, 'actor': self.actor, 'command': self.command_name, 'pid': os.getpid(),
                  'workdir': str(self.workdir), 'runs_dir': str(self.runs_dir), 'last_cycle_at': _iso(now),
                  'last_cycle_central': self.central.local(now).isoformat(timespec='minutes'),
                  'last_result': result.get('status'),
                  'last_reason': short(result.get('reason') or result.get('code') or '', 200) or None,
                  'last_task_id': result.get('task_id'),
                  'last_deferral': previous.get('last_deferral') if type(previous.get('last_deferral')) is dict else None,
                  'last_run': last_run if type(last_run) is dict else None,
                  'notes_posted': self._notes()[-50:]}
        if result.get('status') == 'deferred':
            status['last_deferral'] = {'reason': result.get('reason'), 'task_id': result.get('task_id'),
                                       'at': status['last_cycle_at']}
        try:
            write_json(self._file(self.seat + '-status.json').parent, self.seat + '-status.json', status)
        except (OSError, ValueError) as exc:
            self._log('status_write_failed', message=short(exc))

    def _deferral_note(self, task, reason):
        """At most one Lounge note per (task, reason, Central day), so a task sitting acknowledged
        has a visible reason. The request id is deterministic, so the room itself refuses a repeat."""
        if reason not in NOTE_REASONS:
            return
        day = self.central.local(self.clock()).date()
        request_id = 'note-%s-%s-%s' % (task['id'], reason.replace('_', '-'), day.strftime('%Y%m%d'))
        posted = self._notes()
        if request_id in posted:
            return
        body = ('%s cannot run task %s ("%s") yet: the command "%s" was not found (the runner looks it up on PATH '
                'before every run), so no agent was started and nothing was claimed. The task stays acknowledged with '
                'no strike, and the runner retries every cycle. Install it, or fix the command in this seat\'s '
                'crew/agent-*.json. (At most one such note per task, reason and Central day: %s.)'
                % (self.actor, task['id'], short(task['title'], 80), short(self.command[0], 200), day.isoformat()))
        try:
            self.client.command('note', {'authority_id': task['authority_id'], 'body': short(body, BODY_LIMIT)}, request_id)
            self._log('note_posted', task_id=task['id'], reason=reason, request_id=request_id)
        except CrewError as exc:
            if exc.code != 'idempotency':
                self._log('note_failed', task_id=task['id'], code=exc.code, message=short(exc))
                return
            self._log('note_already_posted', task_id=task['id'], request_id=request_id)
        posted.append(request_id)
        del posted[:-50]

    # -- cycles ----------------------------------------------------------------------------
    def run_once(self):
        held_here = self.lock is None
        if held_here:
            busy = self._take_seat()
            if busy is not None:
                self._log('cycle_result', **busy)
                return busy
        self.last_run = None
        try:
            try:
                result = self._cycle()
            except CrewError as exc:
                self._log('room_refused', code=exc.code, message=short(exc))
                result = {'status': 'error', 'code': exc.code, 'reason': 'room_refused'}
                if self.last_run is not None and self.last_run.get('outcome') == 'running':
                    self.last_run['outcome'] = 'room_refused'
            except Stopped as exc:
                if self.last_run is not None and self.last_run.get('outcome') == 'running':
                    self.last_run['outcome'] = 'stopped'
                self._write_status({'status': 'stopped', 'reason': 'signal %d' % exc.signum})
                raise
            except Exception as exc:
                self._write_status({'status': 'error', 'code': 'exception', 'reason': type(exc).__name__})
                raise
            self._meter(result)
            self._write_status(result)
            self._log('cycle_result', **result)
            return result
        finally:
            if held_here:
                self._release_seat()

    # -- the usage meter: one line per job, numbers and names only (USAGE_FORMAT.md) ---------------
    def _flag_value(self, name):
        for index, arg in enumerate(self.command):
            if arg == name and index + 1 < len(self.command):
                return self.command[index + 1]
            if arg.startswith(name + '='):
                return arg[len(name) + 1:]
        return None

    def _meter(self, result):
        """One usage line for a headless job that finished (submitted), asked the owner or ended badly.
        A question or blocked note from the agent is outcome question; every other ask (timeout, crash,
        nothing written, refused outcome), a room refusal mid-run, and a run the STOP flag killed are failed,
        the last with the seconds and exit code measured up to the kill. Deferrals ran no agent and write none;
        neither does a cycle that finds the flag up before any agent starts, nor a run cut short by an operating
        system stop signal (the runner itself is going down). budget_cap_usd is the command's own
        --max-budget-usd, so a CLAIMED cost the agent reports is never written here."""
        run = self.last_run or {}
        status, task_id = result.get('status'), result.get('task_id') or run.get('task_id')
        if not task_id:
            return
        if status == 'submitted':
            outcome = 'finished'
        elif status == 'asked':
            outcome = 'question' if result.get('reason') in ('question', 'blocked') else 'failed'
        elif status in ('error', 'stopped') and run:     # 'stopped' with a run: the STOP flag killed an agent that ran
            outcome = 'failed'
        else:
            return
        cap = self._flag_value('--max-budget-usd')
        try:
            cap = float(cap) if cap is not None else None
        except ValueError:
            cap = None
        usage.try_append(self.runtime, self.seat, {
            'ts': self.clock(), 'kind': 'headless', 'task_id': task_id,
            'model': self._flag_value('--model') or self.command_name,
            'seconds': run.get('wall_seconds') or 0.0, 'exit': run.get('exit_code'),
            'budget_cap_usd': cap, 'outcome': outcome}, self._log)

    def run_forever(self, poll=60, max_cycles=None):
        """Holds the seat for the whole loop. Returns early only when another process holds it."""
        busy = self._take_seat() if self.lock is None else None
        if busy is not None:
            self._log('cycle_result', **busy)
            return [busy]
        results, cycles = [], 0
        try:
            while True:
                try:
                    result = self.run_once()
                except Exception as exc:     # logged, never silent; the loop keeps its schedule
                    self._log('cycle_error', error=type(exc).__name__, message=short(exc))
                    result = {'status': 'error', 'code': 'exception', 'reason': type(exc).__name__}
                cycles += 1
                if max_cycles is not None:
                    results.append(result)
                    if cycles >= max_cycles:
                        return results
                self.sleep(poll)
        finally:
            self._release_seat()

    def _live_mine(self, task, room_now):
        lease = task.get('lease')
        return (task.get('status') == 'claimed' and bool(lease) and lease.get('holder') == self.actor
                and lease.get('until', 0) > room_now)

    def _stopped(self, task_id=None, **fields):
        """The result of a cycle that took no job (or ended the running one) because the STOP flag is present."""
        self._log('stopped', reason='STOP flag', task_id=task_id, **fields)
        result = {'status': 'stopped', 'reason': STOP_REASON}
        if task_id is not None:
            result['task_id'] = task_id
        return result

    def _cycle(self):
        if stop_flag_present(self.runtime):      # before anything: no ack, no claim, no agent
            return self._stopped()
        state = self.client.state()
        me = state.get('actor') or {}
        self.actor = me.get('id')
        if me.get('role') != 'worker':
            self._log('not_worker', role=me.get('role'))
            return {'status': 'error', 'code': 'not_worker', 'reason': 'This credential is not a worker seat.'}
        mine = [t for t in state.get('tasks', [])
                if t.get('kind') == 'handoff.record' and t.get('recipient') == self.actor
                and (t.get('status') in ('delivered', 'acknowledged')
                     or (t.get('status') == 'claimed' and (t.get('lease') or {}).get('holder') == self.actor))]
        if not mine:
            self._log('idle')
            return {'status': 'idle'}
        for index, task in enumerate(mine):
            if task['status'] == 'delivered':
                # The fence is part of the ID so a re-delivered task is not answered from an old receipt.
                request_id = 'ack-%s-f%d' % (task['id'], task['fence'])
                mine[index] = self.client.command('ack', {'task_id': task['id']}, request_id)['task']
                self._log('acknowledged', task_id=task['id'])
        live = [t for t in mine if self._live_mine(t, state['server_time'])]
        task = live[0] if live else next((t for t in mine if t['status'] in ('acknowledged', 'claimed')), None)
        if task is None:
            self._log('idle')
            return {'status': 'idle'}
        return self._work(task, state)

    def _deferred(self, task_id, reason, **fields):
        self._log('deferred', task_id=task_id, reason=reason, **fields)
        return {'status': 'deferred', 'reason': reason, 'task_id': task_id}

    def _hold(self, task, state):
        """Resume our own live lease, or claim at the current fence."""
        if self._live_mine(task, state['server_time']):
            fence = task['lease']['fence']
            self._log('resumed', task_id=task['id'], fence=fence)
            return fence
        request_id = 'claim-%s-f%d' % (task['id'], task['fence'])
        claimed = self.client.command('claim', {'task_id': task['id'], 'seconds': LEASE_SECONDS}, request_id)['task']
        lease = claimed.get('lease') or {}
        if lease.get('holder') != self.actor:
            raise CrewError('lease', 'The claim did not return a lease held by this seat.')
        self._log('claimed', task_id=task['id'], fence=lease['fence'])
        return lease['fence']

    def _ask(self, task_id, fence, question, reason):
        question = short(question, QUESTION_LIMIT)
        self.client.command('wait_decision', {'task_id': task_id, 'fence': fence, 'question': question},
                            'wait-%s-f%d' % (task_id, fence))
        self._log('asked_owner', task_id=task_id, fence=fence, reason=reason)
        return {'status': 'asked', 'reason': reason, 'task_id': task_id, 'fence': fence}

    def _facts(self, run_dir, code=None, wall=None, agent=None):
        parts = ['command %s' % self.command_name]
        if code is not None:
            parts.append('exit code %s' % code)
        if wall is not None:
            parts.append('wall time %s s' % seconds_text(wall))
        parts.append('run folder %s' % run_dir)
        agent = agent or {}
        if 'subtype' in agent:
            parts.append('agent result subtype %s' % agent['subtype'])
        if 'total_cost_usd' in agent:
            parts.append("total_cost_usd %s (as reported in the agent's JSON output; CLAIMED)"
                         % ('%.6f' % agent['total_cost_usd']).rstrip('0').rstrip('.'))
        return ', '.join(parts)

    def _ask_run(self, task_id, fence, reason, text, run_dir, code=None, wall=None, agent=None, relay=None):
        """Ask the owner about a run, with no strike: any relay WARNING first, the reason, then the run's facts."""
        tail = ('\n\n(Seat %s, headless run: %s. No strike was recorded. Needs Shane: %s)'
                % (self.actor, self._facts(run_dir, code, wall, agent), NEXT_STEP[reason].replace('SEAT', str(self.actor))))
        tail = short(tail, QUESTION_LIMIT - 200)
        head = short(clean_text(relay_warning(relay) + text).strip(), QUESTION_LIMIT - len(tail))
        if self.last_run is not None:
            self.last_run['outcome'] = reason
        try:
            return self._ask(task_id, fence, head + tail, reason)
        except CrewError as exc:
            # The agent's QUESTION.md or BLOCKED.md held a room key: ask again with none of the agent's text.
            if reason not in ('question', 'blocked') or not room_key_refusal(exc):
                raise
            return self._key_refused(task_id, fence, run_dir, 'QUESTION.md' if reason == 'question' else 'BLOCKED.md',
                                     dict(code=code, wall=wall, agent=agent, relay=relay))

    # -- the work --------------------------------------------------------------------------
    def _work(self, task, state):
        task_id = task['id']
        if stop_flag_present(self.runtime):
            return self._stopped(task_id)
        now = self.clock()
        resumed = self._live_mine(task, state['server_time'])
        program = self.which(self.command[0])
        if not program:
            self._deferral_note(task, 'executable_missing')
            return self._deferred(task_id, 'executable_missing', command=short(self.command[0], 200), claim_left=resumed)
        # A PATH entry like "." makes which() answer with a relative path, which the child would
        # otherwise resolve against the agent's workdir, not the runner's folder.
        program = os.path.abspath(program)
        blocked = self._cap_block(now)
        if blocked:
            return self._deferred(task_id, blocked, max_runs_per_hour=self.max_runs_per_hour,
                                  max_runs_per_day=self.max_runs_per_day, claim_left=resumed)
        try:
            seat_dir = self._seat_runs_dir()
        except (OSError, ValueError) as exc:
            return self._deferred(task_id, 'runs_dir_unusable', message=short(exc), claim_left=resumed)
        running = self._running_agent(task_id, seat_dir)
        if running is not None:
            return self._deferred(task_id, 'agent_still_running', pid=running[0], run_folder=running[1],
                                  claim_left=resumed)
        fence = self._hold(task, state)
        run_dir = seat_dir / ('%s-f%d' % (task_id, fence))
        if os.path.lexists(run_dir):
            refused = self._recorded_refusal(run_dir)
            if refused is not None:     # the room refused this run's outcome for a key; asking about it failed then
                return self._ask_run(task_id, fence, 'key_refused', key_refused_text(refused), run_dir)
            return self._ask_run(task_id, fence, 'interrupted',
                                 'A previous runner process for this seat stopped while this task was claimed at fence '
                                 '%d: its run folder already exists. The runner never starts a second agent on the same '
                                 'claim.' % fence, run_dir)
        if not self._record_run(now):
            return dict(self._deferred(task_id, 'run_state_unusable', claim_left=True, fence=fence), fence=fence)
        try:
            run_dir = self._make_run_dir(seat_dir, run_dir.name)
            task_file = run_dir / 'TASK.md'
            context = returned_context(task, state.get('history', []))
            with open(task_file, 'x', encoding='utf-8', newline='\n') as handle:
                handle.write(task_markdown(self.actor, task, fence, run_dir, self.workdir, context, self.report_limit))
        except (OSError, ValueError) as exc:
            return self._ask_run(task_id, fence, 'run_folder_refused',
                                 'The run folder could not be created safely (%s).' % short(exc, 300), run_dir)
        return self._run(task, fence, program, run_dir, task_file)

    def _seat_runs_dir(self):
        runs = self.runs_dir
        if not os.path.lexists(runs):
            runs.mkdir(parents=True, exist_ok=True)
        if runs.is_symlink() or runs.is_junction() or not runs.is_dir():
            raise ValueError('The runs folder is a link or not a folder: %s' % runs)
        seat_dir = safe_path(runs, runs / self.seat)
        seat_dir.mkdir(exist_ok=True)
        seat_dir = safe_path(runs, seat_dir)
        if not seat_dir.is_dir():
            raise ValueError('The seat runs folder is not a folder: %s' % seat_dir)
        return seat_dir

    def _make_run_dir(self, seat_dir, name):
        path = safe_path(self.runs_dir, seat_dir / name)
        path.mkdir()
        return safe_path(self.runs_dir, path)

    def _running_agent(self, task_id, seat_dir):
        """(pid, run folder name) when a run folder of this task, at any fence, records an agent that may still
        run; else None. A record with a start time counts while that pid runs with that same start time. A record
        with no usable start time (none recorded, or the platform gives none) counts only while its pid runs AND its
        file is younger than timeout + RECORD_MARGIN seconds; the record written just before a launch (no pid yet)
        counts while its file is that young. So no record blocks a task for ever. A record this runner would not
        have written (a link, a linked folder, bad content) is logged and ignored."""
        pattern = re.compile(re.escape(task_id) + r'-f\d{1,9}')
        try:
            names = sorted(os.listdir(seat_dir))
        except OSError as exc:
            self._log('agent_records_unlisted', task_id=task_id, message=short(exc))
            return None
        for name in names:
            if not pattern.fullmatch(name):
                continue
            folder = seat_dir / name
            record = folder / AGENT_RECORD
            try:
                if not _plain_folder(folder):
                    raise ValueError('the run folder is a link or not a folder')
                if not os.path.lexists(record):
                    continue
                pid, start = read_agent_record(record)
                age = time.time() - os.lstat(record).st_mtime
            except (OSError, ValueError) as exc:
                self._log('agent_record_ignored', task_id=task_id, run_folder=name, message=short(exc))
                continue
            young = -RECORD_SKEW < age < self.timeout + RECORD_MARGIN
            if pid is None:
                if young:
                    return None, name
                continue
            now = process_start(pid)
            if now is None:
                continue
            if start is not None and now != START_UNKNOWN:
                if now == start:
                    return pid, name
                continue
            if young:
                return pid, name
        return None

    def _write_agent_record(self, run_dir, value):
        temporary = run_dir / (AGENT_RECORD + '.tmp')
        with open(temporary, 'x', encoding='utf-8', newline='\n') as handle:
            handle.write(json.dumps(value))
        os.replace(temporary, run_dir / AGENT_RECORD)

    def _record_launching(self, run_dir):
        """<run folder>/agent.pid BEFORE the launch, with no pid yet: a runner killed between the launch and the
        full record still leaves a record that holds the task back (for timeout + RECORD_MARGIN seconds)."""
        self._write_agent_record(run_dir, {'version': 1, 'pid': None, 'start': None,
                                           'launched_at': _iso(self.clock())})

    def _record_agent(self, run_dir, proc):
        """<run folder>/agent.pid: the agent's pid and start time, written (temp file, then replace) right after
        launch, so no later runner starts a second agent on this task while this one runs, even when this
        runner is killed without warning. The start time is read before the runner reaps the agent, so an agent
        that has already exited has one too."""
        start = launched_start(proc.pid)
        value = {'version': 1, 'pid': proc.pid, 'start': start, 'launched_at': _iso(self.clock())}
        try:
            self._write_agent_record(run_dir, value)
        except OSError as exc:
            self._log('agent_record_failed', pid=proc.pid, message=short(exc))

    def _drop_launch_record(self, run_dir):
        """No agent was launched (the launch failed, or a refusal or a stop came first): the record written before
        the launch must not hold the task back."""
        try:
            os.unlink(run_dir / AGENT_RECORD)
        except FileNotFoundError:
            pass
        except OSError as exc:
            self._log('agent_record_failed', message=short(exc), action='the task waits out the record')

    def _recorded_refusal(self, run_dir):
        """The outcome file named in <run folder>/room-refusal.json (the room refused it for holding a room key), or
        None when there is no such record this runner would have written."""
        try:
            if not _plain_folder(run_dir) or not os.path.lexists(Path(run_dir) / REFUSAL_RECORD):
                return None
            text, cut = read_outcome(Path(run_dir) / REFUSAL_RECORD, AGENT_RECORD_LIMIT)
            value = json.loads(text)
        except (OSError, ValueError, RecursionError):
            return None
        if (cut or type(value) is not dict or value.get('version') != 1 or value.get('refused') != 'room_key'
                or value.get('file') not in OUTCOMES):
            return None
        return value['file']

    def _record_refusal(self, run_dir, name):
        try:
            temporary = Path(run_dir) / (REFUSAL_RECORD + '.tmp')
            with open(temporary, 'x', encoding='utf-8', newline='\n') as handle:
                handle.write(json.dumps({'version': 1, 'refused': 'room_key', 'file': name,
                                         'at': _iso(self.clock())}))
            os.replace(temporary, Path(run_dir) / REFUSAL_RECORD)
        except OSError as exc:
            self._log('refusal_record_failed', message=short(exc))

    def _key_refused(self, task_id, fence, run_dir, name, facts):
        """The room refused text taken from the agent's outcome file `name` because it holds a room key (any seat's
        or the owner's; this seat's own is caught before sending). Nothing of it reached the room: record that in
        the run folder, then ask the owner with that reason and none of the agent's text."""
        self._log('key_refused', task_id=task_id, fence=fence, file=name)
        self._record_refusal(run_dir, name)
        return self._ask_run(task_id, fence, 'key_refused', key_refused_text(name), run_dir, **facts)

    # -- stopping and the agent's processes ------------------------------------------------
    def _on_signal(self, signum, frame=None):
        """SIGTERM, SIGINT, SIGBREAK: stop this runner (Stopped). On the way out, _run kills and reaps the running
        agent's whole tree before anything else. A signal that arrives while the agent is being launched waits
        until the runner knows the agent's process."""
        if self._launching:
            self._pending_signal = signum
            return
        if self._proc is not None:
            self.killed_on_stop = self._proc.pid
        raise Stopped(signum)

    def _stop_if_signalled(self):
        signum, self._pending_signal = self._pending_signal, None
        if signum is not None:
            self._on_signal(signum)

    def _kill_agent(self, proc):
        # Look once more while the parent chain is intact: a child that left the agent's session since the last
        # 15 s look (or before the first) is found now and swept after the group dies.
        self._track(proc)
        if self._agent_job is None or not self._agent_job.terminate():
            kill_tree(proc)        # no job (not Windows, or it could not be made): the group kill as before
        self._sweep_descendants()

    def _track(self, proc):
        """Linux: remember the agent's descendants (pid and start time) while it runs."""
        if os.name != 'nt':
            self._descendants.update(linux_descendants(proc.pid))

    def _sweep_descendants(self):
        """Kill every remembered descendant that still runs with the same start time (a pid reused since is
        left alone): this reaches a child that left the agent's process group and session."""
        for pid, start in list(self._descendants.items()):
            if pid > 1 and pid != os.getpid() and process_start(pid) == start:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

    def _after_stop(self, task_id, fence, run_dir, code, wall):
        """The STOP flag killed the agent's whole tree. No strike is recorded and nothing is sent: the claim is
        left to lapse (there is no release command in the room, and fail would record a strike), which under
        release-v1 returns the task with no strike. The dead run's folder is renamed aside so the next cycle,
        even on the same live lease, starts a fresh run instead of asking about an interrupted one."""
        self.last_run.update(exit_code=code, wall_seconds=round(wall, 3), outcome='stopped')
        aside = None
        for number in range(1, 100):
            target = Path(run_dir).with_name('%s.stopped-%d' % (Path(run_dir).name, number))
            if os.path.lexists(target):
                continue
            try:
                os.rename(run_dir, target)
                aside = target.name
            except OSError as exc:
                self._log('run_folder_not_set_aside', message=short(exc),
                          action='the next cycle will ask the owner about an interrupted run')
            break
        return self._stopped(task_id, fence=fence, exit_code=code, wall_seconds=round(wall, 3), run_folder_set_aside=aside)

    def _relay_new(self, before, task_id, fence):
        changes = relay_changes(before, relay_snapshot(self.runtime))
        if changes['seats'] or changes['incomplete']:
            self._log('relay_requests_appeared', task_id=task_id, fence=fence, seats=changes['seats'],
                      count=sum(changes['seats'].values()), incomplete=changes['incomplete'])
        return changes

    def _keep_lease(self, task_id, fence):
        """The lease must still be this seat's at this fence; it is renewed when less than
        renew_below seconds remain. Returns the local monotonic time at which it ends."""
        state = self.client.state()
        seen = time.monotonic()
        task = self.client.task_in(state, task_id)
        lease = task.get('lease') or {}
        room_now = state['server_time']
        if (task.get('status') != 'claimed' or lease.get('holder') != self.actor or lease.get('fence') != fence
                or lease.get('until', 0) <= room_now):
            raise CrewError('lease', 'This worker lease is missing, expired or fenced out.')
        until = lease['until']
        if until - room_now < self.renew_below:
            request_id, renewed = self.renew_ids.renew(self.client, task_id, fence, LEASE_SECONDS)
            value = ((renewed.get('task') or {}).get('lease') or {}).get('until')
            until = value if type(value) in (int, float) else until
            self._log('renewed', task_id=task_id, fence=fence, request_id=request_id)
        return seen + (until - room_now)

    def _run(self, task, fence, program, run_dir, task_file):
        task_id = task['id']
        values = {'task_file': str(task_file), 'run_dir': str(run_dir), 'workdir': str(self.workdir)}
        if self.rule_root is not None:
            values['runtime_rule_root'] = self.rule_root
        argv = [program] + [fill(arg, values) for arg in self.command[1:]]
        lease_end = self._keep_lease(task_id, fence)
        streams = []
        try:
            for name in ('stdout.txt', 'stderr.txt'):
                streams.append(open(run_dir / name, 'xb+'))
        except OSError as exc:
            for stream in streams:
                stream.close()
            return self._ask_run(task_id, fence, 'run_folder_refused',
                                 'The run folder could not be prepared (%s).' % type(exc).__name__, run_dir)
        out, err = streams
        self.last_run = {'task_id': task_id, 'fence': fence, 'run_dir': str(run_dir), 'exit_code': None,
                         'wall_seconds': None, 'timed_out': False, 'outcome': 'running'}
        with out, err:
            self._write_status({'status': 'running', 'reason': 'agent_running', 'task_id': task_id})
            self._log('launching', task_id=task_id, fence=fence, command=self.command_name, run_dir=str(run_dir))
            relay_before = relay_snapshot(self.runtime)
            self._descendants = {}
            started = time.monotonic()
            proc = failure = None
            try:
                try:
                    self._record_launching(run_dir)          # agent.pid exists before the agent does
                except OSError as exc:
                    return self._ask_run(task_id, fence, 'run_folder_refused', 'The run folder could not be prepared '
                                         '(%s writing %s).' % (type(exc).__name__, AGENT_RECORD), run_dir)
                # A signal during the launch waits (_on_signal) until the agent's process is known here.
                self._launching = True
                try:
                    proc = subprocess.Popen(argv, cwd=str(self.workdir), stdin=subprocess.DEVNULL, stdout=out,
                                            stderr=err, env=agent_environment(), shell=False, **launch_options())
                    self._proc = proc
                    self._agent_job = AgentJob(proc)
                    if self._agent_job.error:
                        self._log('agent_job_unavailable', message=self._agent_job.error, fallback='taskkill')
                except (OSError, ValueError) as exc:
                    failure = exc
                finally:
                    self._launching = False
                if proc is not None:
                    self._record_agent(run_dir, proc)
                self._stop_if_signalled()
                if failure is not None:
                    return self._ask_run(task_id, fence, 'launch_failed', 'The command could not be started (%s: %s).'
                                         % (type(failure).__name__, short(failure, 300)), run_dir)
                timed_out = self._supervise(proc, task_id, fence, started, lease_end)
            finally:
                self._proc = None
                if proc is None:                   # never launched (refused, failed or stopped before it): no agent
                    self._drop_launch_record(run_dir)
                # Leaving early (a room refusal, a signal, Ctrl+C): the agent's whole tree dies first, then is reaped.
                elif proc.poll() is None:
                    self._kill_agent(proc)
                    try:
                        proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        pass
                relay = self._relay_new(relay_before, task_id, fence) if proc is not None else {}
                if self._agent_job is not None:
                    self._agent_job.close()
                    self._agent_job = None
            wall = time.monotonic() - started
            if timed_out != 'stop':
                out.seek(0)
                agent = agent_json_facts(out.read(AGENT_JSON_LIMIT + 1))
        if timed_out == 'stop':           # the STOP flag appeared: the agent's tree is gone, the task is left to resume
            return self._after_stop(task_id, fence, run_dir, proc.returncode, wall)
        self.last_run.update(exit_code=proc.returncode, wall_seconds=round(wall, 3), timed_out=timed_out)
        self._log('agent_exited', task_id=task_id, fence=fence, exit_code=proc.returncode, wall_seconds=round(wall, 3),
                  timed_out=timed_out, **agent)
        return self._conclude(task, fence, run_dir, proc.returncode, wall, timed_out, agent, relay)

    def _supervise(self, proc, task_id, fence, started, lease_end):
        """Waits for the agent in slices, renewing the lease and (Linux) noting its descendants; kills the
        tree on timeout. Any exception (a room refusal, a signal) leaves through _run, which kills the tree
        on the way out. Returns True when the run timed out."""
        while True:
            left = self.timeout - (time.monotonic() - started)
            if left <= 0:
                self._log('timeout', task_id=task_id, fence=fence, timeout=self.timeout)
                self._kill_agent(proc)
                proc.wait()
                return True
            try:
                proc.wait(timeout=min(self.slice_seconds, left))
                break
            except subprocess.TimeoutExpired:
                pass
            if stop_flag_present(self.runtime):
                self._log('stop_flag_seen', task_id=task_id, fence=fence)
                self._kill_agent(proc)
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    pass
                return 'stop'
            self._track(proc)
            if lease_end - time.monotonic() < self.renew_below:
                try:
                    lease_end = self._keep_lease(task_id, fence)
                except CrewError as exc:
                    if exc.code not in TRANSIENT:
                        raise
                    self._log('renew_retry', task_id=task_id, fence=fence, code=exc.code, message=short(exc))
        if os.name != 'nt':
            kill_tree(proc)          # anything the agent left running in its process group
        self._sweep_descendants()    # Linux: descendants seen during the run that left its group or session
        return False

    def _holds_own_key(self, text):
        try:
            token = load_credential(self.client.path, getattr(self.client, 'root', ROOT))['token']
        except (CrewError, OSError, ValueError) as exc:
            self._log('key_check_unavailable', code=getattr(exc, 'code', None))
            return False
        return type(token) is str and len(token) >= 16 and token in text

    def _conclude(self, task, fence, run_dir, code, wall, timed_out, agent, relay=None):
        task_id = task['id']
        facts = dict(code=code, wall=wall, agent=agent, relay=relay)
        present = [name for name in OUTCOMES if os.path.lexists(run_dir / name)]
        if timed_out:
            return self._ask_run(task_id, fence, 'timeout',
                                 'The headless run timed out after %s s; the runner killed its whole process tree.%s'
                                 % (seconds_text(self.timeout), ' It had written %s.' % ', '.join(present) if present else ''),
                                 run_dir, **facts)
        try:
            checked = safe_path(self.runs_dir, run_dir)
            if not checked.is_dir():
                raise ValueError('it is not a folder')
        except (OSError, ValueError) as exc:
            return self._ask_run(task_id, fence, 'run_folder_refused',
                                 'After the run the run folder was no longer a plain folder inside the runs folder (%s); '
                                 'nothing in it was read.' % short(exc, 200), run_dir, **facts)
        if not present:
            if code != 0:
                return self._ask_run(task_id, fence, 'crashed', 'The headless run ended with exit code %s and wrote none '
                                     'of RESULT.md, QUESTION.md or BLOCKED.md.' % code, run_dir, **facts)
            return self._ask_run(task_id, fence, 'nothing_written', 'The headless run ended with exit code 0 but wrote '
                                 'none of RESULT.md, QUESTION.md or BLOCKED.md.', run_dir, **facts)
        if len(present) > 1:
            return self._ask_run(task_id, fence, 'several_outcomes', 'The headless run wrote more than one outcome file '
                                 '(%s); exactly one is expected, so nothing was sent for review.' % ', '.join(present),
                                 run_dir, **facts)
        name = present[0]
        try:
            text, cut = read_outcome(run_dir / name, RESULT_READ_LIMIT if name == 'RESULT.md' else NOTE_READ_LIMIT)
        except OutcomeRefused as exc:
            return self._ask_run(task_id, fence, 'outcome_refused', "The agent's %s was not read: it %s. The runner reads "
                                 'only plain, single-linked files, so nothing from it was sent.' % (name, exc),
                                 run_dir, **facts)
        if not text.strip():
            return self._ask_run(task_id, fence, 'empty_outcome', 'The headless run wrote an empty %s.' % name,
                                 run_dir, **facts)
        if self._holds_own_key(text):
            self._log('key_in_outcome', task_id=task_id, fence=fence, file=name)
            return self._ask_run(task_id, fence, 'key_in_outcome', "The agent's %s contains this seat's own room key, "
                                 'so nothing from it was sent to the room.' % name, run_dir, **facts)
        if name == 'QUESTION.md':
            return self._ask_run(task_id, fence, 'question', text, run_dir, **facts)
        if name == 'BLOCKED.md':
            return self._ask_run(task_id, fence, 'blocked', 'Blocked: ' + text.strip(), run_dir, **facts)
        try:
            return self._submit(task, fence, run_dir, text, cut, code, wall, agent, relay)
        except CrewError as exc:
            if not room_key_refusal(exc):
                raise
            return self._key_refused(task_id, fence, run_dir, name, facts)

    def _submit(self, task, fence, run_dir, text, read_cut, code, wall, agent, relay=None):
        task_id = task['id']
        header = ('# Headless agent result (unreviewed)\n\n'
                  'Seat %s ran %s headless for task %s (fence %d): exit code %s, wall time %s s.\n'
                  'Run folder: %s\n'
                  "Below is the agent's RESULT.md. It is not accepted work until a separate reviewer checks it.\n\n"
                  '---\n\n' % (self.actor, self.command_name, task_id, fence, code, seconds_text(wall), run_dir))
        report, truncated = fit_text(clean_text(header + text), task_id, fence, self.report_limit)
        truncated = truncated or read_cut
        ref = self.client.handoff('stage_text', {'task_id': task_id, 'fence': fence, 'text': report})
        ref = {'path': ref['path'], 'sha256': ref['sha256']}
        size = len(report.encode('utf-8'))
        body = relay_warning(relay) + (
               'Headless run by %s: %s. Report %s, %d bytes%s. Unreviewed: a separate reviewer checks it against the '
               'acceptance: %s' % (self.actor, self._facts(run_dir, code, wall, agent), ref['path'], size,
                                    '; truncated by the runner at the %d-byte report limit' % self.report_limit
                                    if truncated else '', short(task['acceptance'], 800)))
        self.client.command('finish', {'task_id': task_id, 'fence': fence, 'body': short(body, BODY_LIMIT), 'refs': [ref]},
                            'finish-%s-f%d' % (task_id, fence))
        if self.last_run is not None:
            self.last_run['outcome'] = 'submitted'
        self._log('submitted', task_id=task_id, fence=fence, path=ref['path'], bytes=size, truncated=truncated)
        return {'status': 'submitted', 'task_id': task_id, 'fence': fence, 'path': ref['path'], 'truncated': truncated}


# -- Command line -------------------------------------------------------------------------------
SETTING_KEYS = frozenset({'credential', 'command', 'workdir', 'runs_dir', 'poll', 'timeout', 'max_runs_per_hour',
                          'max_runs_per_day', 'report_limit'})
DEFAULTS = {'workdir': '../../..', 'runs_dir': '../../common-room-runs', 'poll': 60, 'timeout': 2700,
            'max_runs_per_hour': 2, 'max_runs_per_day': 8, 'report_limit': REPORT_LIMIT}
BOUNDS = {'poll': (5, 86400), 'timeout': (MIN_TIMEOUT, MAX_TIMEOUT), 'max_runs_per_hour': (1, MAX_PER_HOUR),
          'max_runs_per_day': (1, MAX_PER_DAY), 'report_limit': (MIN_REPORT_LIMIT, REPORT_LIMIT)}


def read_config(path):
    data = Path(path).read_bytes()
    if len(data) > 16384:
        raise ValueError('Runner config exceeds 16 KiB.')
    config = decode_request(data)
    unknown = set(config) - SETTING_KEYS
    if unknown:
        raise ValueError('Unknown runner config keys: ' + ', '.join(sorted(unknown)))
    return config


def resolve_path(value, package_root, name):
    """Relative paths are relative to the package root (the folder holding common_room/)."""
    if type(value) is not str or not value or '\x00' in value:
        raise ValueError('%s is a path (text).' % name)
    path = Path(value)
    if not path.is_absolute():
        path = Path(package_root) / path
    return Path(os.path.normpath(str(path.absolute())))


def build_parser():
    p = argparse.ArgumentParser(prog='python -B -m common_room.agent_worker',
                                description='Headless agent runner for one worker seat: runs Claude Code or Codex on '
                                            'the seat\'s handoffs and submits the result for a separate reviewer.')
    p.add_argument('--config', required=True, help='The seat\'s nonsecret JSON config, e.g. crew/agent-claude-code.json')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--once', dest='mode', action='store_const', const='once', help='One cycle, then exit (default)')
    mode.add_argument('--loop', dest='mode', action='store_const', const='loop', help='Keep polling')
    p.set_defaults(mode=None)
    return p


def parse_settings(argv=None, package_root=ROOT):
    args = build_parser().parse_args(argv)
    settings = dict(DEFAULTS)
    settings.update(read_config(args.config))
    settings['mode'] = args.mode or 'once'
    for key in ('credential', 'command'):
        if key not in settings:
            raise ValueError('%s is required in the runner config.' % key)
    settings['command'] = check_command(settings['command'])
    for key, (low, high) in BOUNDS.items():
        value = settings[key]
        allowed = (int, float) if key == 'timeout' else (int,)
        if type(value) not in allowed or not math.isfinite(value) or not low <= value <= high:
            raise ValueError('%s must be a number from %s to %s.' % (key, low, high))
    settings['credential'] = resolve_path(settings['credential'], package_root, 'credential')
    if not settings['credential'].name.endswith('-credential.json'):
        raise ValueError('credential names this seat\'s <seat>-credential.json.')
    settings['workdir'] = resolve_path(settings['workdir'], package_root, 'workdir')
    settings['runs_dir'] = resolve_path(settings['runs_dir'], package_root, 'runs_dir')
    return settings


def build_runner(settings, clock=time.time, log=None):
    return Runner(CrewClient(settings['credential']), settings['command'], settings['workdir'], settings['runs_dir'],
                  timeout=settings['timeout'], max_runs_per_hour=settings['max_runs_per_hour'],
                  max_runs_per_day=settings['max_runs_per_day'], report_limit=settings['report_limit'],
                  clock=clock, log=log)


def main(argv=None, clock=time.time):
    """Exit codes for --once: 0 worked, asked or idle, 2 deferred (including another process
    holding this seat or a task whose agent still runs), 1 error or stopped by a signal (after the
    agent's tree was killed). --loop stopped by a signal exits 0."""
    try:
        settings = parse_settings(argv)
        runner = build_runner(settings, clock=clock)
    except SystemExit as exc:            # argparse usage errors and --help
        return 0 if exc.code in (0, None) else 1
    except (ValueError, OSError) as exc:
        _print_line(json.dumps({'event': 'config_error', 'message': short(exc)}, ensure_ascii=True))
        return 1
    runner._log('started', mode=settings['mode'], command=runner.command_name, workdir=str(runner.workdir),
                runs_dir=str(runner.runs_dir), timeout=settings['timeout'], max_runs_per_hour=settings['max_runs_per_hour'],
                max_runs_per_day=settings['max_runs_per_day'], seat_files=str(runner.runtime / 'workers'))
    try:
        previous = install_signal_handlers(runner)
    except ValueError:                   # not the main thread (a test calling main): nothing installed
        previous = {}
    try:
        return _main_mode(runner, settings)
    finally:
        # Put back what was there, so no handler of this runner outlives main() (an in-process caller, a test). None
        # means the previous handler was not set from Python and cannot be put back: the default is.
        for number, handler in previous.items():
            signal.signal(number, signal.SIG_DFL if handler is None else handler)


def _main_mode(runner, settings):
    if settings['mode'] == 'loop':
        try:
            results = runner.run_forever(settings['poll'])       # returns only if the seat is busy
        except (KeyboardInterrupt, Stopped) as exc:
            runner._log('stopped', reason='signal' if isinstance(exc, Stopped) else 'interrupted',
                        signal=getattr(exc, 'signum', None), agent_killed=runner.killed_on_stop)
            return 0
        return EXIT_CODES.get(results[-1].get('status'), 1) if results else 0
    try:
        result = runner.run_once()
    except Stopped as exc:
        runner._log('stopped', reason='signal', signal=exc.signum, agent_killed=runner.killed_on_stop)
        return 1
    except Exception as exc:
        runner._log('cycle_error', error=type(exc).__name__, message=short(exc))
        return 1
    return EXIT_CODES.get(result.get('status'), 1)


if __name__ == '__main__':
    sys.exit(main())
