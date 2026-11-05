"""Owner approvals on the board (autonomy build 2026-09-24, piece B, improvement #4).

Written before the feature (RED first). Each test runs a real room from
activation.start_room and talks to it over loopback HTTP the way the browser does: the
exact Host and Origin, a session cookie from /api/login, JSON bodies inside the 16 KB
request budget. The same plan and decisions files are also run through the CLI
(`crew plan-approve` / `crew decide-file`) to prove board and CLI share one implementation:
the same deterministic request ids, so neither ever duplicates the other.

Budget envelopes (improvement #5) are at the bottom: plan tasks marked "reserve": true, the
envelope wording shown by the CLI confirm and the board, the room's 60-jobs-per-approved-task
cap reached for real, and proof that an event log written before the cap folds identically.

Every actor and file is fictional and lives under tests.helpers.case_dir. Tokens are read
from the fixture credential files only to sign in and are never printed.
"""
import contextlib, hashlib, http.client, io, json, os, unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.helpers import case_dir
import common_room.crew as crew

CONFIG = {
    'version': 1,
    'actors': [
        {'id': 'bp-worker-a', 'role': 'worker', 'label': 'Fictional worker A', 'session': 'fixture-a'},
        {'id': 'bp-worker-b', 'role': 'worker', 'label': 'Fictional worker B', 'session': 'fixture-b'},
        {'id': 'bp-reviewer', 'role': 'reviewer', 'label': 'Fictional reviewer', 'session': 'fixture-r'},
        {'id': 'bp-observer', 'role': 'observer', 'label': 'Fictional observer', 'session': 'fixture-o'},
    ],
    'authorities': [
        {'id': 'bp-scope', 'members': ['keeper', 'bp-worker-a', 'bp-worker-b', 'bp-reviewer', 'bp-observer'],
         'scope': 'Fictional board-plan scope'},
    ],
}
SEATS = ['bp-worker-a', 'bp-worker-b', 'bp-reviewer', 'bp-observer']
NON_OWNERS = ['bp-worker-a', 'bp-reviewer', 'bp-observer']      # worker, reviewer, observer
MAX_FILE = 256 * 1024


def plan_doc(plan_id='bp-plan', scope='bp-scope', title='Fictional board plan', tasks=None):
    if tasks is None:
        tasks = [
            {'key': 't1', 'to': 'bp-worker-a', 'title': 'Task one',
             'body': 'Do thing one.\nINPUT: Artifacts/fixture/one.md#L1-L20\nMore words.',
             'acceptance': 'Thing one cites one.md lines.'},
            {'key': 't2', 'to': 'bp-worker-b', 'title': 'Task two', 'body': 'Do thing two.',
             'acceptance': 'Thing two looks right.'},
        ]
    return {'protocol': 'crew-plan/1', 'plan_id': plan_id, 'title': title, 'scope': scope, 'tasks': tasks}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Browser:
    """One signed-in browser tab: exact Host/Origin, one session cookie."""
    def __init__(self, room):
        self.origin = room.server.origin
        self.host = self.origin[len('http://'):]
        self.port = room.server.server_address[1]
        self.cookie = None

    def request(self, method, path, payload=None, raw=None, headers=None):
        h = {'Host': self.host, 'Origin': self.origin}
        if self.cookie:
            h['Cookie'] = self.cookie
        body = raw if raw is not None else (json.dumps(payload).encode('utf-8') if payload is not None else None)
        if body is not None:
            h['Content-Type'] = 'application/json'
        if headers:
            h.update(headers)
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=60)
        try:
            conn.request(method, path, body=body, headers=h)
            res = conn.getresponse()
            data = res.read()
            return res.status, (json.loads(data) if data else None), res.getheader('Set-Cookie')
        finally:
            conn.close()

    def login(self, credential_path):
        token = json.loads(Path(credential_path).read_text(encoding='utf-8'))['token']
        status, body, cookie = self.request('POST', '/api/login', {'token': token})
        if status != 200:
            raise AssertionError('login refused with status ' + str(status))
        self.cookie = cookie.split(';')[0]
        return body['actor']

    def plans(self):
        return self.request('GET', '/api/plans')

    def approve(self, plan_file_id, sha256, confirm):
        return self.request('POST', '/api/plan-approve', {'id': plan_file_id, 'sha256': sha256, 'confirm': confirm})

    def apply(self, decisions_file_id, sha256, confirm):
        return self.request('POST', '/api/decisions-apply', {'id': decisions_file_id, 'sha256': sha256, 'confirm': confirm})


class BoardPlanBase(unittest.TestCase):
    DEFAULT = object()

    def start(self, plans_root=DEFAULT):
        from common_room.activation import start_room
        self.plans_root = case_dir('board-plans-root') if plans_root is self.DEFAULT else plans_root
        room = start_room(case_dir('board-plans'), enrollment=CONFIG, selected=SEATS, plans_root=self.plans_root)
        self.addCleanup(room.close)
        self.room = room
        return room

    def browser(self, actor='keeper'):
        b = Browser(self.room)
        self.assertEqual(b.login(self.room.root / (actor + '-credential.json')), actor)
        return b

    def write(self, folder, name, value, root=None):
        target = Path(root or self.plans_root) / folder
        target.mkdir(parents=True, exist_ok=True)
        path = target / name
        path.write_text(value if isinstance(value, str) else json.dumps(value), encoding='utf-8')
        return path

    def events(self, kind=None):
        events, _, _ = self.room.store._snapshot(self.room.store.people['keeper'])
        return [e for e in events if kind is None or e['kind'] == kind]

    def tokens(self):
        return [json.loads((self.room.root / (a + '-credential.json')).read_text(encoding='utf-8'))['token']
                for a in ['keeper'] + SEATS]

    def assertNoToken(self, blob):
        for token in self.tokens():
            if token in blob:
                raise AssertionError('a room token appeared in board output or a receipt')

    # --- the CLI, in-process, exactly as test_crew_plans runs it ---------------------------
    def cli(self, actor, *args, stdin=None):
        argv = ['--credential', str(self.room.root / (actor + '-credential.json')), '--json'] + list(args)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            if stdin is not None:
                stack.enter_context(mock.patch('sys.stdin', new=stdin))
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            code = crew.main(argv)
        return code, out.getvalue(), err.getvalue()

    def yes(self):
        return SimpleNamespace(isatty=lambda: True, readline=lambda: 'yes\n')

    def waiting_task(self, worker='bp-worker-a', question='Which format?'):
        code, out, err = self.cli('keeper', 'intake', '--scope', 'bp-scope', '--to', worker, '--title', 'Question task',
                                  '--body', 'Ask before starting: ' + question, '--acceptance', 'Answered and done.')
        self.assertEqual(code, 0, err)
        task_id = json.loads(out)['task_id']
        for args in (('ack', task_id), ('claim', task_id, '--seconds', '300'), ('ask', task_id, '--question', question)):
            code, out, err = self.cli(worker, *args)
            self.assertEqual(code, 0, err)
        return task_id

    def listing_entry(self, data, kind, entry_id):
        for entry in data[kind]:
            if entry['id'] == entry_id:
                return entry
        return None


# ---------------------------------------------------------------------------------------
# Configuration and listing
# ---------------------------------------------------------------------------------------

class PlansRootTests(BoardPlanBase):
    def test_no_plans_root_reports_not_configured_and_refuses_both_actions(self):
        self.start(plans_root=None)
        b = self.browser()
        status, body, _ = b.plans()
        self.assertEqual((status, body), (200, {'configured': False}))
        status, body, _ = b.approve('bp-plan/PLAN.json', '0' * 64, 'bp-plan')
        self.assertEqual((status, body['error']), (400, 'config'))
        status, body, _ = b.apply('bp-plan/DECISIONS-1.json', '0' * 64, 'apply')
        self.assertEqual((status, body['error']), (400, 'config'))
        self.assertEqual(self.events('task_created'), [])

    def test_missing_plans_root_is_allowed_and_lists_nothing(self):
        missing = case_dir('board-plans-parent') / 'orchestra-not-yet'
        self.start(plans_root=missing)
        status, body, _ = self.browser().plans()
        self.assertEqual(status, 200)
        self.assertTrue(body['configured'])
        self.assertEqual((body['plans'], body['decisions']), ([], []))
        # The folder appearing later is picked up without a restart.
        self.write('bp-late', 'PLAN.json', plan_doc('bp-late'), root=missing)
        status, body, _ = self.browser().plans()
        self.assertEqual([p['id'] for p in body['plans']], ['bp-late/PLAN.json'])

    def test_linked_plans_root_is_refused_at_start(self):
        target = case_dir('board-plans-real')
        link = case_dir('board-plans-link') / 'orchestra'
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symbolic links are not available here')
        from common_room.activation import start_room
        with self.assertRaises(ValueError):
            start_room(case_dir('board-plans'), enrollment=CONFIG, selected=SEATS, plans_root=link)

    def test_start_room_records_an_absolute_resolved_plans_root(self):
        room = self.start()
        self.assertEqual(room.server.plans_root, Path(self.plans_root).resolve())
        self.assertTrue(room.server.plans_root.is_absolute())

    def test_run_renewing_gives_every_cycle_the_plans_root(self):
        from common_room.activation import run_renewing
        plans = case_dir('board-plans-root')
        self.write('bp-plan', 'PLAN.json', plan_doc(), root=plans)
        root = case_dir('board-plans-renew')
        clock = SimpleNamespace(now=1_800_000_000.0)
        seen = []

        def wait(seconds):
            descriptor = json.loads((root / 'connection.json').read_text(encoding='utf-8'))
            b = Browser(SimpleNamespace(server=SimpleNamespace(origin=descriptor['origin'],
                                                               server_address=('127.0.0.1', int(descriptor['origin'].rsplit(':', 1)[1])))))
            b.login(root / 'keeper-credential.json')
            status, data, _ = b.plans()
            seen.append((status, [p['id'] for p in data['plans']]))
            clock.now += seconds

        run_renewing(root, SEATS, CONFIG, lifetime=600, margin=60, clock=lambda: clock.now, wait=wait,
                     max_cycles=2, log=lambda line: None, plans_root=plans)
        self.assertEqual(seen, [(200, ['bp-plan/PLAN.json'])] * 2)

    def test_the_command_line_flag_reaches_start_room_and_a_linked_folder_stops_it(self):
        from common_room import activation
        missing = case_dir('board-plans-parent') / 'orchestra'
        with mock.patch.object(activation, 'start_room') as start, mock.patch.object(activation, 'run_console'):
            activation.main(['--root', str(case_dir('board-plans')), '--plans-root', str(missing)])
        self.assertEqual(start.call_args.kwargs['plans_root'], missing)
        target = case_dir('board-plans-real')
        link = case_dir('board-plans-link') / 'orchestra'
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symbolic links are not available here')
        with mock.patch.object(activation, 'start_room') as start, mock.patch.object(activation, 'run_console'), \
                redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                activation.main(['--root', str(case_dir('board-plans')), '--plans-root', str(link)])
        self.assertEqual(stopped.exception.code, 2)
        start.assert_not_called()


class ListingTests(BoardPlanBase):
    def test_listing_shows_every_task_in_full_with_the_file_hash(self):
        self.start()
        path = self.write('bp-plan', 'PLAN.json', plan_doc())
        bad = self.write('bp-plan', 'PLAN-2.json', plan_doc('bp-plan-2', tasks=[
            {'key': 'x1', 'to': 'bp-reviewer', 'title': 'Wrong seat', 'body': 'b', 'acceptance': 'a'}]))
        if not (Path(self.plans_root) / 'bp-plan' / 'plan.json').exists():     # a case-insensitive disk (Windows)
            self.write('bp-plan', 'plan.json', plan_doc('bp-lower'))       # would overwrite PLAN.json instead
        self.write('bp-plan', 'NOTES.md', 'not a plan')
        self.write('Bad_Folder', 'PLAN.json', plan_doc('bp-bad-folder'))    # folder is not a plan id
        status, data, _ = self.browser().plans()
        self.assertEqual(status, 200)
        self.assertTrue(data['configured'])
        self.assertEqual(sorted(p['id'] for p in data['plans']), ['bp-plan/PLAN-2.json', 'bp-plan/PLAN.json'])

        entry = self.listing_entry(data, 'plans', 'bp-plan/PLAN.json')
        self.assertEqual(entry['sha256'], sha(path))
        self.assertEqual((entry['plan_id'], entry['title'], entry['scope']), ('bp-plan', 'Fictional board plan', 'bp-scope'))
        self.assertEqual(entry['problems'], [])
        self.assertEqual(entry['conflicts'], [])
        self.assertFalse(entry['approved'])
        t1 = entry['tasks'][0]
        self.assertEqual({k: t1[k] for k in ('key', 'to', 'title', 'body', 'acceptance')}, plan_doc()['tasks'][0])
        self.assertIn('INPUT: Artifacts/fixture/one.md#L1-L20', t1['body'])

        wrong = self.listing_entry(data, 'plans', 'bp-plan/PLAN-2.json')
        self.assertEqual(wrong['sha256'], sha(bad))
        self.assertTrue(any('bp-reviewer' in p for p in wrong['problems']), wrong['problems'])

    def test_an_unreadable_plan_file_is_listed_with_its_error(self):
        self.start()
        self.write('bp-plan', 'PLAN.json', '{"protocol": "crew-plan/1", "protocol": "crew-plan/1"}')
        status, data, _ = self.browser().plans()
        self.assertEqual(status, 200)
        entry = self.listing_entry(data, 'plans', 'bp-plan/PLAN.json')
        self.assertTrue(entry['error'].startswith('invalid'), entry)
        self.assertNotIn('tasks', entry)

    def test_decisions_listing_shows_each_entry_with_the_current_question(self):
        self.start()
        task_id = self.waiting_task(question='Which format, markdown or text?')
        path = self.write('bp-plan', 'DECISIONS-1.json', {'protocol': 'crew-decisions/1', 'decisions': [
            {'task': task_id, 'action': 'resolve', 'text': 'Use markdown.'},
            {'task': 'tzzzzzzzzzzz', 'action': 'cancel', 'text': 'No such task.'}]})
        status, data, _ = self.browser().plans()
        self.assertEqual(status, 200)
        entry = self.listing_entry(data, 'decisions', 'bp-plan/DECISIONS-1.json')
        self.assertEqual(entry['sha256'], sha(path))
        self.assertFalse(entry['applied'])
        first, second = entry['entries']
        self.assertEqual((first['task_id'], first['action'], first['text']), (task_id, 'resolve', 'Use markdown.'))
        self.assertEqual(first['question'], 'Which format, markdown or text?')
        self.assertIsNone(first['error'])
        self.assertIsNone(second['task_id'])
        self.assertTrue(second['error'].startswith('forbidden'), second)

    def test_folder_and_file_caps(self):
        self.start()
        for i in range(51):
            self.write('bp-f%02d' % i, 'PLAN.json', plan_doc('bp-f%02d' % i))
        for n in range(1, 21):
            self.write('bp-f00', 'PLAN-%d.json' % n, plan_doc('bp-f00-%d' % n))
        status, data, _ = self.browser().plans()
        self.assertEqual(status, 200)
        folders = {p['id'].split('/')[0] for p in data['plans']}
        self.assertEqual(len(folders), 50)
        self.assertEqual(len([p for p in data['plans'] if p['id'].startswith('bp-f00/')]), 20)
        self.assertTrue(data['truncated'])


