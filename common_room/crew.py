"""`crew`: a friendly command-line tool for any shell-capable seat on the Common Room
(Claude Code, Hermes Agent, Codex, Shane himself).

Usage:
    python -B -m common_room.crew [--credential PATH | --as ACTOR] [--room-root DIR]
                                   [--json] <subcommand> ...

This module never talks to `room.sqlite`, never reads another seat's credential and
never invents authority: every write goes through CrewClient, which itself goes
through the room's own /api/command or /api/handoff under a caller-kept request ID.
The room is always the one authority; this file only shapes friendlier input and
output around it, and checks the room's own published text limits locally so a typo
gets a clear local refusal instead of a silent cap.
"""
from pathlib import Path
import argparse, datetime, fnmatch, hashlib, importlib.util, json, os, re, subprocess, sys, time, uuid

from . import keeper_vault
from .crew_client import CrewClient, CrewError, ROOT, new_request_id
from .protocol import canonical

DEFAULT_ROOM_ROOT = ROOT / '.runtime' / 'handoffs'
ACTOR_RE = re.compile(r'[a-z][a-z0-9_-]{0,63}')
SHORT_ID_LEN = 10

# The room's own published text limits (protocol.py / store.py), checked here first
# so a violation is refused locally with a clear message instead of silently capping
# or bouncing off the room with no context.
LIMIT_TITLE = 120
LIMIT_BODY = 4000
LIMIT_ACCEPTANCE = 2000
LIMIT_NOTE = 4000
LIMIT_REVIEW_NOTE = 2000
LIMIT_QUESTION = 2000
LIMIT_REPORT_BYTES = 12000
LIMIT_CANCEL_REASON = 2000

# Plan and decisions files (`crew-plan/1`, `crew-decisions/1`): the one implementation
# the CLI and the owner's board share lives in plans.py (2026-09-24, autonomy build B).
# Re-exported here so every existing `crew.<name>` keeps working.
from .plans import (                                             # noqa: E402,F401
    PLAN_PROTOCOL, MAX_PLAN_BYTES, MAX_PLAN_TASKS, PLAN_ID_RE, TASK_KEY_RE, SCOPE_RE,
    PLAN_FIELDS, PLAN_TASK_FIELDS, DECISIONS_PROTOCOL, MAX_DECISIONS, DECISION_FIELDS,
    DECISION_COMMAND, DECISION_PREFIX,
    check_text, resolve_task_id, read_local_bytes, decode_strict_json, write_receipt, receipt_path_for,
    validate_plan, parse_plan, load_plan_file, check_plan_against_state, check_previous_approval_conflicts,
    refuse_unapprovable, refuse_plan_keys, plan_summary, approve_plan, plan_receipt,
    PLAN_TASK_OPTIONAL, MAX_RESERVE_TASKS, RESERVE_LINE, PLAN_ENVELOPE_LINE, reserve_keys, envelope_lines,
    validate_decisions, parse_decisions, load_decisions_file, prepare_decisions, decisions_summary,
    apply_decisions, decision_lines, decisions_receipt, folder_mark, read_receipt,
)


# ---------------------------------------------------------------------------
# Small pure helpers, kept import-level so tests can exercise them directly.
# ---------------------------------------------------------------------------

def short_id(task_id):
    return task_id[:SHORT_ID_LEN]


def resolve_as_actor(actor, room_root):
    """--as is a speed bump against a seat casually acting as another seat, not a
    security boundary: every credential file lives in the same shared folder, so
    anything that can read one can read them all. It refuses unless the launcher
    that started this seat already said, via COMMON_ROOM_SEAT, that this IS that
    seat, and --as keeper additionally refuses without an interactive terminal,
    since only Shane at a keyboard should casually reach for the owner seat."""
    if type(actor) is not str or not ACTOR_RE.fullmatch(actor):
        raise CrewError('config', 'Actor name must be a bounded lowercase identifier.')
    seat = os.environ.get('COMMON_ROOM_SEAT')
    if seat != actor:
        raise CrewError('config', '--as ' + actor + ' refused: the COMMON_ROOM_SEAT environment variable '
                         'does not already name this seat (this is a mistake guard, not a security boundary; '
                         'set COMMON_ROOM_SEAT=' + actor + ' if this really is your seat, or use --credential '
                         'with an explicit path).')
    if actor == 'keeper' and not sys.stdin.isatty():
        raise CrewError('config', '--as keeper refused: it needs an interactive terminal (Shane at the '
                         'keyboard). From a script or another seat, use --credential with an explicit path.')
    room_root = Path(room_root).resolve()
    candidate = (room_root / (actor + '-credential.json')).resolve()
    if candidate.parent != room_root:
        raise CrewError('config', 'Actor credential must resolve directly inside the room root.')
    return candidate


def lease_remaining(task, server_time):
    lease = task.get('lease')
    if not lease:
        return None
    return lease['until'] - server_time


def read_local_text(path, limit_bytes=None, name='File'):
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise CrewError('config', name + ' could not be read: ' + str(exc)) from exc
    if limit_bytes is not None and len(data) > limit_bytes:
        raise CrewError('invalid', name + ' exceeds ' + str(limit_bytes) + ' bytes.')
    try:
        return data.decode('utf-8', errors='strict')
    except UnicodeDecodeError as exc:
        raise CrewError('invalid', name + ' is not valid UTF-8.') from exc


def confirm_interactive(lines, noun='request'):
    """The one interactive confirm gate shared by plan-approve and
    decide-file: an owner action with no bypass flag. Checked before printing
    anything, so a non-interactive caller (a script, a pipe) is refused fast
    rather than being shown the whole summary first. The summary and prompt
    go to stderr, never stdout: a caller running with --json expects only the
    final result object on stdout, and this confirmation is not it."""
    if not sys.stdin.isatty():
        raise CrewError('config', 'This ' + noun + ' needs an interactive terminal so you can type yes to '
                         'confirm it; there is no flag that skips this.')
    for line in lines:
        print(line, file=sys.stderr)
    print("Type 'yes' to proceed, anything else to cancel:", file=sys.stderr)
    answer = sys.stdin.readline()
    if type(answer) is not str or answer.strip() != 'yes':
        raise CrewError('cancelled', 'Not confirmed; nothing in this ' + noun + ' was sent.')


def own_active_lease(task, actor_id, server_time):
    lease = task.get('lease')
    if task.get('status') == 'claimed' and lease and lease['holder'] == actor_id and lease['until'] > server_time:
        return lease
    return None


def emit_request_id(request_id):
    """Print the id BEFORE sending, for every write: if the process dies right after
    the room answers but before we see the response, the id printed here is exactly
    what a caller needs to retry deliberately (see new_request_id's docstring)."""
    print('request-id: ' + request_id, file=sys.stderr)


def current_task(client, task_id):
    """Re-read the task from a fresh state() after a write. A command's own return
    value can be answered from the room's stored receipt (an old event replayed
    verbatim for idempotency), which is only ever safe to treat as *today's* status
    if we independently confirm it — a fresh read is that confirmation."""
    return CrewClient.task_in(client.state(), task_id)


def add_mark_options(parser):
    """A2 D10: the undoable mark a task gets at creation. Only the owner at intake, or a marker the review stand-ins
    names when asking for a helper, may set it; the room refuses anyone else. No later command changes it."""
    mark = parser.add_mutually_exclusive_group()
    mark.add_argument('--undoable', dest='undoable', action='store_const', const=True,
                      help='Mark the new task undoable: a review stand-in the room was given may then review it.')
    mark.add_argument('--cant-undo', dest='undoable', action='store_const', const=False,
                      help='Mark the new task can\'t-undo: only a reviewer seat may review it (as for an unmarked task).')


def mark_part(args):
    """'' when no mark was given, so unmarked default ids stay exactly what they were before D10."""
    value = getattr(args, 'undoable', None)
    return '' if value is None else ('undoable' if value else 'cant-undo')


def add_tier_options(parser):
    """A4: the model tier and effort the new task is meant for. Shown to every seat, never enforced."""
    from .store import EFFORTS, TIERS
    parser.add_argument('--tier', choices=TIERS, help='The model tier the task is meant for.')
    parser.add_argument('--effort', choices=EFFORTS, help='The effort to run it at.')


def tier_payload(args):
    """{} when neither --tier nor --effort was given, so untagged requests and their default ids stay as before."""
    return {k: getattr(args, k) for k in ('tier', 'effort') if getattr(args, k, None)}


def tier_text(task):
    """'' for an untagged task; otherwise the tier, effort and any mismatch with the seat's own label."""
    if not task.get('tier') and not task.get('effort'):
        return ''
    text = 'tier=' + (task.get('tier') or '-') + '/' + (task.get('effort') or '-')
    if task.get('tier_mismatch'):
        text += ' (seat label ' + task['tier_mismatch'] + ')'
    return text


def content_digest(*parts):
    # Joined with a control character the room's text fields can never contain
    # (they're checked for control characters elsewhere in this file and in
    # store.py's text()), so two different splits of the same parts can't collide.
    return hashlib.sha256('\x1f'.join(parts).encode('utf-8')).hexdigest()


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

WATCH_POLL_SECONDS = 20          # `crew watch`: how often it asks the room
WATCH_DEFAULT_WAIT = 540
WATCH_MAX_WAIT = 590             # inside a 10-minute tool call, with room to spare
WATCH_DOWN_AFTER = 180           # unbroken seconds of failures before ROOM DOWN
WATCH_BACKOFF = (2, 4, 8, 16)    # then WATCH_POLL_SECONDS
WATCH_STOP_CHARS = 200
WATCH_LINE_MAX = 280             # the whole line stays under 300 characters
WATCH_RETRY_CODES = {'unavailable', 'unauthenticated', 'config', 'error'}
STOP_NOTE = re.compile(r'\s*STOP\b')
QUESTION_LOOKALIKE = re.compile(r'\s*QUESTION\s+FOR\s+(?!SHANE\b)\S', re.I)   # A1: a near miss of QUESTION FOR <seat>:
QUESTION_MARK_NAMES = {'read': 'read', 'accept': 'accepted', 'check': 'checked'}
WATCH_STOP_SENDERS = ('keeper', 'claude-room')   # RUN_RULES: only Shane (keeper) or the orchestrator may stop the room by note


