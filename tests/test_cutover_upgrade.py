"""Tests for crew/cutover_upgrade.py and crew/cutover-upgrade.cmd (core-cutover, plan cr-upgrade-2026-10-01).

Everything runs on throwaway git repos in a temp folder, with the crew's stop, start, window check, suite and the
YES prompt stubbed: no test ever calls Stop Common Room Crew.cmd, a real crew or a real room, and no test ever
points at the live tree. Written before the code: on the base commit crew/cutover_upgrade.py does not exist, so
every case here fails there.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))      # lets `python tests/test_cutover_upgrade.py` run from a tree root

import builtins, contextlib, hashlib, importlib.util, io, json, os, shutil, subprocess, unittest
from contextlib import redirect_stdout
from unittest import mock

from tests.helpers import case_dir

GIT = shutil.which('git')
GIT_ARGS = ['-c', 'user.name=Cutover Test', '-c', 'user.email=cutover@example.invalid', '-c', 'commit.gpgsign=false',
            '-c', 'core.autocrlf=false']
# A4d: the probe child of test_the_command_does_not_wait_for_windows_the_script_opens_with_start checks for stop.txt
# about once a second (ping -n 2), so it exits within about 2 s of it; 30 s is the bound its test waits.
CHILD_EXIT_BOUND = 30
CHILD_WAIT_LOG = ROOT / '.checks' / 'probe_child_wait.log'
# LOFIX (t56fdc2aaea): a test whose script run_command ends by its timeout kill (taskkill /F /T) can leave run_command's
# temp output file behind: run_command unlinks it at once, while a process of the killed tree may still hold it, and the
# failed unlink is ignored by design. Those tests record the file's name and, in one cleanup, wait (bounded) until it
# is free and remove it. A killed tree lets go within a moment; 30 s is the bound.
KILLED_OUTPUT_BOUND = 30
KILLED_OUTPUT_LOG = ROOT / '.checks' / 'killed_output_wait.log'


def free_killed_outputs(test, module):
    """Record the names of the temp files tempfile.mkstemp makes during `test` (run_command's output files), and add
    one cleanup that, for each recorded name only, retries removing it every 0.2 s until it is gone, up to
    KILLED_OUTPUT_BOUND seconds; the test fails naming the file if it is still held at the bound. Each wait is logged
    to KILLED_OUTPUT_LOG. TEMP, TMP, TMPDIR and tempfile.tempdir are not touched, and nothing is removed by pattern."""
    import time
    held = []
    real_mkstemp = module.tempfile.mkstemp
    def recording_mkstemp(*args, **kwargs):
        handle, name = real_mkstemp(*args, **kwargs)
        held.append(name)
        return handle, name
    patcher = mock.patch.object(module.tempfile, 'mkstemp', recording_mkstemp)
    patcher.start()
    def wait_until_free_and_remove():
        patcher.stop()
        started = time.monotonic()
        deadline = started + KILLED_OUTPUT_BOUND
        states = []
        for name in held:
            tries = 0
            while True:
                tries += 1
                try:
                    os.unlink(name)
                    states.append('removed after ' + str(tries) + ' tr' + ('y' if tries == 1 else 'ies'))
                    break
                except FileNotFoundError:
                    states.append('already gone' if tries == 1 else 'gone after ' + str(tries) + ' tries')
                    break
                except PermissionError:
                    if time.monotonic() >= deadline:
                        raise AssertionError('the killed tree still holds ' + name + ' after ' + str(KILLED_OUTPUT_BOUND) + ' s')
                    time.sleep(0.2)
        with open(KILLED_OUTPUT_LOG, 'a', encoding='utf-8') as log:
            log.write(test.id().rsplit('.', 1)[-1] + ': ' + str(len(held)) + ' file(s): ' + ', '.join(states or ['none made']) +
                      '; wait ' + format(time.monotonic() - started, '.2f') + ' s\n')
    test.addCleanup(wait_until_free_and_remove)
    return held


def load_module():
    path = ROOT / 'crew' / 'cutover_upgrade.py'
    spec = importlib.util.spec_from_file_location('cutover_upgrade_under_test', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules['cutover_upgrade_under_test'] = module
    spec.loader.exec_module(module)
    return module


def git(repo, *args, check=True):
    done = subprocess.run([GIT, *GIT_ARGS, '-C', str(repo), *args], capture_output=True, text=True)
    if check:
        assert done.returncode == 0, ' '.join(args) + ': ' + done.stderr
    return done.stdout.strip()


def sha(text):
    return hashlib.sha256(text.encode('utf-8') if isinstance(text, str) else text).hexdigest()


def write(root, rel, text):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode('utf-8') if isinstance(text, str) else text)


PASS = 'Ran 12 tests in 1.0s\n\nOK\n'
FAIL_ONE = ('======\nFAIL: test_refusal (tests.test_agent_worker.RunnerTests.test_refusal)\n------\n'
            'Ran 12 tests in 1.0s\n\nFAILED (failures=1)\n')
FAIL_TWO = ('FAIL: test_refusal (tests.test_agent_worker.RunnerTests.test_refusal)\n'
            'ERROR: test_other (tests.test_other.Other.test_other)\nRan 12 tests in 1.0s\n\nFAILED (failures=1, errors=1)\n')


class World:
    """The stubbed outside world: every call is recorded in order."""
    def __init__(self, suite=(0, PASS), confirm=True, gone=True):
        self.calls, self.lines = [], []
        self.suite_result, self.confirm_result, self.gone_result = suite, confirm, gone
        self.suite_hook = None
        self.stop_error = self.gone_error = self.suite_error = self.confirm_error = None   # an exception to raise instead of working
        self.start_errors = []                                            # one entry per start call: an exception or None
        self.running = True                                               # is any crew window up? (the old crew runs until stopped)
        self.partial_launch = False                                       # a failing start still leaves windows up, like a half-started room

    def stop(self):
        self.calls.append('stop')
        if self.stop_error:
            raise self.stop_error
        self.running = False

    def gone(self):
        self.calls.append('gone')
        if self.gone_error:
            raise self.gone_error
        return self.gone_result

    def start(self):
        self.calls.append('start')
        error = self.start_errors.pop(0) if self.start_errors else None
        if error:
            if self.partial_launch:
                self.running = True                                       # it launched some windows, then failed or could not verify
            raise error
        self.running = True

    def suite(self, live, output):
        self.calls.append('suite')
        if self.suite_error:
            raise self.suite_error
        if self.suite_hook:
            self.suite_hook(live)
        Path(output).write_text(self.suite_result[1], encoding='utf-8')
        return self.suite_result

    def confirm(self):
        self.calls.append('confirm')
        if self.confirm_error:
            raise self.confirm_error
        return self.confirm_result

    def say(self, line=''):
        self.lines.append(line)

    @property
    def text(self):
        return '\n'.join(self.lines)


@unittest.skipIf(GIT is None, 'git is not available')
class CutoverBase(unittest.TestCase):
    """A live tree with every category the switch has to get right (see build_live)."""
    def setUp(self):
        self.mod = load_module()
        self.fx = self.build_live()

    def build_live(self, tip_files=None, tip_adds_runtime=False):
        root = case_dir('cutover')
        origin = root / 'origin'
        origin.mkdir()
        git(origin, 'init', '-q', '-b', 'live-branch')
        h_gitignore = '.runtime/\n.checks/\n__pycache__/\n'
        for rel, text in {'.gitignore': h_gitignore, 't1.txt': 'h-t1\n', 'm1.txt': 'h-m1\n', 'd1.txt': 'h-d1\n',
                          's1.txt': 'same\n', 'sub/deep.txt': 'deep\n'}.items():
            write(origin, rel, text)
        git(origin, 'add', '-A')
        git(origin, 'commit', '-q', '-m', 'H')
        head = git(origin, 'rev-parse', 'HEAD')
        git(origin, 'checkout', '-q', '-b', 'upgrade')
        b_gitignore = h_gitignore + 'WINDOWS_*.txt\n'
        write(origin, '.gitignore', b_gitignore)
        write(origin, 'm1.txt', 'b-m1\n')
        write(origin, 'u1.txt', 'b-u1\n')
        git(origin, 'add', '-A')
        git(origin, 'commit', '-q', '-m', 'B baseline')
        baseline = git(origin, 'rev-parse', 'HEAD')
        write(origin, 'm1.txt', 't-m1\n')                    # modified in the live tree AND changed by the tip
        write(origin, 't1.txt', 'new-t1\n')                  # tracked and untouched live, changed by the tip
        write(origin, 'a1.txt', 'tip-added\n')               # added by the tip
        write(origin, 'crew/CUTOVER_ALLOWED.txt', '')
        (origin / 'd1.txt').unlink()                         # deleted by the tip
        for rel, text in (tip_files or {}).items():
            write(origin, rel, text)
        if tip_adds_runtime:
            write(origin, '.runtime/oops.txt', 'a tracked runtime file')
        git(origin, 'add', '-A', '-f')
        git(origin, 'commit', '-q', '-m', 'T tip')
        tip = git(origin, 'rev-parse', 'HEAD')
        git(origin, 'checkout', '-q', 'live-branch')
        live = root / 'live'
        subprocess.run([GIT, *GIT_ARGS, 'clone', '-q', str(origin), str(live)], check=True, capture_output=True)
        git(live, 'checkout', '-q', '-B', 'live-branch', head)
        git(live, 'branch', '-q', '-f', 'upgrade', 'origin/upgrade')
        # the live tree's dirt: modified tracked file, an untracked baseline file, and things outside the manifest
        write(live, 'm1.txt', 'b-m1\n')
        write(live, 'u1.txt', 'b-u1\n')
        write(live, 'evidence/e1.txt', 'a screenshot, not in the manifest\n')
        write(live, 'WINDOWS_AGENT.txt', 'old test output, untracked, not in the manifest\n')
        write(live, '.runtime/handoffs/room.sqlite', b'\x00room state\x00')
        write(live, '.runtime/handoffs/k-credential.json', '{"token": "fake-test-token"}')
        write(live, '.checks/scratch.txt', 'test scratch')
        manifest = root / 'BASELINE_MANIFEST.txt'
        manifest.write_text(
            '# BASELINE_MANIFEST (test)\n# baseline commit ' + baseline + ' on upgrade (parent ' + head + ')\n'
            '# columns: path | sha256 in tree-next | sha256 in live tree | match\n## COPIED\n'
            '.gitignore | ' + sha(b_gitignore) + ' | ' + sha(h_gitignore) + ' | tree-next only\n'
            'm1.txt | ' + sha('b-m1\n') + ' | ' + sha('b-m1\n') + ' | match\n'
            'u1.txt | ' + sha('b-u1\n') + ' | ' + sha('b-u1\n') + ' | match\n'
            '## EXCLUDED (not copied)\nevidence/ | excluded\nWINDOWS_AGENT.txt | excluded\n', encoding='utf-8')
        return {'root': root, 'origin': origin, 'live': live, 'head': head, 'baseline': baseline, 'tip': tip,
                'manifest': manifest, 'backups': root / 'backups'}

    def cutover(self, world, **kw):
        fx = self.fx
        args = dict(target='upgrade', live_branch='live-branch', stop_crew=world.stop, windows_gone=world.gone,
                    start_crew=world.start, run_suite=world.suite, confirm=world.confirm, say=world.say)
        args.update(kw)
        return self.mod.Cutover(fx['live'], fx['manifest'], fx['backups'], fx['live'] / 'crew' / 'CUTOVER_ALLOWED.txt', **args)

    # ---- the state a rollback must restore, and the checks that nothing else moved ----
    def state(self):
        live = self.fx['live']
        runtime = {}
        for path in sorted((live / '.runtime').rglob('*')):
            if path.is_file():
                stat = path.stat()
                runtime[path.relative_to(live).as_posix()] = (stat.st_size, stat.st_mtime_ns, sha(path.read_bytes()))
        checks = {p.relative_to(live).as_posix(): sha(p.read_bytes()) for p in (live / '.checks').rglob('*') if p.is_file()}
        return {'head': git(live, 'rev-parse', 'HEAD'), 'branch': git(live, 'rev-parse', '--abbrev-ref', 'HEAD'),
                'status': subprocess.run([GIT, '-C', str(live), 'status', '--porcelain=v1', '-z'], capture_output=True).stdout,
                'files': self.mod.inventory(live), 'runtime': runtime, 'checks': checks}

    def assert_runtime_untouched(self, before):
        self.assertEqual(self.state()['runtime'], before['runtime'], '.runtime must never change')

    def tracked_text(self, rel):
        return (self.fx['live'] / rel).read_text(encoding='utf-8')












# ---------------------------------------------------------------------------------------------------------------
# core-cutover-fix: the rollback's .runtime check. On 2026-10-02 the real rollback restored the branch and every file
# but reported failure because .runtime sizes and times had changed, which they always do around a room. Now: nothing
# that was there may be missing (journal and temp files excepted) and the key files must be unchanged, unless the new
# crew was started, since every room start rewrites them.
# ---------------------------------------------------------------------------------------------------------------




# ---------------------------------------------------------------------------------------------------------------
# turnons integration (rulings #1214, #1215) and SW3-RB (2026-10-31): a new crew that grows a scope rewrites
# .runtime/handoffs/enrollment.json, and its start appends the new members to the grant in room.sqlite. The cutover saves
# that file before the switch. On a rollback after the new crew started it keeps an append-only growth the room reads
# (the old code with the seat-dot start check refuses the older file then, activation.py:250; SW3I 3.3, V1, V2) and puts
# back anything else, before the old crew starts. The stubbed crews in test_cutover_runtime.py behave like 5d5ee83's.
# The fixtures are real enrollment shapes (validate_enrollment accepts them); CHANGED edits a label, so it is no growth.
# ---------------------------------------------------------------------------------------------------------------

def _enrollment(actors, members, label='Room'):
    rows = [{'id': a, 'role': 'worker', 'label': label if a == 'claude-room' else a.title(), 'session': 'fixture'} for a in actors]
    return json.dumps({'version': 1, 'actors': rows,
                       'authorities': [{'id': 'orchestra', 'members': members, 'scope': 'Fictional orchestra scope'}]}) + '\n'

OLD_ENROLLMENT = _enrollment(['claude-room'], ['keeper', 'claude-room'])
GROWN_ENROLLMENT = _enrollment(['claude-room', 'dot'], ['keeper', 'claude-room', 'dot'])
CHANGED_ENROLLMENT = _enrollment(['claude-room'], ['keeper', 'claude-room'], label='Room renamed')





class DefaultManifestTests(unittest.TestCase):
    # Keep historical test IDs for the split audit; assertions now bind the 223eda6 baseline.
    """Ruling #1296: Shane's second switch-over runs the same two lines, so the tool's DEFAULT manifest must be the one
    made for it: baseline c4462c3 (the live tree since the first switch-over), the same 102 paths, each hash equal to
    that path's blob at c4462c3. Read from the estate the default live tree sits in; nothing live is opened."""
    def setUp(self):
        self.mod = load_module()
        self.estate = self.mod.find_estate_root(self.mod.DEFAULT_LIVE)
        if self.estate is None:
            self.skipTest('the estate root of the default live tree is not on this machine')
        self.path = self.estate.joinpath(*self.mod.MANIFEST_RELATIVE)

    def test_the_default_manifest_is_the_second_switch_over_one_and_parses(self):
        self.assertEqual(self.mod.MANIFEST_RELATIVE[-1], 'BASELINE_MANIFEST_223eda6.txt')
        self.assertTrue(self.path.is_file(), self.path)
        baseline, copied = self.mod.parse_manifest(self.path)
        self.assertTrue(baseline.startswith('223eda6'), baseline)
        self.assertEqual(len(copied), 2115)
        self.assertTrue(all(tree_next == live for tree_next, live in copied.values()), 'both columns are the 223eda6 hash')
        here = self.mod.find_estate_root(Path(self.mod.__file__).resolve().parent)   # what main() uses for the default
        if here is not None:
            self.assertEqual(here.joinpath(*self.mod.MANIFEST_RELATIVE), self.path)

    def test_every_hash_is_its_c4462c3_blob(self):
        baseline, copied = self.mod.parse_manifest(self.path)
        repo = ROOT     # the integration repo shares the live tree's objects; never the live tree
        if GIT is None or not (repo / '.git').exists():
            self.skipTest('the integration repo is not on this machine')
        entries = sorted(copied.items())
        request = ''.join(baseline + ':' + rel + '\n' for rel, _ in entries).encode('utf-8')
        shown = subprocess.run([GIT, '-C', str(repo), 'cat-file', '--batch'], input=request, capture_output=True)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        stream, mismatched = io.BytesIO(shown.stdout), []
        for rel, (blob_sha, _live) in entries:
            header = stream.readline().split()
            self.assertEqual(header[1], b'blob', rel)
            data = stream.read(int(header[2]))
            self.assertEqual(stream.read(1), b'\n', rel)
            if hashlib.sha256(data).hexdigest() != blob_sha:
                mismatched.append(rel)
        self.assertEqual(mismatched, [])


class CutoverUnitTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()

    def test_judge_suite(self):
        judge = self.mod.judge_suite
        self.assertEqual(judge(0, PASS, set()), (True, []))
        self.assertFalse(judge(1, FAIL_ONE, set())[0])
        self.assertTrue(judge(1, FAIL_ONE, {'tests.test_agent_worker.RunnerTests.test_refusal'})[0])
        self.assertTrue(judge(1, FAIL_ONE, {'test_refusal'})[0])
        self.assertFalse(judge(1, FAIL_TWO, {'test_refusal'})[0])
        self.assertTrue(judge(1, FAIL_TWO, {'test_refusal', 'test_other'})[0])
        self.assertFalse(judge(1, 'no summary at all', set())[0])
        self.assertFalse(judge(0, 'Ran 0 tests in 0s\nOK\n', set())[0])
        self.assertFalse(judge(2, 'Ran 5 tests in 1s\nFAILED (errors=1)\n', {'x'})[0])    # failures it cannot name

    def test_fast_final_pass_ignores_only_superseded_parallel_failures(self):
        for total in (1318, 1411):
            text = ('Fast unittest: 8 worker(s)\n'
                    '=== test_retry.py: initial parallel failure; rerun alone ===\n' + FAIL_ONE +
                    '=== test_retry.py: rerun alone; serial-only detected, 1.0s ===\n' + PASS +
                    '\n=== COMBINED FINAL RESULT (reruns replace initial attempts) ===\n'
                    f'Ran {total} tests in 3.0s\n\nOK (skipped=2)\n')
            self.assertEqual(self.mod.judge_suite(0, text, set()), (True, []))

    def test_fast_failed_final_and_failed_rerun_are_refused(self):
        marker = '\n=== COMBINED FINAL RESULT (reruns replace initial attempts) ===\n'
        for text in (marker + 'Ran 12 tests\nFAILED (failures=1)\n',
                     '=== test_retry.py: initial parallel failure; rerun alone ===\n' + FAIL_ONE +
                     '=== test_retry.py: rerun alone, 1.0s ===\n' + FAIL_ONE + marker + 'Ran 12 tests\nFAILED (failures=1)\n'):
            self.assertFalse(self.mod.judge_suite(1, text, set())[0])
            self.assertFalse(self.mod.judge_suite(0, text, set())[0])

    def test_fast_incomplete_or_duplicate_final_summary_is_refused(self):
        self.assertEqual(self.mod.judge_suite(0, PASS + 'Fast unittest: a later diagnostic\n', set()), (True, []))
        marker = '\n=== COMBINED FINAL RESULT (reruns replace initial attempts) ===\n'
        for text in ('Fast unittest: 8 worker(s)\n' + PASS, PASS + marker, PASS + marker + 'Ran 0 tests\nOK\n',
                     PASS + marker + 'Ran 2 tests\n', PASS + marker + PASS + marker + PASS):
            self.assertFalse(self.mod.judge_suite(0, text, set())[0])

    def test_fast_actual_producer_expected_failure_and_skip_are_accepted(self):
        folder = case_dir('fast-expected-producer')
        tests = folder / 'tests'
        tests.mkdir()
        (tests / '__init__.py').write_text('', encoding='utf-8')
        (tests / 'test_expected.py').write_text(
            'import unittest\nclass Expected(unittest.TestCase):\n'
            '    @unittest.expectedFailure\n'
            '    def test_expected(self): self.fail("synthetic known failure")\n'
            '    @unittest.skip("synthetic skip")\n'
            '    def test_skip(self): pass\n', encoding='utf-8')
        spec = importlib.util.spec_from_file_location('fast_producer_under_test', ROOT / 'crew' / 'fast_tests.py')
        producer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(producer)
        output = folder / 'actual-fast-output.txt'
        with redirect_stdout(io.StringIO()):
            code = producer.run(folder, output, 2, folder / 'serial.local.txt', 'test*.py')
        text = output.read_text(encoding='utf-8')
        self.assertEqual(code, 0, text)
        self.assertIn('expected failures=1', text)
        self.assertIn('skipped=1', text)
        self.assertEqual(self.mod.judge_suite(code, text, set()), (True, []))

    def test_fast_spaced_unexpected_success_and_positive_failures_are_refused(self):
        marker = '=== COMBINED FINAL RESULT (reruns replace initial attempts) ===\n'
        for details in ('failures=1', 'errors=1', 'unexpected successes=1'):
            self.assertFalse(self.mod.judge_suite(0, marker + 'Ran 2 tests\nOK (' + details + ')\n', set())[0])
        self.assertTrue(self.mod.judge_suite(0, marker + 'Ran 2 tests\nOK (unexpected successes=0)\n', set())[0])

    def test_fast_marker_quoted_in_earlier_failure_is_not_a_final_section(self):
        marker = '=== COMBINED FINAL RESULT (reruns replace initial attempts) ==='
        text = ('Fast unittest: 8 worker(s)\n'
                '=== test_retry.py: initial parallel failure; rerun alone ===\n'
                'Traceback (most recent call last):\n  self.assertIn(' + repr(marker) + ', output)\n' + FAIL_ONE +
                '=== test_retry.py: rerun alone, 1.0s ===\n' + PASS + marker + '\nRan 12 tests\nOK\n')
        self.assertEqual(self.mod.judge_suite(0, text, set()), (True, []))

    def test_fast_malformed_or_duplicate_detail_fields_are_refused(self):
        marker = '=== COMBINED FINAL RESULT (reruns replace initial attempts) ===\n'
        for details in ('other=1', 'errors=0, errors=0', 'errors=-1', 'errors=oops'):
            self.assertFalse(self.mod.judge_suite(0, marker + 'Ran 2 tests\nOK (' + details + ')\n', set())[0])

    def test_real_switch_suite_uses_eight_workers(self):
        folder = case_dir('fast-suite-command')
        output = folder / 'out with spaces.txt'
        with mock.patch.object(self.mod, 'run_command', return_value=(0, '')) as run:
            self.mod.real_suite(folder, output)
        self.assertEqual(run.call_args.args[0], [folder / 'crew' / 'windows-tests.cmd', output, '--fast', '8'])

    def test_dry_run_suite_stays_serial_and_reports_elapsed(self):
        from types import SimpleNamespace
        folder = case_dir('dry-run-serial')
        manifest = folder / 'manifest.txt'
        manifest.write_text('# baseline commit abc1234\n## COPIED\n', encoding='utf-8')
        args = SimpleNamespace(live=str(folder), manifest=str(manifest), scratch_root=str(folder),
                               target='upgrade', files_from=None, skip_suite=False, live_branch='live', allow=None)
        calls, lines = [], []
        class FakeCut:
            folder = folder_marker = None
            def __init__(self, *args, **kwargs):
                self.suite = kwargs['run_suite']
                self.folder = folder
            def run(self):
                self.suite(folder, folder / 'out.txt')
                return 0
            def rollback_from(self, value):
                return 0
        def captured(live, output, **kwargs):
            calls.append(kwargs)
            return 0, PASS
        with mock.patch.object(self.mod, 'make_scratch', return_value=folder), \
             mock.patch.object(self.mod, 'Cutover', FakeCut), \
             mock.patch.object(self.mod, 'real_suite', side_effect=captured):
            self.assertEqual(self.mod.dry_run(args, lines.append), 0)
        self.assertEqual(calls, [{'fast': False}])
        self.assertTrue(any('elapsed' in line.lower() and 'seconds' in line.lower() for line in lines))

    def test_parse_manifest_and_allowed(self):
        folder = case_dir('cutover-parse')
        manifest = folder / 'm.txt'
        manifest.write_text('\ufeff# BASELINE_MANIFEST\n# baseline commit abc1234 on x\n## COPIED\n'
                            'a/b c.txt | ' + 'a' * 64 + ' | ' + 'b' * 64 + ' | match\n'
                            'bad.txt | nothex | nothex | x\n## EXCLUDED (not copied)\nz.txt | ' + 'c' * 64 + ' | ' + 'd' * 64 + '\n',
                            encoding='utf-8')
        baseline, copied = self.mod.parse_manifest(manifest)
        self.assertEqual(baseline, 'abc1234')
        self.assertEqual(copied, {'a/b c.txt': ('a' * 64, 'b' * 64)})
        allowed = folder / 'allowed.txt'
        allowed.write_text('# comment\n\ntests.x.Y.test_z  # trailing note\n', encoding='utf-8')
        self.assertEqual(self.mod.parse_allowed(allowed), {'tests.x.Y.test_z'})
        self.assertEqual(self.mod.parse_allowed(folder / 'missing.txt'), set())

    def test_inventory_skips_runtime_git_checks_and_pycache_and_lists_runtime_without_content(self):
        root = case_dir('cutover-inv')
        for rel in ('a.txt', 'sub/b.txt', '.runtime/secret.json', '.checks/x.txt', '__pycache__/c.pyc', 'sub/__pycache__/d.pyc'):
            write(root, rel, rel)
        write(root, '.git', 'gitdir: elsewhere')
        self.assertEqual(sorted(self.mod.inventory(root)), ['a.txt', 'sub/b.txt'])
        listing = self.mod.runtime_listing(root)
        self.assertEqual(list(listing), ['secret.json'])
        self.assertEqual(listing['secret.json'][0], len('.runtime/secret.json'))


class CutoverCommandTests(CutoverBase):
    def run_main(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = self.mod.main(list(args))
        return code, out.getvalue()

    def test_dry_run_does_every_step_on_a_scratch_copy_and_touches_nothing_live(self):
        fx = self.fx
        before = self.state()
        real_hash, seen = self.mod.sha256_file, []
        def spy(path):
            seen.append(str(path))
            return real_hash(path)
        with mock.patch.object(self.mod, 'sha256_file', spy):
            code, out = self.run_main('--dry-run', '--live', str(fx['live']), '--manifest', str(fx['manifest']),
                                      '--target', 'upgrade', '--live-branch', 'live-branch', '--skip-suite')
        self.assertEqual(code, 0, out)
        self.assertEqual([p for p in seen if p.lower().startswith(str(fx['live']).lower())], [],
                         'the baseline files come from the baseline commit: no live file may be read')
        self.assertEqual(self.state(), before, 'the live fixture must be exactly as before')
        for phrase in ('Dry run: scratch copy', 'Step 2: all 3 manifest files match', 'Step 4: backing up', 'Step 5: switching',
                       'The suite passed', 'Switched. Rollback folder', 'manual rollback verified', 'never stopped or started'):
            self.assertIn(phrase, out)
        scratch = Path(next(l for l in out.splitlines() if l.startswith('The scratch copy is left at ')).split(' at ', 1)[1].split(' for ')[0])
        self.assertTrue((scratch / 'tree' / '.runtime' / 'handoffs' / 'fake-room.sqlite').is_file())
        self.assertFalse(str(scratch).lower().startswith(str(fx['live']).lower()))
        shutil.rmtree(scratch, ignore_errors=True)

    def test_dry_run_can_copy_the_baseline_files_from_another_tree_and_never_reads_the_live_files(self):
        fx = self.fx
        other = case_dir('cutover-files-from')           # like tree-next: baseline files, and a .gitignore that is NOT the live one
        write(other, 'm1.txt', 'b-m1\n')
        write(other, 'u1.txt', 'b-u1\n')
        write(other, '.gitignore', '.runtime/\n.checks/\n__pycache__/\nWINDOWS_*.txt\n')
        before = self.state()
        real_hash = self.mod.sha256_file
        seen = []
        def spy(path):
            seen.append(str(path))
            return real_hash(path)
        with mock.patch.object(self.mod, 'sha256_file', spy):
            code, out = self.run_main('--dry-run', '--live', str(fx['live']), '--manifest', str(fx['manifest']),
                                      '--target', 'upgrade', '--live-branch', 'live-branch', '--skip-suite',
                                      '--files-from', str(other))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.state(), before)
        live_prefix = str(fx['live']).lower()
        self.assertEqual([p for p in seen if p.lower().startswith(live_prefix)], [], 'no live file may be read')
        shutil.rmtree(Path(next(l for l in out.splitlines() if l.startswith('The scratch copy is left at ')).split(' at ', 1)[1].split(' for ')[0]),
                      ignore_errors=True)

    def test_the_scratch_copy_gets_fresh_file_times_so_git_never_trusts_an_old_stat(self):
        # Seen once in the full suite: a baseline file copied with its OLD mtime and the same size as the committed
        # file can match git's stat cache, so `git status` calls a changed file clean and the before/after status
        # records disagree. The scratch copy must therefore carry the time it was written, not the source's.
        fx = self.fx
        other = case_dir('cutover-old-mtimes')
        for rel, text in (('m1.txt', 'b-m1' + chr(10)), ('u1.txt', 'b-u1' + chr(10))):
            write(other, rel, text)
            os.utime(other / rel, (946684800, 946684800))          # 2000-01-01
        _baseline, copied = self.mod.parse_manifest(fx['manifest'])
        scratch_root = case_dir('cutover-scratch-times')
        scratch = self.mod.make_scratch(fx['live'], copied, 'upgrade', scratch_root, files_from=other)
        self.assertEqual((scratch / 'm1.txt').read_text(encoding='utf-8'), 'b-m1' + chr(10))
        self.assertGreater((scratch / 'm1.txt').stat().st_mtime, 1_600_000_000)
        self.assertIn('M m1.txt', git(scratch, 'status', '--porcelain'))      # the change is seen, not hidden by the stat cache

    def test_dry_run_flags_are_refused_in_a_real_run(self):
        for extra in (['--skip-suite'], ['--allow', 'x'], ['--live-branch', 'other'], ['--files-from', 'x']):
            code, out = self.run_main(*extra)
            self.assertEqual(code, 4, extra)
            self.assertIn('--dry-run only', out)

    def test_a_real_run_only_targets_the_live_tree_path_it_was_built_for(self):
        code, out = self.run_main('--live', str(self.fx['live']), '--manifest', str(self.fx['manifest']))
        self.assertEqual(code, 4)
        self.assertIn('only targets', out)

    def test_a_real_rollback_only_targets_the_live_tree_path_it_was_built_for(self):
        code, out = self.run_main('--live', str(self.fx['live']), '--rollback', str(self.fx['root']))
        self.assertEqual(code, 4)

    @unittest.skipUnless(os.name == 'nt', 'the launcher is a Windows .cmd')
    def test_the_launcher_runs_the_helper_and_returns_its_exit_code(self):
        launcher = ROOT / 'crew' / 'cutover-upgrade.cmd'
        raw = launcher.read_bytes()
        self.assertTrue(raw.isascii() and b'\r\n' in raw and b'\n' not in raw.replace(b'\r\n', b''))
        env = {k: v for k, v in os.environ.items() if k not in ('PYTHONIOENCODING', 'PYTHONUTF8')}
        for args, expected in ((['--help'], 0), (['--bogus-flag'], 2), (['--rollback', 'nowhere', '--live', 'C:\\nowhere'], 4)):
            done = subprocess.run(['cmd', '/c', str(launcher)] + args, capture_output=True, env=env, cwd=str(ROOT))
            self.assertEqual(done.returncode, expected, (args, done.stdout, done.stderr))
        done = subprocess.run(['cmd', '/c', str(launcher), '--help'], capture_output=True, env=env, cwd=str(ROOT))
        self.assertIn(b'--dry-run', done.stdout)
        self.assertIn(b'--rollback', done.stdout)


# ---------------------------------------------------------------------------------------------------------------
# Review #854 and #856: the REAL wrappers (stop, window check, start, suite) fail closed, take paths with spaces, and
# every failure on the way brings the crew back. These call the real functions against a fake tree whose folder names
# contain spaces, like the live tree and its backup folder do.
# ---------------------------------------------------------------------------------------------------------------

def crlf(*lines):
    return ('\r\n'.join(lines) + '\r\n').encode('ascii')


class FakeCrewTree:
    """A tree with spaces in its path holding stand-ins for Stop/Start Common Room Crew.cmd and crew\\windows-tests.cmd.
    None of them touches a process: they leave marker files and exit with the code the test sets in the environment."""
    def __init__(self, name='cutover spaced'):
        self.root = case_dir(name)
        self.live = self.root / 'live tree with spaces'
        self.out = self.root / 'backup folder with spaces'
        self.out.mkdir(parents=True)
        write(self.live, 'Stop Common Room Crew.cmd', crlf('@echo off', 'echo stopped> "%~dp0stop-ran.txt"', 'exit /b %FAKE_STOP_RC%'))
        write(self.live, 'Start Common Room Crew.cmd', crlf('@echo off', 'echo unattended=%CR_UNATTENDED% > "%~dp0start-ran.txt"',
                                                            'exit /b %FAKE_START_RC%'))
        write(self.live, 'crew/windows-tests.cmd', crlf('@echo off', '> "%~1" echo Ran 2 tests in 0.1s', '>> "%~1" echo.',
                                                        '>> "%~1" echo OK', 'exit /b %FAKE_SUITE_RC%'))
        write(self.live, 'crew/slow-tests.cmd', crlf('@echo off', ':loop', 'if exist "%~dp0stop.txt" exit /b 0', 'echo beat>> "%~dp0beat.txt"', 'ping -n 2 127.0.0.1 >nul', 'goto loop'))

    def env(self, **codes):
        base = {'FAKE_STOP_RC': '0', 'FAKE_START_RC': '0', 'FAKE_SUITE_RC': '0'}
        base.update({k: str(v) for k, v in codes.items()})
        return mock.patch.dict(os.environ, base)


@unittest.skipUnless(os.name == 'nt', 'the real wrappers drive cmd.exe')
class RealWrapperTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()
        self.tree = FakeCrewTree()

    def test_the_command_line_quotes_every_part_so_two_spaced_paths_work(self):
        line = self.mod.cmd_line(r'D:\Work Area\a b\x.cmd', r'D:\Work Area\out dir\o.txt')
        self.assertEqual(line, 'cmd /d /s /c ""D:\\Work Area\\a b\\x.cmd" "D:\\Work Area\\out dir\\o.txt""')
        with self.assertRaises(self.mod.ControlError):
            self.mod.cmd_line('bad"name')

    def test_the_suite_runs_with_a_spaced_live_tree_and_a_spaced_output_path(self):
        output = self.tree.out / 'SUITE OUTPUT.txt'
        with self.tree.env():
            code, text = self.mod.real_suite(self.tree.live, output)
        self.assertEqual(code, 0, text)
        self.assertIn('Ran 2 tests', text)
        self.assertTrue(self.mod.judge_suite(code, text, set())[0])

    def test_the_suite_exit_code_and_a_missing_script_are_reported(self):
        output = self.tree.out / 'SUITE OUTPUT.txt'
        with self.tree.env(FAKE_SUITE_RC=1):
            code, text = self.mod.real_suite(self.tree.live, output)
        self.assertEqual(code, 1)
        (self.tree.live / 'crew' / 'windows-tests.cmd').unlink()
        with self.tree.env():
            code, text = self.mod.real_suite(self.tree.live, output)
        self.assertNotEqual(code, 0)
        self.assertFalse(self.mod.judge_suite(code, text, set())[0])

    def test_a_suite_that_times_out_is_killed_with_its_children_and_reported(self):
        import time
        outputs = free_killed_outputs(self, self.mod)   # registered first, so it runs last: after stop.txt below
        # a stand-in suite that writes a heartbeat line every second until it is killed
        (self.tree.live / 'crew' / 'windows-tests.cmd').write_bytes((self.tree.live / 'crew' / 'slow-tests.cmd').read_bytes())
        beat = self.tree.live / 'crew' / 'beat.txt'
        self.addCleanup(lambda: (self.tree.live / 'crew' / 'stop.txt').write_text('stop', encoding='ascii'))   # safety: a survivor exits
        with self.tree.env():
            code, text = self.mod.real_suite(self.tree.live, self.tree.out / 'o.txt', timeout=3)
        self.assertEqual(code, 124)
        self.assertIn('timed out', text)
        self.assertTrue(beat.is_file(), 'the stand-in suite really started')
        size = beat.stat().st_size
        self.assertGreater(size, 0)
        time.sleep(4)                                           # a surviving script would write several more heartbeats
        self.assertEqual(beat.stat().st_size, size, 'the suite process tree must have been killed')
        self.assertEqual(len(outputs), 1, 'run_command makes exactly one temp output file')

    def test_stop_runs_the_spaced_script_and_fails_closed(self):
        stop = self.mod.real_stop(self.tree.live)
        with self.tree.env():
            stop()
        self.assertTrue((self.tree.live / 'stop-ran.txt').is_file())
        with self.tree.env(FAKE_STOP_RC=3), self.assertRaises(self.mod.ControlError) as caught:
            stop()
        self.assertIn('code 3', str(caught.exception))
        (self.tree.live / 'Stop Common Room Crew.cmd').unlink()
        with self.assertRaises(self.mod.ControlError):
            stop()

    def test_stop_runs_unattended_and_its_unguarded_pause_and_prompt_do_not_wait(self):
        # core-cutover-fix: the real Stop script ends with a bare `pause`; the tool gives it CR_UNATTENDED=1 and stdin from NUL
        import time
        write(self.tree.live, 'Stop Common Room Crew.cmd', crlf('@echo off', '> "%~dp0stop-ran.txt" echo unattended=%CR_UNATTENDED%',
                                                               'pause', 'set /p ANSWER=Type something: ', 'exit /b 0'))
        started = time.monotonic()
        with mock.patch.dict(os.environ):
            os.environ.pop('CR_UNATTENDED', None)          # it must come from the tool, not from whoever runs the tests
            self.mod.real_stop(self.tree.live, timeout=60)()
        self.assertLess(time.monotonic() - started, 30, 'a pause or prompt waited for a key')
        self.assertEqual((self.tree.live / 'stop-ran.txt').read_text(encoding='ascii').strip(), 'unattended=1')

    def test_the_command_does_not_wait_for_windows_the_script_opens_with_start(self):
        # core-cutover-fix: on 2026-10-02 the real Start script finished but the tool waited 300 s, because the crew
        # windows it opened with `start` inherited the output pipe. Here the stand-in Start opens one minimized window
        # (title 'cutover probe child', never CRCREW) that runs about 40 s, or until stop.txt appears.
        import time
        write(self.tree.live, 'child.cmd', crlf('@echo off', 'set /a N=0', ':loop', 'if exist "%~dp0stop.txt" exit /b 0',
                                                'echo beat>> "%~dp0beat.txt"', 'set /a N+=1', 'if %N% GEQ 40 exit /b 0',
                                                'ping -n 2 127.0.0.1 >nul', 'goto loop'))
        write(self.tree.live, 'Start Common Room Crew.cmd', crlf('@echo off',
                                                                'start "cutover probe child" /min cmd /d /c ""%~dp0child.cmd""',
                                                                'echo started the child', 'exit /b 0'))
        # A4d: the child inherits run_command's temp output file (in TEMP, which under crew\fast_tests.py is this test
        # file's own temp folder). The test learns that file's name from mkstemp, and one cleanup first writes stop.txt
        # and then waits, bounded, until the file is free, which proves the child has exited; then it deletes the file.
        # Both are in one cleanup because addCleanup runs last in, first out.
        held = []
        real_mkstemp = self.mod.tempfile.mkstemp
        def recording_mkstemp(*args, **kwargs):
            handle, name = real_mkstemp(*args, **kwargs)
            held.append(name)
            return handle, name
        def stop_the_child_and_wait_until_it_has_exited():
            (self.tree.live / 'stop.txt').write_text('stop', encoding='ascii')
            waited = time.monotonic()
            deadline = waited + CHILD_EXIT_BOUND
            for name in held:
                while True:
                    try:
                        os.unlink(name)
                        break
                    except FileNotFoundError:
                        break                      # run_command could remove it: nothing held it any more
                    except PermissionError:
                        if time.monotonic() >= deadline:
                            raise AssertionError('the probe child did not exit within ' + str(CHILD_EXIT_BOUND) +
                                                 ' s of stop.txt: it still holds ' + name)
                        time.sleep(0.2)
            with open(CHILD_WAIT_LOG, 'a', encoding='utf-8') as log:
                log.write('probe child exit wait: ' + format(time.monotonic() - waited, '.2f') + ' s\n')
        self.addCleanup(stop_the_child_and_wait_until_it_has_exited)
        started = time.monotonic()
        # the environment is built here, not with unattended_env(), so that on the base commit this test fails on the
        # wait itself (run_command times out after 20 s) and not on a missing helper
        with mock.patch.object(self.mod.tempfile, 'mkstemp', recording_mkstemp):
            code, out = self.mod.run_command([self.tree.live / 'Start Common Room Crew.cmd'], self.tree.live, 20,
                                             dict(os.environ, CR_UNATTENDED='1'))
        self.assertEqual(len(held), 1, 'run_command makes exactly one temp output file')
        elapsed = time.monotonic() - started
        self.assertEqual(code, 0, out)
        self.assertIn('started the child', out)
        self.assertLess(elapsed, 10, 'the tool waited for the window the script opened')
        deadline = time.monotonic() + 10
        while not (self.tree.live / 'beat.txt').is_file() and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertTrue((self.tree.live / 'beat.txt').is_file(), 'the child window really started and outlived the script')

    def test_ctrl_c_while_a_script_runs_kills_its_whole_tree_and_still_interrupts(self):
        # With the console as Start's stdin, a Start cmd that got the same Ctrl+C would wait at "Terminate batch job (Y/N)?"
        # and could go on launching crew windows after the rollback: run_command must kill the tree, then re-raise.
        # Popen and taskkill are both fakes here: a real taskkill /T on a made-up pid could hit an unrelated process.
        class FakeProcess:
            pid, returncode, args = 424242, None, 'fake'
            def __init__(self, *a, **k):
                self.polls = 0
            def poll(self):                              # the Ctrl+C arrives while the tool is waiting
                self.polls += 1
                if self.polls == 1:
                    raise KeyboardInterrupt
                return 0
            def wait(self, timeout=None):                # the reap after the tree kill
                return 0
            def communicate(self, timeout=None):        # what the base commit calls instead
                raise KeyboardInterrupt
            def kill(self):
                pass
        killed = []
        def fake_run(args, **k):
            killed.append(list(args))
            return subprocess.CompletedProcess(args, 0, b'', b'')
        with mock.patch.object(self.mod.subprocess, 'Popen', FakeProcess), mock.patch.object(self.mod.subprocess, 'run', fake_run):
            with self.assertRaises(KeyboardInterrupt):
                self.mod.run_command([self.tree.live / 'Start Common Room Crew.cmd'], self.tree.live, 60)
        self.assertIn(['taskkill', '/F', '/T', '/PID', '424242'], killed)

    def test_start_gets_the_console_and_180_seconds_and_stop_gets_nul(self):
        # claude-room ruling #1055: Start's own room wait uses `timeout /t 3`, which refuses NUL, so Start gets the console
        calls = []
        def fake_run(parts, cwd, timeout, env=None, stdin=subprocess.DEVNULL):
            calls.append((Path(parts[0]).name, timeout, (env or {}).get('CR_UNATTENDED'), stdin))
            return 0, ''
        with mock.patch.object(self.mod, 'run_command', fake_run), mock.patch.dict(os.environ):
            os.environ.pop('CR_UNATTENDED', None)
            self.mod.real_stop(self.tree.live)()
            self.mod.real_start(self.tree.live, count=lambda: 1)()
        self.assertEqual(calls, [('Stop Common Room Crew.cmd', 120, '1', subprocess.DEVNULL),
                                 ('Start Common Room Crew.cmd', 180, '1', None)])

    def test_a_failed_start_shows_the_last_40_lines_of_its_output(self):
        write(self.tree.live, 'Start Common Room Crew.cmd', crlf('@echo off', 'for /l %%i in (1,1,50) do echo start line %%i',
                                                                'exit /b 1'))
        with self.assertRaises(self.mod.ControlError) as caught:
            self.mod.real_start(self.tree.live, count=lambda: 1)()
        message = str(caught.exception)
        self.assertIn('exited with code 1', message)
        for n in (11, 30, 50):
            self.assertIn('start line ' + str(n) + '\n', message + '\n')
        self.assertNotIn('start line 10\n', message + '\n')
        self.assertEqual(len([l for l in message.splitlines() if 'start line' in l]), 40)

    def test_a_start_that_runs_past_its_limit_is_killed_and_shows_its_output(self):
        outputs = free_killed_outputs(self, self.mod)   # registered first, so it runs last: after stop.txt below
        write(self.tree.live, 'Start Common Room Crew.cmd', crlf('@echo off', 'echo started, now waiting for the room',
                                                                ':loop', 'if exist "%~dp0stop.txt" exit /b 0',
                                                                'ping -n 2 127.0.0.1 >nul', 'goto loop'))
        self.addCleanup(lambda: (self.tree.live / 'stop.txt').write_text('stop', encoding='ascii'))   # safety: a survivor exits
        with self.assertRaises(self.mod.ControlError) as caught:
            self.mod.real_start(self.tree.live, count=lambda: 1, timeout=3)()
        message = str(caught.exception)
        self.assertIn('did not finish within 3 seconds', message)
        self.assertIn('started, now waiting for the room', message)
        self.assertEqual(len(outputs), 1, 'run_command makes exactly one temp output file')

    def test_start_runs_the_spaced_script_unattended_and_waits_for_a_crew_window(self):
        counts, ticks = iter([0, 0, 2]), iter(range(0, 1000, 3))
        start = self.mod.real_start(self.tree.live, count=lambda: next(counts), sleep=lambda s: None, now=lambda: next(ticks))
        with self.tree.env():
            start()
        self.assertEqual((self.tree.live / 'start-ran.txt').read_text(encoding='ascii').strip(), 'unattended=1')

    def test_start_fails_closed_on_the_exit_code_and_on_no_window(self):
        with self.tree.env(FAKE_START_RC=1), self.assertRaises(self.mod.ControlError) as caught:
            self.mod.real_start(self.tree.live, count=lambda: 2)()
        self.assertIn('code 1', str(caught.exception))
        ticks = iter(range(0, 1000, 20))
        with self.tree.env(), self.assertRaises(self.mod.ControlError) as caught:
            self.mod.real_start(self.tree.live, count=lambda: 0, sleep=lambda s: None, now=lambda: next(ticks), wait=60)()
        self.assertIn('no crew window appeared', str(caught.exception))

    def test_start_says_why_when_the_window_check_itself_fails(self):
        def broken():
            raise self.mod.ControlError('the window check failed (exit code 1)')
        ticks = iter(range(0, 1000, 20))
        with self.tree.env(), self.assertRaises(self.mod.ControlError) as caught:
            self.mod.real_start(self.tree.live, count=broken, sleep=lambda s: None, now=lambda: next(ticks), wait=60)()
        self.assertIn('window check failed', str(caught.exception))

    def test_a_failed_or_unreadable_window_check_is_never_read_as_gone(self):
        import subprocess
        done = lambda code, out: (lambda *a, **k: subprocess.CompletedProcess(a, code, stdout=out, stderr=b''))
        self.assertEqual(self.mod.crew_window_count(done(0, b'3\r\n')), 3)
        self.assertEqual(self.mod.crew_window_count(done(0, b'0')), 0)
        for label, runner in (('exit 1', done(1, b'')), ('exit 1 with output', done(1, b'0')), ('not a number', done(0, b'abc')),
                              ('empty', done(0, b'')), ('negative', done(0, b'-1'))):
            with self.subTest(label), self.assertRaises(self.mod.ControlError):
                self.mod.crew_window_count(runner)
        def timeout(*a, **k):
            raise subprocess.TimeoutExpired('powershell', 60)
        def missing(*a, **k):
            raise FileNotFoundError('powershell')
        for runner in (timeout, missing):
            with self.assertRaises(self.mod.ControlError):
                self.mod.crew_window_count(runner)
        self.assertTrue(self.mod.real_windows_gone(count=lambda: 0))
        self.assertFalse(self.mod.real_windows_gone(count=lambda: 2))
        def unknown():
            raise self.mod.ControlError('could not tell')
        self.assertFalse(self.mod.real_windows_gone(count=unknown))

    def test_the_window_check_uses_a_fast_powershell_query_not_tasklist_v(self):
        source = (ROOT / 'crew' / 'cutover_upgrade.py').read_text(encoding='utf-8')
        self.assertIn('MainWindowTitle', source)
        self.assertNotIn("'/v'", source)






if __name__ == '__main__':
    argv = [sys.argv[0]] + [arg for arg in sys.argv[1:] if arg != '.']
    program = unittest.main(argv=argv, exit=False)
    result = program.result
    bad = len(result.failures) + len(result.errors) + len(result.unexpectedSuccesses)
    print(f'{result.testsRun - bad}/{result.testsRun} tests passed')
    raise SystemExit(0 if result.wasSuccessful() else 1)
