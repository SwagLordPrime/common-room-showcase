"""A2b (task t438e201de, 2026-10-30): the undoable mark in plan files and the board's forms, dot's review guide and
join-card line (BRIEF_A2B_UNDOABLE_MARK_2026-10-25.md).

A crew-plan/1 task may carry "undoable": true or false. plan-check refuses any other value with the room's own rule
(store.undoable_mark), naming the task key, and nothing is created; plan-approve (CLI and board) sends the mark in
that task's intake, so the mark is the keeper's. An unmarked plan task keeps the base's ids, payload and
fingerprint. A changed mark on re-approve is a conflict before anything is sent. The CLI confirm and the board's
Plans panel show each task's mark; "Leave a task" offers the mark only when it sends intake; "Ask a helper" offers
it only to a listed marker; every card shows one mark line. dot's guide has a review section tied to
crew/review-standins.json, and dot's join card one line pointing to it. Rollback: the base's code (5d5ee83) opens
a store this code wrote and keeps every mark; the base's plan-check refuses a marked plan.

Fictional rooms in .checks only (tests.helpers.case_dir); keys made here; no keeper vault, no live tree or relay.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # lets `python tests/test_a2b_undoable_mark.py` run from a tree root

import contextlib, inspect, io, json, re, shutil, subprocess, types, unittest
from types import SimpleNamespace
from unittest import mock

from common_room import activation, join_cards, plans
from common_room.board import state_body
from common_room.handoff_api import handoff_operation
from common_room.crew_client import CrewError
from common_room.protocol import Actor, RoomError
from tests.helpers import case_dir
from tests.test_board_plans import Browser
import common_room.crew as crew

TREE = Path(__file__).resolve().parents[1]
BASE = '5d5ee83d91f749bf498c66534158603a43c279c3'   # the switched head A2b is built on (the brief's dispatch record)
CONFIG = {'version': 1, 'actors': [
    {'id': 'cc', 'role': 'worker', 'label': 'Fictional Claude builder', 'session': 'fixture-cc'},
    {'id': 'room', 'role': 'worker', 'label': 'Fictional orchestrator', 'session': 'fixture-room'},
    {'id': 'dot', 'role': 'worker', 'label': 'Fictional dot', 'session': 'fixture-dot'},
    {'id': 'w2', 'role': 'worker', 'label': 'Fictional other worker', 'session': 'fixture-w2'},
    {'id': 'audit', 'role': 'reviewer', 'label': 'Fictional reviewer', 'session': 'fixture-audit'}],
    'authorities': [{'id': 'orch', 'members': ['keeper', 'cc', 'room', 'dot', 'w2', 'audit'], 'scope': 'Fictional orchestra scope'}]}
STANDIN = {'actor': 'dot', 'stands_for': 'audit', 'authority': 'orch', 'builders': ['cc', 'room'], 'markers': ['room']}
SEATS = ['cc', 'room', 'dot', 'w2', 'audit']
WORDS = {True: 'Undoable', False: "Can't undo", None: "Not marked (counts as can't undo)"}
BAD_MARKS = ('true', 1, 0, None, [], {})


def plan_doc(marks, plan_id='a2b-plan', reserve=()):
    """One task per entry of `marks` (True, False or None = no field), all for the Claude builder cc."""
    tasks = []
    for i, mark in enumerate(marks, 1):
        task = {'key': 't%d' % i, 'to': 'cc', 'title': 'Task %d' % i, 'body': 'Do thing %d.' % i,
                'acceptance': 'Thing %d looks right.' % i}
        if mark is not None:
            task['undoable'] = mark
        if 't%d' % i in reserve:
            task['reserve'] = True
        tasks.append(task)
    return {'protocol': 'crew-plan/1', 'plan_id': plan_id, 'title': 'Fictional A2b plan', 'scope': 'orch', 'tasks': tasks}


def git_show(path, rev=BASE):
    shown = subprocess.run(['git', '-C', str(TREE), 'show', rev + ':' + path], capture_output=True, text=True, encoding='utf-8')
    return shown.stdout if shown.returncode == 0 else None


def base_module(path, name):
    """The base's own module text, run as a module of this package (its relative imports resolve here)."""
    source = git_show(path)
    if source is None:
        raise unittest.SkipTest('the base ' + BASE[:7] + ' is not reachable through git here')
    mod = types.ModuleType('common_room._a2b_base_' + name)
    mod.__package__ = 'common_room'
    exec(compile(source, BASE[:7] + ':' + path, 'exec'), mod.__dict__)
    return mod


