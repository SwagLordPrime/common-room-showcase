"""Switch 2 integration (task t0fb0b17f3, 2026-10-24): A1, A2, A4a, A4c and A4d merged onto 356bbac, checked together.

What the merges had to keep, each from both sides (RESOLUTION_LOG.md in the task's OUTPUT folder):
  * request ids and brief fingerprints: unmarked and untagged equal 356bbac's; mark only equals A2's (646c839); tags
    only equal A4a's (12a01d5); a mark and tags together put the mark first, then the tags (pinned golden values,
    computed through a stub client from each head's own code; GOLDEN_<head>.txt);
  * the store takes the undoable mark (A2) and the tier tag (A4a) together, at creation only, and checks the mark first;
  * a start takes both review stand-ins (A2) and seat tier labels (A4a); each file is read only for the default root;
  * the state body carries A1's questions and asked and A4c's room_renewed, and a question read mark leaves the
    renewal note in place.
Every room here is a scratch room under this tree's .checks, with keys these tests make. activation's ROOT,
STANDINS_FILE and SEAT_TIERS_FILE are patched to a scratch tree for the start tests and restored after each; no test
reads or writes the real ROOT\\.runtime or crew\\seat-tiers.json. The CLI id checks use a stub client: no socket.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # lets `python tests/test_sw2_integration.py` run from a tree root

import contextlib, io, json, unittest
from unittest import mock

import common_room.crew as crew
from common_room import activation
from common_room import handoff_room
from common_room.handoff_api import handoff_operation
from common_room.protocol import Actor, RoomError
from common_room.store import Store
from tests.helpers import case_dir
from tests.test_crew_watch import FakeClock

CONFIG = {'version': 1, 'actors': [
    {'id': 'cc', 'role': 'worker', 'label': 'Fictional Claude builder', 'session': 'fixture-cc'},
    {'id': 'room', 'role': 'worker', 'label': 'Fictional orchestrator', 'session': 'fixture-room'},
    {'id': 'dot', 'role': 'worker', 'label': 'Fictional dot', 'session': 'fixture-dot'},
    {'id': 'w2', 'role': 'worker', 'label': 'Fictional other worker', 'session': 'fixture-w2'},
    {'id': 'audit', 'role': 'reviewer', 'label': 'Fictional reviewer', 'session': 'fixture-audit'}],
    'authorities': [{'id': 'orch', 'members': ['keeper', 'cc', 'room', 'dot', 'w2', 'audit'], 'scope': 'Fictional orchestra scope'}]}
SEATS = ['cc', 'room', 'dot', 'w2', 'audit']
STANDIN = {'actor': 'dot', 'stands_for': 'audit', 'authority': 'orch', 'builders': ['cc', 'room'], 'markers': ['room']}
LABELS = {'cc': 'sonnet', 'audit': 'opus'}

# Golden values (GOLDEN_356bbac1e8…, GOLDEN_646c8392c0…, GOLDEN_12a01d5a27… in the task's OUTPUT folder), for the fixed
# inputs below: CR4 = crew delegate's default request id, CR5 = crew intake's default request id, ST5 = the store's
# intake fingerprint. 'none' is the same on all three heads; each other single case comes from the head that added it.
GOLDEN = {
    'none': ('help-t00000000000000000000000000000000-f3-5ef7a520', 'intake-916f38d25b9269e5dd9407c8',
             '545318cdefe2470f580d3c78afa714fbe4ad8f4cfbf0fa0a11860b4abcc59519'),
    'undoable': ('help-t00000000000000000000000000000000-f3-ea8a821c', 'intake-cbc012b331205dd1f67b0b8c',
                 '36d972d89ae76aff0d25513b72c178c5f102a085f26e38c028599224d8724012'),
    'cant-undo': ('help-t00000000000000000000000000000000-f3-8d3e8eab', 'intake-9fb1c68f2598f841ada5bc0b',
                  'e2ed88b6671c0fafeaee4e1cb3212c57f14c081de9b53d11a33a1f1cf7636701'),
    'tier': ('help-t00000000000000000000000000000000-f3-832070de', 'intake-f9afbe62638400da541a7bea',
             '1bea505298af914662760866e87b190fb664a7afca77e0fa63057497809683a6'),
    'effort': ('help-t00000000000000000000000000000000-f3-dbad1333', 'intake-5e272d9ab3abf0d121d1a3c9',
               'dd1ec8036ec39279b75d2ecf51ab1de382f89343f11eb6e9fc08d9e48e835112'),
    'tier+effort': ('help-t00000000000000000000000000000000-f3-a16f188a', 'intake-8f5766e2db300cc899f58742',
                    '483ddf9db6039cd92b20e647ef6bb51275c1c7f55fea27ce8e8da30d2a52da99'),
}
# A mark and tags together (only the merged code has both): the mark, then the sorted tags.
BOTH = {
    'undoable+tier': ('help-t00000000000000000000000000000000-f3-f844cffe', 'intake-c08ac001727af617d430eb9a',
                      'cda3f9b705e02e53aa3526cecb2e36a90ddf920ddc3d04ecfdf48e9f8f87130e'),
    'cant-undo+tier+effort': ('help-t00000000000000000000000000000000-f3-9141c9df', 'intake-75c34183c6d1a9ec9c57a93e',
                              '94ed9447cf0599e3bf484a8fc6f3f1f92333f4a8d872a39194bb9402fdbc2c37'),
}
FLAGS = {'none': [], 'undoable': ['--undoable'], 'cant-undo': ['--cant-undo'], 'tier': ['--tier', 'sonnet'],
         'effort': ['--effort', 'high'], 'tier+effort': ['--tier', 'sonnet', '--effort', 'high'],
         'undoable+tier': ['--undoable', '--tier', 'sonnet'],
         'cant-undo+tier+effort': ['--cant-undo', '--tier', 'sonnet', '--effort', 'high']}
FIELDS_OF = {'none': {}, 'undoable': {'undoable': True}, 'cant-undo': {'undoable': False}, 'tier': {'tier': 'sonnet'},
             'effort': {'effort': 'high'}, 'tier+effort': {'tier': 'sonnet', 'effort': 'high'},
             'undoable+tier': {'undoable': True, 'tier': 'sonnet'},
             'cant-undo+tier+effort': {'undoable': False, 'tier': 'sonnet', 'effort': 'high'}}
TID, FENCE = 't' + '0' * 32, 3
FIX = dict(to='w2', title='Golden title', body='Golden body.', acceptance='Golden acceptance.', scope='orch')


class Stub:
    """Records what the CLI would send. It never opens a socket or reads a credential."""
    def __init__(self, actor_id):
        self.actor_id, self.sent = actor_id, []

    def command(self, action, payload, request_id):
        self.sent.append((action, payload, request_id))
        return {'task': {'id': 't' + '1' * 32, 'status': 'draft'}}

    def state(self):
        return {'tasks': [{'id': 't' + '1' * 32, 'status': 'delivered'}], 'server_time': 1000.0}


def no_network(*a, **k):
    raise AssertionError('the id tests must not open a socket')


class IdTests(unittest.TestCase):
    def ids(self, case):
        with mock.patch('socket.create_connection', no_network), mock.patch.object(crew.CrewClient, '__init__', no_network), \
                contextlib.redirect_stderr(io.StringIO()):
            args = crew.build_parser().parse_args(['--credential', 'NOT-READ', 'delegate', TID, '--to', FIX['to'], '--title', FIX['title'],
                                                   '--body', FIX['body'], '--acceptance', FIX['acceptance']] + FLAGS[case])
            stub = Stub('cc')
            crew.cmd_delegate(stub, {'tasks': [{'id': TID, 'status': 'claimed', 'lease': {'holder': 'cc', 'until': 9e9, 'fence': FENCE}}],
                                     'server_time': 1000.0}, args)
            cr4 = stub.sent[0][2]
            args = crew.build_parser().parse_args(['--credential', 'NOT-READ', 'intake', '--scope', FIX['scope'], '--to', FIX['to'],
                                                   '--title', FIX['title'], '--body', FIX['body'], '--acceptance', FIX['acceptance']] + FLAGS[case])
            stub = Stub('keeper')
            crew.cmd_intake(stub, {'tasks': []}, args)
            cr5 = stub.sent[0][2]
        people = {'keeper': Actor('keeper', 'owner'), 'w2': Actor('w2', 'worker'), 'audit': Actor('audit', 'reviewer')}
        root = case_dir('sw2-golden')
        (root / 'art').mkdir()
        st = Store(root / 'room.sqlite', people, {'fixture': root / 'art'})
        st.execute(people['keeper'], 'grant_handoff', {'id': 'orch', 'members': ['keeper', 'w2', 'audit'], 'capabilities': ['handoff.record'],
                                                       'root_id': 'fixture', 'scope': 'Golden scope', 'source_namespaces': ['estate']}, 'g-grant')
        task = st.execute(people['keeper'], 'intake', dict({'source_namespace': 'estate', 'external_reference': 'golden-ref', 'message': {
            'title': FIX['title'], 'body': FIX['body'], 'recipient': FIX['to'], 'authority_id': 'orch', 'kind': 'handoff.record',
            'acceptance': FIX['acceptance']}}, **FIELDS_OF[case]), 'g-intake')['task']
        return cr4, cr5, task['intake']['fingerprint']

    def test_unmarked_untagged_mark_only_and_tags_only_ids_equal_each_heads_own(self):
        for case, want in GOLDEN.items():
            with self.subTest(case=case):
                self.assertEqual(self.ids(case), want)

    def test_a_mark_with_tags_puts_the_mark_first_and_differs_from_every_single_case(self):
        singles = {v for values in GOLDEN.values() for v in values}
        for case, want in BOTH.items():
            with self.subTest(case=case):
                got = self.ids(case)
                self.assertEqual(got, want)
                self.assertFalse(singles & set(got))


class MergedRoom(unittest.TestCase):
    def setUp(self):
        self.root = case_dir('sw2-room')
        self.room = activation.start_room(self.root, enrollment=CONFIG, selected=SEATS, review_standins=[STANDIN], seat_tiers=dict(LABELS))
        self.addCleanup(self.room.close)
        self.store, self.n = self.room.store, 0

    def who(self, actor):
        return self.store.people[actor]

    def run_(self, actor, action, payload):
        self.n += 1
        return self.store.execute(self.who(actor), action, payload, 'sw2-%d' % self.n)

    def intake(self, to='cc', **fields):
        self.n += 1
        tid = self.run_('keeper', 'intake', dict({'source_namespace': 'estate', 'external_reference': 'ref-%d' % self.n, 'message': {
            'title': 'Fictional task', 'body': 'Do it.', 'recipient': to, 'authority_id': 'orch', 'kind': 'handoff.record',
            'acceptance': 'Check it.'}}, **fields))['task']['id']
        self.run_('keeper', 'deliver', {'task_id': tid})
        return tid

    def claim(self, actor, tid):
        self.run_(actor, 'ack', {'task_id': tid})
        return self.run_(actor, 'claim', {'task_id': tid, 'seconds': 1800})['task']['lease']['fence']

    def task(self, tid):
        return self.store.task(self.who('keeper'), tid)

    def cli(self, actor, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = crew.main(['--credential', str(self.root / (actor + '-credential.json')), '--json'] + list(args))
        return code, out.getvalue(), err.getvalue()


class StoreTests(MergedRoom):
    def test_intake_and_request_help_carry_the_mark_and_the_tags_together(self):
        tid = self.intake(undoable=True, tier='opus', effort='max')
        self.assertEqual([self.task(tid).get(k) for k in ('undoable', 'tier', 'effort')], [True, 'opus', 'max'])
        parent = self.intake(to='room')
        fence = self.claim('room', parent)
        child = self.run_('room', 'request_help', {'task_id': parent, 'fence': fence, 'recipient': 'cc', 'title': 'Help', 'body': 'Help me.',
                                                   'acceptance': 'Helped.', 'undoable': False, 'tier': 'haiku', 'effort': 'low'})['task']
        self.assertEqual([child.get(k) for k in ('undoable', 'tier', 'effort')], [False, 'haiku', 'low'])

    def test_no_later_command_takes_the_mark_or_the_tags(self):
        tid = self.intake(to='cc')
        fence = self.claim('cc', tid)
        for field, value in (('undoable', True), ('tier', 'opus'), ('effort', 'low')):
            for action, payload in (('renew', {'task_id': tid, 'fence': fence, 'seconds': 600}),
                                    ('note', {'authority_id': 'orch', 'body': 'A note.'})):
                with self.subTest(action=action, field=field):
                    with self.assertRaises(RoomError) as caught:
                        self.run_('cc', action, dict(payload, **{field: value}))
                    self.assertEqual(caught.exception.code, 'invalid')

    def test_the_mark_is_checked_before_the_tags(self):
        with self.assertRaises(RoomError) as caught:            # intake: a bad mark and a bad tier: the mark's error
            self.intake(undoable='yes', tier='gpt')
        self.assertEqual((caught.exception.code, str(caught.exception)), ('invalid', 'The undoable mark must be true or false.'))
        parent = self.intake(to='cc')
        fence = self.claim('cc', parent)
        with self.assertRaises(RoomError) as caught:            # request_help: a non-marker with a mark: forbidden, before the tag
            self.run_('cc', 'request_help', {'task_id': parent, 'fence': fence, 'recipient': 'w2', 'title': 'H', 'body': 'B',
                                             'acceptance': 'A', 'undoable': True, 'tier': 'gpt'})
        self.assertEqual(caught.exception.code, 'forbidden')
        with self.assertRaises(RoomError) as caught:            # with no mark, the bad tag is the error
            self.run_('cc', 'request_help', {'task_id': parent, 'fence': fence, 'recipient': 'w2', 'title': 'H', 'body': 'B',
                                             'acceptance': 'A', 'tier': 'gpt'})
        self.assertEqual((caught.exception.code, str(caught.exception)), ('invalid', 'The tier must be one of haiku, sonnet, opus.'))


class DelegateTests(MergedRoom):
    def test_a_listed_marker_asks_a_marked_tagged_helper_and_a_non_marker_is_refused(self):
        parent = self.intake(to='room')
        self.claim('room', parent)
        code, out, err = self.cli('room', 'delegate', parent, '--to', 'cc', '--title', 'Helper', '--body', 'Help.',
                                  '--acceptance', 'Helped.', '--undoable', '--tier', 'haiku', '--effort', 'low')
        self.assertEqual(code, 0, err)
        child = self.task(json.loads(out)['child_task_id'])
        self.assertEqual([child.get(k) for k in ('undoable', 'tier', 'effort')], [True, 'haiku', 'low'])
        other = self.intake(to='cc')
        self.claim('cc', other)
        before = len(self.store.list_tasks(self.who('keeper')))
        code, out, err = self.cli('cc', 'delegate', other, '--to', 'w2', '--title', 'Helper', '--body', 'Help.',
                                  '--acceptance', 'Helped.', '--undoable', '--tier', 'haiku')
        self.assertNotEqual(code, 0)
        self.assertIn('forbidden', out + err)
        self.assertEqual(len(self.store.list_tasks(self.who('keeper'))), before)


class StandinTests(MergedRoom):
    def test_dot_accepts_an_undoable_tagged_claude_built_task_and_state_shows_the_mismatch(self):
        tid = self.intake(to='cc', undoable=True, tier='opus')
        fence = self.claim('cc', tid)
        ref = handoff_operation(self.room.server, self.who('cc'), 'stage_text', {'task_id': tid, 'fence': fence, 'text': 'Result.'})
        self.run_('cc', 'finish', {'task_id': tid, 'fence': fence, 'body': 'Done.', 'refs': [{'path': ref['path'], 'sha256': ref['sha256']}]})
        done = self.run_('dot', 'review', {'task_id': tid, 'accepted': True, 'note': 'Stand-in review.'})['task']
        self.assertEqual((done['status'], done['reviewer'], done.get('standin_for')), ('done', 'dot', 'audit'))
        from common_room.board import state_body
        view = next(t for t in state_body(self.room.server, self.who('keeper'))['tasks'] if t['id'] == tid)
        self.assertEqual((view['tier'], view.get('tier_mismatch'), view.get('undoable')), ('opus', 'sonnet', True))


class StartTests(unittest.TestCase):
    """activation with both files. ROOT, the two default file paths and handoff_room.ROOT point at a scratch tree."""
    def setUp(self):
        self.tree = case_dir('sw2-tree')
        (self.tree / 'crew').mkdir()
        for target, name, value in ((activation, 'ROOT', self.tree), (handoff_room, 'ROOT', self.tree),
                                    (activation, 'STANDINS_FILE', self.tree / 'crew' / 'review-standins.json'),
                                    (activation, 'SEAT_TIERS_FILE', self.tree / 'crew' / 'seat-tiers.json')):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.default_root = self.tree / '.runtime' / 'handoffs'

    def write(self, name, value):
        (self.tree / 'crew' / name).write_text(json.dumps(value), encoding='utf-8')

    def main(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            activation.main(list(argv))
        return ctx.exception.code, err.getvalue().strip().splitlines()

    def test_a_start_takes_both_stand_ins_and_seat_tiers(self):
        root = self.tree / '.checks' / 'start'      # with ROOT patched, a room lives under the scratch tree's own .checks
        root.mkdir(parents=True)
        room = activation.start_room(root, enrollment=CONFIG, selected=SEATS, review_standins=[STANDIN], seat_tiers=dict(LABELS))
        self.addCleanup(room.close)
        self.assertEqual(list(room.store.standins), [('orch', 'dot')])
        self.assertEqual(room.store.seat_tiers, LABELS)

    def test_each_bad_file_stops_the_default_root_start_with_its_own_message(self):
        self.write('review-standins.json', {'version': 2})
        code, lines = self.main('--root', str(self.default_root))
        self.assertEqual((code, lines), (2, ['common_room.activation: Invalid review stand-ins file.']))
        (self.tree / 'crew' / 'review-standins.json').unlink()
        self.write('seat-tiers.json', {'seat_tiers': {}})
        code, lines = self.main('--root', str(self.default_root))
        self.assertEqual(code, 2)
        self.assertEqual(lines, ['common_room.activation: Invalid seat tier file: ' + str(self.tree / 'crew' / 'seat-tiers.json') +
                                 ' (expected {"version": 1, "seat_tiers": {seat: tier}}).'])

    def test_another_root_reads_neither_file_unless_named(self):
        self.write('review-standins.json', {'version': 2})
        self.write('seat-tiers.json', {'seat_tiers': {}})
        other = self.tree / '.checks' / 'other'
        other.mkdir(parents=True)
        (other / 'enroll.json').write_text(json.dumps(CONFIG), encoding='utf-8')
        started = []

        def console(room, headless=False):
            started.append((dict(room.store.standins), dict(room.store.seat_tiers)))
            room.close()
        with mock.patch.object(activation, 'run_console', console), contextlib.redirect_stdout(io.StringIO()):
            activation.main(['--root', str(other), '--enrollment', str(other / 'enroll.json'), '--participants'] + SEATS)
        self.assertEqual(started, [({}, {})])


class RenewalQuestionTests(unittest.TestCase):
    """A1's questions and A4c's renewal note in one room: a question read mark is not the seat's next command."""
    def setUp(self):
        self.root = case_dir('sw2-renew')
        self.rooms = []
        self.room = self.start(CONFIG)

    def tearDown(self):
        for room in self.rooms:
            room.close()

    def start(self, enrollment=None):
        room = activation.start_room(self.root, enrollment=enrollment, selected=SEATS, review_standins=[STANDIN])
        self.rooms.append(room)
        return room

    def renew(self):
        self.room.close()
        self.room = self.start()

    def cli(self, actor, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = crew.main(['--credential', str(self.root / (actor + '-credential.json')), '--json'] + list(args))
        return code, out.getvalue(), err.getvalue()

    def state(self, actor):
        from common_room.board import state_body
        return state_body(self.room.server, self.room.store.people[actor])

    def ask(self, asker, seat, text):
        code, out, err = self.cli(asker, 'note', 'orch', 'QUESTION FOR ' + seat + ': ' + text)
        self.assertEqual(code, 0, err)
        return json.loads(out)['question']['seq']

    def test_a_watch_read_mark_leaves_the_renewal_note_and_the_next_command_clears_it(self):
        self.renew()
        seq = self.ask('w2', 'cc', 'Ready for the merge?')
        body = self.state('cc')
        self.assertEqual([q['seq'] for q in body['questions']], [seq])
        self.assertEqual(body['asked'], [])
        self.assertIn('room_renewed', body)                                  # one state body carries all three
        path = str(self.root / 'cc-credential.json')
        args = crew.build_parser().parse_args(['--credential', path, 'watch', '--max-wait', '45'])
        clock = FakeClock(None)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = crew.run_watch(crew.CrewClient(path), args, sleep=clock.sleep, monotonic=clock.monotonic)
        self.assertEqual(code, 0)
        self.assertTrue(out.getvalue().startswith('QUESTION #' + str(seq)), out.getvalue())
        body = self.state('cc')
        self.assertEqual(body['questions'][0]['state'], 'read')
        self.assertIn('room_renewed', body)                                  # the read mark did not clear it
        code, out, err = self.cli('cc', 'note', 'orch', 'Carrying on.')     # the seat's next real command does
        self.assertEqual(code, 0, err)
        self.assertNotIn('room_renewed', self.state('cc'))

    def test_a_relay_state_request_read_mark_leaves_the_renewal_note_and_the_next_command_clears_it(self):
        from common_room.relay import Relay
        self.renew()
        seq = self.ask('w2', 'room', 'Which window?')
        relay = Relay(self.root, ['room'], clock=self.room.server.clock, state_every=0)
        relay.clients['room'].sleep = lambda seconds: None
        folder = self.root / 'relay' / 'room'
        (folder / 'requests' / 's1.json').write_text(json.dumps({'request_id': 's1', 'kind': 'state'}), encoding='utf-8')
        relay.process_once()
        answer = json.loads((folder / 'responses' / 'resp-s1.json').read_text(encoding='utf-8'))
        self.assertEqual(answer['result'], {'written': True, 'questions_read': [seq]})
        relay_state = json.loads((folder / 'relay-state.json').read_text(encoding='utf-8'))
        self.assertIn('room_renewed', relay_state)
        self.assertEqual([(q['seq'], q['state']) for q in relay_state['questions']], [(seq, 'read')])
        self.assertIn('asked', relay_state)
        self.assertIn('room_renewed', self.state('room'))
        (folder / 'requests' / 'n1.json').write_text(json.dumps({'request_id': 'n1', 'kind': 'command', 'action': 'note',
                                                                  'payload': {'authority_id': 'orch', 'body': 'Carrying on.'}}), encoding='utf-8')
        relay.process_once()
        self.assertTrue(json.loads((folder / 'responses' / 'resp-n1.json').read_text(encoding='utf-8'))['ok'])
        self.assertNotIn('room_renewed', self.state('room'))


if __name__ == '__main__':
    unittest.main(argv=sys.argv[:1])    # a tree root passed as an argument names no test
