"""The room refuses room keys in room text (fix-board 2026-09-24, VERIFY7B L1).

The headless runner's own guard caught only its own key: another seat's key, or the owner's, could
still reach a note, a report or a question. The server knows every key it has issued, so
/api/command and /api/handoff refuse any request whose text holds one, whoever sends it and by
whichever route (the board, the crew CLI, the relay, the runner): RoomError('forbidden',
'Room text may not contain a room key.'). The owner's board approvals, which reach the store
without /api/command, are held to the same rule.

Real rooms from activation.start_room, fictional seats, loopback only. Keys are read from the room
object to build the refused requests and are never printed; no assertion message includes one.
"""
import contextlib, io, json, secrets, statistics, time, unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from tests.helpers import case_dir
from tests.test_board_plans import CONFIG, SEATS, Browser, plan_doc, sha
from tests.test_relay import RelayTestCase
import common_room.crew as crew

MESSAGE = 'Room text may not contain a room key.'


class KeyGuardBase(unittest.TestCase):
    def setUp(self):
        from common_room.activation import start_room
        self.plans_root = case_dir('key-guard-plans')
        self.room = start_room(case_dir('key-guard'), enrollment=CONFIG, selected=SEATS, plans_root=self.plans_root)
        self.addCleanup(self.room.close)

    def key(self, actor):
        return self.room.credentials[actor]['token']

    def browser(self, actor):
        b = Browser(self.room)
        self.assertEqual(b.login(self.room.root / (actor + '-credential.json')), actor)
        return b

    def command(self, b, action, payload, request_id):
        return b.request('POST', '/api/command', {'action': action, 'payload': payload, 'request_id': request_id})[:2]

    def handoff(self, b, operation, payload):
        return b.request('POST', '/api/handoff', {'operation': operation, 'payload': payload})[:2]

    def events(self, kind=None):
        events, _, _ = self.room.store._snapshot(self.room.store.people['keeper'])
        return [e for e in events if kind is None or e['kind'] == kind]

    def assertRefused(self, answer, what):
        status, body = answer
        self.assertEqual((status, body.get('error'), body.get('message')), (403, 'forbidden', MESSAGE), what)

    def pasted(self):
        """(label, text) for the worker's own key, another worker's, the reviewer's and the owner's,
        each inside ordinary words, plus one split across two lines."""
        texts = []
        for label, actor in (('own key', 'bp-worker-a'), ("another seat's key", 'bp-worker-b'),
                             ("the reviewer's key", 'bp-reviewer'), ("the owner's key", 'keeper')):
            texts.append((label, 'Found this in a file: ' + self.key(actor) + ' -- is it useful?'))
        owner = self.key('keeper')
        texts.append(('the owner key split over two lines', 'Part one ' + owner[:20] + '\n' + owner[20:] + ' end'))
        return texts

    def delivered_task(self, worker='bp-worker-a', ref='kg-1'):
        owner = self.room.store.people['keeper']
        message = {'title': 'Key guard task', 'body': 'Write a fictional report.', 'acceptance': 'Report exists.',
                   'recipient': worker, 'authority_id': 'bp-scope', 'kind': 'handoff.record'}
        task_id = self.room.store.execute(owner, 'intake', {'source_namespace': 'estate', 'external_reference': ref,
                                                            'message': message}, 'intake-' + ref)['task']['id']
        self.room.store.execute(owner, 'deliver', {'task_id': task_id}, 'deliver-' + ref)
        return task_id

    def claimed_task(self, b, worker='bp-worker-a'):
        task_id = self.delivered_task(worker)
        self.assertEqual(self.command(b, 'ack', {'task_id': task_id}, 'ack-kg')[0], 200)
        status, body = self.command(b, 'claim', {'task_id': task_id, 'seconds': 600}, 'claim-kg')
        self.assertEqual(status, 200, body)
        return task_id, body['task']['lease']['fence']