class Room(unittest.TestCase):
    """A scratch room with the stand-in, a plans root and the CLI in-process (as tests/test_crew_plans.py runs it)."""

    def setUp(self):
        self.root = case_dir('a2b-room')
        self.plans_root = case_dir('a2b-plans')
        self.room = activation.start_room(self.root, enrollment=CONFIG, selected=SEATS, plans_root=self.plans_root,
                                          review_standins=[STANDIN])
        self.addCleanup(self.room.close)
        self.store = self.room.store
        self.n = 0

    def who(self, actor):
        return self.store.people[actor]

    def run_(self, actor, action, payload):
        self.n += 1
        return self.store.execute(self.who(actor), action, payload, 'a2b-%d' % self.n)

    def cli(self, actor, *args, stdin=None):
        argv = ['--credential', str(self.root / (actor + '-credential.json')), '--json'] + list(args)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            if stdin is not None:
                stack.enter_context(mock.patch('sys.stdin', new=stdin))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            code = crew.main(argv)
        return code, out.getvalue(), err.getvalue()

    def yes(self):
        return SimpleNamespace(isatty=lambda: True, readline=lambda: 'yes\n')

    def write_plan(self, plan, folder='a2b-plan', name='PLAN.json'):
        target = Path(self.plans_root) / folder
        target.mkdir(parents=True, exist_ok=True)
        (target / name).write_text(json.dumps(plan), encoding='utf-8')
        return target / name

    def cli_approve(self, plan, **kw):
        path = self.write_plan(plan, **kw)
        for receipt in path.parent.glob('*.receipt.json'):
            receipt.unlink()
        return self.cli('keeper', 'plan-approve', str(path), stdin=self.yes())

    def tasks(self, actor='keeper'):
        return {t['title']: t for t in state_body(self.room.server, self.who(actor))['tasks']}

    def build(self, tid, builder='cc'):
        self.run_(builder, 'ack', {'task_id': tid})
        fence = self.run_(builder, 'claim', {'task_id': tid, 'seconds': 1800})['task']['lease']['fence']
        ref = handoff_operation(self.room.server, self.who(builder), 'stage_text', {'task_id': tid, 'fence': fence, 'text': 'Fictional result.'})
        self.run_(builder, 'finish', {'task_id': tid, 'fence': fence, 'body': 'Done.', 'refs': [{'path': ref['path'], 'sha256': ref['sha256']}]})
        return tid

    def dot_review(self, tid):
        try:
            return 'done', self.run_('dot', 'review', {'task_id': tid, 'accepted': True, 'note': 'A fictional review.'})['task']['status']
        except RoomError as exc:
            return exc.code, str(exc)

    def relay_state(self, actor):
        from common_room.relay import Relay
        relay = Relay(self.root, [actor], clock=self.room.server.clock)
        (self.root / 'relay' / actor / 'requests' / 's1.json').write_text(json.dumps({'request_id': 's1', 'kind': 'state'}), encoding='utf-8')
        relay.process_once()
        return json.loads((self.root / 'relay' / actor / 'relay-state.json').read_text(encoding='utf-8'))


# ---------------------------------------------------------------------------------------------------------------
# 1. Plan mark round trip (CLI and the board's server path)
# ---------------------------------------------------------------------------------------------------------------