# ---------------------------------------------------------------------------------------
# Who may use the endpoints
# ---------------------------------------------------------------------------------------

class RoleTests(BoardPlanBase):
    def test_every_non_owner_role_gets_403_from_each_endpoint(self):
        self.start()
        task_id = self.waiting_task()
        plan_path = self.write('bp-plan', 'PLAN.json', plan_doc())
        dec_path = self.write('bp-plan', 'DECISIONS-1.json', {'protocol': 'crew-decisions/1', 'decisions': [
            {'task': task_id, 'action': 'resolve', 'text': 'Use markdown.'}]})
        before = len(self.events())
        roles = set()
        for actor in NON_OWNERS:
            b = self.browser(actor)
            roles.add(self.room.store.people[actor].role)
            for status, body, _ in (b.plans(), b.approve('bp-plan/PLAN.json', sha(plan_path), 'bp-plan'),
                                    b.apply('bp-plan/DECISIONS-1.json', sha(dec_path), 'apply')):
                self.assertEqual((status, body['error']), (403, 'forbidden'), actor)
        self.assertEqual(roles, {'worker', 'reviewer', 'observer'})
        self.assertEqual(len(self.events()), before)
        self.assertFalse((plan_path.parent / 'PLAN.receipt.json').exists())
        self.assertFalse((plan_path.parent / 'DECISIONS-1.receipt.json').exists())

    def test_endpoints_need_a_session_and_pass_the_host_and_origin_guard(self):
        self.start()
        plan_path = self.write('bp-plan', 'PLAN.json', plan_doc())
        anonymous = Browser(self.room)
        self.assertEqual(anonymous.plans()[0], 401)
        self.assertEqual(anonymous.approve('bp-plan/PLAN.json', sha(plan_path), 'bp-plan')[0], 401)
        b = self.browser()
        self.assertEqual(b.request('GET', '/api/plans', headers={'Host': 'evil.test'})[0], 403)
        self.assertEqual(b.request('POST', '/api/plan-approve', {'id': 'bp-plan/PLAN.json', 'sha256': sha(plan_path),
                                   'confirm': 'bp-plan'}, headers={'Origin': 'http://evil.test'})[0], 403)
        self.assertEqual(b.request('POST', '/api/plans', {})[0], 405)
        self.assertEqual(b.request('GET', '/api/plan-approve')[0], 405)
        self.assertEqual(self.events('task_created'), [])

    def test_request_budget_is_the_existing_16_kb(self):
        self.start()
        status, body, _ = self.browser().approve('bp-plan/PLAN.json', '0' * 64, 'x' * 17000)
        self.assertEqual(status, 413)


# ---------------------------------------------------------------------------------------
# Approving a plan
# ---------------------------------------------------------------------------------------

class ApproveTests(BoardPlanBase):
    def test_board_approve_creates_delivers_and_writes_the_receipt(self):
        self.start()
        path = self.write('bp-plan', 'PLAN.json', plan_doc())
        b = self.browser()
        status, result, _ = b.approve('bp-plan/PLAN.json', sha(path), 'bp-plan')
        self.assertEqual(status, 200, result)
        self.assertEqual(result['plan_id'], 'bp-plan')
        self.assertEqual(set(result['tasks']), {'t1', 't2'})
        for info in result['tasks'].values():
            self.assertEqual(info['status'], 'delivered')
        receipt_path = path.with_name('PLAN.receipt.json')
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        self.assertEqual((receipt['plan_id'], receipt['scope']), ('bp-plan', 'bp-scope'))
        self.assertIn('approved_at', receipt)
        self.assertEqual({k: v['task_id'] for k, v in receipt['tasks'].items()},
                         {k: v['task_id'] for k, v in result['tasks'].items()})
        self.assertEqual(len(self.events('task_created')), 2)
        self.assertEqual(len(self.events('task_deliver')), 2)
        self.assertNoToken(json.dumps(result) + receipt_path.read_text(encoding='utf-8'))

        _, data, _ = b.plans()
        entry = self.listing_entry(data, 'plans', 'bp-plan/PLAN.json')
        self.assertTrue(entry['approved'])
        self.assertNoToken(json.dumps(data))

    def test_sha256_mismatch_is_refused(self):
        self.start()
        path = self.write('bp-plan', 'PLAN.json', plan_doc())
        b = self.browser()
        _, data, _ = b.plans()
        seen = self.listing_entry(data, 'plans', 'bp-plan/PLAN.json')['sha256']
        status, body, _ = b.approve('bp-plan/PLAN.json', 'f' * 64, 'bp-plan')
        self.assertEqual((status, body['error']), (409, 'state'))
        # The file changed after Shane looked at it: the hash he saw no longer matches.
        self.write('bp-plan', 'PLAN.json', plan_doc(title='Swapped after viewing'))
        status, body, _ = b.approve('bp-plan/PLAN.json', seen, 'bp-plan')
        self.assertEqual((status, body['error']), (409, 'state'))
        status, body, _ = b.approve('bp-plan/PLAN.json', 'not-a-hash', 'bp-plan')
        self.assertEqual(status, 400)
        self.assertEqual(self.events('task_created'), [])
        self.assertFalse(path.with_name('PLAN.receipt.json').exists())

    def test_confirm_must_be_exactly_the_plan_id(self):
        self.start()
        path = self.write('bp-plan', 'PLAN.json', plan_doc())
        b = self.browser()
        for confirm in ('yes', 'BP-PLAN', ' bp-plan', 'bp-plan ', '', 'bp-pla'):
            status, body, _ = b.approve('bp-plan/PLAN.json', sha(path), confirm)
            self.assertEqual((status, body['error']), (400, 'invalid'), repr(confirm))
        status, body, _ = b.request('POST', '/api/plan-approve', {'id': 'bp-plan/PLAN.json', 'sha256': sha(path)})
        self.assertEqual(status, 400)
        self.assertEqual(self.events('task_created'), [])

    def test_a_plan_with_problems_is_refused(self):
        self.start()
        path = self.write('bp-plan', 'PLAN.json', plan_doc(tasks=[
            {'key': 't1', 'to': 'bp-reviewer', 'title': 'Wrong seat', 'body': 'b', 'acceptance': 'a'}]))
        status, body, _ = self.browser().approve('bp-plan/PLAN.json', sha(path), 'bp-plan')
        self.assertEqual((status, body['error']), (400, 'invalid'))
        self.assertIn('bp-reviewer', body['message'])
        self.assertEqual(self.events('task_created'), [])

    def test_an_edited_approved_task_is_listed_as_a_conflict_and_refused(self):
        self.start()
        path = self.write('bp-plan', 'PLAN.json', plan_doc())
        b = self.browser()
        self.assertEqual(b.approve('bp-plan/PLAN.json', sha(path), 'bp-plan')[0], 200)
        edited = plan_doc()
        edited['tasks'][0]['title'] = 'Task one EDITED'
        edited_path = self.write('bp-plan', 'PLAN-2.json', edited)
        _, data, _ = b.plans()
        self.assertEqual(self.listing_entry(data, 'plans', 'bp-plan/PLAN-2.json')['conflicts'], ['t1'])
        status, body, _ = b.approve('bp-plan/PLAN-2.json', sha(edited_path), 'bp-plan')
        self.assertEqual((status, body['error']), (400, 'invalid'))
        self.assertIn('already approved with different content', body['message'])
        self.assertEqual(len(self.events('task_created')), 2)

    def test_board_then_cli_on_the_same_file_creates_nothing_new(self):
        self.start()
        path = self.write('bp-plan', 'PLAN.json', plan_doc())
        status, result, _ = self.browser().approve('bp-plan/PLAN.json', sha(path), 'bp-plan')
        self.assertEqual(status, 200, result)
        before = len(self.events())
        code, out, err = self.cli('keeper', 'plan-approve', str(path), stdin=self.yes())
        self.assertEqual(code, 0, err)
        self.assertEqual({k: v['task_id'] for k, v in json.loads(out)['tasks'].items()},
                         {k: v['task_id'] for k, v in result['tasks'].items()})
        self.assertEqual(len(self.events()), before)
        self.assertEqual(len(self.events('task_created')), 2)

    def test_cli_then_board_on_the_same_file_creates_nothing_new(self):
        self.start()
        path = self.write('bp-plan', 'PLAN.json', plan_doc())
        code, out, err = self.cli('keeper', 'plan-approve', str(path), stdin=self.yes())
        self.assertEqual(code, 0, err)
        path.with_name('PLAN.receipt.json').unlink()
        before = len(self.events())
        status, result, _ = self.browser().approve('bp-plan/PLAN.json', sha(path), 'bp-plan')
        self.assertEqual(status, 200, result)
        self.assertEqual({k: v['task_id'] for k, v in result['tasks'].items()},
                         {k: v['task_id'] for k, v in json.loads(out)['tasks'].items()})
        self.assertEqual(len(self.events()), before)
        self.assertTrue(path.with_name('PLAN.receipt.json').exists())


# ---------------------------------------------------------------------------------------
# Applying a decisions file
# ---------------------------------------------------------------------------------------

class DecisionsTests(BoardPlanBase):
    def decisions(self, name, entries):
        return self.write('bp-plan', name, {'protocol': 'crew-decisions/1', 'decisions': entries})

    def test_board_apply_resolves_and_writes_a_receipt(self):
        self.start()
        task_id = self.waiting_task()
        path = self.decisions('DECISIONS-1.json', [{'task': task_id, 'action': 'resolve', 'text': 'Use markdown.'}])
        b = self.browser()
        status, result, _ = b.apply('bp-plan/DECISIONS-1.json', sha(path), 'apply')
        self.assertEqual(status, 200, result)
        self.assertTrue(result['ok'])
        self.assertEqual((result['results'][0]['task'], result['results'][0]['status']), (task_id, 'delivered'))
        receipt = json.loads(path.with_name('DECISIONS-1.receipt.json').read_text(encoding='utf-8'))
        self.assertEqual(set(receipt), {'applied_at', 'results'})
        self.assertEqual(receipt['results'], result['results'])
        _, data, _ = b.plans()
        self.assertTrue(self.listing_entry(data, 'decisions', 'bp-plan/DECISIONS-1.json')['applied'])
        self.assertNoToken(json.dumps(result) + json.dumps(receipt))

    def test_confirm_must_be_apply_and_the_hash_must_match(self):
        self.start()
        task_id = self.waiting_task()
        path = self.decisions('DECISIONS-1.json', [{'task': task_id, 'action': 'resolve', 'text': 'Use markdown.'}])
        b = self.browser()
        for confirm in ('yes', 'APPLY', 'apply ', 'bp-plan', ''):
            status, body, _ = b.apply('bp-plan/DECISIONS-1.json', sha(path), confirm)
            self.assertEqual((status, body['error']), (400, 'invalid'), repr(confirm))
        status, body, _ = b.apply('bp-plan/DECISIONS-1.json', 'e' * 64, 'apply')
        self.assertEqual((status, body['error']), (409, 'state'))
        self.assertEqual(self.events('task_resolve_decision'), [])
        self.assertFalse(path.with_name('DECISIONS-1.receipt.json').exists())

    def test_cli_decide_file_writes_a_receipt_too(self):
        self.start()
        task_id = self.waiting_task()
        path = self.decisions('DECISIONS-2.json', [{'task': task_id, 'action': 'resolve', 'text': 'Use text.'}])
        code, out, err = self.cli('keeper', 'decide-file', str(path), stdin=self.yes())
        self.assertEqual(code, 0, err)
        receipt = json.loads(path.with_name('DECISIONS-2.receipt.json').read_text(encoding='utf-8'))
        self.assertEqual(set(receipt), {'applied_at', 'results'})
        self.assertEqual(receipt['results'], json.loads(out)['results'])

    def test_board_then_cli_on_the_same_decisions_file_creates_nothing_new(self):
        self.start()
        task_id = self.waiting_task()
        path = self.decisions('DECISIONS-1.json', [{'task': task_id, 'action': 'resolve', 'text': 'Use markdown.'}])
        self.assertEqual(self.browser().apply('bp-plan/DECISIONS-1.json', sha(path), 'apply')[0], 200)
        before = len(self.events())
        code, out, err = self.cli('keeper', 'decide-file', str(path), stdin=self.yes())
        # fix-board M1: the earlier success is carried forward (not sent again), so the run is ok.
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.events()), before)
        self.assertEqual(len(self.events('task_resolve_decision')), 1)


