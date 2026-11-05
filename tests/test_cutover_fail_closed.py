"""Cutover synthetic regression classes, split without changing test bodies."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import builtins, contextlib, hashlib, importlib.util, io, json, os, shutil, subprocess, unittest
from contextlib import redirect_stdout
from unittest import mock
from tests.helpers import case_dir
from tests.test_cutover_upgrade import (GIT, GIT_ARGS, load_module, git, sha, write,
                                       PASS, FAIL_ONE, FAIL_TWO, World, CutoverBase, FakeCrewTree, crlf)

class CutoverFailClosedTests(CutoverBase):
    """Review #854: every failure on the way brings the crew back, or says plainly that it could not."""
    def assert_unchanged(self, before):
        self.assertEqual(self.state(), before)

    def test_a_stop_that_raises_aborts_and_restarts_the_old_crew(self):
        world, before = World(), self.state()
        world.stop_error = self.mod.ControlError('Stop Common Room Crew.cmd exited with code 3')
        code = self.cutover(world).run()
        self.assertEqual(code, 1)
        self.assertEqual(world.calls, ['confirm', 'stop', 'start'])
        self.assertIn('Stopping the crew failed', world.text)
        self.assertIn('code 3', world.text)
        self.assertIn('NOTHING WAS CHANGED', world.text)
        self.assert_unchanged(before)

    def test_a_window_check_that_raises_is_not_proof_the_crew_is_gone(self):
        ticks = iter(range(0, 1000, 5))
        world, before = World(), self.state()
        world.gone_error = self.mod.ControlError('the window check failed (exit code 1)')
        code = self.cutover(world, stop_wait=20, sleep=lambda s: None, now=lambda: next(ticks)).run()
        self.assertEqual(code, 1)
        self.assertNotIn('suite', world.calls)
        self.assertIn('not proven closed', world.text)
        self.assertIn('window check failed', world.text)
        self.assertEqual(world.calls[-1], 'start')
        self.assert_unchanged(before)

    def test_an_unexpected_error_before_the_switch_restarts_the_old_crew(self):
        world, before = World(), self.state()
        def boom(src, dst, *a, **k):
            raise PermissionError('antivirus has the file open')
        with mock.patch.object(self.mod.shutil, 'copy2', boom):
            code = self.cutover(world).run()
        self.assertEqual(code, 1)
        self.assertIn('unexpected error before the switch', world.text)
        self.assertEqual(world.calls[-1], 'start')
        self.assertNotIn('suite', world.calls)
        self.assert_unchanged(before)

    def test_a_restart_that_fails_after_an_abort_says_so_and_exits_5(self):
        write(self.fx['live'], 'm1.txt', 'someone edited this\n')
        world, before = World(), self.state()
        world.start_errors = [self.mod.ControlError('Start Common Room Crew.cmd exited with code 1')]
        code = self.cutover(world).run()
        self.assertEqual(code, 5)
        self.assertIn('THE CREW DID NOT START', world.text)
        self.assertIn('Start Common Room Crew.cmd', world.text)
        self.assert_unchanged(before)

    def safe(self, call):
        """Ctrl+C must never escape the cutover. Without this, an escape at the OLD code would abort the whole unittest run
        (unittest treats KeyboardInterrupt as an abort), so a RED check would print no result at all."""
        try:
            return call()
        except KeyboardInterrupt:
            self.fail('Ctrl+C escaped the cutover')

    # ---- review of ef487af: a new crew that does not start is not a success, and Ctrl+C is handled in every post-stop phase ----

    def test_a_new_crew_that_does_not_start_after_a_good_suite_is_rolled_back_and_the_old_crew_restarted(self):
        world, before = World(), self.state()
        world.start_errors = [self.mod.ControlError('no crew window appeared')]          # the first start is the NEW crew
        code = self.safe(lambda: self.cutover(world).run())
        self.assertEqual(code, 2, world.text)
        self.assertEqual(self.state(), before)
        self.assertEqual(world.calls, ['confirm', 'stop', 'gone', 'suite', 'start', 'stop', 'gone', 'start'])   # new start failed, stopped, old crew started
        self.assertIn('The suite passed, but the new crew did not start', world.text)
        self.assertIn('Rolling back', world.text)
        self.assertIn('Rollback verified', world.text)
        self.assertNotIn('Switched. Rollback folder', world.text)
        self.assertTrue((max(self.fx['backups'].iterdir()) / 'cutover.log').is_file())

    def test_and_when_the_old_crew_will_not_start_either_the_exit_is_5_with_the_tree_restored(self):
        world, before = World(), self.state()
        world.start_errors = [self.mod.ControlError('no crew window appeared'), self.mod.ControlError('exit code 1')]
        code = self.safe(lambda: self.cutover(world).run())
        self.assertEqual(code, 5, world.text)
        self.assertEqual(self.state(), before)
        self.assertEqual(world.text.count('THE CREW DID NOT START'), 2)
        self.assertIn('Rollback verified', world.text)

    def test_and_when_the_rollback_cannot_be_verified_the_exit_is_3_and_nothing_else_is_started(self):
        real_git = self.mod.git
        def flaky(repo, *args, **kw):
            if args and args[0] == 'reset' and args[-1] == self.fx['head']:
                raise RuntimeError('simulated failure while resetting')
            return real_git(repo, *args, **kw)
        world = World()
        world.start_errors = [self.mod.ControlError('no crew window appeared')]
        with mock.patch.object(self.mod, 'git', flaky):
            code = self.safe(lambda: self.cutover(world).run())
        self.assertEqual(code, 3, world.text)
        self.assertEqual(world.calls.count('start'), 1)               # only the new crew was tried; the old one is not started on a half-restored tree
        self.assertIn('ROLLBACK DID NOT FULLY RESTORE', world.text)
        self.assertIn('--rollback', world.text)

    def interrupted(self, world=None, **patches):
        """Run a cutover in which Ctrl+C arrives at one point; returns (code, world, state before)."""
        world, before = world or World(), self.state()
        with contextlib.ExitStack() as stack:
            for name, error in patches.items():
                stack.enter_context(mock.patch.object(self.mod.Cutover, name, side_effect=error))
            code = self.safe(lambda: self.cutover(world).run())
        return code, world, before

    # ---- third review (ruling #966): whatever a failed new-crew start launched is stopped and proven gone BEFORE any rollback ----

    def spy_on_rollback(self, world):
        """Record the moment a rollback starts, and whether any crew window was still up at that instant."""
        real = self.mod.Cutover._roll_back
        def spy(cut, record, code, manual=False):
            world.calls.append('rollback')
            world.running_at_rollback = world.running
            return real(cut, record, code, manual)
        return mock.patch.object(self.mod.Cutover, '_roll_back', spy)

    def test_a_partly_launched_new_crew_is_stopped_and_proven_gone_before_the_rollback_and_the_old_start(self):
        world, before = World(), self.state()
        world.partial_launch = True
        world.start_errors = [self.mod.ControlError('the start script finished but no crew window appeared')]
        with self.spy_on_rollback(world):
            code = self.safe(lambda: self.cutover(world).run())
        self.assertEqual(code, 2, world.text)
        self.assertEqual(self.state(), before)
        self.assertEqual(world.calls, ['confirm', 'stop', 'gone', 'suite', 'start', 'stop', 'gone', 'rollback', 'start'])
        self.assertFalse(world.running_at_rollback, 'no crew window may be up while the tree is rolled back underneath it')
        self.assertTrue(world.running, 'the old crew is running at the end')
        self.assertIn('Stopping whatever the new crew launched before rolling back', world.text)

    def test_ctrl_c_while_a_new_crew_is_starting_stops_it_before_the_rollback(self):
        world, before = World(), self.state()
        world.partial_launch = True
        world.start_errors = [KeyboardInterrupt()]
        with self.spy_on_rollback(world):
            code = self.safe(lambda: self.cutover(world).run())
        self.assertEqual(code, 2, world.text)
        self.assertEqual(self.state(), before)
        self.assertEqual(world.calls[-5:], ['start', 'stop', 'gone', 'rollback', 'start'])
        self.assertFalse(world.running_at_rollback)

    def test_when_the_partly_launched_crew_cannot_be_proven_stopped_the_tree_is_not_touched(self):
        for label, setup in (('windows still up', lambda w: setattr(w, 'gone_result', False)),
                             ('the stop raises', lambda w: setattr(w, 'stop_error', self.mod.ControlError('Stop exited with code 3'))),
                             ('the window check raises', lambda w: setattr(w, 'gone_error', self.mod.ControlError('the window check failed')))):
            with self.subTest(label):
                self.setUp()
                ticks = iter(range(0, 100000, 5))
                world = World()
                world.start_errors = [self.mod.ControlError('no crew window appeared')]
                world.partial_launch = True
                # the FIRST stop (before the switch) must work; only the cleanup after the failed new start misbehaves
                original_stop, original_gone = world.stop, world.gone
                state = {'stops': 0, 'gones': 0}
                def stop(w=world, s=state, o=original_stop, setup=setup):
                    s['stops'] += 1
                    if s['stops'] == 2 and label == 'the stop raises':
                        w.calls.append('stop')
                        raise self.mod.ControlError('Stop exited with code 3')
                    return o()
                def gone(w=world, s=state, o=original_gone):
                    s['gones'] += 1
                    if s['gones'] >= 2 and label == 'windows still up':
                        w.calls.append('gone')
                        return False
                    if s['gones'] >= 2 and label == 'the window check raises':
                        w.calls.append('gone')
                        raise self.mod.ControlError('the window check failed (exit code 1)')
                    return o()
                world.stop, world.gone = stop, gone
                cut = self.cutover(world, stop_wait=10, sleep=lambda s: None, now=lambda: next(ticks))
                with self.spy_on_rollback(world):
                    code = self.safe(cut.run)
                self.assertEqual(code, 3, world.text)
                self.assertNotIn('rollback', world.calls, 'the tree must not be rewritten under a crew that may still be running')
                self.assertEqual(self.state()['head'], self.fx['tip'])             # still at the new tip
                self.assertIn('THE LIVE TREE WAS NOT ROLLED BACK', world.text)
                self.assertIn('Stop Common Room Crew.cmd', world.text)
                self.assertIn('--rollback', world.text)
                self.assertEqual(world.calls.count('start'), 1)                    # the old crew was not started either
                self.assertTrue((max(self.fx['backups'].iterdir()) / 'record.json').is_file())

    def test_the_documented_undo_works_after_that_refusal(self):
        world = World()
        world.start_errors = [self.mod.ControlError('no crew window appeared')]
        world.partial_launch = True
        world.gone_result = True
        # the cleanup after the failed new start never sees the windows close (every 'gone' after the first says False)
        original_gone, counter = world.gone, {'n': 0}
        def gone():
            counter['n'] += 1
            return False if counter['n'] >= 2 else original_gone()
        world.gone = gone
        ticks = iter(range(0, 100000, 5))
        self.assertEqual(self.safe(lambda: self.cutover(world, stop_wait=10, sleep=lambda s: None, now=lambda: next(ticks)).run()), 3)
        folder = max(self.fx['backups'].iterdir())
        undo = World()
        self.assertEqual(self.safe(lambda: self.cutover(undo).rollback_from(folder)), 0, undo.text)
        self.assertEqual(self.state()['head'], self.fx['head'])
        self.assertEqual(undo.calls, ['confirm', 'stop', 'gone', 'start'])

    def test_ctrl_c_in_every_phase_before_the_switch_restarts_the_old_crew_and_changes_nothing(self):
        phases = {'during the stop': dict(), 'during the window poll': dict(), 'during the manifest check': dict(check_manifest=KeyboardInterrupt()),
                  'during the snapshot': dict(snapshot=KeyboardInterrupt()), 'during the backup': dict(make_backup=KeyboardInterrupt()),
                  'while writing the record': dict(record=KeyboardInterrupt())}
        for label, patches in phases.items():
            with self.subTest(label):
                self.setUp()
                world = World()
                if label == 'during the stop':
                    world.stop_error = KeyboardInterrupt()
                if label == 'during the window poll':
                    world.gone_error = KeyboardInterrupt()
                code, world, before = self.interrupted(world, **patches)
                self.assertEqual(code, 1, world.text)
                self.assertIn('Interrupted with Ctrl+C before the switch', world.text)
                self.assertIn('NOTHING WAS CHANGED', world.text)
                self.assertEqual(self.state(), before)
                self.assertEqual(world.calls[-1], 'start')
                self.assertNotIn('suite', world.calls)

    def test_ctrl_c_during_the_switch_rolls_back_even_when_it_is_half_done(self):
        real_git = self.mod.git
        def interrupt_after_the_reset(repo, *args, **kw):
            if args and args[0] == 'checkout-index':
                raise KeyboardInterrupt()                     # `reset --mixed <tip>` already moved the branch and the index
            return real_git(repo, *args, **kw)
        world, before = World(), self.state()
        with mock.patch.object(self.mod, 'git', interrupt_after_the_reset):
            code = self.safe(lambda: self.cutover(world).run())
        self.assertEqual(code, 2, world.text)
        self.assertIn('Interrupted with Ctrl+C after the switch began', world.text)
        self.assertEqual(self.state(), before)
        self.assertEqual(world.calls[-1], 'start')
        self.assertNotIn('suite', world.calls)

    def test_ctrl_c_at_the_start_of_the_switch_also_rolls_back(self):
        code, world, before = self.interrupted(switch=KeyboardInterrupt())
        self.assertEqual(code, 2, world.text)
        self.assertEqual(self.state(), before)
        self.assertEqual(world.calls[-1], 'start')

    def test_ctrl_c_while_the_new_crew_starts_is_a_failed_start_and_rolls_back(self):
        world, before = World(), self.state()
        world.start_errors = [KeyboardInterrupt()]
        code = self.safe(lambda: self.cutover(world).run())
        self.assertEqual(code, 2, world.text)
        self.assertEqual(self.state(), before)
        self.assertIn('interrupted with Ctrl+C', world.text)
        self.assertEqual(world.calls[-4:], ['start', 'stop', 'gone', 'start'])

    def test_ctrl_c_during_a_rollback_is_reported_and_nothing_is_started(self):
        real_git = self.mod.git
        def interrupt_the_reset(repo, *args, **kw):
            if args and args[0] == 'reset' and args[-1] == self.fx['head']:
                raise KeyboardInterrupt()
            return real_git(repo, *args, **kw)
        world = World(suite=(1, FAIL_ONE))
        with mock.patch.object(self.mod, 'git', interrupt_the_reset):
            code = self.safe(lambda: self.cutover(world).run())
        self.assertEqual(code, 3, world.text)
        self.assertIn('ROLLBACK DID NOT FULLY RESTORE', world.text)
        self.assertIn('--rollback', world.text)
        self.assertNotIn('start', world.calls)

    def test_ctrl_c_at_the_yes_prompt_changes_nothing_and_stops_nothing(self):
        world, before = World(), self.state()
        world.confirm_error = KeyboardInterrupt()
        code = self.safe(lambda: self.cutover(world).run())
        self.assertEqual(code, 1)
        self.assertEqual(world.calls, ['confirm'])
        self.assertIn('Cancelled with Ctrl+C', world.text)
        self.assertEqual(self.state(), before)

    def test_ctrl_c_during_a_manual_rollback(self):
        self.assertEqual(self.cutover(World()).run(), 0)
        folder = max(self.fx['backups'].iterdir())
        switched = self.state()
        stop_interrupted = World()
        stop_interrupted.stop_error = KeyboardInterrupt()
        self.assertEqual(self.safe(lambda: self.cutover(stop_interrupted).rollback_from(folder)), 1)
        self.assertEqual(self.state(), switched)
        self.assertEqual(stop_interrupted.calls[-1], 'start')
        real_git = self.mod.git
        def interrupt_the_reset(repo, *args, **kw):
            if args and args[0] == 'reset' and args[-1] == self.fx['head']:
                raise KeyboardInterrupt()
            return real_git(repo, *args, **kw)
        restore_interrupted = World()
        with mock.patch.object(self.mod, 'git', interrupt_the_reset):
            self.assertEqual(self.safe(lambda: self.cutover(restore_interrupted).rollback_from(folder)), 3)
        self.assertNotIn('start', restore_interrupted.calls)

    def test_the_top_level_guard_ends_every_escape_with_a_plain_message(self):
        printed = io.StringIO()
        cut = self.cutover(World())
        def boom():
            raise ValueError('something nobody planned for')
        def stop():
            raise KeyboardInterrupt()
        for action, word in ((boom, 'UNEXPECTED ERROR'), (stop, 'INTERRUPTED')):
            printed = io.StringIO()
            with redirect_stdout(printed):
                self.assertEqual(self.mod.guarded(action, cut, self.fx['backups']), 3)
            self.assertIn(word, printed.getvalue())
            self.assertIn('--rollback', printed.getvalue())
            self.assertIn(str(self.fx['backups']), printed.getvalue())
        self.assertEqual(self.mod.guarded(lambda: 0, cut, self.fx['backups']), 0)
        self.assertEqual(self.mod.guarded(lambda: 2, cut, self.fx['backups']), 2)

    def test_a_restart_that_fails_after_a_rollback_exits_5_with_the_tree_restored(self):
        world, before = World(suite=(1, FAIL_ONE)), self.state()
        world.start_errors = [self.mod.ControlError('no crew window appeared')]
        code = self.cutover(world).run()
        self.assertEqual(code, 5)
        self.assertEqual(self.state(), before)
        self.assertIn('Rollback verified', world.text)
        self.assertIn('THE CREW DID NOT START', world.text)

    def test_a_suite_that_cannot_run_or_is_interrupted_is_a_failed_suite(self):
        for label, error in (('OSError', OSError('the runner is gone')), ('Ctrl+C', KeyboardInterrupt())):
            with self.subTest(label):
                self.setUp()
                world, before = World(), self.state()
                world.suite_error = error
                try:
                    code = self.cutover(world).run()
                except KeyboardInterrupt:
                    self.fail('Ctrl+C escaped the run')
                self.assertEqual(code, 2, world.text)
                self.assertEqual(self.state(), before)
                self.assertEqual(world.calls[-1], 'start')
                self.assertIn('Rolling back', world.text)

    def test_an_unexpected_error_inside_the_rollback_is_reported_and_the_crew_stays_stopped(self):
        real_git = self.mod.git
        def flaky(repo, *args, **kw):
            if args and args[0] == 'reset' and args[-1] == self.fx['head']:
                raise RuntimeError('simulated failure while resetting')
            return real_git(repo, *args, **kw)
        world = World(suite=(1, FAIL_ONE))
        with mock.patch.object(self.mod, 'git', flaky):
            code = self.cutover(world).run()
        self.assertEqual(code, 3)
        self.assertIn('ROLLBACK DID NOT FULLY RESTORE', world.text)
        self.assertIn('--rollback', world.text)
        self.assertNotIn('start', world.calls)

    def test_an_unexpected_exception_inside_the_rollback_is_reported_not_raised(self):
        calls = {'n': 0}
        real_inventory = self.mod.inventory
        def failing_after_the_snapshot(root):
            calls['n'] += 1
            if calls['n'] >= 2:                      # the first call is the pre-switch snapshot; the second is the rollback check
                raise ValueError('simulated unexpected failure')
            return real_inventory(root)
        world = World(suite=(1, FAIL_ONE))
        with mock.patch.object(self.mod, 'inventory', failing_after_the_snapshot):
            code = self.cutover(world).run()
        self.assertEqual(code, 3)
        self.assertIn('an unexpected error stopped it', world.text)
        self.assertIn('ValueError', world.text)
        self.assertIn('--rollback', world.text)
        self.assertNotIn('start', world.calls)

    def test_a_manual_rollback_whose_stop_fails_restarts_and_changes_nothing(self):
        world = World()
        self.assertEqual(self.cutover(world).run(), 0)
        folder = max(self.fx['backups'].iterdir())
        switched = self.state()
        undo = World()
        undo.stop_error = self.mod.ControlError('Stop Common Room Crew.cmd exited with code 3')
        self.assertEqual(self.cutover(undo).rollback_from(folder), 1)
        self.assertEqual(self.state(), switched)
        self.assertEqual(undo.calls, ['confirm', 'stop', 'start'])

    def test_a_manual_rollback_whose_restart_fails_exits_5(self):
        self.assertEqual(self.cutover(World()).run(), 0)
        folder = max(self.fx['backups'].iterdir())
        undo = World()
        undo.start_errors = [self.mod.ControlError('no crew window appeared')]
        self.assertEqual(self.cutover(undo).rollback_from(folder), 5)
        self.assertEqual(self.state()['head'], self.fx['head'])
        self.assertIn('THE CREW DID NOT START', undo.text)

    def test_describe_names_the_error_even_with_no_message(self):
        self.assertEqual(self.mod.describe(KeyboardInterrupt()), 'KeyboardInterrupt')
        self.assertEqual(self.mod.describe(OSError('x')), 'OSError: x')


@unittest.skipUnless(os.name == 'nt', 'the real suite wrapper drives cmd.exe')
class CutoverSpacedDryRunTests(CutoverBase):
    def test_a_dry_run_at_a_spaced_scratch_path_runs_the_real_suite_wrapper(self):
        fx = self.fx
        self.fx = self.build_live(tip_files={'crew/windows-tests.cmd': crlf(
            '@echo off', '> "%~1" echo Ran 1 test in 0.1s', '>> "%~1" echo.', '>> "%~1" echo OK', 'exit /b 0')})
        fx = self.fx
        spaced = case_dir('cutover-dryrun spaced path') / 'scratch with spaces'
        before = self.state()
        out = io.StringIO()
        with redirect_stdout(out):
            code = self.mod.main(['--dry-run', '--live', str(fx['live']), '--manifest', str(fx['manifest']), '--target', 'upgrade',
                                  '--live-branch', 'live-branch', '--scratch-root', str(spaced)])
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        self.assertIn('The suite passed.', text)
        self.assertIn('scratch with spaces', text)
        self.assertIn('manual rollback verified', text)
        self.assertEqual(self.state(), before)
        shutil.rmtree(spaced, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
