"""Local-model worker seat for the Common Room (piece W of the 2026-09-23 crew build).

One process acts as ONE worker seat (for example local-a with a strong model, local-b with a
light one). It can be woken by Windows Task Scheduler (--once) or keep polling (--loop). Each
cycle it reads the room as that seat, acknowledges handoffs addressed to it, drafts at most one
of them with a local Ollama model, saves the draft in the room's own content-addressed result
store and submits it for a SEPARATE reviewer. It never reviews, never creates tasks and never
acts as another seat.

Laws kept here (BUILD_PLAN_COMMON_ROOM_CREW_2026-09-23.md):
- All room access goes through CrewClient under this seat's own credential file.
- Empty inbox: zero model calls, not even a health check.
- Inputs come only from the task's INPUT: lines, inside configured read roots, text only and
  size-capped, with private, secret, credential, key and database paths refused.
- The only write of task content is stage_text into the room's result store. The worker's own
  bookkeeping (seat lock, rate-cap times, status) lives in <room-runtime>/workers/ and never
  holds model text, file contents or keys.
- A helper task (delegated by another worker) may read only files the owner named in the
  original task, so one seat cannot make another read what the owner never offered.
- Only GET /api/tags, POST /api/chat and POST /api/generate (an unload with keep_alive 0) are
  ever sent to Ollama, and redirects are never followed. Nothing is ever pulled.
- BATCH's quiet windows are never entered by a generation, and keep_alive shrinks so a
  model is not left resident into one. An optional shared GPU calendar (config key
  gpu_calendar, see GPU_CALENDAR.md) adds windows to the worker's own; a calendar that is
  named but missing or invalid keeps the worker quiet, with the reason in its status file.
- Model text is inert: it is saved as a draft and used for nothing else.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import argparse, fnmatch, hashlib, http.client, json, math, os, re, stat, sys, time

from . import gpu_calendar, usage
from .activation import read_json, safe_path, write_json
from .crew_client import ROOT, CrewClient, CrewError, RenewIds, check_credential_path
from .protocol import canonical, decode_request

DEFAULT_OLLAMA = 'http://127.0.0.1:11434'   # set "ollama" in the seat config to point at your own Ollama
LEASE_SECONDS = 1800
LEASE_MARGIN = 120
MAX_TIMEOUT = LEASE_SECONDS - LEASE_MARGIN
MAX_INPUTS = 6
MAX_FILE_BYTES = 64 * 1024
MAX_RANGED_FILE_BYTES = 1024 * 1024   # a #L range may excerpt a larger file; the excerpt stays <= 64 KB
MAX_TOTAL_BYTES = 96 * 1024
REPORT_LIMIT = 12000
ROOM_REQUEST_LIMIT = 16384
BODY_LIMIT = 4000
QUESTION_LIMIT = 2000
TEXT_EXTENSIONS = frozenset({'.md', '.txt', '.json', '.csv', '.py', '.ps1', '.cmd', '.yaml', '.yml',
                             '.toml', '.html', '.js', '.css', '.log'})
DENIED_COMPONENTS = frozenset({'_private', 'secrets', '.runtime', '.git', '.ssh', '.obsidian', '.config', '.docker',
                               '.aws', '.azure', '.gnupg'})
DENIED_NAMES = ('*.env', '.env*', '*credential*', '*secret*', '*token*', '*password*', '*passwd*', '*key*', '*auth*',
                '*cookie*', '*session*', '*wallet*', 'id_*', 'service-account*', '*.pem', '*.kdbx', '*.pfx', '*.p12',
                '*.sqlite*', '*.db')
DENY_REASON = ('denied path component or file name (%s): the worker refuses names that look like secrets, keys, '
               'credentials, sessions or databases. This can refuse an innocent name (for example keyboard.md); that '
               'is the safe direction. If the file is harmless, rename it or copy it under a plain name')
MAX_PER_HOUR = 240
SEAT_FILE = re.compile(r'([a-z][a-z0-9_-]{0,63})-credential\.json')
NOTE_REASONS = frozenset({'model_missing', 'ollama_unavailable'})
DEFAULT_QUIET_WINDOWS = (
    {'days': 'weekdays', 'start': '11:30', 'end': '12:05', 'label': 'GPU desk (midday)'},
    {'days': 'weekdays', 'start': '14:30', 'end': '15:05', 'label': 'GPU desk (afternoon)'},
    {'days': 'daily', 'start': '20:50', 'end': '21:35', 'label': 'nightly index'},
)
ALLOWED_ENDPOINTS = frozenset({('GET', '/api/tags'), ('POST', '/api/chat'), ('POST', '/api/generate')})
OLLAMA_URL = re.compile(r'http://([A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?):([0-9]{1,5})')
MODEL_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}')
INPUT_LINE = re.compile(r'INPUT:\s*(\S.*)')
LINE_RANGE = re.compile(r'(.*?)#L([0-9]{1,7})-L([0-9]{1,7})')
CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')
SURROGATE = re.compile(r'[\ud800-\udfff]')
TRUNCATION_MARKER = '[TRUNCATED by worker at 12000 bytes]'
JSON_TRUNCATION_NOTE = "[Cut further so the report's request fits the room's 16 KB limit.]"
EXIT_CODES = {'idle': 0, 'submitted': 0, 'asked': 0, 'failed': 0, 'deferred': 2, 'stopped': 2, 'error': 1}
STOP_FLAG = 'STOP'                       # <room runtime>/STOP, beside the seat's credential: present means stop
STOP_REASON = 'stopped: STOP flag'
SYSTEM_PROMPT = """You are drafting for a separate reviewer in a shared task room. Your answer is an UNREVIEWED DRAFT: a reviewer will check it against the inputs before anyone relies on it.
House rules:
1. Use only the task text and the numbered input excerpts in the user message.
2. Cite path:line (for example notes/plan.md:12) for every factual claim you take from an input.
3. When the inputs do not say something the task asks for, write MISSING for it. Do not guess.
4. Label anything you reason out yourself, rather than read in an input, as INFERENCE.
5. You cannot run commands, browse, edit files, or contact anyone. Never claim that you did any of those things.
6. Keep the whole answer under about 8,000 characters. Use plain Markdown."""


def clean_text(value):
    """Room text refuses control characters and lone surrogates; model text may carry both."""
    return SURROGATE.sub('\ufffd', CONTROL.sub('', value))


def short(value, limit=300):
    value = clean_text(str(value))
    return value if len(value) <= limit else value[:limit - 3] + '...'


def seconds_text(value):
    return ('%.3f' % value).rstrip('0').rstrip('.')


def stop_flag_present(runtime):
    """True while the file STOP exists in the seat's runtime folder (the one holding its credential), whatever it
    holds or whether it can be read: a folder, a link, a file of any bytes all count. The flag is absent ONLY when
    the OS says plainly that nothing is there (not found, or a parent that is not a folder); any other answer, an
    error asking included, counts as present: when in doubt, stay stopped. (os.path.lexists is not used: it turns
    every OSError into False, which would treat a flag the OS refuses to show as absent.)"""
    try:
        os.lstat(os.path.join(str(runtime), STOP_FLAG))
    except (FileNotFoundError, NotADirectoryError):
        return False
    except (OSError, ValueError):
        return True
    return True


# -- Central time and quiet windows ---------------------------------------------------------
EPOCH = datetime(1970, 1, 1)


def _sunday(year, month, n):
    first = datetime(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


class CentralTime:
    """America/Chicago wall clock. Uses zoneinfo when its data loads; Windows Python often has
    no tzdata, so the fallback is the real US rule: daylight time from the second Sunday of
    March 02:00 to the first Sunday of November 02:00. Wall times convert with fold=0 meaning:
    the skipped spring hour reads as standard time, the repeated autumn hour as daylight time."""

    def __init__(self, use_zoneinfo=True):
        self.zone = None
        if use_zoneinfo:
            try:
                from zoneinfo import ZoneInfo
                self.zone = ZoneInfo('America/Chicago')
            except Exception:           # ImportError, ZoneInfoNotFoundError, missing tzdata
                self.zone = None
        self.source = 'zoneinfo' if self.zone is not None else 'fallback'

    def local(self, epoch):
        if self.zone is not None:
            return datetime.fromtimestamp(epoch, self.zone).replace(tzinfo=None)
        moment = EPOCH + timedelta(seconds=epoch)
        start = _sunday(moment.year, 3, 2) + timedelta(hours=8)    # 02:00 CST is 08:00 UTC
        end = _sunday(moment.year, 11, 1) + timedelta(hours=7)     # 02:00 CDT is 07:00 UTC
        return moment + timedelta(hours=-5 if start <= moment < end else -6)

    def epoch(self, wall):
        if self.zone is not None:
            return wall.replace(tzinfo=self.zone, fold=0).timestamp()
        start = _sunday(wall.year, 3, 2) + timedelta(hours=3)      # first wall time on CDT
        end = _sunday(wall.year, 11, 1) + timedelta(hours=2)       # first wall time back on CST
        offset = 5 if start <= wall < end else 6
        return (wall - EPOCH).total_seconds() + offset * 3600


DAY_SETS = {'weekdays': frozenset(range(5)), 'daily': frozenset(range(7)), 'weekends': frozenset({5, 6})}


def _minutes(value):
    match = re.fullmatch(r'([01][0-9]|2[0-3]):([0-5][0-9])', value) if type(value) is str else None
    if not match:
        raise ValueError('Quiet window times are HH:MM, 24-hour, US Central.')
    return int(match[1]) * 60 + int(match[2])


def parse_quiet_windows(windows):
    if type(windows) not in (list, tuple) or len(windows) > 24:
        raise ValueError('Quiet windows are a list of at most 24 entries.')
    parsed = []
    for row in windows:
        if type(row) is not dict or not {'days', 'start', 'end'} <= set(row) or set(row) - {'days', 'start', 'end', 'label'}:
            raise ValueError('Each quiet window needs days, start and end (and may have a label).')
        days = row['days']
        if type(days) is str and days in DAY_SETS:
            days = DAY_SETS[days]
        elif type(days) is list and days and all(type(d) is int and 0 <= d <= 6 for d in days):
            days = frozenset(days)
        else:
            raise ValueError('Quiet window days are weekdays, weekends, daily or a list of 0-6 (Monday is 0).')
        start, end = _minutes(row['start']), _minutes(row['end'])
        if not start < end:
            raise ValueError('A quiet window must start before it ends on the same day.')
        label = row.get('label', row['start'] + '-' + row['end'])
        if type(label) is not str:
            raise ValueError('Quiet window labels are text.')
        parsed.append((days, start, end, short(label, 60)))
    return parsed


class QuietSchedule:
    def __init__(self, windows=DEFAULT_QUIET_WINDOWS, central=None):
        self.windows = parse_quiet_windows(list(windows))
        self.central = central or CentralTime()

    def intervals(self, now):
        today = self.central.local(now).date()
        found = []
        for offset in range(-1, 9):
            day = today + timedelta(days=offset)
            midnight = datetime(day.year, day.month, day.day)
            for days, start, end, label in self.windows:
                if day.weekday() in days:
                    found.append((self.central.epoch(midnight + timedelta(minutes=start)),
                                  self.central.epoch(midnight + timedelta(minutes=end)), label))
        return sorted(found)

    def conflict(self, now, span):
        """The window that [now, now+span] touches, or None."""
        for start, end, label in self.intervals(now):
            if start <= now + span and end > now:
                return start, end, label
        return None

    def next_start(self, now):
        starts = [start for start, _, _ in self.intervals(now) if start > now]
        return min(starts) if starts else None


def keep_alive_for(configured, now, next_start):
    """min(configured, seconds until the next quiet window starts - 60), floored at 0."""
    if next_start is None:
        return int(configured)
    return max(0, min(int(configured), int(next_start - now - 60)))


# -- Inputs ---------------------------------------------------------------------------------
class InputRefused(ValueError):
    def __init__(self, spec, reason):
        self.spec = spec
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class InputFile:
    label: str
    root: str
    path: str               # normalized real path, used to compare files across tasks
    sha256: str
    first: int
    last: int
    total_lines: int
    lines: tuple
    size: int
    excerpt_bytes: int


def _denied(parts):
    for part in parts:
        name = part.casefold()
        if name in DENIED_COMPONENTS or any(fnmatch.fnmatchcase(name, pattern) for pattern in DENIED_NAMES):
            return part
    return None


def input_specs(body):
    specs = []
    for line in body.split('\n'):
        match = INPUT_LINE.fullmatch(line.rstrip('\r'))
        if match:
            specs.append(match[1].strip())
    return specs


def resolve_input(spec, roots):
    """Resolve one INPUT spec to a read-only text excerpt, or raise InputRefused."""
    text = spec.strip()
    if not text or len(text) > 400 or any(ord(c) < 32 for c in text):
        raise InputRefused(spec, 'empty, overlong or control characters in the path')
    first = last = None
    ranged = LINE_RANGE.fullmatch(text)
    if ranged:
        text, first, last = ranged[1].rstrip(), int(ranged[2]), int(ranged[3])
        if first < 1 or last < first:
            raise InputRefused(spec, 'invalid line range; use #L<first>-L<last> with 1 <= first <= last')
    if text.startswith(('/', '\\')) or ':' in text:
        raise InputRefused(spec, 'absolute or drive path refused; name a path relative to a read root')
    parts = [p for p in re.split(r'[\\/]+', text) if p not in ('', '.')]
    if not parts:
        raise InputRefused(spec, 'empty path')
    if '..' in parts:
        raise InputRefused(spec, 'parent traversal (..) refused')
    if any(p != p.rstrip('. ') for p in parts):
        raise InputRefused(spec, 'path components ending in a dot or space are refused')
    hit = _denied(parts)
    if hit:
        raise InputRefused(spec, DENY_REASON % hit)
    suffix = os.path.splitext(parts[-1])[1].casefold()
    if suffix not in TEXT_EXTENSIONS:
        raise InputRefused(spec, 'not a text file type (%s)' % (suffix or 'no extension'))
    # A spec may name the read root itself ('Lab Notes/04_Log/x.md'); that form is tried first,
    # then the bare path in root order, as before.
    # A spec that names a root is looked up under that root only, never anywhere else.
    attempts = [(Path(r), parts[1:]) for r in roots if len(parts) > 1 and parts[0].casefold() == Path(r).name.casefold()]
    named_root = bool(attempts)
    if not named_root:
        attempts = [(Path(r), parts) for r in roots]
    for root, rest in attempts:
        try:
            if root.is_symlink() or root.is_junction():
                raise InputRefused(spec, 'read root is a symlink or junction')
            if not root.is_dir():
                continue
            candidate = root
            for part in rest:
                candidate = candidate / part
                if candidate.is_symlink() or candidate.is_junction():
                    raise InputRefused(spec, 'symlink or junction in the path refused')
                if not os.path.lexists(candidate):
                    break
            else:
                return _read_input(spec, '/'.join(parts), root, candidate, first, last)
        except OSError as exc:
            raise InputRefused(spec, 'path could not be checked (%s)' % type(exc).__name__) from exc
    if named_root:
        raise InputRefused(spec, 'not found under the named read root %s' % parts[0])
    raise InputRefused(spec, 'not found under any read root')


def _read_input(spec, label, root, candidate, first, last):
    try:
        real = candidate.resolve(strict=True)
        relative = real.relative_to(root.resolve(strict=True))
    except (OSError, ValueError, RuntimeError) as exc:
        raise InputRefused(spec, 'resolves outside its read root') from exc
    # Checked again on the resolved name, so a short (8.3) or differently cased alias cannot slip by.
    hit = _denied(relative.parts)
    if hit:
        raise InputRefused(spec, DENY_REASON % hit)
    if os.path.splitext(relative.name)[1].casefold() not in TEXT_EXTENSIONS:
        raise InputRefused(spec, 'not a text file type')
    info = candidate.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise InputRefused(spec, 'not a regular file')
    if info.st_nlink != 1:
        raise InputRefused(spec, 'hard-linked file refused (link count %d)' % info.st_nlink)
    ceiling = MAX_FILE_BYTES if first is None else MAX_RANGED_FILE_BYTES
    if info.st_size > ceiling:
        raise InputRefused(spec, ('file is larger than 64 KB (%d bytes); name a line range like #L1-L200 to read part of it'
                                  if first is None else 'file is larger than 1 MB even for a line range (%d bytes)') % info.st_size)
    descriptor = os.open(candidate, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise InputRefused(spec, 'file changed while it was being checked')
        chunks, size = [], 0
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > ceiling:
                raise InputRefused(spec, 'file grew past its size limit while being read')
    finally:
        os.close(descriptor)
    data = b''.join(chunks)
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise InputRefused(spec, 'not strict UTF-8 text') from exc
    if '\x00' in text:
        raise InputRefused(spec, 'contains NUL bytes; not text')
    if text.startswith('\ufeff'):
        text = text[1:]
    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    total = len(lines)
    if first is None:
        first, last = (1, total) if total else (0, 0)
    else:
        if first > total:
            raise InputRefused(spec, 'line range starts past the end of the file (%d lines)' % total)
        last = min(last, total)
    chosen = tuple((n, lines[n - 1]) for n in range(first, last + 1)) if total else ()
    excerpt = sum(len(t.encode('utf-8')) + 1 for _, t in chosen)
    if excerpt > MAX_FILE_BYTES:
        raise InputRefused(spec, 'the chosen lines are more than 64 KB (%d bytes); name a smaller range' % excerpt)
    return InputFile(label=label, root=root.name, path=os.path.normcase(str(real)),
                     sha256=hashlib.sha256(data).hexdigest(), first=first,
                     last=last, total_lines=total, lines=chosen, size=len(data),
                     excerpt_bytes=sum(len(t.encode('utf-8')) + 1 for _, t in chosen))


def load_inputs(body, roots):
    """Returns (inputs, None) or ([], (refused input, reason)). All or nothing."""
    specs = input_specs(body)
    if len(specs) > MAX_INPUTS:
        return [], ('%d INPUT lines' % len(specs), 'too many INPUT lines (%d); at most 6 are read' % len(specs))
    inputs, total = [], 0
    for spec in specs:
        try:
            item = resolve_input(spec, roots)
        except InputRefused as exc:
            return [], (spec, exc.reason)
        total += item.excerpt_bytes
        if total > MAX_TOTAL_BYTES:
            return [], (spec, 'inputs total more than 96 KB')
        inputs.append(item)
    return inputs, None


def is_helper(task):
    return (task.get('intake') or {}).get('source_namespace') == 'room-helper'


def owner_named_files(task, tasks, roots, fetch=None):
    """Confused-deputy rule for a helper task (created by another worker's request_help).
    Walks parent_id up to the first task that is not room-helper (the owner-authored root) and
    returns ((set of normalized real paths its INPUT lines name, root task id), None), or
    (None, reason). Only the owner's own INPUT lines grant files; line ranges may differ."""
    current, seen = task, set()
    while is_helper(current):
        if current['id'] in seen or len(seen) > 8:
            return None, 'the helper chain loops or is deeper than the room allows'
        seen.add(current['id'])
        parent = tasks.get(current.get('parent_id'))
        if parent is None:
            return None, 'parent task %s is not visible to this seat' % current.get('parent_id')
        current = parent
    if current.get('clipped') and fetch is not None:
        # S0 F5: a quiet root's body is clipped in /api/state; its INPUT lines are read from the full task (task_view).
        try:
            current = fetch(current['id'])
        except (CrewError, KeyError, TypeError, ValueError):
            return None, 'the root task %s is clipped and its full text could not be read' % current['id']
    allowed = set()
    for spec in input_specs(current.get('body') or ''):
        # Resolve the owner's spec exactly as written, range included, so a ranged excerpt of a
        # large file still grants that file to helpers (they may choose a different range).
        try:
            allowed.add(resolve_input(spec, roots).path)
        except InputRefused:
            continue
    return (allowed, current['id']), None


def estimate_prompt_tokens(messages):
    """Conservative: UTF-8 bytes of every message divided by 3, rounded up."""
    total = sum(len(m['content'].encode('utf-8')) for m in messages)
    return -(-total // 3)


# -- Ollama ---------------------------------------------------------------------------------
class OllamaUnavailable(Exception):
    def __init__(self, kind, message, retryable):
        self.kind = kind
        self.retryable = retryable
        super().__init__(message)


class OllamaClient:
    """Plain HTTP to one Ollama host. Allowlisted endpoints only; redirects are never followed."""

    def __init__(self, base_url, connection=http.client.HTTPConnection):
        match = OLLAMA_URL.fullmatch(base_url) if type(base_url) is str else None
        if not match or not 1 <= int(match[2]) <= 65535:
            raise ValueError('Ollama URL must be exactly http://<host>:<port>, with no path, query or credentials.')
        self.base_url = base_url
        self.host = match[1]
        self.port = int(match[2])
        self.connection = connection

    def request(self, method, path, payload=None, timeout=30.0, limit=1048576):
        if (method, path) not in ALLOWED_ENDPOINTS:
            raise ValueError('Endpoint is not on the worker allowlist: %s %s' % (method, path))
        headers = {'Accept': 'application/json'}
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        connection = self.connection(self.host, self.port, timeout=timeout)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            status = response.status
            raw = response.read(limit + 1)
        except TimeoutError as exc:
            raise OllamaUnavailable('timeout', 'no reply within %s s' % seconds_text(timeout), True) from exc
        except ConnectionRefusedError as exc:
            raise OllamaUnavailable('refused', 'connection refused', True) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise OllamaUnavailable('connection', 'connection lost (%s)' % type(exc).__name__, True) from exc
        finally:
            connection.close()
        if 300 <= status < 400:
            raise OllamaUnavailable('redirect', 'HTTP %d redirect, not followed' % status, False)
        if status >= 500:
            raise OllamaUnavailable('http_5xx', 'HTTP %d' % status, True)
        if status != 200:
            raise OllamaUnavailable('http_%d' % status, 'HTTP %d' % status, False)
        if len(raw) > limit:
            raise OllamaUnavailable('too_large', 'reply larger than %d bytes' % limit, False)
        try:
            value = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise OllamaUnavailable('bad_reply', 'reply is not JSON', False) from exc
        if type(value) is not dict:
            raise OllamaUnavailable('bad_reply', 'reply is not a JSON object', False)
        return value

    def tags(self, timeout):
        models = self.request('GET', '/api/tags', timeout=timeout).get('models')
        if type(models) is not list:
            raise OllamaUnavailable('bad_reply', 'tags reply has no model list', False)
        names = set()
        for row in models:
            if type(row) is dict:
                names.update(row[k] for k in ('name', 'model') if type(row.get(k)) is str)
        return names

    def chat(self, payload, timeout):
        return self.request('POST', '/api/chat', payload, timeout=timeout, limit=8 * 1048576)

    def unload(self, model, timeout):
        return self.request('POST', '/api/generate', {'model': model, 'keep_alive': 0}, timeout=timeout)


def model_listed(model, names):
    """Exact match, or 'name' and 'name:latest' treated as the same model."""
    def normal(name):
        return name if ':' in name.rsplit('/', 1)[-1] else name + ':latest'
    return normal(model) in {normal(n) for n in names}


# -- Prompt and report ----------------------------------------------------------------------
def build_messages(task, inputs):
    parts = ['Task %s: %s' % (task['id'], task['title']), '', 'Task body:', task['body'], '',
             'Acceptance:', task['acceptance'], '']
    if inputs:
        parts.append('Inputs (%d). Each line is numbered as L<n>: text.' % len(inputs))
        for item in inputs:
            parts.append('')
            parts.append('=== %s (sha256 %s) ===' % (item.label, item.sha256))
            parts.extend('L%d: %s' % (n, line) for n, line in item.lines)
    else:
        parts.append('No input files were named in the task.')
    return [{'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': '\n'.join(parts) + '\n'}]


def measured(stats):
    items = []
    for key in ('prompt_eval_count', 'eval_count'):
        items.append('%s %s' % (key, stats[key]) if key in stats else '%s not reported' % key)
    if 'total_duration' in stats:
        items.append('total_duration %s s' % seconds_text(stats['total_duration'] / 1e9))
    else:
        items.append('total_duration not reported')
    return ', '.join(items)


def report_header(actor, model, task, inputs, stats, done_reason, wall_seconds, warning=''):
    lines = ['# UNREVIEWED DRAFT', '',
             'This is an UNREVIEWED DRAFT by %s using model %s via Ollama. It is not accepted work until a '
             'separate reviewer checks it against the cited inputs.' % (actor, model), '']
    if warning:
        lines += [warning, '']
    lines += ['- Task: %s - %s' % (task['id'], task['title']),
             '- Measured by Ollama: ' + measured(stats)]
    if done_reason:
        lines.append('- Ollama done_reason: ' + done_reason)
    lines.append('- Worker wall time: %s s' % seconds_text(wall_seconds))
    if inputs:
        lines.append('- Inputs:')
        for item in inputs:
            span = 'empty file' if not item.total_lines else 'lines L%d-L%d of %d' % (item.first, item.last, item.total_lines)
            lines.append('  - %s (read root: %s) file sha256 %s, %s' % (item.label, item.root, item.sha256, span))
    else:
        lines.append('- Inputs: none named')
    lines += ['', '---', '', '']
    return clean_text('\n'.join(lines))


def fit_report(text, task_id, fence):
    """Returns (report, truncated). The report is at most 12000 UTF-8 bytes and its stage_text
    request fits the room's 16 KB limit; a cut is on a character boundary and says so."""
    def fits(value):
        if len(value.encode('utf-8')) > REPORT_LIMIT:
            return False
        envelope = {'operation': 'stage_text', 'payload': {'task_id': task_id, 'fence': fence, 'text': value}}
        return len(canonical(envelope).encode('utf-8')) <= ROOM_REQUEST_LIMIT
    if fits(text):
        return text, False
    marker = '\n\n' + TRUNCATION_MARKER
    cut = text.encode('utf-8')[:REPORT_LIMIT - len(marker.encode('utf-8'))].decode('utf-8', 'ignore')
    if fits(cut + marker):
        return cut + marker, True
    # Escape-heavy text (quotes, backslashes, newlines) doubles in JSON: cut further.
    marker = '\n\n' + TRUNCATION_MARKER + '\n' + JSON_TRUNCATION_NOTE
    low, high = 0, len(cut)
    while low < high:
        middle = (low + high + 1) // 2
        if fits(cut[:middle] + marker):
            low = middle
        else:
            high = middle - 1
    return cut[:low] + marker, True


# -- The worker -----------------------------------------------------------------------------
def _print_line(line):
    if sys.stdout is not None:          # pythonw under Task Scheduler has no stdout
        print(line, flush=True)


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec='seconds')


class SeatLock:
    """One worker process per seat: <room-runtime>/workers/<seat>.lock, the same pattern as the
    room's .launcher.lock (fcntl on POSIX, msvcrt on Windows). Closing the file releases it."""

    def __init__(self, path):
        self.path = path
        self.handle = None

    def acquire(self):
        handle = open(self.path, 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b'0')
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self):
        """Returns False when an explicit Windows unlock failed (closing still releases it)."""
        handle, self.handle = self.handle, None
        if handle is None:
            return True
        clean = True
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            clean = False
        finally:
            handle.close()
        return clean


