"""The usage meter: one JSON line per worker job, kept beside the seat's runtime files.

Every local-model job (common_room/ollama_worker.py) and every headless-agent job
(common_room/agent_worker.py) appends exactly one line to <room-runtime>/usage/<seat>.jsonl.
The line holds numbers and a few short names only: never task text, prompts, results, paths
or keys (a record with any other field is refused). USAGE_FORMAT.md at the package root
describes the file. Standard library only; reading and appending never touch the network.

    {"ts":"2026-10-01T22:15:03Z","seat":"local-a","kind":"local","task_id":"t0123abcd",
     "model":"gemma4:31b-it-qat","seconds":41.2,"prompt_tokens":1900,"output_tokens":850,
     "outcome":"finished"}
"""
from datetime import datetime, timezone
from pathlib import Path
import json, os, re

from .activation import safe_path

USAGE_DIR = 'usage'
KINDS = ('local', 'headless')
OUTCOMES = ('finished', 'question', 'failed', 'skipped_quiet')
SEAT = re.compile(r'[a-z][a-z0-9_-]{0,63}')
TASK_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,159}')
COMMON = ('ts', 'seat', 'kind', 'task_id', 'model', 'seconds')
EXTRA = {'local': ('prompt_tokens', 'output_tokens'), 'headless': ('exit', 'budget_cap_usd')}
MAX_LINE = 1024
FILE_APPEND_DATA, SHARE_ALL, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL = 0x4, 0x7, 4, 0x80      # Windows CreateFileW values
MAX_FILE_BYTES = 16 * 1024 * 1024        # a reader skips a file bigger than this rather than load it all


def _iso(value):
    """Epoch seconds, a datetime or an ISO-8601 string as 'YYYY-MM-DDTHH:MM:SSZ' (UTC)."""
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.astimezone()
    elif type(value) in (int, float) and not isinstance(value, bool) and value == value and abs(value) < 4e10:
        moment = datetime.fromtimestamp(value, timezone.utc)
    elif type(value) is str:
        try:
            moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            raise ValueError('ts must be epoch seconds or an ISO-8601 time.') from None
        if moment.tzinfo is None:
            raise ValueError('ts as text must carry a time zone (for example a trailing Z).')
    else:
        raise ValueError('ts must be epoch seconds or an ISO-8601 time.')
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _count(value, name):
    if value is None:
        return None
    if type(value) is not int or value < 0 or value > 10 ** 12:
        raise ValueError('%s is a whole number of 0 or more, or null.' % name)
    return value


def check_record(seat, record):
    """The record as it is written: the fixed fields in a fixed order, or ValueError.

    Only the fields below are accepted, so a prompt, a result or a path cannot be carried by
    accident: unknown keys are refused, and text fields must match a short name pattern."""
    if type(seat) is not str or not SEAT.fullmatch(seat):
        raise ValueError('seat is a seat id such as local-a.')
    if type(record) is not dict:
        raise ValueError('A usage record is a dict.')
    kind = record.get('kind')
    if kind not in KINDS:
        raise ValueError('kind is local or headless.')
    allowed = set(COMMON) | set(EXTRA[kind]) | {'outcome'}
    unknown = set(record) - allowed
    if unknown:
        raise ValueError('Unknown usage fields for a %s job: %s.' % (kind, ', '.join(sorted(str(k)[:40] for k in unknown))))
    if record.get('seat', seat) != seat:
        raise ValueError('The record names another seat.')
    if record.get('outcome') not in OUTCOMES:
        raise ValueError('outcome is one of %s.' % ', '.join(OUTCOMES))
    task_id, model = record.get('task_id'), record.get('model')
    if type(task_id) is not str or not TASK_ID.fullmatch(task_id):
        raise ValueError('task_id is the task id.')
    if type(model) is not str or not MODEL.fullmatch(model):
        raise ValueError('model is a short model or command name.')
    seconds = record.get('seconds')
    if type(seconds) not in (int, float) or isinstance(seconds, bool) or not 0 <= seconds < 10 ** 7 or seconds != seconds:
        raise ValueError('seconds is a number of 0 or more.')
    line = {'ts': _iso(record['ts']) if 'ts' in record else _iso(datetime.now(timezone.utc)),
            'seat': seat, 'kind': kind, 'task_id': task_id, 'model': model, 'seconds': round(float(seconds), 3)}
    if kind == 'local':
        line['prompt_tokens'] = _count(record.get('prompt_tokens'), 'prompt_tokens')
        line['output_tokens'] = _count(record.get('output_tokens'), 'output_tokens')
    else:
        exit_code = record.get('exit')
        if exit_code is not None and (type(exit_code) is not int or not -2 ** 31 <= exit_code < 2 ** 32):
            raise ValueError('exit is the process exit code, or null.')
        cap = record.get('budget_cap_usd')
        if cap is not None and (type(cap) not in (int, float) or isinstance(cap, bool) or not 0 <= cap < 10 ** 7 or cap != cap):
            raise ValueError('budget_cap_usd is a dollar amount of 0 or more, or null.')
        line['exit'] = exit_code
        line['budget_cap_usd'] = float(cap) if cap is not None else None
    line['outcome'] = record['outcome']
    return line