class PlanMarkTests(Room):
    def check_three(self):
        tasks = self.tasks()
        self.assertIs(tasks['Task 1'].get('undoable'), True)
        self.assertIs(tasks['Task 2'].get('undoable'), False)
        self.assertNotIn('undoable', tasks['Task 3'])
        relay = {t['title']: t for t in self.relay_state('dot')['tasks']}
        self.assertIs(relay['Task 1'].get('undoable'), True)
        self.assertIs(relay['Task 2'].get('undoable'), False)
        self.assertNotIn('undoable', relay['Task 3'])
        answers = {title: self.dot_review(self.build(tasks[title]['id'])) for title in ('Task 1', 'Task 2', 'Task 3')}
        self.assertEqual(answers['Task 1'], ('done', 'done'))
        self.assertEqual(answers['Task 2'][0], 'forbidden')
        self.assertIn("can't-undo", answers['Task 2'][1])
        self.assertEqual(answers['Task 3'][0], 'forbidden')
        self.assertIn('unmarked', answers['Task 3'][1])

    def test_cli_plan_approve_creates_each_task_with_its_mark_and_dot_is_let_in_only_on_undoable(self):
        code, out, err = self.cli_approve(plan_doc([True, False, None]))
        self.assertEqual(code, 0, err)
        self.check_three()
        code, out, err = self.cli_approve(plan_doc([True, False, None]))   # the unchanged plan again: nothing new
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.tasks()), 3)

    def test_board_plan_approve_server_path_creates_each_task_with_its_mark(self):
        """Server path (plans.board_approve over loopback HTTP, as tests/test_board_plans.py drives it), not a browser.
        The Plans panel posts the same {id, sha256, confirm} as at the base: the mark comes from the plan file."""
        path = self.write_plan(plan_doc([True, False, None]))
        browser = Browser(self.room)
        self.assertEqual(browser.login(self.root / 'keeper-credential.json'), 'keeper')
        status, listing, _ = browser.request('GET', '/api/plans')
        entry = next(p for p in listing['plans'] if p['id'] == 'a2b-plan/PLAN.json')
        self.assertEqual([t.get('undoable') for t in entry['tasks']], [True, False, None])
        status, body, _ = browser.request('POST', '/api/plan-approve', {'id': entry['id'], 'sha256': entry['sha256'], 'confirm': 'a2b-plan'})
        self.assertEqual(status, 200, body)
        self.check_three()
        for receipt in path.parent.glob('*.receipt.json'):
            receipt.unlink()
        status, body, _ = browser.request('POST', '/api/plan-approve', {'id': entry['id'], 'sha256': entry['sha256'], 'confirm': 'a2b-plan'})
        self.assertEqual(status, 200, body)
        self.assertEqual(len(self.tasks()), 3)

    def test_a_marked_reserve_task_is_created_with_its_mark(self):
        code, out, err = self.cli_approve(plan_doc([True, None], reserve=('t1',)))
        self.assertEqual(code, 0, err)
        self.assertIs(self.tasks()['Task 1']['undoable'], True)
        self.assertEqual(json.loads(out)['reserve'], ['t1'])


# ---------------------------------------------------------------------------------------------------------------
# 2. plan-check refusals
# ---------------------------------------------------------------------------------------------------------------

class RefusalTests(Room):
    def test_every_non_boolean_mark_is_refused_with_the_rooms_words_naming_the_task_and_nothing_is_created(self):
        for bad in BAD_MARKS:
            with self.subTest(bad=bad):
                plan = plan_doc([True, None])
                plan['tasks'][1]['undoable'] = bad
                with self.assertRaises(CrewError) as caught:
                    plans.validate_plan(plan)
                self.assertEqual(str(caught.exception), 'Task t2: The undoable mark must be true or false.')
                path = self.write_plan(plan)
                code, out, err = self.cli('keeper', 'plan-check', str(path))
                self.assertNotEqual(code, 0)
                self.assertIn('Task t2: The undoable mark must be true or false.', err)
                code, out, err = self.cli_approve(plan)
                self.assertNotEqual(code, 0)
                self.assertIn('Task t2: The undoable mark must be true or false.', err)
        self.assertEqual(self.tasks(), {})

    def test_the_store_rule_is_one_function_used_at_intake_request_help_and_plans(self):
        from common_room import store
        self.assertEqual(store.undoable_mark({}), {})
        self.assertEqual(store.undoable_mark({'undoable': False}), {'undoable': False})
        for bad in BAD_MARKS:
            with self.assertRaises(RoomError) as caught:
                store.undoable_mark({'undoable': bad})
            self.assertEqual((caught.exception.code, str(caught.exception)), ('invalid', 'The undoable mark must be true or false.'))
        source = inspect.getsource(store.Store._apply)
        self.assertEqual(source.count('undoable_mark(p)'), 2)
        self.assertNotIn("type(p['undoable'])", source)


# ---------------------------------------------------------------------------------------------------------------
# 3. Id invariants: an unmarked plan task is exactly the base's
# ---------------------------------------------------------------------------------------------------------------

class Stub:
    def __init__(self):
        self.sent = []

    def command(self, action, payload, request_id):
        self.sent.append((action, json.loads(json.dumps(payload)), request_id))
        return {'task': {'id': 't' + '1' * 32}}

    def state(self):
        return {'tasks': [{'id': 't' + '1' * 32, 'status': 'delivered'}]}


