"""Gate E2E3 of BUILD_PLAN_COMMON_ROOM_AUTONOMY_2026-09-24.md, in one process: the owner approves
a plan once, ON THE BOARD over HTTP; everything after that happens without the owner.

Seats (fictional, under .checks):
  keeper    the owner; signs in to the board and approves the plan (the ONLY owner step)
  e3-orch   a Cowork-style orchestrator; acts only through its relay folder
  e3-agent  a claude-code-like worker seat; worked only by common_room.agent_worker's Runner,
            whose "agent" is tests/fake_agent.py (never Claude Code or Codex)
  e3-judge  a reviewer; acts only through its relay folder

Flow: PLAN.json (crew-plan/1: one workstream for e3-orch plus one reserve task) in a plans folder
-> the room starts with plans_root there -> keeper signs in, GET /api/plans (the reserve envelope
states the 60-job cap), POST /api/plan-approve with the listed sha256 and the typed plan id ->
tick.py check for e3-orch: work (exit 0) -> e3-orch acks and claims its workstream and delegates a
helper to e3-agent -> tick.py check for e3-orch: still work while the workstream waits -> the
Runner acks, claims, writes TASK.md, runs the fake agent (its "AGENT: COPY_RESULT <draft>" line
makes it write RESULT.md from a fixture draft whose citations point at a real fixture file) and
submits -> tick.py check for e3-judge: work (exit 0) -> e3-judge fetches the report with view_text
through its relay and pipes that relay answer to crew/cite_check.py --relay-answer - (exit 0; a
tampered copy of the same answer exits 1, so the check is not vacuous) -> e3-judge accepts ->
e3-orch finishes the workstream -> e3-judge accepts it -> tick.py check for e3-judge: idle (exit 3).

tick.py and cite_check.py run as subprocesses under python3.10 when it is installed (their Python
3.8-grammar, Cowork-VM form), else python3.12; while tick.py waits for its relay round trip a
background thread pumps the relay in this process.

Owner steps are the room events by keeper after setup; they must be exactly intake + deliver per
plan task, all from the board approval. No token is printed or written by this script.

Run from the package root:  python3.12 -B scripts/autonomy_e2e.py
Prints one JSON summary with "result": "PASS" and exits 0, or "result": "FAIL" with the failing step
and exits 1.
"""
import http.client, json, shutil, subprocess, sys, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.helpers import case_dir                       # noqa: E402  (loopback-only guard)
from common_room import agent_worker as aw               # noqa: E402
from common_room.activation import start_room            # noqa: E402
from common_room.crew_client import CrewClient           # noqa: E402
from common_room.relay import Relay                      # noqa: E402
from common_room.store import ROOT_JOB_CAP               # noqa: E402

TICK = ROOT / 'crew' / 'tick.py'
CITE = ROOT / 'crew' / 'cite_check.py'
FAKE_AGENT = ROOT / 'tests' / 'fake_agent.py'
TOOL_PY = shutil.which('python3.10') or shutil.which('python3.12') or sys.executable
EXPECTED_CAP = 60           # the number the build plan and AUTONOMY guide promise Shane

SCOPE, PLAN_ID = 'e3-band', 'e3-plan'
ORCH, AGENT, JUDGE = 'e3-orch', 'e3-agent', 'e3-judge'
ENROLLMENT = {'version': 1, 'actors': [
    {'id': ORCH, 'role': 'worker', 'label': 'Fictional orchestrator', 'session': 'relay'},
    {'id': AGENT, 'role': 'worker', 'label': 'Fictional headless agent seat', 'session': 'agent_worker + fake agent'},
    {'id': JUDGE, 'role': 'reviewer', 'label': 'Fictional reviewer', 'session': 'relay'}],
    'authorities': [{'id': SCOPE, 'members': ['keeper', ORCH, AGENT, JUDGE],
                     'scope': 'Fictional autonomy scope for the E2E3 run.'}]}