class Worker:
    """One worker seat. run_once() is one wake-up; run_forever() is --loop."""

    def __init__(self, client, model, ollama, read_roots, *, clock=time.time, monotonic=time.monotonic,
                 sleep=time.sleep, log=None, timeout=600, num_ctx=16384, num_predict=3000, keep_alive=600,
                 max_per_hour=12, quiet_windows=DEFAULT_QUIET_WINDOWS, central=None, gpu_calendar=None):
        if type(model) is not str or not MODEL_NAME.fullmatch(model):
            raise ValueError('Model names are 1-160 letters, digits and . _ : / -')
        if type(timeout) not in (int, float) or not 0 < timeout <= MAX_TIMEOUT:
            raise ValueError('Timeout must be above 0 and at most %d seconds so one lease covers it.' % MAX_TIMEOUT)
        for name, value in (('num_ctx', num_ctx), ('num_predict', num_predict), ('max_per_hour', max_per_hour)):
            if type(value) is not int or value < 1:
                raise ValueError(name + ' must be a positive whole number.')
        if max_per_hour > MAX_PER_HOUR:
            raise ValueError('max_per_hour is at most %d.' % MAX_PER_HOUR)
        if type(keep_alive) is not int or keep_alive < 0:
            raise ValueError('keep_alive is a whole number of seconds, 0 or more.')
        if gpu_calendar is not None and (not isinstance(gpu_calendar, (str, Path)) or not str(gpu_calendar)):
            raise ValueError('gpu_calendar is the path of the shared GPU calendar file.')
        credential = getattr(client, 'path', None)
        seat = SEAT_FILE.fullmatch(Path(credential).name) if credential is not None else None
        if not seat:
            raise ValueError('The worker keeps its lock, rate and status files beside its own seat credential; '
                             'give it a CrewClient for <seat>-credential.json.')
        # Same boundary the client enforces, checked before any file is created beside the credential.
        credential = check_credential_path(credential, getattr(client, 'root', ROOT))
        self.roots = []
        for root in read_roots:
            root = Path(root).absolute()
            if root.is_symlink() or root.is_junction() or not root.is_dir():
                raise ValueError('Read roots must be existing, non-linked folders: %s' % root)
            self.roots.append(root)
        self.client = client
        self.renew_ids = RenewIds()
        self.model = model
        self.ollama = ollama
        self.clock = clock
        self.monotonic = monotonic
        self.sleep = sleep
        self.log = log or _print_line
        self.timeout = timeout
        self.num_ctx = num_ctx
        self.num_predict = num_predict
        self.keep_alive = keep_alive
        self.max_per_hour = max_per_hour
        self.quiet = QuietSchedule(quiet_windows, central)
        self.gpu_calendar = Path(gpu_calendar) if gpu_calendar is not None else None
        self.seat = seat[1]
        self.runtime = credential.parent
        self.lock = None
        self.notes_posted = None
        self.actor = None
        self._job = None             # this cycle's measured generation (seconds, tokens), for the usage line
        self._skips = set()          # (task, reason, Central day) already metered as skipped

    # logging: one JSON line per event; never a token or credential content
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

    def _generation_times(self, now):
        """Generation start times within the last hour, kept in <seat>-generations.json so a
        --once process honors the cap too. None means the file is unusable: a full cap."""
        name = self.seat + '-generations.json'
        try:
            path = self._file(name)
        except (OSError, ValueError) as exc:
            self._log('rate_state_unsafe', message=short(exc))
            return None
        try:
            value = read_json(path.parent, name, {'version': 1, 'generations': []})
            stamps = value.get('generations')
            if (value.get('version') != 1 or type(stamps) is not list or len(stamps) > 4 * MAX_PER_HOUR
                    or any(type(t) not in (int, float) or not math.isfinite(t) for t in stamps)):
                raise ValueError('unexpected rate-state content')
        except (OSError, ValueError) as exc:
            self._log('rate_state_corrupt', message=short(exc), action='counted as a full cap this cycle; file reset')
            try:
                write_json(path.parent, name, {'version': 1, 'generations': []})
            except (OSError, ValueError) as again:
                self._log('rate_state_unwritable', message=short(again))
            return None
        return [t for t in stamps if abs(now - t) < 3600]

    def _rate_block(self, now):
        stamps = self._generation_times(now)
        if stamps is None:
            return 'rate_state_unusable'
        return 'rate_cap' if len(stamps) >= self.max_per_hour else None

    def _record_generation(self, now):
        stamps = self._generation_times(now)
        if stamps is None:
            return False
        name = self.seat + '-generations.json'
        try:
            write_json(self._file(name).parent, name, {'version': 1, 'generations': stamps + [round(now, 3)]})
        except (OSError, ValueError) as exc:
            self._log('rate_state_unwritable', message=short(exc))
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

    # -- quiet: the worker's own windows, plus the shared GPU calendar when one is configured ------
    def _calendar(self):
        """(windows, None) from the shared calendar, (None, reason) when it cannot be used, and
        (None, None) when no calendar is configured. Read afresh each time, so a fixed file
        takes effect on the next poll."""
        if self.gpu_calendar is None:
            return None, None
        try:
            return gpu_calendar.load_calendar(self.gpu_calendar), None
        except gpu_calendar.CalendarError as exc:
            return None, short(exc, 200)
        except Exception as exc:        # last resort: whatever the file did, the worker stays quiet and says so
            return None, '%s while reading the calendar' % type(exc).__name__

    def _quiet(self, now, span):
        """None when a generation of `span` seconds may start now; otherwise (reason, fields) for
        the deferral. An unusable calendar means quiet: better idle than on the wrong GPU day."""
        window = self.quiet.conflict(now, span)
        if window:
            return 'quiet_window', {'window': window[2]}
        windows, problem = self._calendar()
        if problem:
            return 'gpu_calendar_unusable', {'calendar_reason': problem}
        if windows is not None:
            hit = gpu_calendar.quiet_window(now, windows, span, self.quiet.central)
            if hit:
                return 'quiet_window', {'window': hit.label, 'source': 'gpu_calendar', 'owner': hit.owner}
        return None

    def _next_quiet_start(self, now):
        starts = [self.quiet.next_start(now)]
        windows, _ = self._calendar()
        if windows is not None:
            starts.append(gpu_calendar.next_start(now, windows, self.quiet.central))
        starts = [s for s in starts if s is not None]
        return min(starts) if starts else None

    def _calendar_status(self, now):
        windows, problem = self._calendar()
        status = {'path': str(self.gpu_calendar), 'state': 'unusable' if problem else 'ok', 'reason': problem,
                  'windows': len(windows) if windows is not None else None}
        if windows is not None:
            hit = gpu_calendar.quiet_window(now, windows, 0, self.quiet.central)
            status['quiet_now'] = hit.label if hit else None
        return status

    def _write_status(self, result):
        """<seat>-status.json: what Shane needs to see why a task sits; never tokens or content."""
        now = self.clock()
        previous = self._status()
        status = {'version': 1, 'seat': self.seat, 'actor': self.actor, 'model': self.model,
                  'ollama': getattr(self.ollama, 'base_url', None), 'pid': os.getpid(), 'last_cycle_at': _iso(now),
                  'last_cycle_central': self.quiet.central.local(now).isoformat(timespec='minutes'),
                  'last_result': result.get('status'), 'last_reason': short(result.get('reason') or result.get('code') or '', 200) or None,
                  'last_task_id': result.get('task_id'),
                  'last_deferral': previous.get('last_deferral') if type(previous.get('last_deferral')) is dict else None,
                  'notes_posted': self._notes()[-50:]}
        if result.get('status') == 'deferred':
            status['last_deferral'] = {'reason': result.get('reason'), 'task_id': result.get('task_id'),
                                       'at': status['last_cycle_at']}
        if self.gpu_calendar is not None:
            status['gpu_calendar'] = self._calendar_status(now)
        try:
            write_json(self._file(self.seat + '-status.json').parent, self.seat + '-status.json', status)
        except (OSError, ValueError) as exc:
            self._log('status_write_failed', message=short(exc))

    # -- the usage meter: one line per job, numbers and names only (USAGE_FORMAT.md) ---------------
    METERED = {'submitted': 'finished', 'asked': 'question', 'failed': 'failed'}
    SKIPPED = frozenset({'quiet_window', 'gpu_calendar_unusable'})

    def _meter(self, result):
        """Exactly one usage line for a job this cycle finished, asked about or failed; one for a job held
        back by a quiet window (once per task, reason and Central day, so a long window does not
        write a line every poll). A cycle that ended in an error after the model ran (the room refused the ask,
        the staged draft or the finish, or something unexpected broke) is one failed line with the measured
        seconds and tokens, found through self._job because such a result carries no task id. Deferrals for other
        reasons (rate cap, Ollama down, model missing) and room refusals before any model call ran nothing and
        write none. A meter that cannot write is logged and never stops the job."""
        job = self._job or {}
        status, task_id = result.get('status'), result.get('task_id') or job.get('task_id')
        if not task_id:
            return
        if job.get('task_id') != task_id:
            job = {}
        if status in self.METERED:
            outcome = self.METERED[status]
        elif status == 'error' and job:
            outcome = 'failed'
        elif status == 'deferred' and result.get('reason') in self.SKIPPED:
            key = (task_id, result['reason'], self.quiet.central.local(self.clock()).date())
            if key in self._skips:
                return
            self._skips.add(key)
            outcome = 'skipped_quiet'
        else:
            return
        usage.try_append(self.runtime, self.seat, {
            'ts': self.clock(), 'kind': 'local', 'task_id': task_id, 'model': self.model,
            'seconds': job.get('seconds', 0.0), 'prompt_tokens': job.get('prompt_tokens'),
            'output_tokens': job.get('output_tokens'), 'outcome': outcome}, self._log)

    def _deferral_note(self, task, reason):
        """At most one Lounge note per (task, reason, Central day), so a task sitting acknowledged
        has a visible reason. The request id is deterministic, so the room itself refuses a repeat."""
        if reason not in NOTE_REASONS:
            return
        day = self.quiet.central.local(self.clock()).date()
        request_id = 'note-%s-%s-%s' % (task['id'], reason.replace('_', '-'), day.strftime('%Y%m%d'))
        posted = self._notes()
        if request_id in posted:
            return
        if reason == 'model_missing':
            what = ('model %s is not listed by Ollama at %s. The worker never pulls models: install it on that host '
                    'or point this seat at an installed model' % (self.model, getattr(self.ollama, 'base_url', None)))
        else:
            what = 'Ollama at %s did not answer its health check (/api/tags)' % getattr(self.ollama, 'base_url', None)
        body = ('%s cannot draft task %s ("%s") yet: %s. The task stays acknowledged with no strike, and the worker '
                'retries every cycle. (At most one such note per task, reason and Central day: %s.)'
                % (self.actor, task['id'], short(task['title'], 80), what, day.isoformat()))
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
        self._job = None
        try:
            try:
                result = self._cycle()
            except CrewError as exc:
                self._log('room_refused', code=exc.code, message=short(exc))
                result = {'status': 'error', 'code': exc.code, 'reason': 'room_refused'}
            except Exception as exc:
                self._meter({'status': 'error', 'reason': type(exc).__name__})
                self._write_status({'status': 'error', 'code': 'exception', 'reason': type(exc).__name__})
                raise
            self._meter(result)
            self._write_status(result)
            self._log('cycle_result', **result)
            return result
        finally:
            if held_here:
                self._release_seat()

    def run_forever(self, poll=30, max_cycles=None):
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
        """The result of a cycle that took no job because the STOP flag is present."""
        self._log('stopped', reason='STOP flag', task_id=task_id, **fields)
        result = {'status': 'stopped', 'reason': STOP_REASON}
        if task_id is not None:
            result['task_id'] = task_id
        return result

    def _cycle(self):
        if stop_flag_present(self.runtime):      # before anything: no ack, no claim, no model call
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

    def _refuse(self, task, state, spec, reason, kind):
        fence = self._hold(task, state)
        self._log('input_refused', task_id=task['id'], input=short(spec, 200), reason=short(reason, 300), kind=kind)
        question = ('Input refused: %s - %s. No model call was made and no strike was recorded. Needs Shane: correct '
                    'the INPUT line or the file (paths are relative to a read root; text files only), then resolve or '
                    're-issue the task.' % (short(spec, 300), reason))
        return self._ask(task['id'], fence, question, kind)

    def _deferred(self, task_id, reason, **fields):
        self._log('deferred', task_id=task_id, reason=reason, **fields)
        return {'status': 'deferred', 'reason': reason, 'task_id': task_id}

    def _work(self, task, state):
        task_id = task['id']
        if stop_flag_present(self.runtime):
            return self._stopped(task_id)
        now = self.clock()
        held = self._quiet(now, self.timeout)
        if held:
            return self._deferred(task_id, held[0], **held[1])
        inputs, refusal = load_inputs(task['body'], self.roots)
        if refusal:
            return self._refuse(task, state, refusal[0], refusal[1], 'input_refused')
        if is_helper(task):
            granted, why = owner_named_files(task, {t['id']: t for t in state.get('tasks', [])}, self.roots,
                                             fetch=lambda tid: self.client.handoff('task_view', {'task_id': tid})['task'])
            rule = 'helper tasks may only read files the owner named in the original task'
            if granted is None:
                return self._refuse(task, state, 'helper chain', '%s, and %s (confused-deputy rule)' % (rule, why),
                                    'helper_input_refused')
            allowed, root_id = granted
            for item in inputs:
                if item.path not in allowed:
                    return self._refuse(task, state, item.label, '%s (%s); this one was delegated by %s through a helper '
                                        'request and names a file the owner did not (confused-deputy rule)'
                                        % (rule, root_id, task.get('sender')), 'helper_input_refused')
        messages = build_messages(task, inputs)
        estimate = estimate_prompt_tokens(messages)
        if estimate + self.num_predict > self.num_ctx:
            fence = self._hold(task, state)
            question = ('Prompt does not fit the model context: estimated %d prompt tokens (UTF-8 bytes / 3, rounded up) '
                        '+ num_predict %d = %d, above num_ctx %d. No model call was made and no strike was recorded. '
                        'Needs Shane: name fewer or shorter inputs (#L<a>-L<b> ranges) or raise num_ctx for this worker, '
                        'then resolve to retry.' % (estimate, self.num_predict, estimate + self.num_predict, self.num_ctx))
            return self._ask(task_id, fence, question, 'context_too_small')
        blocked = self._rate_block(now)
        if blocked:
            return self._deferred(task_id, blocked, max_per_hour=self.max_per_hour)
        try:
            names = self.ollama.tags(timeout=min(10.0, self.timeout))
        except OllamaUnavailable as exc:
            self._deferral_note(task, 'ollama_unavailable')
            return self._deferred(task_id, 'ollama_unavailable', kind=exc.kind, message=short(exc))
        if not model_listed(self.model, names):
            self._deferral_note(task, 'model_missing')
            return self._deferred(task_id, 'model_missing', model=self.model)
        fence = self._hold(task, state)
        return self._generate_and_submit(task, inputs, fence, messages, estimate)

    def _hold(self, task, state):
        """Resume our own live lease after a restart, or claim at the current fence."""
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

    def _ensure_lease(self, task_id, fence):
        """Before generating: the lease must still be ours and outlast timeout + 120 s."""
        state = self.client.state()
        task = self.client.task_in(state, task_id)
        lease = task.get('lease') or {}
        room_now = state['server_time']
        if (task.get('status') != 'claimed' or lease.get('holder') != self.actor or lease.get('fence') != fence
                or lease.get('until', 0) <= room_now):
            raise CrewError('lease', 'This worker lease is missing, expired or fenced out.')
        if lease['until'] - room_now >= self.timeout + LEASE_MARGIN:
            return
        request_id, _ = self.renew_ids.renew(self.client, task_id, fence, LEASE_SECONDS)
        self._log('renewed', task_id=task_id, fence=fence, request_id=request_id)

    def _ask(self, task_id, fence, question, reason):
        question = short(question, QUESTION_LIMIT)
        self.client.command('wait_decision', {'task_id': task_id, 'fence': fence, 'question': question},
                            'wait-%s-f%d' % (task_id, fence))
        self._log('asked_owner', task_id=task_id, fence=fence, reason=reason)
        return {'status': 'asked', 'reason': reason, 'task_id': task_id, 'fence': fence}

    def _generate_and_submit(self, task, inputs, fence, messages, estimate):
        task_id = task['id']
        attempt = 0
        while True:
            attempt += 1
            self._ensure_lease(task_id, fence)
            if stop_flag_present(self.runtime):      # also before a retry; the claim is left to lapse with no strike
                return dict(self._stopped(task_id, claim_left=True, fence=fence), fence=fence)
            now = self.clock()
            # Re-checked right before every chat, retries included. On a conflict the claim is left
            # in place: under release-v1 the lease lapses without a strike, and the next cycle
            # resumes it (or re-claims it) once the window has passed. No owner step is needed.
            held = self._quiet(now, self.timeout)
            if held:
                result = self._deferred(task_id, held[0], **held[1], stage='before_generation',
                                        claim_left=True, fence=fence)
                return dict(result, fence=fence)
            if not self._record_generation(now):
                return dict(self._deferred(task_id, 'rate_state_unusable', claim_left=True, fence=fence), fence=fence)
            keep_alive = keep_alive_for(self.keep_alive, now, self._next_quiet_start(now))
            payload = {'model': self.model, 'messages': messages, 'stream': False, 'keep_alive': keep_alive,
                       'options': {'num_ctx': self.num_ctx, 'num_predict': self.num_predict, 'temperature': 0.2}}
            self._log('generating', task_id=task_id, fence=fence, attempt=attempt, keep_alive=keep_alive,
                      inputs=len(inputs), prompt_estimate=estimate)
            started = self.monotonic()
            self._job = self._job or {'task_id': task_id, 'seconds': 0.0}
            try:
                reply = self.ollama.chat(payload, timeout=self.timeout)
                break
            except OllamaUnavailable as exc:
                self._job['seconds'] += self.monotonic() - started
                self._log('model_unavailable', task_id=task_id, kind=exc.kind, message=short(exc), attempt=attempt)
                if attempt == 1 and exc.retryable and self._rate_block(self.clock()) is None:
                    continue
                return self._ask(task_id, fence, 'Local model unavailable mid-task: %s: %s; resolve to retry.'
                                 % (exc.kind, short(exc, 300)), 'model_unavailable')
        wall = self.monotonic() - started
        self._job['seconds'] += wall
        self._unload_if_needed(keep_alive)
        message = reply.get('message')
        content = message.get('content') if type(message) is dict else None
        output = clean_text(content) if type(content) is str else ''
        stats = {k: reply[k] for k in ('prompt_eval_count', 'eval_count', 'total_duration')
                 if type(reply.get(k)) is int and reply[k] >= 0}
        self._job.update(prompt_tokens=stats.get('prompt_eval_count'), output_tokens=stats.get('eval_count'))
        done_reason = short(reply['done_reason'], 40) if type(reply.get('done_reason')) is str else ''
        if not output.strip():
            if reply.get('done_reason') == 'length':
                question = ('Local model spent its whole output budget without an answer (done_reason length; %s; '
                            'num_predict %d). A thinking model may have used it all on thinking. No strike was recorded '
                            'and nothing was saved. Needs Shane: raise num_predict (and num_ctx to match) for this worker, '
                            'or use a non-thinking model, then resolve to retry.' % (measured(stats), self.num_predict))
                return self._ask(task_id, fence, question, 'output_budget_spent')
            body = ('Local model returned empty or non-text output (model %s via Ollama; %s%s). One failure strike '
                    'recorded by the worker; nothing was saved.'
                    % (self.model, measured(stats), '; done_reason ' + done_reason if done_reason else ''))
            self.client.command('fail', {'task_id': task_id, 'fence': fence, 'body': short(body, BODY_LIMIT)},
                                'fail-%s-f%d' % (task_id, fence))
            self._log('failed', task_id=task_id, fence=fence, reason='empty_output')
            return {'status': 'failed', 'reason': 'empty_output', 'task_id': task_id, 'fence': fence}
        warning = ''
        if 'prompt_eval_count' in stats and stats['prompt_eval_count'] < estimate / 2:
            warning = ('WARNING: model may have truncated the input (prompt_eval_count %d vs estimate %d)'
                       % (stats['prompt_eval_count'], estimate))
        header = report_header(self.actor, self.model, task, inputs, stats, done_reason, wall, warning)
        report, truncated = fit_report(header + output, task_id, fence)
        ref = self.client.handoff('stage_text', {'task_id': task_id, 'fence': fence, 'text': report})
        ref = {'path': ref['path'], 'sha256': ref['sha256']}
        seconds = seconds_text(stats['total_duration'] / 1e9) if 'total_duration' in stats else 'not reported'
        listed = '; '.join('%s L%d-L%d file sha256 %s' % (i.label, i.first, i.last, i.sha256[:12]) for i in inputs)
        body = ('Unreviewed draft by %s using model %s via Ollama. Tokens reported by Ollama: prompt %s, output %s. '
                'Seconds (Ollama total_duration): %s. Inputs: %d. Report %s, %d bytes%s. This draft needs review '
                'against cited inputs before it counts.%s Reviewer checklist: (1) acceptance: %s (2) inputs: %s '
                '(3) check each path:line citation against its input, and every MISSING/INFERENCE label.'
                % (self.actor, self.model, stats.get('prompt_eval_count', 'not reported'),
                   stats.get('eval_count', 'not reported'), seconds, len(inputs), ref['path'],
                   len(report.encode('utf-8')), '; the worker truncated it at the 12000-byte cap' if truncated else '',
                   ' ' + warning + '.' if warning else '', short(task['acceptance'], 800),
                   short(listed or 'none named', 1200)))
        self.client.command('finish', {'task_id': task_id, 'fence': fence, 'body': short(body, BODY_LIMIT), 'refs': [ref]},
                            'finish-%s-f%d' % (task_id, fence))
        self._log('submitted', task_id=task_id, fence=fence, path=ref['path'], bytes=len(report.encode('utf-8')),
                  truncated=truncated, prompt_estimate=estimate, **stats)
        return {'status': 'submitted', 'task_id': task_id, 'fence': fence, 'path': ref['path'], 'truncated': truncated}

    def _unload_if_needed(self, sent):
        """A generation that ran long could leave the model resident into a quiet window."""
        if sent <= 0:
            return
        now = self.clock()
        if (keep_alive_for(sent, now, self._next_quiet_start(now)) >= sent
                and self._quiet(now, 0) is None):
            return
        try:
            self.ollama.unload(self.model, timeout=min(10.0, self.timeout))
            self._log('unloaded', model=self.model)
        except OllamaUnavailable as exc:
            self._log('unload_failed', kind=exc.kind, message=short(exc))


