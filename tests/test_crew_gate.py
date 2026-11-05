"""Behavioral tests for the `crew submit` gate (common_room/crew.py, 2026-10-01, crew-gate).

Before any report reaches a reviewer, `crew submit` checks the paperwork itself: citations, the
task's FILES: lines against a real git diff, and the RED/GREEN evidence file. A failed gate sends
nothing and records no strike. Written before the code: on the base commit `submit` has no
--repo/--base/--commit/--evidence/--cite-root/--skip-gate flags, so every gate case fails there.
Real rooms in-process on loopback, real temp git repos, no network, no model.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # lets `python tests/test_crew_gate.py` run from a tree root

import argparse, hashlib, json, shutil, subprocess, tempfile, unittest
from unittest import mock

import common_room.crew as crew
import common_room.plans as plans
from tests.helpers import case_dir
from tests.test_crew_cli import CrewCLIBase

GIT = shutil.which('git')
GIT_ARGS = ['-c', 'user.name=Gate Test', '-c', 'user.email=gate@example.invalid', '-c', 'commit.gpgsign=false',
            '-c', 'core.autocrlf=false']


def git(repo, *args):
    done = subprocess.run([GIT, *GIT_ARGS, '-C', str(repo), *args], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def make_repo(base_files, commit_files, allow_empty=False):
    """A throwaway repo: a base commit with `base_files`, then a commit that writes `commit_files`.
    Returns (repo, base, commit)."""
    repo = case_dir('gate-repo')
    git(repo, 'init', '-q')
    for name, text in base_files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding='utf-8')
    git(repo, 'add', '-A')
    git(repo, 'commit', '-q', '--allow-empty', '-m', 'base')
    base = git(repo, 'rev-parse', 'HEAD')
    for name, text in commit_files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding='utf-8')
    git(repo, 'add', '-A')
    git(repo, 'commit', '-q', '--allow-empty', '-m', 'work')
    return repo, base, git(repo, 'rev-parse', 'HEAD')


def make_estate():
    """A tiny estate root: Artifacts/notes/source.md with three numbered lines."""
    root = case_dir('gate-estate')
    folder = root / 'Artifacts' / 'notes'
    folder.mkdir(parents=True)
    (folder / 'source.md').write_text('first line of the source\nsecond line of the source\nthird line\n', encoding='utf-8')
    return root


@unittest.skipIf(GIT is None, 'git is not available')
class GateBase(CrewCLIBase):
    def setUp(self):
        self.room = self.start_room()
        self.estate = make_estate()

    def claimed(self, body='Do the fixture thing.', title='Gated task'):
        task_id = self.intake(self.room, title=title, body=body)
        self.cli(task_id=None, args=('ack', task_id))
        self.cli(task_id=None, args=('claim', task_id, '--seconds', '600'))
        return task_id

    def cli(self, task_id, args, json_mode=True):
        return self.run_cli(self.room, 'crew-worker-a', *args, json_mode=json_mode)

    def report(self, text, name='REPORT.md'):
        folder = case_dir('gate-report')
        path = folder / name
        path.write_text(text, encoding='utf-8')
        return path

    def clean_report(self):
        return '# Report\nMEASURED the fixture, nothing cited.\n'

    def submit(self, task_id, report, *extra, json_mode=True):
        return self.cli(None, ('submit', task_id, '--file', str(report), '--summary', 'Done.',
                               '--cite-root', str(self.estate), *extra), json_mode=json_mode)

    def task(self, task_id):
        code, out, err = self.run_cli(self.room, 'crew-worker-a', 'show', task_id)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def assert_nothing_sent(self, task_id, before):
        after = self.task(task_id)
        self.assertEqual(after['task']['status'], 'claimed')
        self.assertEqual(after['task'].get('result_refs'), [])
        self.assertEqual(len(after['history']), len(before['history']))
        self.assertEqual(after['task']['failed_attempts'], 0)

    def blocked(self, task_id, report, *extra):
        before = self.task(task_id)
        code, out, err = self.submit(task_id, report, *extra)
        self.assertEqual(code, 1, (out, err))
        self.assertEqual(out.strip(), '')
        self.assertIn('gate', err.lower())
        self.assertIn('nothing was sent', err.lower())
        self.assertIn('no strike', err.lower())
        self.assert_nothing_sent(task_id, before)
        return err


class GateCitationTests(GateBase):
    def test_a_citation_to_a_missing_file_blocks_the_submit(self):
        task_id = self.claimed()
        report = self.report('MEASURED. See `Artifacts/notes/nowhere.md:1-2` for the proof.\n')
        err = self.blocked(task_id, report)
        self.assertIn('Artifacts/notes/nowhere.md', err)
        self.assertIn('MISSING FILE', err)

    def test_a_line_range_past_the_end_blocks_the_submit(self):
        task_id = self.claimed()
        err = self.blocked(task_id, self.report('MEASURED. `Artifacts/notes/source.md:2-90` says so.\n'))
        self.assertIn('OUT OF RANGE', err)

    def test_a_quote_that_is_not_there_blocks_the_submit(self):
        task_id = self.claimed()
        err = self.blocked(task_id, self.report(
            'VERIFIED: `Artifacts/notes/source.md:1-2` says "this sentence is not in the file at all".\n'))
        self.assertIn('QUOTE NOT FOUND', err)

    def test_good_citations_pass(self):
        task_id = self.claimed()
        code, out, err = self.submit(task_id, self.report(
            'VERIFIED: `Artifacts/notes/source.md:1-2` says "second line of the source".\n'))
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data['status'], 'ready_for_review')
        self.assertEqual(data['gate']['citations']['OK'], 1)

    def test_not_checked_lines_are_listed_but_allowed(self):
        task_id = self.claimed()
        code, out, err = self.submit(task_id, self.report('MEASURED at `common_room/store.py:12` in the tree.\n'),
                                     json_mode=False)
        self.assertEqual(code, 0, err)
        self.assertIn('NOT CHECKED', out)
        self.assertIn('common_room/store.py:12', out)
        self.assertIn('gate: citations: 1 found, 0 checked, 0 problems', out)

    def test_a_report_with_no_evidence_labels_is_warned_not_blocked(self):
        task_id = self.claimed()
        code, out, err = self.submit(task_id, self.report('Plain words, no labels at all.\n'), json_mode=False)
        self.assertEqual(code, 0, err)
        self.assertIn('no evidence labels', out.lower())


class GateFilesTests(GateBase):
    def repo_args(self, repo, base, commit):
        return ('--repo', str(repo), '--base', base, '--commit', commit)

    def test_a_file_outside_the_files_list_blocks_the_submit(self):
        repo, base, commit = make_repo({'a.py': '1\n'}, {'a.py': '2\n', 'b.py': 'oops\n'})
        task_id = self.claimed(body='Do it.\nFILES: a.py')
        err = self.blocked(task_id, self.report(self.clean_report()), *self.repo_args(repo, base, commit))
        self.assertIn('b.py', err)
        self.assertIn('FILES', err)

    def test_files_that_match_pass_and_the_list_may_have_notes_and_globs(self):
        repo, base, commit = make_repo({}, {'common_room/x.py': '1\n', 'tests/test_x_one.py': '1\n', 'tests/test_x_two.py': '1\n'})
        body = 'Do it.\nFILES: common_room/x.py (only if needed), tests/test_x_*.py (new).\nReport: elsewhere.'
        task_id = self.claimed(body=body)
        code, out, err = self.submit(task_id, self.report(self.clean_report()), *self.repo_args(repo, base, commit))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['gate']['files'], 'OK')

    def test_several_files_lines_are_added_together(self):
        repo, base, commit = make_repo({}, {'a.py': '1\n', 'b.py': '1\n'})
        task_id = self.claimed(body='FILES: a.py\nmore words\nFILES: b.py')
        code, out, err = self.submit(task_id, self.report(self.clean_report()), *self.repo_args(repo, base, commit))
        self.assertEqual(code, 0, err)

    def test_files_none_with_a_diff_blocks_the_submit(self):
        repo, base, commit = make_repo({'a.py': '1\n'}, {'a.py': '2\n'})
        task_id = self.claimed(body='Review only.\nFILES: none')
        err = self.blocked(task_id, self.report(self.clean_report()), *self.repo_args(repo, base, commit))
        self.assertIn('a.py', err)
        self.assertIn('FILES: none', err)

    def test_files_none_with_no_diff_passes(self):
        repo, base, commit = make_repo({'a.py': '1\n'}, {}, allow_empty=True)
        task_id = self.claimed(body='Review only.\nFILES: none')
        code, out, err = self.submit(task_id, self.report(self.clean_report()), *self.repo_args(repo, base, commit))
        self.assertEqual(code, 0, err)

    def test_files_lines_without_repo_base_and_commit_block_and_say_which_flag(self):
        task_id = self.claimed(body='Do it.\nFILES: a.py')
        err = self.blocked(task_id, self.report(self.clean_report()))
        for flag in ('--repo', '--base', '--commit'):
            self.assertIn(flag, err)
        repo, base, commit = make_repo({}, {'a.py': '1\n'})
        err = self.blocked(task_id, self.report(self.clean_report()), '--repo', str(repo), '--commit', commit)
        self.assertIn('--base', err)

    def test_without_files_lines_the_check_is_skipped_and_says_so(self):
        task_id = self.claimed(body='Do it, no file list here.')
        code, out, err = self.submit(task_id, self.report(self.clean_report()), json_mode=False)
        self.assertEqual(code, 0, err)
        self.assertIn('FILES check skipped', out)

    def test_an_unknown_commit_blocks_the_submit(self):
        repo, base, commit = make_repo({}, {'a.py': '1\n'})
        task_id = self.claimed(body='FILES: a.py')
        err = self.blocked(task_id, self.report(self.clean_report()), '--repo', str(repo), '--base', base,
                           '--commit', 'deadbeefdeadbeefdeadbeefdeadbeefdeadbeef')
        self.assertIn('deadbeef', err)

    def test_the_repo_is_only_read(self):
        repo, base, commit = make_repo({}, {'a.py': '1\n'})
        before = git(repo, 'status', '--porcelain') + git(repo, 'rev-parse', 'HEAD')
        task_id = self.claimed(body='FILES: a.py')
        code, out, err = self.submit(task_id, self.report(self.clean_report()), *self.repo_args(repo, base, commit))
        self.assertEqual(code, 0, err)
        self.assertEqual(git(repo, 'status', '--porcelain') + git(repo, 'rev-parse', 'HEAD'), before)


class GateEvidenceTests(GateBase):
    BODY = 'Build it.\nTests RED/GREEN.\nFILES: a.py'

    def evidence_json(self, commit, folder):
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / 'EVIDENCE.json'
        path.write_text(json.dumps({'tool': 'evidence.py 1.0', 'commit': {'given': commit[:7], 'sha': commit}}), encoding='utf-8')
        return path

    def setup_work(self):
        repo, base, commit = make_repo({}, {'a.py': '1\n'})
        return repo, base, commit, ('--repo', str(repo), '--base', base, '--commit', commit)

    def test_missing_evidence_blocks_the_submit(self):
        repo, base, commit, flags = self.setup_work()
        task_id = self.claimed(body=self.BODY)
        err = self.blocked(task_id, self.report(self.clean_report()), *flags)
        self.assertIn('EVIDENCE.json', err)
        self.assertIn('evidence.py', err)

    def test_evidence_for_another_commit_blocks_the_submit(self):
        repo, base, commit, flags = self.setup_work()
        task_id = self.claimed(body=self.BODY)
        report = self.report(self.clean_report())
        self.evidence_json(base, report.parent)
        err = self.blocked(task_id, report, *flags)
        self.assertIn(base[:7], err)
        self.assertIn(commit[:7], err)

    def test_matching_evidence_next_to_the_report_passes(self):
        repo, base, commit, flags = self.setup_work()
        task_id = self.claimed(body=self.BODY)
        report = self.report(self.clean_report())
        self.evidence_json(commit, report.parent)
        code, out, err = self.submit(task_id, report, *flags)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['gate']['evidence'], 'OK')

    def test_evidence_in_an_evidence_folder_beside_the_report_passes(self):
        repo, base, commit, flags = self.setup_work()
        task_id = self.claimed(body=self.BODY)
        report = self.report(self.clean_report())
        self.evidence_json(commit, report.parent / 'evidence')
        code, out, err = self.submit(task_id, report, *flags)
        self.assertEqual(code, 0, err)

    def test_an_evidence_path_can_be_named(self):
        repo, base, commit, flags = self.setup_work()
        task_id = self.claimed(body=self.BODY)
        elsewhere = self.evidence_json(commit, case_dir('gate-evidence-elsewhere'))
        code, out, err = self.submit(task_id, self.report(self.clean_report()), *flags, '--evidence', str(elsewhere))
        self.assertEqual(code, 0, err)

    def test_a_full_length_commit_matches_an_abbreviated_one(self):
        repo, base, commit, flags = self.setup_work()
        task_id = self.claimed(body=self.BODY)
        report = self.report(self.clean_report())
        self.evidence_json(commit, report.parent)
        short = ('--repo', str(repo), '--base', base, '--commit', commit[:8])
        code, out, err = self.submit(task_id, report, *short)
        self.assertEqual(code, 0, err)

    def test_a_task_that_does_not_mention_red_green_needs_no_evidence(self):
        repo, base, commit, flags = self.setup_work()
        task_id = self.claimed(body='Build it.\nFILES: a.py')
        code, out, err = self.submit(task_id, self.report(self.clean_report()), *flags)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['gate']['evidence'], 'skipped: the task does not mention RED/GREEN')

    def test_red_green_without_a_named_commit_skips_the_check_and_says_so(self):
        task_id = self.claimed(body='Write the plan.\nRED/GREEN where code changes.')
        code, out, err = self.submit(task_id, self.report(self.clean_report()), json_mode=False)
        self.assertEqual(code, 0, err)
        self.assertIn('evidence check skipped', out.lower())


class GateSkipTests(GateBase):
    BAD = 'MEASURED. `Artifacts/notes/nowhere.md:1` is the proof.\n'

    def test_skip_gate_needs_a_written_reason(self):
        task_id = self.claimed()
        before = self.task(task_id)
        for reason in ('', '   ', 'x'):
            with self.subTest(reason=reason):
                code, out, err = self.submit(task_id, self.report(self.BAD), '--skip-gate', reason)
                self.assertEqual(code, 1)
                self.assertIn('--skip-gate', err)
                self.assertIn('reason', err.lower())
                self.assert_nothing_sent(task_id, before)

    def test_skip_gate_with_a_reason_sends_and_the_reviewer_can_see_the_reason(self):
        task_id = self.claimed()
        reason = 'The cited file is on a share this seat cannot open.'
        code, out, err = self.submit(task_id, self.report(self.BAD), '--skip-gate', reason)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['status'], 'ready_for_review')
        code, out, err = self.run_cli(self.room, 'crew-worker-a', 'read', task_id)
        self.assertEqual(code, 0, err)
        text = json.loads(out)['text']
        self.assertIn('GATE SKIPPED', text)
        self.assertIn(reason, text)
        self.assertTrue(text.startswith('MEASURED'))
        shown = self.task(task_id)['task']
        self.assertIn('GATE SKIPPED', shown['result_body'])
        self.assertIn(reason, shown['result_body'])

    def test_skipping_does_not_run_any_check(self):
        task_id = self.claimed(body='FILES: a.py\nRED/GREEN')
        code, out, err = self.submit(task_id, self.report(self.BAD), '--skip-gate', 'No repo on this machine, ask Shane.')
        self.assertEqual(code, 0, err)

    def test_the_skip_note_cannot_push_the_report_past_the_size_limit(self):
        task_id = self.claimed()
        big = 'MEASURED ' + 'x' * (crew.LIMIT_REPORT_BYTES - 30) + '\n'
        before = self.task(task_id)
        code, out, err = self.submit(task_id, self.report(big), '--skip-gate', 'A reason long enough to matter here.')
        self.assertEqual(code, 1)
        self.assert_nothing_sent(task_id, before)


class GateSafetyTests(GateBase):
    def test_a_clean_submit_is_unchanged_for_a_task_with_no_gate_inputs(self):
        task_id = self.claimed()
        code, out, err = self.submit(task_id, self.report(self.clean_report()), json_mode=False)
        self.assertEqual(code, 0, err)
        self.assertIn('submitted for review', out)
        self.assertIn('gate: passed', out)

    def test_a_blocked_submit_can_be_fixed_and_resent_on_the_same_lease(self):
        task_id = self.claimed()
        self.blocked(task_id, self.report('MEASURED. `Artifacts/notes/nowhere.md:1` proves it.\n'))
        code, out, err = self.submit(task_id, self.report('MEASURED. `Artifacts/notes/source.md:1` proves it.\n'))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['status'], 'ready_for_review')

    def test_a_retry_of_an_already_submitted_task_is_not_gated(self):
        task_id = self.claimed()
        self.submit(task_id, self.report(self.clean_report()))
        code, out, err = self.submit(task_id, self.report('MEASURED. `Artifacts/notes/nowhere.md:1` x.\n'))
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)['already_done'])

    def test_a_cite_root_that_is_not_a_folder_is_refused(self):
        task_id = self.claimed()
        nowhere = case_dir('gate-no-estate')
        code, out, err = self.cli(None, ('submit', task_id, '--file', str(self.report(self.clean_report())),
                                         '--summary', 'Done.', '--cite-root', str(nowhere / 'missing-folder')),
                                  json_mode=False)
        self.assertEqual(code, 1)
        self.assertIn('--cite-root', err)

    def test_with_no_estate_root_to_be_found_the_citation_check_is_skipped_and_says_so(self):
        task_id = self.claimed()
        with tempfile.TemporaryDirectory() as bare, mock.patch.object(crew, 'ROOT', Path(bare)):
            code, out, err = self.cli(None, ('submit', task_id, '--file', str(self.report(self.clean_report())),
                                             '--summary', 'Done.'), json_mode=False)
        self.assertEqual(code, 0, err)
        self.assertIn('citation check skipped', out.lower())

    def test_the_estate_root_is_found_by_walking_up_to_the_folder_that_holds_artifacts(self):
        deep = self.estate / 'Artifacts' / 'astra' / 'wt' / 'some-task'
        deep.mkdir(parents=True)
        self.assertEqual(crew.find_estate_root(deep), self.estate)
        with tempfile.TemporaryDirectory() as bare:
            self.assertIsNone(crew.find_estate_root(Path(bare)))


class PlanSummaryFilesTests(unittest.TestCase):
    def test_the_approval_screen_shows_files_lines_like_input_lines(self):
        plan = {'plan_id': 'p-1', 'title': 'Plan', 'scope': 'orchestra', 'tasks': [
            {'key': 't1', 'to': 'claude-code-b', 'title': 'Task one', 'reserve': False,
             'body': 'Do it.\nINPUT: Artifacts/foo.md#L1-L10\nFILES: common_room/crew.py, tests/test_x.py (new)\n'
                     'Not a files line: FILES: hidden\nFILES: none',
             'acceptance': 'Looks right.'}]}
        text = '\n'.join(plans.plan_summary(plan, 'a' * 64))
        self.assertIn('     INPUT: Artifacts/foo.md#L1-L10', text)
        self.assertIn('     FILES: common_room/crew.py, tests/test_x.py (new)', text)
        self.assertIn('     FILES: none', text)
        self.assertNotIn('hidden', text)



class GateNextOutputTests(unittest.TestCase):
    def setUp(self):
        self.root = make_estate()
        self.folder = self.root / 'Artifacts' / 'results'
        self.folder.mkdir()
        self.report = self.folder / 'REPORT.md'
        self.report.write_text('MEASURED fixture.\n')
        self.task = {'id': 'one', 'body': 'OUTPUT: Artifacts/results/', 'status': 'claimed'}

    def check(self, body=None, others=(), report=None):
        task = dict(self.task, body=body if body is not None else self.task['body'])
        args = argparse.Namespace(file=str(report or self.report), cite_root=str(self.root))
        problems, notes, info = [], [], {}
        crew._gate_output(task, {'tasks': [task, *others]}, args, problems, notes, info)
        return problems, notes, info

    def test_folder_and_exact_file_rules_allow_report(self):
        for body in ('OUTPUT: Artifacts/results/', 'OUTPUT: Artifacts/results/REPORT.md',
                     'OUTPUT: Artifacts/elsewhere/\nOUTPUT: `Artifacts/results/REPORT.md`'):
            self.assertEqual(self.check(body)[0], [])

    def test_outside_allowed_folder_and_estate_escape_block(self):
        for body in ('OUTPUT: Artifacts/other/', 'OUTPUT: ../outside/', 'OUTPUT:'):
            self.assertTrue(self.check(body)[0])

    def test_resolved_casefolded_same_file_open_task_blocks(self):
        other = {'id': 'two', 'status': 'delivered', 'body': 'OUTPUT: Artifacts/results/./report.MD'}
        problems, notes, info = self.check('OUTPUT: Artifacts/results/REPORT.md', [other])
        self.assertTrue(problems)
        self.assertIn('two', ' '.join(problems))

    def assert_partial_neighbor_collision(self, separator, bad_first):
        valid, invalid = 'Artifacts/results/REPORT.md', '../invalid/'
        entries = (invalid, valid) if bad_first else (valid, invalid)
        other = {'id': 'two', 'status': 'claimed', 'body': 'OUTPUT: ' + separator.join(entries)}
        problems, notes, info = self.check(others=[other])
        self.assertEqual(info['output'], 'blocked')
        self.assertIn('same-file overlap', ' '.join(problems))
        self.assertIn('invalid OUTPUT', ' '.join(notes))

    def test_comma_neighbor_keeps_collision_with_invalid_entry_last(self):
        self.assert_partial_neighbor_collision(', ', False)

    def test_comma_neighbor_keeps_collision_with_invalid_entry_first(self):
        self.assert_partial_neighbor_collision(', ', True)

    def test_multiline_neighbor_keeps_collision_with_invalid_entry_last(self):
        self.assert_partial_neighbor_collision('\nOUTPUT: ', False)

    def test_multiline_neighbor_keeps_collision_with_invalid_entry_first(self):
        self.assert_partial_neighbor_collision('\nOUTPUT: ', True)

    def test_partial_neighbor_shared_folder_stays_warning_and_unrelated_path_stays_clear(self):
        for separator in (', ', '\nOUTPUT: '):
            for entries in (('Artifacts/results/', '../invalid/'), ('../invalid/', 'Artifacts/results/')):
                other = {'id': 'two', 'status': 'claimed', 'body': 'OUTPUT: ' + separator.join(entries)}
                problems, notes, info = self.check(others=[other])
                self.assertEqual(problems, [])
                self.assertEqual(info['output'], 'OK')
                self.assertIn('folder overlap', ' '.join(notes))
                self.assertIn('invalid OUTPUT', ' '.join(notes))
            other = {'id': 'two', 'status': 'claimed',
                     'body': 'OUTPUT: ' + separator.join(('Artifacts/unrelated/OTHER.md', '../invalid/'))}
            problems, notes, info = self.check(others=[other])
            self.assertEqual(problems, [])
            self.assertNotIn('overlap', ' '.join(notes))
            self.assertIn('invalid OUTPUT', ' '.join(notes))

    def test_own_partial_declaration_remains_strict(self):
        for separator in (', ', '\nOUTPUT: '):
            problems, notes, info = self.check('OUTPUT: ' + separator.join(('Artifacts/results/', '../invalid/')))
            self.assertTrue(problems)
            self.assertEqual(info['output'], 'blocked')

    def test_claim_keeps_known_collision_warning_with_malformed_neighbor(self):
        task = dict(self.task, body='OUTPUT: Artifacts/results/REPORT.md')
        other = {'id': 'two', 'status': 'claimed', 'body': task['body'] + ', ../invalid/'}
        with mock.patch.object(crew, 'find_estate_root', return_value=self.root):
            warnings = crew._claim_output_warnings(task, {'tasks': [task, other]})
        self.assertIn('same-file overlap', ' '.join(warnings))
        self.assertIn('invalid OUTPUT', ' '.join(warnings))

    def test_report_file_collision_blocks_even_with_current_folder_rule(self):
        other = {'id': 'two', 'status': 'claimed', 'body': 'OUTPUT: Artifacts/results/REPORT.md'}
        self.assertTrue(self.check(others=[other])[0])

    def test_shared_and_nested_folders_note_only(self):
        for rule in ('Artifacts/results/', 'Artifacts/', 'Artifacts/results/child/'):
            other = {'id': 'two', 'status': 'claimed', 'body': 'OUTPUT: ' + rule}
            problems, notes, info = self.check(others=[other])
            self.assertEqual(problems, [])
            self.assertIn('overlap', ' '.join(notes).lower())

    def test_done_cancelled_and_self_are_ignored(self):
        for status in ('done', 'cancelled'):
            other = {'id': 'two', 'status': status, 'body': 'OUTPUT: Artifacts/results/REPORT.md'}
            self.assertEqual(self.check(others=[other])[0], [])
        self.assertEqual(self.check()[0], [])

    def test_no_output_skips_and_prefix_sibling_is_not_contained(self):
        self.assertIn('OUTPUT check skipped', ' '.join(self.check('No output rule.')[1]))
        self.assertTrue(self.check('OUTPUT: Artifacts/result/')[0])

    def test_claim_warns_without_blocking_including_existing_claim(self):
        task = dict(self.task, body='OUTPUT: Artifacts/results/REPORT.md', fence=1,
                    lease={'actor': 'worker', 'until': 200, 'fence': 1})
        other = {'id': 'two', 'status': 'claimed', 'body': task['body']}
        client = mock.Mock(actor_id='worker')
        args = argparse.Namespace(task='one', request_id=None, seconds=60)
        with mock.patch.object(crew, 'find_estate_root', return_value=self.root), \
             mock.patch.object(crew, 'own_active_lease', return_value=task['lease']):
            result, lines = crew.cmd_claim(client, {'tasks': [task, other], 'server_time': 100}, args)
        self.assertIn('OUTPUT', '\n'.join(lines))
        self.assertTrue(result['output_warnings'])
        client.command.assert_not_called()


class GateNextCitationTests(GateBase):
    def test_submit_enforces_output_before_staging(self):
        task = self.claimed(body='OUTPUT: Artifacts/notes/')
        err = self.blocked(task, self.report(self.clean_report()))
        self.assertIn('OUTPUT', err)

    def test_submit_rejects_same_file_declared_by_other_open_task(self):
        report = self.estate / 'Artifacts' / 'notes' / 'REPORT.md'
        report.write_text(self.clean_report())
        other = self.intake(self.room, title='Other output owner', body='OUTPUT: Artifacts/notes/REPORT.md')
        task = self.claimed(body='OUTPUT: Artifacts/notes/')
        err = self.blocked(task, report)
        self.assertIn(other, err)

    def assert_partial_neighbor_submit_blocked(self, separator, bad_first):
        report = self.estate / 'Artifacts' / 'notes' / 'REPORT.md'
        report.write_text(self.clean_report())
        valid, invalid = 'Artifacts/notes/REPORT.md', '../invalid/'
        entries = (invalid, valid) if bad_first else (valid, invalid)
        other = self.intake(self.room, title='Partial output owner', body='OUTPUT: ' + separator.join(entries))
        task = self.claimed(body='OUTPUT: ' + valid)
        err = self.blocked(task, report)
        self.assertIn(other, err)
        self.assertIn('same-file overlap', err)
        self.assertIn('invalid OUTPUT', err)

    def test_submit_comma_neighbor_invalid_last_blocks_without_staging(self):
        self.assert_partial_neighbor_submit_blocked(', ', False)

    def test_submit_comma_neighbor_invalid_first_blocks_without_staging(self):
        self.assert_partial_neighbor_submit_blocked(', ', True)

    def test_submit_multiline_neighbor_invalid_last_blocks_without_staging(self):
        self.assert_partial_neighbor_submit_blocked('\nOUTPUT: ', False)

    def test_submit_multiline_neighbor_invalid_first_blocks_without_staging(self):
        self.assert_partial_neighbor_submit_blocked('\nOUTPUT: ', True)

    def test_zero_counts_and_warning_always_print(self):
        task = self.claimed()
        code, out, err = self.submit(task, self.report('MEASURED [OP 12].'), json_mode=False)
        self.assertEqual(code, 0, err)
        self.assertIn('gate: citations: 0 found, 0 checked, 0 problems', out)
        self.assertIn('no citations found', out)

    def test_additional_deliverables_are_repeatable_and_aggregate(self):
        task = self.claimed()
        report = self.report('MEASURED `Artifacts/notes/source.md:1`.')
        one = self.report('MEASURED `Artifacts/notes/source.md:2`.', 'one.md')
        two = self.report('MEASURED `Artifacts/notes/source.md:3`.', 'two.md')
        code, out, err = self.submit(task, report, '--also-check', str(one), '--also-check', str(two))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['gate']['citation_totals'], {'found': 3, 'checked': 3, 'problems': 0})

    def test_bad_deliverable_blocks_and_prints_counts(self):
        task = self.claimed()
        bad = self.report('MEASURED `Artifacts/notes/absent.md:1`.', 'bad.md')
        err = self.blocked(task, self.report(self.clean_report()), '--also-check', str(bad))
        self.assertIn('gate: citations: 1 found, 1 checked, 1 problems', err)
        self.assertIn('bad.md', err)

    def test_no_root_still_has_counts(self):
        task = self.claimed()
        with tempfile.TemporaryDirectory() as bare, mock.patch.object(crew, 'ROOT', Path(bare)):
            code, out, err = self.cli(None, ('submit', task, '--file', str(self.report(self.clean_report())),
                                          '--summary', 'Done.'), json_mode=False)
        self.assertEqual(code, 0, err)
        self.assertIn('gate: citations: 0 found, 0 checked, 0 problems', out)


class GateDeclaredEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.repo, self.base, self.commit = make_repo({}, {'a.py': '1\n'})
        self.folder = case_dir('declared-evidence')
        self.argv = ['lune', 'run', 'tests/all.luau']
        self.body = 'RED/GREEN\nTEST_COMMAND: ' + json.dumps(self.argv)
        self.args = argparse.Namespace(repo=str(self.repo), base=self.base, commit=self.commit,
                                       file=str(self.folder / 'REPORT.md'), evidence=str(self.folder / 'EVIDENCE.json'))
        self.data = {'tool': 'declared-command-evidence-v1', 'parent': {'sha': self.base},
                     'commit': {'sha': self.commit}, 'runs': {}}
        for phase, revision, passed, failed, code in [('parent', self.base, 1, 1, 1), ('commit', self.commit, 2, 0, 0)]:
            output = self.folder / (phase + '.txt')
            output.write_text('Fixture suite\n%d passed, %d failed (143 spec files)\n' % (passed, failed))
            receipt = {'protocol': 'declared-command-run-v1', 'revision': revision, 'argv': self.argv,
                       'exit_code': code, 'status': 'RED' if failed else 'GREEN', 'total': 2, 'passed': passed,
                       'failed': failed, 'summary_line': 2,
                       'output': {'path': output.name, 'sha256': self.digest(output)}}
            self.save_run(phase, receipt)
        self.save_manifest()

    def digest(self, path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def save_run(self, phase, receipt):
        path = self.folder / (phase + '.json')
        path.write_text(json.dumps(receipt))
        self.data['runs'][phase] = {'path': path.name, 'sha256': self.digest(path)}

    def save_manifest(self):
        Path(self.args.evidence).write_text(json.dumps(self.data))

    def run_gate(self):
        problems, notes, info = [], [], {}
        crew._gate_evidence(self.body, self.args, self.commit, problems, notes, info)
        return problems, notes, info

    def edit_run(self, phase, field, value):
        receipt = json.loads((self.folder / (phase + '.json')).read_text())
        receipt[field] = value
        self.save_run(phase, receipt)
        self.save_manifest()

    def test_bound_saved_receipts_pass_without_executing_declared_argv(self):
        with mock.patch.object(crew.subprocess, 'run', wraps=subprocess.run) as run:
            problems, notes, info = self.run_gate()
        self.assertEqual(problems, [])
        self.assertEqual(info['evidence'], 'OK')
        self.assertIn('independent', ' '.join(notes))
        self.assertTrue(all(call.args[0][0] == 'git' for call in run.call_args_list))

    def test_mismatched_argv_revision_exit_status_counts_and_summary_block(self):
        for field, value in [('argv', ['lune', 'run', 'other.luau']), ('revision', self.base),
                             ('exit_code', 2), ('status', 'RED'), ('total', 0), ('passed', 1),
                             ('failed', 1), ('summary_line', 1)]:
            original = json.loads((self.folder / 'commit.json').read_text())
            self.edit_run('commit', field, value)
            self.assertTrue(self.run_gate()[0])
            self.save_run('commit', original)
            self.save_manifest()

    def test_unknown_tool_and_missing_commit_cannot_bypass_declared_validation(self):
        self.data['tool'] = 'declared-command-evidence-v0'
        self.save_manifest()
        self.assertTrue(self.run_gate()[0])
        self.data['tool'] = 'declared-command-evidence-v1'
        self.save_manifest()
        self.args.commit = None
        self.assertTrue(self.run_gate()[0])

    def test_non_red_parent_blocks(self):
        self.edit_run('parent', 'exit_code', 0)
        self.assertTrue(self.run_gate()[0])

    def test_receipt_and_output_tampering_block(self):
        receipt = self.folder / 'commit.json'
        original = receipt.read_bytes()
        receipt.write_bytes(original + b' ')
        self.assertTrue(self.run_gate()[0])
        receipt.write_bytes(original)
        (self.folder / 'commit.txt').write_text('forged')
        self.assertTrue(self.run_gate()[0])

    def test_parent_must_match_explicit_base_and_be_ancestor(self):
        self.data['parent']['sha'] = self.commit
        self.save_manifest()
        self.assertTrue(self.run_gate()[0])
        self.data['parent']['sha'] = self.base
        self.save_manifest()
        self.args.base = None
        self.assertTrue(self.run_gate()[0])

    def test_path_escape_and_missing_receipts_block(self):
        for path in ('../outside.json', str(self.folder / 'commit.json'), 'missing.json'):
            self.data['runs']['commit']['path'] = path
            self.save_manifest()
            self.assertTrue(self.run_gate()[0])

    def test_declared_argv_is_required_bounded_and_not_echoed(self):
        for command in ('', 'TEST_COMMAND: "secret-marker"',
                        'TEST_COMMAND: ["lune", "secret-marker\\n"]'):
            self.body = 'RED/GREEN\n' + command
            problems, _, _ = self.run_gate()
            self.assertTrue(problems)
            self.assertNotIn('secret-marker', ' '.join(problems))


if __name__ == '__main__':
    unittest.main(argv=sys.argv[:1])    # evidence.py passes the tree root as an argument; it names no test
