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

class CutoverSwitchTests(CutoverBase):
    def test_a_clean_switch_moves_everything_to_the_tip_and_starts_the_new_crew(self):
        world, live, before = World(), self.fx['live'], self.state()
        code = self.cutover(world).run()
        self.assertEqual(code, 0, world.text)
        self.assertEqual(world.calls, ['confirm', 'stop', 'gone', 'suite', 'start'])
        after = self.state()
        self.assertEqual(after['head'], self.fx['tip'])
        self.assertEqual(after['branch'], 'live-branch')
        self.assertEqual(self.tracked_text('m1.txt'), 't-m1\n')
        self.assertEqual(self.tracked_text('t1.txt'), 'new-t1\n')
        self.assertEqual(self.tracked_text('a1.txt'), 'tip-added\n')
        self.assertEqual(self.tracked_text('u1.txt'), 'b-u1\n')
        self.assertFalse((live / 'd1.txt').exists())
        self.assertEqual(self.tracked_text('s1.txt'), 'same\n')
        self.assertIn('WINDOWS_*.txt', self.tracked_text('.gitignore'))
        # only untracked leftovers remain in git status; nothing tracked differs from the tip
        lines = sorted(l for l in after['status'].split(b'\0') if l)
        self.assertEqual(lines, [b'?? evidence/'])
        self.assertEqual(self.tracked_text('evidence/e1.txt'), 'a screenshot, not in the manifest\n')
        self.assertEqual(self.tracked_text('WINDOWS_AGENT.txt'), 'old test output, untracked, not in the manifest\n')
        self.assert_runtime_untouched(before)
        self.assertEqual(after['checks'], before['checks'])
        folder = Path(next(l for l in world.lines if l.startswith('Switched. Rollback folder: ')).split(': ', 1)[1])
        self.assertEqual((folder / 'files' / 'm1.txt').read_text(encoding='utf-8'), 'b-m1\n')
        self.assertEqual((folder / 'files' / 't1.txt').read_text(encoding='utf-8'), 'h-t1\n')
        self.assertEqual((folder / 'files' / 'd1.txt').read_text(encoding='utf-8'), 'h-d1\n')
        self.assertTrue((folder / 'record.json').is_file() and (folder / 'cutover.log').is_file())
        self.assertFalse(str(folder).lower().startswith(str(live).lower()), 'the backup must be outside the tree')

    def test_it_says_how_long_the_new_crew_may_take_before_starting_it(self):
        world = World()
        self.assertEqual(self.cutover(world).run(), 0, world.text)
        line = 'Starting the new crew. This can take up to about 90 seconds.'
        self.assertIn(line, world.lines)
        self.assertLess(world.lines.index('The suite passed.'), world.lines.index(line))

    def test_it_needs_yes_and_changes_nothing_without_it(self):
        world, before = World(confirm=False), self.state()
        code = self.cutover(world).run()
        self.assertEqual(code, 1)
        self.assertEqual(world.calls, ['confirm'])               # the crew was never stopped
        self.assertEqual(self.state(), before)
        self.assertFalse(self.fx['backups'].exists())

    def test_the_plan_is_shown_before_anything_happens(self):
        world = World(confirm=False)
        self.cutover(world).run()
        text = world.text
        for phrase in ('live tree', self.fx['head'][:7], self.fx['tip'][:7], 'stop the crew', 'back up', 'full test suite',
                       'write to .runtime'):
            self.assertIn(phrase, text)

    def test_a_manifest_mismatch_aborts_with_nothing_changed_and_restarts_the_old_crew(self):
        write(self.fx['live'], 'm1.txt', 'someone edited this\n')
        world, before = World(), self.state()
        code = self.cutover(world).run()
        self.assertEqual(code, 1)
        self.assertEqual(world.calls, ['confirm', 'stop', 'gone', 'start'])      # no suite, old crew back
        self.assertIn('m1.txt: content differs', world.text)
        self.assertIn('NOTHING WAS CHANGED', world.text)
        self.assertEqual(self.state(), before)

    def test_a_missing_manifest_file_aborts_the_same_way(self):
        (self.fx['live'] / 'u1.txt').unlink()
        world, before = World(), self.state()
        self.assertEqual(self.cutover(world).run(), 1)
        self.assertIn('u1.txt: missing from the live tree', world.text)
        self.assertEqual(self.state(), before)
        self.assertEqual(world.calls[-1], 'start')

    def test_the_crew_windows_not_closing_aborts_with_nothing_changed(self):
        ticks = iter(range(0, 1000, 5))
        world, before = World(gone=False), self.state()
        code = self.cutover(world, stop_wait=20, sleep=lambda s: None, now=lambda: next(ticks)).run()
        self.assertEqual(code, 1)
        self.assertEqual(world.calls[:2], ['confirm', 'stop'])
        self.assertIn('not proven closed', world.text)
        self.assertEqual(world.calls[-1], 'start')
        self.assertNotIn('suite', world.calls)
        self.assertEqual(self.state(), before)

    def test_a_backup_copy_that_does_not_verify_aborts_before_the_switch(self):
        real_copy = shutil.copy2
        def corrupt(src, dst, *a, **k):
            result = real_copy(src, dst, *a, **k)
            if str(src).endswith('m1.txt'):
                Path(dst).write_bytes(b'corrupted copy')
            return result
        world, before = World(), self.state()
        with mock.patch.object(self.mod.shutil, 'copy2', corrupt):
            code = self.cutover(world).run()
        self.assertEqual(code, 1)
        self.assertIn('NOTHING WAS CHANGED', world.text)
        self.assertNotIn('suite', world.calls)
        self.assertEqual(world.calls[-1], 'start')
        self.assertEqual(self.state(), before)


