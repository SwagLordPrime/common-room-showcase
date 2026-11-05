"""Plan and decisions files: ONE implementation for the CLI and the owner's board.

`crew plan-approve` / `crew decide-file` (a Command Prompt, Shane types yes) and the board's
owner-only `/api/plans`, `/api/plan-approve` and `/api/decisions-apply` both come here, so both
use the SAME deterministic request ids and intake external references:

    plan task      external reference  <plan_id>-<key>
                   intake request id   plan-<plan_id>-<key>
                   deliver request id  plan-deliver-<plan_id>-<key>
    decision       request id          decide-|cancel-|reinstate-<task id>-f<fence at read time>

A plan approved on the board and then re-run at the CLI (or the reverse) is answered from the
room's own receipts and external-reference dedup: nothing is created twice.

Budget envelopes (#5): a plan task may carry `"reserve": true` (at most 3 per plan), marking
where the orchestrator may add work later without asking Shane again. The room is never told:
store.py caps EVERY approved (root) task at ROOT_JOB_CAP jobs in total however they are split,
so the envelope the CLI confirm and the board both state (envelope_lines) is enforced, and the
receipt lists the reserve keys.

Moved out of crew.py on 2026-09-24 (autonomy build, piece B, improvement #4); crew.py re-exports
every moved name. The approval and decisions loops take any client with
`command(action, payload, request_id)` and `state()`: the CLI passes its CrewClient, the board
passes OwnerRoomClient (the signed-in owner's own commands through the same store.execute the
board's /api/command uses). This module never reads a credential and never writes a token.

Fix-board, 2026-09-24 (after VERIFY7A M1 and L1):
- Applying a decisions file again (board then CLI, CLI then board, twice on the board) keeps
  what the earlier run applied. An entry whose earlier receipt result is ok, whose task, action
  and text are unchanged, and whose event the room's own history still shows (so a forged
  receipt cannot hide an answer) is carried forward as ok ("already applied") and not sent
  again; only the rest are sent, and the receipt holds the merged result.
- A receipt is written only into the folder its file was read from: the folder is checked again
  immediately before the write (not a link, the same folder by identity and, on the board,
  still inside the plans folder), and nothing is written otherwise.

fix2-board, 2026-09-24 (after VERIFY8A N1 and N3):
- An earlier success is carried forward only while the task has not moved on since the event it
  cites: a resolve whose task is waiting on a decision again (it asked again, failed out, or a
  review sent it back), or a reinstate whose task has recorded failures again, is sent, and the
  room decides; the confirm, the lines and the receipt (`earlier_seq`) say so. A receipt, forged
  or real, therefore never keeps back an answer the room would still take.
- A plan is checked for room keys, every field of every task, by the room itself before anything
  is sent (refuse_plan_keys), on the board and the CLI: one key anywhere refuses the whole plan
  and nothing is created.
"""
from pathlib import Path
import hashlib, json, os, re, stat, uuid

from .crew_client import CrewClient, CrewError, REQUEST_ID
from .protocol import RoomError, canonical
from .store import ROOT_JOB_CAP, tier_tags, undoable_mark

# The room's own published text limits (protocol.py / store.py), checked locally first.
LIMIT_TITLE = 120
LIMIT_BODY = 4000
LIMIT_ACCEPTANCE = 2000
LIMIT_CANCEL_REASON = 2000

ACTOR_RE = re.compile(r'[a-z][a-z0-9_-]{0,63}')

# Plan file contract (`crew-plan/1`).
PLAN_PROTOCOL = 'crew-plan/1'
MAX_PLAN_BYTES = 256 * 1024
MAX_PLAN_TASKS = 60
PLAN_ID_RE = re.compile(r'[a-z0-9][a-z0-9-]{0,39}')
TASK_KEY_RE = re.compile(r'[a-z0-9][a-z0-9-]{0,23}')
SCOPE_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}')
PLAN_FIELDS = {'protocol', 'plan_id', 'title', 'scope', 'tasks'}
PLAN_TASK_FIELDS = {'key', 'to', 'title', 'body', 'acceptance'}
PLAN_TASK_OPTIONAL = {'reserve', 'tier', 'effort', 'undoable'}   # reserve: a JSON true when present; A4: tier and effort, strings; A2b: undoable, a JSON true or false
# A2b: the three words Shane sees for a task's undoable mark, on the CLI confirm and the board (board.js markWords says the same)
MARK_WORDS = {True: 'Undoable', False: "Can't undo", None: "Not marked (counts as can't undo)"}


def mark_words(task):
    return MARK_WORDS[task.get('undoable')]
MAX_RESERVE_TASKS = 3
# Budget envelopes (#5). The room enforces all three numbers in store.py request_help: at most
# ROOT_JOB_CAP jobs under one approved task in total, 24 helpers straight under any one task
# (cancelled ones included) and 8 of those open at once.
ENVELOPE_DIRECT = 24
ENVELOPE_OPEN = 8
RESERVE_LINE = ('RESERVE [{key}]: the orchestrator may add work under this task without asking you: '
                'at most ' + str(ROOT_JOB_CAP) + ' jobs in total, ' + str(ENVELOPE_DIRECT) + ' directly, ' +
                str(ENVELOPE_OPEN) + ' open at once.')
PLAN_ENVELOPE_LINE = ('Every task in this plan can grow to at most ' + str(ROOT_JOB_CAP) +
                      ' jobs in total, however it is split.')

# Decisions file contract (`crew-decisions/1`).
DECISIONS_PROTOCOL = 'crew-decisions/1'
MAX_DECISIONS = 40
DECISION_FIELDS = {'task', 'action', 'text'}
DECISION_COMMAND = {'resolve': 'resolve_decision', 'cancel': 'cancel', 'reinstate': 'reinstate'}
DECISION_PREFIX = {'resolve': 'decide-', 'cancel': 'cancel-', 'reinstate': 'reinstate-'}

