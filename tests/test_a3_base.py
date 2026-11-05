"""A3 against the base head's own code (brief rev 3, Tests: "Tests that run the base head's code"): the OFF
differential, rollback R1, R3 and R4.

The base head is up/duet-fix1 at 76742ac049a98ecd556264089d5fd044c228caa8. Its code is found only through this
module's own environment variable, A3_BASE_TREE (never A1_BASELINE_TREE); there is no default path, and every test
here that needs it skips when it is unset or empty. Set to anything else that is not an absolute checkout of that
commit, each test fails before any work. The base tree's half runs in short child processes started with
sys.executable -B, PYTHONDONTWRITEBYTECODE=1, the base tree first on the import path and as the working folder; the
child script points the base code's two root guards (start_room's, handoff_room.ROOT and activation.ROOT, and the
crew client's credential guard, the root=ROOT default of crew_client.check_credential_path, load_credential and CrewClient) at this worktree, so
every room, store, relay folder and join-card tree sits under this worktree's .checks. The child never imports
tests.* (tests/helpers.py makes .checks at import). Before and after each test, the base tree must have no .checks,
no .runtime and an empty `git status --porcelain`.

Both sides of every comparison run the same child script on one shared fake clock (1_800_000_000 plus the same
steps), so seqs, server times, every note's timestamp and the notes' sha256 (a hash of the whole event, timestamp
and seq included) match exactly; sha256 is NOT normalised. What is normalised, and why:
  - `timestamp` (events, notes, history): start_room writes the room's first event on the real clock before the
    fake clock is used (MEASURED in a development run: only event 1 differed), so times are normalised; the notes'
    sha256, which is not, still pins every note's own timestamp.
  - `instance_id` (state bodies and relay-state.json): a fresh random id each time a room starts (an instance id).
  - `previous_instance_id` and the text `instance <8 hex>` in the room's new-key-cycle notice (room_renewed, and the
    crew CLI's line about it): the id of the room instance that ran before this one (an instance id).
  - `relay_state_age_seconds` (tick.py check output): tick.py's own reading of the real clock (a time).
  - the loopback origin `http://127.0.0.1:<port>` in any string: the OS picks the port when a room first starts
    (an instance id).
  - the absolute paths of the run's scratch room folder, its join-card tree and the code tree under test, in any
    string: each run has its own scratch folders, and the code tree is the thing being compared (instance and
    code identity). They are replaced by <ROOM>, <CARDS> and <CODE>.
Every other field must match exactly. The flag value each run read is compared separately (`flag`): the base head
has no flag; the OFF runs must have read None from common_room.chat.chat_pair on a scratch flag path.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # lets `python tests/test_a3_base.py` run from a tree root

import json, os, re, subprocess, unittest

from common_room.protocol import canonical
from common_room.store import fold
from tests.helpers import case_dir, ROOT
import tests.test_a3_chat as a3            # module only: its TestCase classes are not collected again here

BASE_ENV = 'A3_BASE_TREE'
BASE_VALUE = os.environ.get(BASE_ENV, '')
BASE_TREE = Path(BASE_VALUE) if BASE_VALUE else None
BASE_FULL = '76742ac049a98ecd556264089d5fd044c228caa8'
BASE_SKIP = 'A3 base-head tests are opt-in: set ' + BASE_ENV + ' to a ' + BASE_FULL + ' checkout to run them'
CLOCK_START = 1_800_000_000
NORMALISED_KEYS = ('timestamp', 'instance_id', 'previous_instance_id', 'relay_state_age_seconds')
ORIGIN = re.compile(r'http://127\.0\.0\.1:\d+')
INSTANCE = re.compile(r'\binstance [0-9a-f]{8}\b')


def git_env():
    return dict(os.environ, GIT_OPTIONAL_LOCKS='0')


def base_problem(value):
    """None when `value` names an absolute checkout of the base head holding common_room/store.py."""
    folder = Path(value)
    if not folder.is_absolute():
        return 'is not an absolute path'
    if not (folder / 'common_room' / 'store.py').is_file():
        return 'holds no common_room/store.py'
    head = subprocess.run(['git', '--no-optional-locks', '-C', str(folder), 'rev-parse', 'HEAD'], capture_output=True,
                          text=True, env=git_env()).stdout.strip()
    if head != BASE_FULL:
        return 'has HEAD ' + (head or '(none)') + ', not ' + BASE_FULL
    return None


def base_dirt(folder):
    """What this module must never leave in the base tree: a .checks or .runtime folder, or any git change."""
    found = [name for name in ('.checks', '.runtime') if (Path(folder) / name).exists()]
    status = subprocess.run(['git', '--no-optional-locks', '-C', str(folder), 'status', '--porcelain'], capture_output=True,
                            text=True, env=git_env())
    if status.returncode != 0:
        found.append('git status failed: ' + status.stderr.strip()[-200:])
    elif status.stdout.strip():
        found.append('git status: ' + status.stdout.strip()[:500])
    return found


def normalise(value, paths):
    if isinstance(value, dict):
        return {k: ('<normalised>' if k in NORMALISED_KEYS else normalise(v, paths)) for k, v in value.items()}
    if isinstance(value, list):
        return [normalise(v, paths) for v in value]
    if isinstance(value, str):
        for path, label in paths:
            for form in (str(path), Path(path).as_posix(), json.dumps(str(path))[1:-1]):
                value = value.replace(form, label)
        return INSTANCE.sub('instance <id>', ORIGIN.sub('http://127.0.0.1:<port>', value))
    return value


# One script for both trees. argv: room root, this worktree (for the base code's start_room guard), action
# (scenario | read), mode (base | head), flag path, enrollment JSON path, join-card tree, run label.
SCRIPT = r'''
import importlib.util, inspect, io, json, os, shutil, subprocess, sys, threading, time
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
sys.dont_write_bytecode = True
tree = Path.cwd().resolve()
import common_room.handoff_room as handoff_room, common_room.activation as activation
assert Path(activation.__file__).resolve().parents[1] == tree, 'not the tree under test'
import common_room.crew_client as crew_client
handoff_room.ROOT = activation.ROOT = Path(sys.argv[2])
for _guarded in (crew_client.check_credential_path, crew_client.load_credential, crew_client.CrewClient.__init__):
    _guarded.__defaults__ = (Path(sys.argv[2]),) + _guarded.__defaults__[1:]   # each one's root=ROOT default, bound at import
room_root, action, mode, flag_path = Path(sys.argv[1]), sys.argv[3], sys.argv[4], sys.argv[5]
config_path, card_tree, label = sys.argv[6], Path(sys.argv[7]), sys.argv[8]
from common_room.activation import start_room
from common_room.board import state_body
from common_room.relay import Relay
from common_room.protocol import canonical
from common_room.store import fold
import common_room.crew as crew
try:
    from common_room.store import fold_questions
except ImportError:
    def fold_questions(events):
        return {}

SEATS = ['claude-room', 'dot', 'a3-worker', 'a3-observer']
RELAY_SEATS = ['claude-room', 'dot', 'a3-worker']

class Clock:
    def __init__(self, now):
        self.now = now
    def __call__(self):
        return self.now
clock = Clock(1800000000)

class FakeClock:
    def __init__(self):
        self.now = 1000.0
    def monotonic(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds

flag, flag_seen, kwargs, relay_kwargs = None, 'no flag on the base head', {}, {}
if mode == 'head':
    from common_room.chat import chat_pair
    flag = chat_pair(flag_path)
    flag_seen = flag
    kwargs['chat_pair'] = flag
    relay_kwargs['chat'] = flag
config = json.loads(Path(config_path).read_text(encoding='utf-8')) if action == 'scenario' else None
room = start_room(room_root, enrollment=config, selected=SEATS, clock=clock, **kwargs)
if room.server.server_address[1] == 50263:
    room.close()
    raise SystemExit('a scratch room took the live port 50263; stopped, no retry')
out = {'flag': flag_seen}
try:
    tick_state = room.root / 'relay' / 'claude-room' / 'tick-state.json'
    if tick_state.exists():
        tick_state.unlink()            # tick.py's own memory from an earlier run on this store
    relay = Relay(room.root, RELAY_SEATS, clock=clock, state_every=1e10, **relay_kwargs)
    relay._log = lambda *a, **k: None
    spec = importlib.util.spec_from_file_location('tick_under_test', tree / 'crew' / 'tick.py')
    tick = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tick)

    def send(seat, request):
        name = request['request_id']
        folder = room.root / 'relay' / seat
        tmp = folder / 'requests' / (name + '.tmp')
        tmp.write_text(json.dumps(request), encoding='utf-8')
        os.replace(tmp, folder / 'requests' / (name + '.json'))
        relay.process_once()
        body = None
        for answer in (folder / 'responses' / ('resp-' + name + '.json'), folder / 'responses' / ('resp-invalid-' + name + '.json')):
            if answer.exists():
                body = json.loads(answer.read_text(encoding='utf-8'))
                os.replace(answer, folder / 'archive' / (answer.stem + '.' + label + '.json'))
                break
        os.replace(folder / 'requests' / (name + '.json'), folder / 'archive' / (name + '.' + label + '.json'))
        return body

    def seq(answer):
        return ((answer or {}).get('result') or {}).get('seq')

    def cli(actor, *args):
        argv = ['--credential', str(room.root / (actor + '-credential.json')), *args]
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            try:
                code = crew.main(argv)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 2
        return {'code': code, 'out': o.getvalue(), 'err': e.getvalue()}

    def watch(actor):
        path = str(room.root / (actor + '-credential.json'))
        args = crew.build_parser().parse_args(['--credential', path, 'watch', '--max-wait', '45'])
        fake = FakeClock()
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            code = crew.run_watch(crew.CrewClient(path), args, sleep=fake.sleep, monotonic=fake.monotonic)
        return {'code': code, 'out': o.getvalue(), 'err': e.getvalue()}

    def tick_run(*argv):
        clock.now += 1
        box = {}
        o, e = io.StringIO(), io.StringIO()
        def run():
            try:
                box['code'] = tick.main([str(room.root / 'relay' / 'claude-room'), *argv])
            except SystemExit as exc:
                box['code'] = exc.code if isinstance(exc.code, int) else 2
        with redirect_stdout(o), redirect_stderr(e):
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            deadline = time.monotonic() + 120
            while worker.is_alive() and time.monotonic() < deadline:
                relay.process_once()
                worker.join(0.05)
        if worker.is_alive():
            raise SystemExit('tick.py did not finish')
        text = o.getvalue()
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = text
        return {'code': box.get('code'), 'out': parsed, 'err': e.getvalue()}

    def cards():
        handoffs = card_tree / '.runtime' / 'handoffs'
        handoffs.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(room.root / 'enrollment.json', handoffs / 'enrollment.json')
        if mode == 'head' and Path(flag_path).is_file():
            (card_tree / 'crew').mkdir(exist_ok=True)
            shutil.copyfile(flag_path, card_tree / 'crew' / 'chat-pair.json')
        env = dict(os.environ, PYTHONPATH=str(tree), PYTHONDONTWRITEBYTECODE='1')
        env.pop('PYTHONIOENCODING', None)
        result = {}
        for seat, flags in (('claude-room', ['--python']), ('dot', ['--no-python']), ('a3-worker', []), ('a3-observer', [])):
            proc = subprocess.run([sys.executable, '-B', str(tree / 'crew' / 'join_card.py'), seat, '--tree', str(card_tree), *flags],
                                  cwd=str(tree), env=env, capture_output=True, timeout=120)
            result[seat] = {'code': proc.returncode, 'out': proc.stdout.decode('utf-8', 'replace'),
                            'err': proc.stderr.decode('utf-8', 'replace')}
        return result

    def collect():
        keeper = room.store.people['keeper']
        events = room.store._snapshot(keeper)[0]
        authorities, tasks = fold(events)
        data = {'events': events, 'fold': canonical([authorities, tasks]), 'questions': sorted(fold_questions(events)),
                'state': {s: state_body(room.server, room.store.people[s]) for s in ['keeper'] + SEATS}, 'relay_state': {},
                'readme': {}}
        for seat in RELAY_SEATS:
            path = room.root / 'relay' / seat / 'relay-state.json'
            data['relay_state'][seat] = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
            data['readme'][seat] = (room.root / 'relay' / seat / 'README.md').read_text(encoding='utf-8')
        return data

    keeper = room.store.people['keeper']
    out['events_before'] = len(room.store._snapshot(keeper)[0])
    steps = {}
    if action == 'scenario':
        steps['c1_plain'] = send('claude-room', {'request_id': 'd-c1', 'kind': 'command', 'action': 'note',
                                                 'payload': {'authority_id': 'orchestra', 'body': 'Plain note from claude-room.'}})
        steps['w1_plain'] = cli('a3-worker', 'note', 'orchestra', 'Plain note from the third seat.', '--request-id', 'd-w1')
        steps['c2_question'] = send('claude-room', {'request_id': 'd-c2', 'kind': 'command', 'action': 'note',
                                                    'payload': {'authority_id': 'orchestra', 'body': 'QUESTION FOR dot: Is the pin set?'}})
        q = seq(steps['c2_question'])
        steps['d1_state'] = send('dot', {'request_id': 'd-d1', 'kind': 'state'})
        steps['d2_accept'] = send('dot', {'request_id': 'd-d2', 'kind': 'command', 'action': 'question_mark',
                                          'payload': {'note_seq': q, 'mark': 'accepted'}})
        steps['c3_chat_form'] = send('claude-room', {'request_id': 'chat-new-claude-room-1', 'kind': 'command', 'action': 'note',
                                                     'payload': {'authority_id': 'orchestra', 'body': 'CHAT FOR dot:\nA chat-form line.'}})
        s = seq(steps['c3_chat_form'])
        steps['d3_chat_answer'] = send('dot', {'request_id': 'chat-' + str(s) + '-dot-1', 'kind': 'command', 'action': 'note',
                                               'payload': {'authority_id': 'orchestra', 'body': 'CHAT FOR claude-room: re #' + str(s) + '\nAn answer.'}})
        steps['d4_chat_mark'] = send('dot', {'request_id': 'chat-' + str(s) + '-dot-mark', 'kind': 'command', 'action': 'chat_mark',
                                             'payload': {'authority_id': 'orchestra', 'note_seq': s, 'mark': 'seen'}})
        steps['d5_chat_status'] = send('dot', {'request_id': 'chat-status-dot-1', 'kind': 'command', 'action': 'chat_status',
                                               'payload': {'authority_id': 'orchestra', 'state': 'busy'}})
        steps['c4_chat_close'] = send('claude-room', {'request_id': 'chat-close-claude-room', 'kind': 'command', 'action': 'chat_close',
                                                      'payload': {'authority_id': 'orchestra', 'thread': s}})
        steps['w2_chat_form'] = cli('a3-worker', '--json', 'note', 'orchestra', 'CHAT FOR dot:\nFrom a third seat.', '--request-id', 'd-w2')
        steps['c5_state'] = send('claude-room', {'request_id': 'd-c5', 'kind': 'state'})
        steps['w3_notes'] = cli('a3-worker', '--json', 'notes')
    else:
        steps['d1_state'] = send('dot', {'request_id': 'r-d1', 'kind': 'state'})
        steps['c1_state'] = send('claude-room', {'request_id': 'r-c1', 'kind': 'state'})
        steps['d2_chat_mark'] = send('dot', {'request_id': 'r-d2', 'kind': 'command', 'action': 'chat_mark',
                                             'payload': {'authority_id': 'orchestra', 'note_seq': 1, 'mark': 'seen'}})
        steps['c2_chat_status'] = send('claude-room', {'request_id': 'r-c2', 'kind': 'command', 'action': 'chat_status',
                                                       'payload': {'authority_id': 'orchestra', 'state': 'clear'}})
        steps['w1_notes'] = cli('a3-worker', '--json', 'notes')
    steps['watch'] = {seat: watch(seat) for seat in ('a3-worker', 'dot', 'claude-room')}
    steps['tick_check'] = tick_run('check', '--role', 'orchestrator', '--timeout', '60', '--max-age', '100000000')
    steps['tick_questions'] = tick_run('questions')
    steps['cards'] = cards()
    out['steps'] = steps
    out['final'] = collect()
    out['events_after'] = len(out['final']['events'])
finally:
    room.close()
print(json.dumps(out))
'''


def run_tree(test, tree, room_root, action, mode, flag_path, config_path, card_tree, label):
    """Run SCRIPT with `tree`'s own code in a short child process; returns its JSON."""
    env = dict(os.environ, PYTHONPATH=str(tree), PYTHONDONTWRITEBYTECODE='1')
    env.pop('PYTHONIOENCODING', None)
    proc = subprocess.run([sys.executable, '-B', '-c', SCRIPT, str(room_root), str(ROOT), action, mode, str(flag_path),
                           str(config_path), str(card_tree), label], cwd=str(tree), env=env, capture_output=True, timeout=600)
    if proc.returncode != 0:
        test.fail(label + ' run failed (exit ' + str(proc.returncode) + '): ' + proc.stderr.decode('utf-8', 'replace')[-3000:])
    return json.loads(proc.stdout.decode('utf-8').strip().splitlines()[-1])


