"""The outbound filter and the board snapshot for the duet seat (plan duet-seat-2026-10-30).

Duet is Shane's cloud chief-of-staff app. It cannot reach this PC's loopback room; its only bridge is the
Google-Drive-synced folder DuetRelay. Everything the relay writes into that folder passes through this module
first, so the folder is a public-ish surface and is treated as one.

The relay removes known secret shapes, but long-path tails, 64-hex strings and lowercase slug passphrases can pass,
and any task title, recent Lounge note or question to or from duet can reach the Drive mirror, so no seat puts a
secret in one.

WHAT LEAVES THE PC, when `common_room.duet_relay` writes under DuetRelay (the fields below, filtered as described):

  1. mirror/board.json, built by build_mirror() from duet's own /api/state, WHITELIST only:
       generated_at, stop (active/by/at/reason), tasks (id, title, status, sender, recipient, builder, parent_id,
       authority_id, lease holder and until, failed_attempts, needs_shane), the last 50 Lounge notes (seq, sender,
       authority_id, timestamp, body), the Needs-Shane list, questions to and from duet, participants (id, role,
       contact_age_s).
     NOT sent: instance_id, origin, connection, labels, session strings, sha256 fields, history, task bodies,
     room_renewed, authorities, anything else in the state body.
  2. responses/resp-*.json: the room's answer to a request duet made, with request_id, kind, request_file and
     request_sha256 (a hash of the request file duet itself wrote).
       - A command answer is the room's own reply to that command, scrubbed.
       - The four handoff operations (stage_text, view_text, receipt, task_view) are allowed ONLY on a task whose
         recipient or sender is duet (anything else is refused `not_yours` before it is sent), and each reply is cut
         down to a whitelist before it is scrubbed:
           task_view  -> task {id, title, status, sender, recipient, builder, parent_id, authority_id, fence, lease
                         (holder, fence, until), failed_attempts, body, acceptance} and events [{seq, kind, actor,
                         timestamp}] (no event payloads). body and acceptance are sent only because it is duet's own
                         task; they are scrubbed like every string.
           receipt    -> {task_id, authority_id, parent_id, status, fence, sender, recipient, builder, reviewer,
                         lease, artifacts [{path, sha256}]}; no external_reference, no events.
           view_text  -> {path, sha256, text}, the text scrubbed.
           stage_text -> {path, sha256}.
  3. README.md: a constant text.

Every value is run through scrub() before it is turned into bytes, and the exact bytes go through guard() before
they are written. scrub() removes:
  - dict keys that name a secret: token, secret, password, cookie, session, credential, instance_id, origin,
    authorization, auth_token, key, *_key, *_token, *_secret and the like (NOT author, authorized_by, authority_id,
    origin_seq, tokens_used, session_count), and any key whose own text changes when scrubbed;
  - any string that holds one of the secrets it is given (duet's own room key and session cookie): the write is
    refused (BlockedSecret), not edited;
  - any string that names a credential-shaped place (a -credential.json file, keeper-credential, keeper-store, a
    .runtime folder, _Private, a secrets folder, a .env file): the whole string becomes
    "[withheld on the PC: private name]";
  - in other strings, a path-like or file-like word that matches the local workers' deny lists
    (ollama_worker.DENIED_NAMES / DENIED_COMPONENTS): "[private name]". The globs are applied to path- or
    file-shaped words only, never to ordinary prose words;
  - IPv4 and IPv6 addresses ("[ip]"), *.ts.net host names ("[host]"), a port after an address or host, email
    addresses ("[email]"), absolute paths including ones with spaces ("[path]"), money with or without a dollar
    sign ("[amount]"; a comma-grouped count such as "1,609 tests" needs a decimal part or a currency word to count),
    runs of 7 or more digits and "ending 4242"/"acct 1234" style fragments ("[number]"), a value after
    "password:"/"pwd=" and NAME_PASSWORD=/_TOKEN=/_KEY=/_SECRET= assignments ("[secret]"), and key-shaped
    runs of 40 or more URL-safe characters or with a known key prefix (xox*-, sk-, ghp_, gho_, github_pat_, AKIA)
    ("[key]"). Pure lowercase hex is kept only at exactly 64 characters (a sha256) and as the 48-hex name of a room
    result file (results/<hex>.txt); any other 40+ hex run, such as a full git commit id, becomes "[key]".
guard() then refuses the bytes if they still hold a secret, a -credential.json or keeper-credential name,
.runtime, _Private (any case), a key with a known prefix, or a key-shaped run of 43 or more URL-safe characters.

One note or other list item that holds duet's own room key is left out of the mirror (the rest is still written).
Plain numbers, booleans and null pass through unchanged. Task ids ("t" + 32 hex) are 33 characters and survive.
"""
import fnmatch, json, re

