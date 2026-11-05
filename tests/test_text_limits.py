"""W3 behavioral gates; fictional fixtures only, directly runnable by evidence.py."""
import copy
import pathlib
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from common_room import store as store_module
from common_room.protocol import RoomError, canonical
from common_room.store import Store, fold, text
from common_room.crew_client import CrewError
from tests.helpers import case_dir
from tests.test_store import StoreFixture
from tests.test_relay import RelayTestCase, RelayDelegatedDecisionTests
from tests.test_cancel_and_delegated_decisions import legacy_scenario


class TextMessages(unittest.TestCase):
    def test_oversize_names_field_length_and_limit(self):
        with self.assertRaises(RoomError) as caught:
            text('x' * 4001, 4000, 'Finish summary')
        self.assertEqual(str(caught.exception), 'Finish summary is 4001 characters; the limit is 4000.')

    def test_empty_wrong_type_and_control_are_distinct(self):
        for value, phrase in [(' \n', 'is empty'), (None, 'must be text'), ('a\x00b', 'has a control character')]:
            with self.assertRaises(RoomError) as caught:
                text(value, 2000, 'Decision question')
            self.assertIn('Decision question', str(caught.exception))
            self.assertIn(phrase, str(caught.exception))

    def test_limit_counts_characters_and_preserves_whitespace(self):
        for limit in (500, 2000, 4000):
            value = ' \n' + '\U0001f600' * (limit - 3) + '\t'
            self.assertEqual(text(value, limit), value)
            with self.assertRaises(RoomError):
                text(value + 'x', limit)

    def test_synthetic_successful_events_and_fold_are_unchanged(self):
        def old_text(value, limit, name=None):
            if type(value) is not str or not value.strip() or len(value) > limit or any(ord(c) < 32 and c not in '\n\r\t' for c in value):
                raise RoomError('invalid', 'Text is missing or exceeds its limit.')
            return value
        with patch.object(store_module, 'text', old_text):
            before = legacy_scenario(Store, case_dir('w3-old-messages'))
        after = legacy_scenario(Store, case_dir('w3-new-messages'))
        self.assertEqual(canonical(before), canonical(after))
        self.assertEqual(fold(before), fold(after))

    def test_help_publishes_caps_prefixes_and_helper_rule(self):
        from common_room.relay import README_TEMPLATE
        from common_room.join_cards import render_card
        enrollment = {'version': 1, 'actors': [{'id': 'dot', 'role': 'worker', 'label': 'Dot', 'session': 'fixture'}],
                      'authorities': [{'id': 'room', 'members': ['dot'], 'scope': 'Fixture'}]}
        for content in (README_TEMPLATE, render_card('dot', enrollment, python=False), render_card('dot', enrollment, python=True)):
            for phrase in ('4000', '2000', '500', '6 or more', 'different worker', 'refund'):
                self.assertIn(phrase, content)


class StoreMessages(StoreFixture):
    def test_missing_task_error_names_received_length_without_echo(self):
        for lookup in (lambda: self.store.task(self.builder, 'secret1234'),
                       lambda: self.call(self.builder, 'ack', {'task_id': 'secret1234'})):
            with self.assertRaises(RoomError) as caught:
                lookup()
            self.assertIn('33 characters', str(caught.exception))
            self.assertIn('10', str(caught.exception))
            self.assertNotIn('secret1234', str(caught.exception))

    def test_named_note_error_records_no_event(self):
        before = self.store._snapshot(self.owner)[0]
        with self.assertRaises(RoomError) as caught:
            self.call(self.builder, 'note', {'authority_id': 'work', 'body': 'x' * 4001})
        self.assertIn('Note body is 4001 characters; the limit is 4000.', str(caught.exception))
        self.assertEqual(before, self.store._snapshot(self.owner)[0])


