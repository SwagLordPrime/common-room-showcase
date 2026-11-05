"""A4d: crew/fast_tests.py reports a temp folder it cannot remove as that file's failure, instead of aborting the run.

The cleanup failure is injected below the runner's own cleanup call: removing one named file (held.txt, which the fake
test file writes into its own temp folder) raises PermissionError(13, '[WinError 32] ...'), the way Windows refuses a
file another process still has open. So these tests need no timing and run on any OS. A runner that swallows cleanup
errors (ignore_cleanup_errors=True, rmtree(ignore_errors=True), a bare except) or retries without saying so fails them.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import contextlib, importlib.util, io, os, re, shutil, unittest
from unittest import mock

from tests.helpers import case_dir

MARKER = '=== COMBINED FINAL RESULT (reruns replace initial attempts) ==='


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FAST = load('fast_tests_under_test_a4d', 'crew/fast_tests.py')
CUTOVER = load('cutover_upgrade_judge_a4d', 'crew/cutover_upgrade.py')

ALPHA = ('import unittest\n'
         'class Alpha(unittest.TestCase):\n'
         '    def test_one(self):\n'
         '        self.assertTrue(True)\n'
         '    def test_two(self):\n'
         '        self.assertTrue(True)\n')
HELD = ('import tempfile, unittest\n'
        'from pathlib import Path\n'
        'class Held(unittest.TestCase):\n'
        '    def test_writes_into_its_temp_folder(self):\n'
        '        Path(tempfile.gettempdir(), "held.txt").write_text("x", encoding="ascii")\n')


class CleanupFailureTests(unittest.TestCase):
    def setUp(self):
        self.root = case_dir('fast-cleanup-root')
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / 'tests').mkdir()
        (self.root / 'tests' / '__init__.py').write_text('', encoding='ascii')
        (self.root / 'tests' / 'test_alpha.py').write_text(ALPHA, encoding='ascii')
        (self.root / 'tests' / 'test_held.py').write_text(HELD, encoding='ascii')
        self.output = self.root / 'out' / 'SUITE.txt'
        self.serial = self.root / 'serial' / 'serial.local.txt'
        self.leftovers = []
        self.addCleanup(self.remove_leftovers)

    def remove_leftovers(self):
        while self.leftovers:                     # each leftover folder is removed once, for real (no injection now)
            shutil.rmtree(self.leftovers.pop(), ignore_errors=False)

    def run_with(self, refuse, jobs):
        """Run the runner with os.unlink refusing held.txt while refuse(call_number) is true."""
        real_unlink = os.unlink
        calls = []
        def unlink(path, *args, **kwargs):
            if os.path.basename(os.fsdecode(path)) == 'held.txt':
                calls.append(path)
                if refuse(len(calls)):
                    raise PermissionError(13, '[WinError 32] The process cannot access the file because it is being used '
                                              'by another process (injected by test_fast_tests_cleanup)', os.fsdecode(path))
            return real_unlink(path, *args, **kwargs)
        printed = io.StringIO()
        with mock.patch.object(FAST.os, 'unlink', unlink), mock.patch.object(FAST, 'CLEANUP_RETRY_PAUSE', 0), \
                contextlib.redirect_stdout(printed):
            code = FAST.run(self.root, self.output, jobs, self.serial, 'test*.py')
        text = self.output.read_text(encoding='utf-8')
        for path in re.findall(r'^' + re.escape(FAST.CLEANUP_ERROR_LINE) + r'could not remove (.+?) after ', text, re.M):
            self.leftovers.append(path)
        return code, text, calls

    def sections(self, text):
        return re.findall(r'^=== (test_\w+\.py): ([^\n]*?), [0-9.]+s ===$', text, re.M)

    def test_case_one_a_lasting_cleanup_failure_fails_only_that_file_and_keeps_every_result(self):
        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                self.leftovers.clear()
                code, text, calls = self.run_with(lambda n: True, jobs)
                named = len(self.leftovers)
                self.remove_leftovers()
                self.assertEqual(code, 1)
                self.assertEqual(len(re.findall(r'^' + re.escape(MARKER) + r'$', text, re.M)), 1)
                final = text.split(MARKER, 1)[1]
                self.assertRegex(final, r'\A\s*Ran 3 tests in [0-9.]+s\n+FAILED \(errors=1\)\n\Z')
                names = [name for name, _ in self.sections(text)]
                self.assertEqual(sorted(names), ['test_alpha.py', 'test_held.py'], 'every file keeps its section')
                self.assertIn(FAST.CLEANUP_ERROR_LINE + 'could not remove ', text)
                self.assertIn('it is left behind', text)
                self.assertEqual(named, 2 if jobs == 2 else 1, 'the leftover path is named for each attempt')
                retries = re.findall(r'^' + re.escape(FAST.CLEANUP_RETRY_LINE) + r'\d of \d: ', text, re.M)
                self.assertEqual(len(retries), (FAST.CLEANUP_TRIES - 1) * (2 if jobs == 2 else 1), 'every retry is written')
                if jobs == 2:
                    modes = dict(self.sections(text))
                    self.assertEqual(modes['test_held.py'], 'rerun alone; temp cleanup failed in parallel')
                    self.assertFalse(self.serial.exists() and 'test_held.py' in self.serial.read_text(encoding='utf-8'))
                ok, reasons = CUTOVER.judge_suite(code, text, set())
                self.assertFalse(ok, 'judge_suite must refuse a run with a cleanup failure')
                self.assertTrue(reasons)

    def test_case_two_a_failure_that_clears_after_the_first_try_passes_and_records_the_retry(self):
        code, text, calls = self.run_with(lambda n: n == 1, 1)
        self.assertEqual(code, 0, text)
        self.assertTrue(CUTOVER.judge_suite(code, text, set())[0], text)
        held = text.split('=== test_held.py: ', 1)[1].split('\n=== ', 1)[0]
        self.assertIn(FAST.CLEANUP_RETRY_LINE + '1 of ' + str(FAST.CLEANUP_TRIES - 1) + ': ', held,
                      'the retry is recorded in that file\'s own section')
        self.assertIn('[WinError 32]', held)
        self.assertNotIn(FAST.CLEANUP_ERROR_LINE, text)
        self.assertEqual(self.leftovers, [])
        self.assertGreaterEqual(len(calls), 2, 'the runner really tried again')

    def test_case_three_a_cleanup_failure_only_in_parallel_is_rerun_alone_and_not_recorded_serial(self):
        code, text, calls = self.run_with(lambda n: n <= FAST.CLEANUP_TRIES, 2)
        self.remove_leftovers()
        self.assertEqual(code, 0, text)
        self.assertTrue(CUTOVER.judge_suite(code, text, set())[0], text)
        self.assertIn('=== test_held.py: initial parallel failure; rerun alone ===', text)
        notes = text.split('=== test_held.py: initial parallel failure; rerun alone ===', 1)[1]
        self.assertIn(FAST.CLEANUP_ERROR_LINE, notes.split('\n=== ', 1)[0], 'the notes keep the cleanup failure')
        self.assertEqual(dict(self.sections(text))['test_held.py'], 'rerun alone; temp cleanup failed in parallel')
        self.assertFalse(self.serial.exists(), 'a cleanup-only failure is not a parallel-only failure')
        final = text.split(MARKER, 1)[1]
        self.assertRegex(final, r'\A\s*Ran 3 tests in [0-9.]+s\n+OK\n\Z')

    def test_a_read_only_file_in_the_temp_folder_is_still_removed_with_no_cleanup_line(self):
        # tempfile.TemporaryDirectory, which the runner used at 356bbac, made read-only entries writable before removing
        # them; the runner's own removal keeps that, so a test leaving a read-only file is not a cleanup failure.
        (self.root / 'tests' / 'test_readonly.py').write_text(
            'import os, stat, tempfile, unittest\n'
            'from pathlib import Path\n'
            'class ReadOnly(unittest.TestCase):\n'
            '    def test_leaves_a_read_only_file(self):\n'
            '        p = Path(tempfile.gettempdir(), "ro.txt")\n'
            '        p.write_text("x", encoding="ascii")\n'
            '        os.chmod(p, stat.S_IREAD)\n', encoding='ascii')
        made = []
        real_mkdtemp = FAST.tempfile.mkdtemp
        def recording_mkdtemp(*args, **kwargs):
            made.append(real_mkdtemp(*args, **kwargs))
            return made[-1]
        with mock.patch.object(FAST.tempfile, 'mkdtemp', recording_mkdtemp):
            code, text, calls = self.run_with(lambda n: False, 2)
        self.assertEqual(code, 0, text)
        self.assertNotIn(FAST.CLEANUP_RETRY_LINE, text)
        self.assertNotIn(FAST.CLEANUP_ERROR_LINE, text)
        self.assertEqual(len(made), 3)
        self.assertEqual([m for m in made if os.path.exists(m)], [], 'every temp folder is gone')

    def test_no_cleanup_failure_writes_no_cleanup_line(self):
        code, text, calls = self.run_with(lambda n: False, 2)
        self.assertEqual(code, 0, text)
        self.assertNotIn(FAST.CLEANUP_RETRY_LINE, text)
        self.assertNotIn(FAST.CLEANUP_ERROR_LINE, text)
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main(argv=sys.argv[:1])
