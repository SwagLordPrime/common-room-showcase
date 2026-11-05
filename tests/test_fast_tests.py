"""End-to-end fast-runner witnesses using tiny fictional suites only."""
import os
from pathlib import Path
import subprocess
import shutil
import sys
import unittest
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.helpers import ROOT, case_dir

RUNNER = ROOT / 'crew' / 'fast_tests.py'

class FastRunnerTests(unittest.TestCase):
    def suite(self):
        root = case_dir('fast-runner-suite')
        (root / 'tests').mkdir()
        (root / 'tests' / '__init__.py').write_text('', encoding='utf-8')
        return root

    def fixture(self, root, name, body):
        (root / 'tests' / name).write_text(body, encoding='utf-8')

    def run_fast(self, root, jobs=2):
        output = root / 'combined.txt'
        command = [sys.executable, '-X', 'utf8', '-B', str(RUNNER), '--root', str(root),
                   '--output', str(output), '--jobs', str(jobs),
                   '--serial-list', str(root / 'SERIAL_TESTS.txt')]
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        return result, output.read_text(encoding='utf-8') if output.exists() else ''

    def test_pass_fail_skip_and_combined_counts(self):
        root = self.suite()
        self.fixture(root, 'test_pass.py', 'import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n @unittest.skip("fixture skip")\n def test_skip(self): pass\n')
        self.fixture(root, 'test_fail.py', 'import unittest\nclass T(unittest.TestCase):\n def test_fail(self): self.assertEqual(1, 2)\n')
        result, output = self.run_fast(root)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('Ran 3 tests in ', output)
        self.assertIn('FAILED (failures=1, skipped=1)', output)
        self.assertIn('FAIL: test_fail', output)
        self.assertIn('test_pass.py:', output)
        self.assertIn('test_fail.py:', output)
        self.assertIn('rerun alone', output)

    def test_parallel_only_failure_becomes_sticky_serial(self):
        root = self.suite()
        self.fixture(root, 'test_sensitive.py', 'import os, unittest\nclass T(unittest.TestCase):\n def test_serial(self): self.assertEqual(os.environ["COMMON_ROOM_TEST_JOBS"], "1")\n')
        self.fixture(root, 'test_other.py', 'import unittest\nclass T(unittest.TestCase):\n def test_ok(self): pass\n')
        first, output = self.run_fast(root)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn('Ran 2 tests in ', output)
        self.assertIn('serial-only detected', output)
        self.assertEqual((root / 'SERIAL_TESTS.txt').read_text(encoding='utf-8').strip(), 'test_sensitive.py')
        second, output = self.run_fast(root)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn('test_sensitive.py: serial', output)
        self.assertNotIn('serial-only detected', output)
        self.assertIn('Ran 2 tests in ', output)

    def default_run(self, root):
        output = root / 'default-combined.txt'
        done = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(RUNNER),
                               '--root', str(root), '--output', str(output), '--jobs', '2'],
                              capture_output=True, text=True, timeout=60)
        return done, output.read_text(encoding='utf-8') if output.exists() else ''

    def test_default_sticky_list_preserves_tracked_baseline(self):
        root = self.suite()
        (root / 'crew').mkdir()
        baseline = root / 'crew' / 'SERIAL_TESTS.txt'
        original = b'# reviewed baseline\r\ntest_baseline.py\r\n'
        baseline.write_bytes(original)
        body = 'import os, unittest\nclass T(unittest.TestCase):\n def test_serial(self): self.assertEqual(os.environ["COMMON_ROOM_TEST_JOBS"], "1")\n'
        for name in ('test_baseline.py', 'test_sensitive.py'):
            self.fixture(root, name, body)
        first, text = self.default_run(root)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(baseline.read_bytes(), original)
        self.assertIn('test_baseline.py: serial', text)
        self.assertIn('test_sensitive.py: rerun alone; serial-only detected', text)
        local = root / '.checks' / 'serial_tests.local.txt'
        self.assertEqual(local.read_text(encoding='utf-8').splitlines(), ['test_sensitive.py'])
        second, text = self.default_run(root)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotIn('initial parallel failure', text)
        self.assertIn('test_sensitive.py: serial', text)
        self.assertEqual(baseline.read_bytes(), original)

    def test_default_reads_existing_local_list_with_baseline(self):
        root = self.suite()
        (root / 'crew').mkdir()
        (root / '.checks').mkdir()
        baseline = root / 'crew' / 'SERIAL_TESTS.txt'
        baseline.write_text('test_baseline.py\n', encoding='utf-8')
        local = root / '.checks' / 'serial_tests.local.txt'
        local.write_text('test_local.py\n', encoding='utf-8')
        before = (baseline.read_bytes(), local.read_bytes())
        body = 'import os, unittest\nclass T(unittest.TestCase):\n def test_serial(self): self.assertEqual(os.environ["COMMON_ROOM_TEST_JOBS"], "1")\n'
        for name in ('test_baseline.py', 'test_local.py'):
            self.fixture(root, name, body)
        done, text = self.default_run(root)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn('initial parallel failure', text)
        self.assertEqual((baseline.read_bytes(), local.read_bytes()), before)

    def test_each_file_has_its_own_temp_folder(self):
        root = self.suite()
        body = 'import pathlib, tempfile, unittest\nclass T(unittest.TestCase):\n def test_private(self):\n  p = pathlib.Path(tempfile.gettempdir()) / "same-marker"\n  self.assertFalse(p.exists())\n  p.write_text("fixture", encoding="utf-8")\n'
        self.fixture(root, 'test_one.py', body)
        self.fixture(root, 'test_two.py', body)
        result, output = self.run_fast(root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Ran 2 tests in ', output)
        self.assertNotIn('rerun alone', output)

    def test_expected_failure_and_unexpected_success_are_preserved(self):
        root = self.suite()
        self.fixture(root, 'test_expected.py', 'import unittest\nclass T(unittest.TestCase):\n @unittest.expectedFailure\n def test_expected(self): self.fail("expected")\n @unittest.expectedFailure\n def test_unexpected(self): pass\n')
        result, output = self.run_fast(root)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('Ran 2 tests in ', output)
        self.assertIn('expected failures=1', output)
        self.assertIn('unexpected successes=1', output)

    def test_import_error_stays_an_error(self):
        root = self.suite()
        self.fixture(root, 'test_broken.py', 'raise RuntimeError("fictional import failure")\n')
        result, output = self.run_fast(root)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('FAILED (errors=1)', output)
        self.assertIn('fictional import failure', output)

    def test_invalid_worker_count_refused_before_running(self):
        root = self.suite()
        self.fixture(root, 'test_no.py', 'raise RuntimeError("must not run")\n')
        result, output = self.run_fast(root, jobs=-1)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(output, '')
        self.assertIn('jobs', result.stderr)

    def test_serial_list_path_escape_refused(self):
        root = self.suite()
        (root / 'SERIAL_TESTS.txt').write_text('../test_elsewhere.py\n', encoding='utf-8')
        result, output = self.run_fast(root)
        self.assertEqual(result.returncode, 2)
        self.assertIn('serial list', result.stderr.lower())
        self.assertEqual(output, '')

    def test_fixture_layout_and_external_temp_boundary_are_preserved(self):
        root = self.suite()
        (root / 'tests' / 'helpers.py').write_text('from pathlib import Path\nROOT = Path(__file__).resolve().parents[1]\nCHECKS = ROOT / ".checks"\n', encoding='utf-8')
        self.fixture(root, 'test_helper.py', 'import pathlib, tempfile, unittest\nfrom tests import helpers\nclass T(unittest.TestCase):\n def test_private(self):\n  self.assertEqual(helpers.CHECKS, helpers.ROOT / ".checks")\n  self.assertFalse(pathlib.Path(tempfile.gettempdir()).is_relative_to(helpers.CHECKS))\n')
        result, output = self.run_fast(root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Ran 1 test', output)
        self.assertNotIn('rerun alone', output)

    @unittest.skipUnless(os.name == 'nt', 'Windows wrapper execution')
    def test_windows_wrapper_fast_option_and_default_serial(self):
        root = self.suite()
        self.fixture(root, 'test_one.py', 'import unittest\nclass T(unittest.TestCase):\n def test_ok(self): pass\n')
        (root / 'crew').mkdir()
        for name in ('windows-tests.cmd', 'find-python.cmd', 'fast_tests.py', 'SERIAL_TESTS.txt'):
            shutil.copyfile(ROOT / 'crew' / name, root / 'crew' / name)
        wrapper = root / 'crew' / 'windows-tests.cmd'
        for arguments, filename, fast in [('--fast 2', 'fast.txt', True), ('test*.py', 'serial.txt', False)]:
            output = root / filename
            command = f'cmd.exe /d /s /c ""{wrapper}" "{output}" {arguments}"'
            result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            text = output.read_text(encoding='utf-8')
            self.assertIn('Ran 1 test', text)
            self.assertEqual('COMBINED FINAL RESULT' in text, fast)

if __name__ == '__main__':
    program = unittest.main(exit=False)
    result = program.result
    bad = len(result.failures) + len(result.errors) + len(result.unexpectedSuccesses)
    print(f'{result.testsRun - bad}/{result.testsRun} tests passed')
    raise SystemExit(0 if result.wasSuccessful() else 1)