class PrefixUnitTests(unittest.TestCase):
    def resolve(self, payload, tasks):
        from common_room.relay import _task_payload
        class Client:
            def state(self):
                return {'tasks': [{'id': i} for i in tasks]}
        return _task_payload(Client(), payload)

    def test_unique_visible_prefix_expands_without_mutating_request(self):
        full = 't123456' + 'a' * 26
        payload = {'task_id': full[:10], 'fence': 7}
        self.assertEqual(self.resolve(payload, [full]), dict(payload, task_id=full))
        self.assertEqual(payload['task_id'], full[:10])

    def test_ambiguous_prefix_lists_only_visible_matches(self):
        ids = ['t123456' + c * 26 for c in 'ab']
        with self.assertRaises(CrewError) as caught:
            self.resolve({'task_id': 't123456'}, ids)
        self.assertEqual(caught.exception.code, 'ambiguous')
        for task in ids:
            self.assertIn(task, str(caught.exception))

    def test_short_and_missing_prefix_have_clear_errors(self):
        for value, phrase in [('t1234', 'at least 6'), ('t999999', 'No visible task')]:
            with self.assertRaises(CrewError) as caught:
                self.resolve({'task_id': value}, ['t123456' + 'a' * 26])
            self.assertIn(phrase, str(caught.exception))

    def test_full_id_and_non_task_payload_need_no_state_fetch(self):
        from common_room.relay import _task_payload
        class Offline:
            def state(self):
                raise AssertionError('Full IDs must pass through without state lookup')
        for payload in ({'task_id': 't' + 'a' * 32}, {'authority_id': 'fixture', 'body': 'hi'}):
            self.assertEqual(_task_payload(Offline(), payload), payload)


class RelayW3Tests(RelayTestCase):
    def send(self, room, relay, rid, action, payload, actor='relay-writer', kind='command'):
        self.write_request(room, actor, rid + '.json', {'request_id': rid, 'kind': kind,
                           'operation' if kind == 'handoff' else 'action': action, 'payload': payload})
        relay.process_once()
        return self.response(room, actor, rid)

    def test_prefix_command_and_handoff_then_long_finish_error(self):
        room = self.start(); task = self.intake_task(room)
        relay = self.relay(room, ['relay-writer'])
        self.assertTrue(self.send(room, relay, 'ack', 'ack', {'task_id': task[:10]})['ok'])
        claimed = self.send(room, relay, 'claim', 'claim', {'task_id': task[:10], 'seconds': 900})
        fence = claimed['result']['task']['fence']
        staged = self.send(room, relay, 'stage', 'stage_text', {'task_id': task[:10], 'fence': fence, 'text': 'Fictional'}, kind='handoff')
        self.assertTrue(staged['ok'], staged)
        before = self.events(room)
        result = self.send(room, relay, 'finish', 'finish', {'task_id': task[:10], 'fence': fence, 'body': 'x' * 4001, 'refs': [staged['result']]})
        self.assertFalse(result['ok'])
        self.assertIn('Finish summary is 4001 characters; the limit is 4000.', result['message'])
        self.assertEqual(before, self.events(room))

    def test_other_scope_prefix_is_not_visible(self):
        room = self.start(); task = self.intake_task(room, recipient='relay-other', authority='relay-two')
        relay = self.relay(room, ['relay-writer'])
        before = self.events(room)
        result = self.send(room, relay, 'hidden', 'ack', {'task_id': task[:10]})
        self.assertFalse(result['ok']); self.assertIn('No visible task', result['message'])
        self.assertNotIn(task, result['message']); self.assertEqual(before, self.events(room))

    def test_self_helper_remains_refused(self):
        room = self.start(); task = self.intake_task(room)
        relay = self.relay(room, ['relay-writer'])
        self.send(room, relay, 'ack', 'ack', {'task_id': task})
        fence = self.send(room, relay, 'claim', 'claim', {'task_id': task, 'seconds': 900})['result']['task']['fence']
        before = self.events(room)
        result = self.send(room, relay, 'self', 'request_help', {'task_id': task, 'fence': fence, 'recipient': 'relay-writer', 'title': 'Fixture', 'body': 'Work', 'acceptance': 'Check'})
        self.assertFalse(result['ok']); self.assertIn('different worker', result['message'])
        self.assertEqual(before, self.events(room))