def chat_texts(value):
    return 'CHAT FOR' in json.dumps(value) or 'Chat with' in json.dumps(value)


class ScriptTests(unittest.TestCase):
    """Run whether A3_BASE_TREE is set or not: the child script parses, and the opt-in reads as A1R's does."""

    def test_the_child_script_compiles(self):
        compile(SCRIPT, '<a3 base child script>', 'exec')
        self.assertNotIn('tests.', SCRIPT)                     # the child never imports tests/helpers (it makes .checks)

    def test_unset_or_empty_skips_and_a_wrong_value_fails(self):
        self.assertIn(BASE_ENV, BASE_SKIP)
        self.assertEqual(getattr(BaseHeadTests, '__unittest_skip__', False), BASE_VALUE == '')
        self.assertEqual(base_problem('tests'), 'is not an absolute path')
        self.assertEqual(base_problem(str(case_dir('a3-base-missing') / 'nothing')), 'holds no common_room/store.py')

    def test_no_room_relay_or_crew_code_reads_the_variable_and_no_tracked_file_names_the_base_folder(self):
        listed = subprocess.run(['git', '--no-optional-locks', '-C', str(ROOT), 'ls-files'], capture_output=True, text=True,
                                env=git_env())
        self.assertEqual(listed.returncode, 0, listed.stderr)
        folder = re.compile(r'[A-Za-z]:[\\/]+t[\\/]+a3' + 'b' + r'\b')
        files = set(listed.stdout.splitlines()) | {'tests/test_a3_chat.py', 'tests/test_a3_base.py'}
        for name in sorted(files):
            path = ROOT / name
            if not path.is_file() or path.suffix not in ('.py', '.cmd', '.json', '.md', '.txt', '.ps1', ''):
                continue
            text = path.read_text(encoding='utf-8', errors='replace')
            with self.subTest(file=name):
                self.assertIsNone(folder.search(text))
                if not name.startswith('tests/') and path.suffix in ('.py', '.cmd', '.ps1'):
                    self.assertNotIn(BASE_ENV, text)


