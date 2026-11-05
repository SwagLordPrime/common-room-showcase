"""Needs Shane: one list of everything in the room that is waiting on the owner (plan turnons-2026-10-03, job panels).

needs_shane(snapshot) takes the body of /api/state (what the board and the crew client already read) and returns a list of
{id, kind, since, summary, action}. It is a pure function of that snapshot: no network, no clock (the 30-minute test
uses the snapshot's own server_time), no file reads, and it never puts a key, a credential path or file content in any
field. The board shows the list first on the page, `crew needs` prints it, and the notify job pushes new ids.

The kinds, in the order they are listed:

  stop      The room is STOPPED (stop_all). Shane resumes it.
  stopped   A task stopped at two strikes. Only Shane can reinstate it or refund a strike.
  decision  A task waiting on a decision whose decider is the keeper: a root task, or one the keeper sent.
  question  A Lounge note starting QUESTION that names Shane (or keeper) in its first line, not yet answered.
  seat      A seat with no active room key that still has open work (delivered, acknowledged or claimed).
  slow      A decision on a helper claude-room sent, or a QUESTION note for claude-room, unanswered for over 30
            minutes, so Shane sees when the orchestrator is slow.

A QUESTION note counts as answered when a later note or task resolution (resolve_decision or reinstate) from the
keeper or claude-room cites it as #<seq> (the house habit: "RULING ... answering #1049"). Only visible snapshot
history and notes count; notes outside the last 100 the room returns are not seen.

Ids are stable while a condition holds and new for a new episode: a task's ids carry its fence, which moves every time
it leaves or re-enters a decision, and a note's id is its sequence number.
"""
import re

ORCHESTRATOR = 'claude-room'
SLOW_SECONDS = 30 * 60
EXCERPT = 160
KIND_ORDER = ('stop', 'stopped', 'decision', 'question', 'seat', 'slow')
OPEN = ('delivered', 'acknowledged', 'claimed')
CLOSED = ('done', 'cancelled')
NAMES_SHANE = re.compile(r'\b(shane|keeper)\b', re.I)
REVIEW_RETURN = 'Review requires an owner decision'
# A word that looks like a path into secret places, or a long random token (a room key is 43 url-safe characters).
SECRET_WORD = re.compile(r'(?i)credential|\.runtime|secrets?[\\/]|_private|\.env\b|keeper-store|vault[\\/]')
TOKEN = re.compile(r'[A-Za-z0-9_\-]{24,}')
HEX = re.compile(r'[0-9a-f]+')               # a commit or sha256: shown as written
WORDS = re.compile(r'[a-z]+(?:[-_][a-z]+)*')  # lowercase words joined by - or _ (core-cutover-fix): shown


def _token(match):
    run = match.group(0)
    return run if HEX.fullmatch(run) or WORDS.fullmatch(run) else '[hidden]'


def scrub(text, limit=EXCERPT):
    """One line of room text, safe to show: secret-looking paths and key-like tokens hidden, then cut to `limit`."""
    words = []
    for word in str(text or '').split():
        words.append('[path hidden]' if SECRET_WORD.search(word) else TOKEN.sub(_token, word))
    line = ' '.join(words)
    return line if len(line) <= limit else line[:limit - 3].rstrip() + '...'


def _title(task):
    return scrub(task.get('title'), 80)


def _owner(snapshot):
    owners = {a.get('owner') for a in snapshot.get('authorities') or [] if a.get('owner')}
    return sorted(owners)[0] if len(owners) == 1 else 'keeper'


def _last_event(snapshot):
    last = {}
    for event in snapshot.get('history') or []:
        tid = event.get('task_id')
        if tid is not None and event.get('timestamp') is not None:
            last[tid] = max(last.get(tid, event['timestamp']), event['timestamp'])
    return last


def _cited(notes, history, note, by):
    mark = re.compile(r'#' + str(note['seq']) + r'(?!\d)')
    return any(n['seq'] > note['seq'] and n.get('sender') in by and mark.search(n.get('body') or '') for n in notes) or any(
        e['seq'] > note['seq'] and e.get('kind') in ('task_resolve_decision', 'task_reinstate')
        and e.get('actor') in by and mark.search(e.get('detail') or '') for e in history)


def _item(id, kind, since, summary, action):
    return {'id': id, 'kind': kind, 'since': since, 'summary': summary, 'action': action}


def room_needs(snapshot):
    """S0 F4: the list the room worked out for this seat (state['needs'], computed before history details are
    clipped), or needs_shane() on the snapshot when the room sent none."""
    items = (snapshot or {}).get('needs')
    return items if isinstance(items, list) else needs_shane(snapshot)