# The board's view of the plans folder (the orchestra folder): <plans root>/<plan id>/<file>.
PLAN_FILE_RE = re.compile(r'PLAN(?:-[1-9][0-9]{0,3})?\.json')
DECISIONS_FILE_RE = re.compile(r'DECISIONS-[1-9][0-9]{0,3}\.json')
SHA256_RE = re.compile(r'[0-9a-f]{64}')
MAX_PLAN_FOLDERS = 50
MAX_FILES_PER_FOLDER = 20
MAX_LISTING_BYTES = 8 * 1024 * 1024
MAX_FILE_ID = 120
MAX_CONFIRM = 200
DECISIONS_CONFIRM = 'apply'


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------

def check_text(value, limit, name='Text'):
    """Mirror common_room.store.text()'s rules, checked locally before sending."""
    if (type(value) is not str or not value.strip() or len(value) > limit
            or any(ord(c) < 32 and c not in '\n\r\t' for c in value)):
        raise CrewError('invalid', name + ' is missing or exceeds its ' + str(limit) + '-character limit.')
    return value


def resolve_task_id(state, ref):
    """Accept the full task id or an unambiguous prefix of at least 6 characters."""
    ids = [t['id'] for t in state.get('tasks', [])]
    if ref in ids:
        return ref
    if type(ref) is not str or len(ref) < 6:
        raise CrewError('invalid', 'Task reference must be the full id or a prefix of at least 6 characters.')
    matches = sorted(i for i in ids if i.startswith(ref))
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise CrewError('forbidden', 'No visible task matches "' + ref + '".')
    raise CrewError('ambiguous', 'That prefix matches more than one visible task: ' + ', '.join(matches))


def read_local_bytes(path, limit_bytes, name='File'):
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise CrewError('config', name + ' could not be read: ' + str(exc)) from exc
    if len(data) > limit_bytes:
        raise CrewError('invalid', name + ' exceeds ' + str(limit_bytes) + ' bytes.')
    return data