# -- Command line ---------------------------------------------------------------------------
SETTING_KEYS = frozenset({'credential', 'model', 'ollama', 'mode', 'poll', 'read_roots', 'timeout', 'num_ctx',
                          'num_predict', 'keep_alive', 'max_per_hour', 'quiet_windows', 'gpu_calendar'})
DEFAULTS = {'ollama': DEFAULT_OLLAMA, 'mode': 'once', 'poll': 30, 'timeout': 600, 'num_ctx': 16384,
            'num_predict': 3000, 'keep_alive': 600, 'max_per_hour': 12, 'quiet_windows': DEFAULT_QUIET_WINDOWS}
BOUNDS = {'poll': (5, 86400), 'timeout': (10, MAX_TIMEOUT), 'num_ctx': (256, 1048576), 'num_predict': (1, 131072),
          'keep_alive': (0, 86400), 'max_per_hour': (1, MAX_PER_HOUR)}


def default_read_roots(package_root=ROOT):
    """The estate root is the folder holding the Artifacts folder above this package
    (D:\\Work Area\\Artifacts\\common-room\\tree). Missing folders are skipped."""
    for parent in Path(package_root).absolute().parents:
        if parent.name.casefold() == 'artifacts':
            return [p for p in (parent, parent.parent / 'Lab Notes')
                    if p.is_dir() and not p.is_symlink() and not p.is_junction()]
    return []