@unittest.skipUnless(BASE_VALUE != '', BASE_SKIP)   # unset or empty: skip; reads no folder, runs no git
class BaseHeadTests(a3.ChatBase):
    def setUp(self):
        super().setUp()
        problem = base_problem(BASE_VALUE)
        if problem:
            self.fail(BASE_ENV + ' is set to ' + repr(BASE_VALUE) + ', which ' + problem + '. Unset ' + BASE_ENV
                      + ' to skip these tests, or point it at a ' + BASE_FULL + ' checkout.')
        self.assertEqual(base_dirt(BASE_TREE), [], 'the base tree was not clean before the test')

    def tearDown(self):
        self.assertEqual(base_dirt(BASE_TREE), [], 'the test left something in the base tree')
        super().tearDown()

    def config_file(self):
        path = case_dir('a3-config') / 'enrollment-in.json'
        path.write_text(json.dumps(a3.CHAT_CONFIG), encoding='utf-8')
        return path

    def run_base(self, root, action, label, config=None):
        cards = case_dir('a3-cards-' + label)
        absent = case_dir('a3-noflag') / 'chat-pair.json'
        result = run_tree(self, BASE_TREE, root, action, 'base', absent, config or self.config_file(), cards, label)
        return result, [(root, '<ROOM>'), (cards, '<CARDS>'), (BASE_TREE, '<CODE>')]

    def run_head(self, root, action, label, flag_path, config=None):
        cards = case_dir('a3-cards-' + label)
        result = run_tree(self, ROOT, root, action, 'head', flag_path, config or self.config_file(), cards, label)
        return result, [(root, '<ROOM>'), (cards, '<CARDS>'), (ROOT, '<CODE>')]

    def assert_same(self, base, head, label):
        b, h = normalise(base[0], base[1]), normalise(head[0], head[1])
        b.pop('flag'), h.pop('flag')
        self.assertEqual(sorted(b), sorted(h), label)
        for section in sorted(b):
            if section in ('steps', 'final'):
                self.assertEqual(sorted(b[section]), sorted(h[section]), label + ': ' + section)
                for part in sorted(b[section]):
                    self.assertEqual(b[section][part], h[section][part], label + ': ' + section + '.' + part)
            else:
                self.assertEqual(b[section], h[section], label + ': ' + section)

    def head_on_store(self, root, clock):
        """Your head with the flag ON writes chat lines, marks, a busy and a quota status, an open and a closed
        thread, in-process; returns the room (still open)."""
        room = a3.start_scratch(self, root, a3.CHAT_CONFIG, a3.SEATS, clock, a3.PAIR)

        def say(seat, text, re=None):
            code, result = self.cmd(room, seat, 'note', {'authority_id': 'orchestra',
                                                         'body': a3.chat_line(a3.OTHER[seat], text, re)},
                                    'r-' + seat + '-' + str(next(self.ids)))
            self.assertEqual(code, 'ok', text)
            return result['seq']
        closed = say('claude-room', 'Thread one opens.')
        answer = say('dot', 'Answer in thread one.', re=closed)
        self.assertEqual(self.cmd(room, 'claude-room', 'chat_mark', {'authority_id': 'orchestra', 'note_seq': answer,
                                                                     'mark': 'handled'}, 'r-mark-1')[0], 'ok')
        self.assertEqual(self.cmd(room, 'claude-room', 'chat_close', {'authority_id': 'orchestra', 'thread': closed},
                                  'r-close-1')[0], 'ok')
        open_line = say('claude-room', 'Thread two opens and stays open.')
        self.assertEqual(self.cmd(room, 'dot', 'chat_mark', {'authority_id': 'orchestra', 'note_seq': open_line,
                                                             'mark': 'seen'}, 'r-mark-2')[0], 'ok')
        self.assertEqual(self.cmd(room, 'claude-room', 'chat_status', {'authority_id': 'orchestra', 'state': 'busy'},
                                  'r-status-1')[0], 'ok')
        self.assertEqual(self.cmd(room, 'dot', 'chat_status', {'authority_id': 'orchestra', 'state': 'quota',
                                                               'until': clock.now + 3 * 3600}, 'r-status-2')[0], 'ok')
        return room

    def test_off_differential_flag_absent_and_off_match_the_base_head(self):
        base = self.run_base(case_dir('a3-diff-base'), 'scenario', 'diff-base')
        absent_flag = case_dir('a3-flag-absent') / 'chat-pair.json'
        absent = self.run_head(case_dir('a3-diff-absent'), 'scenario', 'diff-absent', absent_flag)
        off_flag = case_dir('a3-flag-off') / 'chat-pair.json'
        off_flag.write_text(json.dumps({'chat': 'off', 'scope': 'orchestra', 'seats': ['claude-room', 'dot']}), encoding='utf-8')
        off = self.run_head(case_dir('a3-diff-off'), 'scenario', 'diff-off', off_flag)
        self.assertEqual(base[0]['flag'], 'no flag on the base head')
        self.assertIsNone(absent[0]['flag'])                     # from the live root's decision function, on a scratch path
        self.assertIsNone(off[0]['flag'])
        self.assert_same(base, absent, 'flag absent')
        self.assert_same(base, off, 'flag present and off')

    def test_r1_the_base_head_reads_a_chat_on_store_as_plain_notes(self):
        root = case_dir('a3-r1')
        clock = a3.Clock(CLOCK_START)
        room = self.head_on_store(root, clock)
        events = self.events(room)
        head_fold = canonical([*fold(events)])
        bodies = [e['payload']['body'] for e in events if e['kind'] == 'note']
        self.assertTrue(any('chat' in e['payload'] for e in events if e['kind'] == 'note'))   # the head wrote chat lines
        room.close()
        old, _ = self.run_base(root, 'read', 'r1-base')
        self.assertEqual(old['events_before'], len(events))
        self.assertEqual(old['events_after'], len(events))        # its state requests wrote no question_marked event
        self.assertEqual(old['final']['fold'], head_fold)          # same authorities and tasks
        self.assertEqual(old['final']['questions'], [])
        for seat in ('claude-room', 'dot'):
            state = old['final']['state'][seat]
            self.assertEqual((state['questions'], state['asked']), ([], []), seat)
            self.assertNotIn('chat', state)
            for body in bodies:
                self.assertIn(body, [n['body'] for n in state['notes']])                      # plain Lounge notes
        self.assertTrue((old['steps']['d1_state'] or {}).get('ok'), old['steps']['d1_state'])  # the base relay really ran
        self.assertNotIn('questions_read', (old['steps']['d1_state'] or {}).get('result', {}))
        self.assertIsNotNone(old['final']['relay_state']['dot'])
        self.assertEqual((old['steps']['d2_chat_mark'] or {}).get('error'), 'invalid')

    def test_r3_the_head_with_the_flag_on_starts_on_a_store_the_base_head_made(self):
        root = case_dir('a3-r3')
        old, _ = self.run_base(root, 'scenario', 'r3-base')
        clock = a3.Clock(CLOCK_START + 3600)
        room = a3.start_scratch(self, root, None, a3.SEATS, clock, a3.PAIR)
        relay = a3.make_relay(room)
        events = self.events(room)
        self.assertEqual(len(events), old['events_after'])
        for e in events:
            if e['kind'] == 'note':
                self.assertNotIn('chat', e['payload'])           # the base head's chat-form lines stay plain
        self.assertEqual(a3.inbox(self.d_state(room, relay)), [])
        line = a3.chat_line('dot', 'First line after the switch.')
        self.assert_ok(self.c_send(room, relay, a3.note_request('chat-new-claude-room-r3', line)))
        self.assertEqual([r['body'] for r in a3.inbox(self.d_state(room, relay))], [line])
        asked = self.c_state(room, relay)['asked']
        self.assertEqual([q['state'] for q in asked], ['accepted'])                              # A1's question holds

    def test_r4_turning_the_flag_off_and_on_again(self):
        root = case_dir('a3-r4')
        clock = a3.Clock(CLOCK_START)
        room = self.head_on_store(root, clock)
        before = {seat: self.state_of(room, seat).get('chat') for seat in ('claude-room', 'dot')}
        self.assertTrue(all(before.values()), before)
        count = len(self.events(room))
        room.close()
        absent_flag = case_dir('a3-flag-absent') / 'chat-pair.json'
        off = self.run_head(root, 'read', 'r4-off', absent_flag)
        base = self.run_base(root, 'read', 'r4-base')
        self.assertIsNone(off[0]['flag'])
        final = off[0]['final']
        for seat in ('claude-room', 'dot'):
            self.assertNotIn('chat', final['state'][seat])
            self.assertNotIn('chat', final['relay_state'][seat])
            self.assertNotIn('Chat with', final['readme'][seat])
        self.assertEqual((off[0]['steps']['d2_chat_mark'] or {}).get('error'), 'invalid')       # the base head's code
        self.assertEqual((off[0]['steps']['c2_chat_status'] or {}).get('error'), 'invalid')
        self.assertFalse(chat_texts([c['out'] for c in off[0]['steps']['cards'].values()]))
        chat_bodies = [e['payload']['body'] for e in final['events'] if e['kind'] == 'note' and 'chat' in e['payload']]
        self.assertTrue(chat_bodies)
        for body in chat_bodies:      # A3P (crew-audit F1, fix b): while OFF the stored lines stay plain notes, and only
            for seat in ('keeper', 'claude-room', 'dot'):           # the pair and keeper see them; every other seat sees none
                self.assertIn(body, [n['body'] for n in final['state'][seat]['notes']], seat)
            for seat in ('a3-worker', 'a3-observer'):
                self.assertNotIn(body, [n['body'] for n in final['state'][seat]['notes']], seat)
        self.assertEqual((off[0]['events_after'], base[0]['events_after']), (count, count))
        # The base head shows those stored lines to every member. A3P differs from it there, and only there: the views of
        # seats outside the pair and keeper, with the chat-key notes taken out, must match the base head byte for byte.
        hide_from_others(base[0]['final'], set(chat_bodies))
        # the third seat's `crew notes --json` (steps.w1_notes): the head's list is the base head's list without the chat
        # lines; every other byte of that step (code, err) is compared by assert_same below
        b_w1, h_w1 = base[0]['steps']['w1_notes'], off[0]['steps']['w1_notes']
        b_notes, h_notes = json.loads(b_w1['out'])['notes'], json.loads(h_w1['out'])['notes']
        self.assertTrue([n for n in b_notes if n['body'] in chat_bodies])
        self.assertEqual([n for n in b_notes if n['body'] not in chat_bodies], h_notes)
        b_w1['out'] = h_w1['out']
        self.assert_same(base, off, 'flag removed on a chat-ON store (A3P: chat lines only for the pair and keeper)')
        again = a3.start_scratch(self, root, None, a3.SEATS, clock, a3.PAIR)
        self.assertEqual({seat: self.state_of(again, seat).get('chat') for seat in ('claude-room', 'dot')}, before)
        self.assertEqual(len(self.events(again)), count)


def hide_from_others(final, bodies):
    """A3P: drop the chat-key notes (by body) from what seats outside the pair and keeper see in a collected run."""
    def scrub(value):
        if isinstance(value, list):
            return [scrub(v) for v in value if not (isinstance(v, dict) and v.get('body') in bodies)]
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items()}
        return value
    for part in ('state', 'relay_state'):
        for seat in list(final.get(part) or {}):
            if seat not in ('keeper', 'claude-room', 'dot') and final[part][seat] is not None:
                final[part][seat] = scrub(final[part][seat])

if __name__ == '__main__':
    unittest.main(argv=sys.argv[:1])