def decode_strict_json(data, name='File'):
    """Same duplicate-key and non-finite-number refusals as
    protocol.decode_request, but sized for a plan or decisions file (which
    can legitimately be much larger than a single command's 16 KiB body)."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CrewError('invalid', name + ' has a duplicate key: ' + str(key))
            result[key] = value
        return result

    def constant(value):
        raise CrewError('invalid', name + ' contains a non-finite number.')

    try:
        result = json.loads(data.decode('utf-8'), object_pairs_hook=unique, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise CrewError('invalid', name + ' must be unambiguous UTF-8 JSON.') from exc
    if not isinstance(result, dict):
        raise CrewError('invalid', name + ' must be a JSON object.')
    try:
        canonical(result)  # also catches decoder exponent overflow and lone surrogates
    except RoomError as exc:
        # 2026-09-24: was an uncaught RoomError (a traceback from the CLI); now the same
        # clean 'invalid' refusal as every other malformed file.
        raise CrewError('invalid', name + ' must be finite, valid JSON.') from exc
    return result


O_BINARY = getattr(os, 'O_BINARY', 0)
O_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
RECEIPT_REFUSED = ('The folder of "{name}" changed after the file was read (it is now a link, another folder, '
                   'or gone), so the receipt was not written. What was already sent to the room stands; '
                   'check the folder before doing anything else.')


def folder_mark(folder):
    """What the receipt write checks again: whether the folder is a link (or junction), and the
    identity (device, inode) of the folder it leads to. Taken before its file is read."""
    folder = Path(folder)
    try:
        link = stat.S_ISLNK(os.lstat(folder).st_mode) or _is_junction(folder)
        st = os.stat(folder)
    except OSError as exc:
        raise CrewError('config', 'The folder ' + str(folder) + ' could not be read: ' + str(exc)) from exc
    if not stat.S_ISDIR(st.st_mode):
        raise CrewError('config', str(folder) + ' is not a folder.')
    return (link, st.st_dev, st.st_ino)


def write_receipt(path, receipt, mark=None):
    """Write the receipt beside its file through a new temporary file (created exclusively, never
    through a link) and an atomic replace. With `mark` (folder_mark taken before the file was read)
    the folder is checked again immediately before writing, and a folder that is now a link, another
    folder or gone gets nothing (CrewError 'state')."""
    path = Path(path)
    payload = canonical(receipt).encode('utf-8')
    if mark is not None:
        try:
            now = folder_mark(path.parent)
        except CrewError:
            now = None
        if now != mark:
            raise CrewError('state', RECEIPT_REFUSED.format(name=path.name))
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_BINARY | O_NOFOLLOW, 0o666)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
        os.replace(tmp, path)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)


def read_receipt(path):
    """The receipt already beside `path` (PLAN.json or DECISIONS-<n>.json), read without following
    a link, or None when there is none or it cannot be used. Never trusted on its own: see
    earlier_successes."""
    receipt = receipt_path_for(path)
    try:
        st = os.lstat(receipt)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or _is_junction(receipt) or st.st_size > MAX_PLAN_BYTES:
        return None
    try:
        fd = os.open(receipt, os.O_RDONLY | O_BINARY | O_NOFOLLOW)
    except OSError:
        return None
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
            return None
        data = os.read(fd, MAX_PLAN_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        return decode_strict_json(data, 'Receipt')
    except CrewError:
        return None


def receipt_path_for(path):
    """PLAN.json -> PLAN.receipt.json, PLAN-2.json -> PLAN-2.receipt.json,
    DECISIONS-3.json -> DECISIONS-3.receipt.json: always beside the file."""
    path = Path(path)
    return path.with_name(path.stem + '.receipt.json')


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

def validate_plan(data):
    """Structural validation only (offline): unknown/duplicate fields, bounds
    and charsets. Membership and role checks against a live room happen
    separately in check_plan_against_state."""
    if type(data) is not dict or set(data) != PLAN_FIELDS:
        raise CrewError('invalid', 'Plan file must have exactly: protocol, plan_id, title, scope, tasks.')
    if data.get('protocol') != PLAN_PROTOCOL:
        raise CrewError('invalid', 'Unsupported plan protocol; expected "' + PLAN_PROTOCOL + '".')
    plan_id = data['plan_id']
    if type(plan_id) is not str or not PLAN_ID_RE.fullmatch(plan_id):
        raise CrewError('invalid', 'plan_id must be lowercase letters, digits and hyphens, at most 40 characters.')
    title = check_text(data['title'], LIMIT_TITLE, 'Plan title')
    scope = data['scope']
    if type(scope) is not str or not SCOPE_RE.fullmatch(scope):
        raise CrewError('invalid', 'scope must be a bounded authority id.')
    tasks = data['tasks']
    if type(tasks) is not list or not 1 <= len(tasks) <= MAX_PLAN_TASKS:
        raise CrewError('invalid', 'Plan must list from 1 through ' + str(MAX_PLAN_TASKS) + ' tasks.')
    seen = set()
    checked = []
    for i, raw_task in enumerate(tasks, 1):
        if (type(raw_task) is not dict or not PLAN_TASK_FIELDS <= set(raw_task) or
                set(raw_task) - PLAN_TASK_FIELDS - PLAN_TASK_OPTIONAL):
            raise CrewError('invalid', 'Task #' + str(i) + ' must have exactly: key, to, title, body, acceptance '
                                       '(and optionally "reserve": true, "tier", "effort" and "undoable": true or false).')
        key = raw_task['key']
        if type(key) is not str or not TASK_KEY_RE.fullmatch(key):
            raise CrewError('invalid',
                             'Task #' + str(i) + ' key must be lowercase letters, digits and hyphens, '
                             'at most 24 characters.')
        if key in seen:
            raise CrewError('invalid', 'Duplicate task key: ' + key)
        seen.add(key)
        to = raw_task['to']
        if type(to) is not str or not ACTOR_RE.fullmatch(to):
            raise CrewError('invalid', 'Task ' + key + ' "to" must be a bounded seat id.')
        t_title = check_text(raw_task['title'], LIMIT_TITLE, 'Task ' + key + ' title')
        t_body = check_text(raw_task['body'], LIMIT_BODY, 'Task ' + key + ' body')
        t_acceptance = check_text(raw_task['acceptance'], LIMIT_ACCEPTANCE, 'Task ' + key + ' acceptance')
        if 'reserve' in raw_task and raw_task['reserve'] is not True:
            raise CrewError('invalid', 'Task ' + key + ' "reserve" must be true when present (leave it out otherwise).')
        try:
            tags = tier_tags(raw_task)   # A4: the room's own rule, so a plan is refused before anything is created
            mark = undoable_mark(raw_task)   # A2b: the room's own rule, likewise; {} when the plan names no mark
        except RoomError as exc:
            raise CrewError('invalid', 'Task ' + key + ': ' + str(exc)) from exc
        checked.append(dict({'key': key, 'to': to, 'title': t_title, 'body': t_body, 'acceptance': t_acceptance,
                             'reserve': 'reserve' in raw_task}, **tags, **mark))
    if sum(t['reserve'] for t in checked) > MAX_RESERVE_TASKS:
        raise CrewError('invalid', 'At most ' + str(MAX_RESERVE_TASKS) + ' tasks in a plan may be marked "reserve".')
    return {'protocol': PLAN_PROTOCOL, 'plan_id': plan_id, 'title': title, 'scope': scope, 'tasks': checked}


def reserve_keys(plan):
    return [t['key'] for t in plan['tasks'] if t['reserve']]


def envelope_lines(plan):
    """The budget envelope in the words the CLI confirm and the board both show: one line per
    reserve task, then the line for the whole plan (the room caps every approved task)."""
    return [RESERVE_LINE.format(key=key) for key in reserve_keys(plan)] + [PLAN_ENVELOPE_LINE]


def parse_plan(raw):
    return validate_plan(decode_strict_json(raw, 'Plan file'))


def load_plan_file(path):
    path = Path(path)
    raw = read_local_bytes(path, MAX_PLAN_BYTES, 'Plan file')
    return parse_plan(raw), path, hashlib.sha256(raw).hexdigest()


def check_plan_against_state(plan, state):
    """Problems that only a live room can answer: is the scope visible to
    this caller, and is each task's `to` a worker member of that scope."""
    problems = []
    authorities = {a['id']: a for a in state.get('authorities', [])}
    scope = plan['scope']
    if scope not in authorities:
        problems.append('Scope "' + scope + '" is not visible to this actor.')
        return problems
    members = set(authorities[scope].get('members', []))
    roles = {p['id']: p['role'] for p in state.get('participants', [])}
    for task in plan['tasks']:
        to = task['to']
        if to not in members:
            problems.append('Task "' + task['key'] + '": "' + to + '" is not a member of scope "' + scope + '".')
        elif roles.get(to) != 'worker':
            problems.append('Task "' + task['key'] + '": "' + to + '" is not enrolled as a worker.')
    return problems


def plan_external_reference(plan, task):
    return plan['plan_id'] + '-' + task['key']


def _same_text(existing, field, text):
    """S0 F2: a quiet task's text fields arrive clipped (`clipped: True`); the clipped value must then be the start
    of the plan's text. A change past the clip is still refused by the room's own intake idempotency check."""
    value = existing.get(field)
    if value == text:
        return True
    return bool(existing.get('clipped')) and isinstance(value, str) and isinstance(text, str) and text.startswith(value)


def check_previous_approval_conflicts(plan, state):
    """F14: a visible task whose intake external_reference is `<plan_id>-<key>`
    (the id plan-approve always uses) but whose title, body, acceptance or
    recipient no longer matches this plan file was approved once already and
    then the plan file was edited under the same key. The room's own
    idempotency check would catch this mid-run, one task late and with an
    opaque message; this catches every such key up front, before the confirm,
    so the whole run refuses instead of half-applying. An already-approved
    task whose content is unchanged is fine (an idempotent re-run)."""
    conflicts = []
    for task in plan['tasks']:
        external_ref = plan_external_reference(plan, task)
        for existing in state.get('tasks', []):
            intake = existing.get('intake') or {}
            if (existing.get('authority_id') == plan['scope'] and
                    intake.get('source_namespace') == 'estate' and
                    intake.get('external_reference') == external_ref):
                if (existing.get('title') != task['title'] or not _same_text(existing, 'body', task['body']) or
                        not _same_text(existing, 'acceptance', task['acceptance']) or
                        existing.get('recipient') != task['to'] or
                        existing.get('tier') != task.get('tier') or existing.get('effort') != task.get('effort') or
                        existing.get('undoable') != task.get('undoable')):   # A2b: a changed mark is a changed brief
                    conflicts.append(task['key'])
                break
    return conflicts