def _usage_folder(runtime_dir, create):
    runtime = Path(runtime_dir)
    folder = safe_path(runtime, runtime / USAGE_DIR)
    if create:
        folder.mkdir(exist_ok=True)
        folder = safe_path(runtime, folder)
    return folder


def _append_bytes(path, data):
    """Add `data` to the end of the file in ONE write that cannot interleave with another writer.

    On Windows the C runtime's O_APPEND is a seek followed by a write, so two writers (measured:
    six threads, 1,800 lines in, 1,630 out and 121 of them torn) overwrite each other. A handle
    opened with only the append-data right (FILE_APPEND_DATA) makes the file system put every
    write at the end, atomically, whatever else is writing. Elsewhere O_APPEND is already atomic."""
    if os.name != 'nt':
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            written = os.write(fd, data)
        finally:
            os.close(fd)
        if written != len(data):
            raise OSError('The usage line was only partly written.')
        return
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                                   ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.CreateFileW(str(path), FILE_APPEND_DATA, SHARE_ALL, None, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        written = wintypes.DWORD(0)
        if not kernel32.WriteFile(handle, data, len(data), ctypes.byref(written), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if written.value != len(data):
            raise OSError('The usage line was only partly written.')
    finally:
        kernel32.CloseHandle(handle)


def append_usage(runtime_dir, seat, record):
    """Append one line to <runtime_dir>/usage/<seat>.jsonl and return the record as written.

    The whole line goes out in a single atomic append (see _append_bytes), so two writers at once
    (or a crash) cannot leave half a line or lose one."""
    line = check_record(seat, record)
    data = (json.dumps(line, ensure_ascii=True, separators=(',', ':')) + '\n').encode('ascii')
    if len(data) > MAX_LINE:
        raise ValueError('A usage line is at most %d bytes.' % MAX_LINE)
    folder = _usage_folder(runtime_dir, create=True)
    path = safe_path(Path(runtime_dir), folder / (seat + '.jsonl'))
    _append_bytes(path, data)
    return line


def try_append(runtime_dir, seat, record, log=None):
    """append_usage for a worker: a meter that cannot write must never stop a job, so the failure
    is logged (event usage_unwritable, with the reason) and False comes back."""
    try:
        append_usage(runtime_dir, seat, record)
        return True
    except (OSError, ValueError) as exc:
        if log is not None:
            log('usage_unwritable', seat=seat, message=str(exc)[:200])
        return False


def read_usage(runtime_dir, since=None, seat=None, problems=None):
    """The usage lines under <runtime_dir>/usage, oldest first, as a list of dicts.

    since: epoch seconds, a datetime or an ISO-8601 string; only lines at or after it.
    seat: only that seat's file. problems: a list to receive one short text per line or file
    that was skipped (bad JSON, wrong fields, too big), so nothing disappears silently."""
    if seat is not None and (type(seat) is not str or not SEAT.fullmatch(seat)):
        raise ValueError('seat is a seat id such as local-a.')
    cutoff = _iso(since) if since is not None else None
    try:
        folder = _usage_folder(runtime_dir, create=False)
    except ValueError as exc:
        if problems is not None:
            problems.append('usage folder refused: %s' % exc)
        return []
    if not folder.is_dir():
        return []
    names = [seat + '.jsonl'] if seat is not None else sorted(
        p.name for p in folder.iterdir() if p.name.endswith('.jsonl') and SEAT.fullmatch(p.name[:-6]))
    found = []
    for name in names:
        try:
            path = safe_path(Path(runtime_dir), folder / name)
            if not path.is_file():
                continue
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError('larger than %d MB' % (MAX_FILE_BYTES >> 20))
            text = path.read_bytes().decode('utf-8')
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            if problems is not None:
                problems.append('%s skipped: %s' % (name, str(exc)[:120]))
            continue
        owner = name[:-6]
        for number, raw in enumerate(text.splitlines(), 1):
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
                if type(value) is not dict or value.get('seat') != owner:
                    raise ValueError('not a usage line for this seat')
                line = check_record(owner, value)
                if line != value:
                    raise ValueError('fields differ from the format')
            except (ValueError, RecursionError) as exc:
                if problems is not None:
                    problems.append('%s line %d skipped: %s' % (name, number, str(exc)[:120]))
                continue
            if cutoff is None or line['ts'] >= cutoff:
                found.append(line)
    return sorted(found, key=lambda x: (x['ts'], x['seat']))