class CutoverRollbackTests(CutoverBase):
    def assert_rolled_back_identically(self, world, before, code, expected=2):
        self.assertEqual(code, expected, world.text)
        after = self.state()
        self.assertEqual(after, before, 'HEAD, git status, every file and .runtime must be exactly as before')
        self.assertEqual(world.calls[-1], 'start')                       # the old crew is started again
        self.assertIn('Rollback verified', world.text)

    def test_a_failing_suite_rolls_back_to_identical_files_and_head(self):
        world, before = World(suite=(1, FAIL_ONE)), self.state()
        code = self.cutover(world).run()
        self.assert_rolled_back_identically(world, before, code)
        self.assertEqual(world.calls, ['confirm', 'stop', 'gone', 'suite', 'start'])
        self.assertIn('failing test not allowed by CUTOVER_ALLOWED.txt', world.text)
        self.assertEqual(self.tracked_text('m1.txt'), 'b-m1\n')
        self.assertEqual(self.tracked_text('t1.txt'), 'h-t1\n')
        self.assertTrue((self.fx['live'] / 'd1.txt').is_file())
        self.assertFalse((self.fx['live'] / 'a1.txt').exists())
        self.assertEqual(self.tracked_text('.gitignore'), '.runtime/\n.checks/\n__pycache__/\n')

    def test_a_crashed_empty_or_timed_out_suite_also_rolls_back(self):
        for label, result in (('exit 1 and no named failure', (1, 'Traceback: the runner crashed\n')),
                              ('no tests ran', (0, 'Ran 0 tests in 0.0s\n\nOK\n')),
                              ('timeout', (124, 'the suite did not finish within 10800 seconds')),
                              ('exit 0 but failures printed', (0, FAIL_ONE))):
            with self.subTest(label):
                self.setUp()
                world, before = World(suite=result), self.state()
                code = self.cutover(world).run()
                self.assert_rolled_back_identically(world, before, code)

    def test_a_failure_named_in_the_allowed_file_does_not_roll_back(self):
        allowed = self.fx['live'] / 'crew' / 'CUTOVER_ALLOWED.txt'
        world = World(suite=(1, FAIL_ONE))
        cut = self.cutover(world)
        # the tip's copy of the file is what a real run reads, so give the tip's copy the line
        git(self.fx['origin'], 'checkout', '-q', 'upgrade')
        write(self.fx['origin'], 'crew/CUTOVER_ALLOWED.txt', '# known baseline failure\ntests.test_agent_worker.RunnerTests.test_refusal\n')
        git(self.fx['origin'], 'add', '-A')
        git(self.fx['origin'], 'commit', '-q', '-m', 'allow the known failure')
        git(self.fx['origin'], 'checkout', '-q', 'live-branch')
        git(self.fx['live'], 'fetch', '-q', 'origin')
        git(self.fx['live'], 'branch', '-q', '-f', 'upgrade', 'origin/upgrade')
        code = cut.run()
        self.assertEqual(code, 0, world.text)
        self.assertEqual(world.calls[-1], 'start')
        self.assertIn('The suite passed.', world.text)
        self.assertEqual(self.state()['head'], git(self.fx['live'], 'rev-parse', 'upgrade'))

    def test_a_second_failure_not_in_the_allowed_list_still_rolls_back(self):
        world, before = World(suite=(1, FAIL_TWO)), self.state()
        cut = self.cutover(world)
        cut.extra_allowed = ['tests.test_agent_worker.RunnerTests.test_refusal']       # one of the two is tolerated
        code = cut.run()
        self.assert_rolled_back_identically(world, before, code)
        self.assertIn('tests.test_other.Other.test_other', world.text)

    def test_a_switch_that_fails_midway_is_rolled_back_too(self):
        real_git = self.mod.git
        def flaky(repo, *args, **kw):
            if args and args[0] == 'checkout-index':
                raise RuntimeError('simulated failure while writing files')
            return real_git(repo, *args, **kw)
        world, before = World(), self.state()
        with mock.patch.object(self.mod, 'git', flaky):
            code = self.cutover(world).run()
        self.assert_rolled_back_identically(world, before, code)
        self.assertNotIn('suite', world.calls)

    def test_a_stray_file_the_run_leaves_behind_is_reported_never_deleted(self):
        world = World(suite=(1, FAIL_ONE))
        world.suite_hook = lambda live: write(live, 'stray.txt', 'created while the suite ran')
        code = self.cutover(world).run()
        self.assertEqual(code, 3)
        self.assertTrue((self.fx['live'] / 'stray.txt').is_file())        # untracked and not in the manifest: untouched
        self.assertIn('ROLLBACK DID NOT FULLY RESTORE', world.text)
        self.assertIn('stray.txt', world.text)
        self.assertNotIn('start', world.calls)                             # the old crew is NOT started on a half-restored tree

    def test_the_suite_scratch_folder_does_not_count_as_a_change(self):
        world, before = World(suite=(1, FAIL_ONE)), self.state()
        world.suite_hook = lambda live: write(live, '.checks/new-test-scratch.txt', 'made by the suite')
        code = self.cutover(world).run()
        self.assertEqual(code, 2)
        self.assertEqual(self.state()['files'], before['files'])