# ---------------------------------------------------------------------------------------
# fix-board M1: applying a decisions file again keeps the earlier successes
# ---------------------------------------------------------------------------------------

class ReapplyDecisionsTests(BoardPlanBase):
    FILE_ID = 'bp-plan/DECISIONS-1.json'

    def decisions(self, name, entries):
        return self.write('bp-plan', name, {'protocol': 'crew-decisions/1', 'decisions': entries})

    def receipt(self, path):
        return json.loads(path.with_name('DECISIONS-1.receipt.json').read_text(encoding='utf-8'))

    def assertCarried(self, results, first):
        """Every result is ok and marked already applied, for the same task and action as the first run."""
        self.assertEqual(len(results), len(first))
        for now, then in zip(results, first):
            self.assertTrue(now['ok'], now)
            self.assertTrue(now.get('already_applied'), now)
            self.assertEqual((now['task'], now['action'], now['status']), (then['task'], then['action'], 'delivered'))

    def board_apply(self, path, b=None):
        status, result, _ = (b or self.browser()).apply(self.FILE_ID, sha(path), 'apply')
        self.assertEqual(status, 200, result)
        return result

    def cli_apply(self, path, expect=0):
        code, out, err = self.cli('keeper', 'decide-file', str(path), stdin=self.yes())
        self.assertEqual(code, expect, err)
        return json.loads(out), err

    def test_board_then_cli_keeps_the_ok_receipt(self):
        self.start()
        task_id = self.waiting_task()
        path = self.decisions('DECISIONS-1.json', [{'task': task_id, 'action': 'resolve', 'text': 'Use markdown.'}])
        first = self.board_apply(path)
        self.assertTrue(first['ok'])
        before = len(self.events())
        result, err = self.cli_apply(path)
        self.assertTrue(result['ok'])
        self.assertCarried(result['results'], first['results'])
        self.assertNotIn('request-id: decide-', err, 'an already applied entry is not sent again')
        self.assertIn('already applied', err)
        self.assertEqual(len(self.events()), before)
        receipt = self.receipt(path)
        self.assertEqual(receipt['results'], result['results'])
        self.assertTrue(all(r['ok'] for r in receipt['results']))

    def test_cli_then_board_keeps_the_ok_receipt(self):
        self.start()
        task_id = self.waiting_task()
        path = self.decisions('DECISIONS-1.json', [{'task': task_id, 'action': 'resolve', 'text': 'Use markdown.'}])
        first, _ = self.cli_apply(path)
        self.assertTrue(first['ok'])
        before = len(self.events())
        result = self.board_apply(path)
        self.assertTrue(result['ok'])
        self.assertCarried(result['results'], first['results'])
        self.assertEqual(len(self.events()), before)
        self.assertEqual(self.receipt(path)['results'], result['results'])
        _, data, _ = self.browser().plans()
        self.assertTrue(self.listing_entry(data, 'decisions', self.FILE_ID)['applied'])

    def test_board_twice_keeps_the_ok_receipt(self):
        self.start()
        task_id = self.waiting_task()
        other = self.waiting_task(worker='bp-worker-b', question='Which colour?')
        path = self.decisions('DECISIONS-1.json', [{'task': task_id, 'action': 'resolve', 'text': 'Use markdown.'},
                                                   {'task': other, 'action': 'cancel', 'text': 'Not needed after all.'}])
        b = self.browser()
        first = self.board_apply(path, b)
        self.assertTrue(first['ok'], first)
        before = len(self.events())
        result = self.board_apply(path, b)
        self.assertTrue(result['ok'], result)
        self.assertEqual([r['status'] for r in result['results']], ['delivered', 'cancelled'])
        for now in result['results']:
            self.assertTrue(now['ok'] and now['already_applied'], now)
        self.assertEqual(len(self.events()), before)
        self.assertEqual(self.receipt(path)['results'], result['results'])
        self.assertTrue(all(line.endswith(')') and 'already applied' in line for line in result['lines'][:2]), result['lines'])

    def test_only_the_entries_not_yet_applied_are_sent(self):
        self.start()
        waiting = self.waiting_task()
        # A second task that has not asked its question yet: its entry fails on the first run.
        code, out, err = self.cli('keeper', 'intake', '--scope', 'bp-scope', '--to', 'bp-worker-b', '--title', 'Later question',
                                  '--body', 'Ask later.', '--acceptance', 'Answered.')
        self.assertEqual(code, 0, err)
        later = json.loads(out)['task_id']
        path = self.decisions('DECISIONS-1.json', [{'task': waiting, 'action': 'resolve', 'text': 'Use markdown.'},
                                                   {'task': later, 'action': 'resolve', 'text': 'Use text.'}])
        first = self.board_apply(path)
        self.assertEqual([r['ok'] for r in first['results']], [True, False])
        for args in (('ack', later), ('claim', later, '--seconds', '300'), ('ask', later, '--question', 'Text or not?')):
            code, out, err = self.cli('bp-worker-b', *args)
            self.assertEqual(code, 0, err)
        resolved_before = len(self.events('task_resolve_decision'))
        result, err = self.cli_apply(path)
        self.assertTrue(result['ok'], result)
        self.assertEqual([r['ok'] for r in result['results']], [True, True])
        self.assertTrue(result['results'][0]['already_applied'])
        self.assertFalse(result['results'][1].get('already_applied'))
        self.assertEqual(len(self.events('task_resolve_decision')), resolved_before + 1, 'only the second entry was sent')
        self.assertIn('request-id: decide-' + later, err)
        self.assertNotIn('request-id: decide-' + waiting, err)
        self.assertEqual(self.receipt(path)['results'], result['results'])

    def test_a_changed_text_is_sent_again_and_its_failure_is_recorded(self):
        self.start()
        task_id = self.waiting_task()
        path = self.decisions('DECISIONS-1.json', [{'task': task_id, 'action': 'resolve', 'text': 'Use markdown.'}])
        self.board_apply(path)
        self.decisions('DECISIONS-1.json', [{'task': task_id, 'action': 'resolve', 'text': 'Use plain text.'}])
        result, err = self.cli_apply(path, expect=1)
        self.assertFalse(result['results'][0]['ok'])
        self.assertTrue(result['results'][0]['error'].startswith('state'), result)
        self.assertIn('request-id: decide-', err)
        self.assertEqual(self.receipt(path)['results'], result['results'])

    def test_a_forged_receipt_does_not_hide_an_answer(self):
        """The room's own history, not the receipt file, decides what was already applied."""
        self.start()
        task_id = self.waiting_task()
        text = 'Use markdown.'
        path = self.decisions('DECISIONS-1.json', [{'task': task_id, 'action': 'resolve', 'text': text}])
        forged = {'applied_at': 1.0, 'results': [{'task': task_id, 'ref': task_id, 'action': 'resolve', 'ok': True,
                                                  'status': 'delivered', 'seq': seq,
                                                  'text_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}
                                                 for seq in (1,)]}
        path.with_name('DECISIONS-1.receipt.json').write_text(json.dumps(forged), encoding='utf-8')
        result = self.board_apply(path)
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['results'][0].get('already_applied'), 'a forged success is not carried')
        self.assertEqual(len(self.events('task_resolve_decision')), 1)
        task = next(t for t in self.room.store.list_tasks(self.room.store.people['keeper']) if t['id'] == task_id)
        self.assertEqual((task['status'], task['decision_response']), ('delivered', text))


# ---------------------------------------------------------------------------------------
# Paths: only strict names directly under the plans root, never through a link
# ---------------------------------------------------------------------------------------