def tasks_in_room(plan, state):
    """How many of this plan's tasks the room already holds (by external reference)."""
    held = {(t.get('authority_id'), (t.get('intake') or {}).get('source_namespace'),
             (t.get('intake') or {}).get('external_reference')) for t in state.get('tasks', [])}
    return sum((plan['scope'], 'estate', plan_external_reference(plan, t)) in held for t in plan['tasks'])


def refuse_unapprovable(plan, state):
    """The same two refusals, with the same words, on the CLI and the board:
    live-room problems first, then F14 conflicts; both before any confirm."""
    problems = check_plan_against_state(plan, state)
    if problems:
        raise CrewError('invalid', 'Plan has problems and cannot be approved: ' + ' | '.join(problems))
    conflicts = check_previous_approval_conflicts(plan, state)
    if conflicts:
        raise CrewError('invalid', ' | '.join(
            'Task "' + key + '" already approved with different content; give the changed task a new key, '
            'or use a new plan_id.' for key in conflicts))


def refuse_plan_keys(client, plan):
    """fix2-board (VERIFY8A N3): the room refuses a room key in any command, but one command at a
    time, so a key in task 2 left task 1 created and delivered. Every field of the plan is checked
    with the room first (client.check_keys: the room holds the keys and writes nothing), so one key
    anywhere refuses the whole plan before anything is sent. One check per task keeps each request
    inside the room's 16 KiB budget: it is never larger than that task's own intake command."""
    client.check_keys({name: plan[name] for name in ('plan_id', 'title', 'scope')})
    for task in plan['tasks']:
        client.check_keys(task)


def plan_summary(plan, plan_sha256):
    """F6: the confirm must show enough to actually decide on, not a truncated
    preview -- the plan file's own hash and task count, then per task the
    key, recipient, title, every INPUT: line (what a local worker may read),
    every FILES: line (what the build may change; `crew submit` checks it) and
    the full acceptance."""
    summary = ['plan file sha256: ' + plan_sha256 + '  (' + str(len(plan['tasks'])) + ' task(s))',
               'About to approve plan "' + plan['plan_id'] + '" (' + plan['title'] + ') in scope "' +
               plan['scope'] + '":']
    for i, task in enumerate(plan['tasks'], 1):
        summary.append(str(i) + '. [' + task['key'] + '] -> ' + task['to'] + ': ' + task['title'])
        for line in task['body'].splitlines():
            if line.startswith(('INPUT:', 'FILES:')):
                summary.append('     ' + line)
        summary.append('     acceptance: ' + task['acceptance'])
        summary.append('     mark: ' + mark_words(task))   # A2b: what dot's review rule reads
        if task['reserve']:
            summary.append('     ' + RESERVE_LINE.format(key=task['key']))
    summary.append(PLAN_ENVELOPE_LINE)
    return summary


def approve_plan(client, plan, state, emit=None):
    """Idempotent by construction: both the intake external_reference and
    every request id are derived only from plan_id and each task's own key,
    never randomly, so re-running this on the same plan file (from the CLI or
    the board, in either order) answers from the room's own receipts and
    dedup instead of creating a second copy of anything. Returns the
    key -> {task_id, status} map and one human line per task."""
    emit = emit or (lambda request_id: None)
    existing_ids = {t['id'] for t in state.get('tasks', [])}
    tasks_receipt = {}
    lines = []
    for task in plan['tasks']:
        message = {'title': task['title'], 'body': task['body'], 'recipient': task['to'],
                   'authority_id': plan['scope'], 'kind': 'handoff.record', 'acceptance': task['acceptance']}
        intake_request_id = 'plan-' + plan['plan_id'] + '-' + task['key']
        emit(intake_request_id)
        payload = {'source_namespace': 'estate', 'external_reference': plan_external_reference(plan, task),
                   'message': message}
        payload.update({k: task[k] for k in ('tier', 'effort') if task.get(k)})   # A4: only when the plan names them
        if 'undoable' in task:
            payload['undoable'] = task['undoable']   # A2b: the keeper's mark, by approving the plan; only when the plan names it
        resp = client.command('intake', payload, intake_request_id)
        task_id = resp['task']['id']
        already_existed = task_id in existing_ids
        deliver_request_id = 'plan-deliver-' + plan['plan_id'] + '-' + task['key']
        emit(deliver_request_id)
        client.command('deliver', {'task_id': task_id}, deliver_request_id)
        # A fresh read, never the command's own (possibly replayed) response.
        status = CrewClient.task_in(client.state(), task_id)['status']
        tasks_receipt[task['key']] = {'task_id': task_id, 'status': status}
        lines.append('[' + task['key'] + '] task ' + task_id +
                     (' already existed (status: ' + status + ')' if already_existed
                      else ' created and delivered to ' + task['to']))
    return tasks_receipt, lines


def plan_receipt(plan, state, tasks_receipt):
    """Never a token: plan_id, scope, approved_at, a key -> {task_id, status} map and the
    keys of the reserve tasks (where the orchestrator may add work without asking again)."""
    return {'plan_id': plan['plan_id'], 'scope': plan['scope'],
            'approved_at': state['server_time'], 'tasks': tasks_receipt, 'reserve': reserve_keys(plan)}


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