class CutoverManualRollbackTests(CutoverBase):
    def switched(self):
        world = World()
        cut = self.cutover(world)
        self.assertEqual(cut.run(), 0, world.text)
        return Path(next(l for l in world.lines if l.startswith('Switched. Rollback folder: ')).split(': ', 1)[1])

    def test_rollback_restores_the_state_before_the_cutover(self):
        before = self.state()
        folder = self.switched()
        self.assertNotEqual(self.state()['head'], before['head'])
        world = World()
        code = self.cutover(world).rollback_from(folder)
        self.assertEqual(code, 0, world.text)
        self.assertEqual(self.state(), before)
        self.assertEqual(world.calls, ['confirm', 'stop', 'gone', 'start'])

    def test_rollback_needs_yes(self):
        folder = self.switched()
        after_switch = self.state()
        world = World(confirm=False)
        self.assertEqual(self.cutover(world).rollback_from(folder), 1)
        self.assertEqual(self.state(), after_switch)
        self.assertEqual(world.calls, ['confirm'])

    def test_rollback_refuses_a_folder_that_is_not_a_backup(self):
        world = World()
        self.assertEqual(self.cutover(world).rollback_from(self.fx['root']), 4)
        self.assertIn('not a cutover backup folder', world.text)

    def test_rollback_refuses_when_the_live_tree_is_not_at_the_switched_tip(self):
        folder = self.switched()
        git(self.fx['live'], 'reset', '--mixed', '-q', self.fx['head'])        # someone moved it: now not at the tip
        world = World()
        self.assertEqual(self.cutover(world).rollback_from(folder), 4)
        self.assertIn('REFUSED', world.text)
        self.assertEqual(world.calls, [])

    def test_rollback_refuses_a_backup_made_for_another_tree(self):
        folder = self.switched()
        record = json.loads((folder / 'record.json').read_text(encoding='utf-8'))
        record['live'] = str(self.fx['root'] / 'some-other-tree')
        (folder / 'record.json').write_text(json.dumps(record), encoding='utf-8')
        world = World()
        self.assertEqual(self.cutover(world).rollback_from(folder), 4)
        self.assertIn('made for', world.text)

    def test_files_made_after_the_switch_are_left_alone_by_a_manual_rollback(self):
        folder = self.switched()
        write(self.fx['live'], 'created-later.txt', 'made by the new crew')
        world = World()
        self.assertEqual(self.cutover(world).rollback_from(folder), 0, world.text)
        self.assertTrue((self.fx['live'] / 'created-later.txt').is_file())