class CommandGuardTests(KeyGuardBase):
    def test_a_note_refuses_every_seats_key_and_the_owners(self):
        b = self.browser('bp-worker-a')
        before = len(self.events())
        for n, (label, text) in enumerate(self.pasted()):
            self.assertRefused(self.command(b, 'note', {'authority_id': 'bp-scope', 'body': text}, 'note-k%d' % n), label)
        self.assertEqual(len(self.events()), before, 'a refused note writes nothing')
        # Normal text, including a random key-shaped string that is no room key, is unaffected.
        for n, text in enumerate(['Plain progress note.', 'A lookalike: ' + secrets.token_urlsafe(32)]):
            status, body = self.command(b, 'note', {'authority_id': 'bp-scope', 'body': text}, 'note-ok%d' % n)
            self.assertEqual(status, 200, body)
        self.assertEqual(len(self.events('note')), 2)

    def test_a_finish_body_refuses_a_key_and_a_clean_finish_goes_through(self):
        b = self.browser('bp-worker-a')
        task_id, fence = self.claimed_task(b)
        status, ref = self.handoff(b, 'stage_text', {'task_id': task_id, 'fence': fence, 'text': 'A clean report.'})
        self.assertEqual(status, 200, ref)
        for n, (label, text) in enumerate(self.pasted()):
            self.assertRefused(self.command(b, 'finish', {'task_id': task_id, 'fence': fence, 'body': text, 'refs': [ref]},
                                            'finish-k%d' % n), label)
        # A key anywhere in the request, a list item included, is refused before the room reads it.
        self.assertRefused(self.command(b, 'finish', {'task_id': task_id, 'fence': fence, 'body': 'Done.',
                                                      'refs': [dict(ref, path='results/' + self.key('keeper') + '.txt')]},
                                        'finish-in-list'), 'a key inside the refs list')
        self.assertEqual(self.events('task_finish'), [])
        status, body = self.command(b, 'finish', {'task_id': task_id, 'fence': fence, 'body': 'Done.', 'refs': [ref]},
                                    'finish-ok')
        self.assertEqual((status, body['task']['status']), (200, 'ready_for_review'))

    def test_a_staged_report_refuses_a_key_and_nothing_is_written(self):
        b = self.browser('bp-worker-a')
        task_id, fence = self.claimed_task(b)
        results = self.room.root / 'artifacts' / 'results'
        for label, text in self.pasted():
            self.assertRefused(self.handoff(b, 'stage_text', {'task_id': task_id, 'fence': fence, 'text': text}), label)
        self.assertEqual(sorted(results.glob('*')) if results.exists() else [], [])
        status, ref = self.handoff(b, 'stage_text', {'task_id': task_id, 'fence': fence, 'text': 'A clean report.'})
        self.assertEqual(status, 200, ref)

    def test_other_text_fields_and_the_owner_are_held_to_the_same_rule(self):
        worker, owner = self.browser('bp-worker-a'), self.browser('keeper')
        task_id, fence = self.claimed_task(worker)
        other = self.key('bp-worker-b')
        self.assertRefused(self.command(worker, 'wait_decision', {'task_id': task_id, 'fence': fence,
                                                                  'question': 'Use key ' + other + '?'}, 'ask-k'), 'question')
        self.assertRefused(self.command(worker, 'fail', {'task_id': task_id, 'fence': fence, 'body': other}, 'fail-k'), 'fail')
        self.assertRefused(self.command(worker, 'note', {'authority_id': 'bp-scope', 'body': 'x'}, 'id-' + other),
                           'request id')
        message = {'title': 'Owner task', 'body': 'Use ' + self.key('bp-worker-a'), 'acceptance': 'Done.',
                   'recipient': 'bp-worker-b', 'authority_id': 'bp-scope', 'kind': 'handoff.record'}
        self.assertRefused(self.command(owner, 'intake', {'source_namespace': 'estate', 'external_reference': 'kg-owner',
                                                          'message': message}, 'intake-kg-owner'), 'owner intake')
        self.assertRefused(self.command(owner, 'note', {'authority_id': 'bp-scope',
                                                        'body': 'My own key ' + self.key('keeper')}, 'owner-note'),
                           "the owner's own key")
        self.assertEqual(len(self.events('task_created')), 1, 'only the fixture task exists')

    def test_a_rotated_key_is_guarded_and_the_retired_one_is_plain_text(self):
        b = self.browser('bp-worker-a')
        old = self.key('bp-worker-b')
        self.room.rotate('bp-worker-b')
        new = self.key('bp-worker-b')
        self.assertRefused(self.command(b, 'note', {'authority_id': 'bp-scope', 'body': new}, 'note-new'), 'new key')
        status, body = self.command(b, 'note', {'authority_id': 'bp-scope', 'body': 'retired ' + old}, 'note-old')
        self.assertEqual(status, 200, 'a key the room no longer holds is ordinary text')