def read_config(path):
    path = Path(path)
    data = path.read_bytes()
    if len(data) > 16384:
        raise ValueError('Worker config exceeds 16 KiB.')
    config = decode_request(data)
    unknown = set(config) - SETTING_KEYS
    if unknown:
        raise ValueError('Unknown worker config keys: ' + ', '.join(sorted(unknown)))
    if 'read_roots' in config:
        if type(config['read_roots']) is not list or any(type(r) is not str for r in config['read_roots']):
            raise ValueError('read_roots is a list of folder paths.')
        config['read_roots'] = [r if Path(r).is_absolute() else str(path.absolute().parent / r) for r in config['read_roots']]
    if 'gpu_calendar' in config:
        calendar = config['gpu_calendar']
        if type(calendar) is not str or not calendar:
            raise ValueError('gpu_calendar is the path of the shared GPU calendar file.')
        config['gpu_calendar'] = calendar if Path(calendar).is_absolute() else str(path.absolute().parent / calendar)
    return config


def build_parser():
    p = argparse.ArgumentParser(prog='python -B -m common_room.ollama_worker',
                                description='Local-model worker seat: drafts handoffs with Ollama for a separate reviewer.')
    p.add_argument('--credential', help='This seat\'s own <actor>-credential.json in the room runtime')
    p.add_argument('--model', help='Ollama model name, which must already be installed')
    p.add_argument('--ollama', help='http://<host>:<port> (default %s)' % DEFAULT_OLLAMA)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--once', dest='mode', action='store_const', const='once', help='One cycle, then exit (default)')
    mode.add_argument('--loop', dest='mode', action='store_const', const='loop', help='Keep polling')
    p.add_argument('--poll', type=int, help='Seconds between --loop cycles (default 30)')
    p.add_argument('--read-root', dest='read_roots', action='append', help='A folder INPUT: paths may name (repeatable)')
    p.add_argument('--config', help='Nonsecret JSON supplying any of these settings; flags override it')
    p.add_argument('--timeout', type=float, help='Seconds one generation may take (default 600, at most %d)' % MAX_TIMEOUT)
    p.add_argument('--num-ctx', dest='num_ctx', type=int)
    p.add_argument('--num-predict', dest='num_predict', type=int)
    p.add_argument('--keep-alive', dest='keep_alive', type=int)
    p.add_argument('--max-per-hour', dest='max_per_hour', type=int)
    p.add_argument('--gpu-calendar', dest='gpu_calendar', help='Shared GPU calendar file (see GPU_CALENDAR.md); unset keeps only this worker\'s own quiet windows')
    p.set_defaults(mode=None)
    return p


