"""Wake-up tooling for a Cowork chat that only checks in occasionally (a scheduled task).

One cheap relay round trip answers "is there work for this seat right now?" so a scheduled
firing can end at once (exit 3) instead of spending a whole session on an empty room. It also
holds an advisory per-seat session lock, so a scheduled run and a live chat never act as the
same seat at once, and it tracks (in a small file it owns) which Lounge notes and which set of
"questions for Shane" this seat has already surfaced, so a phone alert only fires once per new
question. Python 3.8+, stdlib only, so it runs inside the Cowork Linux VM exactly like
crew/relay_ask.py, which it imports directly (never re-implementing the relay wire protocol).

tick.py never reads or opens any *-credential.json file: it only ever opens three names it
constructs itself under <relay-folder> -- relay-state.json (read-only; the relay's own file),
tick-state.json and session.lock (both owned by this tool) -- never a path taken from state
content or from another seat's folder.

The relay folder may sit on a mounted Windows drive where deleting a file is refused and
replacing an existing file by rename can fail too (see crew/relay.py's own folder-safety notes
for why: a shared/linked file is never trusted). So tick.py never deletes and never renames a
file over an existing one. Its own two small files are created with O_CREAT|O_EXCL when absent
and, when present, opened for write and truncated in place instead.

    python3 tick.py <relay-folder> check --role orchestrator|reviewer [--holder LABEL]
                                          [--timeout 90] [--max-age 120]
    python3 tick.py <relay-folder> lock --holder auto|LABEL [--minutes 45]
    python3 tick.py <relay-folder> unlock --holder LABEL
    python3 tick.py <relay-folder> lock-status
    python3 tick.py <relay-folder> questions
    python3 tick.py <relay-folder> notified --digest SHA256
    python3 tick.py <relay-folder> seen --note-seq N
    python3 tick.py <relay-folder> seen --all
    python3 tick.py <relay-folder> answered --note-seq N

An orchestrator's own open task counts as work when it is new (delivered), when this tool has not
yet recorded it as seen, or when it or any helper under it changed status since `seen --all` last
recorded them. An open question from one of its own helpers (which it answers itself, or escalates
to Shane through QUESTIONS.md) follows the same rule: work until `seen --all` records it, then quiet
until that helper's status or its question text changes. So a task that only waits (a reserve
task, a workstream whose helpers are still working, a helper question escalated to Shane) goes
quiet after `seen --all`, and wakes the seat again as soon as something moves. A session that
still has its own work to continue should NOT run `seen --all` at the end, so the next check
wakes it again.

`seen` only decides what wakes the seat. It never changes `questions_for_shane` or its digest:
that list is every task waiting on a decision this seat cannot answer, plus every Lounge note
starting QUESTION (while it is in the room's latest-100 window) that `answered --note-seq N` has
not yet recorded as answered. Recording an answer when Shane was already notified of the list
carries that notification forward, so one question fewer is not a new batch for his phone.

`lock --holder auto` makes a holder for this session alone (<seat>-<UTC time>-<random>), takes the
lock with it and prints it as `holder`; `unlock` needs that exact holder (never `auto`). Running
`lock` again with the same holder renews it. `check` without --holder reports busy (4) whenever
anyone holds an unexpired lock.

<relay-folder> is .../tree/.runtime/handoffs/relay/<seat>; the seat is the folder's own name.

Exit codes for `check`: 0 there is work (or a new question for Shane), 3 idle (nothing to do),
4 another session holds the session lock, 5 relay trouble: the relay did not answer the state
request in time, refused it, or left relay-state.json missing, no newer than before, or with an
age outside [-max-age, max-age] widened by the round trip (the relay's clock and this one differ).
A relay-state.json whose age is negative is stale, never fresh. On 5 the output's `reason` says
which. `lock` and `unlock` also use 4 for a refusal (busy, or not yours to release). Any other
code (1 or 2: a Python error, an argparse usage error, or tick.py not found at all) means tick.py
itself failed, never the relay.
"""
import argparse, hashlib, json, os, re, sys, time, uuid
from os.path import abspath, dirname, join, normpath, basename

sys.path.insert(0, dirname(abspath(__file__)))
import relay_ask  # local module: the one place that knows the relay's wire protocol

STATE_NAME = 'relay-state.json'
TICK_STATE_NAME = 'tick-state.json'
LOCK_NAME = 'session.lock'
FLAGGED_PREFIXES = ('QUESTION', 'SHANE UPDATE')
AUTO_HOLDER = 'auto'