from .needs import room_needs
from .ollama_worker import DENIED_COMPONENTS, DENIED_NAMES

WITHHELD = '[withheld on the PC: private name]'
PRIVATE_NAME = '[private name]'
NOTE_LIMIT = 50
MAX_DEPTH = 64

DROP_KEYS = frozenset({
    'token', 'tokens', 'secret', 'secrets', 'password', 'passwd', 'pwd', 'passphrase', 'cookie', 'cookies', 'session',
    'sessions', 'session_id', 'credential', 'credentials', 'key', 'keys', 'api_key', 'instance_id', 'origin',
    'authorization', 'auth', 'auth_token', 'bearer', 'sha256'})
DROP_SUFFIXES = ('_token', '_secret', '_password', '_passwd', '_cookie', '_credential', '_credentials', '_key',
                 '_session_id', '_api_key')

# A string holding any of these is withheld whole (case-insensitive).
CREDENTIAL_SHAPED = re.compile(r'(?i)-credential\.json|keeper-credential|keeper-store|\.runtime|_private|secrets[\\/]|\.env\b')

# -- absolute paths. A drive or UNC path may hold single spaces ("D:\Work Area\x.md") while a separator still follows.
_WORD = r'[^\s\\/"\'<>|:]+'                        # no colon: a folder name never holds one, and "http:" is not one
_SEG = _WORD + r'(?: ' + _WORD + r'){0,3}'         # a folder name of up to four words ("My Old Tax Files")
_FILE = r'(?:(?:[^\s\\/"\'<>|.]+ ){0,5}' + _WORD + r'\.[A-Za-z0-9]{1,5}\b|[^\s"\'<>|]*)'   # "Chase Statement 2026-09.pdf"
_TAIL = r'(?:' + _SEG + r'[\\/]+)*' + _FILE
ABS_PATH = re.compile(
    r'(?:\\\\\?\\)?\b[A-Za-z]:[\\/]+' + _TAIL +                                    # C:\x, D:/x, \\?\D:\x
    r'|\\\\[^\s\\]+(?:\\' + _TAIL + r')?' +                                        # \\host\share\x
    r'|(?<![\w.])/[A-Za-z](?:/' + _TAIL + r'|(?=\s|$))' +                          # /d/x  (Git Bash drive root)
    r'|(?<![\w.])/(?:home|mnt|Users|users|opt|srv|var|etc|root|tmp|usr|Volumes)(?:/' + _TAIL + r'|(?=\s|$))')
PATH_TAIL = re.compile(r'\[path\](?: [^\s\\/\[\]]+){1,5}[\\/]\S*')   # safety net: a fragment the path match left behind
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b')   # a letter-only TLD: react@18.3.1 is not mail
IPV4 = re.compile(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w])')
_H = r'[0-9A-Fa-f]{1,4}'
IPV6 = re.compile(
    r'(?<![\w:])\[?(?:(?:' + _H + r':){6,7}' + _H +
    r'|(?:' + _H + r':){1,7}:(?:' + _H + r'(?::' + _H + r'){0,6})?'
    r'|::' + _H + r'(?::' + _H + r'){0,6})(?![\w:])\]?')
HOST = re.compile(r'(?<![\w.-])(?:[A-Za-z0-9-]+\.)+ts\.net\b', re.I)
PORT = re.compile(r'(\[(?:ip|host)\]):\d{1,5}\b')
MONEY = re.compile(
    r'\$\s?\d[\d,]*(?:\.\d+)?'
    r'|\b\d{1,3}(?:,\d{3})+\.\d{1,2}\b'              # 12,345.67 (1,609 tests is a count: only with a decimal or a currency word)
    r'|\b\d{3,}\.\d{2}\b'
    r'|(?i:\b(?:usd|eur|gbp|cad)\s?\d[\d,]*(?:\.\d+)?)'
    r'|(?i:\b\d[\d,]*(?:\.\d+)?\s?(?:usd|dollars?|bucks)\b)')