def validate_decisions(data):
    if type(data) is not dict or set(data) != {'protocol', 'decisions'}:
        raise CrewError('invalid', 'Decisions file must have exactly: protocol, decisions.')
    if data.get('protocol') != DECISIONS_PROTOCOL:
        raise CrewError('invalid', 'Unsupported decisions protocol; expected "' + DECISIONS_PROTOCOL + '".')
    decisions = data['decisions']
    if type(decisions) is not list or not 1 <= len(decisions) <= MAX_DECISIONS:
        raise CrewError('invalid', 'Decisions file must list from 1 through ' + str(MAX_DECISIONS) + ' entries.')
    checked = []
    for i, entry in enumerate(decisions, 1):
        if type(entry) is not dict or set(entry) != DECISION_FIELDS:
            raise CrewError('invalid', 'Entry #' + str(i) + ' must have exactly: task, action, text.')
        action = entry['action']
        if action not in ('resolve', 'cancel', 'reinstate'):
            raise CrewError('invalid', 'Entry #' + str(i) + ' action must be "resolve", "cancel" or "reinstate".')
        task_ref = entry['task']
        if type(task_ref) is not str or not task_ref:
            raise CrewError('invalid', 'Entry #' + str(i) + ' needs a task reference.')
        limit = LIMIT_CANCEL_REASON if action == 'cancel' else LIMIT_BODY
        text_value = check_text(entry['text'], limit, 'Entry #' + str(i) + ' text')
        checked.append({'task': task_ref, 'action': action, 'text': text_value})
    return checked


def parse_decisions(raw):
    return validate_decisions(decode_strict_json(raw, 'Decisions file'))


def load_decisions_file(path):
    return parse_decisions(read_local_bytes(Path(path), MAX_PLAN_BYTES, 'Decisions file'))


def text_digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def earlier_successes(receipt):
    """(task reference as written, action, text sha256) -> that earlier ok result, from a receipt
    this module wrote (a receipt from before 2026-09-24's fix-board has no text hash: nothing in it
    is carried)."""
    found = {}
    results = receipt.get('results') if type(receipt) is dict else None
    for r in results if type(results) is list else []:
        if (type(r) is dict and r.get('ok') is True and type(r.get('task')) is str and type(r.get('ref')) is str
                and r.get('action') in DECISION_COMMAND and type(r.get('text_sha256')) is str
                and type(r.get('seq')) is int):
            found.setdefault((r['ref'], r['action'], r['text_sha256']), r)
    return found


def room_confirms(state, earlier, text):
    """The room's own history, not the receipt file, says this answer was applied: the event the
    earlier run recorded (its seq) is this action on this task, with this text."""
    event = next((h for h in state.get('history', []) if h.get('seq') == earlier['seq']), None)
    if (event is None or event.get('task_id') != earlier['task'] or
            event.get('kind') != 'task_' + DECISION_COMMAND[earlier['action']]):
        return False
    detail = event.get('detail') or ''
    # S0 F3: the history detail of a quiet task is clipped; then it must be the start of the full text. The seq,
    # task and kind above already pin the one event the earlier run recorded.
    clipped = any(t.get('clipped') for t in state.get('tasks', []) if t.get('id') == earlier['task'])
    if earlier['action'] == 'resolve':
        return detail == text or (clipped and text.startswith(detail))
    if earlier['action'] == 'reinstate':
        if detail.endswith(' recorded failure(s): ' + text):
            return True
        lead = re.match(r'Owner reinstated after \d+ recorded failure\(s\): ', detail)
        return bool(clipped and lead and (lead.group(0) + text).startswith(detail))
    task = next((t for t in state.get('tasks', []) if t['id'] == earlier['task']), None)
    return task is not None and task.get('status') == 'cancelled' and task.get('cancel_note') == text


def moved_on(state, earlier):
    """fix2-board (VERIFY8A N1): whether the task has moved on since the earlier answer, so that the
    room could take this action on it again. That answer left a resolve's task delivered and a
    reinstate's task with no recorded failures, so a task now waiting on a decision (it asked
    again, failed out, or a review sent it back) or with failures again got there after it. Such
    an entry is sent, never carried. (A cancel is final: room_confirms sees the task cancelled.)"""
    task = next((t for t in state.get('tasks', []) if t['id'] == earlier['task']), {})
    if earlier['action'] == 'resolve':
        return task.get('status') == 'waiting_decision'
    if earlier['action'] == 'reinstate':
        return task.get('failed_attempts', 0) >= 1
    return False


def prepare_decisions(entries, state, receipt=None):
    """Resolve each entry's task reference against one state read and record
    the task's fence, status and current question at that moment. The fence is
    part of each entry's request id, so the id names exactly this answer to
    exactly this question. With the file's earlier `receipt`, an entry that run
    applied (unchanged, and confirmed by the room's history) is marked
    `already_applied` and will not be sent again, unless the task has moved on
    since (moved_on): then it is sent, marked with the earlier event's
    `earlier_seq` so the confirm, the lines and the receipt say so."""
    successes = earlier_successes(receipt)
    statuses = {t['id']: t['status'] for t in state.get('tasks', [])}
    prepared = []
    for entry in entries:
        item = dict(entry, already_applied=None, earlier_seq=None)
        try:
            tid = resolve_task_id(state, entry['task'])
            task = CrewClient.task_in(state, tid)
            item.update(task_id=tid, fence=task['fence'], question=task.get('decision'),
                        status=task['status'], error=None)
        except CrewError as exc:
            item.update(task_id=None, fence=None, question=None, status=None, error=exc.code + ': ' + str(exc))
        earlier = successes.get((entry['task'], entry['action'], text_digest(entry['text'])))
        # VERIFY9 N1: the receipt's own task must be this entry's task, not just its written reference.
        if earlier is not None and earlier.get('task') == item.get('task_id') and room_confirms(state, earlier, entry['text']):
            if moved_on(state, earlier):
                item['earlier_seq'] = earlier['seq']
            else:
                item['already_applied'] = {'task': earlier['task'], 'seq': earlier['seq'],
                                           'status': statuses.get(earlier['task'], earlier.get('status'))}
        prepared.append(item)
    return prepared


def sent_again_note(seq):
    return ('the room shows this same answer at event #' + str(seq) + ', but the task has asked again or failed '
            'since; it is sent again and the room decides')


