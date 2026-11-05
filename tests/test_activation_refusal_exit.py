"""Autonomy integration: a room that cannot start for a configuration reason exits 2 with one
clear line, so common_room.keepalive treats repeated quick refusals as a stop, not a retry loop."""
import io, json, unittest
from contextlib import redirect_stderr
from tests.helpers import case_dir
from tests.test_activation import CONFIG


class ActivationRefusalExitTests(unittest.TestCase):
    def test_enrollment_that_drops_a_recorded_seat_exits_2_with_a_message(self):
        from common_room import activation
        root = case_dir('activation-refusal') / 'handoffs'
        activation.start_room(root, CONFIG, []).close()
        shrunk = dict(CONFIG, actors=CONFIG['actors'][:-1],
                      authorities=[dict(a, members=[m for m in a['members'] if m != CONFIG['actors'][-1]['id']])
                                   for a in CONFIG['authorities']])
        (root / 'shrunk.json').write_text(json.dumps(shrunk), encoding='utf-8')
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            activation.main(['--root', str(root), '--enrollment', str(root / 'shrunk.json')])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn('common_room.activation:', err.getvalue())
        self.assertIn('immutable', err.getvalue())
        self.assertNotIn('Traceback', err.getvalue())

    def test_a_missing_or_unreadable_enrollment_file_exits_2_with_one_line(self):
        """fix-board (VERIFY7A I3): an OSError reading --enrollment is a configuration refusal, so
        keepalive stops after its quick refusals instead of restarting the room forever."""
        from common_room import activation
        root = case_dir('activation-refusal') / 'handoffs'
        activation.start_room(root, CONFIG, []).close()
        (root / 'a-folder.json').mkdir()
        for name in ('missing.json', 'a-folder.json'):
            err = io.StringIO()
            with self.subTest(name), redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
                activation.main(['--root', str(root), '--enrollment', str(root / name)])
            self.assertEqual(ctx.exception.code, 2, name)
            lines = err.getvalue().strip().splitlines()
            self.assertEqual(len(lines), 1, err.getvalue())
            self.assertTrue(lines[0].startswith('common_room.activation: The enrollment file could not be read: '), lines)
            self.assertIn(name, lines[0])

    def test_keepalive_stops_on_a_missing_enrollment_instead_of_retrying(self):
        import os, subprocess, sys
        from tests.helpers import ROOT
        root = case_dir('activation-refusal') / 'handoffs'
        root.mkdir()
        child = ['-X', 'utf8', '-B', '-m', 'common_room.activation', '--root', str(root),
                 '--enrollment', str(root / 'missing.json'), '--headless']
        done = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-m', 'common_room.keepalive', '--name', 'room-test',
                               '--max-quick-refusals', '1', '--'] + child, cwd=ROOT, capture_output=True, text=True,
                              timeout=30, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn('The enrollment file could not be read', done.stderr)
        self.assertNotIn('Traceback', done.stdout + done.stderr)
        self.assertEqual(sorted(p.name for p in root.iterdir()), [], 'nothing was started or written')


if __name__ == '__main__':
    unittest.main()
