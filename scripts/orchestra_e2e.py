"""End-to-end orchestrated plan, in one process: the owner approves a plan once; everything
after that happens without the owner.

Seats (fictional, under .checks): keeper; e2-orch, a Cowork-style orchestrator acting only through
its relay folder; e2-local, the local-model worker (fake Ollama on 127.0.0.1); e2-judge, a reviewer
acting only through its relay folder.

Flow: plan-approve (the one owner step) -> orchestrator claims its workstream and delegates two
helpers -> helper 1 hits a model outage and asks; helper 2 names a file the owner never named and
is refused -> the orchestrator answers helper 1 itself and cancels helper 2, delegating a corrected
helper 3 -> the worker drafts helpers 1 and 3 -> the reviewer accepts both -> the orchestrator
finishes the workstream -> the reviewer accepts it.

Run from the package root:  python3.12 -B scripts/orchestra_e2e.py
"""
import io, json, sys, time
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.helpers import case_dir                       # noqa: E402  (loopback-only guard)
from tests.test_ollama_worker import FakeOllama          # noqa: E402
from common_room import crew                             # noqa: E402
from common_room.activation import start_room            # noqa: E402
from common_room.crew_client import CrewClient           # noqa: E402
from common_room.ollama_worker import Worker, OllamaClient   # noqa: E402
from common_room.relay import Relay                      # noqa: E402

ENROLLMENT = {'version': 1, 'actors': [
    {'id': 'e2-orch', 'role': 'worker', 'label': 'Fictional orchestrator', 'session': 'relay'},
    {'id': 'e2-local', 'role': 'worker', 'label': 'Fictional local model', 'session': 'fake Ollama'},
    {'id': 'e2-judge', 'role': 'reviewer', 'label': 'Fictional reviewer', 'session': 'relay'}],
    'authorities': [{'id': 'e2-band', 'members': ['keeper', 'e2-orch', 'e2-local', 'e2-judge'],
                     'scope': 'Fictional orchestra scope for the end-to-end run.'}]}


class Seat:
    """A chat that can only write files: one relay folder, one request at a time."""
    def __init__(self, relay, root, actor):
        self.relay, self.folder, self.actor, self.n = relay, root / 'relay' / actor, actor, 0

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
        answer = json.loads((self.folder / 'responses' / ('resp-' + rid + '.json')).read_text(encoding='utf-8'))
        if not answer.get('ok'):
            raise SystemExit('%s %s %s refused: %s' % (self.actor, kind, action, answer))
        return answer['result']

    def state(self):
        self.ask('state')
        return json.loads((self.folder / 'relay-state.json').read_text(encoding='utf-8'))


def task(state, task_id):
    return CrewClient.task_in(state, task_id)