ACCOUNT = re.compile(r'(?i)\b(ending(?: in)?|acct\.?|account(?: (?:number|no\.?|#))?|card|routing)\s*#?:?\s*\d{3,}\b')
PASSWORD = re.compile(r'(?i)\b(password|passwd|pwd)\b(\s*[:=]\s*)(?!\[)\S+')                     # "password: x", not "the password rule"
ASSIGN = re.compile(r'(?i)\b([\w-]*(?:password|passwd|pwd|token|key|secret))(=\s*)(?!\[)[^\s,;&]+')   # "TOKEN=x", not "sort key = id"
IPV6_KNOWN = ('fd7a', 'fe80')                                                                     # Tailscale and link-local prefixes
DIGITS = re.compile(r'\b\d{7,}\b')
PREFIXED = re.compile(r'(?<![\w-])(?:eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]*|https://hooks\.slack\.com/services/\S+|xox[a-z]-[\w-]{8,}|sk-[\w-]{16,}|gh[pos]_\w{20,}|github_pat_\w{20,}|AKIA[0-9A-Z]{12,})')
RUN = re.compile(r'(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{40,}(?![A-Za-z0-9_-])')
HEXRUN = re.compile(r'[0-9a-f]+')
SLUG = re.compile(r'[a-z0-9]+(?:[-_][a-z0-9]+)+')
SHA256 = re.compile(r'[0-9a-f]{64}')
RESULT_NAME = re.compile(r'[0-9a-f]{48}')
FILE_EXT = re.compile(r'\.[A-Za-z]{1,5}$')
EDGE = '`"\'()[]{}<>,;:!?*'


class BlockedSecret(Exception):
    """A value about to be written holds a secret. Nothing is written for that item."""


def key_shaped(run, before='', after=''):
    """True for a run that looks like a room key (secrets.token_urlsafe(32), 43 characters). Exempt: exactly 64
    lowercase hex (a sha256), the 48-hex name of a room result file (results/<hex>.txt, `before`/`after` give the
    context), and a lowercase slug joined by - or _ (a request id, a branch name). Any other hex run is a key."""
    if SHA256.fullmatch(run):
        return False
    if RESULT_NAME.fullmatch(run) and before.endswith('results/') and after.startswith('.txt'):
        return False
    if HEXRUN.fullmatch(run):
        return True
    return not SLUG.fullmatch(run)


def _drop_key(name):
    low = name.lower().replace('-', '_')
    return low in DROP_KEYS or low.endswith(DROP_SUFFIXES) or 'sha256' in low


def _secret_list(secrets):
    return [s for s in (secrets or ()) if isinstance(s, str) and s]


def _holds(text, secrets):
    squeezed = ''.join(text.split())
    return any(s in text or s in squeezed for s in secrets)


def _word_is_private(word):
    core = word.strip(EDGE)
    if not core:
        return False
    pathlike = '\\' in core or '/' in core
    if not pathlike and not FILE_EXT.search(core):
        return False
    parts = [p for p in re.split(r'[\\/]', core) if p]
    low = [p.lower() for p in parts]
    if any(p in DENIED_COMPONENTS for p in low):
        return True
    base = low[-1] if low else ''
    return any(fnmatch.fnmatchcase(base, pattern) for pattern in DENIED_NAMES)


def _key_sub(match):
    text = match.string
    return '[key]' if key_shaped(match.group(0), text[max(0, match.start() - 8):match.start()],
                                 text[match.end():match.end() + 4]) else match.group(0)


def _redact_paths(text):
    """Absolute paths -> [path]. A path whose last part has no extension is a folder; if a capitalised word follows it
    (a folder name split on a space, "...\\Lab Notes") that one word goes with it. A lowercase word never does."""
    out, pos = [], 0
    for match in ABS_PATH.finditer(text):
        end = match.end()
        if not FILE_EXT.search(match.group(0)):
            tail = re.match(r' [A-Z][\w-]*', text[end:])
            if tail:
                end += tail.end()
        out.append(text[pos:match.start()])
        out.append('[path]')
        pos = end
    out.append(text[pos:])
    return ''.join(out)