EXIT_WORK, EXIT_IDLE, EXIT_BUSY, EXIT_RELAY = 0, 3, 4, 5


# -- small, deterministic JSON helpers (no dependency on common_room.protocol: stdlib only) --

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def sha256_of(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def seat_of(folder):
    return basename(normpath(folder))


# -- chat-owned small files: never delete, never rename over an existing one -----------------

def _read_json_file(path):
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _write_in_place(path, value):
    """Create with O_CREAT|O_EXCL when the file is absent; otherwise open the existing
    file for write and truncate to the new length in place. Never unlinks anything and
    never calls os.replace/os.rename, both refused for an existing file on the mount
    this is written for."""
    data = canonical(value).encode('utf-8')
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)
    try:
        fd = os.open(path, flags, 0o600)
        created = True
    except FileExistsError:
        fd = os.open(path, os.O_WRONLY | getattr(os, 'O_BINARY', 0))
        created = False
    try:
        os.write(fd, data)
        if not created:
            os.ftruncate(fd, len(data))
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        os.close(fd)


# -- the advisory session lock ----------------------------------------------------------------

def read_lock(folder):
    return _read_json_file(join(folder, LOCK_NAME))


def lock_state(record, now):
    if record is None:
        return 'absent'
    if record.get('released'):
        return 'released'
    until = record.get('until')
    if not isinstance(until, (int, float)) or until <= now:
        return 'expired'
    return 'held'


def auto_holder(folder, now=None):
    """A holder for one session alone: <seat>-<UTC yyyymmddTHHMMSSZ>-<8 random hex>. Two firings in
    the same second still differ, so one can neither pass the other's lock nor release it."""
    now = time.time() if now is None else now
    seat = re.sub(r'[^A-Za-z0-9_.-]', '-', seat_of(folder)).strip('-') or 'seat'
    return '%s-%s-%s' % (seat, time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now)), uuid.uuid4().hex[:8])