class IdInvariantTests(unittest.TestCase):
    def sent(self, module, plan):
        stub = Stub()
        module.approve_plan(stub, module.validate_plan(plan), {'tasks': []})
        return stub.sent

    def test_an_unmarked_plan_sends_exactly_what_the_base_sends(self):
        base = base_module('common_room/plans.py', 'plans')
        plan = plan_doc([None, None])
        plan['tasks'][0].update(tier='sonnet', effort='high')
        self.assertEqual(self.sent(plans, plan), self.sent(base, plan))
        self.assertEqual(plans.validate_plan(plan), base.validate_plan(plan))
        ids = [request_id for _, _, request_id in self.sent(plans, plan)]
        self.assertEqual(ids, ['plan-a2b-plan-t1', 'plan-deliver-a2b-plan-t1', 'plan-a2b-plan-t2', 'plan-deliver-a2b-plan-t2'])
        self.assertEqual(self.sent(plans, plan)[0][1]['external_reference'], 'a2b-plan-t1')

    def test_a_marked_plan_sends_the_mark_in_intake_only_and_keeps_the_same_ids(self):
        sent = self.sent(plans, plan_doc([True, False]))
        unmarked = self.sent(plans, plan_doc([None, None]))
        self.assertEqual([r for _, _, r in sent], [r for _, _, r in unmarked])
        self.assertEqual([p.get('undoable') for a, p, _ in sent if a == 'intake'], [True, False])
        self.assertTrue(all('undoable' not in p for a, p, _ in sent if a == 'deliver'))

    def test_an_unmarked_plan_tasks_intake_fingerprint_equals_the_bases(self):
        base = base_module('common_room/store.py', 'store')
        people = {'keeper': Actor('keeper', 'owner'), 'cc': Actor('cc', 'worker'), 'audit': Actor('audit', 'reviewer')}
        payload = self.sent(plans, plan_doc([None]))[0][1]
        prints = []
        for module in (base, __import__('common_room.store', fromlist=['Store'])):
            root = case_dir('a2b-fingerprint')
            (root / 'art').mkdir()
            st = module.Store(root / 'room.sqlite', people, {'fixture': root / 'art'})
            st.execute(people['keeper'], 'grant_handoff', {'id': 'orch', 'members': ['keeper', 'cc', 'audit'], 'capabilities': ['handoff.record'],
                                                           'root_id': 'fixture', 'scope': 'Fingerprint scope', 'source_namespaces': ['estate']}, 'g')
            prints.append(st.execute(people['keeper'], 'intake', payload, 'i')['task']['intake']['fingerprint'])
        self.assertEqual(prints[0], prints[1])

    def test_sw2is_four_pinned_id_cases_pass_unchanged(self):
        from tests import test_sw2_integration
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(test_sw2_integration.IdTests)
        result = unittest.TestResult()
        suite.run(result)
        self.assertEqual((result.testsRun, len(result.failures), len(result.errors)), (2, 0, 0), result.failures + result.errors)


# ---------------------------------------------------------------------------------------------------------------
# 4. Re-approve conflict, 5. plan views
# ---------------------------------------------------------------------------------------------------------------

class ConflictTests(Room):
    def test_a_changed_mark_is_a_conflict_before_anything_is_sent_on_the_cli_and_the_board(self):
        self.assertEqual(self.cli_approve(plan_doc([True, None]))[0], 0)
        for changed in ([False, None], [None, None], [True, True], [True, False]):
            with self.subTest(changed=changed):
                code, out, err = self.cli_approve(plan_doc(changed))
                self.assertNotEqual(code, 0)
                self.assertIn('already approved with different content', err)
                self.assertEqual(len(self.tasks()), 2)
        self.write_plan(plan_doc([False, None]))
        browser = Browser(self.room)
        browser.login(self.root / 'keeper-credential.json')
        entry = next(p for p in browser.request('GET', '/api/plans')[1]['plans'] if p['id'] == 'a2b-plan/PLAN.json')
        self.assertEqual(entry['conflicts'], ['t1'])
        status, body, _ = browser.request('POST', '/api/plan-approve', {'id': entry['id'], 'sha256': entry['sha256'], 'confirm': 'a2b-plan'})
        self.assertNotEqual(status, 200)
        self.assertEqual(len(self.tasks()), 2)
        self.assertEqual(self.cli_approve(plan_doc([True, None]))[0], 0)        # the unchanged marked plan: idempotent
        self.assertEqual(len(self.tasks()), 2)