class RelayRefundTests(RelayTestCase):
    CONFIG = RelayDelegatedDecisionTests.CONFIG
    _setup = RelayDelegatedDecisionTests._setup

    def send(self, room, relay, rid, actor, child, body, refund=True):
        self.write_request(room, actor, rid + '.json', {'request_id': rid, 'kind': 'command', 'action': 'resolve_decision',
                           'payload': {'task_id': child, 'body': body, 'refund': refund}})
        relay.process_once()
        return self.response(room, actor, rid)

    def test_existing_refund_records_reason_before_after_and_replay(self):
        room, child = self._setup()
        orch = room.store.people['orch']; helper = room.store.people['helper']
        room.store.execute(orch, 'resolve_decision', {'task_id': child, 'body': 'Continue'}, 'resume')
        room.store.execute(helper, 'ack', {'task_id': child}, 'ack-again')
        for n in range(2):
            f = room.store.execute(helper, 'claim', {'task_id': child, 'seconds': 900}, 'claim-' + str(n))['task']['fence']
            room.store.execute(helper, 'fail', {'task_id': child, 'fence': f, 'body': 'Synthetic failure'}, 'fail-' + str(n))
        # Only the owner may refund at two strikes; sender cannot expand its authority.
        relay = self.relay(room, ['orch', 'other'])
        before = self.events(room)
        result = self.send(room, relay, 'stopped', 'orch', child, 'Reason')
        self.assertFalse(result['ok']); self.assertEqual(before, self.events(room))
        owner_result = room.store.execute(room.store.people['keeper'], 'resolve_decision', {'task_id': child, 'body': 'Owner corrected fixture brief', 'refund': True}, 'owner-refund')
        self.assertEqual(owner_result['task']['refunds'][-1]['strikes_before'], 2)
        room.store.execute(helper, 'ack', {'task_id': child}, 'ack-third')
        f = room.store.execute(helper, 'claim', {'task_id': child, 'seconds': 900}, 'claim-third')['task']['fence']
        room.store.execute(helper, 'wait_decision', {'task_id': child, 'fence': f, 'question': 'Correct brief?'}, 'wait-again')
        before = self.events(room)
        for rid, actor, body, refund in [('outsider', 'other', 'Reason', True), ('blank', 'orch', ' ', True), ('type', 'orch', 'Reason', 'yes')]:
            self.assertFalse(self.send(room, relay, rid, actor, child, body, refund)['ok'])
            self.assertEqual(before, self.events(room))
        result = self.send(room, relay, 'refund', 'orch', child, 'Brief corrected')
        self.assertTrue(result['ok'], result)
        record = {'by': 'orch', 'reason': 'Brief corrected', 'strikes_before': 1, 'strikes_after': 0}
        self.assertEqual(result['result']['task']['refunds'][-1], record)
        events = self.events(room)
        self.assertEqual(events[-1]['payload']['changes']['refunds'][-1], record)
        self.assertEqual(fold(events)[1][child]['refunds'][-1], record)
        room.store.execute(helper, 'ack', {'task_id': child}, 'ack-zero')
        f = room.store.execute(helper, 'claim', {'task_id': child, 'seconds': 900}, 'claim-zero')['task']['fence']
        room.store.execute(helper, 'wait_decision', {'task_id': child, 'fence': f, 'question': 'Zero count?'}, 'wait-zero')
        result = self.send(room, relay, 'zero', 'orch', child, 'No negative count')
        self.assertEqual(result['result']['task']['failed_attempts'], 0)
        self.assertEqual(result['result']['task']['refunds'][-1]['strikes_before'], 0)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(loader.loadTestsFromTestCase(cls) for cls in
                              (TextMessages, StoreMessages, PrefixUnitTests, RelayW3Tests, RelayRefundTests))


if __name__ == '__main__':
    unittest.main()