# The estate fixture the report cites (resolved by cite_check under --root, like D:\Work Area).
NOTES_REL = 'Artifacts/e3-fixture/notes.md'
NOTES = ('# Fixture notes (fictional)\n'
         'alpha: the room keeps one event log per scope.\n'
         'beta: a reviewer never accepts its own work.\n'
         'gamma: every approved task is capped at sixty jobs.\n')
# What the fake agent writes as RESULT.md (fake_agent.py's COPY_RESULT behaviour copies this file).
DRAFT = ('# Summary of the fixture notes\n\n'
         '- %s:3 says `a reviewer never accepts its own work` (VERIFIED IN LOCAL SOURCE).\n'
         '- %s#L2-L4 lists three rules; I counted them (MEASURED).\n' % (NOTES_REL, NOTES_REL))
TAMPER = (NOTES_REL + ':3 says', NOTES_REL + ':30 says')     # the control: a line the file does not have
AGENT_LINE = 'AGENT: COPY_RESULT '    # the helper body's line fake_agent.py acts on (+ the draft's path)


class Failed(Exception):
    pass


def check(condition, message):
    if not condition:
        raise Failed(message)


class Seat:
    """A chat that can only write files: one relay folder, one request at a time."""
    def __init__(self, relay, root, actor):
        self.relay, self.folder, self.actor, self.n = relay, root / 'relay' / actor, actor, 0
        self.last_raw = None

    def ask(self, kind, action=None, payload=None):
        self.n += 1
        rid = '%s-%d' % (self.actor, self.n)
        body = {'request_id': rid, 'kind': kind}
        if kind == 'command':
            body.update(action=action, payload=payload)
        elif kind == 'handoff':
            body.update(operation=action, payload=payload)
        tmp = self.folder / 'requests' / (rid + '.json.tmp')
        tmp.write_text(json.dumps(body), encoding='utf-8')
        tmp.rename(self.folder / 'requests' / (rid + '.json'))
        self.relay.process_once()
        self.last_raw = (self.folder / 'responses' / ('resp-' + rid + '.json')).read_bytes()
        answer = json.loads(self.last_raw.decode('utf-8'))
        check(answer.get('ok'), '%s %s %s refused: %s %s' % (self.actor, kind, action, answer.get('error'),
                                                             answer.get('message')))
        return answer['result']

    def state(self):
        self.ask('state')
        return json.loads((self.folder / 'relay-state.json').read_text(encoding='utf-8'))


class Board:
    """The owner's browser tab: exact Host and Origin, one session cookie, JSON bodies."""
    def __init__(self, room):
        self.origin = room.server.origin
        self.port = room.server.server_address[1]
        self.cookie = None

    def request(self, method, path, payload=None):
        headers = {'Host': self.origin[len('http://'):], 'Origin': self.origin}
        if self.cookie:
            headers['Cookie'] = self.cookie
        body = None
        if payload is not None:
            body = json.dumps(payload).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=60)
        try:
            conn.request(method, path, body=body, headers=headers)
            res = conn.getresponse()
            data = res.read()
            return res.status, (json.loads(data) if data else None), res.getheader('Set-Cookie')
        finally:
            conn.close()

    def login(self, credential_path):
        token = json.loads(Path(credential_path).read_text(encoding='utf-8'))['token']   # read, never printed
        status, body, cookie = self.request('POST', '/api/login', {'token': token})
        check(status == 200 and cookie, 'board login refused with status %s' % status)
        self.cookie = cookie.split(';')[0]
        return body['actor']


def task_in(state, task_id):
    return CrewClient.task_in(state, task_id)


def main():
    started = time.time()
    summary = {'result': 'FAIL', 'step': 'setup', 'tool_python': Path(TOOL_PY).name, 'tick': []}
    try:
        return _run(summary, started)
    except Failed as exc:
        summary['error'] = str(exc)
    except Exception as exc:   # an unexpected crash is still a failure at a named step
        summary['error'] = '%s: %s' % (type(exc).__name__, exc)
    summary['seconds'] = round(time.time() - started, 1)
    print(json.dumps(summary, indent=2))
    return 1