def main():
    started = time.time()
    root = case_dir('orchestra-e2e')
    reads = case_dir('orchestra-e2e-reads')
    (reads / 'notes.md').write_text('alpha\nbeta\ngamma\n', encoding='utf-8')
    (reads / 'design.md').write_text('not named by the owner\n', encoding='utf-8')
    plan_dir = case_dir('orchestra-e2e-plan')
    plan = plan_dir / 'PLAN.json'
    plan.write_text(json.dumps({'protocol': 'crew-plan/1', 'plan_id': 'e2e-plan', 'title': 'End-to-end plan',
        'scope': 'e2-band', 'tasks': [{'key': 'ws-1', 'to': 'e2-orch', 'title': 'Workstream one',
        'body': 'Split this into helper pieces.\nINPUT: notes.md', 'acceptance': 'Helpers reviewed; report links them.'}]}),
        encoding='utf-8')
    room = start_room(root, enrollment=ENROLLMENT, selected=['e2-orch', 'e2-local', 'e2-judge'])
    ollama = FakeOllama()
    ollama.models = ['fixture-model:latest']
    try:
        # 1. The one owner step: approve the plan at a terminal (stdin is a mocked terminal here).
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, 'stdin', io.StringIO('yes\n')), mock.patch.object(sys.stdin, 'isatty', return_value=True, create=True), \
                redirect_stdout(out), redirect_stderr(err):
            code = crew.main(['--credential', str(root / 'keeper-credential.json'), '--json', 'plan-approve', str(plan)])
        if code != 0:
            raise SystemExit('plan-approve failed: ' + err.getvalue())
        receipt = json.loads((plan_dir / 'PLAN.receipt.json').read_text(encoding='utf-8'))
        ws = receipt['tasks']['ws-1']['task_id']

        relay = Relay(root, ['e2-orch', 'e2-judge'])
        relay._log = lambda *a, **k: None             # keep the relay's own log lines out of the summary
        orch, judge = Seat(relay, root, 'e2-orch'), Seat(relay, root, 'e2-judge')
        worker_log = []
        worker = Worker(CrewClient(root / 'e2-local-credential.json'), 'fixture-model', OllamaClient(ollama.url),
                        [reads], log=worker_log.append, quiet_windows=[])

        # 2. The orchestrator takes its workstream and delegates two helpers.
        orch.ask('command', 'ack', {'task_id': ws})
        fence = orch.ask('command', 'claim', {'task_id': ws, 'seconds': 1800})['task']['fence']
        h1 = orch.ask('command', 'request_help', {'task_id': ws, 'fence': fence, 'recipient': 'e2-local',
                      'title': 'Summarize the notes', 'body': 'List what notes.md says.\nINPUT: notes.md',
                      'acceptance': 'Cites notes.md lines.'})['task']['id']
        h2 = orch.ask('command', 'request_help', {'task_id': ws, 'fence': fence, 'recipient': 'e2-local',
                      'title': 'Read the design', 'body': 'Summarize design.md.\nINPUT: design.md',
                      'acceptance': 'Cites design.md lines.'})['task']['id']

        # 3. Helper 1 meets a model outage (two 500s: the worker retries once, then asks);
        #    helper 2 names a file the owner never named and is refused before any model call.
        ollama.script['/api/chat'] = [('status', 500), ('status', 500)]
        worker.run_once()
        worker.run_once()
        view = orch.state()
        if (task(view, h1)['status'], task(view, h2)['status']) != ('waiting_decision', 'waiting_decision'):
            raise SystemExit('helpers did not both ask: %s %s' % (task(view, h1)['status'], task(view, h2)['status']))

        # 4. The orchestrator answers helper 1 itself and cancels helper 2, delegating a fix.
        orch.ask('command', 'resolve_decision', {'task_id': h1, 'body': 'The model is back; retry as written.'})
        orch.ask('command', 'cancel', {'task_id': h2, 'body': 'Wrong file; replaced by a helper on notes.md.'})
        fence = orch.ask('command', 'renew', {'task_id': ws, 'fence': fence, 'seconds': 1800})['task']['fence']
        h3 = orch.ask('command', 'request_help', {'task_id': ws, 'fence': fence, 'recipient': 'e2-local',
                      'title': 'Summarize lines 2-3', 'body': 'Quote notes.md lines 2-3.\nINPUT: notes.md#L2-L3',
                      'acceptance': 'Cites notes.md:2 and :3.'})['task']['id']

        # 5. The worker drafts helpers 1 and 3; the reviewer accepts both through its relay.
        for _ in range(4):
            worker.run_once()
        view = judge.state()
        for helper in (h1, h3):
            if task(view, helper)['status'] != 'ready_for_review':
                raise SystemExit('helper %s is %s' % (helper, task(view, helper)['status']))
            ref = task(view, helper)['result_refs'][0]
            text = judge.ask('handoff', 'view_text', dict(task_id=helper, **ref))['text']
            if 'UNREVIEWED DRAFT' not in text:
                raise SystemExit('draft label missing')
            judge.ask('command', 'review', {'task_id': helper, 'accepted': True, 'note': 'Checked against notes.md.'})

        # 6. The orchestrator finishes its workstream; the reviewer accepts it.
        staged = orch.ask('handoff', 'stage_text', {'task_id': ws, 'fence': fence,
                          'text': 'Workstream report: helpers %s and %s accepted; %s cancelled (wrong file).' % (h1, h3, h2)})
        orch.ask('command', 'finish', {'task_id': ws, 'fence': fence, 'body': 'Workstream done; helpers accepted.',
                                        'refs': [{'path': staged['path'], 'sha256': staged['sha256']}]})
        judge.ask('command', 'review', {'task_id': ws, 'accepted': True, 'note': 'Report links both accepted helpers.'})

        final = CrewClient(root / 'keeper-credential.json').state()
        statuses = {name: task(final, tid)['status'] for name, tid in (('workstream', ws), ('h1', h1), ('h2', h2), ('h3', h3))}
        owner_events = [(h['kind'], h['task_id']) for h in final['history'] if h['actor'] == 'keeper']
        strikes = {name: task(final, tid)['failed_attempts'] for name, tid in (('h1', h1), ('h2', h2), ('h3', h3))}
        expected = {'workstream': 'done', 'h1': 'done', 'h2': 'cancelled', 'h3': 'done'}
        if statuses != expected or [k for k, _ in owner_events] != ['task_created', 'task_deliver'] or any(strikes.values()):
            raise SystemExit('unexpected end state: %s %s %s' % (statuses, owner_events, strikes))
        print(json.dumps({'result': 'PASS', 'statuses': statuses, 'owner_steps': [k for k, _ in owner_events],
                          'strikes': strikes, 'ollama_requests': ollama.paths(),
                          'seconds': round(time.time() - started, 1)}, indent=2))
        return 0
    finally:
        ollama.close()
        room.close()


if __name__ == '__main__':
    sys.exit(main())