def decisions_summary(prepared):
    summary = ['About to apply ' + str(len(prepared)) + ' decision(s):']
    for i, item in enumerate(prepared, 1):
        if item.get('already_applied'):
            summary.append(str(i) + '. ' + item['already_applied']['task'] + ' ' + item['action'] +
                           ' -- already applied (the room shows it); not sent again')
        elif item['error']:
            summary.append(str(i) + '. ' + item['task'] + ' ' + item['action'] + ' -- UNRESOLVED: ' + item['error'])
        else:
            summary.append(str(i) + '. ' + item['task_id'] + ' ' + item['action'] +
                           ' (current question: ' + (item['question'] or '(none)') + ')')
            summary.append('     -> ' + item['text'][:200])
            if item.get('earlier_seq') is not None:
                summary.append('     note: ' + sent_again_note(item['earlier_seq']))
    return summary


def apply_decisions(client, prepared, emit=None):
    """Applies each entry under its deterministic request id and keeps going
    past an individual refusal so one bad entry never blocks the rest. F11:
    'reinstate' is a third action alongside 'resolve' and 'cancel' -- the room
    itself enforces that only the owner may use it."""
    emit = emit or (lambda request_id: None)
    results = []
    for item in prepared:
        # Every result names the entry as written (ref), its action and its text's hash, so a later
        # run can tell an unchanged entry from an edited one.
        same = {'ref': item['task'], 'action': item['action'], 'text_sha256': text_digest(item['text'])}
        done = item.get('already_applied')
        if done:
            results.append(dict(same, task=done['task'], ok=True, status=done['status'], seq=done['seq'],
                                already_applied=True))
            continue
        if item['error']:
            results.append(dict(same, task=item['task'], ok=False, error=item['error']))
            continue
        tid = item['task_id']
        # fix2-board N1: an entry sent although the room shows the same answer earlier says so.
        again = {} if item.get('earlier_seq') is None else {'earlier_seq': item['earlier_seq']}
        request_id = DECISION_PREFIX[item['action']] + tid + '-f' + str(item['fence'])
        try:
            emit(request_id)
            answer = client.command(DECISION_COMMAND[item['action']], {'task_id': tid, 'body': item['text']}, request_id)
            status = CrewClient.task_in(client.state(), tid)['status']
            result = dict(same, task=tid, ok=True, status=status, **again)
            if type(answer) is dict and type(answer.get('seq')) is int:
                result['seq'] = answer['seq']
            results.append(result)
        except CrewError as exc:
            results.append(dict(same, task=tid, ok=False, error=exc.code + ': ' + str(exc), **again))
    return results


def decision_lines(results):
    lines = []
    for r in results:
        again = ('sent again: event #' + str(r['earlier_seq']) + ' was an earlier answer') if 'earlier_seq' in r else ''
        if r['ok'] and r.get('already_applied'):
            outcome = 'ok (already applied; status: ' + r.get('status', '') + ')'
        elif r['ok']:
            outcome = 'ok (' + r.get('status', '') + ('; ' + again if again else '') + ')'
        else:
            outcome = 'FAILED - ' + r['error'] + (' (' + again + ')' if again else '')
        lines.append(r['task'] + ' ' + r['action'] + ': ' + outcome)
    return lines


def decisions_receipt(state, results):
    """Written beside DECISIONS-<n>.json as DECISIONS-<n>.receipt.json, by the CLI and the board."""
    return {'applied_at': state['server_time'], 'results': results}


# ---------------------------------------------------------------------------
# The board: the owner's in-process client and the plans folder
# ---------------------------------------------------------------------------

class OwnerRoomClient:
    """The board's stand-in for CrewClient: `execute(action, payload, request_id)`
    is the signed-in owner's own store.execute (the same call the board's
    /api/command makes, so the same receipts table answers a CLI retry) and
    `state()` is that owner's own /api/state body. Only the five actions a plan
    or decisions file can need are passed through. `check_keys(value)` is the
    room's own key check (board.refuse_room_keys), which writes nothing."""
    ACTIONS = frozenset({'intake', 'deliver', 'resolve_decision', 'cancel', 'reinstate'})

    def __init__(self, execute, state, check_keys=None):
        self._execute = execute
        self._state = state
        self._check_keys = check_keys

    def check_keys(self, value):
        """A client built without the room's key check refuses: a missing check never reads as a clean plan."""
        if self._check_keys is None:
            raise CrewError('config', 'This board client cannot check a plan for room keys; nothing was sent.')
        try:
            self._check_keys(value)
        except RoomError as exc:
            raise CrewError(exc.code, str(exc)) from exc

    def command(self, action, payload, request_id):
        if action not in self.ACTIONS:
            raise CrewError('forbidden', 'The board approves plans and applies decisions only.')
        if type(request_id) is not str or not REQUEST_ID.fullmatch(request_id):
            raise CrewError('invalid', 'Request IDs are 1-128 letters, digits, _ . : or -.')
        try:
            return self._execute(action, payload, request_id)
        except RoomError as exc:
            raise CrewError(exc.code, str(exc)) from exc

    def state(self):
        return self._state()


def _same_path(a, b):
    """Equal paths, compared the way this OS compares them (case-insensitively on Windows)."""
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


def _is_junction(path):
    probe = getattr(Path(path), 'is_junction', None)
    return bool(probe and probe())


def resolve_plans_root(path):
    """None stays None. Otherwise the folder as an absolute, resolved path. A
    folder that does not exist yet is allowed (it lists nothing until it is
    created); a linked folder, or a path that is not a folder, is refused."""
    if path is None:
        return None
    candidate = Path(path).absolute()
    if candidate.is_symlink() or _is_junction(candidate):
        raise CrewError('config', 'The plans folder must not be a link: ' + str(candidate))
    if candidate.exists() and not candidate.is_dir():
        raise CrewError('config', 'The plans folder is not a folder: ' + str(candidate))
    return candidate.resolve()


def _checked_root(plans_root):
    """Re-checked on every request: the folder may appear later, and must not
    have become a link since the room started. Returns None while it is missing."""
    root = Path(plans_root)
    try:
        st = os.lstat(root)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CrewError('config', 'The plans folder could not be read: ' + str(exc)) from exc
    if stat.S_ISLNK(st.st_mode) or _is_junction(root) or not stat.S_ISDIR(st.st_mode):
        raise CrewError('config', 'The plans folder is now a link or not a folder; refused.')
    if not _same_path(root.resolve(strict=True), root):
        raise CrewError('config', 'The plans folder now resolves somewhere else; refused.')
    return root