def parse_settings(argv=None, package_root=ROOT):
    args = build_parser().parse_args(argv)
    settings = dict(DEFAULTS)
    if args.config:
        settings.update(read_config(args.config))
    for key in ('credential', 'model', 'ollama', 'mode', 'poll', 'timeout', 'num_ctx', 'num_predict', 'keep_alive',
                'max_per_hour', 'gpu_calendar'):
        value = getattr(args, key)
        if value is not None:
            settings[key] = value
    if args.read_roots is not None:
        settings['read_roots'] = args.read_roots
    for key in ('credential', 'model'):
        if type(settings.get(key)) is not str or not settings[key]:
            raise ValueError('--%s is required (on the command line or in --config).' % key)
    if not MODEL_NAME.fullmatch(settings['model']):
        raise ValueError('Model names are 1-160 letters, digits and . _ : / -')
    OllamaClient(settings['ollama'])
    if settings['mode'] not in ('once', 'loop'):
        raise ValueError('mode is once or loop.')
    for key, (low, high) in BOUNDS.items():
        value = settings[key]
        allowed = (int, float) if key == 'timeout' else (int,)
        if type(value) not in allowed or not low <= value <= high:
            raise ValueError('%s must be a number from %s to %s.' % (key, low, high))
    parse_quiet_windows(list(settings['quiet_windows']))
    if settings.get('gpu_calendar') is not None and (type(settings['gpu_calendar']) is not str
                                                     or not settings['gpu_calendar']):
        raise ValueError('gpu_calendar is the path of the shared GPU calendar file.')
    if 'read_roots' in settings:
        settings['read_roots'] = [Path(r).absolute() for r in settings['read_roots']]
    else:
        settings['read_roots'] = default_read_roots(package_root)
    return settings