class RouteTests(KeyGuardBase):
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

    def test_the_crew_cli_gets_the_room_refusal(self):
        code, out, err = self.cli('bp-worker-a', 'note', 'bp-scope', 'Here: ' + self.key('keeper'))
        self.assertEqual(code, 1)
        self.assertEqual(err.strip().splitlines()[-1], 'forbidden: ' + MESSAGE)
        self.assertEqual(self.events('note'), [])

    def test_a_board_approval_of_a_plan_holding_a_key_is_refused(self):
        doc = plan_doc()
        doc['tasks'][1]['body'] = 'Use this key: ' + self.key('bp-worker-b')
        folder = self.plans_root / 'bp-plan'
        folder.mkdir()
        path = folder / 'PLAN.json'
        path.write_text(json.dumps(doc), encoding='utf-8')
        status, body, _ = self.browser('keeper').approve('bp-plan/PLAN.json', sha(path), 'bp-plan')
        self.assertEqual((status, body['error'], body['message']), (403, 'forbidden', MESSAGE))
        self.assertFalse(any(self.key('bp-worker-b') in (t['body'] or '')
                             for t in self.room.store.list_tasks(self.room.store.people['keeper'])))
        self.assertFalse(path.with_name('PLAN.receipt.json').exists())


class RelayRouteTests(RelayTestCase):
    def test_the_relay_gets_the_room_refusal(self):
        room = self.start()
        relay = self.relay(room, ['relay-writer'])
        key = room.credentials['relay-reader']['token']
        before = len(self.events(room))
        self.write_request(room, 'relay-writer', 'note1.json', {'request_id': 'note-k1', 'kind': 'command', 'action': 'note',
                                                               'payload': {'authority_id': 'relay-one', 'body': 'see ' + key}})
        relay.process_once()
        answer = self.response(room, 'relay-writer', 'note-k1')
        self.assertFalse(answer['ok'])
        self.assertEqual(answer['error'], 'forbidden')
        self.assertEqual(len(self.events(room)), before)


class OverheadTests(unittest.TestCase):
    def test_every_string_at_any_depth_is_checked(self):
        from common_room.board import holds_room_key
        key = secrets.token_urlsafe(32)
        for value in ({'payload': {'body': 'x ' + key}}, {'payload': {'refs': [{'path': key}]}}, {key: 1},
                      [[['deep ' + key]]], {'a': {'b': {'c': [{'d': key[:10] + '\r\n\t ' + key[10:]}]}}}):
            self.assertTrue(holds_room_key(value, ['unrelated-' + secrets.token_urlsafe(8), key]))
        deep = 'x'
        for _ in range(5000):
            deep = [deep]
        self.assertFalse(holds_room_key(deep, [key]), 'deep nesting is walked without recursion')
        for value in ({'n': 12345, 'b': True, 'z': None, 'f': 1.5}, 'plain text', [], {}):
            self.assertFalse(holds_room_key(value, [key]))
        self.assertFalse(holds_room_key({'body': 'anything at all'}, []))
        self.assertFalse(holds_room_key({'body': 'anything at all'}, ['']), 'an empty key never matches everything')

    def test_the_check_on_a_16_kb_payload_with_16_keys_is_negligible(self):
        from common_room.board import holds_room_key
        keys = [secrets.token_urlsafe(32) for _ in range(16)]
        words = ('Fictional progress words for a long note, nothing secret here. ' * 400)[:16000]
        clean = {'action': 'note', 'payload': {'authority_id': 'bp-scope', 'body': words}, 'request_id': 'r-1'}
        dirty = {'action': 'note', 'payload': {'authority_id': 'bp-scope', 'body': words[:-60] + keys[-1]}, 'request_id': 'r-1'}
        self.assertFalse(holds_room_key(clean, keys))
        self.assertTrue(holds_room_key(dirty, keys))
        timings = []
        for _ in range(200):
            started = time.perf_counter()
            holds_room_key(clean, keys)
            timings.append(time.perf_counter() - started)
        self.assertLess(statistics.median(timings), 0.005, 'median seconds per 16 KB request with 16 keys')


if __name__ == '__main__':
    unittest.main()