def _checked_folder(root, folder):
    path = root / folder
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise CrewError('invalid', 'No plan folder "' + folder + '".') from None
    except OSError as exc:
        raise CrewError('config', 'Plan folder "' + folder + '" could not be read: ' + str(exc)) from exc
    if stat.S_ISLNK(st.st_mode) or _is_junction(path) or not stat.S_ISDIR(st.st_mode):
        raise CrewError('invalid', 'Plan folder "' + folder + '" is a link or not a folder; refused.')
    if not _same_path(path.resolve(strict=True), path):
        raise CrewError('invalid', 'Plan folder "' + folder + '" resolves outside the plans folder; refused.')
    return path


def _read_checked(root, folder, name, budget=None):
    """Read <root>/<folder>/<name> without following any link: a regular file
    with exactly one link, at most MAX_PLAN_BYTES, still the same file when
    opened as when checked."""
    file_id = folder + '/' + name
    folder_path = _checked_folder(root, folder)
    path = folder_path / name
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise CrewError('invalid', 'No file "' + file_id + '".') from None
    except OSError as exc:
        raise CrewError('config', '"' + file_id + '" could not be read: ' + str(exc)) from exc
    if stat.S_ISLNK(st.st_mode) or _is_junction(path) or not stat.S_ISREG(st.st_mode):
        raise CrewError('invalid', '"' + file_id + '" is a link or not a regular file; refused.')
    if st.st_nlink != 1:
        raise CrewError('invalid', '"' + file_id + '" is hard-linked elsewhere; refused.')
    if st.st_size > MAX_PLAN_BYTES:
        raise CrewError('invalid', '"' + file_id + '" exceeds ' + str(MAX_PLAN_BYTES) + ' bytes.')
    if budget is not None and st.st_size > budget[0]:
        raise CrewError('too_large', '"' + file_id + '" was not read: this list is already at its size budget; '
                                     'approve or apply it with the CLI.')
    if not _same_path(path.resolve(strict=True), path):
        raise CrewError('invalid', '"' + file_id + '" resolves outside its plan folder; refused.')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise CrewError('invalid', '"' + file_id + '" could not be opened without following a link: ' + str(exc)) from exc
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or
                (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino)):
            raise CrewError('invalid', '"' + file_id + '" changed while it was being read; refused.')
        chunks, remaining = [], MAX_PLAN_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    data = b''.join(chunks)
    if len(data) > MAX_PLAN_BYTES:
        raise CrewError('invalid', '"' + file_id + '" exceeds ' + str(MAX_PLAN_BYTES) + ' bytes.')
    if budget is not None:
        budget[0] -= len(data)
    return data


def _receipt_present(folder_path, name):
    receipt = receipt_path_for(folder_path / name)
    try:
        st = os.lstat(receipt)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and not _is_junction(receipt)


def _file_order(name):
    number = re.search(r'-([0-9]+)\.json$', name)
    return (0 if name.startswith('PLAN') else 1, int(number.group(1)) if number else 0)


def _plan_entry(root, folder, name, state, budget):
    entry = {'id': folder + '/' + name}
    try:
        raw = _read_checked(root, folder, name, budget)
        entry['sha256'] = hashlib.sha256(raw).hexdigest()
        plan = parse_plan(raw)
    except CrewError as exc:
        entry['error'] = exc.code + ': ' + str(exc)
        return entry
    entry.update(plan_id=plan['plan_id'], title=plan['title'], scope=plan['scope'],
                 tasks=[dict(t) for t in plan['tasks']],
                 problems=check_plan_against_state(plan, state),
                 conflicts=check_previous_approval_conflicts(plan, state),
                 envelope=envelope_lines(plan),
                 approved=_receipt_present(root / folder, name),
                 in_room=tasks_in_room(plan, state))
    return entry


def _decisions_entry(root, folder, name, state, budget):
    entry = {'id': folder + '/' + name}
    try:
        raw = _read_checked(root, folder, name, budget)
        entry['sha256'] = hashlib.sha256(raw).hexdigest()
        entries = parse_decisions(raw)
    except CrewError as exc:
        entry['error'] = exc.code + ': ' + str(exc)
        return entry
    entry['entries'] = [{k: item[k] for k in ('task', 'action', 'text', 'task_id', 'status', 'question', 'error')}
                        for item in prepare_decisions(entries, state)]
    entry['applied'] = _receipt_present(root / folder, name)
    return entry


def board_listing(plans_root, state):
    """GET /api/plans. Folders named like a plan id directly under the plans
    folder; in each, PLAN.json, PLAN-<n>.json and DECISIONS-<n>.json by strict
    name. Links are never followed: a linked folder is listed as refused, a
    linked, hard-linked or oversized file as an entry with its error."""
    if plans_root is None:
        return {'configured': False}
    listing = {'configured': True, 'root_exists': False, 'plans': [], 'decisions': [],
               'refused': [], 'truncated': []}
    root = _checked_root(plans_root)
    if root is None:
        return listing
    listing['root_exists'] = True
    try:
        with os.scandir(root) as found:
            names = sorted(e.name for e in found if PLAN_ID_RE.fullmatch(e.name))
    except OSError as exc:
        raise CrewError('config', 'The plans folder could not be listed: ' + str(exc)) from exc
    folders = []
    for name in names:
        try:
            _checked_folder(root, name)
        except CrewError as exc:
            listing['refused'].append({'id': name, 'reason': str(exc)})
            continue
        folders.append(name)
    if len(folders) > MAX_PLAN_FOLDERS:
        listing['truncated'].append('Only the first ' + str(MAX_PLAN_FOLDERS) + ' of ' + str(len(folders)) +
                                    ' plan folders are listed (by name).')
        folders = folders[:MAX_PLAN_FOLDERS]
    budget = [MAX_LISTING_BYTES]
    for folder in folders:
        try:
            with os.scandir(root / folder) as found:
                files = sorted((e.name for e in found if PLAN_FILE_RE.fullmatch(e.name) or DECISIONS_FILE_RE.fullmatch(e.name)),
                               key=_file_order)
        except OSError as exc:
            listing['refused'].append({'id': folder, 'reason': 'Plan folder "' + folder + '" could not be listed: ' + str(exc)})
            continue
        if len(files) > MAX_FILES_PER_FOLDER:
            listing['truncated'].append('Only the first ' + str(MAX_FILES_PER_FOLDER) + ' of ' + str(len(files)) +
                                        ' files in "' + folder + '" are listed.')
            files = files[:MAX_FILES_PER_FOLDER]
        for name in files:
            if PLAN_FILE_RE.fullmatch(name):
                listing['plans'].append(_plan_entry(root, folder, name, state, budget))
            else:
                listing['decisions'].append(_decisions_entry(root, folder, name, state, budget))
    return listing


