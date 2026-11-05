"""Windows directory junctions are refused wherever links are (Windows only; skipped elsewhere).

Why this file exists (review of the 2026-09-25 Windows test fixes): the link-refusal tests in
test_relay.py and test_agent_worker.py plant links with os.symlink, which a Windows host without
Developer Mode or admin rights cannot do, so they skip there. A directory junction needs no such
right. On exactly that host a junction is the link an unprivileged process could plant, so this
file plants real junctions (_winapi.CreateJunction, the call CPython's own tests use) and checks
the product's refusals directly. The product code checks Path.is_junction() next to every
is_symlink(); these tests are the first to exercise those branches with a real junction.

Evidence status when written: VERIFIED IN LOCAL SOURCE only. They could not be run where they
were written (Linux has no junctions); the first Windows run is their first run.
"""
import json, os, unittest
from pathlib import Path

from tests.helpers import case_dir


@unittest.skipUnless(os.name == 'nt', 'directory junctions exist only on Windows')
class JunctionRefusalTests(unittest.TestCase):
    def junction(self, target, link):
        """A real junction at `link` pointing to the existing folder `target`; removed afterwards
        with os.rmdir, which deletes the junction itself and never the folder it points to."""
        import _winapi
        _winapi.CreateJunction(str(Path(target).absolute()), str(Path(link).absolute()))
        self.addCleanup(os.rmdir, str(link))
        self.assertTrue(Path(link).is_junction(), 'the fixture must be a real junction')
        return Path(link)

    def test_a_junction_is_not_a_symlink_to_python(self):
        # The reason every refusal must ask is_junction() as well: is_symlink() alone says False.
        root = case_dir('junction-kind')
        link = self.junction(case_dir('junction-target'), root / 'j')
        self.assertFalse(link.is_symlink())
        self.assertTrue(link.is_dir())

    def test_a_room_path_through_a_junction_is_refused(self):
        from common_room.activation import safe_path
        root = case_dir('junction-room')
        (root / 'real').mkdir()
        (root / 'real' / 'x.json').write_text('{}', encoding='utf-8')
        # Inside the room on purpose: the resolved path stays in the room, so only the link check can refuse it.
        self.junction(root / 'real', root / 'j')
        with self.assertRaises(ValueError) as ctx:
            safe_path(root, root / 'j' / 'x.json')
        self.assertIn('Linked room path refused', str(ctx.exception))
        self.assertEqual(safe_path(root, root / 'real' / 'x.json'), (root / 'real' / 'x.json').absolute())

    def test_the_relay_refuses_a_junction_for_a_seat_folder(self):
        from common_room.relay import Relay, RelayError, RelayFault
        root = case_dir('junction-relay')
        seat = self.junction(case_dir('junction-seat-target'), root / 'relay-writer')
        with self.assertRaises(RelayError) as ctx:
            Relay._prepare_dir(None, seat)          # uses no instance state
        self.assertIn('must not be a symlink or junction', str(ctx.exception))
        with self.assertRaises(RelayFault) as fault:
            Relay._dir_identity(seat)
        self.assertEqual(fault.exception.code, 'linked_folder')

    def test_the_runner_never_lists_a_junctioned_relay_folder(self):
        from common_room import agent_worker as aw
        root = case_dir('junction-runner')
        relay = root / 'relay'
        (relay / 'seat-a' / 'requests').mkdir(parents=True)
        (relay / 'seat-a' / 'requests' / 'a.json').write_text('{}', encoding='utf-8')
        target = case_dir('junction-runner-target')
        (target / 'requests').mkdir()
        (target / 'requests' / 't.json').write_text('{}', encoding='utf-8')
        self.junction(target, relay / 'seat-j')                                 # a whole seat folder
        (relay / 'seat-c').mkdir()
        self.junction(target / 'requests', relay / 'seat-c' / 'requests')      # only its requests folder
        self.assertFalse(aw._plain_folder(relay / 'seat-j'))
        snapshot = aw.relay_snapshot(root)
        self.assertNotIn('seat-j', snapshot['files'])
        listed = {name for entries in snapshot['files'].values() for (_, name) in entries}
        self.assertEqual(listed, {'a.json'}, json.dumps(sorted(listed)))

    def test_a_junctioned_plans_folder_is_refused(self):
        from common_room import plans
        from common_room.crew_client import CrewError
        outside = case_dir('junction-plans-target')
        link = self.junction(outside, case_dir('junction-plans') / 'orchestra')
        with self.assertRaises(CrewError):
            plans.resolve_plans_root(link)
        with self.assertRaises(CrewError):
            plans._checked_root(link)
        self.assertIs(plans.folder_mark(link)[0], True)


if __name__ == '__main__':
    unittest.main()