def build_worker(settings, clock=time.time, log=None):
    return Worker(CrewClient(settings['credential']), settings['model'], OllamaClient(settings['ollama']),
                  settings['read_roots'], clock=clock, log=log, timeout=settings['timeout'],
                  num_ctx=settings['num_ctx'], num_predict=settings['num_predict'], keep_alive=settings['keep_alive'],
                  max_per_hour=settings['max_per_hour'], quiet_windows=settings['quiet_windows'],
                  gpu_calendar=settings.get('gpu_calendar'))


def main(argv=None, clock=time.time):
    """Exit codes for --once: 0 worked or idle, 2 deferred (including another process holding
    this seat), 1 error."""
    try:
        settings = parse_settings(argv)
        worker = build_worker(settings, clock=clock)
    except SystemExit as exc:            # argparse usage errors and --help
        return 0 if exc.code in (0, None) else 1
    except (ValueError, OSError) as exc:
        _print_line(json.dumps({'event': 'config_error', 'message': short(exc)}, ensure_ascii=True))
        return 1
    worker._log('started', mode=settings['mode'], model=settings['model'], ollama=settings['ollama'],
                read_roots=[str(r) for r in worker.roots], clock=worker.quiet.central.source,
                seat_files=str(worker.runtime / 'workers'),
                **({'gpu_calendar': str(worker.gpu_calendar)} if worker.gpu_calendar is not None else {}))
    if settings['mode'] == 'loop':
        try:
            results = worker.run_forever(settings['poll'])       # returns only if the seat is busy
        except KeyboardInterrupt:
            worker._log('stopped', reason='interrupted')
            return 0
        return EXIT_CODES.get(results[-1].get('status'), 1) if results else 0
    try:
        result = worker.run_once()
    except Exception as exc:
        worker._log('cycle_error', error=type(exc).__name__, message=short(exc))
        return 1
    return EXIT_CODES.get(result.get('status'), 1)


if __name__ == '__main__':
    sys.exit(main())
