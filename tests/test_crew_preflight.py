"""crew/preflight.py: tells a stale-but-running room apart from nothing and from a stranger."""
import importlib.util, json, os, unittest
from pathlib import Path
from tests.helpers import case_dir

SPEC = importlib.util.spec_from_file_location('preflight', Path(__file__).resolve().parents[1] / 'crew' / 'preflight.py')
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)
CONFIG = {'version': 1, 'actors': [{'id': 'pf-worker', 'role': 'worker', 'label': 'Fictional', 'session': 'pf'}],
          'authorities': [{'id': 'pf-one', 'members': ['keeper', 'pf-worker'], 'scope': 'Fictional scope'}]}


class PreflightTests(unittest.TestCase):
    def test_empty_folder_is_safe(self):
        self.assertEqual(preflight.check(case_dir('preflight'))[0], 0)

    def test_recorded_running_room_is_reported_with_pid_then_safe_after_close(self):
        from common_room.activation import start_room
        root = case_dir('preflight')
        room = start_room(root, enrollment=CONFIG)
        self.addCleanup(room.close)
        (root / 'background-process.json').write_text(json.dumps(
            {'version': 1, 'pid': os.getpid(), 'instance_id': room.descriptor['instance_id']}), encoding='utf-8')
        code, info = preflight.check(root)
        self.assertEqual((code, info['pid'], info['keys_expired']), (3, os.getpid(), False))
        (root / 'background-process.json').write_text(json.dumps(
            {'version': 1, 'pid': 1, 'instance_id': 'someone-else'}), encoding='utf-8')
        self.assertEqual(preflight.check(root)[0], 4)            # a stranger: never offer to stop it
        room.close()
        self.assertEqual(preflight.check(root)[0], 0)

    def test_never_reads_credentials(self):
        source = (Path(__file__).resolve().parents[1] / 'crew' / 'preflight.py').read_text(encoding='utf-8')
        self.assertNotIn('-credential.json', source)


if __name__ == '__main__':
    unittest.main()