def _run(summary, started):
    def step(name):
        summary['step'] = name

    root = case_dir('autonomy-e2e')
    plans = case_dir('autonomy-e2e-plans')
    estate = case_dir('autonomy-e2e-estate')
    drafts = case_dir('autonomy-e2e-drafts')
    runs = case_dir('autonomy-e2e-runs') / 'runs'
    notes = estate / NOTES_REL
    notes.parent.mkdir(parents=True)
    notes.write_text(NOTES, encoding='utf-8')
    draft = drafts / 'report-draft.md'
    draft.write_text(DRAFT, encoding='utf-8')
    (plans / PLAN_ID).mkdir()
    (plans / PLAN_ID / 'PLAN.json').write_text(json.dumps({
        'protocol': 'crew-plan/1', 'plan_id': PLAN_ID, 'title': 'Autonomy end-to-end plan', 'scope': SCOPE,
        'tasks': [
            {'key': 'ws-1', 'to': ORCH, 'title': 'Summarize the fixture notes',
             'body': 'Delegate the summary to the headless agent seat, then report.\nINPUT: ' + NOTES_REL,
             'acceptance': 'The helper is accepted with a clean citation check; the report links it.'},
            {'key': 'reserve-1', 'to': ORCH, 'title': 'Reserve for follow-up helpers',
             'body': 'Envelope for follow-up work the orchestrator may add without asking again.',
             'acceptance': 'Each helper under it is reviewed.', 'reserve': True}]}, indent=2), encoding='utf-8')

    room = start_room(root, enrollment=ENROLLMENT, selected=[ORCH, AGENT, JUDGE], plans_root=plans)
    try:
        keeper = room.store.people['keeper']

        def events():
            return room.store._snapshot(keeper)[0]
        setup_seq = max([e['seq'] for e in events()] or [0])
        tokens = [json.loads((root / (a + '-credential.json')).read_text(encoding='utf-8'))['token']
                  for a in ('keeper', ORCH, AGENT, JUDGE)]

        relay = Relay(root, [ORCH, JUDGE])
        relay._log = lambda *a, **k: None          # keep the relay's own log lines out of the summary
        relay_lock = threading.Lock()
        names = {}                                 # task id -> readable name, for the summary
        orch, judge = Seat(relay, root, ORCH), Seat(relay, root, JUDGE)
        runner_log = []
        runner = aw.Runner(CrewClient(root / (AGENT + '-credential.json')),
                           [sys.executable, str(FAKE_AGENT), '{task_file}', '{run_dir}'], estate, runs,
                           timeout=120, max_runs_per_hour=12, max_runs_per_day=48, slice_seconds=0.05,
                           log=runner_log.append)

        def tick(actor, role, when, expect):
            """crew/tick.py check as a subprocess (the scheduled-task form); the relay is pumped
            here meanwhile. --max-age 0 forces one real relay round trip."""
            stop = threading.Event()

            def pump():
                while not stop.is_set():
                    with relay_lock:
                        relay.process_once()
                    stop.wait(0.1)
            thread = threading.Thread(target=pump, daemon=True)
            thread.start()
            try:
                proc = subprocess.run([TOOL_PY, '-B', str(TICK), str(root / 'relay' / actor), 'check', '--role', role,
                                       '--timeout', '30', '--max-age', '0'], capture_output=True, text=True, timeout=90)
            finally:
                stop.set()
                thread.join(10)
            out = json.loads(proc.stdout) if proc.stdout.strip().startswith('{') else {}
            record = {'seat': actor, 'role': role, 'when': when, 'exit': proc.returncode,
                      'items': ['%s %s' % (i.get('kind'), names.get(i.get('task_id'), i.get('task_id')))
                                for i in out.get('items', [])]}
            summary['tick'].append(record)
            check(proc.returncode == expect, 'tick.py check %s (%s) %s: exit %s, expected %s; stderr: %s'
                  % (actor, role, when, proc.returncode, expect, proc.stderr.strip()[-300:]))
            return record

        # 0. Before anything: the reviewer has nothing to do.
        step('tick reviewer idle before the plan')
        tick(JUDGE, 'reviewer', 'before the plan', 3)

        # 1. The one owner step: approve the plan on the board, over HTTP, signed in as keeper.
        step('owner signs in to the board')
        board = Board(room)
        check(board.login(root / 'keeper-credential.json') == 'keeper', 'the board did not sign keeper in')
        step('owner lists plans on the board')
        status, listing, _ = board.request('GET', '/api/plans')
        check(status == 200 and listing.get('configured') and listing.get('root_exists'),
              'GET /api/plans: %s %s' % (status, listing))
        entry = next((p for p in listing['plans'] if p['id'] == PLAN_ID + '/PLAN.json'), None)
        check(entry is not None and 'error' not in entry, 'the plan is not listed cleanly: %s' % entry)
        check(entry['problems'] == [] and entry['conflicts'] == [] and not entry['approved'] and entry['in_room'] == 0,
              'the listing does not show a fresh, approvable plan: %s' % entry)
        check([t['key'] for t in entry['tasks'] if t['reserve']] == ['reserve-1'], 'reserve flag not listed')
        step('reserve envelope states the 60-job cap')
        reserve_lines = [line for line in entry['envelope'] if line.startswith('RESERVE [reserve-1]')]
        cap_text = 'at most %d jobs in total' % EXPECTED_CAP
        check(ROOT_JOB_CAP == EXPECTED_CAP, 'store.ROOT_JOB_CAP is %s, not %d' % (ROOT_JOB_CAP, EXPECTED_CAP))
        check(len(reserve_lines) == 1 and cap_text in reserve_lines[0],
              'the reserve envelope does not state the cap: %s' % entry['envelope'])
        check(any(cap_text in line for line in entry['envelope'] if not line.startswith('RESERVE')),
              'the plan-wide envelope line does not state the cap: %s' % entry['envelope'])
        summary['plan'] = {'id': entry['id'], 'sha256': entry['sha256'], 'envelope': entry['envelope']}

        step('owner approves the plan on the board')
        status, approved, _ = board.request('POST', '/api/plan-approve',
                                            {'id': entry['id'], 'sha256': entry['sha256'], 'confirm': PLAN_ID})
        check(status == 200, 'POST /api/plan-approve: %s %s' % (status, approved))
        check(approved['reserve'] == ['reserve-1'], 'approval did not report the reserve key: %s' % approved)
        ws, reserve = approved['tasks']['ws-1']['task_id'], approved['tasks']['reserve-1']['task_id']
        names.update({ws: 'ws-1', reserve: 'reserve-1'})
        check((plans / PLAN_ID / 'PLAN.receipt.json').is_file(), 'no receipt beside the plan')
        status, relisted, _ = board.request('GET', '/api/plans')
        again = next(p for p in relisted['plans'] if p['id'] == entry['id'])
        check(again['approved'] and again['in_room'] == 2, 'the listing does not show the approval: %s' % again)
        summary['plan']['receipt'] = approved['receipt']

        # 2. The orchestrator's wake-up check sees its workstream waiting.
        step('tick orchestrator sees the delivered workstream')
        record = tick(ORCH, 'orchestrator', 'workstream delivered', 0)
        check('task ws-1' in record['items'], 'tick did not list the workstream: %s' % record)

        # 3. The orchestrator takes its workstream and delegates a helper to the agent seat.
        step('orchestrator claims and delegates')
        with relay_lock:
            orch.ask('command', 'ack', {'task_id': ws})
            fence = orch.ask('command', 'claim', {'task_id': ws, 'seconds': 1800})['task']['fence']
            helper = orch.ask('command', 'request_help', {
                'task_id': ws, 'fence': fence, 'recipient': AGENT, 'title': 'Summarize the notes with citations',
                'body': 'Summarize what notes.md says; cite path:line for every claim.\nINPUT: ' + NOTES_REL +
                        '\n' + AGENT_LINE + str(draft),
                'acceptance': 'Every citation checks out (crew/cite_check.py exit 0).'})['task']['id']
        names[helper] = 'helper'
        step('tick orchestrator sees the workstream still waiting')
        record = tick(ORCH, 'orchestrator', 'workstream waiting on its helper', 0)
        check('task ws-1' in record['items'], 'tick did not list the waiting workstream: %s' % record)

        # 4. The Runner works the helper with the fake agent and submits it.
        step('runner works the helper')
        result = runner.run_once()
        check(result.get('status') == 'submitted' and result.get('task_id') == helper,
              'the runner did not submit the helper: %s' % result)
        run_dir = Path(runs) / AGENT / ('%s-f%d' % (helper, result['fence']))
        check((run_dir / 'RESULT.md').read_text(encoding='utf-8') == DRAFT, 'RESULT.md is not the agent draft')
        summary['runner'] = {'status': result['status'], 'report_path': result['path'],
                             'run_folder_files': sorted(p.name for p in run_dir.iterdir())}

        # 5. The reviewer sees the queue, fetches the report, checks the citations, accepts.
        step('tick reviewer sees the helper queued')
        record = tick(JUDGE, 'reviewer', 'helper queued for review', 0)
        check(record['items'] == ['review helper'], 'tick did not list the helper review: %s' % record)
        step('reviewer fetches the report with view_text')
        with relay_lock:
            view = judge.state()
            h = task_in(view, helper)
            check(h['status'] == 'ready_for_review' and h['builder'] == AGENT, 'helper is %s by %s'
                  % (h['status'], h['builder']))
            text = judge.ask('handoff', 'view_text', dict(task_id=helper, **h['result_refs'][0]))['text']
            answer = judge.last_raw
        check('Headless agent result (unreviewed)' in text and NOTES_REL + ':3' in text,
              'the report is not the runner report with the agent citations')
        step('reviewer runs cite_check on the relay answer')
        cite = subprocess.run([TOOL_PY, '-B', str(CITE), '--root', str(estate), '--relay-answer', '-', '--json'],
                              input=answer.decode('utf-8'), capture_output=True, text=True, timeout=60)
        verdict = json.loads(cite.stdout) if cite.stdout.strip().startswith('{') else {}
        check(cite.returncode == 0, 'cite_check exit %s: %s %s' % (
            cite.returncode, [(r['citation'], r['result'], r['detail']) for r in verdict.get('citations', [])
                              if r['result'] != 'OK'] or verdict.get('evidence_labels'), cite.stderr.strip()[-300:]))
        check(verdict['ok'] and verdict['totals']['checked'] == 2 and
              [row['result'] for row in verdict['citations']] == ['OK', 'OK'],
              'cite_check did not check both citations: %s' % verdict)
        tampered = json.loads(answer.decode('utf-8'))
        check(TAMPER[0] in tampered['result']['text'], 'control: the citation to tamper with is missing')
        tampered['result']['text'] = tampered['result']['text'].replace(*TAMPER)
        control = subprocess.run([TOOL_PY, '-B', str(CITE), '--root', str(estate), '--relay-answer', '-', '--json'],
                                 input=json.dumps(tampered), capture_output=True, text=True, timeout=60)
        control_rows = [row['result'] for row in json.loads(control.stdout)['citations']] if control.stdout else []
        check(control.returncode == 1 and 'OUT OF RANGE' in control_rows,
              'control: a tampered citation was not flagged (exit %s, %s)' % (control.returncode, control_rows))
        summary['cite_check'] = {'exit': cite.returncode, 'citations': [(r['citation'], r['result']) for r in verdict['citations']],
                                 'evidence_labels': {k: v for k, v in verdict['evidence_labels'].items() if v},
                                 'control_tampered_exit': control.returncode, 'control_results': control_rows}
        step('reviewer accepts the helper')
        with relay_lock:
            judge.ask('command', 'review', {'task_id': helper, 'accepted': True,
                                            'note': 'cite_check exit 0: both citations OK against the file.'})

        # 6. The orchestrator finishes its workstream; the reviewer accepts it.
        step('orchestrator finishes the workstream')
        with relay_lock:
            staged = orch.ask('handoff', 'stage_text', {'task_id': ws, 'fence': fence, 'text':
                              'Workstream report: helper %s (headless agent seat %s) accepted by %s with a clean '
                              'citation check.' % (helper, AGENT, JUDGE)})
            orch.ask('command', 'finish', {'task_id': ws, 'fence': fence, 'body': 'Workstream done; helper accepted.',
                                           'refs': [{'path': staged['path'], 'sha256': staged['sha256']}]})
        step('tick reviewer sees the workstream queued')
        record = tick(JUDGE, 'reviewer', 'workstream queued for review', 0)
        check(record['items'] == ['review ws-1'], 'tick did not list the workstream review: %s' % record)
        step('reviewer accepts the workstream')
        with relay_lock:
            judge.ask('command', 'review', {'task_id': ws, 'accepted': True, 'note': 'Report links the accepted helper.'})

        # 7. Idle at the end; the only work left for the orchestrator is the untouched reserve envelope.
        step('tick reviewer idle at the end')
        tick(JUDGE, 'reviewer', 'end', 3)
        step('tick orchestrator at the end')
        record = tick(ORCH, 'orchestrator', 'end (reserve envelope still open)', 0)
        check(record['items'] == ['task reserve-1'], 'the orchestrator should see only its reserve task: %s' % record)

        # 8. End state and the owner-step count.
        step('end state')
        with relay_lock:
            final = judge.state()
        statuses = {'workstream': task_in(final, ws)['status'], 'helper': task_in(final, helper)['status'],
                    'reserve': task_in(final, reserve)['status']}
        strikes = {name: task_in(final, tid)['failed_attempts'] for name, tid in (('workstream', ws), ('helper', helper))}
        check(statuses == {'workstream': 'done', 'helper': 'done', 'reserve': 'delivered'} and not any(strikes.values()),
              'unexpected end state: %s %s' % (statuses, strikes))
        after = [e for e in events() if e['seq'] > setup_seq]
        owner_steps = ['%s %s' % (e['kind'], names.get(e['task_id'], e['task_id'])) for e in after if e['actor'] == 'keeper']
        check(owner_steps == ['task_created ws-1', 'task_deliver ws-1', 'task_created reserve-1', 'task_deliver reserve-1'],
              'owner steps are not exactly the plan approval: %s' % owner_steps)
        by_actor = {}
        for e in after:
            by_actor.setdefault(e['actor'], []).append('%s %s' % (e['kind'], names.get(e['task_id'], e['task_id'])))
        check(by_actor.get(AGENT) == ['task_ack helper', 'task_claim helper', 'task_finish helper'],
              'the agent seat did more than ack, claim and finish: %s' % by_actor.get(AGENT))
        summary.update(statuses=statuses, strikes=strikes, owner_steps=owner_steps,
                       events_by_actor=dict(sorted(by_actor.items())))
        blob = json.dumps(summary) + ''.join(p.read_text(encoding='utf-8', errors='replace')
                                             for p in run_dir.iterdir() if p.is_file())
        check(not any(t in blob for t in tokens), 'a room key appeared in the summary or the run folder')
        summary['result'] = 'PASS'
        summary.pop('step')
        summary['seconds'] = round(time.time() - started, 1)
        print(json.dumps(summary, indent=2))
        return 0
    finally:
        room.close()


if __name__ == '__main__':
    sys.exit(main())
