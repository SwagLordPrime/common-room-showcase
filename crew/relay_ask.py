"""Helper for a chat that can only reach the room through files (a Cowork chat's shell).

Writes one request into this seat's relay folder the safe way (temporary name, then rename),
waits for the relay on Shane's PC to answer, and prints the answer. Python 3.8+, stdlib only,
so it runs inside the Cowork Linux shell as well as on Windows. It never talks to the room
itself and never reads a credential: the relay does the work as the folder's own seat.

    python3 relay_ask.py <relay-folder> '<request json>'        send one request, print the answer
    python3 relay_ask.py <relay-folder> state                    ask for a fresh relay-state.json, print a summary
    python3 relay_ask.py <relay-folder> queue                    print my tasks and the review queue from relay-state.json

<relay-folder> is .../tree/.runtime/handoffs/relay/<seat>. Give "request_id": "auto" and a fresh,
never-reused id is made and printed; to repeat a request after a timeout, send it again with that
printed id. Exit 0 on ok, 1 on a refusal, 2 on usage or timeout.
"""
import hashlib, json, os, re, sys, time, uuid

# Starts and ends with a letter or digit: the relay trims a trailing . or - from file names.
REQUEST_ID = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,62}[A-Za-z0-9])?')


def fail(message, code=2):
    sys.stderr.write(message + '\n')
    raise SystemExit(code)


def unique_request_id(seat):
    """F9: turn the literal request_id "auto" into a fresh, never-reused id, so a
    replacement chat (or a stray retry) can never collide with an id an earlier
    chat on this same seat already used and get back that older, possibly stale
    reply. Format: <seat>-<UTC yyyymmddHHMMSS>-<6 hex>, trimmed to fit
    REQUEST_ID's 64-character, alnum-first-and-last rule."""
    suffix = time.strftime('%Y%m%d%H%M%S', time.gmtime()) + '-' + uuid.uuid4().hex[:6]
    seat_part = re.sub(r'[^A-Za-z0-9_.-]', '-', seat or '').strip('-_.')
    budget = 64 - 1 - len(suffix)
    seat_part = seat_part[:budget].strip('-_.') or 'seat'
    candidate = seat_part + '-' + suffix
    if not REQUEST_ID.fullmatch(candidate):
        candidate = 'seat-' + suffix  # defensive fallback; construction above should always match
    return candidate


def send(folder, request, timeout=90.0):
    if not isinstance(request, dict) or 'request_id' not in request:
        fail('A request is a JSON object with a request_id.')
    rid = request['request_id']
    if not isinstance(rid, str) or not REQUEST_ID.fullmatch(rid):
        fail('request_id: 1-64 letters, digits, _ . or -, starting and ending with a letter or digit (no colon).')
    requests = os.path.join(folder, 'requests')
    responses = os.path.join(folder, 'responses')
    archive = os.path.join(folder, 'archive')
    if not os.path.isdir(requests):
        fail('No relay folder at ' + folder + ' (is the relay running with this seat?).')
    answers = (os.path.join(responses, 'resp-' + rid + '.json'),
               os.path.join(responses, 'resp-invalid-' + rid + '.json'))
    # An older answer for this id must never be mistaken for the new one: set it aside first.
    for old in answers:
        if os.path.exists(old):
            os.replace(old, old + '.prev')
    data = json.dumps(request, ensure_ascii=False).encode('utf-8')
    digest = hashlib.sha256(data).hexdigest()
    target = os.path.join(requests, rid + '.json')
    tmp = os.path.join(requests, rid + '.' + uuid.uuid4().hex[:8] + '.tmp')
    with open(tmp, 'wb') as handle:
        handle.write(data)
    os.replace(tmp, target)
    started = time.time()
    # The relay never moves requests; it answers each file once and names the file and its
    # sha256 in the answer, so a match on both is exactly this request.
    while time.time() - started < timeout:
        for answer in answers:
            try:
                with open(answer, encoding='utf-8') as handle:
                    body = json.load(handle)
            except (OSError, ValueError):
                continue
            if body.get('request_sha256') == digest:
                tidy(target, archive, rid)
                return body
        time.sleep(1.0)
    fail('No answer in %d seconds. Your request is still in requests/; the relay may not be running.' % timeout)


def tidy(path, archive, rid):
    """The chat keeps its own folder tidy: move the answered request into archive/."""
    try:
        if os.path.isdir(archive):
            os.replace(path, os.path.join(archive, rid + '.' + time.strftime('%Y%m%dT%H%M%S') + '.' + uuid.uuid4().hex[:6] + '.json'))
    except OSError:
        pass


def summary(state, me):
    rows = []
    for t in state.get('tasks', []):
        mine = t.get('recipient') == me and t.get('status') not in ('done', 'cancelled')
        review = t.get('status') == 'ready_for_review' and t.get('builder') != me
        if mine or review:
            rows.append({'task_id': t['id'], 'status': t['status'], 'recipient': t['recipient'],
                         'builder': t.get('builder'), 'fence': t.get('fence'), 'title': t.get('title'),
                         'queue': 'review' if review else 'mine'})
    return {'actor': state.get('actor'), 'server_time': state.get('server_time'), 'tasks': rows}


def main(argv):
    if len(argv) != 2:
        fail(__doc__.strip())
    folder, what = argv
    me = os.path.basename(os.path.normpath(folder))
    if what == 'state':
        answer = send(folder, {'request_id': 'state-' + uuid.uuid4().hex[:12], 'kind': 'state'})
        if not answer.get('ok'):
            print(json.dumps(answer, indent=2))
            return 1
        what = 'queue'
    if what == 'queue':
        with open(os.path.join(folder, 'relay-state.json'), encoding='utf-8') as handle:
            print(json.dumps(summary(json.load(handle), me), indent=2, ensure_ascii=False))
        return 0
    try:
        request = json.loads(what)
    except ValueError:
        fail('The request must be JSON, or the word state or queue.')
    if isinstance(request, dict) and request.get('request_id') == 'auto':
        request = dict(request)
        request['request_id'] = unique_request_id(me)
        sys.stderr.write('request_id: ' + request['request_id'] + '\n')
    answer = send(folder, request)
    print(json.dumps(answer, indent=2, ensure_ascii=False))
    return 0 if answer.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