class PathSafetyTests(BoardPlanBase):
    def outside_plan(self, plan_id):
        folder = case_dir('board-plans-outside')
        path = folder / 'PLAN.json'
        path.write_text(json.dumps(plan_doc(plan_id)), encoding='utf-8')
        return folder, path

    def assertRefused(self, b, plan_file_id, digest, confirm):
        status, body, _ = b.approve(plan_file_id, digest, confirm)
        self.assertEqual(status, 400, (plan_file_id, body))
        self.assertEqual(self.events('task_created'), [])

    def test_traversal_and_malformed_ids_are_refused(self):
        self.start()
        good = self.write('bp-plan', 'PLAN.json', plan_doc())
        dec = self.write('bp-plan', 'DECISIONS-1.json', {'protocol': 'crew-decisions/1', 'decisions': [
            {'task': 'tzzzzzzzzzzz', 'action': 'resolve', 'text': 'x'}]})
        outside_folder, outside = self.outside_plan('bp-plan')
        b = self.browser()
        rel = os.path.relpath(outside, self.plans_root).replace(os.sep, '/')
        for bad in ('../' + outside_folder.name + '/PLAN.json', rel, 'bp-plan/../bp-plan/PLAN.json',
                    '..\\bp-plan\\PLAN.json', 'bp-plan\\PLAN.json', '/bp-plan/PLAN.json', str(outside),
                    'bp-plan/PLAN.json/', 'bp-plan//PLAN.json', 'bp-plan/sub/PLAN.json', './bp-plan/PLAN.json',
                    'bp-plan', 'PLAN.json', 'bp-plan/plan.json', 'bp-plan/PLAN.receipt.json', 'bp-plan/PLAN-01.json',
                    'bp-plan/PLAN.json\x00', 'bp-plan/DECISIONS-1.json', 'BP-PLAN/PLAN.json', ''):
            self.assertRefused(b, bad, sha(good), 'bp-plan')
        for bad in ('bp-plan/PLAN.json', '../bp-plan/DECISIONS-1.json', 'bp-plan/DECISIONS-01.json',
                    'bp-plan/DECISIONS.json', 'bp-plan/DECISIONS-1.receipt.json'):
            status, body, _ = b.apply(bad, sha(dec), 'apply')
            self.assertEqual(status, 400, (bad, body))
        status, body, _ = b.request('POST', '/api/plan-approve', {'id': ['bp-plan', 'PLAN.json'], 'sha256': sha(good),
                                                                  'confirm': 'bp-plan'})
        self.assertEqual(status, 400)
        self.assertEqual(self.events('task_created'), [])

    def test_a_linked_plan_folder_is_refused(self):
        self.start()
        outside_folder, outside = self.outside_plan('bp-linked')
        try:
            os.symlink(outside_folder, Path(self.plans_root) / 'bp-linked', target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symbolic links are not available here')
        b = self.browser()
        _, data, _ = b.plans()
        self.assertIsNone(self.listing_entry(data, 'plans', 'bp-linked/PLAN.json'))
        self.assertIn('bp-linked', [r['id'] for r in data['refused']])
        self.assertRefused(b, 'bp-linked/PLAN.json', sha(outside), 'bp-linked')

    def test_a_linked_plan_file_is_refused(self):
        self.start()
        _, outside = self.outside_plan('bp-symfile')
        (Path(self.plans_root) / 'bp-symfile').mkdir()
        try:
            os.symlink(outside, Path(self.plans_root) / 'bp-symfile' / 'PLAN.json')
        except (OSError, NotImplementedError):
            self.skipTest('symbolic links are not available here')
        b = self.browser()
        _, data, _ = b.plans()
        entry = self.listing_entry(data, 'plans', 'bp-symfile/PLAN.json')
        self.assertIn('error', entry)
        self.assertNotIn('tasks', entry)
        self.assertRefused(b, 'bp-symfile/PLAN.json', sha(outside), 'bp-symfile')

    def test_a_hardlinked_plan_file_is_refused(self):
        self.start()
        _, outside = self.outside_plan('bp-hard')
        (Path(self.plans_root) / 'bp-hard').mkdir()
        try:
            os.link(outside, Path(self.plans_root) / 'bp-hard' / 'PLAN.json')
        except (OSError, NotImplementedError):
            self.skipTest('hard links are not available here')
        b = self.browser()
        _, data, _ = b.plans()
        entry = self.listing_entry(data, 'plans', 'bp-hard/PLAN.json')
        self.assertIn('error', entry)
        self.assertNotIn('tasks', entry)
        self.assertRefused(b, 'bp-hard/PLAN.json', sha(outside), 'bp-hard')

    def test_an_oversized_plan_file_is_refused(self):
        self.start()
        text = json.dumps(plan_doc('bp-big'))
        path = self.write('bp-big', 'PLAN.json', text + ' ' * (MAX_FILE + 1 - len(text)))
        self.assertEqual(path.stat().st_size, MAX_FILE + 1)
        b = self.browser()
        _, data, _ = b.plans()
        entry = self.listing_entry(data, 'plans', 'bp-big/PLAN.json')
        self.assertIn('exceeds', entry['error'])
        self.assertRefused(b, 'bp-big/PLAN.json', sha(path), 'bp-big')

    # fix-board L4: controls that no test pinned before (each test fails when its control is removed).

    def test_a_plans_folder_swapped_for_a_link_after_start_is_refused_on_every_request(self):
        self.start()
        self.write('bp-plan', 'PLAN.json', plan_doc())
        outside = case_dir('board-plans-root-outside')
        evil = self.write('bp-evil', 'PLAN.json', plan_doc('bp-evil'), root=outside)
        root = Path(self.room.server.plans_root)
        os.rename(root, root.with_name(root.name + '-moved'))
        try:
            os.symlink(outside, root, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symbolic links are not available here')
        b = self.browser()
        status, body, _ = b.plans()
        self.assertEqual((status, body['error']), (400, 'config'), body)
        status, body, _ = b.approve('bp-evil/PLAN.json', sha(evil), 'bp-evil')
        self.assertEqual((status, body['error']), (400, 'config'), body)
        self.assertEqual(self.events('task_created'), [])
        self.assertFalse(evil.with_name('PLAN.receipt.json').exists())

    def test_the_listing_reads_at_most_8_mib_of_files_in_all(self):
        from common_room import plans
        self.assertEqual(plans.MAX_LISTING_BYTES, 8 * 1024 * 1024)
        self.start()
        for i in range(33):
            text = json.dumps(plan_doc('bp-b%02d' % i))
            self.write('bp-b%02d' % i, 'PLAN.json', text + ' ' * (MAX_FILE - len(text)))
        status, data, _ = self.browser().plans()
        self.assertEqual(status, 200)
        read = [p['id'] for p in data['plans'] if 'tasks' in p]
        refused = [p for p in data['plans'] if 'error' in p]
        self.assertEqual(len(read), 32, '32 full-size files are exactly the 8 MiB budget')
        self.assertEqual([p['id'] for p in refused], ['bp-b32/PLAN.json'])
        self.assertTrue(refused[0]['error'].startswith('too_large: '), refused[0])
        self.assertNotIn('sha256', refused[0], 'a file past the budget is not read at all')


class StrictJsonTests(BoardPlanBase):
    """fix-board L4: the 1e999 refusal in decode_strict_json (a clean 'invalid', never a traceback)."""
    FINITE = 'Plan file must be finite, valid JSON.'

    def overflowing_plan(self):
        return json.dumps(plan_doc()).replace('"title": "Fictional board plan"', '"title": 1e999').encode('utf-8')

    def test_parse_refuses_an_overflowing_number_cleanly(self):
        from common_room import plans
        from common_room.crew_client import CrewError
        raw = self.overflowing_plan()
        self.assertIn(b'1e999', raw)
        with self.assertRaises(CrewError) as caught:
            plans.parse_plan(raw)
        self.assertEqual((caught.exception.code, str(caught.exception)), ('invalid', self.FINITE))
        with self.assertRaises(CrewError) as caught:
            plans.parse_decisions(b'{"protocol": "crew-decisions/1", "decisions": [{"task": "t123456", '
                                  b'"action": "resolve", "text": 1e999}]}')
        self.assertEqual((caught.exception.code, str(caught.exception)),
                         ('invalid', 'Decisions file must be finite, valid JSON.'))

    def test_board_and_cli_list_and_refuse_it_without_a_traceback(self):
        self.start()
        path = self.write('bp-plan', 'PLAN.json', self.overflowing_plan().decode('utf-8'))
        status, data, _ = self.browser().plans()
        self.assertEqual(status, 200, data)
        self.assertEqual(self.listing_entry(data, 'plans', 'bp-plan/PLAN.json')['error'], 'invalid: ' + self.FINITE)
        code, out, err = self.cli('keeper', 'plan-check', str(path))
        self.assertEqual(code, 1)
        self.assertEqual(err.strip().splitlines()[-1], 'invalid: ' + self.FINITE)
        self.assertNotIn('Traceback', err)


class BoardClientAndLockTests(BoardPlanBase):
    def test_the_board_client_passes_only_the_five_plan_and_decision_actions(self):
        from common_room.plans import OwnerRoomClient
        from common_room.crew_client import CrewError
        self.assertEqual(OwnerRoomClient.ACTIONS,
                         frozenset({'intake', 'deliver', 'resolve_decision', 'cancel', 'reinstate'}))
        calls = []
        client = OwnerRoomClient(lambda action, payload, request_id: calls.append(action) or {'seq': 1}, lambda: {})
        for action in ('note', 'claim', 'ack', 'finish', 'fail', 'review', 'assign', 'request_help', 'create', 'grant',
                       'grant_handoff', 'pause', 'resume', 'revoke', 'renew', 'wait_decision'):
            with self.assertRaises(CrewError) as caught:
                client.command(action, {}, 'fixture-' + action)
            self.assertEqual(caught.exception.code, 'forbidden', action)
        self.assertEqual(calls, [], 'a refused action never reaches the room')
        for action in sorted(OwnerRoomClient.ACTIONS):
            client.command(action, {}, 'fixture-' + action)
        self.assertEqual(calls, sorted(OwnerRoomClient.ACTIONS))

    def test_board_plan_requests_run_one_at_a_time(self):
        import threading, time
        from common_room import board as board_module
        self.start()
        self.write('bp-plan', 'PLAN.json', plan_doc())
        real = board_module.board_listing
        guard, active, peak = threading.Lock(), [0], [0]

        def slow_listing(*args):
            with guard:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            try:
                time.sleep(0.4)
                return real(*args)
            finally:
                with guard:
                    active[0] -= 1
        b = self.browser()
        statuses = []
        with mock.patch.object(board_module, 'board_listing', slow_listing):
            threads = [threading.Thread(target=lambda: statuses.append(b.plans()[0])) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        self.assertEqual(statuses, [200, 200, 200])
        self.assertEqual(peak[0], 1, 'plans_lock lets one plans request run at a time')


# ---------------------------------------------------------------------------------------
# fix-board L1: the receipt goes into the folder the file was read from, or nowhere
# ---------------------------------------------------------------------------------------

class ReceiptFileTests(unittest.TestCase):
    def test_an_earlier_receipt_is_read_only_from_a_plain_file(self):
        from common_room import plans
        folder = case_dir('receipt-read')
        decisions = folder / 'DECISIONS-1.json'
        receipt = folder / 'DECISIONS-1.receipt.json'
        self.assertIsNone(plans.read_receipt(decisions), 'no receipt yet')
        receipt.write_text(json.dumps({'applied_at': 1.0, 'results': []}), encoding='utf-8')
        self.assertEqual(plans.read_receipt(decisions), {'applied_at': 1.0, 'results': []})
        for bad in (b'not json', b'{"a": 1, "a": 2}', b'[1, 2]', b' ' * (MAX_FILE + 1)):
            receipt.write_bytes(bad)
            self.assertIsNone(plans.read_receipt(decisions), bad[:20])
        receipt.unlink()
        elsewhere = case_dir('receipt-read-outside') / 'DECISIONS-1.receipt.json'
        elsewhere.write_text(json.dumps({'applied_at': 1.0, 'results': []}), encoding='utf-8')
        try:
            os.symlink(elsewhere, receipt)
        except (OSError, NotImplementedError):
            self.skipTest('symbolic links are not available here')
        self.assertIsNone(plans.read_receipt(decisions), 'a linked receipt is never read')
        receipt.unlink()
        receipt.mkdir()
        self.assertIsNone(plans.read_receipt(decisions), 'a folder is not a receipt')

    def test_the_receipt_write_never_follows_a_planted_temporary_link(self):
        import uuid
        from common_room import plans
        folder = case_dir('receipt-tmp-link')
        victim = case_dir('receipt-tmp-outside') / 'victim.txt'
        victim.write_text('untouched', encoding='utf-8')
        fixed = uuid.UUID(int=7)
        try:
            os.symlink(victim, folder / ('PLAN.receipt.json.' + fixed.hex + '.tmp'))
        except (OSError, NotImplementedError):
            self.skipTest('symbolic links are not available here')
        with mock.patch.object(plans.uuid, 'uuid4', lambda: fixed):
            with self.assertRaises(OSError):
                plans.write_receipt(folder / 'PLAN.receipt.json', {'plan_id': 'x'})
        self.assertEqual(victim.read_text(encoding='utf-8'), 'untouched')
        self.assertFalse(os.path.lexists(folder / 'PLAN.receipt.json'))


class ReceiptFolderSwapTests(BoardPlanBase):
    def swap(self, folder, link=True):
        """Move the plan folder away and put a link to an empty outside folder (or a new, different
        real folder) in its place: what a seat could do between the read and the receipt write."""
        moved = folder.with_name(folder.name + '-moved')
        os.rename(folder, moved)
        outside = case_dir('board-plans-swap-outside')
        if link:
            os.symlink(outside, folder, target_is_directory=True)
        else:
            folder.mkdir()
        self.swapped = (outside, moved)

    def swapping(self, module, name, folder, link=True):
        real = getattr(module, name)

        def then_swap(*args, **kwargs):
            result = real(*args, **kwargs)
            self.swap(folder, link)
            return result
        return mock.patch.object(module, name, then_swap)

    def assertNoReceiptAnywhere(self, folder, receipt_name):
        outside, moved = self.swapped
        self.assertEqual(sorted(p.name for p in outside.iterdir()), [], 'nothing was written through the link')
        self.assertFalse((moved / receipt_name).exists())
        self.assertFalse(os.path.lexists(folder / receipt_name))
        self.assertEqual([p.name for p in moved.iterdir() if p.name.endswith('.tmp')], [])

    def link_or_skip(self):
        probe = case_dir('board-plans-link-probe')
        try:
            os.symlink(probe, probe.with_name(probe.name + '-l'), target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symbolic links are not available here')

    def test_board_plan_receipt_is_refused_when_the_folder_becomes_a_link(self):
        self.link_or_skip()
        from common_room import plans
        self.start()
        path = self.write('bp-plan', 'PLAN.json', plan_doc())
        with self.swapping(plans, 'approve_plan', path.parent):
            status, body, _ = self.browser().approve('bp-plan/PLAN.json', sha(path), 'bp-plan')
        self.assertEqual((status, body['error']), (409, 'state'), body)
        self.assertIn('receipt was not written', body['message'])
        self.assertNoReceiptAnywhere(path.parent, 'PLAN.receipt.json')

    def test_board_plan_receipt_is_refused_when_the_folder_is_another_folder(self):
        from common_room import plans
        self.start()
        path = self.write('bp-plan', 'PLAN.json', plan_doc())
        with self.swapping(plans, 'approve_plan', path.parent, link=False):
            status, body, _ = self.browser().approve('bp-plan/PLAN.json', sha(path), 'bp-plan')
        self.assertEqual((status, body['error']), (409, 'state'), body)
        self.assertNoReceiptAnywhere(path.parent, 'PLAN.receipt.json')

    def test_board_decisions_receipt_is_refused_when_the_folder_becomes_a_link(self):
        self.link_or_skip()
        from common_room import plans
        self.start()
        task_id = self.waiting_task()
        path = self.write('bp-plan', 'DECISIONS-1.json', {'protocol': 'crew-decisions/1', 'decisions': [
            {'task': task_id, 'action': 'resolve', 'text': 'Use markdown.'}]})
        with self.swapping(plans, 'apply_decisions', path.parent):
            status, body, _ = self.browser().apply('bp-plan/DECISIONS-1.json', sha(path), 'apply')
        self.assertEqual((status, body['error']), (409, 'state'), body)
        self.assertNoReceiptAnywhere(path.parent, 'DECISIONS-1.receipt.json')

    def test_cli_plan_receipt_is_refused_when_the_folder_becomes_a_link(self):
        self.link_or_skip()
        self.start()
        path = self.write('bp-plan', 'PLAN.json', plan_doc())
        with self.swapping(crew, 'approve_plan', path.parent):
            code, out, err = self.cli('keeper', 'plan-approve', str(path), stdin=self.yes())
        self.assertEqual(code, 1, err)
        self.assertTrue(err.strip().splitlines()[-1].startswith('state: '), err)
        self.assertIn('receipt was not written', err)
        self.assertNoReceiptAnywhere(path.parent, 'PLAN.receipt.json')

    def test_cli_decisions_receipt_is_refused_when_the_folder_is_swapped(self):
        self.link_or_skip()
        self.start()
        task_id = self.waiting_task()
        path = self.write('bp-plan', 'DECISIONS-1.json', {'protocol': 'crew-decisions/1', 'decisions': [
            {'task': task_id, 'action': 'resolve', 'text': 'Use markdown.'}]})
        for link in (True, False):
            with self.subTest(link=link):
                try:
                    with self.swapping(crew, 'apply_decisions', path.parent, link=link):
                        code, out, err = self.cli('keeper', 'decide-file', str(path), stdin=self.yes())
                    self.assertEqual(code, 1, err)
                    self.assertTrue(err.strip().splitlines()[-1].startswith('state: '), err)
                    self.assertNoReceiptAnywhere(path.parent, 'DECISIONS-1.receipt.json')
                finally:
                    # Put the original folder back for the next round.
                    outside, moved = self.swapped
                    if os.path.islink(path.parent):
                        os.unlink(path.parent)
                    else:
                        for leftover in path.parent.iterdir():
                            leftover.unlink()
                        os.rmdir(path.parent)
                    os.rename(moved, path.parent)


# ---------------------------------------------------------------------------------------
# fix2-board N1 and N2 (VERIFY8A): what an earlier receipt may carry forward
# ---------------------------------------------------------------------------------------

def receipt_result(task_id, action, text, seq):
    """One ok result exactly as plans.apply_decisions writes it (what a seat could forge)."""
    return {'task': task_id, 'ref': task_id, 'action': action, 'ok': True, 'status': 'delivered', 'seq': seq,
            'text_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}


class CarryForwardTests(BoardPlanBase):
    """An earlier ok result is carried forward only when the room's history shows that very answer
    (task, action and text at the cited event) AND the task has not moved on since: a resolve whose
    task waits on a decision again (asked again, failed out, sent back by a review), or a reinstate
    whose task has recorded failures again, is sent, and the room decides. So a receipt, forged or
    real, never keeps back an answer the room would still take."""

    def decisions(self, name, entries):
        return self.write('bp-plan', name, {'protocol': 'crew-decisions/1', 'decisions': entries})

    def put_receipt(self, path, *results):
        path.with_name(path.stem + '.receipt.json').write_text(
            json.dumps({'applied_at': 1.0, 'results': list(results)}), encoding='utf-8')

    def owner(self, action, payload, request_id):
        return self.room.store.execute(self.room.store.people['keeper'], action, payload, request_id)

    def task(self, task_id):
        return next(t for t in self.room.store.list_tasks(self.room.store.people['keeper']) if t['id'] == task_id)

    def last_seq(self, kind, task_id):
        return max(e['seq'] for e in self.events(kind) if e['task_id'] == task_id)

    def worker(self, *args, worker='bp-worker-a'):
        code, out, err = self.cli(worker, *args)
        self.assertEqual(code, 0, err)
        return out

    def ask_again(self, task_id, question='Second question?'):
        for args in (('ack', task_id), ('claim', task_id, '--seconds', '300'), ('ask', task_id, '--question', question)):
            self.worker(*args)

    def fail_once(self, task_id, reason='Fixture failure.'):
        if self.task(task_id)['status'] == 'delivered':
            self.worker('ack', task_id)
        for args in (('claim', task_id, '--seconds', '300'), ('fail', task_id, '--reason', reason)):
            self.worker(*args)

    def delivered_task(self, title='Plain task'):
        code, out, err = self.cli('keeper', 'intake', '--scope', 'bp-scope', '--to', 'bp-worker-a', '--title', title,
                                  '--body', 'Nothing to ask.', '--acceptance', 'Done.')
        self.assertEqual(code, 0, err)
        return json.loads(out)['task_id']

    def board_apply(self, path):
        status, result, _ = self.browser().apply('bp-plan/' + path.name, sha(path), 'apply')
        self.assertEqual(status, 200, result)
        return result

    def cli_apply(self, path, expect):
        code, out, err = self.cli('keeper', 'decide-file', str(path), stdin=self.yes())
        self.assertEqual(code, expect, err)
        return json.loads(out), err

    # --- N1: the task asked again after the answer the receipt cites ------------------------

    def asked_twice(self, text):
        """A task answered once through DECISIONS-1 (on the board) and now waiting on a second question."""
        task_id = self.waiting_task(question='First question?')
        first = self.decisions('DECISIONS-1.json', [{'task': task_id, 'action': 'resolve', 'text': text}])
        seq1 = self.board_apply(first)['results'][0]['seq']
        self.ask_again(task_id)
        self.assertEqual(self.task(task_id)['status'], 'waiting_decision')
        return task_id, first, seq1

    def assertSentAgain(self, result, task_id, text, seq1):
        entry = result['results'][0]
        self.assertTrue(entry['ok'], entry)
        self.assertFalse(entry.get('already_applied'), 'the repeat answer must be sent, not carried')
        self.assertEqual(entry['earlier_seq'], seq1)
        self.assertEqual(len(self.events('task_resolve_decision')), 2)
        self.assertEqual((self.task(task_id)['status'], self.task(task_id)['decision_response']), ('delivered', text))

    def test_cli_a_forged_receipt_citing_the_answer_to_an_earlier_question_does_not_hide_a_repeat_answer(self):
        self.start()
        text = 'Yes, go ahead.'
        task_id, _, seq1 = self.asked_twice(text)
        second = self.decisions('DECISIONS-2.json', [{'task': task_id, 'action': 'resolve', 'text': text}])
        self.put_receipt(second, receipt_result(task_id, 'resolve', text, seq1))
        result, err = self.cli_apply(second, expect=0)
        self.assertSentAgain(result, task_id, text, seq1)
        self.assertIn('request-id: decide-' + task_id, err)
        self.assertNotIn('already applied', err)
        self.assertIn('(current question: Second question?)', err)
        self.assertIn('the room shows this same answer at event #' + str(seq1) + ', but the task has asked again or '
                      'failed since; it is sent again and the room decides', err)
        self.assertEqual(crew.decision_lines(result['results']),
                         [task_id + ' resolve: ok (delivered; sent again: event #' + str(seq1) + ' was an earlier answer)'])

    def test_board_a_forged_receipt_citing_the_answer_to_an_earlier_question_does_not_hide_a_repeat_answer(self):
        self.start()
        text = 'Yes, go ahead.'
        task_id, _, seq1 = self.asked_twice(text)
        second = self.decisions('DECISIONS-2.json', [{'task': task_id, 'action': 'resolve', 'text': text}])
        self.put_receipt(second, receipt_result(task_id, 'resolve', text, seq1))
        result = self.board_apply(second)
        self.assertTrue(result['ok'], result)
        self.assertSentAgain(result, task_id, text, seq1)
        self.assertEqual(result['lines'][0], task_id + ' resolve: ok (delivered; sent again: event #' + str(seq1) +
                         ' was an earlier answer)')
        receipt = json.loads(second.with_name('DECISIONS-2.receipt.json').read_text(encoding='utf-8'))
        self.assertEqual(receipt['results'], result['results'])

    def test_rerunning_an_old_file_after_the_task_asked_again_is_sent_and_the_confirm_says_so(self):
        """The same shape without a forgery: the entry is sent to the question the task waits on now,
        and the confirm shows that question and the note before Shane types yes."""
        self.start()
        text = 'Yes, go ahead.'
        task_id, first, seq1 = self.asked_twice(text)
        result, err = self.cli_apply(first, expect=0)
        self.assertSentAgain(result, task_id, text, seq1)
        confirm = err[:err.index("Type 'yes'")]
        self.assertIn(task_id + ' resolve (current question: Second question?)', confirm)
        self.assertIn('event #' + str(seq1), confirm)

    def test_a_task_stopped_by_failures_after_the_answer_is_sent_and_the_refusal_reported(self):
        self.start()
        text = 'Use markdown.'
        task_id = self.waiting_task()
        path = self.decisions('DECISIONS-1.json', [{'task': task_id, 'action': 'resolve', 'text': text}])
        seq1 = self.board_apply(path)['results'][0]['seq']
        self.fail_once(task_id)
        self.fail_once(task_id)
        self.assertEqual((self.task(task_id)['status'], self.task(task_id)['failed_attempts']), ('waiting_decision', 2))
        result, err = self.cli_apply(path, expect=1)
        entry = result['results'][0]
        self.assertFalse(entry['ok'])
        self.assertFalse(entry.get('already_applied'))
        self.assertTrue(entry['error'].startswith('budget: '), entry)
        self.assertEqual(entry['earlier_seq'], seq1)
        self.assertIn('request-id: decide-' + task_id, err)

    def test_a_reinstatement_is_sent_again_when_the_task_failed_again_since(self):
        self.start()
        task_id = self.delivered_task()
        self.fail_once(task_id)
        path = self.decisions('DECISIONS-1.json', [{'task': task_id, 'action': 'reinstate', 'text': 'Try again.'}])
        seq1 = self.board_apply(path)['results'][0]['seq']
        self.assertEqual(self.task(task_id)['failed_attempts'], 0)
        self.fail_once(task_id)
        result = self.board_apply(path)
        entry = result['results'][0]
        self.assertTrue(entry['ok'], entry)
        self.assertFalse(entry.get('already_applied'))
        self.assertEqual(entry['earlier_seq'], seq1)
        self.assertEqual(len(self.events('task_reinstate')), 2)
        self.assertEqual(self.task(task_id)['failed_attempts'], 0)

    def test_an_unmoved_task_is_still_carried(self):
        """M1 stays fixed: nothing happened since the answer, so it is carried, not sent."""
        self.start()
        task_id = self.waiting_task()
        path = self.decisions('DECISIONS-1.json', [{'task': task_id, 'action': 'resolve', 'text': 'Use markdown.'}])
        seq1 = self.board_apply(path)['results'][0]['seq']
        self.worker('ack', task_id)
        self.worker('claim', task_id, '--seconds', '300')
        result, err = self.cli_apply(path, expect=0)
        self.assertTrue(result['results'][0]['already_applied'])
        self.assertEqual(result['results'][0]['seq'], seq1)
        self.assertNotIn('earlier_seq', result['results'][0])
        self.assertNotIn('request-id: decide-', err)

    # --- N2: each room-history check is needed (VERIFY8A mutants A02, A04, A05, A09) --------

    def assertNotCarried(self, result, code):
        entry = result['results'][0]
        self.assertFalse(entry.get('already_applied'), 'the room shows no such answer: it must not be reported applied')
        self.assertFalse(entry['ok'], entry)
        self.assertTrue(entry['error'].startswith(code + ': '), entry)
        self.assertNotIn('earlier_seq', entry)

    def test_a02_a_receipt_citing_another_tasks_identical_answer_is_not_carried(self):
        self.start()
        text = 'Yes, go ahead.'
        other = self.waiting_task(worker='bp-worker-b', question='Other?')
        self.owner('resolve_decision', {'task_id': other, 'body': text}, 'fixture-resolve-other')
        seq_other = self.last_seq('task_resolve_decision', other)
        mine = self.delivered_task()           # never asked anything: the room has no answer of it
        path = self.decisions('DECISIONS-1.json', [{'task': mine, 'action': 'resolve', 'text': text}])
        self.put_receipt(path, receipt_result(mine, 'resolve', text, seq_other))
        self.assertNotCarried(self.board_apply(path), 'state')

    def test_a02_and_n1_a_receipt_citing_another_tasks_answer_never_hides_a_waiting_question(self):
        self.start()
        text = 'Yes, go ahead.'
        mine = self.waiting_task()
        other = self.waiting_task(worker='bp-worker-b', question='Other?')
        self.owner('resolve_decision', {'task_id': other, 'body': text}, 'fixture-resolve-other')
        path = self.decisions('DECISIONS-1.json', [{'task': mine, 'action': 'resolve', 'text': text}])
        self.put_receipt(path, receipt_result(mine, 'resolve', text, self.last_seq('task_resolve_decision', other)))
        result = self.board_apply(path)
        self.assertTrue(result['results'][0]['ok'] and not result['results'][0].get('already_applied'), result)
        self.assertEqual((self.task(mine)['status'], self.task(mine)['decision_response']), ('delivered', text))

    def test_v9_n1_a_receipt_naming_my_task_as_ref_but_another_task_as_task_is_not_carried(self):
        """VERIFY9 N1: ref = my task, task = another task whose real event has the same text."""
        self.start()
        text = 'Yes, go ahead.'
        mine = self.waiting_task()
        other = self.waiting_task(worker='bp-worker-b', question='Other?')
        self.owner('resolve_decision', {'task_id': other, 'body': text}, 'fixture-resolve-other-v9')
        forged = receipt_result(other, 'resolve', text, self.last_seq('task_resolve_decision', other))
        forged['ref'] = mine
        path = self.decisions('DECISIONS-1.json', [{'task': mine, 'action': 'resolve', 'text': text}])
        self.put_receipt(path, forged)
        result = self.board_apply(path)
        self.assertTrue(result['results'][0]['ok'] and not result['results'][0].get('already_applied'), result)
        self.assertEqual((self.task(mine)['status'], self.task(mine)['decision_response']), ('delivered', text))

    def test_a04_a_receipt_citing_the_same_tasks_different_earlier_answer_is_not_carried(self):
        self.start()
        task_id = self.waiting_task(question='First?')
        self.owner('resolve_decision', {'task_id': task_id, 'body': 'Old answer.'}, 'fixture-resolve-old')
        seq_old = self.last_seq('task_resolve_decision', task_id)
        new = 'A different new answer.'
        path = self.decisions('DECISIONS-1.json', [{'task': task_id, 'action': 'resolve', 'text': new}])
        self.put_receipt(path, receipt_result(task_id, 'resolve', new, seq_old))
        self.assertNotCarried(self.board_apply(path), 'state')

    def test_a05_a_receipt_citing_the_same_tasks_different_earlier_reinstatement_is_not_carried(self):
        self.start()
        task_id = self.delivered_task()
        self.fail_once(task_id)
        self.owner('reinstate', {'task_id': task_id, 'body': 'Old reason.'}, 'fixture-reinstate-old')
        seq_old = self.last_seq('task_reinstate', task_id)
        new = 'A different new reason.'
        path = self.decisions('DECISIONS-1.json', [{'task': task_id, 'action': 'reinstate', 'text': new}])
        self.put_receipt(path, receipt_result(task_id, 'reinstate', new, seq_old))
        self.assertNotCarried(self.board_apply(path), 'state')

    def test_a09_an_earlier_result_without_a_ref_is_ignored_cleanly(self):
        self.start()
        text = 'Use markdown.'
        task_id = self.waiting_task()
        path = self.decisions('DECISIONS-1.json', [{'task': task_id, 'action': 'resolve', 'text': text}])
        seq1 = self.board_apply(path)['results'][0]['seq']
        forged = receipt_result(task_id, 'resolve', text, seq1)
        del forged['ref']
        self.put_receipt(path, forged)
        result, err = self.cli_apply(path, expect=1)
        self.assertNotIn('Traceback', err)
        self.assertNotCarried(result, 'state')


# ---------------------------------------------------------------------------------------
# fix2-board N2 (VERIFY8A P11): _read_checked refuses a file swapped between check and open
# ---------------------------------------------------------------------------------------

class ReadCheckedSwapTests(unittest.TestCase):
    def test_a_file_swapped_for_another_regular_file_before_the_open_is_refused(self):
        from common_room import plans
        from common_room.crew_client import CrewError
        root = case_dir('read-checked-swap')
        (root / 'bp-plan').mkdir()
        path = root / 'bp-plan' / 'PLAN.json'
        path.write_text(json.dumps(plan_doc()), encoding='utf-8')
        self.assertEqual(plans._read_checked(root, 'bp-plan', 'PLAN.json'), path.read_bytes())
        other = root / 'bp-plan' / 'other.json'
        other.write_bytes(json.dumps(plan_doc('bp-other')).encode('utf-8'))
        real_open, swapped = os.open, []

        def swapping_open(target, flags, *args, **kwargs):
            if not swapped and os.fspath(target) == os.fspath(path):
                os.replace(other, path)      # a plain file, one link, only its identity differs
                swapped.append(True)
            return real_open(target, flags, *args, **kwargs)
        with mock.patch.object(plans.os, 'open', swapping_open):
            with self.assertRaises(CrewError) as caught:
                plans._read_checked(root, 'bp-plan', 'PLAN.json')
        self.assertEqual(swapped, [True])
        self.assertEqual((caught.exception.code, str(caught.exception)),
                         ('invalid', '"bp-plan/PLAN.json" changed while it was being read; refused.'))


# ---------------------------------------------------------------------------------------
# fix2-board N3 (VERIFY8A): a plan holding a room key is refused whole, before anything is sent
# ---------------------------------------------------------------------------------------

class PlanKeyGuardTests(BoardPlanBase):
    REFUSAL = 'Room text may not contain a room key.'

    def token(self, actor):
        return json.loads((self.room.root / (actor + '-credential.json')).read_text(encoding='utf-8'))['token']

    def keyed_plans(self):
        """(label, plan) pairs, each holding a live room key in a different place; task 1 is clean."""
        other, owner = self.token('bp-worker-b'), self.token('keeper')
        cases = []
        doc = plan_doc('bp-key0')
        doc['tasks'][1]['acceptance'] = 'Check with ' + other          # VERIFY8A's case
        cases.append(('task 2 acceptance', doc))
        doc = plan_doc('bp-key1')
        doc['tasks'][1]['body'] = 'Wrapped:\n' + owner[:20] + '\n' + owner[20:] + '\nend.'
        cases.append(('task 2 body, wrapped over lines', doc))
        doc = plan_doc('bp-key2')
        doc['tasks'][1]['title'] = 'Key ' + self.token('bp-reviewer')
        cases.append(('task 2 title', doc))
        doc = plan_doc('bp-key3', title='Plan ' + self.token('bp-observer'))
        cases.append(('plan title', doc))
        return cases

    def assertNothingSent(self, before, path, label):
        self.assertEqual(len(self.events()), before, label + ': no event at all')
        self.assertEqual(self.events('task_created'), [], label)
        self.assertFalse(path.with_name('PLAN.receipt.json').exists(), label)

    def test_the_board_refuses_the_whole_plan_before_creating_anything(self):
        self.start()
        b = self.browser()
        for label, doc in self.keyed_plans():
            path = self.write(doc['plan_id'], 'PLAN.json', doc)
            before = len(self.events())
            status, body, _ = b.approve(doc['plan_id'] + '/PLAN.json', sha(path), doc['plan_id'])
            self.assertEqual((status, body), (403, {'error': 'forbidden', 'message': self.REFUSAL}), label)
            self.assertNothingSent(before, path, label)
            _, listing, _ = b.plans()
            self.assertEqual(self.listing_entry(listing, 'plans', doc['plan_id'] + '/PLAN.json')['in_room'], 0, label)
        # The same plan without the key approves normally.
        clean = self.write('bp-clean', 'PLAN.json', plan_doc('bp-clean'))
        status, body, _ = b.approve('bp-clean/PLAN.json', sha(clean), 'bp-clean')
        self.assertEqual(status, 200, body)
        self.assertEqual(len(self.events('task_created')), 2)

    def test_the_cli_refuses_the_whole_plan_before_the_confirm_and_sends_nothing(self):
        self.start()
        for label, doc in self.keyed_plans():
            path = self.write(doc['plan_id'], 'PLAN.json', doc)
            before = len(self.events())
            code, out, err = self.cli('keeper', 'plan-approve', str(path), stdin=self.yes())
            # The CLI output may echo the plan (and so its key): assert without ever printing it.
            self.assertEqual(code, 1, label)
            self.assertTrue(err.strip().splitlines()[-1] == 'forbidden: ' + self.REFUSAL, label)
            self.assertFalse('request-id:' in err, label + ': nothing was sent')
            self.assertFalse("Type 'yes'" in err, label + ': refused before the confirm')
            self.assertNothingSent(before, path, label)
            self.assertNoToken(out + err)

    def test_the_key_check_route_is_owner_only_and_writes_nothing(self):
        self.start()
        owner, worker = self.browser(), self.browser('bp-worker-a')
        key = self.token('bp-worker-b')
        before = len(self.events())
        self.assertEqual(owner.request('POST', '/api/key-check', {'text': {'title': 'clean'}})[:2], (200, {'ok': True}))
        self.assertEqual(owner.request('POST', '/api/key-check', {'text': ['x', {'y': 'see ' + key}]})[:2],
                         (403, {'error': 'forbidden', 'message': self.REFUSAL}))
        status, body, _ = owner.request('POST', '/api/key-check', {'text': 'x', 'more': 1})
        self.assertEqual((status, body['error']), (400, 'invalid'))
        self.assertEqual(owner.request('GET', '/api/key-check')[0], 405)
        status, body, _ = worker.request('POST', '/api/key-check', {'text': 'clean'})
        self.assertEqual((status, body['error']), (403, 'forbidden'))
        self.assertNotEqual(body['message'], self.REFUSAL)
        self.assertEqual(Browser(self.room).request('POST', '/api/key-check', {'text': 'x'})[0], 401)
        self.assertEqual(len(self.events()), before)

    def test_a_room_without_the_key_check_is_a_clear_refusal_at_the_cli(self):
        """A room started from an older tree answers /api/key-check with 404: say so, send nothing."""
        from common_room.crew_client import CrewClient, CrewError

        class OlderRoom:
            def key_check(self, value):
                raise ValueError('not_found: Request refused')
        client = CrewClient(case_dir('older-room') / 'keeper-credential.json')
        client._participant = OlderRoom()
        with self.assertRaises(CrewError) as caught:
            client.check_keys({'title': 'clean'})
        self.assertEqual(caught.exception.code, 'config')
        self.assertIn('restart the room', str(caught.exception))

    def test_the_board_client_without_a_key_check_refuses_rather_than_skipping_it(self):
        from common_room.plans import OwnerRoomClient, refuse_plan_keys, parse_plan
        from common_room.crew_client import CrewError
        client = OwnerRoomClient(lambda *a: {'seq': 1}, lambda: {})
        with self.assertRaises(CrewError) as caught:
            refuse_plan_keys(client, parse_plan(json.dumps(plan_doc()).encode('utf-8')))
        self.assertEqual(caught.exception.code, 'config')


# ---------------------------------------------------------------------------------------
# The owner-only Plans panel in web/board.js, run against a minimal fake DOM
# ---------------------------------------------------------------------------------------

PANEL_HARNESS = r'''
const fs=require('fs'),vm=require('vm'),path=require('path'),nodeCrypto=require('crypto');
const {PendingJournal}=require(path.resolve('web/recovery.js'));
const source=fs.readFileSync('web/board.js','utf8');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
function make(tag){return {tagName:String(tag).toUpperCase(),children:[],dataset:{},listeners:{},hidden:false,disabled:false,required:false,value:'',textContent:'',className:'',
 get options(){return this.children.filter(x=>x.tagName==='OPTION');},get firstChild(){return this.children[0];},
 append(...k){this.children.push(...k);},replaceChildren(...k){this.children=[...k];},after(){},focus(){},setAttribute(k,v){this[k]=v;},
 addEventListener(t,f){(this.listeners[t]=this.listeners[t]||[]).push(f);}};}
const settle=async()=>{for(let i=0;i<40;i++)await new Promise(r=>setImmediate(r));};
const walk=(n,out=[])=>{out.push(n);for(const c of n.children||[])walk(c,out);return out;};
async function run(){
 const ids={},requests=[];
 const document={getElementById:id=>ids[id]||(ids[id]=make('div')),createElement:make,querySelector:()=>make('p'),hidden:false};
 const answers={'/api/plans':input.listing,'/api/plan-approve':{plan_id:'fixture',tasks:{},lines:['approved fixture line'],receipt:'r'},
  '/api/decisions-apply':{ok:true,results:[],lines:['applied fixture line'],receipt:'r'}};
 const context=vm.createContext({document,PendingJournal,console,crypto:nodeCrypto,setInterval:()=>0,
  sessionStorage:{getItem:()=>null,setItem(){},removeItem(){}},
  fetch:async(p,o)=>{requests.push({path:p,body:o&&o.body?JSON.parse(o.body):null});const a=answers[p]||input.state;return {ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(a))};}});
 vm.runInContext(source,context);await settle();
 const panel=ids['plans-panel'],root=ids['plans'];
 const out={panel_hidden:panel?panel.hidden:true,plans_requested:requests.some(r=>r.path==='/api/plans'),cards:{},posts:[]};
 for(const node of walk(root||make('div')))if(node.dataset&&node.dataset.fileId){
  const nodes=walk(node);out.cards[node.dataset.fileId]={texts:nodes.map(n=>n.textContent).filter(Boolean),
   labels:nodes.filter(n=>n.tagName==='LABEL').map(n=>n.textContent),buttons:nodes.filter(n=>n.tagName==='BUTTON').map(n=>n.textContent)};
 }
 for(const [fileId,typed] of input.typed||[]){
  const card=walk(root).find(n=>n.dataset&&n.dataset.fileId===fileId);if(!card)continue;
  const form=walk(card).find(n=>n.tagName==='FORM'),box=walk(form).find(n=>n.tagName==='INPUT');
  box.value=typed;for(const f of box.listeners.input||[])f();
  requests.length=0;await form.onsubmit({preventDefault(){}});await settle();
  out.posts.push(...requests.filter(r=>r.path!=='/api/state'&&r.path!=='/api/plans'));
 }
 process.stdout.write(JSON.stringify(out));
}
run().catch(e=>{console.error(e.stack||String(e));process.exit(1);});
'''


class PanelTests(BoardPlanBase):
    def panel(self, state, listing, typed=()):
        import shutil, subprocess
        from tests.helpers import ROOT
        node = os.environ.get('COMMON_ROOM_NODE') or shutil.which('node')
        if not node:
            self.skipTest('node not found: set COMMON_ROOM_NODE or put node on PATH')
        run = subprocess.run([node, '-e', PANEL_HARNESS], input=json.dumps({'state': state, 'listing': listing, 'typed': list(typed)}),
                             capture_output=True, text=True, cwd=str(ROOT), timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def test_owner_panel_shows_the_whole_plan_and_posts_the_typed_confirm_with_the_hash(self):
        self.start()
        plan_path = self.write('bp-plan', 'PLAN.json', plan_doc())
        task_id = self.waiting_task(question='Which colour for the chart?')
        dec_path = self.write('bp-plan', 'DECISIONS-1.json', {'protocol': 'crew-decisions/1', 'decisions': [
            {'task': task_id, 'action': 'resolve', 'text': 'Use green.'}]})
        b = self.browser()
        state = b.request('GET', '/api/state')[1]
        listing = b.plans()[1]
        view = self.panel(state, listing, typed=[('bp-plan/PLAN.json', 'wrong-id'), ('bp-plan/PLAN.json', 'bp-plan'),
                                                 ('bp-plan/DECISIONS-1.json', 'apply')])
        self.assertFalse(view['panel_hidden'])
        plan_card = view['cards']['bp-plan/PLAN.json']
        texts = '\n'.join(plan_card['texts'])
        for expected in (sha(plan_path), 'INPUT: Artifacts/fixture/one.md#L1-L20', 'Thing one cites one.md lines.',
                         'Thing two looks right.', 'bp-worker-a', 'bp-worker-b', 'Fictional board plan'):
            self.assertIn(expected, texts)
        self.assertIn('Type the plan id to approve', plan_card['labels'])
        self.assertIn('Approve', plan_card['buttons'])
        dec_card = view['cards']['bp-plan/DECISIONS-1.json']
        self.assertIn('Which colour for the chart?', '\n'.join(dec_card['texts']))
        self.assertIn('Use green.', '\n'.join(dec_card['texts']))
        self.assertIn('Type apply', dec_card['labels'])
        self.assertIn('Apply', dec_card['buttons'])
        # The mistyped id sends nothing; the right one posts exactly id, hash and the typed text.
        self.assertEqual(view['posts'], [
            {'path': '/api/plan-approve', 'body': {'id': 'bp-plan/PLAN.json', 'sha256': sha(plan_path), 'confirm': 'bp-plan'}},
            {'path': '/api/decisions-apply', 'body': {'id': 'bp-plan/DECISIONS-1.json', 'sha256': sha(dec_path), 'confirm': 'apply'}}])

    def test_the_panel_stays_hidden_and_silent_for_every_other_role(self):
        self.start()
        self.write('bp-plan', 'PLAN.json', plan_doc())
        for actor in NON_OWNERS:
            state = self.browser(actor).request('GET', '/api/state')[1]
            view = self.panel(state, {'configured': True, 'plans': [], 'decisions': [], 'refused': [], 'truncated': []})
            self.assertTrue(view['panel_hidden'], actor)
            self.assertFalse(view['plans_requested'], actor)


# =======================================================================================
# #5 Budget envelopes
# =======================================================================================

RESERVE_LINE = ('RESERVE [{}]: the orchestrator may add work under this task without asking you: '
                'at most 60 jobs in total, 24 directly, 8 open at once.')
PLAN_ENVELOPE_LINE = 'Every task in this plan can grow to at most 60 jobs in total, however it is split.'


def reserve_plan(plan_id='bp-env', reserve_keys=('t1',)):
    doc = plan_doc(plan_id)
    for task in doc['tasks']:
        if task['key'] in reserve_keys:
            task['reserve'] = True
    return doc


class ReserveValidationTests(unittest.TestCase):
    def validate(self, doc):
        from common_room import plans
        return plans.validate_plan(json.loads(json.dumps(doc)))

    def test_reserve_true_is_the_only_optional_field(self):
        checked = self.validate(reserve_plan())
        self.assertEqual([(t['key'], t['reserve']) for t in checked['tasks']], [('t1', True), ('t2', False)])
        plain = self.validate(plan_doc())
        self.assertEqual([t['reserve'] for t in plain['tasks']], [False, False])

    def test_reserve_must_be_a_json_true(self):
        from common_room.crew_client import CrewError
        for bad in (False, 'true', 1, None, [], {}, 'yes'):
            doc = plan_doc()
            doc['tasks'][0]['reserve'] = bad
            with self.assertRaises(CrewError) as caught:
                self.validate(doc)
            self.assertEqual(caught.exception.code, 'invalid', repr(bad))
        doc = plan_doc()
        doc['tasks'][0]['spare'] = True
        with self.assertRaises(CrewError):
            self.validate(doc)

    def test_at_most_three_reserve_tasks_per_plan(self):
        from common_room.crew_client import CrewError
        tasks = [{'key': 'k%d' % i, 'to': 'bp-worker-a', 'title': 'T', 'body': 'b', 'acceptance': 'a', 'reserve': True}
                 for i in range(4)]
        with self.assertRaises(CrewError) as caught:
            self.validate(plan_doc(tasks=tasks))
        self.assertEqual(caught.exception.code, 'invalid')
        self.assertIn('3', str(caught.exception))
        checked = self.validate(plan_doc(tasks=tasks[:3]))
        self.assertEqual(sum(t['reserve'] for t in checked['tasks']), 3)


class EnvelopeCliTests(BoardPlanBase):
    def test_cli_confirm_states_each_envelope_and_the_receipt_lists_reserve_keys(self):
        self.start()
        path = self.write('bp-env', 'PLAN.json', reserve_plan())
        code, out, err = self.cli('keeper', 'plan-check', str(path))
        self.assertEqual(code, 0, err)
        code, out, err = self.cli('keeper', 'plan-approve', str(path), stdin=self.yes())
        self.assertEqual(code, 0, err)
        self.assertIn(RESERVE_LINE.format('t1'), err)
        self.assertNotIn('RESERVE [t2]', err)
        self.assertIn(PLAN_ENVELOPE_LINE, err)
        self.assertEqual(json.loads(out)['reserve'], ['t1'])
        receipt = json.loads(path.with_name('PLAN.receipt.json').read_text(encoding='utf-8'))
        self.assertEqual(receipt['reserve'], ['t1'])
        # The room is never told: a reserve task is an ordinary root task, capped like every other.
        task = next(t for t in self.room.store.list_tasks(self.room.store.people['keeper'])
                    if t['id'] == receipt['tasks']['t1']['task_id'])
        self.assertNotIn('reserve', task)

    def test_cli_confirm_states_the_whole_plan_envelope_even_without_reserve_tasks(self):
        self.start()
        path = self.write('bp-plain', 'PLAN.json', plan_doc('bp-plain'))
        code, out, err = self.cli('keeper', 'plan-approve', str(path), stdin=self.yes())
        self.assertEqual(code, 0, err)
        self.assertIn(PLAN_ENVELOPE_LINE, err)
        self.assertNotIn('RESERVE [', err)
        receipt = json.loads(path.with_name('PLAN.receipt.json').read_text(encoding='utf-8'))
        self.assertEqual(receipt['reserve'], [])


class EnvelopeBoardTests(BoardPlanBase):
    def test_board_lists_reserve_tasks_with_their_envelope_and_the_receipt_lists_them(self):
        self.start()
        path = self.write('bp-env', 'PLAN.json', reserve_plan())
        b = self.browser()
        _, data, _ = b.plans()
        entry = self.listing_entry(data, 'plans', 'bp-env/PLAN.json')
        self.assertEqual([(t['key'], t['reserve']) for t in entry['tasks']], [('t1', True), ('t2', False)])
        self.assertEqual(entry['envelope'], [RESERVE_LINE.format('t1'), PLAN_ENVELOPE_LINE])
        status, result, _ = b.approve('bp-env/PLAN.json', sha(path), 'bp-env')
        self.assertEqual(status, 200, result)
        self.assertEqual(result['reserve'], ['t1'])
        receipt = json.loads(path.with_name('PLAN.receipt.json').read_text(encoding='utf-8'))
        self.assertEqual(receipt['reserve'], ['t1'])

    def test_board_and_cli_show_the_same_envelope_words(self):
        self.start()
        path = self.write('bp-env', 'PLAN.json', reserve_plan(reserve_keys=('t1', 't2')))
        _, data, _ = self.browser().plans()
        board_lines = self.listing_entry(data, 'plans', 'bp-env/PLAN.json')['envelope']
        code, out, err = self.cli('keeper', 'plan-approve', str(path), stdin=self.yes())
        self.assertEqual(code, 0, err)
        for line in board_lines:
            self.assertIn(line, err)
        self.assertEqual(len(board_lines), 3)

    def test_panel_shows_each_reserve_envelope(self):
        self.start()
        self.write('bp-env', 'PLAN.json', reserve_plan())
        b = self.browser()
        view = PanelTests.panel(self, b.request('GET', '/api/state')[1], b.plans()[1])
        texts = '\n'.join(view['cards']['bp-env/PLAN.json']['texts'])
        self.assertIn(RESERVE_LINE.format('t1'), texts)
        self.assertNotIn('RESERVE [t2]', texts)
        self.assertIn(PLAN_ENVELOPE_LINE, texts)


# ---------------------------------------------------------------------------------------
# The room's rule: at most 60 jobs grow under one approved (root) task
# ---------------------------------------------------------------------------------------

from tests.test_store import Clock, StoreFixture          # noqa: E402


class RootJobCapTests(StoreFixture):
    CAP_MESSAGE = 'At most sixty jobs can grow under one approved task.'

    def setUp(self):
        super().setUp()
        self.call(self.owner, 'grant_handoff', {'id': 'orch', 'members': ['owner', 'builder', 'second', 'reviewer'],
                  'capabilities': ['handoff.record'], 'root_id': 'fixture', 'scope': 'Fictional envelope scope',
                  'source_namespaces': ['estate']})
        self.pieces = 0

    def approved_root(self, reference):
        message = {'title': 'Approved ' + reference, 'body': 'A fictional approved task.', 'recipient': 'builder',
                   'authority_id': 'orch', 'kind': 'handoff.record', 'acceptance': 'Reviewed.'}
        task = self.call(self.owner, 'intake', {'source_namespace': 'estate', 'external_reference': reference,
                                               'message': message})['task']['id']
        self.call(self.owner, 'deliver', {'task_id': task})
        self.call(self.builder, 'ack', {'task_id': task})
        return task, self.claim(task, self.builder)['fence']

    def take(self, task, worker):
        self.call(worker, 'ack', {'task_id': task})
        return self.claim(task, worker)['fence']

    def helper(self, worker, parent, fence, recipient):
        self.pieces += 1
        return self.call(worker, 'request_help', {'task_id': parent, 'fence': fence, 'recipient': recipient,
                         'title': 'Piece %d' % self.pieces, 'body': 'A fictional piece.', 'acceptance': 'Reviewed.'})['task']['id']

    def cancel(self, worker, task):
        self.call(worker, 'cancel', {'task_id': task, 'body': 'Fictional churn, withdrawn by its sender.'})

    def descendants(self, root):
        tasks = {t['id']: t for t in self.store.list_tasks(self.owner)}
        def top(task_id):
            while tasks[task_id]['parent_id'] is not None:
                task_id = tasks[task_id]['parent_id']
            return task_id
        return sum(1 for t in tasks.values() if t['parent_id'] is not None and top(t['id']) == root)

    def test_the_sixtieth_job_is_accepted_and_the_sixty_first_refused(self):
        root, fence = self.approved_root('env-cap')
        # 24 helpers straight under the approved task (its per-parent total), all but two
        # cancelled as they are made so no more than 8 are ever open at once.
        direct = []
        for i in range(24):
            direct.append(self.helper(self.builder, root, fence, 'second'))
            if i >= 2:
                self.cancel(self.builder, direct[-1])
        # 24 more one level down, under the first helper, cancelled as they are made.
        first = self.take(direct[0], self.second)
        for _ in range(24):
            self.cancel(self.second, self.helper(self.second, direct[0], first, 'builder'))
        # 11 more under the second helper: 59 jobs in the tree.
        second = self.take(direct[1], self.second)
        for _ in range(11):
            self.cancel(self.second, self.helper(self.second, direct[1], second, 'builder'))
        self.assertEqual(self.descendants(root), 59)

        sixtieth = self.helper(self.second, direct[1], second, 'builder')       # accepted
        self.assertEqual(self.descendants(root), 60)
        events_before = len(self.store.poll(self.owner)['events'])
        with self.assertRaises(RoomError) as caught:                            # refused
            self.helper(self.second, direct[1], second, 'builder')
        self.assertEqual((caught.exception.code, str(caught.exception)), ('budget', self.CAP_MESSAGE))
        # Neither per-parent limit is what refused it: that helper has 12 children, 1 open.
        children = [t for t in self.store.list_tasks(self.owner) if t['parent_id'] == direct[1]]
        self.assertEqual((len(children), sum(t['status'] != 'cancelled' for t in children)), (12, 1))
        # Deeper in the same tree is refused too, and a refusal writes nothing.
        deeper = self.take(sixtieth, self.builder)
        with self.assertRaises(RoomError) as caught:
            self.helper(self.builder, sixtieth, deeper, 'second')
        self.assertEqual((caught.exception.code, str(caught.exception)), ('budget', self.CAP_MESSAGE))
        with self.assertRaises(RoomError):
            self.helper(self.builder, root, fence, 'second')
        self.assertEqual(len(self.store.poll(self.owner)['events']), events_before)
        self.assertEqual(self.descendants(root), 60)
        # Another approved task has its own sixty.
        other, other_fence = self.approved_root('env-other')
        self.helper(self.builder, other, other_fence, 'second')
        self.assertEqual(self.descendants(other), 1)
        import common_room.store as store_module
        self.assertEqual(getattr(store_module, 'ROOT_JOB_CAP', None), 60)


# ---------------------------------------------------------------------------------------
# fold() is unchanged: an event log written before the cap folds identically
# ---------------------------------------------------------------------------------------

import copy, subprocess, types                               # noqa: E402
from common_room.protocol import Actor, RoomError, canonical  # noqa: E402
from common_room.store import Store, fold                     # noqa: E402

ENVELOPE_BASELINE = '62326a2'


def envelope_scenario(store_cls, home):
    """A tree of nested helpers with cancels, every action a pre-cap store had; ids from a patched
    uuid4 and time from a fixed clock, so the normalized log is byte-stable."""
    art = home / 'artifacts'
    art.mkdir()
    people = {a.id: a for a in [Actor('owner', 'owner'), Actor('builder', 'worker'), Actor('second', 'worker'),
                                Actor('reviewer', 'reviewer')]}
    clock = Clock()
    store = store_cls(home / 'envelope.sqlite', people, {'fixture': art}, clock=clock)
    n = [0]

    def call(actor, action, payload):
        n[0] += 1
        clock.now += 1
        return store.execute(people[actor], action, payload, 'envelope-' + str(n[0]))

    def msg(title):
        return {'title': title, 'body': 'Fictional envelope body.', 'recipient': 'builder', 'authority_id': 'orch',
                'kind': 'handoff.record', 'acceptance': 'Reviewed.'}

    def helper(actor, parent, fence, recipient, title):
        return call(actor, 'request_help', {'task_id': parent, 'fence': fence, 'recipient': recipient, 'title': title,
                                            'body': 'Fictional piece.', 'acceptance': 'Reviewed.'})['task']['id']

    counter = iter(range(1, 1000))
    import uuid as uuid_module
    with mock.patch.object(uuid_module, 'uuid4', side_effect=lambda: uuid_module.UUID(int=next(counter))):
        call('owner', 'grant_handoff', {'id': 'orch', 'members': ['owner', 'builder', 'second', 'reviewer'],
             'capabilities': ['handoff.record'], 'root_id': 'fixture', 'scope': 'Fictional envelope replay',
             'source_namespaces': ['estate']})
        root = call('owner', 'intake', {'source_namespace': 'estate', 'external_reference': 'env-root',
                    'message': msg('Approved root')})['task']['id']
        call('owner', 'deliver', {'task_id': root})
        call('builder', 'ack', {'task_id': root})
        fence = call('builder', 'claim', {'task_id': root, 'seconds': 900})['task']['lease']['fence']
        h1, h2, h3 = (helper('builder', root, fence, 'second', 'Helper ' + str(i)) for i in (1, 2, 3))
        call('builder', 'cancel', {'task_id': h3, 'body': 'Withdrawn by its sender.'})
        call('second', 'ack', {'task_id': h1})
        f1 = call('second', 'claim', {'task_id': h1, 'seconds': 600})['task']['lease']['fence']
        g1 = helper('second', h1, f1, 'builder', 'Grandchild one')
        call('second', 'cancel', {'task_id': g1, 'body': 'Withdrawn one level down.'})
        g2 = helper('second', h1, f1, 'builder', 'Grandchild two')
        call('builder', 'ack', {'task_id': g2})
        f2 = call('builder', 'claim', {'task_id': g2, 'seconds': 600})['task']['lease']['fence']
        helper('builder', g2, f2, 'second', 'Great-grandchild')
        call('owner', 'cancel', {'task_id': h2, 'body': 'Withdrawn by the owner.'})
        other = call('owner', 'intake', {'source_namespace': 'estate', 'external_reference': 'env-other',
                     'message': msg('Second approved root')})['task']['id']
        call('owner', 'deliver', {'task_id': other})
        call('builder', 'note', {'authority_id': 'orch', 'body': 'Fictional lounge note.'})
    events, _, _ = store._snapshot(people['owner'])
    for event in events:
        if event['kind'] == 'authority_granted':
            event['payload']['authority']['artifact_root'] = 'ENVELOPE-ROOT'
    return events


def fold_digest(fold_fn, events):
    authorities, tasks = fold_fn(copy.deepcopy(events))
    return hashlib.sha256(canonical({'authorities': authorities, 'tasks': tasks}).encode('utf-8')).hexdigest()


def baseline_store_module():
    """common_room/store.py exactly as committed at the base of this build, or None without git."""
    from tests.helpers import ROOT
    try:
        shown = subprocess.run(['git', '-C', str(ROOT), 'show', ENVELOPE_BASELINE + ':common_room/store.py'],
                               capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if shown.returncode != 0:
        return None
    module = types.ModuleType('common_room._store_envelope_baseline')
    module.__package__ = 'common_room'
    module.__file__ = ENVELOPE_BASELINE + ':common_room/store.py'
    exec(compile(shown.stdout, module.__file__, 'exec'), module.__dict__)
    module.SOURCE_TEXT = shown.stdout
    return module


def fold_source(text):
    """The text of `def fold` up to the Store class that follows it."""
    start = text.index('\ndef fold(events):')
    return text[start:text.index('\nclass Store', start)]


class FoldCompatibilityTests(unittest.TestCase):
    def test_recorded_pre_cap_log_folds_to_the_recorded_state(self):
        events = json.loads(ENVELOPE_EVENTS_JSON)
        self.assertEqual(hashlib.sha256(canonical(events).encode('utf-8')).hexdigest(), ENVELOPE_LOG_SHA256)
        self.assertEqual(fold_digest(fold, events), ENVELOPE_STATE_SHA256)

    def test_the_same_actions_still_write_the_recorded_log(self):
        self.assertEqual(envelope_scenario(Store, case_dir('envelope-now')), json.loads(ENVELOPE_EVENTS_JSON))

    def test_baseline_and_current_fold_agree_and_fold_itself_is_unchanged(self):
        baseline = baseline_store_module()
        if baseline is None:
            self.skipTest('baseline commit ' + ENVELOPE_BASELINE + ' is not reachable through git here')
        recorded = json.loads(ENVELOPE_EVENTS_JSON)
        self.assertEqual(envelope_scenario(baseline.Store, case_dir('envelope-then')), recorded)
        self.assertEqual(canonical(baseline.fold(copy.deepcopy(recorded))), canonical(fold(copy.deepcopy(recorded))))
        import common_room.store as store_module
        self.assertEqual(fold_source(baseline.SOURCE_TEXT), fold_source(Path(store_module.__file__).read_text(encoding='utf-8')))
        # The committed log of the 2026-09-24 cancel build folds identically too.
        from tests.test_cancel_and_delegated_decisions import LEGACY_EVENTS_JSON
        legacy = json.loads(LEGACY_EVENTS_JSON)
        self.assertEqual(canonical(baseline.fold(copy.deepcopy(legacy))), canonical(fold(copy.deepcopy(legacy))))


# Recorded 2026-09-24 by driving envelope_scenario() through common_room/store.py exactly as
# committed at 62326a2 (before the 60-job cap), artifact root normalized. The state digest is
# canonical JSON of that baseline fold(). Do not regenerate with new code.
ENVELOPE_LOG_SHA256 = 'b7ce6bbf39cc4cb9e16588bdd48e34f63416565fec95e49f64895e358713922e'
ENVELOPE_STATE_SHA256 = 'c5d44afcd8c0ec0f07b2286d02c093e1e34cf49d0d90dd735abc2bf87d4e37e7'
ENVELOPE_EVENTS_JSON = r'''[
{"actor":"owner","kind":"authority_granted","payload":{"authority":{"artifact_root":"ENVELOPE-ROOT","capabilities":["handoff.record"],"id":"orch","members":["owner","builder","second","reviewer"],"owner":"owner","paused":false,"revoked":false,"root_id":"fixture","scope":"Fictional envelope replay","source_namespaces":["estate"]},"authority_id":"orch"},"seq":1,"task_id":null,"timestamp":1001.0},
{"actor":"owner","kind":"task_created","payload":{"authority_id":"orch","task":{"acceptance":"Reviewed.","authority_id":"orch","body":"Fictional envelope body.","builder":null,"chain_depth":0,"decision":null,"failed_attempts":0,"fence":0,"id":"t00000000000000000000000000000001","intake":{"external_reference":"env-root","fingerprint":"86c9444486724131ea5f6dc6e2affcb6a0a18c53e6181f65ac03659c3bb1824e","source_namespace":"estate"},"kind":"handoff.record","lease":null,"owner":"owner","parent_id":null,"recipient":"builder","refs":[],"result_body":"","result_refs":[],"reviewer":null,"sender":"owner","status":"draft","title":"Approved root"}},"seq":2,"task_id":"t00000000000000000000000000000001","timestamp":1002.0},
{"actor":"owner","kind":"task_deliver","payload":{"authority_id":"orch","changes":{"status":"delivered"}},"seq":3,"task_id":"t00000000000000000000000000000001","timestamp":1003.0},
{"actor":"builder","kind":"task_ack","payload":{"authority_id":"orch","changes":{"status":"acknowledged"}},"seq":4,"task_id":"t00000000000000000000000000000001","timestamp":1004.0},
{"actor":"builder","kind":"task_claim","payload":{"authority_id":"orch","changes":{"builder":"builder","fence":1,"lease":{"fence":1,"holder":"builder","until":1905.0},"status":"claimed"}},"seq":5,"task_id":"t00000000000000000000000000000001","timestamp":1005.0},
{"actor":"builder","kind":"task_created","payload":{"authority_id":"orch","parent_fence":1,"requested_from":"t00000000000000000000000000000001","task":{"acceptance":"Reviewed.","authority_id":"orch","body":"Fictional piece.","builder":null,"chain_depth":1,"decision":null,"failed_attempts":0,"fence":0,"id":"t00000000000000000000000000000002","intake":{"external_reference":"t00000000000000000000000000000002","fingerprint":"a3124e12f1f4e8709157dddf3fcd011fb0aa6916da1669ba7738111fb5519c2c","source_namespace":"room-helper"},"kind":"handoff.record","lease":null,"owner":"owner","parent_id":"t00000000000000000000000000000001","recipient":"second","refs":[],"result_body":"","result_refs":[],"reviewer":null,"sender":"builder","status":"delivered","title":"Helper 1"}},"seq":6,"task_id":"t00000000000000000000000000000002","timestamp":1006.0},
{"actor":"builder","kind":"task_created","payload":{"authority_id":"orch","parent_fence":1,"requested_from":"t00000000000000000000000000000001","task":{"acceptance":"Reviewed.","authority_id":"orch","body":"Fictional piece.","builder":null,"chain_depth":1,"decision":null,"failed_attempts":0,"fence":0,"id":"t00000000000000000000000000000003","intake":{"external_reference":"t00000000000000000000000000000003","fingerprint":"21b69b5905e691f68fb70d20ebe7e2efc1dec24348dbee6a52b618a1989840cc","source_namespace":"room-helper"},"kind":"handoff.record","lease":null,"owner":"owner","parent_id":"t00000000000000000000000000000001","recipient":"second","refs":[],"result_body":"","result_refs":[],"reviewer":null,"sender":"builder","status":"delivered","title":"Helper 2"}},"seq":7,"task_id":"t00000000000000000000000000000003","timestamp":1007.0},
{"actor":"builder","kind":"task_created","payload":{"authority_id":"orch","parent_fence":1,"requested_from":"t00000000000000000000000000000001","task":{"acceptance":"Reviewed.","authority_id":"orch","body":"Fictional piece.","builder":null,"chain_depth":1,"decision":null,"failed_attempts":0,"fence":0,"id":"t00000000000000000000000000000004","intake":{"external_reference":"t00000000000000000000000000000004","fingerprint":"15576a7fd38c425b7401a8a058f10fd01bd0f2575dc090f37c36ee6840564bd6","source_namespace":"room-helper"},"kind":"handoff.record","lease":null,"owner":"owner","parent_id":"t00000000000000000000000000000001","recipient":"second","refs":[],"result_body":"","result_refs":[],"reviewer":null,"sender":"builder","status":"delivered","title":"Helper 3"}},"seq":8,"task_id":"t00000000000000000000000000000004","timestamp":1008.0},
{"actor":"builder","kind":"task_cancel","payload":{"authority_id":"orch","changes":{"cancel_note":"Withdrawn by its sender.","cancelled_by":"builder","decision":null,"fence":1,"lease":null,"status":"cancelled"}},"seq":9,"task_id":"t00000000000000000000000000000004","timestamp":1009.0},
{"actor":"second","kind":"task_ack","payload":{"authority_id":"orch","changes":{"status":"acknowledged"}},"seq":10,"task_id":"t00000000000000000000000000000002","timestamp":1010.0},
{"actor":"second","kind":"task_claim","payload":{"authority_id":"orch","changes":{"builder":"second","fence":1,"lease":{"fence":1,"holder":"second","until":1611.0},"status":"claimed"}},"seq":11,"task_id":"t00000000000000000000000000000002","timestamp":1011.0},
{"actor":"second","kind":"task_created","payload":{"authority_id":"orch","parent_fence":1,"requested_from":"t00000000000000000000000000000002","task":{"acceptance":"Reviewed.","authority_id":"orch","body":"Fictional piece.","builder":null,"chain_depth":2,"decision":null,"failed_attempts":0,"fence":0,"id":"t00000000000000000000000000000005","intake":{"external_reference":"t00000000000000000000000000000005","fingerprint":"6bfc354dfcfdda6228f9833fc8206a21c9743ce6a8f8167d54d9f1eeadc064ef","source_namespace":"room-helper"},"kind":"handoff.record","lease":null,"owner":"owner","parent_id":"t00000000000000000000000000000002","recipient":"builder","refs":[],"result_body":"","result_refs":[],"reviewer":null,"sender":"second","status":"delivered","title":"Grandchild one"}},"seq":12,"task_id":"t00000000000000000000000000000005","timestamp":1012.0},
{"actor":"second","kind":"task_cancel","payload":{"authority_id":"orch","changes":{"cancel_note":"Withdrawn one level down.","cancelled_by":"second","decision":null,"fence":1,"lease":null,"status":"cancelled"}},"seq":13,"task_id":"t00000000000000000000000000000005","timestamp":1013.0},
{"actor":"second","kind":"task_created","payload":{"authority_id":"orch","parent_fence":1,"requested_from":"t00000000000000000000000000000002","task":{"acceptance":"Reviewed.","authority_id":"orch","body":"Fictional piece.","builder":null,"chain_depth":2,"decision":null,"failed_attempts":0,"fence":0,"id":"t00000000000000000000000000000006","intake":{"external_reference":"t00000000000000000000000000000006","fingerprint":"204905bcadcf77bc3096fb07c26454e8e686f97220384df95bda8e917a25c54f","source_namespace":"room-helper"},"kind":"handoff.record","lease":null,"owner":"owner","parent_id":"t00000000000000000000000000000002","recipient":"builder","refs":[],"result_body":"","result_refs":[],"reviewer":null,"sender":"second","status":"delivered","title":"Grandchild two"}},"seq":14,"task_id":"t00000000000000000000000000000006","timestamp":1014.0},
{"actor":"builder","kind":"task_ack","payload":{"authority_id":"orch","changes":{"status":"acknowledged"}},"seq":15,"task_id":"t00000000000000000000000000000006","timestamp":1015.0},
{"actor":"builder","kind":"task_claim","payload":{"authority_id":"orch","changes":{"builder":"builder","fence":1,"lease":{"fence":1,"holder":"builder","until":1616.0},"status":"claimed"}},"seq":16,"task_id":"t00000000000000000000000000000006","timestamp":1016.0},
{"actor":"builder","kind":"task_created","payload":{"authority_id":"orch","parent_fence":1,"requested_from":"t00000000000000000000000000000006","task":{"acceptance":"Reviewed.","authority_id":"orch","body":"Fictional piece.","builder":null,"chain_depth":3,"decision":null,"failed_attempts":0,"fence":0,"id":"t00000000000000000000000000000007","intake":{"external_reference":"t00000000000000000000000000000007","fingerprint":"2f04f41325f4a2392dc947ef168ba9bed84ee50149a4804ac2f906e643c22936","source_namespace":"room-helper"},"kind":"handoff.record","lease":null,"owner":"owner","parent_id":"t00000000000000000000000000000006","recipient":"second","refs":[],"result_body":"","result_refs":[],"reviewer":null,"sender":"builder","status":"delivered","title":"Great-grandchild"}},"seq":17,"task_id":"t00000000000000000000000000000007","timestamp":1017.0},
{"actor":"owner","kind":"task_cancel","payload":{"authority_id":"orch","changes":{"cancel_note":"Withdrawn by the owner.","cancelled_by":"owner","decision":null,"fence":1,"lease":null,"status":"cancelled"}},"seq":18,"task_id":"t00000000000000000000000000000003","timestamp":1018.0},
{"actor":"owner","kind":"task_created","payload":{"authority_id":"orch","task":{"acceptance":"Reviewed.","authority_id":"orch","body":"Fictional envelope body.","builder":null,"chain_depth":0,"decision":null,"failed_attempts":0,"fence":0,"id":"t00000000000000000000000000000008","intake":{"external_reference":"env-other","fingerprint":"552e7e24f8d40e86ab7fe00b155c8c25902955747eee63404df44d3e7fcd4aab","source_namespace":"estate"},"kind":"handoff.record","lease":null,"owner":"owner","parent_id":null,"recipient":"builder","refs":[],"result_body":"","result_refs":[],"reviewer":null,"sender":"owner","status":"draft","title":"Second approved root"}},"seq":19,"task_id":"t00000000000000000000000000000008","timestamp":1019.0},
{"actor":"owner","kind":"task_deliver","payload":{"authority_id":"orch","changes":{"status":"delivered"}},"seq":20,"task_id":"t00000000000000000000000000000008","timestamp":1020.0},
{"actor":"builder","kind":"note","payload":{"authority_id":"orch","body":"Fictional lounge note."},"seq":21,"task_id":null,"timestamp":1021.0}
]'''


if __name__ == '__main__':
    unittest.main()