def _requested_file(plans_root, payload, pattern, kind):
    """The {id, sha256, confirm} request, the id checked by strict name before
    any filesystem call: '<plan folder>/<file name>' and nothing else."""
    if type(payload) is not dict or set(payload) != {'id', 'sha256', 'confirm'}:
        raise CrewError('invalid', 'Send exactly: id, sha256, confirm.')
    file_id, digest, confirm = payload['id'], payload['sha256'], payload['confirm']
    if type(file_id) is not str or not 0 < len(file_id) <= MAX_FILE_ID:
        raise CrewError('invalid', 'Unknown ' + kind + ' file id.')
    if type(digest) is not str or not SHA256_RE.fullmatch(digest):
        raise CrewError('invalid', 'sha256 must be the 64-character hash shown with the file.')
    if type(confirm) is not str or len(confirm) > MAX_CONFIRM:
        raise CrewError('invalid', 'The typed confirmation is missing or too long.')
    parts = file_id.split('/')
    if len(parts) != 2 or not PLAN_ID_RE.fullmatch(parts[0]) or not pattern.fullmatch(parts[1]):
        raise CrewError('invalid', 'Unknown ' + kind + ' file id; use the id exactly as the list shows it.')
    if plans_root is None:
        raise CrewError('config', 'No plans folder is configured for this room.')
    root = _checked_root(plans_root)
    if root is None:
        raise CrewError('invalid', 'The plans folder does not exist yet.')
    mark = folder_mark(_checked_folder(root, parts[0]))       # checked again before the receipt write
    raw = _read_checked(root, parts[0], parts[1])
    if hashlib.sha256(raw).hexdigest() != digest:
        raise CrewError('state', 'This file changed since you viewed it (its SHA-256 no longer matches). '
                                 'Refresh the list and review it again.')
    return root, parts[0], parts[1], raw, confirm, mark


def _write_board_receipt(plans_root, folder, name, receipt, mark):
    """L1: immediately before the write, the plans folder and the plan folder pass the same checks
    as on the read (no link, still resolving inside the plans folder) and the plan folder is the
    same folder (identity) as when its file was read; otherwise nothing is written."""
    receipt_name = receipt_path_for(Path(name)).name
    try:
        root = _checked_root(plans_root)
        if root is None:
            raise CrewError('state', 'The plans folder is gone.')
        path = _checked_folder(root, folder) / receipt_name
    except CrewError as exc:
        raise CrewError('state', RECEIPT_REFUSED.format(name=folder + '/' + receipt_name)) from exc
    try:
        write_receipt(path, receipt, mark)
    except CrewError as exc:
        raise CrewError(exc.code, RECEIPT_REFUSED.format(name=folder + '/' + receipt_name)) from exc
    return folder + '/' + receipt_name


def board_approve(plans_root, payload, client):
    """POST /api/plan-approve {id, sha256, confirm}: the bytes Shane saw (by
    hash), confirmed by typing that plan's id, through the same approval as the
    CLI, then the same receipt beside the file."""
    if plans_root is None:
        raise CrewError('config', 'No plans folder is configured for this room.')
    _, folder, name, raw, confirm, mark = _requested_file(plans_root, payload, PLAN_FILE_RE, 'plan')
    plan = parse_plan(raw)
    if confirm != plan['plan_id']:
        raise CrewError('invalid', 'Type the plan id exactly ("' + plan['plan_id'] + '") to approve this plan.')
    state = client.state()
    if state['actor']['role'] != 'owner':
        raise CrewError('forbidden', 'Only the room owner may approve a plan.')
    refuse_unapprovable(plan, state)
    refuse_plan_keys(client, plan)                   # fix2-board N3: the whole plan, before anything is sent
    tasks_receipt, lines = approve_plan(client, plan, state)
    receipt_id = _write_board_receipt(plans_root, folder, name, plan_receipt(plan, state, tasks_receipt), mark)
    lines.append('receipt written to ' + receipt_id)
    return {'plan_id': plan['plan_id'], 'scope': plan['scope'], 'tasks': tasks_receipt, 'reserve': reserve_keys(plan),
            'receipt': receipt_id, 'lines': lines}


def board_apply_decisions(plans_root, payload, client):
    """POST /api/decisions-apply {id, sha256, confirm="apply"}: the same batch
    as `crew decide-file`, then DECISIONS-<n>.receipt.json beside the file."""
    if plans_root is None:
        raise CrewError('config', 'No plans folder is configured for this room.')
    root, folder, name, raw, confirm, mark = _requested_file(plans_root, payload, DECISIONS_FILE_RE, 'decisions')
    entries = parse_decisions(raw)
    if confirm != DECISIONS_CONFIRM:
        raise CrewError('invalid', 'Type apply exactly to apply this decisions file.')
    state = client.state()
    if state['actor']['role'] != 'owner':
        raise CrewError('forbidden', 'Only the room owner may apply a decisions file.')
    results = apply_decisions(client, prepare_decisions(entries, state, read_receipt(root / folder / name)))
    receipt_id = _write_board_receipt(plans_root, folder, name, decisions_receipt(state, results), mark)
    return {'ok': all(r['ok'] for r in results), 'results': results, 'receipt': receipt_id,
            'lines': decision_lines(results) + ['receipt written to ' + receipt_id]}
