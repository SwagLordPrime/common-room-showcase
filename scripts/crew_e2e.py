"""End-to-end crew run in one process, with no human step after the owner posts the task.

Real renewing room (75 s keys, renewed 30 s early, so one real key rotation happens mid-run),
a fake Ollama on 127.0.0.1, the local-model worker seat, and a Claude reviewer seat acting only
through the file relay. Fictional seats and data under .checks. No real model, no network
beyond loopback (tests.helpers installs an audit hook that refuses anything else).

Run from the package root:  python3.12 -B scripts/crew_e2e.py
Exit 0 and a JSON summary on success; exit 1 with the failing step otherwise.
"""
import io, json, sys, threading, time
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.helpers import case_dir                      # noqa: E402  (also installs the loopback-only guard)
from tests.test_ollama_worker import FakeOllama         # noqa: E402
from common_room import crew                            # noqa: E402
from common_room.activation import run_renewing         # noqa: E402
from common_room.crew_client import CrewClient          # noqa: E402
from common_room.ollama_worker import Worker, OllamaClient  # noqa: E402
from common_room.relay import Relay                     # noqa: E402

ENROLLMENT = {'version': 1, 'actors': [
    {'id': 'e2e-local', 'role': 'worker', 'label': 'Fictional local model seat', 'session': 'e2e fake Ollama'},
    {'id': 'e2e-reviewer', 'role': 'reviewer', 'label': 'Fictional Claude reviewer seat', 'session': 'e2e relay'}],
    'authorities': [{'id': 'e2e-crew', 'members': ['keeper', 'e2e-local', 'e2e-reviewer'],
                     'scope': 'Fictional end-to-end crew scope: drafts by a local seat, reviewed by a Claude seat.'}]}


def cli(root, actor, *args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = crew.main(['--credential', str(root / (actor + '-credential.json')), '--json', *args])
    if code != 0:
        raise SystemExit('crew %s %s failed: %s' % (actor, args[0], err.getvalue().strip()))
    return json.loads(out.getvalue())


def wait_for(predicate, seconds, what):
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.25)
    raise SystemExit('timed out waiting for ' + what)


def relay_call(relay, root, actor, request):
    folder = root / 'relay' / actor
    name = request['request_id']
    tmp = folder / 'requests' / (name + '.json.tmp')
    tmp.write_text(json.dumps(request), encoding='utf-8')
    tmp.rename(folder / 'requests' / (name + '.json'))
    relay.process_once()
    response = json.loads((folder / 'responses' / ('resp-' + name + '.json')).read_text(encoding='utf-8'))
    if not response.get('ok'):
        raise SystemExit('relay %s %s refused: %s' % (actor, name, response))
    return response['result']


def main():
    started = time.time()
    root = case_dir('crew-e2e')
    reads = case_dir('crew-e2e-reads')
    (reads / 'notes.md').write_text('alpha\nbeta\ngamma\n', encoding='utf-8')
    ollama = FakeOllama()
    ollama.models = ['fixture-model:latest']
    stop = threading.Event()
    lines = []
    thread = threading.Thread(target=run_renewing, daemon=True, kwargs=dict(
        root=root, selected=['e2e-local', 'e2e-reviewer'], enrollment=ENROLLMENT, lifetime=75, margin=30,
        stop=stop, log=lines.append))
    thread.start()
    try:
        wait_for(lambda: (root / 'e2e-local-credential.json').exists(), 20, 'the room to start')
        first_instance = json.loads((root / 'connection.json').read_text())['instance_id']

        # The only human step: the owner posts one task.
        task = cli(root, 'keeper', 'intake', '--scope', 'e2e-crew', '--to', 'e2e-local',
                   '--title', 'Summarize the notes', '--body', 'List what notes.md says.\nINPUT: notes.md',
                   '--acceptance', 'Every claim cites notes.md:line; unknowns say MISSING.')['task_id']

        # Live clients built BEFORE the key rotation and kept across it: the owner's view and the
        # reviewer's relay. Neither is recreated afterwards.
        keeper = CrewClient(root / 'keeper-credential.json')
        keeper.state()
        relay = Relay(root, ['e2e-reviewer'])
        relay.process_once()

        # The local-model seat wakes once: ack, claim, generate, save, submit.
        worker_log = []
        worker = Worker(CrewClient(root / 'e2e-local-credential.json'), 'fixture-model', OllamaClient(ollama.url),
                        [reads], log=worker_log.append, quiet_windows=[])
        result = worker.run_once()
        status = CrewClient.task_in(keeper.state(), task)['status']
        if status != 'ready_for_review':
            raise SystemExit('worker left the task at %s: %s' % (status, result))

        # Wait through one real key rotation before review happens.
        second_instance = wait_for(lambda: (lambda i: i if i != first_instance else None)(
            json.loads((root / 'connection.json').read_text())['instance_id']), 60, 'the room to renew its keys')

        # The Claude reviewer seat acts only through its relay folder (same Relay object as before).
        relay.process_once()
        visible = json.loads((root / 'relay' / 'e2e-reviewer' / 'relay-state.json').read_text(encoding='utf-8'))
        submitted = CrewClient.task_in(visible, task)
        ref = submitted['result_refs'][0]
        report = relay_call(relay, root, 'e2e-reviewer', {'request_id': 'e2e-view-1', 'kind': 'handoff',
                            'operation': 'view_text', 'payload': {'task_id': task, **ref}})['text']
        if 'notes.md' not in report or 'UNREVIEWED' not in report.upper():
            raise SystemExit('saved report is missing its source or its draft label')
        relay_call(relay, root, 'e2e-reviewer', {'request_id': 'e2e-review-1', 'kind': 'command', 'action': 'review',
                   'payload': {'task_id': task, 'accepted': True,
                               'note': 'Checked against notes.md lines 1-3; citations match.'}})

        final_state = keeper.state()                      # same live client, after the rotation
        final_task = CrewClient.task_in(final_state, task)
        if final_task['status'] != 'done':
            raise SystemExit('task ended at %s' % final_task['status'])
        history = [h for h in final_state['history'] if h['task_id'] == task]
        owner_steps = [h['kind'] for h in history if h['actor'] == 'keeper']
        if owner_steps != ['task_created', 'task_deliver']:
            raise SystemExit('unexpected owner steps: %s' % owner_steps)
        cycles = [json.loads(line) for line in (root / 'renewal-log.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
        instances = [c['instance_id'] for c in cycles]
        if len(set(instances)) < 2:
            raise SystemExit('no key rotation happened during the run')
        summary = {
            'result': 'PASS', 'task': task, 'final_status': final_task['status'],
            'builder': final_task['builder'], 'reviewer': final_task['reviewer'],
            'owner_steps': owner_steps,
            'human_steps_after_intake': len(owner_steps) - 2,
            'task_history': [(h['seq'], h['actor'], h['kind']) for h in history],
            'room_instances_seen': instances,
            'key_rotations_survived_by_live_clients': len(set(instances)) - 1,
            'ollama_requests': ollama.paths(),
            'worker_events': [e.get('event') if isinstance(e, dict) else json.loads(e).get('event') for e in worker_log],
            'seconds': round(time.time() - started, 1),
        }
        print(json.dumps(summary, indent=2))
        return 0
    finally:
        stop.set()
        thread.join(5)
        ollama.close()


if __name__ == '__main__':
    sys.exit(main())