class PlanViewTests(Room):
    def test_the_cli_confirm_shows_each_tasks_mark_in_the_three_wordings(self):
        code, out, err = self.cli_approve(plan_doc([True, False, None]))
        self.assertEqual(code, 0, err)
        lines = [l.strip() for l in err.splitlines() if l.strip().startswith('mark: ')]
        self.assertEqual(lines, ['mark: ' + WORDS[True], 'mark: ' + WORDS[False], 'mark: ' + WORDS[None]])
        summary = plans.plan_summary(plans.validate_plan(plan_doc([None])), '0' * 64)
        self.assertEqual(summary[summary.index('     acceptance: Thing 1 looks right.') + 1], '     mark: ' + WORDS[None])

    def test_the_board_listing_carries_each_mark_and_the_plans_panel_draws_it(self):
        self.write_plan(plan_doc([True, False, None]))
        listing = plans.board_listing(self.plans_root, state_body(self.room.server, self.who('keeper')))
        self.assertEqual([t.get('undoable') for t in listing['plans'][0]['tasks']], [True, False, None])
        self.assertEqual([plans.mark_words(t) for t in listing['plans'][0]['tasks']], [WORDS[True], WORDS[False], WORDS[None]])
        js = (TREE / 'web' / 'board.js').read_text(encoding='utf-8')
        self.assertIn("box.append(el('p','Mark: '+markWords(t.undoable),'plan-mark'));", js)


# ---------------------------------------------------------------------------------------------------------------
# 6-7. Forms and card: the pure functions in web/board.js, run in node; source checks for the wiring
# ---------------------------------------------------------------------------------------------------------------

NODE_CHECK = r"""
const assert=require('assert');
BLOCK
assert.deepStrictEqual(MARK_CHOICES.map(c=>c.value),['unmarked','true','false']);
assert.deepStrictEqual(MARK_CHOICES.map(c=>c.label),["Not marked (counts as can't undo)",'Undoable',"Can't undo"]);
assert.strictEqual(markWords(true),'Undoable');assert.strictEqual(markWords(false),"Can't undo");
assert.strictEqual(markWords(undefined),"Not marked (counts as can't undo)");assert.strictEqual(markWords(null),"Not marked (counts as can't undo)");
assert.deepStrictEqual(markField('unmarked'),{});assert.deepStrictEqual(markField('true'),{undoable:true});assert.deepStrictEqual(markField('false'),{undoable:false});
assert.deepStrictEqual(markField(undefined),{});
const msg={title:'T',body:'B',recipient:'cc',authority_id:'orch',kind:'handoff.record',acceptance:'A'};
assert.deepStrictEqual(intakePayload('estate','ref',msg,'unmarked'),{source_namespace:'estate',external_reference:'ref',message:msg});
assert.deepStrictEqual(intakePayload('estate','ref',msg,'true'),{source_namespace:'estate',external_reference:'ref',message:msg,undoable:true});
assert.deepStrictEqual(intakePayload('estate','ref',msg,'false'),{source_namespace:'estate',external_reference:'ref',message:msg,undoable:false});
const f={recipient:'w2',title:'H',body:'B',acceptance:'A'};
assert.deepStrictEqual(helperPayload('t1',3,f),{task_id:'t1',fence:3,recipient:'w2',title:'H',body:'B',acceptance:'A'});
assert.deepStrictEqual(helperPayload('t1',3,{...f,mark:'unmarked'}),{task_id:'t1',fence:3,recipient:'w2',title:'H',body:'B',acceptance:'A'});
assert.deepStrictEqual(helperPayload('t1',3,{...f,mark:'true'}),{task_id:'t1',fence:3,recipient:'w2',title:'H',body:'B',acceptance:'A',undoable:true});
assert.deepStrictEqual(helperPayload('t1',3,{...f,mark:'false'}),{task_id:'t1',fence:3,recipient:'w2',title:'H',body:'B',acceptance:'A',undoable:false});
const st={authorities:[{id:'orch',review_standins:[{actor:'dot',markers:['room'],builders:['cc']}]},{id:'other',review_standins:[]}]};
assert.strictEqual(isMarker(st,'orch','room'),true);assert.strictEqual(isMarker(st,'orch','cc'),false);assert.strictEqual(isMarker(st,'orch','dot'),false);
assert.strictEqual(isMarker(st,'other','room'),false);assert.strictEqual(isMarker({authorities:[{id:'orch'}]},'orch','room'),false);
assert.strictEqual(isMarker(undefined,'orch','room'),false);
console.log('A2B NODE PASS');
"""