def needs_shane(snapshot, addressed=None):
    """Everything waiting on Shane in this snapshot of /api/state, most urgent kind first, oldest first within a kind.

    `addressed` (A1, 2026-10-24) maps a note's seq to the seat it was delivered to as an addressed question
    (`QUESTION FOR <seat>:`); the room passes it from its own events. Such a question waits on that seat, not on
    claude-room, so it never becomes a "slow" item unless it is addressed to claude-room. Without the map (an older
    room) every note is read exactly as before."""
    snapshot = snapshot or {}
    addressed = addressed or {}
    now = snapshot.get('server_time')
    owner = _owner(snapshot)
    last = _last_event(snapshot)
    items = []

    stop = snapshot.get('stop') or {}
    if stop.get('active'):
        at = stop.get('at')
        items.append(_item('stop:' + ('%.3f' % at if isinstance(at, (int, float)) else 'on'), 'stop', at,
                           'The room is STOPPED (by ' + scrub(stop.get('by'), 40) + '): ' + scrub(stop.get('reason')) +
                           '. No AI can claim or finish work until you resume it.',
                           'When it is safe: crew-shane resume-all  (or the Resume everything button on this board)'))

    tasks = snapshot.get('tasks') or []
    for t in tasks:
        tid, fence, status = t['id'], t.get('fence', 0), t.get('status')
        since = last.get(tid)
        if t.get('failed_attempts', 0) >= 2 and status not in CLOSED:
            items.append(_item('stopped:' + tid + ':f' + str(fence), 'stopped', since,
                               'Stopped at two strikes: "' + _title(t) + '" (' + scrub(t.get('recipient'), 40) +
                               '). Last: ' + scrub(t.get('decision') or t.get('failure_note') or t.get('review_note')),
                               'crew-shane reinstate ' + tid + ' --reason "why it may try again"  (or give one strike '
                               'back: crew-shane decide ' + tid + ' --refund --response "why")'))
            continue
        if status != 'waiting_decision':
            continue
        helper = t.get('parent_id') is not None
        question = str(t.get('decision') or '')
        if question.startswith(REVIEW_RETURN):
            what = 'Review returned "' + _title(t) + '" (' + scrub(t.get('recipient'), 40) + '): ' + \
                   scrub(question[len(REVIEW_RETURN):].lstrip(': '))
        else:
            what = scrub(t.get('recipient'), 40) + ' asks on "' + _title(t) + '": ' + scrub(question)
        answer = 'crew-shane decide ' + tid + ' --response "your answer"'
        if not helper or t.get('sender') == owner:
            items.append(_item('decision:' + tid + ':f' + str(fence), 'decision', since, what, answer))
        elif t.get('sender') == ORCHESTRATOR and now is not None and since is not None and now - since > SLOW_SECONDS:
            items.append(_item('slow:' + tid + ':f' + str(fence), 'slow', since,
                               'Waiting over 30 minutes for ' + ORCHESTRATOR + '. ' + what,
                               'Ask ' + ORCHESTRATOR + ' in its chat to answer task ' + tid + ', or answer it '
                               'yourself: ' + answer))

    notes = sorted(snapshot.get('notes') or [], key=lambda n: n['seq'])
    answerers = {owner, ORCHESTRATOR}
    for n in notes:
        body = n.get('body') or ''
        if not body.startswith('QUESTION') or _cited(notes, snapshot.get('history') or [], n, answerers):
            continue
        seq, scope = n['seq'], n.get('authority_id') or 'orchestra'
        first = (body.splitlines() or [body])[0]
        said = 'QUESTION from ' + scrub(n.get('sender'), 40) + ' (#' + str(seq) + '): ' + scrub(body[len('QUESTION'):].lstrip(' :'))
        reply = 'crew-shane note ' + scope + ' "answering #' + str(seq) + ': your answer"'
        if NAMES_SHANE.search(first) and n.get('sender') != owner:
            items.append(_item('question:' + str(seq), 'question', n.get('timestamp'), said,
                               'Answer in the ' + ORCHESTRATOR + ' chat (it posts your answer citing #' + str(seq) +
                               '), or yourself: ' + reply))
        elif n.get('sender') not in answerers and now is not None and n.get('timestamp') is not None \
                and now - n['timestamp'] > SLOW_SECONDS and addressed.get(seq, ORCHESTRATOR) == ORCHESTRATOR:
            items.append(_item('slow:note:' + str(seq), 'slow', n.get('timestamp'),
                               'Waiting over 30 minutes for ' + ORCHESTRATOR + '. ' + said,
                               'Ask ' + ORCHESTRATOR + ' in its chat to answer #' + str(seq) + ', or answer it yourself: ' + reply))

    for p in snapshot.get('participants') or []:
        pid = p.get('id')
        if not pid or pid == owner or p.get('role') == 'owner' or p.get('legacy'):
            continue
        active = p['key_active'] if 'key_active' in p else p.get('contact') != 'No active key'
        if active:
            continue
        work = [t for t in tasks if t.get('recipient') == pid and t.get('status') in OPEN]
        if not work:
            continue
        since = max((last[t['id']] for t in work if t['id'] in last), default=None)
        items.append(_item('seat:' + pid + ':no-key', 'seat', since,
                           scrub(p.get('label') or pid, 60) + ' has no active room key but holds ' + str(len(work)) +
                           ' open task(s), so that work cannot move.',
                           'Restart the crew so every seat gets a fresh key (Stop Common Room Crew.cmd, then Start Common '
                           'Room Crew.cmd); if the seat was revoked on purpose, withdraw its work: crew-shane cancel ' +
                           work[0]['id'] + ' --reason "why"'))

    rank = {k: i for i, k in enumerate(KIND_ORDER)}
    return sorted(items, key=lambda i: (rank[i['kind']], i['since'] is None, i['since'] or 0, i['id']))