def _max_wait(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError('--max-wait is a whole number of seconds.')
    if not 1 <= value <= WATCH_MAX_WAIT:
        raise argparse.ArgumentTypeError('--max-wait is 1 to ' + str(WATCH_MAX_WAIT) + ' seconds.')
    return value

def build_parser():
    p = argparse.ArgumentParser(prog='python -B -m common_room.crew', description=__doc__)
    identity = p.add_mutually_exclusive_group(required=True)
    identity.add_argument('--credential', type=Path, help='Path to an <actor>-credential.json file.')
    identity.add_argument('--as', dest='as_actor',
                           help='Resolve to <room-root>/<ACTOR>-credential.json. A guard against acting as '
                                'the wrong seat by mistake, NOT a security boundary (every credential file '
                                'lives in one shared folder): refused unless the COMMON_ROOM_SEAT environment '
                                'variable already names this seat, and --as keeper additionally needs an '
                                'interactive terminal.')
    p.add_argument('--room-root', type=Path, default=DEFAULT_ROOM_ROOT,
                    help='Directory --as resolves credentials in (default: package .runtime/handoffs).')
    p.add_argument('--json', action='store_true', help='Print one canonical JSON object instead of text.')

    sub = p.add_subparsers(dest='cmd', required=True)

    sub.add_parser('whoami')

    t = sub.add_parser('tasks')
    t.add_argument('--all', action='store_true')
    t.add_argument('--status')

    sub.add_parser('review-queue')

    s = sub.add_parser('show')
    s.add_argument('task')

    r = sub.add_parser('read')
    r.add_argument('task')
    r.add_argument('--ref', type=int, default=1, help='1-based index into the task\'s result references.')

    a = sub.add_parser('ack')
    a.add_argument('task')
    a.add_argument('--request-id')

    c = sub.add_parser('claim')
    c.add_argument('task')
    c.add_argument('--seconds', type=int, default=1800)
    c.add_argument('--request-id')

    rn = sub.add_parser('renew')
    rn.add_argument('task')
    rn.add_argument('--seconds', type=int, default=1800)
    rn.add_argument('--request-id')

    sm = sub.add_parser('submit')
    sm.add_argument('task')
    sm.add_argument('--file', required=True)
    sm.add_argument('--summary', required=True)
    sm.add_argument('--request-id')
    sm.add_argument('--repo', help='Gate: the git repo or worktree the work lives in (read only).')
    sm.add_argument('--base', help='Gate: the commit the work started from.')
    sm.add_argument('--commit', help='Gate: the commit being submitted.')
    sm.add_argument('--evidence', help='Gate: an EVIDENCE.json (or its folder) from tools/evidence.py, if it is not '
                                       'beside the report.')
    sm.add_argument('--cite-root', help='Gate: the estate root report citations resolve under (default: the folder '
                                        'above Artifacts/ that holds this tree).')
    sm.add_argument('--also-check', action='append', default=[], metavar='FILE',
                    help='Gate: also check citations in this deliverable (repeatable).')
    sm.add_argument('--skip-gate', metavar='REASON',
                    help='Send without the gate checks. Needs a written reason; it is added to the result '
                         'for reviewers to see.')

    f = sub.add_parser('fail')
    f.add_argument('task')
    f.add_argument('--reason', required=True)
    f.add_argument('--request-id')

    ak = sub.add_parser('ask')
    ak.add_argument('task')
    ak.add_argument('--question', required=True)
    ak.add_argument('--request-id')

    rv = sub.add_parser('review')
    rv.add_argument('task')
    decision = rv.add_mutually_exclusive_group(required=True)
    decision.add_argument('--accept', action='store_true')
    decision.add_argument('--reject', action='store_true')
    rv.add_argument('--note', required=True)
    rv.add_argument('--paperwork', action='store_true',
                    help='Mark a rejection as paperwork (the work is right, the report or evidence is not): it costs no strike '
                         'until the third on one task. The default stays a substance rejection.')
    rv.add_argument('--request-id')

    n = sub.add_parser('note')
    n.add_argument('scope')
    n.add_argument('text')
    n.add_argument('--request-id')

    ns = sub.add_parser('notes')
    ns.add_argument('--scope')
    ns.add_argument('--last', type=int)

    d = sub.add_parser('delegate')
    d.add_argument('task')
    d.add_argument('--to', required=True)
    d.add_argument('--title', required=True)
    body = d.add_mutually_exclusive_group(required=True)
    body.add_argument('--body')
    body.add_argument('--body-file')
    d.add_argument('--acceptance', required=True)
    d.add_argument('--request-id')
    add_mark_options(d)
    add_tier_options(d)

    ik = sub.add_parser('intake')
    ik.add_argument('--scope', required=True)
    ik.add_argument('--to', required=True)
    ik.add_argument('--title', required=True)
    ibody = ik.add_mutually_exclusive_group(required=True)
    ibody.add_argument('--body')
    ibody.add_argument('--body-file')
    ik.add_argument('--acceptance', required=True)
    ik.add_argument('--ref', dest='external_ref')
    ik.add_argument('--request-id')
    add_mark_options(ik)
    add_tier_options(ik)

    dc = sub.add_parser('decide')
    dc.add_argument('task')
    dc.add_argument('--response', required=True,
                    help='The answer; with --refund it is also the written reason for the refund.')
    dc.add_argument('--refund', action='store_true',
                    help="Give one strike back (the strike came from a brief error, not the builder). Only the task's owner or "
                         'the sender of a helper may; at two strikes only the owner. The room refuses what it refuses.')
    dc.add_argument('--request-id')

    ri = sub.add_parser('reinstate')
    ri.add_argument('task')
    ri.add_argument('--reason', required=True)
    ri.add_argument('--request-id')

    cn = sub.add_parser('cancel')
    cn.add_argument('task')
    cn.add_argument('--reason', required=True)
    cn.add_argument('--request-id')

    pc = sub.add_parser('plan-check')
    pc.add_argument('file')

    pa = sub.add_parser('plan-approve')
    pa.add_argument('file')

    df = sub.add_parser('decide-file')
    df.add_argument('file')

    qs = sub.add_parser('questions')
    qs.add_argument('--since', type=int)

    qm = sub.add_parser('question', help='Mark a question addressed with QUESTION FOR <seat>: (A1). read and accept are '
                                         'the addressed seat\'s marks (crew watch marks read itself), check is the '
                                         'asker\'s, in that order. A mark posts no note and wakes no one: answer in a '
                                         'note that cites #<seq>.')
    qm.add_argument('mark', choices=('read', 'accept', 'check'))
    qm.add_argument('seq', type=int, help='The question note\'s number, as in #<seq>.')
    qm.add_argument('--request-id')

    cd = sub.add_parser('card')
    cd.add_argument('seat')

    sa = sub.add_parser('stop-all', help='Keeper only (crew-shane stop-all): pause every authority, write the STOP file and post '
                                         'a STOP: note, so every waiting seat stops.')
    sa.add_argument('--reason', required=True, help='Why; it goes in the STOP: note every seat sees.')
    sa.add_argument('--request-id')
    ra = sub.add_parser('resume-all', help='Keeper only (crew-shane resume-all): undo stop-all and post a RESUME: note.')
    ra.add_argument('--reason')
    ra.add_argument('--request-id')

    us = sub.add_parser('usage', help="Read-only: what the workers' usage logs (usage/<seat>.jsonl beside the credential files) "
                                      'add up to, per seat. Bad lines are counted, never a crash.')
    us.add_argument('--seat', type=_usage_seat, help='Only this seat.')
    us.add_argument('--days', type=_usage_days, help='Only the last N days (default: everything).')

    sub.add_parser('login', help='Owner only (crew-shane login): put the owner key on the Windows clipboard for '
                                 'the board, wait for Enter, then clear the clipboard. Never prints the key.')

    br = sub.add_parser('branch', help='Give a task its own clean copy of the code: a worktree <root>/<task-key> on a new '
                                       'branch up/<task-key> made from --base. Needs no room.')
    br.add_argument('key', metavar='task-key', help='The task key, e.g. crew-watch (lowercase letters, digits, hyphens).')
    br.add_argument('--repo', required=True, help='The git repo or worktree to branch from (its own worktree is not touched).')
    br.add_argument('--base', required=True, help='The commit, branch or tag the new branch starts from.')
    br.add_argument('--root', help='The folder the new worktree goes in (default: a "wt" folder beside the repo).')

    w = sub.add_parser('watch', help='Wait for work with no model calls: exit 0 when there is something to do '
                                     '(TASK, ANSWER, DECIDE, REVIEWED, or REVIEW with --reviews; QUESTION when a note '
                                     'addressed QUESTION FOR <this seat>: is waiting, which marks it read), 3 WAIT at '
                                     '--max-wait, 4 STOP, 5 ROOM DOWN. One short line, run it again after WAIT.')
    w.add_argument('--reviews', action='store_true', help='Also wake for a task waiting for this seat\'s review.')
    w.add_argument('--max-wait', type=_max_wait, default=WATCH_DEFAULT_WAIT, metavar='SECONDS',
                   help='Give up with WAIT after this long (default %d, at most %d, so it fits inside a '
                        '10-minute tool call).' % (WATCH_DEFAULT_WAIT, WATCH_MAX_WAIT))

    sub.add_parser('needs', help="Read-only: everything waiting on Shane that this seat can see (the board's Needs Shane "
                                 'list), each with the exact line or click. Exit 0 when nothing needs him, 2 when '
                                 'something does (2 is also what a mistyped command exits with), 1 when the room refused '
                                 'or could not be reached. --json prints the items.')

    return p


def body_text(args, limit, name='Body'):
    if getattr(args, 'body_file', None):
        return check_text(read_local_text(args.body_file, name=name + ' file'), limit, name)
    return check_text(args.body, limit, name)


def check_request_id(request_id):
    if request_id is None:
        return None
    if type(request_id) is not str or '/' in request_id:
        raise CrewError('invalid', 'Request ids must be bounded identifiers and never contain a slash.')
    return request_id


# ---------------------------------------------------------------------------
# Subcommand handlers. Each returns (result_dict, [human-readable lines]).
# ---------------------------------------------------------------------------

def cmd_whoami(client, state, args):
    scopes = [{'id': a['id'], 'scope': a['scope']} for a in state.get('authorities', [])]
    result = {'actor': state['actor']['id'], 'role': state['actor']['role'],
              'scopes': scopes, 'server_time': state['server_time']}
    lines = ['actor: ' + result['actor'] + ' (' + result['role'] + ')',
             'server_time: ' + str(result['server_time']), 'scopes:']
    lines += ['  ' + s['id'] + ': ' + s['scope'] for s in scopes]
    return result, lines


def _task_row(task, server_time):
    remaining = lease_remaining(task, server_time)
    return {
        'id': task['id'], 'short_id': short_id(task['id']), 'status': task['status'],
        'recipient': task['recipient'], 'title': task['title'],
        'lease_remaining': remaining, 'failed_attempts': task['failed_attempts'],
        'needs_shane': task['status'] == 'waiting_decision',
        **{k: task[k] for k in ('tier', 'effort', 'tier_mismatch') if task.get(k)},   # A4: only on tagged tasks
    }


def cmd_tasks(client, state, args):
    tasks = state.get('tasks', [])
    if not args.all:
        tasks = [t for t in tasks if t['recipient'] == client.actor_id]
        if args.status is None:
            tasks = [t for t in tasks if t['status'] not in ('done', 'cancelled')]
    if args.status is not None:
        tasks = [t for t in tasks if t['status'] == args.status]
    rows = [_task_row(t, state['server_time']) for t in tasks]
    result = {'tasks': rows}
    lines = [_task_line(r) for r in rows] or ['(no tasks)']
    return result, lines


def _task_line(row):
    remaining = row['lease_remaining']
    lease_text = 'no lease' if remaining is None else ('expired' if remaining <= 0 else str(int(remaining)) + 's left')
    flag = ' NEEDS SHANE' if row['needs_shane'] else ''
    tier = ' ' + tier_text(row) if tier_text(row) else ''   # A4: nothing on an untagged task
    return '{}  {:<16}  {:<16}  {}  [{}] failed={}{}{}'.format(
        row['short_id'], row['status'], row['recipient'], row['title'], lease_text, row['failed_attempts'], flag, tier)


def cmd_review_queue(client, state, args):
    tasks = client.review_queue(state)
    rows = [_task_row(t, state['server_time']) for t in tasks]
    result = {'tasks': rows}
    lines = [_task_line(r) for r in rows] or ['(nothing waiting for your review)']
    return result, lines


def cmd_show(client, state, args):
    tid = resolve_task_id(state, args.task)
    task = CrewClient.task_in(state, tid)
    task = dict(task, needs_shane=task['status'] == 'waiting_decision')
    history = [{'seq': h['seq'], 'actor': h['actor'], 'kind': h['kind'], 'detail': h['detail']}
               for h in state.get('history', []) if h['task_id'] == tid]
    result = {'task': task, 'history': history}
    lines = ['task ' + tid, 'status: ' + task['status'], 'recipient: ' + task['recipient'],
             'title: ' + task['title'], 'failed_attempts: ' + str(task['failed_attempts'])]
    if tier_text(task):
        lines.append(tier_text(task))   # A4: tier, effort and a mismatch with the seat's label, when tagged
    lines.append('history:')
    lines += ['  #{} {} {} {}'.format(h['seq'], h['actor'], h['kind'], h['detail']) for h in history]
    return result, lines


def cmd_read(client, state, args):
    tid = resolve_task_id(state, args.task)
    task = CrewClient.task_in(state, tid)
    refs = task.get('result_refs') or []
    if not 1 <= args.ref <= len(refs):
        raise CrewError('invalid', 'Task has ' + str(len(refs)) + ' result reference(s); --ref must be 1..' + str(len(refs)) + '.')
    ref = refs[args.ref - 1]
    viewed = client.handoff('view_text', {'task_id': tid, 'path': ref['path'], 'sha256': ref['sha256']})
    result = {'task_id': tid, 'path': viewed['path'], 'sha256': viewed['sha256'], 'text': viewed['text']}
    return result, [viewed['text']]


def cmd_ack(client, state, args):
    tid = resolve_task_id(state, args.task)
    task = CrewClient.task_in(state, tid)
    request_id = check_request_id(args.request_id) or ('ack-' + tid + '-f' + str(task['fence']))
    emit_request_id(request_id)
    client.command('ack', {'task_id': tid}, request_id)
    status = current_task(client, tid)['status']
    result = {'task_id': tid, 'status': status, 'request_id': request_id}
    return result, ['task ' + tid + ' acknowledged (status: ' + status + ')']


def cmd_claim(client, state, args):
    tid = resolve_task_id(state, args.task)
    task = CrewClient.task_in(state, tid)
    now = state['server_time']
    warnings = _claim_output_warnings(task, state)
    held = own_active_lease(task, client.actor_id, now)
    if held is not None and args.request_id is None:
        result = {'task_id': tid, 'lease': held, 'already_held': True, 'output_warnings': warnings}
        return result, ['task ' + tid + ' already claimed by you; ' + str(int(held['until'] - now)) + 's left on the lease'] + warnings
    request_id = check_request_id(args.request_id) or ('claim-' + tid + '-f' + str(task['fence']))
    emit_request_id(request_id)
    client.command('claim', {'task_id': tid, 'seconds': args.seconds}, request_id)
    lease = current_task(client, tid)['lease']
    result = {'task_id': tid, 'lease': lease, 'request_id': request_id, 'already_held': False, 'output_warnings': warnings}
    return result, ['task ' + tid + ' claimed for ' + str(args.seconds) + 's (fence ' + str(lease['fence']) + ')'] + warnings


def cmd_renew(client, state, args):
    tid = resolve_task_id(state, args.task)
    task = CrewClient.task_in(state, tid)
    now = state['server_time']
    held = own_active_lease(task, client.actor_id, now)
    if held is None:
        raise CrewError('lease', 'No active lease held by you on this task; claim it first.')
    request_id = check_request_id(args.request_id) or new_request_id('renew')
    emit_request_id(request_id)
    client.command('renew', {'task_id': tid, 'fence': held['fence'], 'seconds': args.seconds}, request_id)
    lease = current_task(client, tid)['lease']
    result = {'task_id': tid, 'lease': lease, 'request_id': request_id}
    return result, ['task ' + tid + ' lease renewed for ' + str(args.seconds) + 's']


# ---------------------------------------------------------------------------
# The submit gate (2026-10-01, upgrade ws-crew step 3). Before `crew submit` sends anything it checks the
# paperwork a reviewer would otherwise reject: the report's citations, the task's FILES: lines against a real
# `git diff --name-only base..commit`, and the RED/GREEN evidence file. A failed gate sends nothing (no stage,
# no finish), so it costs no strike; it prints exactly what to fix. Everything it reads stays read only.
# ---------------------------------------------------------------------------

GATE_FILES_LINE = re.compile(r'\s*FILES:\s*(\S.*?)\s*')
GATE_RED_GREEN = re.compile(r'RED\s*/\s*GREEN|RED\s+then\s+GREEN', re.IGNORECASE)
GATE_MIN_REASON = 10            # characters a --skip-gate reason must have
GATE_LIST_MAX = 10              # rows of one kind listed before "and N more"


def find_estate_root(start):
    """The folder above `start` that holds an Artifacts/ folder (D:\\Work Area on Shane's PC), or None."""
    start = Path(start)
    for folder in [start] + list(start.parents):
        if (folder / 'Artifacts').is_dir():
            return folder
    return None


def gate_file_rules(body):
    """(patterns, none) from every `FILES:` line of a task body, or None when it has none. Notes in
    parentheses are dropped, entries are comma separated, `none` means no repo change is allowed."""
    found, patterns, none = False, [], False
    for line in body.splitlines():
        match = GATE_FILES_LINE.fullmatch(line.rstrip('\r'))
        if not match:
            continue
        found = True
        for item in re.sub(r'\([^)]*\)', ' ', match.group(1)).split(','):
            item = item.strip().strip('`').strip().rstrip('.').strip().replace('\\', '/')
            while item.startswith('./'):
                item = item[2:]
            if not item:
                continue
            if item.lower() == 'none':
                none = True
            else:
                patterns.append(item)
    return (patterns, none) if found else None


def _gate_file_allowed(path, patterns):
    return any(path == p or (any(c in p for c in '*?[') and fnmatch.fnmatchcase(path, p)) for p in patterns)


def _gate_git(repo, *argv):
    """(stdout bytes, error text): one read-only git command, never a write."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0')
    try:
        done = subprocess.run(['git', '--no-optional-locks', '-C', str(repo)] + list(argv),
                              capture_output=True, env=env, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, 'git could not be run: ' + str(exc)
    if done.returncode != 0:
        return None, done.stderr.decode('utf-8', errors='replace').strip() or 'git failed'
    return done.stdout, ''


def _gate_resolve(repo, ref, flag, problems):
    out, error = _gate_git(repo, 'rev-parse', '--verify', '--quiet', ref + '^{commit}')
    sha = out.decode('utf-8', errors='replace').strip() if out else ''
    if not sha:
        problems.append(flag + ' ' + ref + ' is not a commit in ' + str(repo) + '. Name a commit that exists there '
                        '(`git -C "' + str(repo) + '" log --oneline` lists them).' + (' (' + error + ')' if error else ''))
    return sha or None


def _load_cite_check():
    path = ROOT / 'crew' / 'cite_check.py'
    spec = importlib.util.spec_from_file_location('crew_cite_check_for_gate', path)
    if spec is None or spec.loader is None or not path.is_file():
        raise CrewError('config', 'crew/cite_check.py is missing from this tree, so the gate cannot check citations.')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _gate_citations(args, problems, notes, info):
    totals = {'found': 0, 'checked': 0, 'problems': 0}
    info['citation_totals'] = totals
    info['citations'] = {}
    root = Path(args.cite_root) if args.cite_root else find_estate_root(ROOT)
    if root is not None and not root.is_dir():
        raise CrewError('config', '--cite-root ' + str(root) + ' is not a folder.')
    if root is None:
        info['citations'] = 'skipped: no estate root found'
        notes.append('citation check skipped: no folder holding Artifacts/ was found above this tree (pass --cite-root).')
    else:
        cite_check = _load_cite_check()
        for filename in [args.file] + list(getattr(args, 'also_check', []) or []):
            try:
                payload = cite_check.run(str(root), str(filename), False)
            except (cite_check.UsageError, OSError) as exc:
                problems.append('The citation check could not read ' + str(filename) + ': ' + str(exc))
                totals['problems'] += 1
                continue
            totals['found'] += payload['totals']['found']
            totals['checked'] += sum(r['result'] != cite_check.RESULT_NOT_CHECKED for r in payload['citations'])
            for key, count in payload['totals']['by_result'].items():
                info['citations'][key] = info['citations'].get(key, 0) + count
            bad = [r for r in payload['citations'] if r['result'] not in (cite_check.RESULT_OK, cite_check.RESULT_NOT_CHECKED)]
            totals['problems'] += len(bad)
            for row in bad[:GATE_LIST_MAX]:
                problems.append('Citation `' + row['citation'] + '` (' + str(filename) + ' line ' + str(row['line']) +
                                ') is ' + row['result'] + (': ' + row['detail'] if row.get('detail') else '') +
                                '. Correct the path or line range, or remove the citation.')
            if len(bad) > GATE_LIST_MAX:
                problems.append('...and ' + str(len(bad) - GATE_LIST_MAX) + ' more citation(s) failed in ' + str(filename) +
                                '; run crew/cite_check.py on that file to see them all.')
            unchecked = [r for r in payload['citations'] if r['result'] == cite_check.RESULT_NOT_CHECKED]
            for row in unchecked[:GATE_LIST_MAX]:
                notes.append('NOT CHECKED (allowed, a reviewer checks it by hand): `' + row['citation'] +
                             '` (' + str(filename) + ' line ' + str(row['line']) + ')')
            if len(unchecked) > GATE_LIST_MAX:
                notes.append('...and ' + str(len(unchecked) - GATE_LIST_MAX) + ' more NOT CHECKED citation(s).')
            if payload['totals'].get('capped'):
                notes.append('warning: citation check capped in ' + str(filename) + '; remaining citations need review.')
            if payload['no_evidence_labels']:
                notes.append('warning: no evidence labels (MEASURED, VERIFIED, CLAIMED, INFERENCE, MISSING, UNVERIFIABLE) found in ' + str(filename) + '.')
    notes.insert(0, 'citations: {found} found, {checked} checked, {problems} problems'.format(**totals))
    if totals['found'] == 0:
        notes.append('warning: no citations found: short-code tags like [OP 12] are not read; use full paths with line numbers, or check them another way.')


def _output_path(value, root):
    """Resolved, case-folded estate path; OUTPUT directories end in / (existing directories also work)."""
    value = value.strip().strip('`').strip().replace('\\', '/')
    if not value or any(c in value for c in '*?[]'):
        raise ValueError('OUTPUT entries must be literal file or folder paths.')
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    key = str(path).replace('\\', '/').casefold().rstrip('/')
    estate = str(root.resolve()).replace('\\', '/').casefold().rstrip('/')
    if key != estate and not key.startswith(estate + '/'):
        raise ValueError('OUTPUT path is outside the estate root.')
    return key, value.endswith('/') or path.is_dir()


def _output_rules(body, root, invalid_entries=None):
    """Strict for our own declaration; neighbors retain valid entries and report invalid ones."""
    entries = []
    found = False
    for line in body.splitlines():
        match = re.fullmatch(r'\s*OUTPUT:\s*(.*?)\s*', line)
        if match:
            found = True
            for value in re.sub(r'\([^)]*\)', ' ', match.group(1)).split(','):
                try:
                    entries.append(_output_path(value, root))
                except (ValueError, OSError):
                    if invalid_entries is None:
                        raise
                    invalid_entries.append(value)
    return entries if found else None


def _output_overlaps(task, state, root, rules, report=None):
    problems, notes = [], []
    own = list(rules)
    if report is not None:
        own.append((report, False))
    for other in state.get('tasks', []):
        if other.get('id') == task.get('id') or other.get('status') in ('done', 'cancelled'):
            continue
        invalid_entries = []
        theirs = _output_rules(other.get('body') or '', root, invalid_entries=invalid_entries) or []
        if invalid_entries:
            notes.append('OUTPUT check: other task ' + str(other.get('id')) + ' has ' + str(len(invalid_entries)) +
                         ' invalid OUTPUT path(s); valid paths were still checked. Ask its sender to correct them.')
        same_file = any(a == b and not ad and not bd for a, ad in own for b, bd in theirs)
        overlap = any((a == b or (ad and b.startswith(a + '/')) or (bd and a.startswith(b + '/')) or
                       (not ad and not bd and a.rsplit('/', 1)[0] == b.rsplit('/', 1)[0]))
                      for a, ad in own for b, bd in theirs)
        if same_file:
            problems.append('OUTPUT same-file overlap with open task ' + str(other.get('id')) + '; choose separate report files.')
        elif overlap:
            notes.append('OUTPUT folder overlap with open task ' + str(other.get('id')) + '; keep file ownership separate.')
    return problems, notes


def _gate_output(task, state, args, problems, notes, info):
    body = task.get('body') or ''
    if not re.search(r'^\s*OUTPUT:', body, re.MULTILINE):
        info['output'] = 'skipped: the task names no OUTPUT: lines'
        notes.append('OUTPUT check skipped: the task names no OUTPUT: lines.')
        return
    root = Path(args.cite_root) if args.cite_root else find_estate_root(ROOT)
    try:
        if root is None:
            raise ValueError('OUTPUT check requires an estate root (pass --cite-root).')
        rules = _output_rules(body, root)
        report, _ = _output_path(str(Path(args.file).resolve()), root)
        own_problems = []
        if not any(report == path or (folder and report.startswith(path + '/')) for path, folder in rules):
            own_problems.append('OUTPUT report is outside the allowed paths: ' + ', '.join(p for p, _ in rules))
        collisions, overlap_notes = _output_overlaps(task, state, root, rules, report)
        problems.extend(own_problems + collisions)
        notes.extend(overlap_notes)
        info['output'] = 'blocked' if own_problems or collisions else 'OK'
    except (ValueError, OSError) as exc:
        info['output'] = 'blocked'
        problems.append(str(exc))


def _claim_output_warnings(task, state):
    root = find_estate_root(ROOT)
    if root is None:
        return ['OUTPUT check skipped at claim: no estate root found.']
    try:
        rules = _output_rules(task.get('body') or '', root)
        if rules is None:
            return ['OUTPUT check skipped: the task names no OUTPUT: lines.']
        problems, notes = _output_overlaps(task, state, root, rules)
        return ['warning: ' + line for line in problems + notes]
    except (ValueError, OSError) as exc:
        return ['warning: ' + str(exc)]


def _gate_files(task_body, args, problems, notes, info):
    """Returns the full sha of --commit when it could be resolved (the evidence check reuses it)."""
    rules = gate_file_rules(task_body)
    named = [(flag, getattr(args, flag[2:])) for flag in ('--repo', '--base', '--commit')]
    if rules is None:
        info['files'] = 'skipped: the task names no FILES: lines'
        notes.append('FILES check skipped: the task names no FILES: lines.')
        if args.repo and args.commit:
            return _gate_resolve(args.repo, args.commit, '--commit', problems)
        return None
    missing = [flag for flag, value in named if not value]
    if missing:
        info['files'] = 'blocked'
        problems.append('The task has FILES: lines, so this submit must also name ' + ', '.join(missing) +
                        ' (the repo or worktree, the commit the work started from, and the commit being submitted).')
        return None
    base = _gate_resolve(args.repo, args.base, '--base', problems)
    commit = _gate_resolve(args.repo, args.commit, '--commit', problems)
    if base is None or commit is None:
        info['files'] = 'blocked'
        return commit
    out, error = _gate_git(args.repo, 'diff', '--name-only', '-z', base + '..' + commit)
    if out is None:
        info['files'] = 'blocked'
        problems.append('`git diff --name-only` failed in ' + str(args.repo) + ': ' + error)
        return commit
    changed = sorted(f.decode('utf-8', errors='replace') for f in out.split(b'\0') if f)
    patterns, none = rules
    outside = [f for f in changed if none or not _gate_file_allowed(f, patterns)]
    if outside:
        info['files'] = 'blocked'
        listed = ', '.join(outside[:GATE_LIST_MAX]) + (' and ' + str(len(outside) - GATE_LIST_MAX) + ' more'
                                                       if len(outside) > GATE_LIST_MAX else '')
        if none:
            problems.append('The task says FILES: none, but ' + args.base + '..' + args.commit + ' changes: ' + listed +
                            '. Take those changes out of the commit, or ask the task\'s sender to correct the FILES: line.')
        else:
            problems.append(args.base + '..' + args.commit + ' changes files the task does not list under FILES: ' + listed +
                            '. Allowed: ' + ', '.join(patterns) + '. Move those changes to another branch, or ask the '
                            'task\'s sender to widen the FILES: line.')
        return commit
    info['files'] = 'OK'
    return commit


def _declared_receipt_bytes(root, ref, limit):
    """Only bounded, hash-bound ordinary files below the evidence directory; never commands."""
    if not isinstance(ref, dict) or not isinstance(ref.get('path'), str):
        raise ValueError('receipt/output path is missing')
    relative = Path(ref['path'])
    if (relative.is_absolute() or relative.drive or not relative.parts or
            any(':' in part or part in ('.', '..') or part.startswith('.') or part.casefold() in ('secrets', '_private')
                for part in relative.parts)):
        raise ValueError('receipt/output path must be a safe relative path')
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file() or path.stat().st_size > limit:
        raise ValueError('receipt/output file is missing, outside the evidence folder, or too large')
    raw = path.read_bytes()
    if len(raw) > limit or not re.fullmatch(r'[0-9a-f]{64}', str(ref.get('sha256', ''))) or hashlib.sha256(raw).hexdigest() != ref['sha256']:
        raise ValueError('receipt/output hash does not match')
    return raw


def _gate_declared_evidence(data, path, task_body, args, commit_sha, problems, notes, info):
    """E1 saved-receipt contract, not a runner or a trusted execution attestation.

    Task: TEST_COMMAND: JSON argv (one line). Manifest tool: declared-command-evidence-v1;
    parent.sha/commit.sha are full revisions, runs.parent/runs.commit are {path, sha256}.
    Each JSON receipt: protocol=declared-command-run-v1, revision, argv, exit_code,
    status=RED|GREEN, total/passed/failed, summary_line (one-based), output={path,sha256}.
    Output is the unchanged captured stream; summary_line selects its final nonblank line.
    Supported final summaries (case insensitive): Results: P passed, F failed, T total;
    or Lune: P passed, F failed (S spec files). For Lune total=P+F; S is not tests.
    An external collector may extract the receipt from saved output; this gate never executes
    argv. Hash/command/revision binding detects inconsistency, not dishonest receipt authors.
    Separate independent review must establish execution provenance and behavioral coverage.
    """
    try:
        if not args.repo or not args.base or not commit_sha:
            raise ValueError('declared evidence requires --repo, --base and a resolved --commit')
        base = _gate_resolve(args.repo, args.base, '--base', problems)
        if not base:
            raise ValueError('base could not be resolved')
        if (data.get('parent', {}).get('sha') != base or data.get('commit', {}).get('sha') != commit_sha or base == commit_sha):
            raise ValueError('parent/commit must match the explicit base/commit and be distinct')
        ancestry, error = _gate_git(args.repo, 'merge-base', '--is-ancestor', base, commit_sha)
        if ancestry is None:
            raise ValueError('parent is not an ancestor of commit')
        commands = re.findall(r'^\s*TEST_COMMAND:\s*(.*?)\s*$', task_body, re.MULTILINE)
        if len(commands) != 1:
            raise ValueError('task needs exactly one TEST_COMMAND JSON argv declaration')
        argv = json.loads(commands[0])
        if (not isinstance(argv, list) or not 1 <= len(argv) <= 64 or
                any(not isinstance(a, str) or not a or len(a) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in a) for a in argv) or
                sum(map(len, argv)) > 8192):
            raise ValueError('TEST_COMMAND must be a bounded nonempty JSON string array without control characters')
        for phase, revision in (('parent', base), ('commit', commit_sha)):
            receipt = json.loads(_declared_receipt_bytes(path.parent, data['runs'][phase], 1 << 20))
            if (receipt.get('protocol') != 'declared-command-run-v1' or receipt.get('revision') != revision or
                    receipt.get('argv') != argv):
                raise ValueError(phase + ' receipt protocol/revision/argv mismatch')
            total, passed, failed, code, line = [receipt.get(k) for k in ('total', 'passed', 'failed', 'exit_code', 'summary_line')]
            if (any(type(n) is not int for n in (total, passed, failed, code, line)) or
                    not 0 < total <= 10000000 or passed < 0 or failed < 0 or total != passed + failed or line < 1):
                raise ValueError(phase + ' receipt has invalid test counts, exit code or summary line')
            output = _declared_receipt_bytes(path.parent, receipt['output'], 4 << 20).decode('utf-8-sig').splitlines()
            if line > len(output) or any(t.strip() for t in output[line:]):
                raise ValueError(phase + ' summary must be the final nonblank output line')
            summary = re.fullmatch(r'\s*Results:\s*(\d+) passed,\s*(\d+) failed,\s*(\d+) total\s*', output[line - 1], re.IGNORECASE)
            measured = tuple(map(int, summary.groups())) if summary else None
            if measured is None:
                lune = re.fullmatch(r'\s*(\d+) passed,\s*(\d+) failed\s*\((\d+) spec files\)\s*', output[line - 1], re.IGNORECASE)
                if lune and int(lune.group(3)) > 0:
                    lp, lf = int(lune.group(1)), int(lune.group(2))
                    measured = (lp, lf, lp + lf)
            if measured != (passed, failed, total):
                raise ValueError(phase + ' saved output summary does not match receipt counts')
            if phase == 'parent' and (code == 0 or failed == 0 or receipt.get('status') != 'RED'):
                raise ValueError('parent receipt is not RED')
            if phase == 'commit' and (code != 0 or failed != 0 or passed != total or receipt.get('status') != 'GREEN'):
                raise ValueError('commit receipt is not GREEN')
    except (ValueError, TypeError, KeyError, AttributeError, OSError):
        # Metadata and output can contain private command arguments: never echo their contents.
        info['evidence'] = 'blocked'
        problems.append('Declared-command evidence is invalid: check explicit revisions/ancestry, TEST_COMMAND argv, '
                        'relative receipt/output paths and hashes, final summary counts, exit codes and RED/GREEN status.')
        return
    info['evidence'] = 'OK'
    notes.append('declared-command receipts match saved revisions, argv, hashes and results; this is internal binding, '
                 'not authenticated execution. Separate independent review of execution provenance is required.')


def _gate_evidence(task_body, args, commit_sha, problems, notes, info):
    if not GATE_RED_GREEN.search(task_body):
        info['evidence'] = 'skipped: the task does not mention RED/GREEN'
        return
    if not args.commit and re.search(r'^\s*TEST_COMMAND:', task_body, re.MULTILINE):
        info['evidence'] = 'blocked'
        problems.append('Declared-command evidence requires --repo, --base and --commit.')
        return
    if not args.commit:
        info['evidence'] = 'skipped: no --commit named'
        notes.append('evidence check skipped: the task mentions RED/GREEN but no --commit was named.')
        return
    folder = Path(args.file).resolve().parent
    if args.evidence:
        given = Path(args.evidence)
        candidates = [given / 'EVIDENCE.json' if given.is_dir() else given]
    else:
        candidates = [folder / 'EVIDENCE.json', folder / 'evidence' / 'EVIDENCE.json']
    path = next((c for c in candidates if c.is_file()), None)
    if path is None:
        info['evidence'] = 'blocked'
        problems.append('The task asks for RED/GREEN evidence but no EVIDENCE.json was found (looked at: ' +
                        ', '.join(str(c) for c in candidates) + '). Run tools/evidence.py for ' + args.commit +
                        ' with --out set to a folder beside the report, or pass --evidence PATH.')
        return
    try:
        data = json.loads(read_local_text(path, limit_bytes=1 << 20, name='Evidence file'))
    except (CrewError, ValueError):
        data = None
    if isinstance(data, dict) and data.get('tool') == 'declared-command-evidence-v1':
        _gate_declared_evidence(data, path, task_body, args, commit_sha, problems, notes, info)
        return
    if isinstance(data, dict) and data.get('tool') != 'evidence.py 1.0':
        info['evidence'] = 'blocked'
        problems.append('Evidence tool format is not recognized; use evidence.py 1.0 or declared-command-evidence-v1.')
        return
    sha = data.get('commit', {}).get('sha') if isinstance(data, dict) and isinstance(data.get('commit'), dict) else None
    if not isinstance(sha, str) or not sha:
        info['evidence'] = 'blocked'
        problems.append(str(path) + ' is not an EVIDENCE.json from tools/evidence.py (it has no commit.sha). Run the tool again.')
        return
    named = args.commit.lower()
    same = sha.lower() == commit_sha.lower() if commit_sha else (len(named) >= 7 and sha.lower().startswith(named))
    if not same:
        info['evidence'] = 'blocked'
        problems.append(str(path) + ' names commit ' + sha[:7] + ' but this submit names ' + args.commit[:12] +
                        '. Run tools/evidence.py for the commit you are submitting.')
        return
    info['evidence'] = 'OK'


def run_submit_gate(args, task_body, task=None, state=None):
    """(problems, notes, info). Every check runs, so one pass lists everything to fix."""
    problems, notes, info = [], [], {}
    _gate_citations(args, problems, notes, info)
    _gate_output(task or {'body': task_body}, state or {}, args, problems, notes, info)
    commit_sha = _gate_files(task_body, args, problems, notes, info)
    _gate_evidence(task_body, args, commit_sha, problems, notes, info)
    info['notes'] = list(notes)
    return problems, notes, info


def cmd_submit(client, state, args):
    tid = resolve_task_id(state, args.task)
    task = CrewClient.task_in(state, tid)
    now = state['server_time']
    summary = check_text(args.summary, LIMIT_BODY, 'Summary')
    held = own_active_lease(task, client.actor_id, now)
    if held is None:
        if task.get('builder') == client.actor_id and task['status'] in ('ready_for_review', 'done'):
            result = {'task_id': tid, 'status': task['status'], 'result_refs': task.get('result_refs'), 'already_done': True}
            return result, ['task ' + tid + ' was already submitted (status: ' + task['status'] + ')']
        raise CrewError('lease', 'No active lease held by you on this task; claim it before submitting.')
    report_text = read_local_text(args.file, limit_bytes=LIMIT_REPORT_BYTES, name='Report')
    check_text(report_text, LIMIT_REPORT_BYTES, 'Report')
    gate_lines, gate_info = [], None
    if getattr(args, 'skip_gate', None) is not None:
        reason = ' '.join(args.skip_gate.split())
        if len(reason) < GATE_MIN_REASON:
            raise CrewError('invalid', '--skip-gate needs a written reason of at least ' + str(GATE_MIN_REASON) +
                            ' characters (it is added to the result so reviewers see it). Nothing was sent.')
        report_text += '\n\n## GATE SKIPPED\nThe submit gate was skipped by ' + client.actor_id + '. Reason: ' + reason + '\n'
        summary = check_text(summary + '\n\nGATE SKIPPED: ' + reason, LIMIT_BODY, 'Summary')
        if len(report_text.encode('utf-8')) > LIMIT_REPORT_BYTES:
            raise CrewError('invalid', 'Report plus the --skip-gate note exceeds ' + str(LIMIT_REPORT_BYTES) + ' bytes. Nothing was sent.')
        check_text(report_text, LIMIT_REPORT_BYTES, 'Report')
        gate_lines = ['gate: SKIPPED (reason added to the result)']
    else:
        problems, notes, gate_info = run_submit_gate(args, task.get('body') or '', task, state)
        if problems:
            raise CrewError('gate', 'Submit blocked by the gate. Nothing was sent and no strike was recorded. Fix, then submit again:\n' +
                            '\n'.join('gate: ' + n for n in notes) + '\n' +
                            '\n'.join('  ' + str(i) + '. ' + p for i, p in enumerate(problems, 1)) +
                            '\n(To send anyway, add --skip-gate "<why>"; the reason is added to the result for reviewers.)')
        gate_lines = ['gate: passed'] + ['gate: ' + n for n in notes]
    ref = client.handoff('stage_text', {'task_id': tid, 'fence': held['fence'], 'text': report_text})
    request_id = check_request_id(args.request_id) or ('finish-' + tid + '-f' + str(held['fence']))
    emit_request_id(request_id)
    client.command('finish', {'task_id': tid, 'fence': held['fence'], 'body': summary, 'refs': [ref]}, request_id)
    status = current_task(client, tid)['status']
    result = {'task_id': tid, 'status': status, 'ref': ref, 'request_id': request_id, 'already_done': False}
    if gate_info is not None:
        result['gate'] = gate_info
    return result, gate_lines + ['task ' + tid + ' submitted for review (' + ref['path'] + ')']


def cmd_fail(client, state, args):
    tid = resolve_task_id(state, args.task)
    task = CrewClient.task_in(state, tid)
    reason = check_text(args.reason, LIMIT_BODY, 'Reason')
    held = own_active_lease(task, client.actor_id, state['server_time'])
    if held is None:
        raise CrewError('lease', 'No active lease held by you on this task; claim it first.')
    request_id = check_request_id(args.request_id) or new_request_id('fail')
    emit_request_id(request_id)
    client.command('fail', {'task_id': tid, 'fence': held['fence'], 'body': reason}, request_id)
    fresh = current_task(client, tid)
    result = {'task_id': tid, 'status': fresh['status'], 'failed_attempts': fresh['failed_attempts'],
              'request_id': request_id}
    return result, ['task ' + tid + ' marked failed (status: ' + result['status'] + ')']


def cmd_ask(client, state, args):
    tid = resolve_task_id(state, args.task)
    task = CrewClient.task_in(state, tid)
    question = check_text(args.question, LIMIT_QUESTION, 'Question')
    held = own_active_lease(task, client.actor_id, state['server_time'])
    if held is None:
        raise CrewError('lease', 'No active lease held by you on this task; claim it first.')
    request_id = check_request_id(args.request_id) or new_request_id('ask')
    emit_request_id(request_id)
    client.command('wait_decision', {'task_id': tid, 'fence': held['fence'], 'question': question}, request_id)
    status = current_task(client, tid)['status']
    result = {'task_id': tid, 'status': status, 'request_id': request_id}
    return result, ['task ' + tid + ' now waiting on Shane\'s decision']


def cmd_review(client, state, args):
    tid = resolve_task_id(state, args.task)
    task = CrewClient.task_in(state, tid)
    note = check_text(args.note, LIMIT_REVIEW_NOTE, 'Review note')
    if getattr(args, 'paperwork', False) and args.accept:
        raise CrewError('invalid', '--paperwork marks a rejection (the work is right, the paperwork is not); use it with --reject. '
                        'Nothing was sent.')
    request_id = check_request_id(args.request_id) or ('review-' + tid + '-f' + str(task['fence']))
    emit_request_id(request_id)
    payload = {'task_id': tid, 'accepted': bool(args.accept), 'note': note}
    if getattr(args, 'paperwork', False):
        payload['kind'] = 'paperwork'                # no kind is sent otherwise, so the default stays a substance review
    client.command('review', payload, request_id)
    fresh = current_task(client, tid)
    status = fresh['status']
    result = {'task_id': tid, 'status': status, 'request_id': request_id}
    lines = ['task ' + tid + ' review recorded (status: ' + status + ')']
    if not args.accept:
        result.update(review_kind=fresh.get('review_kind') or 'substance', failed_attempts=fresh['failed_attempts'],
                      paperwork_returns=fresh.get('paperwork_returns', 0))
        if result['review_kind'] == 'paperwork':
            lines = ['task ' + tid + ' sent back as PAPERWORK (return ' + str(result['paperwork_returns']) +
                     ' on this task; strikes: ' + str(result['failed_attempts']) + ', status: ' + status + ')']
        else:
            lines = ['task ' + tid + ' rejected as substance (strikes: ' + str(result['failed_attempts']) +
                     ', status: ' + status + ')']
    return result, lines


def cmd_note(client, state, args):
    body = check_text(args.text, LIMIT_NOTE, 'Note')
    request_id = check_request_id(args.request_id) or new_request_id('note')
    emit_request_id(request_id)
    response = client.command('note', {'authority_id': args.scope, 'body': body}, request_id)
    result = {'authority_id': args.scope, 'request_id': request_id}
    lines = ['note posted to ' + args.scope]
    # A1: say what the room recorded, never more. Posted is not arrived: "delivered" means stored for that seat,
    # and "read" comes only when that seat itself picks the question up.
    question = response.get('question') if isinstance(response, dict) else None
    if question is not None:
        result['question'] = {'seq': question['seq'], 'to': question['to'], 'state': question['state']}
        lines.append('question #' + str(question['seq']) + ' delivered to ' + question['to'] +
                     ' (not yet read; crew questions shows its marks)')
    elif QUESTION_LOOKALIKE.match(body.split('\n', 1)[0]):
        result['question'] = None
        lines.append('not delivered as a question: the first line must start QUESTION FOR <seat>: with a seat of this '
                     'scope other than you and the owner, written in lowercase as the room names it; it was posted as a '
                     'plain note')
    return result, lines


def cmd_notes(client, state, args):
    notes = state.get('notes', [])
    warning = None
    if args.scope is not None:
        if args.scope not in {a['id'] for a in state.get('authorities', [])}:
            raise CrewError('forbidden', 'Scope is not visible to this actor.')
        if all('authority_id' in n for n in notes):
            notes = [n for n in notes if n['authority_id'] == args.scope]
        else:
            warning = ('--scope was validated but cannot be applied: this room does not report which '
                       'scope a note belongs to, so all notes visible to you are shown below.')
    if args.last:
        notes = notes[-args.last:]
    result = {'notes': notes}
    if warning:
        result['warning'] = warning
    lines = (['warning: ' + warning] if warning else [])
    lines += ['#{} {}{}: {}'.format(n['seq'], n['sender'], ' [' + n['authority_id'] + ']' if n.get('authority_id') else '',
                                  n['body']) for n in notes] or ['(no notes)']
    return result, lines


def cmd_delegate(client, state, args):
    """Default id is content-derived (parent + fence + a short digest of the helper's
    own fields), so a crash-and-retry with identical args answers from the SAME
    helper task instead of creating a second one. To deliberately create a second,
    textually-identical helper under the same held lease, pass an explicit
    --request-id (nothing about the helper's own content can distinguish it)."""
    tid = resolve_task_id(state, args.task)
    task = CrewClient.task_in(state, tid)
    title = check_text(args.title, LIMIT_TITLE, 'Title')
    body = body_text(args, LIMIT_BODY, 'Body')
    acceptance = check_text(args.acceptance, LIMIT_ACCEPTANCE, 'Acceptance')
    held = own_active_lease(task, client.actor_id, state['server_time'])
    if held is None:
        raise CrewError('lease', 'No active lease held by you on this task; claim it before delegating.')
    digest = content_digest(args.to, title, body, acceptance, *([mark_part(args)] if mark_part(args) else []),
                            *['%s=%s' % kv for kv in sorted(tier_payload(args).items())])[:8]   # the mark, then the tags
    request_id = check_request_id(args.request_id) or ('help-' + tid + '-f' + str(held['fence']) + '-' + digest)
    emit_request_id(request_id)
    payload = {'task_id': tid, 'fence': held['fence'], 'recipient': args.to, 'title': title, 'body': body,
               'acceptance': acceptance}
    if getattr(args, 'undoable', None) is not None:
        payload['undoable'] = args.undoable
    payload.update(tier_payload(args))   # A4: tier and effort, when given
    resp = client.command('request_help', payload, request_id)
    child = resp['task']
    result = {'parent_task_id': tid, 'child_task_id': child['id'], 'request_id': request_id}
    return result, ['helper task ' + child['id'] + ' created for ' + args.to]


def cmd_intake(client, state, args):
    """Default external reference AND request id are both derived from the brief's
    own content (scope, recipient, title, body, acceptance), so a crash-and-retry
    with identical args answers from the SAME task instead of leaving a duplicate
    stranded in draft. To deliberately create a second, textually-identical task,
    pass an explicit --ref (the room itself dedups by external reference) AND an
    explicit --request-id (otherwise the repeated default id still collides)."""
    title = check_text(args.title, LIMIT_TITLE, 'Title')
    body = body_text(args, LIMIT_BODY, 'Body')
    acceptance = check_text(args.acceptance, LIMIT_ACCEPTANCE, 'Acceptance')
    digest = content_digest(args.scope, args.to, title, body, acceptance, *([mark_part(args)] if mark_part(args) else []),
                            *['%s=%s' % kv for kv in sorted(tier_payload(args).items())])   # the mark, then the tags
    external_ref = args.external_ref or digest[:24]
    message = {'title': title, 'body': body, 'recipient': args.to, 'authority_id': args.scope,
               'kind': 'handoff.record', 'acceptance': acceptance}
    intake_request_id = check_request_id(args.request_id) or ('intake-' + digest[:24])
    emit_request_id(intake_request_id)
    # N10: a task that already existed before this call (either because this
    # exact request_id was replayed from its own receipt, or because the
    # room's own external_reference dedup found it) must never be reported
    # as freshly "created and delivered" — that already_existed check is
    # taken from the state fetched before the command was ever sent, not
    # from anything the intake/deliver responses themselves claim.
    existing_ids = {t['id'] for t in state.get('tasks', [])}
    payload = {'source_namespace': 'estate', 'external_reference': external_ref, 'message': message}
    if getattr(args, 'undoable', None) is not None:
        payload['undoable'] = args.undoable
    payload.update(tier_payload(args))   # A4: tier and effort, when given
    resp = client.command('intake', payload, intake_request_id)
    task_id = resp['task']['id']
    already_existed = task_id in existing_ids
    deliver_request_id = 'deliver-' + task_id
    emit_request_id(deliver_request_id)
    client.command('deliver', {'task_id': task_id}, deliver_request_id)
    status = current_task(client, task_id)['status']
    result = {'task_id': task_id, 'status': status, 'external_reference': external_ref,
              'intake_request_id': intake_request_id, 'deliver_request_id': deliver_request_id,
              'already_existed': already_existed}
    if already_existed:
        line = 'task ' + task_id + ' already exists (status: ' + status + ')'
    else:
        line = 'task ' + task_id + ' created and delivered to ' + args.to
    return result, [line]


def cmd_decide(client, state, args):
    tid = resolve_task_id(state, args.task)
    response = check_text(args.response, LIMIT_BODY, 'Response')
    refund = bool(getattr(args, 'refund', False))
    before = CrewClient.task_in(state, tid)['failed_attempts']
    request_id = check_request_id(args.request_id) or new_request_id('decide')
    emit_request_id(request_id)
    payload = {'task_id': tid, 'body': response}
    if refund:
        payload['refund'] = True
    client.command('resolve_decision', payload, request_id)
    fresh = current_task(client, tid)
    status = fresh['status']
    result = {'task_id': tid, 'status': status, 'request_id': request_id, 'refund': refund,
              'strikes_before': before, 'strikes_after': fresh['failed_attempts']}
    text = ('task ' + tid + ' decided' + (' with a refund' if refund else '') + ' (strikes: ' + str(before) + ' -> ' +
            str(fresh['failed_attempts']) + ', status: ' + status + ')')
    return result, [text]


def cmd_reinstate(client, state, args):
    tid = resolve_task_id(state, args.task)
    reason = check_text(args.reason, LIMIT_BODY, 'Reason')
    request_id = check_request_id(args.request_id) or new_request_id('reinstate')
    emit_request_id(request_id)
    client.command('reinstate', {'task_id': tid, 'body': reason}, request_id)
    status = current_task(client, tid)['status']
    result = {'task_id': tid, 'status': status, 'request_id': request_id}
    return result, ['task ' + tid + ' reinstated (status: ' + status + ')']


def cmd_cancel(client, state, args):
    tid = resolve_task_id(state, args.task)
    task = CrewClient.task_in(state, tid)
    reason = check_text(args.reason, LIMIT_CANCEL_REASON, 'Reason')
    request_id = check_request_id(args.request_id) or ('cancel-' + tid + '-f' + str(task['fence']))
    emit_request_id(request_id)
    client.command('cancel', {'task_id': tid, 'body': reason}, request_id)
    status = current_task(client, tid)['status']
    result = {'task_id': tid, 'status': status, 'request_id': request_id}
    return result, ['task ' + tid + ' cancelled (status: ' + status + ')']


def cmd_plan_check(client, state, args):
    plan, _, _ = load_plan_file(args.file)
    problems = check_plan_against_state(plan, state)
    if problems:
        result = {'ok': False, 'plan_id': plan['plan_id'], 'problems': problems}
        return result, problems
    result = {'ok': True, 'plan_id': plan['plan_id'], 'scope': plan['scope'], 'task_count': len(plan['tasks'])}
    return result, ['ok']


def cmd_plan_approve(client, state, args):
    """Idempotent by construction: both the intake external_reference and
    every request id are derived only from plan_id and each task's own key,
    never randomly, so re-running this on the same plan file answers from the
    room's own receipts and dedup instead of creating a second copy of
    anything -- including after the same file was approved on the board, which
    runs the very same plans.approve_plan. Never writes any token: the receipt
    holds only plan_id, scope, approved_at and a key -> {task_id, status} map."""
    if state['actor']['role'] != 'owner':
        raise CrewError('forbidden', 'Only the room owner may approve a plan.')
    mark = folder_mark(Path(args.file).parent)       # fix-board L1: the receipt goes back into THIS folder
    plan, plan_path, plan_sha256 = load_plan_file(args.file)
    # Live-room problems, then F14 (a task key already approved with different
    # content): the whole run refuses, named, before the confirm.
    refuse_unapprovable(plan, state)
    # fix2-board N3: the room checks every field of the plan for room keys first; one key anywhere
    # refuses the whole plan here, before the confirm and before anything is sent.
    refuse_plan_keys(client, plan)
    confirm_interactive(plan_summary(plan, plan_sha256), 'plan')

    tasks_receipt, lines = approve_plan(client, plan, state, emit=emit_request_id)
    receipt_path = receipt_path_for(plan_path)
    write_receipt(receipt_path, plan_receipt(plan, state, tasks_receipt), mark)
    lines.append('receipt written to ' + str(receipt_path))

    result = {'plan_id': plan['plan_id'], 'scope': plan['scope'], 'tasks': tasks_receipt,
              'reserve': reserve_keys(plan), 'receipt_path': str(receipt_path)}
    return result, lines


def cmd_decide_file(client, state, args):
    """Shows each entry with the task's current question, asks ONE confirm
    for the whole batch, then applies each entry under a deterministic
    request id and keeps going past an individual refusal so one bad entry
    never blocks the rest (plans.apply_decisions, shared with the board).
    Writes <file stem>.receipt.json beside the file: {applied_at, results}.
    Run again (here or on the board), an entry the earlier receipt shows applied, unchanged and
    confirmed by the room's history is carried forward as ok and not sent again (fix-board M1),
    unless the task has asked again or failed since that answer: then it is sent again, and the
    confirm, the lines and the receipt (`earlier_seq`) say so (fix2-board N1)."""
    path = Path(args.file)
    mark = folder_mark(path.parent)                  # fix-board L1: the receipt goes back into THIS folder
    prepared = prepare_decisions(load_decisions_file(path), state, read_receipt(path))
    confirm_interactive(decisions_summary(prepared), 'decisions file')

    results = apply_decisions(client, prepared, emit=emit_request_id)
    receipt_path = receipt_path_for(path)
    write_receipt(receipt_path, decisions_receipt(state, results), mark)

    result = {'ok': all(r['ok'] for r in results), 'results': results, 'receipt_path': str(receipt_path)}
    lines = decision_lines(results) + ['receipt written to ' + str(receipt_path)]
    return result, lines


STOPPED_ANSWERABLE_BY = 'stopped: only Shane can reinstate (use action reinstate)'
# 2026-10-01 (crew-cli, core-strikes finding #571): once refunds exist the line above is incomplete. It stays byte for
# byte as it was, because other tools and tests key on it; this note says the rest and is printed after it.
STOPPED_NOTE = ('stopped at two strikes: only the owner (Shane) can reinstate it (crew-shane reinstate) or refund one strike '
                '(crew-shane decide --refund); its sender cannot refund it, and a plain decision cannot erase a strike')
NOTES_SCOPE_NOTE = ('only the last 100 notes the room returns to this actor are considered here; an '
                     'older QUESTION or SHANE UPDATE note may not appear.')


def cmd_questions(client, state, args):
    tasks = [t for t in state.get('tasks', []) if t['status'] == 'waiting_decision']
    task_rows = []
    for t in tasks:
        is_helper = t.get('parent_id') is not None
        # F11: two recorded failures stop a task until Shane reinstates it --
        # resolve_decision is refused at that point, so it is no longer
        # "answerable" the normal way, by owner or by the helper's sender.
        stopped_note = None
        if t.get('failed_attempts', 0) >= 2:
            answerable_by = STOPPED_ANSWERABLE_BY
            stopped_note = STOPPED_NOTE
        else:
            answerable_by = ('owner or ' + t['sender']) if is_helper else 'owner'
        task_rows.append({
            'id': t['id'], 'recipient': t['recipient'], 'sender': t['sender'], 'is_helper': is_helper,
            'question': t.get('decision'), 'failed_attempts': t['failed_attempts'],
            'answerable_by': answerable_by, 'stopped_note': stopped_note,
        })
    notes = [n for n in state.get('notes', [])
             if n['body'].startswith('QUESTION') or n['body'].startswith('SHANE UPDATE')]
    if args.since is not None:
        notes = [n for n in notes if n['seq'] > args.since]
    note_rows = [{'seq': n['seq'], 'sender': n['sender'], 'scope': n.get('authority_id'), 'body': n['body']}
                 for n in notes]
    result = {'tasks': task_rows, 'notes': note_rows, 'notes_scope_note': NOTES_SCOPE_NOTE}
    lines = ['tasks waiting on a decision:']
    if task_rows:
        for t in task_rows:
            lines.append('  ' + t['id'] + '  to=' + t['recipient'] + '  from=' + t['sender'] +
                          ('  [helper]' if t['is_helper'] else '') +
                          '  answerable by: ' + t['answerable_by'] + ('; ' + t['stopped_note'] if t['stopped_note'] else '') +
                          '  -- ' + (t['question'] or ''))
    else:
        lines.append('  (none)')
    lines.append('questions/updates in lounge notes (' + NOTES_SCOPE_NOTE + '):')
    if note_rows:
        for n in note_rows:
            lines.append('  #{} {} [{}]: {}'.format(n['seq'], n['sender'], n['scope'], n['body']))
    else:
        lines.append('  (none)')
    # A1: open questions addressed to this seat or asked by it, with every mark (a room older than A1 sends none).
    addressed = sorted([q for q in (state.get('questions') or []) + (state.get('asked') or [])
                        if args.since is None or q['seq'] > args.since], key=lambda q: q['seq'])
    result['addressed'] = addressed
    lines.append('questions addressed to you or asked by you, not yet checked (marks: delivered, read, accepted, checked):')
    if addressed:
        for q in addressed:
            marks = ', '.join(m for m in ('delivered', 'read', 'accepted', 'checked') if q['marks'].get(m) is not None)
            lines.append('  #{} {} -> {} [{}]: {}  ({})'.format(q['seq'], q['from'], q['to'], q['authority_id'],
                                                                  _flat(q.get('excerpt', ''), 120), marks))
    else:
        lines.append('  (none)')
    return result, lines


def cmd_question(client, state, args):
    """A1: record one mark on an addressed question. The room enforces who and in what order."""
    mark = QUESTION_MARK_NAMES[args.mark]
    request_id = check_request_id(args.request_id) or ('qmark-' + mark + '-' + str(args.seq))
    emit_request_id(request_id)
    response = client.command('question_mark', {'note_seq': args.seq, 'mark': mark}, request_id)
    question = response.get('question') if isinstance(response, dict) else None
    result = {'seq': args.seq, 'mark': mark, 'request_id': request_id, 'question': question}
    state_now = question['state'] if question else mark
    return result, ['question #' + str(args.seq) + ' marked ' + mark + ' (now: ' + state_now + ')']


def cmd_card(client, state, args):
    from . import join_cards
    handoffs_root = Path(client.path).resolve().parent
    # F15: render_card's `tree` is the package tree root (the folder that
    # holds common_room/ and every top-level guide such as
    # HERMES_SEAT_2026-09-23.md), never the handoffs folder itself -- the
    # room root convention nests handoffs two levels below it
    # (<tree>/.runtime/handoffs or, in this test fixture layout,
    # <tree>/.checks/<case>). Passing the handoffs folder here silently
    # dropped the hermes card's scheduled-job block.
    tree_root = handoffs_root.parent.parent
    enrollment_path = handoffs_root / 'enrollment.json'
    try:
        raw = enrollment_path.read_bytes()
    except OSError as exc:
        raise CrewError('config', 'Could not read enrollment.json: ' + str(exc)) from exc
    try:
        enrollment = json.loads(raw.decode('utf-8'))
    except ValueError as exc:
        raise CrewError('invalid', 'enrollment.json is not valid JSON.') from exc
    try:
        card_text = join_cards.render_card(args.seat, enrollment, tree=tree_root)
    except join_cards.JoinCardError as exc:
        raise CrewError('invalid', str(exc)) from exc
    result = {'seat': args.seat, 'card': card_text}
    return result, [card_text]


def copy_to_clipboard(text):
    """Put `text` on the Windows clipboard through clip.exe's stdin, never its command line
    (which other processes can read). An empty string clears the clipboard. clip.exe is
    named by its full System32 path so a clip.exe in the current folder is never run."""
    if os.name != 'nt':
        raise CrewError('config', 'login uses the Windows clipboard; run crew-shane login on the PC.')
    try:
        data = text.encode('ascii')
    except (AttributeError, UnicodeError) as exc:
        raise CrewError('invalid', 'Only ASCII text can go on the clipboard here.') from exc
    clip = os.path.join(os.environ.get('SystemRoot', 'C:\\Windows'), 'System32', 'clip.exe')
    try:
        done = subprocess.run([clip], input=data, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CrewError('config', 'Could not reach the Windows clipboard (clip.exe).') from exc
    if done.returncode != 0:
        raise CrewError('config', 'The Windows clipboard (clip.exe) refused the text.')


def cmd_login(client, state, args):
    """Owner only: the key goes to the clipboard for the board's Room access key box, and is
    cleared after Enter (or an interrupt). The key itself is never printed."""
    if state['actor']['id'] != 'keeper' or state['actor']['role'] != 'owner':
        raise CrewError('forbidden', 'login is for the owner seat only (crew-shane login).')
    if not sys.stdin.isatty():
        raise CrewError('config', 'login needs an interactive terminal (Shane at the keyboard).')
    origin = client._participant.origin
    copy_to_clipboard(client._participant.token)
    try:
        print('Board: ' + origin + '/', file=sys.stderr)
        print('The owner key is on the clipboard: open the board, paste it into Room access key, then press '
              'Enter here to clear the clipboard.', file=sys.stderr)
        sys.stdin.readline()
    finally:
        copy_to_clipboard('')
    result = {'origin': origin, 'clipboard': 'cleared'}
    return result, ['Clipboard cleared. Board: ' + origin + '/']


# ---------------------------------------------------------------------------
# stop-all, resume-all and usage (2026-10-01, upgrade ws-crew step 5, crew-cli). The room is the authority for the
# first two (keeper only: it refuses everyone else); `usage` reads the workers' own logs and sends nothing.
# ---------------------------------------------------------------------------

def _authority_flags(state):
    return {a['id']: bool(a.get('paused')) for a in state.get('authorities', [])}


def cmd_stop_all(client, state, args):
    reason = check_text(args.reason, 500, 'Reason')
    before = _authority_flags(state)
    request_id = check_request_id(args.request_id) or new_request_id('stop-all')
    emit_request_id(request_id)
    client.command('stop_all', {'reason': reason}, request_id)
    fresh = client.state()
    after = _authority_flags(fresh)
    paused = sorted(k for k, v in after.items() if v and not before.get(k, False))
    already = sorted(k for k, v in before.items() if v)
    stopped = bool((fresh.get('stop') or {}).get('active'))
    result = {'paused': paused, 'already_paused': already, 'stopped': stopped, 'reason': reason, 'request_id': request_id}
    lines = ['room STOPPED: paused ' + (', '.join(paused) or '(none)') +
             ('; already paused: ' + ', '.join(already) if already else '') +
             '. A STOP: note was posted; every waiting seat stops. Undo with: resume-all']
    return result, lines


def cmd_resume_all(client, state, args):
    reason = check_text(args.reason, 500, 'Reason') if args.reason is not None else None
    before = _authority_flags(state)
    request_id = check_request_id(args.request_id) or new_request_id('resume-all')
    emit_request_id(request_id)
    client.command('resume_all', {'reason': reason} if reason is not None else {}, request_id)
    fresh = client.state()
    after = _authority_flags(fresh)
    resumed = sorted(k for k, v in before.items() if v and not after.get(k, True))
    still = sorted(k for k, v in after.items() if v)
    stopped = bool((fresh.get('stop') or {}).get('active'))
    result = {'resumed': resumed, 'still_paused': still, 'stopped': stopped, 'request_id': request_id}
    lines = ['room RESUMED: resumed ' + (', '.join(resumed) or '(none)') +
             ('; still paused (by hand or revoked): ' + ', '.join(still) if still else '') + '.']
    return result, lines


USAGE_MAX_FILE_BYTES = 16 * 1024 * 1024
USAGE_OUTCOMES = ('finished', 'question', 'failed', 'skipped_quiet')
USAGE_COMMON = {'ts', 'seat', 'kind', 'task_id', 'model', 'seconds', 'outcome'}
USAGE_FIELDS = {'local': USAGE_COMMON | {'prompt_tokens', 'output_tokens'},
                'headless': USAGE_COMMON | {'exit', 'budget_cap_usd'}}
USAGE_PROBLEM_LIST_MAX = 20


def _usage_seat(text):
    if not ACTOR_RE.fullmatch(text):
        raise argparse.ArgumentTypeError('--seat is a seat id like local-a.')
    return text


def _usage_days(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError('--days is a whole number of days.')
    if value < 1:
        raise argparse.ArgumentTypeError('--days is at least 1.')
    return value


def _is_number(value):
    return type(value) in (int, float) and value == value and value >= 0


def _usage_row(line, seat):
    """(row, why-not): one usage line per USAGE_FORMAT.md, or the short reason it is refused. No text from the line is echoed."""
    try:
        row = json.loads(line)
    except ValueError:
        return None, 'not valid JSON'
    if not isinstance(row, dict):
        return None, 'not a JSON object'
    fields = USAGE_FIELDS.get(row.get('kind'))
    if fields is None:
        return None, 'unknown kind'
    if set(row) != fields:
        return None, 'wrong or extra fields'
    if row['seat'] != seat:
        return None, 'seat does not match the file'
    if row['outcome'] not in USAGE_OUTCOMES:
        return None, 'unknown outcome'
    if not _is_number(row['seconds']):
        return None, 'seconds is not a number'
    for name in ('prompt_tokens', 'output_tokens'):
        if name in row and row[name] is not None and not (type(row[name]) is int and row[name] >= 0):
            return None, name + ' is not a count'
    if 'exit' in row and row['exit'] is not None and type(row['exit']) is not int:
        return None, 'exit is not a number'
    try:
        row['_epoch'] = datetime.datetime.strptime(row['ts'], '%Y-%m-%dT%H:%M:%SZ').replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None, 'ts is not a UTC time'
    return row, None


def cmd_usage(client, args):
    folder = Path(client.path).parent / 'usage'
    since = (time.time() - args.days * 86400) if args.days else None
    seats, problems, bad = {}, [], 0
    files = sorted(folder.glob('*.jsonl')) if folder.is_dir() else []
    for path in files:
        seat = path.stem
        if args.seat and seat != args.seat:
            continue
        try:
            size = path.stat().st_size
            if size > USAGE_MAX_FILE_BYTES:
                bad += 1
                problems.append(path.name + ': file over 16 MB, skipped')
                continue
            raw = path.read_bytes()
        except OSError as exc:
            bad += 1
            problems.append(path.name + ': unreadable (' + type(exc).__name__ + ')')
            continue
        for number, line in enumerate(raw.decode('utf-8', errors='replace').splitlines(), 1):
            if not line.strip():
                continue
            row, why = _usage_row(line, seat)
            if row is None:
                bad += 1
                problems.append(path.name + ' line ' + str(number) + ': ' + why)
                continue
            if since is not None and row['_epoch'] < since:
                continue
            s = seats.setdefault(seat, {'kind': row['kind'], 'jobs': 0, 'seconds': 0.0, 'outcomes': {}, 'prompt_tokens': 0,
                                        'output_tokens': 0, 'nonzero_exits': 0})
            s['jobs'] += 1
            s['seconds'] = round(s['seconds'] + row['seconds'], 3)
            s['outcomes'][row['outcome']] = s['outcomes'].get(row['outcome'], 0) + 1
            s['prompt_tokens'] += row.get('prompt_tokens') or 0
            s['output_tokens'] += row.get('output_tokens') or 0
            if row.get('exit') not in (None, 0):
                s['nonzero_exits'] += 1
    jobs = sum(s['jobs'] for s in seats.values())
    result = {'folder': str(folder), 'window_days': args.days, 'seats': seats, 'jobs': jobs, 'bad_lines': bad,
              'problems': problems[:USAGE_PROBLEM_LIST_MAX]}
    if not files:
        return result, ['usage: no usage recorded yet (' + str(folder) + ' has no *.jsonl files)']
    window = 'last ' + str(args.days) + ' day(s)' if args.days else 'all time'
    lines = ['usage: ' + str(jobs) + ' jobs from ' + str(len(seats)) + ' seat(s), ' + window + ' (read-only, from ' + str(folder) + ')']
    for seat, s in sorted(seats.items()):
        counts = ', '.join(k + ' ' + str(s['outcomes'][k]) for k in USAGE_OUTCOMES if k in s['outcomes'])
        if s['kind'] == 'local':
            extra = 'model seconds ' + str(s['seconds']) + ', tokens in ' + str(s['prompt_tokens']) + ' out ' + str(s['output_tokens'])
        else:
            extra = 'agent seconds ' + str(s['seconds']) + ', nonzero exits ' + str(s['nonzero_exits'])
        lines.append('  {:<14} {:<8} jobs {} ({}); {}'.format(seat, s['kind'], s['jobs'], counts, extra))
    if bad:
        lines.append('  ' + str(bad) + ' bad line(s), never counted as jobs:')
        lines += ['    ' + p for p in result['problems']]
        if bad > len(result['problems']):
            lines.append('    ...and ' + str(bad - len(result['problems'])) + ' more')
    return result, lines


HANDLERS = {
    'whoami': cmd_whoami, 'tasks': cmd_tasks, 'review-queue': cmd_review_queue,
    'show': cmd_show, 'read': cmd_read, 'ack': cmd_ack, 'claim': cmd_claim,
    'renew': cmd_renew, 'submit': cmd_submit, 'fail': cmd_fail, 'ask': cmd_ask,
    'review': cmd_review, 'note': cmd_note, 'notes': cmd_notes, 'delegate': cmd_delegate,
    'intake': cmd_intake, 'decide': cmd_decide, 'reinstate': cmd_reinstate,
    'cancel': cmd_cancel, 'plan-check': cmd_plan_check, 'plan-approve': cmd_plan_approve,
    'decide-file': cmd_decide_file, 'questions': cmd_questions, 'question': cmd_question, 'card': cmd_card,
    'login': cmd_login, 'stop-all': cmd_stop_all, 'resume-all': cmd_resume_all,
}


# needs (turnons-2026-10-03, job panels): the board's Needs Shane list on the command line. The list comes from
# common_room/needs.py applied to this seat's own /api/state, so it is the same list the board shows that seat.
NEEDS_EXIT = 2


def cmd_needs(client, state, args):
    from .needs import room_needs
    from .ollama_worker import CentralTime
    items = room_needs(state)
    if not items:
        return items, ['Nothing needs Shane.']
    central = CentralTime()
    lines = ['Needs Shane: ' + str(len(items)) + ' item(s), oldest first within each kind.']
    for n, item in enumerate(items, 1):
        when = central.local(item['since']).strftime('%Y-%m-%d %H:%M') + ' CT' if item['since'] is not None else 'time unknown'
        lines += ['{}. [{}] since {}  {}'.format(n, item['kind'], when, item['summary']), '   do: ' + item['action'],
                  '   id: ' + item['id']]
    return items, lines


# ---------------------------------------------------------------------------
# `crew watch`: wait for work without a model in the loop (2026-10-01, upgrade ws-crew).
# It polls the room, prints ONE short line and exits: 0 something to do, 3 WAIT (nothing yet,
# run it again), 4 STOP, 5 ROOM DOWN. It reads only what the room already shows this seat and
# prints only a short id, a status and a title, never a credential, token or note beyond the
# first 200 characters of a STOP note, or (A1) the opening of a question addressed to this seat,
# with secret-looking words hidden (needs.scrub).
# ---------------------------------------------------------------------------

def _flat(text, limit):
    text = ''.join(c if c.isprintable() else ' ' for c in str(text))
    return ' '.join(text.split())[:limit]


def _watch_line(event, task, middle, others):
    head = event + ' ' + short_id(task['id']) + ' ' + middle
    tail = ' (+' + str(others) + ' more)' if others else ''
    return head + _flat(task.get('title', ''), max(0, WATCH_LINE_MAX - len(head) - len(tail))) + tail


def _watch_stop_file(client):
    """The STOP file `core-stop` writes beside the credential files: present means stop, whatever it holds."""
    path = Path(client.path).parent / 'STOP'
    if not path.exists():
        return None
    try:
        text = path.read_bytes()[:4 * WATCH_STOP_CHARS].decode('utf-8', errors='replace')
    except OSError:
        text = ''
    return _flat(text, WATCH_STOP_CHARS)


def _watch_event(client, state, reviews, base):
    """What this seat should act on in one snapshot: (event, line, exit code) or None. `base` keeps what
    was true when the watch began, so only notes and review results that arrive AFTER it count."""
    me = client.actor_id
    now = state['server_time']
    tasks = state.get('tasks', [])
    notes = state.get('notes', [])
    mine = [t for t in tasks if t['recipient'] == me or t.get('builder') == me]
    if base['note_seq'] is None:
        base['note_seq'] = max((n['seq'] for n in notes), default=0)
        base['status'] = {t['id']: t['status'] for t in mine}
    else:
        for n in notes:
            if n['seq'] > base['note_seq'] and n.get('sender') in WATCH_STOP_SENDERS and STOP_NOTE.match(n['body']):
                return 'STOP', 'STOP ' + _flat(n['body'], WATCH_STOP_CHARS), 4
    found = []
    # A1 (2026-10-24): a question addressed to this seat that it has not read yet. Level-triggered like DECIDE, so a
    # question delivered while no watch ran still wakes the next one. Returning it IS the seat reading it, so the watch
    # records the read mark first; a question the room will not mark is skipped for this watch, never returned again
    # and again (it stays delivered, and `crew questions` shows it).
    question = _watch_question(client, state, base)
    for t in mine:
        if base['status'].get(t['id']) == 'ready_for_review' and t['status'] != 'ready_for_review':
            found.append(('REVIEWED', t, 'accepted: ' if t['status'] == 'done' else 'rejected: '))
    # DECIDE is level-triggered, unlike REVIEWED: a helper this seat sent is waiting on this seat's decision (it was
    # rejected, or it asked a question). At two strikes the decision is the owner's, so it is left out.
    for t in tasks:
        if t.get('sender') == me and t['status'] == 'waiting_decision' and t.get('failed_attempts', 0) < 2:
            rejected = str(t.get('decision') or '').startswith('Review requires an owner decision')
            found.append(('DECIDE', t, 'rejected: ' if rejected else 'question: '))
    for t in mine:
        if t['recipient'] != me:
            continue
        if t['status'] == 'delivered' and t.get('decision_response'):
            found.append(('ANSWER', t, ''))
        elif t['status'] in ('delivered', 'acknowledged') or (
                t['status'] == 'claimed' and own_active_lease(t, me, now) is None):
            found.append(('TASK', t, t['status'] + ': '))
    if reviews:
        found += [('REVIEW', t, '') for t in client.review_queue(state)]
    if question is not None:
        return 'QUESTION', _question_line(question, len(found)), 0
    if not found:
        return None
    event, task, middle = found[0]
    return event, _watch_line(event, task, middle, len(found) - 1), 0


def _question_line(question, others):
    head = 'QUESTION #' + str(question['seq']) + ' from ' + _flat(question['from'], 64) + ': '
    tail = ' (+' + str(others) + ' more)' if others else ''
    from .needs import scrub
    excerpt = scrub(_flat(question.get('excerpt', ''), WATCH_LINE_MAX), WATCH_LINE_MAX)
    return head + _flat(excerpt, max(0, WATCH_LINE_MAX - len(head) - len(tail))) + tail


def _watch_question(client, state, base):
    """The oldest question addressed to this seat and not yet read, after recording its read mark; or None. The
    request id is one per question, so a second watch, or this seat's relay, answers from the room's receipt."""
    me = client.actor_id
    skipped = base.setdefault('unmarked', set())
    for q in state.get('questions') or []:
        if q.get('to') != me or (q.get('marks') or {}).get('read') is not None or q['seq'] in skipped:
            continue
        try:
            client.command('question_mark', {'note_seq': q['seq'], 'mark': 'read'}, 'qmark-read-' + str(q['seq']))
        except CrewError:
            skipped.add(q['seq'])
            continue
        return q
    return None


def _watch_poll(client):
    """One poll over the watch's ONE session. `client.watch_state()` connects on the first call and reuses that session
    on every poll after it: the room keeps at most 256 logins, each for up to 900 s, so a login per poll let a few
    watching seats lock everyone out (#1232). Only when the room answers unauthenticated (session expired, keys
    rotated, room restarted) does the client reconnect, once, and that re-reads this seat's own credential file, so a
    key rotation still never strands a watch. A client without watch_state (the fakes in the older tests) polls with
    its state()."""
    poll = getattr(client, 'watch_state', None)
    return poll() if callable(poll) else client.state()


def _end_session(client):
    """Log the client out of the room when a command or watch ends (best effort: it never raises and never changes an
    exit code). A client without a logout (a test's fake) is left alone."""
    logout = getattr(client, 'logout', None)
    if callable(logout):
        try:
            logout()
        except Exception:
            pass


def run_watch(client, args, sleep=time.sleep, monotonic=time.monotonic):
    """The loop behind `crew watch`, which logs its session out however it ends (normal, Ctrl+C or error).
    `sleep` and `monotonic` are parameters so tests run it on a fake clock."""
    try:
        return _watch_loop(client, args, sleep, monotonic)
    finally:
        _end_session(client)


def _watch_loop(client, args, sleep, monotonic):
    deadline = monotonic() + args.max_wait
    base = {'note_seq': None, 'status': {}}
    failing_since, failures = None, 0

    def finish(event, line, code):
        print(canonical({'event': event, 'line': line, 'exit_code': code}) if args.json else line)
        return code

    while True:
        stopped = _watch_stop_file(client)
        if stopped is not None:
            return finish('STOP', ('STOP ' + stopped).rstrip(), 4)
        try:
            state = _watch_poll(client)
        except CrewError as exc:
            if exc.code not in WATCH_RETRY_CODES:
                raise
            now = monotonic()
            if failing_since is None:
                failing_since = now
            if now - failing_since >= WATCH_DOWN_AFTER:
                return finish('ROOM DOWN', 'ROOM DOWN', 5)
            pause = WATCH_BACKOFF[failures] if failures < len(WATCH_BACKOFF) else WATCH_POLL_SECONDS
            failures += 1
        else:
            failing_since, failures = None, 0
            found = _watch_event(client, state, args.reviews, base)
            if found is not None:
                return finish(*found)
            pause = WATCH_POLL_SECONDS
        left = deadline - monotonic()
        if left <= 0:
            return finish('WAIT', 'WAIT', 3)
        sleep(min(pause, left))


# ---------------------------------------------------------------------------
# `crew branch` (2026-10-01, upgrade ws-crew step 4): one command gives a job its own clean copy of the code, so no
# job's report gets tangled with another job's changes. It needs no room and never touches the repo's own worktree.
# ---------------------------------------------------------------------------

EVIDENCE_TOOL = ('Artifacts', 'cc-build-bandm-v2-2026-09-30', 'tools', 'evidence.py')


def _evidence_tool_path():
    estate = find_estate_root(ROOT)
    candidate = estate.joinpath(*EVIDENCE_TOOL) if estate is not None else None
    return str(candidate) if candidate is not None and candidate.is_file() else '<path to tools\\evidence.py>'


def _path_inside(path, folder):
    """True when `path` is `folder` or beneath it (real paths, case-folded on Windows)."""
    p = os.path.normcase(os.path.realpath(path))
    f = os.path.normcase(os.path.realpath(folder)).rstrip(os.sep)
    return p == f or p.startswith(f + os.sep)


def cmd_branch(args):
    key = args.key
    if not TASK_KEY_RE.fullmatch(key):
        raise CrewError('invalid', 'The task key "' + key + '" is not valid: use 1 to 24 lowercase letters, digits or '
                        'hyphens, starting with a letter or digit (for example crew-watch).')
    if args.base.startswith('-'):
        raise CrewError('invalid', '--base names a commit, branch or tag; it cannot start with "-".')
    repo = Path(args.repo)
    if not repo.is_dir():
        raise CrewError('config', '--repo ' + str(repo) + ' is not a folder.')
    repo = repo.resolve()
    top, error = _gate_git(repo, 'rev-parse', '--show-toplevel')
    if top is None:
        raise CrewError('config', str(repo) + ' is not a git repo or worktree (git says: ' + error + ').')
    if Path(top.decode('utf-8', errors='replace').strip()).resolve() != repo:
        raise CrewError('config', str(repo) + ' is a folder inside a git repo, not the top of one; name the repo or '
                        'worktree folder itself (' + top.decode('utf-8', errors='replace').strip() + ').')
    out, error = _gate_git(repo, 'rev-parse', '--verify', '--quiet', args.base + '^{commit}')
    base = out.decode('utf-8', errors='replace').strip() if out else ''
    if not base:
        raise CrewError('config', '--base ' + args.base + ' is not a commit, branch or tag in ' + str(repo) + '.')
    root = Path(args.root) if args.root else repo.parent / 'wt'
    target = root / key
    branch = 'up/' + key
    listing, _ = _gate_git(repo, 'worktree', 'list', '--porcelain')
    for line in (listing or b'').decode('utf-8', errors='replace').splitlines():
        if line.startswith('worktree ') and _path_inside(root, line[len('worktree '):]):
            raise CrewError('invalid', 'The folder ' + str(root) + ' is the git worktree ' + line[len('worktree '):] + ' of this '
                            'repo or inside it; nothing was created. Pick a --root outside every worktree of the repo '
                            '(the default is a "wt" folder beside it).')
    if os.path.lexists(target):
        raise CrewError('exists', 'The folder ' + str(target) + ' already exists; nothing was created. Pick another task '
                        'key or --root, or remove that folder yourself if it is a leftover.')
    taken, _ = _gate_git(repo, 'rev-parse', '--verify', '--quiet', 'refs/heads/' + branch)
    if taken:
        raise CrewError('exists', 'The branch ' + branch + ' already exists in ' + str(repo) + '; nothing was created. '
                        'Pick another task key, or delete that branch yourself if it is a leftover.')
    root.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    try:
        added = subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-b', branch, str(target), base],
                               capture_output=True, env=env, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CrewError('git', 'git could not be run: ' + str(exc)) from exc
    if added.returncode != 0:
        raise CrewError('git', '`git worktree add` failed: ' + added.stderr.decode('utf-8', errors='replace').strip())
    head, error = _gate_git(target, 'rev-parse', 'HEAD')
    if head is None or head.decode('utf-8', errors='replace').strip() != base:
        raise CrewError('git', 'The new worktree ' + str(target) + ' is not at ' + base + '; check it by hand.')
    command = ('py -3 "' + _evidence_tool_path() + '" --repo "' + str(target) + '" --parent ' + base[:7] +
               ' --commit <your commit> --check <test file inside your commit> --out <folder outside every git repo>')
    result = {'worktree': str(target), 'branch': branch, 'base': base, 'base_given': args.base, 'repo': str(repo),
              'evidence_command': command}
    lines = ['worktree: ' + str(target), 'branch: ' + branch,
             'base: ' + base + ('' if args.base == base else ' (' + args.base + ')'),
             'RED/GREEN: ' + command, 'The repo\'s own worktree at ' + str(repo) + ' was not touched.']
    return result, lines


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def build_client(parser, args):
    if args.credential is not None:
        path = args.credential
        expected = None
    else:
        path = resolve_as_actor(args.as_actor, args.room_root)
        expected = args.as_actor
        if expected == 'keeper':
            # Only here does the owner key come out of the vault (the file holds a stub).
            return CrewClient(path, expected_actor='keeper', loader=keeper_vault.keeper_loader(path, args.room_root))
    return CrewClient(path, expected_actor=expected)


def run_with_client(client, args):
    """Run one already-connected CrewClient session against one parsed subcommand.
    Kept separate from `run` so a caller (or a test) can hold ONE CrewClient across
    several commands and a key rotation in between: the client re-reads its own
    credential file on the room's next 401, so the SAME object keeps working. Each
    fresh `python -m common_room.crew` process only ever calls this once, through
    `run`, but the persistence guarantee lives in CrewClient itself, not here."""
    if args.cmd == 'watch':
        return run_watch(client, args)    # owns its own polling: a down room is ROOM DOWN, not an error
    if args.cmd == 'branch':              # local git only: no room call, so it works with the room down
        result, lines = cmd_branch(args)
        for line in ([canonical(result)] if args.json else lines):
            print(line)
        return 0
    if args.cmd == 'usage':               # local files only: no room call, so it works with the room down
        result, lines = cmd_usage(client, args)
        for line in ([canonical(result)] if args.json else lines):
            print(line)
        return 0
    state = client.state()
    if args.cmd == 'needs':               # exit 2 when something needs Shane, so a script can test it
        items, lines = cmd_needs(client, state, args)
        for line in ([canonical(items)] if args.json else lines):
            print(line)
        return NEEDS_EXIT if items else 0
    handler = HANDLERS[args.cmd]
    result, lines = handler(client, state, args)
    # A4: after the room starts a new key cycle, this seat's calls carry one note (until its first command there)
    # pointing it to its current join card. crew already uses the new key; the note only says so. It goes to stderr,
    # beside the request-id lines, so stdout stays byte for byte what it was (a saved `crew read` still hashes right).
    renewed = state.get('room_renewed') if isinstance(state, dict) else None
    if isinstance(renewed, dict) and isinstance(renewed.get('note'), str):
        print('note: ' + renewed['note'], file=sys.stderr)
    if args.json:
        print(canonical(result))
    else:
        for line in lines:
            print(line)
    if isinstance(result, dict) and result.get('ok') is False:
        return 1
    return 0


def run(parser, args):
    client = build_client(parser, args)
    try:
        return run_with_client(client, args)
    finally:
        _end_session(client)    # one command, one login: log it out, or it lingers in the room's table for 900 s


def _reconfigure_streams():
    """A pipe on Windows defaults to the console's legacy code page (e.g. cp1252),
    not UTF-8; any note, title or report text outside that page would otherwise
    crash the whole command with an uncaught UnicodeEncodeError. backslashreplace
    means a character that can't round-trip is shown escaped rather than crashing.
    """
    for name in ('stdout', 'stderr'):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, 'reconfigure', None)
        if callable(reconfigure):
            try:
                reconfigure(encoding='utf-8', errors='backslashreplace')
            except (ValueError, OSError):
                pass


def main(argv=None):
    _reconfigure_streams()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        return run(parser, args)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 2
    except CrewError as exc:
        print(exc.code + ': ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