def cmd_lock(folder, holder, minutes, now=None):
    now = time.time() if now is None else now
    if holder == AUTO_HOLDER:
        holder = auto_holder(folder, now)
    path = join(folder, LOCK_NAME)
    record = {'holder': holder, 'since': now, 'until': now + minutes * 60.0}
    data = canonical(record).encode('utf-8')
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)
    try:
        fd = os.open(path, flags, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except FileExistsError:
        current = read_lock(folder)
        state = lock_state(current, now)
        already_ours = state == 'held' and (current or {}).get('holder') == holder
        if state not in ('absent', 'expired', 'released') and not already_ours:
            return {'ok': False, 'error': 'locked', 'holder': current.get('holder'),
                    'until': current.get('until')}, 4
        fd = os.open(path, os.O_WRONLY | getattr(os, 'O_BINARY', 0))
        try:
            os.write(fd, data)
            os.ftruncate(fd, len(data))
        finally:
            os.close(fd)
    check = read_lock(folder)
    if check is None or check.get('holder') != holder or check.get('since') != record['since']:
        # Another acquire landed on top of ours: last writer wins, and we lost.
        return {'ok': False, 'error': 'locked', 'holder': (check or {}).get('holder'),
                'until': (check or {}).get('until')}, 4
    return {'ok': True, 'holder': holder, 'result': record}, 0


def cmd_unlock(folder, holder):
    now = time.time()
    path = join(folder, LOCK_NAME)
    if holder == AUTO_HOLDER:
        return {'ok': False, 'error': 'holder_required',
                'message': 'unlock needs the exact holder that `lock --holder auto` printed, never auto.'}, 4
    current = read_lock(folder)
    if current is None:
        return {'ok': True, 'result': 'no_lock'}, 0
    if current.get('holder') != holder:
        return {'ok': False, 'error': 'forbidden', 'holder': current.get('holder')}, 4
    record = dict(current, released=True, until=now)
    _write_in_place(path, record)
    return {'ok': True, 'result': record}, 0


def cmd_lock_status(folder):
    now = time.time()
    current = read_lock(folder)
    result = {'state': lock_state(current, now)}
    if current is not None:
        result['holder'] = current.get('holder')
        result['since'] = current.get('since')
        result['until'] = current.get('until')
    return result


# -- reading relay-state.json for work -----------------------------------------------------

def _state_stamp(path):
    """When the relay wrote relay-state.json, by its own clock (relay_written_at, else server_time,
    else the file's mtime), or None when the file is missing or is not a JSON object."""
    data = _read_json_file(path)
    if not isinstance(data, dict):
        return None
    ts = data.get('relay_written_at', data.get('server_time'))
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return float(ts)
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _flagged(body):
    return isinstance(body, str) and body.startswith(FLAGGED_PREFIXES)


OPEN_OWN = ('delivered', 'acknowledged', 'claimed')


def _me(state):
    return state.get('relay_actor') or (state.get('actor') or {}).get('id')


def tree_snapshot(state):
    """{task id: status} for this seat's own open tasks and every helper below them (any depth),
    plus {own task id: [ids in its tree]} -- what `seen --all` records and `check` compares."""
    me = _me(state)
    tasks = [t for t in (state.get('tasks') or []) if isinstance(t, dict)]
    children = {}
    for t in tasks:
        children.setdefault(t.get('parent_id'), []).append(t)
    snapshot, trees = {}, {}
    for t in tasks:
        if t.get('recipient') != me or t.get('status') not in OPEN_OWN:
            continue
        ids, stack = [], [t]
        while stack:
            node = stack.pop()
            if node.get('id') in ids:
                continue
            ids.append(node.get('id'))
            snapshot[node.get('id')] = node.get('status')
            stack.extend(children.get(node.get('id'), []))
        trees[t.get('id')] = ids
    return snapshot, trees


def _answerable_helper(task, me):
    """An open question on a helper this seat sent: this seat answers it (or escalates it)."""
    return (task.get('parent_id') is not None and task.get('sender') == me
            and task.get('status') == 'waiting_decision' and task.get('failed_attempts', 0) < 2)


def _decision_key(task):
    return sha256_of([task.get('status'), task.get('decision') or ''])


def decision_snapshot(state):
    """{helper task id: hash of (status, question)} for every open question on this seat's own
    helpers -- what `seen --all` records and `check` compares."""
    me = _me(state)
    return {t.get('id'): _decision_key(t) for t in (state.get('tasks') or [])
            if isinstance(t, dict) and _answerable_helper(t, me)}


def _answered_seqs(tick_state):
    seqs = tick_state.get('answered_note_seqs')
    if not isinstance(seqs, list):
        return set()
    return set(s for s in seqs if isinstance(s, int) and not isinstance(s, bool))


def _is_question_note(note):
    return isinstance(note, dict) and isinstance(note.get('body'), str) and note['body'].startswith('QUESTION')


def compute_orchestrator(state, tick_state):
    """Returns (items, questions_for_shane) for the orchestrator role. `state` is the
    parsed relay-state.json content; `tick_state` is this tool's own small memory."""
    me = _me(state)
    last_seen = tick_state.get('seen_note_seq', 0)
    if not isinstance(last_seen, (int, float)):
        last_seen = 0
    seen_tree = tick_state.get('seen_tree')
    if not isinstance(seen_tree, dict):
        seen_tree = None
    seen_decisions = tick_state.get('seen_decisions')
    if not isinstance(seen_decisions, dict):
        seen_decisions = {}
    answered = _answered_seqs(tick_state)
    tasks = [t for t in (state.get('tasks') or []) if isinstance(t, dict)]
    notes = [n for n in (state.get('notes') or []) if isinstance(n, dict)]

    items = []
    snapshot, trees = tree_snapshot(state)
    for t in tasks:
        if t.get('recipient') != me or t.get('status') not in OPEN_OWN:
            continue
        tid = t.get('id')
        if t.get('status') == 'delivered':
            why = 'new task'
        elif seen_tree is None or tid not in seen_tree:
            why = 'not seen yet'
        else:
            moved = [x for x in trees.get(tid, []) if seen_tree.get(x) != snapshot.get(x)]
            if not moved:
                continue      # only waiting on helpers (or a reserve task): quiet until something moves
            why = 'changed since last seen: ' + ', '.join(str(x) + '=' + str(snapshot.get(x)) for x in moved[:8])
        items.append({'kind': 'task', 'task_id': tid, 'title': t.get('title'),
                      'detail': 'status=' + str(t.get('status')) + '; ' + why})
    for t in tasks:
        if _answerable_helper(t, me):
            if seen_decisions.get(t.get('id')) == _decision_key(t):
                continue      # recorded by seen --all (answered in a later turn, or escalated): quiet
            items.append({'kind': 'helper_decision', 'task_id': t.get('id'), 'title': t.get('title'),
                          'detail': t.get('decision') or ''})
    new_notes = [n for n in notes if _flagged(n.get('body')) and n.get('seq', 0) > last_seen]
    for n in new_notes:
        first_line = (n.get('body') or '').splitlines()[0][:120] if n.get('body') else ''
        items.append({'kind': 'note', 'task_id': None, 'title': first_line,
                      'detail': n.get('body'), 'note_seq': n.get('seq')})

    questions = []
    for t in tasks:
        if t.get('status') != 'waiting_decision':
            continue
        if not _answerable_helper(t, me):
            questions.append({'task_id': t.get('id'), 'from': t.get('sender'), 'question': t.get('decision') or ''})
    for n in notes:
        # seen never hides a question from Shane: only `answered` does.
        if _is_question_note(n) and n.get('seq') not in answered:
            questions.append({'note_seq': n.get('seq'), 'from': n.get('sender'), 'question': n.get('body')})
    return items, questions


def compute_chat(state):
    """A3: chat lines to this seat that are not yet marked handled (relay-state's chat.inbox, present only while
    the chat is ON). They are work for this seat, never a question for Shane and never a flagged note; reading them
    here, or the stale-state refresh above, marks nothing."""
    chat = state.get('chat')
    if not isinstance(chat, dict):
        return None
    rows = []
    for row in chat.get('inbox') or []:
        marks = row.get('marks') if isinstance(row, dict) else None
        if isinstance(marks, dict) and not marks.get('handled'):
            body = row.get('body') or ''
            rows.append({'note_seq': row.get('seq'), 'thread': row.get('thread'), 'from': row.get('from'),
                         'seen': bool(marks.get('seen')), 'first_line': body.splitlines()[0][:120] if body else ''})
    return rows


def compute_reviewer(state):
    me = state.get('relay_actor') or (state.get('actor') or {}).get('id')
    items = []
    for t in (state.get('tasks') or []):
        if t.get('status') == 'ready_for_review' and t.get('builder') != me:
            items.append({'kind': 'review', 'task_id': t.get('id'), 'title': t.get('title'),
                          'detail': 'builder=' + str(t.get('builder'))})
    return items


def _question_sort_key(q):
    if 'task_id' in q:
        return (0, str(q.get('task_id')))
    return (1, q.get('note_seq') if isinstance(q.get('note_seq'), (int, float)) else 0)


def questions_digest(questions):
    ordered = sorted(questions, key=_question_sort_key)
    return sha256_of(ordered)


def _relay_trouble(folder, reason):
    """Exit 5: never "idle" and never "work" -- the seat cannot know, so the run says why and stops."""
    return {'ok': False, 'error': 'relay_trouble', 'seat': seat_of(folder), 'reason': reason}, EXIT_RELAY


def refresh_state(folder, state_path, before, timeout, max_age):
    """One state round trip through the relay. Returns (age, None) when relay-state.json is now
    newer than `before` (the stamp read before the request) and its age is plausible, else
    (None, trouble) where trouble is _relay_trouble's (result, code)."""
    if not os.path.isdir(join(folder, 'requests')):
        return None, _relay_trouble(folder, 'no relay folder for this seat at %s (is the relay running with '
                                            'this seat?)' % folder)
    sent = time.time()
    rid = 'tick-' + uuid.uuid4().hex[:20]
    try:
        answer = relay_ask.send(folder, {'request_id': rid, 'kind': 'state'}, timeout=timeout)
    except SystemExit:
        return None, _relay_trouble(folder, 'no answer to the state request in %g seconds; the relay may not '
                                            'be running (request %s is still in requests/)' % (timeout, rid))
    except OSError as exc:
        return None, _relay_trouble(folder, 'the relay folder could not be used for the state request (%s)'
                                    % type(exc).__name__)
    if not isinstance(answer, dict) or not answer.get('ok'):
        answer = answer if isinstance(answer, dict) else {}
        return None, _relay_trouble(folder, 'the relay refused or failed the state request (error: %s; %s)'
                                    % (answer.get('error'), str(answer.get('message') or '')[:160]))
    after = _state_stamp(state_path)
    done = time.time()
    if after is None:
        return None, _relay_trouble(folder, 'relay-state.json is missing or unreadable although the relay '
                                            'answered the state request')
    if before is not None and not after > before:
        return None, _relay_trouble(folder, 'relay-state.json is no newer after the refresh (written at %.3f, '
                                            'before it %.3f): the relay did not rewrite it' % (after, before))
    age = done - after
    # Written during the round trip, so on one clock its age is between 0 and the round trip's
    # length. Outside [-max_age, max_age + round trip] the relay's clock and this one differ.
    if age < -max_age or age > max_age + (done - sent):
        return None, _relay_trouble(folder, 'relay-state.json is %.0f seconds old right after a refresh, outside '
                                            '[-%g, %g]: the relay\'s clock and this machine\'s clock differ'
                                    % (age, max_age, max_age + (done - sent)))
    return age, None


def cmd_check(folder, role, holder, timeout, max_age):
    if role not in ('orchestrator', 'reviewer'):
        raise SystemExit('--role must be orchestrator or reviewer.')
    now = time.time()
    current = read_lock(folder)
    if lock_state(current, now) == 'held' and (holder is None or current.get('holder') != holder):
        return {'ok': False, 'error': 'locked', 'holder': current.get('holder'),
                'until': current.get('until')}, EXIT_BUSY

    state_path = join(folder, STATE_NAME)
    before = _state_stamp(state_path)
    age = None if before is None else now - before
    if age is None or age < 0 or age > max_age:          # a negative age (relay clock ahead) is stale
        age, trouble = refresh_state(folder, state_path, before, timeout, max_age)
        if trouble is not None:
            return trouble

    state = _read_json_file(state_path)
    if not isinstance(state, dict):
        return _relay_trouble(folder, 'relay-state.json became unreadable while it was being read')
    tick_state = _read_json_file(join(folder, TICK_STATE_NAME))
    if not isinstance(tick_state, dict):
        tick_state = {}

    if role == 'orchestrator':
        items, questions = compute_orchestrator(state, tick_state)
        digest = questions_digest(questions)
        new_questions = bool(questions) and digest != tick_state.get('notified_digest')
    else:
        items = compute_reviewer(state)
        questions = []
        digest = questions_digest(questions)
        new_questions = False

    result = {'seat': seat_of(folder), 'role': role, 'work': bool(items), 'items': items,
              'questions_for_shane': questions, 'questions_digest': digest,
              'new_questions': new_questions, 'relay_state_age_seconds': age}
    chat = compute_chat(state) if role == 'orchestrator' else None
    if chat is not None:
        result['chat'] = chat                              # A3: its own heading, only while the chat is ON
    code = EXIT_WORK if (items or new_questions or chat) else EXIT_IDLE
    return result, code


def cmd_questions_text(folder):
    state = _read_json_file(join(folder, STATE_NAME))
    if not isinstance(state, dict):
        return '(no relay-state.json yet; run check first)'
    tick_state = _read_json_file(join(folder, TICK_STATE_NAME))
    if not isinstance(tick_state, dict):
        tick_state = {}
    _, questions = compute_orchestrator(state, tick_state)
    ordered = sorted(questions, key=_question_sort_key)
    if not ordered:
        return '(no open questions)'
    lines = []
    for i, q in enumerate(ordered, 1):
        if 'task_id' in q:
            lines.append('%d. [task %s] from %s: %s' % (i, q.get('task_id'), q.get('from'), q.get('question')))
        else:
            lines.append('%d. [note #%s] from %s: %s' % (i, q.get('note_seq'), q.get('from'), q.get('question')))
    return '\n'.join(lines)


def cmd_notified(folder, digest):
    path = join(folder, TICK_STATE_NAME)
    current = _read_json_file(path)
    if not isinstance(current, dict):
        current = {}
    current['notified_digest'] = digest
    _write_in_place(path, current)


def cmd_seen(folder, note_seq=None, everything=False):
    """Record what this seat has dealt with. --note-seq N: flagged Lounge notes up to N.
    --all: every flagged note in relay-state.json and the current status of every own open task
    and the helpers under it, so only a later change wakes the seat again."""
    path = join(folder, TICK_STATE_NAME)
    current = _read_json_file(path)
    if not isinstance(current, dict):
        current = {}
    previous = current.get('seen_note_seq', 0)
    if not isinstance(previous, (int, float)):
        previous = 0
    if everything:
        state = _read_json_file(join(folder, STATE_NAME))
        if not isinstance(state, dict):
            raise SystemExit('relay-state.json is missing or unreadable; run check first.')
        seqs = [n.get('seq') for n in (state.get('notes') or [])
                if isinstance(n, dict) and _flagged(n.get('body')) and isinstance(n.get('seq'), (int, float))]
        note_seq = max(seqs) if seqs else previous
        current['seen_tree'] = tree_snapshot(state)[0]
        current['seen_decisions'] = decision_snapshot(state)
    current['seen_note_seq'] = max(previous, note_seq)
    _write_in_place(path, current)


def cmd_answered(folder, note_seq):
    """Record that Shane's answer to the QUESTION note `note_seq` was passed on, so it leaves
    questions_for_shane. When Shane was already notified of the list as it stood, the notification
    is carried forward to the shorter list: a question fewer is not a new batch for his phone."""
    path = join(folder, TICK_STATE_NAME)
    current = _read_json_file(path)
    if not isinstance(current, dict):
        current = {}
    state = _read_json_file(join(folder, STATE_NAME))
    if not isinstance(state, dict):
        state = None
    before = questions_digest(compute_orchestrator(state, current)[1]) if state is not None else None
    answered = _answered_seqs(current)
    answered.add(note_seq)
    found = False
    if state is not None:
        notes = [n for n in (state.get('notes') or []) if isinstance(n, dict)]
        found = any(_is_question_note(n) and n.get('seq') == note_seq for n in notes)
        window = [n.get('seq') for n in notes if isinstance(n.get('seq'), int)]
        if window:   # a note below the room's latest-100 window never comes back: forget it
            answered = set(s for s in answered if s >= min(window))
    current['answered_note_seqs'] = sorted(answered)
    result = {'ok': True, 'answered_note_seqs': current['answered_note_seqs'], 'note_found': found}
    if state is not None:
        after = questions_digest(compute_orchestrator(state, current)[1])
        if before is not None and current.get('notified_digest') == before:
            current['notified_digest'] = after
        result['questions_digest'] = after
    if not found:
        result['warning'] = ('note %s is not a QUESTION note in relay-state.json; recorded anyway, check the '
                             'number with `questions`' % note_seq)
    _write_in_place(path, current)
    return result


def build_parser():
    parser = argparse.ArgumentParser(prog='tick.py', description=__doc__)
    parser.add_argument('folder')
    sub = parser.add_subparsers(dest='subcommand', required=True)

    p_check = sub.add_parser('check')
    p_check.add_argument('--role', required=True, choices=['orchestrator', 'reviewer'])
    p_check.add_argument('--holder', default=None)
    p_check.add_argument('--timeout', type=float, default=90.0)
    p_check.add_argument('--max-age', type=float, default=120.0)

    p_lock = sub.add_parser('lock')
    p_lock.add_argument('--holder', required=True)
    p_lock.add_argument('--minutes', type=float, default=45.0)

    p_unlock = sub.add_parser('unlock')
    p_unlock.add_argument('--holder', required=True)

    sub.add_parser('lock-status')
    sub.add_parser('questions')

    p_notified = sub.add_parser('notified')
    p_notified.add_argument('--digest', required=True)

    p_seen = sub.add_parser('seen')
    which = p_seen.add_mutually_exclusive_group(required=True)
    which.add_argument('--note-seq', type=int)
    which.add_argument('--all', action='store_true', dest='everything')

    p_answered = sub.add_parser('answered')
    p_answered.add_argument('--note-seq', type=int, required=True)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    folder = args.folder
    if args.subcommand == 'check':
        result, code = cmd_check(folder, args.role, args.holder, args.timeout, args.max_age)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return code
    if args.subcommand == 'lock':
        result, code = cmd_lock(folder, args.holder, args.minutes)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return code
    if args.subcommand == 'unlock':
        result, code = cmd_unlock(folder, args.holder)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return code
    if args.subcommand == 'lock-status':
        print(json.dumps(cmd_lock_status(folder), indent=2, ensure_ascii=False))
        return 0
    if args.subcommand == 'questions':
        print(cmd_questions_text(folder))
        return 0
    if args.subcommand == 'notified':
        cmd_notified(folder, args.digest)
        return 0
    if args.subcommand == 'seen':
        cmd_seen(folder, args.note_seq, args.everything)
        return 0
    if args.subcommand == 'answered':
        print(json.dumps(cmd_answered(folder, args.note_seq), indent=2, ensure_ascii=False))
        return 0
    raise SystemExit(2)


if __name__ == '__main__':
    sys.exit(main())