def _ipv6_sub(match):
    """An IPv6 address needs 3 or more groups, or a known prefix (fd7a, fe80), or to be the loopback ::1; so
    "dead::beef" and "std::vector" survive."""
    text = match.group(0)
    core = text.strip('[]')
    groups = [g for g in core.split(':') if g]
    if len(groups) >= 3 or core == '::1' or (groups and groups[0].lower() in IPV6_KNOWN):
        return '[ip]'
    return text


def _scrub_string(text, secrets):
    if secrets and _holds(text, secrets):
        raise BlockedSecret('A value held a private value.')
    if CREDENTIAL_SHAPED.search(text):
        return WITHHELD
    pieces = re.split(r'(\s+)', text)
    for i in range(0, len(pieces), 2):
        word = pieces[i]
        if word and _word_is_private(word):
            pieces[i] = PRIVATE_NAME
    out = ''.join(pieces)
    out = PASSWORD.sub(lambda m: m.group(1) + m.group(2) + '[secret]', out)
    out = ASSIGN.sub(lambda m: m.group(1) + m.group(2) + '[secret]', out)
    out = PREFIXED.sub('[key]', out)
    out = EMAIL.sub('[email]', out)
    out = _redact_paths(out)
    while True:
        tighter = PATH_TAIL.sub('[path]', out)
        if tighter == out:
            break
        out = tighter
    out = IPV4.sub('[ip]', out)
    out = IPV6.sub(_ipv6_sub, out)
    out = HOST.sub('[host]', out)
    out = PORT.sub(r'\1', out)
    out = ACCOUNT.sub(lambda m: m.group(1) + ' [number]', out)
    out = MONEY.sub('[amount]', out)
    out = DIGITS.sub('[number]', out)
    out = RUN.sub(_key_sub, out)
    return out