class CutoverRefusalTests(CutoverBase):
    def refused(self, world, **kw):
        before = self.state()
        code = self.cutover(world, **kw).run()
        self.assertEqual(code, 4, world.text)
        self.assertEqual(world.calls, [])                  # not confirmed, not stopped: the crew keeps running
        self.assertEqual(self.state(), before)
        self.assertIn('REFUSED', world.text)
        return world.text

    def test_a_tip_that_tracks_runtime_files_is_refused(self):
        self.fx = self.build_live(tip_adds_runtime=True)
        self.assertIn('.runtime', self.refused(World()))

    def test_a_tip_that_would_overwrite_an_untracked_non_manifest_file_is_refused(self):
        self.fx = self.build_live(tip_files={'WINDOWS_AGENT.txt': 'the tip tracks a file that exists live, untracked\n'})
        self.assertIn('WINDOWS_AGENT.txt', self.refused(World()))

    def test_a_live_tree_on_the_wrong_branch_is_refused(self):
        git(self.fx['live'], 'checkout', '-q', '-b', 'some-other-branch')
        self.assertIn('some-other-branch', self.refused(World()))

    def test_a_live_tree_already_at_the_tip_is_refused(self):
        self.assertEqual(self.cutover(World()).run(), 0)
        self.assertIn('already at', self.refused(World()))

    def test_a_missing_upgrade_branch_is_refused(self):
        self.assertIn('does not exist', self.refused(World(), target='no-such-branch'))

    def test_a_tip_that_does_not_contain_the_baseline_is_refused(self):
        empty_tree = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'
        unrelated = git(self.fx['live'], 'commit-tree', empty_tree, '-m', 'unrelated history')
        git(self.fx['live'], 'branch', '-q', '-f', 'upgrade', unrelated)
        self.assertIn('does not contain the baseline', self.refused(World()))

    def test_a_backup_folder_inside_the_live_tree_is_refused(self):
        fx = self.fx
        world = World()
        cut = self.mod.Cutover(fx['live'], fx['manifest'], fx['live'] / 'inside-backups', fx['live'] / 'crew' / 'x.txt',
                               target='upgrade', live_branch='live-branch', stop_crew=world.stop, windows_gone=world.gone,
                               start_crew=world.start, run_suite=world.suite, confirm=world.confirm, say=world.say)
        self.assertEqual(cut.run(), 4)
        self.assertIn('outside the live tree', world.text)

    def test_a_backup_root_that_would_make_a_path_too_long_is_refused_before_the_crew_is_stopped(self):
        fx = self.fx
        world = World()
        long_root = Path('C:/') / ('a-very-long-backup-folder-name-' * 8)         # never created: the check is on the length
        cut = self.mod.Cutover(fx['live'], fx['manifest'], long_root, fx['live'] / 'crew' / 'x.txt', target='upgrade',
                               live_branch='live-branch', stop_crew=world.stop, windows_gone=world.gone,
                               start_crew=world.start, run_suite=world.suite, confirm=world.confirm, say=world.say)
        before = self.state()
        self.assertEqual(cut.run(), 4)
        self.assertIn('Windows allows', world.text)
        self.assertIn('--backup-root', world.text)
        self.assertEqual(world.calls, [])
        self.assertEqual(self.state(), before)
        self.assertFalse(long_root.exists())

    def test_a_missing_manifest_is_refused(self):
        self.fx['manifest'].unlink()
        self.assertIn('manifest', self.refused(World()))

    def test_a_folder_that_is_not_a_repo_is_refused(self):
        plain = case_dir('cutover-plain')
        world = World()
        cut = self.mod.Cutover(plain, self.fx['manifest'], self.fx['backups'], plain / 'x.txt', target='upgrade',
                               live_branch='live-branch', stop_crew=world.stop, windows_gone=world.gone,
                               start_crew=world.start, run_suite=world.suite, confirm=world.confirm, say=world.say)
        self.assertEqual(cut.run(), 4)
        self.assertEqual(world.calls, [])


if __name__ == '__main__':
    unittest.main()
