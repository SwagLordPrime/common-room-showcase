"""Offline regression pins for the silent failures listed in proof-silent.

All data is fictional, inside tests.helpers.case_dir. No live runtime, socket,
real credential, room listener, worker process or relay process is used.
The expectedFailure is tracked by orchestra QUESTION on task te72385ce5.
"""
import hashlib
import json
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from common_room.activation import start_room
from common_room.protocol import Actor, RoomError
from common_room.relay import Relay, RelayMalformed
from common_room.store import Store
from tests.helpers import case_dir


class SilentFailureTests(unittest.TestCase):
    def setUp(self):
        self.home = case_dir('silent')
        self.artifacts = self.home / 'artifacts'
        self.artifacts.mkdir()
        self.now = 1000.0
        self.people = {a.id: a for a in (
            Actor('owner', 'owner'), Actor('builder', 'worker'),
            Actor('other', 'worker'), Actor('reviewer', 'reviewer'))}
        self.owner = self.people['owner']
        self.builder = self.people['builder']
        self.store = Store(self.home / 'fixture.sqlite', self.people,
                           {'fixture': self.artifacts}, clock=lambda: self.now)
        self.counter = 0
        self.addCleanup(patch.stopall)
        patch('socket.socket', side_effect=AssertionError('No sockets in silent pins')).start()
        self.grant('work', list(self.people))

    def call(self, actor, action, payload, request_id=None):
        self.counter += 1
        return self.store.execute(actor, action, payload,
                                  request_id or 'silent-' + str(self.counter))

    def grant(self, authority, members):
        return self.call(self.owner, 'grant_handoff', {
            'id': authority, 'members': members, 'capabilities': ['handoff.record'],
            'root_id': 'fixture', 'scope': 'Fictional silent failure checks',
            'source_namespaces': ['fixture']})

    def message(self, authority='work', body='Fictional task'):
        return {'title': 'Silent fixture', 'body': body, 'recipient': 'builder',
                'authority_id': authority, 'kind': 'handoff.record',
                'acceptance': 'Fictional independently reviewed artifact'}

    def intake(self, authority='work', body='Fictional task'):
        return self.call(self.owner, 'intake', {
            'source_namespace': 'fixture', 'external_reference': 'item-' + str(self.counter),
            'message': self.message(authority, body)})['task']['id']

    def claimed(self):
        task = self.intake()
        self.call(self.owner, 'deliver', {'task_id': task})
        self.call(self.builder, 'ack', {'task_id': task})
        lease = self.call(self.builder, 'claim', {'task_id': task, 'seconds': 1800})['task']['lease']
        return task, lease

    def submitted(self):
        task, lease = self.claimed()
        data = b'Entirely fictional result.\n'
        (self.artifacts / 'result.txt').write_bytes(data)
        self.call(self.builder, 'finish', {
            'task_id': task, 'fence': lease['fence'], 'body': 'Fictional result',
            'refs': [{'path': 'result.txt', 'sha256': hashlib.sha256(data).hexdigest()}]})
        return task

    @staticmethod
    def refusal(fn):
        try:
            fn()
        except RoomError as exc:
            return exc.code, str(exc)
        return None, 'accepted'

    def state(self, task):
        return self.store.task(self.owner, task)

    def test_draft_cannot_be_acknowledged(self):
        task = self.intake()
        code, _ = self.refusal(lambda: self.call(self.builder, 'ack', {'task_id': task}))
        self.assertEqual((code, self.state(task)['status']), ('state', 'draft'))  # PIN

    def test_request_id_slash_is_refused(self):
        code, _ = self.refusal(lambda: self.call(self.builder, 'note',
            {'authority_id': 'work', 'body': 'Fictional'}, 'bad/id'))
        parser = object.__new__(Relay)
        try:
            parser._parse_bytes(json.dumps({'request_id': 'bad/id', 'kind': 'state'}).encode())
        except RelayMalformed as exc:
            relay_code = exc.code
        else:
            relay_code = None
        self.assertEqual((code, relay_code), ('invalid', 'invalid'))  # PIN

    def test_queue_without_distinct_reviewer_is_refused(self):
        # Known gap: root intake currently succeeds; QUESTION on te72385ce5.
        self.grant('no-review', ['owner', 'builder'])
        before = len(self.store.list_tasks(self.owner))
        code, _ = self.refusal(lambda: self.intake('no-review'))
        after = len(self.store.list_tasks(self.owner))
        self.assertEqual((code, after), ('forbidden', before))  # PIN

    def test_review_text_cap_preserves_submission(self):
        task = self.submitted()
        code, _ = self.refusal(lambda: self.call(self.people['reviewer'], 'review',
            {'task_id': task, 'accepted': True, 'note': 'x' * 2001}))
        after = self.state(task)['status']
        self.call(self.people['reviewer'], 'review',
                  {'task_id': task, 'accepted': True, 'note': 'x' * 2000})
        self.assertEqual((code, after, self.state(task)['status']),
                         ('invalid', 'ready_for_review', 'done'))  # PIN

    def test_task_body_cap_refuses_only_over_limit(self):
        self.intake(body='x' * 4000)
        before = len(self.store.list_tasks(self.owner))
        code, _ = self.refusal(lambda: self.intake(body='x' * 4001))
        self.assertEqual((code, len(self.store.list_tasks(self.owner))), ('invalid', before))  # PIN

    def test_lounge_note_cap_refuses_only_over_limit(self):
        self.call(self.builder, 'note', {'authority_id': 'work', 'body': 'x' * 4000})
        before = len(self.store.poll(self.owner)['events'])
        code, _ = self.refusal(lambda: self.call(self.builder, 'note',
            {'authority_id': 'work', 'body': 'x' * 4001}))
        self.assertEqual((code, len(self.store.poll(self.owner)['events'])), ('invalid', before))  # PIN

    def test_second_room_on_one_root_is_refused(self):
        root = case_dir('silent-root')
        fake_server = SimpleNamespace(origin='http://127.0.0.1:1',
            server_address=('127.0.0.1', 1), build_identity='fixture',
            serve_forever=lambda: None, shutdown=lambda: None, server_close=lambda: None)
        # Exercise the real OS root lock; the server and publishing are inert.
        with patch('common_room.activation.make_server', return_value=fake_server) as factory, \
             patch('common_room.activation.ActivationRoom.publish'):
            first = start_room(root, clock=lambda: self.now)
            self.addCleanup(first.close)
            before = (root / 'connection.json').read_bytes()
            try:
                second = start_room(root, clock=lambda: self.now)
            except OSError:
                refused = True
            else:
                refused = False
                second.close()
            after = (root / 'connection.json').read_bytes()
            self.assertEqual((refused, factory.call_count, after), (True, 1, before))  # PIN

    def test_builder_cannot_review_own_submission(self):
        task = self.submitted()
        code, _ = self.refusal(lambda: self.call(self.builder, 'review',
            {'task_id': task, 'accepted': True, 'note': 'Fictional self acceptance'}))
        self.assertEqual((code, self.state(task)['status']),
                         ('self_review', 'ready_for_review'))  # PIN

    def test_worker_role_cannot_review(self):
        task = self.submitted()
        code, message = self.refusal(lambda: self.call(self.people['other'], 'review',
            {'task_id': task, 'accepted': True, 'note': 'Fictional wrong-role review'}))
        self.assertEqual((code, message, self.state(task)['status']),
            ('forbidden', 'Completion needs a separately registered reviewer.', 'ready_for_review'))  # PIN

    def test_lapsed_lease_is_not_a_strike(self):
        task, lease = self.claimed()
        # Retain one real historical strike, then prove expiry does not add one.
        self.call(self.builder, 'fail', {'task_id': task, 'fence': lease['fence'], 'body': 'Fictional failure'})
        self.call(self.builder, 'claim', {'task_id': task, 'seconds': 1800})
        self.now += 1801
        current = self.call(self.builder, 'claim', {'task_id': task, 'seconds': 1800})['task']
        self.assertEqual((current['failed_attempts'], current['status']), (1, 'claimed'))  # PIN

    def test_expired_renewal_refused_and_reclaim_fences_old_holder(self):
        task, lease = self.claimed()
        self.now += 1800
        code, _ = self.refusal(lambda: self.call(self.builder, 'renew',
            {'task_id': task, 'fence': lease['fence'], 'seconds': 1800}))
        current = self.call(self.builder, 'claim', {'task_id': task, 'seconds': 1800})['task']['lease']
        stale, _ = self.refusal(lambda: self.call(self.builder, 'renew',
            {'task_id': task, 'fence': lease['fence'], 'seconds': 1800}))
        self.assertEqual((code, current['fence'] > lease['fence'], stale), ('lease', True, 'lease'))  # PIN

    def test_reused_relay_request_id_returns_old_answer(self):
        root = case_dir('silent-relay')
        (root / 'builder-credential.json').write_text('{}', encoding='utf-8')
        fixture = self
        class OfflineClient:
            def __init__(self, *args, **kwargs):
                pass
            def state(self):
                return {'actor': {'id': 'builder', 'role': 'worker'}, 'server_time': fixture.now}
            def command(self, action, payload, request_id):
                return fixture.store.execute(fixture.builder, action, payload, request_id)
        with patch('common_room.relay.CrewClient', OfflineClient):
            relay = Relay(root, ['builder'], clock=lambda: self.now)
            folder = relay.folders['builder']
            request = {'request_id': 'repeat-note', 'kind': 'command', 'action': 'note',
                       'payload': {'authority_id': 'work', 'body': 'Fictional replay note'}}
            (folder['requests'] / 'first.json').write_text(json.dumps(request), encoding='utf-8')
            relay.process_once()
            answer_path = folder['responses'] / 'resp-repeat-note.json'
            first = json.loads(answer_path.read_text(encoding='utf-8'))
            before = len(self.store.poll(self.owner)['events'])
            # Restart the in-memory relay and use a fresh filename with the same id.
            restarted = Relay(root, ['builder'], clock=lambda: self.now)
            (folder['requests'] / 'second.json').write_text(json.dumps(request), encoding='utf-8')
            restarted.process_once()
            second = json.loads(answer_path.read_text(encoding='utf-8'))
            self.assertEqual((first['ok'], second['ok'], second['result'],
                              len(self.store.poll(self.owner)['events']), second['request_file']),
                             (True, True, first['result'], before, 'second.json'))  # PIN