def scrub(value, secrets=(), keep_sha256=False, _depth=0):
    """A new JSON-able copy of `value` with private things removed (see the module docstring).

    `keep_sha256` keeps a key named like sha256 when its value is a 64-hex digest: the room's own answers to
    stage_text/import carry {path, sha256}, which duet needs to finish a task. The mirror never sets it."""
    secrets = _secret_list(secrets)
    if _depth > MAX_DEPTH:
        return '[too deep]'
    if isinstance(value, str):
        return _scrub_string(value, secrets)
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            name = key if isinstance(key, str) else str(key)
            if _drop_key(name):
                if keep_sha256 and name.lower().endswith('sha256') and isinstance(item, str) and SHA256.fullmatch(item):
                    out[name] = item
                continue
            clean_name = _scrub_string(name, secrets)
            if clean_name != name:           # a key that names a path, an address or a secret is dropped, not edited
                continue
            out[name] = scrub(item, secrets, keep_sha256, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [scrub(item, secrets, keep_sha256, _depth + 1) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _scrub_string(str(value), secrets)


def guard(data_bytes, secrets=()):
    """Final check on the exact bytes about to be written. Raises BlockedSecret on any hit."""
    text = data_bytes.decode('utf-8', 'replace') if isinstance(data_bytes, (bytes, bytearray)) else str(data_bytes)
    secrets = _secret_list(secrets)
    for secret in secrets:
        escaped = json.dumps(secret)[1:-1]
        if secret in text or escaped in text or secret in ''.join(text.split()):
            raise BlockedSecret('The bytes held a private value.')
    low = text.lower()
    for needle in ('-credential.json', 'keeper-credential', '.runtime', '_private'):
        if needle in low:
            raise BlockedSecret('The bytes held a private name.')
    if PREFIXED.search(text):
        raise BlockedSecret('The bytes held a key with a known prefix.')
    for match in re.finditer(r'(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43,}(?![A-Za-z0-9_-])', text):
        if key_shaped(match.group(0), text[max(0, match.start() - 8):match.start()], text[match.end():match.end() + 4]):
            raise BlockedSecret('The bytes held a key-shaped run.')


def scrub_mirror(mirror, secrets=()):
    """Scrub a build_mirror() dict item by item. An item (one note, one task, ...) that holds one of `secrets` is left
    out and counted; the rest is kept. Returns (clean mirror, number of items left out)."""
    left_out = 0
    clean = {}
    for key, value in mirror.items():
        if isinstance(value, list):
            rows = []
            for item in value:
                try:
                    rows.append(scrub(item, secrets))
                except BlockedSecret:
                    left_out += 1
            clean[key] = rows
        else:
            try:
                clean[key] = scrub(value, secrets)
            except BlockedSecret:
                left_out += 1
                clean[key] = None
    return clean, left_out


def _lease(task):
    lease = task.get('lease')
    if not isinstance(lease, dict):
        return None
    return {'holder': lease.get('holder'), 'until': lease.get('until')}


def _task_row(task):
    row = {'id': task.get('id'), 'title': task.get('title'), 'status': task.get('status'),
           'sender': task.get('sender'), 'recipient': task.get('recipient'), 'builder': task.get('builder'),
           'parent_id': task.get('parent_id'), 'authority_id': task.get('authority_id'),
           'failed_attempts': task.get('failed_attempts', 0),
           'needs_shane': task.get('status') == 'waiting_decision'}
    lease = _lease(task)
    if lease is not None:
        row['lease'] = lease
    return row


def build_mirror(state, now_text=None):
    """The whitelisted board snapshot from a duet /api/state body. The caller scrubs and guards it."""
    state = state or {}
    stop = state.get('stop') or {}
    notes = sorted((n for n in state.get('notes') or [] if isinstance(n, dict)), key=lambda n: n.get('seq', 0))
    return {
        'format': 'duet-mirror/1',
        'generated_at': state.get('server_time'),
        'written_at': now_text,
        'stop': {k: stop.get(k) for k in ('active', 'by', 'at', 'reason') if k in stop},
        'tasks': [_task_row(t) for t in state.get('tasks') or [] if isinstance(t, dict)],
        'notes': [{'seq': n.get('seq'), 'sender': n.get('sender'), 'authority_id': n.get('authority_id'),
                   'timestamp': n.get('timestamp'), 'body': n.get('body')} for n in notes[-NOTE_LIMIT:]],
        'needs': room_needs(state) or [],
        'questions': state.get('questions') or [],
        'asked': state.get('asked') or [],
        'participants': [{'id': p.get('id'), 'role': p.get('role'), 'contact_age_s': p.get('contact_age_s')}
                         for p in state.get('participants') or [] if isinstance(p, dict)],
    }


# -- handoff answers: cut down to a whitelist before they are scrubbed ------------------------------------------------

def _lease_fields(lease):
    if not isinstance(lease, dict):
        return None
    return {k: lease.get(k) for k in ('holder', 'fence', 'until') if k in lease}


def _artifacts(rows):
    return [{'path': r.get('path'), 'sha256': r.get('sha256')} for r in rows or [] if isinstance(r, dict)]


def handoff_view(operation, result):
    """The part of a handoff reply that may leave the PC. Unknown shapes give nothing (fail closed)."""
    if not isinstance(result, dict):
        return {}
    if operation == 'task_view':
        task = result.get('task') if isinstance(result.get('task'), dict) else {}
        view = {k: task.get(k) for k in ('id', 'title', 'status', 'sender', 'recipient', 'builder', 'parent_id',
                                         'authority_id', 'fence', 'failed_attempts', 'body', 'acceptance') if k in task}
        if 'lease' in task:
            view['lease'] = _lease_fields(task['lease'])
        events = [{k: e.get(k) for k in ('seq', 'kind', 'actor', 'timestamp') if k in e}
                  for e in result.get('events') or [] if isinstance(e, dict)]
        return {'task': view, 'events': events}
    if operation == 'receipt':
        view = {k: result.get(k) for k in ('task_id', 'authority_id', 'parent_id', 'status', 'fence', 'sender',
                                           'recipient', 'builder', 'reviewer') if k in result}
        if 'lease' in result:
            view['lease'] = _lease_fields(result['lease'])
        view['artifacts'] = _artifacts(result.get('artifacts'))
        return view
    if operation == 'view_text':
        return {k: result.get(k) for k in ('path', 'sha256', 'text') if k in result}
    if operation == 'stage_text':
        return {k: result.get(k) for k in ('path', 'sha256') if k in result}
    return {}
