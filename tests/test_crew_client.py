"""Shared crew client: own-credential boundary and survival across key rotation."""
import json, unittest
from pathlib import Path
from tests.helpers import case_dir
from common_room.crew_client import CrewClient, CrewError, load_credential, new_request_id, REQUEST_ID

CONFIG = {'version': 1, 'actors': [
    {'id': 'test-writer', 'role': 'worker', 'label': 'Fictional writer', 'session': 'fixture-writer'},
    {'id': 'test-reader', 'role': 'reviewer', 'label': 'Fictional reviewer', 'session': 'fixture-reviewer'}],
    'authorities': [
    {'id': 'test-one', 'members': ['keeper', 'test-writer', 'test-reader'], 'scope': 'Fictional first scope'}]}


class CrewClientTests(unittest.TestCase):
    def start(self, root=None, selected=('test-writer', 'test-reader')):
        from common_room.activation import start_room
        room = start_room(root or case_dir('crew-client'), enrollment=CONFIG, selected=list(selected))
        self.addCleanup(room.close)
        return room

    def test_state_is_scoped_to_own_actor(self):
        room = self.start()
        client = CrewClient(room.root / 'test-writer-credential.json')
        state = client.state()
        self.assertEqual(state['actor'], {'id': 'test-writer', 'role': 'worker'})
        self.assertEqual(client.actor_id, 'test-writer')

    def test_rotation_is_survived_by_rereading_own_credential(self):
        room = self.start()
        client = CrewClient(room.root / 'test-writer-credential.json', sleep=lambda s: None)
        client.state()
        room.rotate('test-writer')
        self.assertEqual(client.state()['actor']['id'], 'test-writer')

    def test_restart_with_new_keys_is_survived(self):
        root = case_dir('crew-client')
        room = self.start(root)
        client = CrewClient(root / 'test-writer-credential.json', sleep=lambda s: None)
        client.state()
        room.close()
        again = self.start(root)
        self.assertNotEqual(again.descriptor['instance_id'], room.descriptor['instance_id'])
        self.assertEqual(client.state()['actor']['id'], 'test-writer')

    def test_expected_actor_is_enforced(self):
        room = self.start()
        client = CrewClient(room.root / 'test-writer-credential.json', expected_actor='test-reader')
        with self.assertRaises(CrewError) as ctx:
            client.state()
        self.assertEqual(ctx.exception.code, 'identity')

    def test_credential_outside_package_runtime_is_refused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'test-writer-credential.json'
            path.write_text(json.dumps({'origin': 'http://127.0.0.1:1', 'token': 'x', 'actor_id': 'test-writer', 'expires_at': 1}))
            with self.assertRaises(CrewError):
                load_credential(path)

    def test_misnamed_credential_is_refused(self):
        root = case_dir('crew-client')
        path = root / 'someone-else-credential.json'
        path.write_text(json.dumps({'origin': 'http://127.0.0.1:1', 'token': 'x', 'actor_id': 'test-writer', 'expires_at': 1}))
        with self.assertRaises(CrewError):
            load_credential(path)

    def test_refusals_keep_their_code(self):
        room = self.start()
        client = CrewClient(room.root / 'test-writer-credential.json')
        with self.assertRaises(CrewError) as ctx:
            client.command('claim', {'task_id': 'tmissing', 'seconds': 1800}, new_request_id('t'))
        self.assertEqual(ctx.exception.code, 'forbidden')

    def test_request_ids_are_bounded(self):
        for prefix in ['review', 'a/b c', '', 'x' * 300]:
            self.assertTrue(REQUEST_ID.fullmatch(new_request_id(prefix)))
        room = self.start()
        client = CrewClient(room.root / 'test-writer-credential.json')
        with self.assertRaises(CrewError):
            client.command('note', {'authority_id': 'test-one', 'body': 'hi'}, 'bad/slash')


class CrewClientIdentityTests(unittest.TestCase):
    """Added by root 2026-09-23 after independent verification (mutant p survived)."""
    start = CrewClientTests.start

    def test_credential_carrying_another_seats_token_is_refused(self):
        room = self.start()
        keeper = json.loads((room.root / 'keeper-credential.json').read_text(encoding='utf-8'))
        forged = dict(keeper, actor_id='test-writer')
        (room.root / 'test-writer-credential.json').write_text(json.dumps(forged), encoding='utf-8')
        before = len(room.store._snapshot(room.store.people['keeper'])[0])
        client = CrewClient(room.root / 'test-writer-credential.json')
        with self.assertRaises(CrewError) as ctx:
            client.command('note', {'authority_id': 'test-one', 'body': 'should never post'}, new_request_id('p'))
        self.assertEqual(ctx.exception.code, 'identity')
        self.assertEqual(len(room.store._snapshot(room.store.people['keeper'])[0]), before)


if __name__ == '__main__':
    unittest.main()