def mark_block():
    js = (TREE / 'web' / 'board.js').read_text(encoding='utf-8')
    found = re.search(r'^// a2b-mark-begin.*?\n(.*?)^// a2b-mark-end', js, re.S | re.M)
    if not found:
        raise AssertionError('board.js has no a2b-mark block')
    return found.group(1)


class BoardFormTests(unittest.TestCase):
    def node(self):
        import os
        node = os.environ.get('COMMON_ROOM_NODE') or shutil.which('node')   # as tests/test_board_plans.py finds it
        if not node:
            self.skipTest('node is not on PATH')
        return node

    def test_the_mark_functions_build_the_right_payloads_and_lines_in_node(self):
        node = self.node()
        run = subprocess.run([node, '-e', NODE_CHECK.replace('BLOCK', mark_block())], capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('A2B NODE PASS', run.stdout)

    def test_node_check_passes_on_board_js(self):
        node = self.node()
        run = subprocess.run([node, '--check', str(TREE / 'web' / 'board.js')], capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_the_forms_and_the_card_use_those_functions(self):
        """A source check: it shows the wiring, not a browser running it (REPORT.md says "not seen in a browser")."""
        js = (TREE / 'web' / 'board.js').read_text(encoding='utf-8')
        html = (TREE / 'web' / 'index.html').read_text(encoding='utf-8')
        handoff = re.search(r'<div id="handoff-fields" hidden>(.*?)</div>', html, re.S).group(1)
        select = re.search(r'<select id="task-mark">(.*?)</select>', handoff, re.S)
        self.assertTrue(select, 'the mark choice sits inside #handoff-fields, shown only when the form sends intake')
        self.assertEqual(re.findall(r'<option value="([^"]+)"', select.group(1)), ['unmarked', 'true', 'false'])
        self.assertEqual(re.findall(r'>([^<]+)</option>', select.group(1)), ["Not marked (counts as can't undo)", 'Undoable', "Can't undo"])
        self.assertIn('<option value="unmarked" selected>', select.group(1))
        self.assertEqual(html.count('id="task-mark"'), 1)
        submit = re.search(r"^\$\('task-form'\)\.onsubmit=.*$", js, re.M).group(0)
        self.assertIn("handoff?intakePayload($('source-namespace').value,$('external-reference').value,msg,$('task-mark').value):msg", submit)
        self.assertIn("$('task-mark').value='unmarked';", submit)      # never carried over to the next task
        helper = re.search(r"cardForm\(c,'Ask a helper'.*$", js, re.M).group(0)
        self.assertIn("...(isMarker(state,t.authority_id,state.actor.id)?[{name:'mark',label:'Can this work be undone?',options:MARK_CHOICES}]:[])", helper)
        self.assertIn("command('request_help',helperPayload(t.id,t.lease.fence,fields))", helper)
        self.assertEqual(js.count("el('small','Mark: '+markWords(t.undoable),'task-mark')"), 1)    # one line on every card

    def test_the_create_path_is_the_bases(self):
        base = git_show('web/board.js')
        if base is None:
            self.skipTest('the base is not reachable through git here')
        msg = re.compile(r"const msg=\{title:\$\('task-title'\)\.value.*?acceptance:\$\('acceptance'\)\.value\};")
        js = (TREE / 'web' / 'board.js').read_text(encoding='utf-8')
        self.assertEqual(msg.search(js).group(0), msg.search(base).group(0))
        self.assertIn("command(handoff?'intake':'create',", js)
        self.assertTrue(js.split("command(handoff?'intake':'create',")[1].split(';')[0].endswith(':msg)'))


# ---------------------------------------------------------------------------------------------------------------
# 8. Join card and guide
# ---------------------------------------------------------------------------------------------------------------

CARD_ENROLLMENT = {'version': 1, 'actors': [
    {'id': 'dot', 'role': 'worker', 'label': 'Fictional dot', 'session': 'fixture-dot'},
    {'id': 'claude-room', 'role': 'worker', 'label': 'Fictional orchestrator', 'session': 'fixture-room'},
    {'id': 'claude-code', 'role': 'worker', 'label': 'Fictional builder', 'session': 'fixture-cc'},
    {'id': 'ferrule', 'role': 'worker', 'label': 'Fictional builder', 'session': 'fixture-f'},
    {'id': 'astra-audit', 'role': 'reviewer', 'label': 'Fictional reviewer', 'session': 'fixture-aa'},
    {'id': 'notify', 'role': 'observer', 'label': 'Fictional observer', 'session': 'fixture-n'}],
    'authorities': [{'id': 'orchestra', 'members': ['keeper', 'dot', 'claude-room', 'claude-code', 'ferrule', 'astra-audit', 'notify'],
                     'scope': 'Fictional orchestra'}]}
DOT_LINE = ('Reviewing: before you review anything, read "Reviewing as astra-audit\'s stand-in (undoable work only)" in '
            'DOT_SEAT_2026-10-05.md.')
SECTION = "## Reviewing as astra-audit's stand-in (undoable work only)"


def section(text):
    found = re.search('^' + re.escape(SECTION) + r'\n(.*?)(?=^## )', text, re.S | re.M)
    return found.group(1) if found else None


class JoinCardAndGuideTests(unittest.TestCase):
    def test_dots_card_has_the_one_line_both_ways_and_every_other_card_is_the_bases(self):
        base = base_module('common_room/join_cards.py', 'join_cards')
        for python in (True, False):
            for seat in ('dot', 'claude-room', 'claude-code', 'ferrule', 'astra-audit'):
                with self.subTest(seat=seat, python=python):
                    new = join_cards.render_card(seat, json.loads(json.dumps(CARD_ENROLLMENT)), python=python)
                    old = base.render_card(seat, json.loads(json.dumps(CARD_ENROLLMENT)), python=python)
                    if seat != 'dot':
                        self.assertEqual(new, old)
                        continue
                    self.assertEqual(new.count(DOT_LINE), 1)
                    lines = new.splitlines()
                    self.assertTrue(lines[lines.index(DOT_LINE) - 1].startswith('Read first: '))
                    self.assertEqual(new.replace(DOT_LINE + '\n', '', 1), old)
                    self.assertNotIn('"action"', DOT_LINE)
                    self.assertNotIn('request_id', DOT_LINE)

    def test_dots_guide_section_names_exactly_the_shipped_stand_in(self):
        text = (TREE / 'DOT_SEAT_2026-10-05.md').read_text(encoding='utf-8')
        body = section(text)
        self.assertIsNotNone(body, 'DOT_SEAT has no review section')
        self.assertLess(text.index('## Job 2:'), text.index(SECTION))
        self.assertLess(text.index(SECTION), text.index('## Never'))
        shipped = json.loads((TREE / 'crew' / 'review-standins.json').read_text(encoding='utf-8'))['review_standins']
        self.assertEqual(len(shipped), 1)
        standin = shipped[0]
        self.assertEqual(standin['actor'], 'dot')
        which = re.search(r'^- \*\*Which tasks\.\*\*(.*?)(?=^- \*\*)', body, re.S | re.M).group(1)
        self.assertIn('scope `' + standin['authority'] + '`', which)
        listed = re.search(r"builders list: (.*?)\.", ' '.join(which.split())).group(1)
        self.assertEqual(re.split(r', (?:or )?| or ', listed), standin['builders'])
        self.assertIn(standin['stands_for'] + "'s would", ' '.join(which.split()))
        marks = ' '.join(re.search(r'^- \*\*Who marks\.\*\*(.*?)(?=^- \*\*)', body, re.S | re.M).group(1).split())
        self.assertIn('(today ' + ', '.join(standin['markers']) + ')', marks)
        self.assertIn('Never you, and never the task\'s builder.', marks)
        self.assertIn("A task with no mark counts as can't undo, so you may not review it.", marks)
        self.assertIn('"kind": "paperwork"', body)
        self.assertIn('"action": "review"', body)
        self.assertIn('CREW_BRIEF_D10_AMENDMENT_R3.md', body)

    def test_dots_guide_changed_only_by_the_new_section(self):
        base = git_show('DOT_SEAT_2026-10-05.md')
        if base is None:
            self.skipTest('the base is not reachable through git here')
        text = (TREE / 'DOT_SEAT_2026-10-05.md').read_text(encoding='utf-8')
        start = text.index(SECTION)
        self.assertEqual(text[:start] + text[text.index('## Never', start):], base)


# ---------------------------------------------------------------------------------------------------------------
# 9. Rollback: the base's code on a store this code wrote; the base's plan-check on a marked plan
# ---------------------------------------------------------------------------------------------------------------

class RollbackTests(Room):
    def test_the_bases_store_opens_a_store_this_code_wrote_and_keeps_every_mark_with_the_same_standin_answers(self):
        self.assertEqual(self.cli_approve(plan_doc([True, False]))[0], 0)         # a plan-approved marked task
        planned = self.tasks()['Task 1']['id']
        board = self.run_('keeper', 'intake', {'source_namespace': 'estate', 'external_reference': 'board-1', 'undoable': True,
                                                'message': {'title': 'Board task', 'body': 'B', 'recipient': 'cc', 'authority_id': 'orch',
                                                            'kind': 'handoff.record', 'acceptance': 'A'}})['task']['id']
        self.run_('keeper', 'deliver', {'task_id': board})      # a board-shaped marked intake (the form's intakePayload)
        parent = self.run_('keeper', 'intake', {'source_namespace': 'estate', 'external_reference': 'parent-1', 'message': {
            'title': 'Parent', 'body': 'B', 'recipient': 'room', 'authority_id': 'orch', 'kind': 'handoff.record', 'acceptance': 'A'}})['task']['id']
        self.run_('keeper', 'deliver', {'task_id': parent})
        self.run_('room', 'ack', {'task_id': parent})
        fence = self.run_('room', 'claim', {'task_id': parent, 'seconds': 1800})['task']['lease']['fence']
        helper = self.run_('room', 'request_help', {'task_id': parent, 'fence': fence, 'recipient': 'cc', 'title': 'Helper',
                                                    'body': 'B', 'acceptance': 'A', 'undoable': True})['task']['id']   # a marker's marked helper
        for tid in (planned, board, helper):
            self.build(tid)
        cant = self.tasks()['Task 2']['id']
        self.build(cant)
        new_answers = {tid: self.dot_review(tid)[0] for tid in (cant,)}
        base = base_module('common_room/store.py', 'store')
        self.room.close()
        people, roots = dict(self.store.people), dict(self.store.roots)
        old = base.Store(self.root / 'room.sqlite', people, roots, standins=[STANDIN])   # the base Store checks the entries itself
        keeper = people['keeper']
        self.assertEqual([old.task(keeper, t).get('undoable') for t in (planned, board, helper, cant)], [True, True, True, False])
        for n, tid in enumerate((planned, board, helper)):
            self.assertEqual(old.execute(people['dot'], 'review', {'task_id': tid, 'accepted': True, 'note': 'n'}, 'old-%d' % n)['task']['status'], 'done')
        with self.assertRaises(RoomError) as caught:
            old.execute(people['dot'], 'review', {'task_id': cant, 'accepted': True, 'note': 'n'}, 'old-cant')
        self.assertEqual((caught.exception.code, new_answers[cant]), ('forbidden', 'forbidden'))

    def test_the_bases_plan_check_refuses_a_marked_plan_and_a_stripped_re_approve_is_refused_part_way(self):
        base = base_module('common_room/plans.py', 'plans')
        with self.assertRaises(CrewError) as caught:
            base.validate_plan(plan_doc([True]))
        self.assertIn('must have exactly', str(caught.exception))
        base.validate_plan(plan_doc([None]))                                  # an unmarked plan is still the base's
        # After a rollback, an already-approved marked plan stripped of its marks: the base's conflict check does not
        # compare marks, so it passes; the room then refuses that task's intake (a different brief, same reference).
        self.assertEqual(self.cli_approve(plan_doc([None, True]))[0], 0)
        stripped = base.validate_plan(plan_doc([None, None]))
        state = state_body(self.room.server, self.who('keeper'))
        self.assertEqual(base.check_previous_approval_conflicts(stripped, state), [])
        sent = []

        class Client:
            def command(inner, action, payload, request_id):   # the plan's own request ids, so repeats answer from receipts
                sent.append(request_id)
                return self.store.execute(self.who('keeper'), action, payload, request_id)

            def state(inner):
                return state_body(self.room.server, self.who('keeper'))
        with self.assertRaises(RoomError) as caught:
            base.approve_plan(Client(), stripped, state)
        self.assertEqual(caught.exception.code, 'idempotency')
        self.assertEqual(sent, ['plan-a2b-plan-t1', 'plan-deliver-a2b-plan-t1', 'plan-a2b-plan-t2'])   # t1 went through, t2 refused


if __name__ == '__main__':
    unittest.main(argv=sys.argv[:1])    # evidence.py passes the tree root as an argument; it names no test
