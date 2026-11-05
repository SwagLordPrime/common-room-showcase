"""Behavioral tests for `crew branch` (common_room/crew.py, 2026-10-01, crew-branch).

One command gives a job its own clean copy of the code: a worktree <root>\\<task-key> on a new branch
up/<task-key> made from a named base commit. Written before the code: on the base commit `branch` is not a
subcommand, so every case fails there. Real throwaway git repos in a temp folder, no room, no network.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # lets `python tests/test_crew_branch.py` run from a tree root

import contextlib, hashlib, io, json, os, shutil, subprocess, unittest
from contextlib import redirect_stderr, redirect_stdout

import common_room.crew as crew
from tests.helpers import case_dir

GIT = shutil.which('git')
GIT_ARGS = ['-c', 'user.name=Branch Test', '-c', 'user.email=branch@example.invalid', '-c', 'commit.gpgsign=false',
            '-c', 'core.autocrlf=false']


def git(repo, *args, check=True):
    done = subprocess.run([GIT, *GIT_ARGS, '-C', str(repo), *args], capture_output=True, text=True)
    if check:
        assert done.returncode == 0, done.stderr
    return done.stdout.strip() if check else done


def make_repo():
    """A repo with two commits on its main worktree: (repo, first sha, second sha). The second changes a file."""
    parent = case_dir('branch-estate')
    repo = parent / 'project'
    repo.mkdir()
    git(repo, 'init', '-q')
    (repo / 'a.txt').write_text('first\n', encoding='utf-8')
    git(repo, 'add', '-A')
    git(repo, 'commit', '-q', '-m', 'first')
    first = git(repo, 'rev-parse', 'HEAD')
    (repo / 'a.txt').write_text('second\n', encoding='utf-8')
    (repo / 'b.txt').write_text('added later\n', encoding='utf-8')
    git(repo, 'add', '-A')
    git(repo, 'commit', '-q', '-m', 'second')
    return repo, first, git(repo, 'rev-parse', 'HEAD')


def tree_digest(folder):
    """sha256 over every file under `folder` except .git, so a test can prove a working tree was not touched."""
    h = hashlib.sha256()
    for path in sorted(Path(folder).rglob('*')):
        if '.git' in path.relative_to(folder).parts or not path.is_file():
            continue
        h.update(str(path.relative_to(folder)).encode() + b'\0' + path.read_bytes() + b'\0')
    return h.hexdigest()


@unittest.skipIf(GIT is None, 'git is not available')
class CrewBranchTests(unittest.TestCase):
    def run_branch(self, *args, json_mode=False):
        """`crew --credential <nowhere> branch ...`: the command needs no room, so no room is started."""
        argv = ['--credential', str(case_dir('branch-cred') / 'nobody-credential.json')]
        if json_mode:
            argv.append('--json')
        argv += ['branch', *args]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = crew.main(argv)
        return code, out.getvalue(), err.getvalue()

    def assert_nothing_created(self, repo, key, root):
        self.assertFalse((Path(root) / key).exists(), 'a refused branch must not leave a folder')
        self.assertEqual(git(repo, 'branch', '--list', 'up/' + key), '', 'a refused branch must not leave a branch')

    def test_creates_the_worktree_and_branch_at_the_named_base(self):
        repo, first, second = make_repo()
        root = repo.parent / 'copies'
        code, out, err = self.run_branch('crew-demo', '--repo', str(repo), '--base', first, '--root', str(root))
        self.assertEqual(code, 0, err)
        target = root / 'crew-demo'
        self.assertTrue((target / 'a.txt').is_file())
        self.assertEqual((target / 'a.txt').read_text(encoding='utf-8'), 'first\n')     # the base, not the tip
        self.assertFalse((target / 'b.txt').exists())
        self.assertEqual(git(target, 'rev-parse', 'HEAD'), first)
        self.assertEqual(git(target, 'rev-parse', '--abbrev-ref', 'HEAD'), 'up/crew-demo')
        self.assertEqual(git(repo, 'rev-parse', 'refs/heads/up/crew-demo'), first)
        self.assertEqual(git(target, 'status', '--porcelain'), '')

    def test_a_branch_name_or_abbreviated_commit_works_as_the_base(self):
        repo, first, second = make_repo()
        git(repo, 'branch', 'start-here', first)
        code, out, err = self.run_branch('by-name', '--repo', str(repo), '--base', 'start-here', '--root', str(repo.parent / 'c'))
        self.assertEqual(code, 0, err)
        self.assertEqual(git(repo.parent / 'c' / 'by-name', 'rev-parse', 'HEAD'), first)
        code, out, err = self.run_branch('by-short', '--repo', str(repo), '--base', first[:8], '--root', str(repo.parent / 'c'))
        self.assertEqual(code, 0, err)
        self.assertEqual(git(repo.parent / 'c' / 'by-short', 'rev-parse', 'HEAD'), first)

    def test_the_default_root_is_a_wt_folder_beside_the_repo(self):
        repo, first, second = make_repo()
        code, out, err = self.run_branch('beside', '--repo', str(repo), '--base', 'HEAD')
        self.assertEqual(code, 0, err)
        target = repo.parent / 'wt' / 'beside'
        self.assertEqual(git(target, 'rev-parse', 'HEAD'), second)
        self.assertIn(str(target), out)

    def test_it_prints_the_path_the_base_commit_and_the_evidence_line(self):
        repo, first, second = make_repo()
        root = repo.parent / 'copies'
        code, out, err = self.run_branch('printed', '--repo', str(repo), '--base', first[:7], '--root', str(root))
        self.assertEqual(code, 0, err)
        target = root / 'printed'
        self.assertIn('worktree: ' + str(target), out)
        self.assertIn('branch: up/printed', out)
        self.assertIn('base: ' + first, out)                       # the full commit, not the abbreviation given
        line = [l for l in out.splitlines() if l.startswith('RED/GREEN: ')]
        self.assertEqual(len(line), 1, out)
        for part in ('evidence.py', '--repo "' + str(target) + '"', '--parent ' + first[:7], '--commit <your commit>',
                     '--check <test file inside your commit>', '--out <folder outside every git repo>'):
            self.assertIn(part, line[0])

    def test_json_mode_prints_one_object(self):
        repo, first, second = make_repo()
        code, out, err = self.run_branch('as-json', '--repo', str(repo), '--base', first, '--root', str(repo.parent / 'c'),
                                         json_mode=True)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual((data['branch'], data['base']), ('up/as-json', first))
        self.assertEqual(Path(data['worktree']), repo.parent / 'c' / 'as-json')
        self.assertIn('evidence.py', data['evidence_command'])

    def test_it_refuses_an_existing_branch_and_creates_nothing(self):
        repo, first, second = make_repo()
        git(repo, 'branch', 'up/taken', first)
        root = repo.parent / 'copies'
        code, out, err = self.run_branch('taken', '--repo', str(repo), '--base', first, '--root', str(root))
        self.assertEqual(code, 1)
        self.assertIn('up/taken', err)
        self.assertIn('already exists', err)
        self.assertFalse((root / 'taken').exists())

    def test_it_refuses_an_existing_folder_and_creates_no_branch(self):
        repo, first, second = make_repo()
        root = repo.parent / 'copies'
        (root / 'occupied').mkdir(parents=True)
        (root / 'occupied' / 'keep.txt').write_text('mine', encoding='utf-8')
        code, out, err = self.run_branch('occupied', '--repo', str(repo), '--base', first, '--root', str(root))
        self.assertEqual(code, 1)
        self.assertIn('already exists', err)
        self.assertEqual((root / 'occupied' / 'keep.txt').read_text(encoding='utf-8'), 'mine')
        self.assertEqual(git(repo, 'branch', '--list', 'up/occupied'), '')

    def test_it_refuses_an_empty_existing_folder_too(self):
        repo, first, second = make_repo()
        root = repo.parent / 'copies'
        (root / 'empty').mkdir(parents=True)
        code, out, err = self.run_branch('empty', '--repo', str(repo), '--base', first, '--root', str(root))
        self.assertEqual(code, 1)
        self.assertEqual(git(repo, 'branch', '--list', 'up/empty'), '')

    def test_running_it_twice_refuses_the_second_time(self):
        repo, first, second = make_repo()
        root = repo.parent / 'copies'
        self.assertEqual(self.run_branch('twice', '--repo', str(repo), '--base', first, '--root', str(root))[0], 0)
        code, out, err = self.run_branch('twice', '--repo', str(repo), '--base', first, '--root', str(root))
        self.assertEqual(code, 1)
        self.assertIn('already exists', err)
        self.assertEqual(git(root / 'twice', 'rev-parse', 'HEAD'), first)

    def test_an_unknown_base_is_refused_and_creates_nothing(self):
        repo, first, second = make_repo()
        root = repo.parent / 'copies'
        code, out, err = self.run_branch('nobase', '--repo', str(repo), '--base', 'no-such-ref', '--root', str(root))
        self.assertEqual(code, 1)
        self.assertIn('no-such-ref', err)
        self.assert_nothing_created(repo, 'nobase', root)

    def test_a_folder_that_is_not_a_git_repo_is_refused(self):
        plain = case_dir('branch-plain')
        code, out, err = self.run_branch('nogit', '--repo', str(plain), '--base', 'HEAD', '--root', str(plain / 'c'))
        self.assertEqual(code, 1)
        self.assertIn('git', err.lower())
        self.assertFalse((plain / 'c').exists())

    def test_a_missing_repo_folder_is_refused(self):
        code, out, err = self.run_branch('norepo', '--repo', str(case_dir('branch-missing') / 'gone'), '--base', 'HEAD')
        self.assertEqual(code, 1)

    def test_bad_task_keys_are_refused(self):
        repo, first, second = make_repo()
        root = repo.parent / 'copies'
        for key in ('..', 'Upper', 'a/b', 'a b', 'x' * 25, 'dot.dot', ''):
            with self.subTest(key=key):
                code, out, err = self.run_branch(key, '--repo', str(repo), '--base', first, '--root', str(root))
                self.assertEqual(code, 1, key)
                self.assertIn('task key', err.lower())
        code, out, err = self.run_branch('-lead', '--repo', str(repo), '--base', first, '--root', str(root))
        self.assertNotEqual(code, 0)       # the option parser itself refuses a leading hyphen
        self.assertFalse(root.exists())

    def test_a_base_that_looks_like_an_option_is_refused(self):
        repo, first, second = make_repo()
        code, out, err = self.run_branch('optbase', '--repo', str(repo), '--base=--output=x', '--root', str(repo.parent / 'c'))
        self.assertEqual(code, 1)
        self.assertFalse((repo.parent / 'c').exists())

    def test_a_folder_inside_a_repo_is_not_a_repo_to_branch_from(self):
        repo, first, second = make_repo()
        inside = repo / 'sub'
        inside.mkdir()
        code, out, err = self.run_branch('inner', '--repo', str(inside), '--base', first, '--root', str(repo.parent / 'c'))
        self.assertEqual(code, 1)
        self.assertIn('inside a git repo', err)
        self.assertFalse((repo.parent / 'c').exists())

    def test_the_repos_own_worktree_is_never_touched(self):
        repo, first, second = make_repo()
        (repo / 'a.txt').write_text('edited, not committed\n', encoding='utf-8')     # a dirty main worktree
        (repo / 'scratch.txt').write_text('untracked\n', encoding='utf-8')
        before = (tree_digest(repo), git(repo, 'status', '--porcelain'), git(repo, 'rev-parse', 'HEAD'),
                  git(repo, 'rev-parse', '--abbrev-ref', 'HEAD'))
        code, out, err = self.run_branch('hands-off', '--repo', str(repo), '--base', first, '--root', str(repo.parent / 'c'))
        self.assertEqual(code, 0, err)
        after = (tree_digest(repo), git(repo, 'status', '--porcelain'), git(repo, 'rev-parse', 'HEAD'),
                 git(repo, 'rev-parse', '--abbrev-ref', 'HEAD'))
        self.assertEqual(after, before)

    def test_a_root_equal_to_or_inside_the_repo_is_refused_before_anything_is_created(self):    # review #744
        repo, first, second = make_repo()
        (repo / 'a.txt').write_text('dirty, uncommitted\n', encoding='utf-8')
        made = self.run_branch('linked', '--repo', str(repo), '--base', first, '--root', str(repo.parent / 'copies'))
        self.assertEqual(made[0], 0, made[2])
        linked = repo.parent / 'copies' / 'linked'
        cases = (('the repo itself', repo), ('inside the repo', repo / 'inside-root'),
                 ('deep inside the repo', repo / 'a' / 'b' / 'c'), ('inside the .git folder', repo / '.git' / 'x'),
                 ('inside a linked worktree', linked / 'sub'), ('a linked worktree itself', linked))
        for label, root in cases:
            with self.subTest(label):
                before = (tree_digest(repo), git(repo, 'status', '--porcelain'), git(repo, 'rev-parse', 'HEAD'),
                          git(repo, 'branch', '--list'), git(repo, 'worktree', 'list'))
                code, out, err = self.run_branch('nested-task', '--repo', str(repo), '--base', 'HEAD', '--root', str(root))
                self.assertEqual(code, 1, (out, err))
                self.assertIn('nothing was created', err)
                self.assertFalse((root / 'nested-task').exists())
                if root != repo and root != linked:
                    self.assertFalse(root.exists(), 'the refused root folder must not be created')
                after = (tree_digest(repo), git(repo, 'status', '--porcelain'), git(repo, 'rev-parse', 'HEAD'),
                         git(repo, 'branch', '--list'), git(repo, 'worktree', 'list'))
                self.assertEqual(after, before, 'the source repo must be byte, status, HEAD, branch and worktree identical')
                self.assertEqual(git(repo, 'branch', '--list', 'up/nested-task'), '')

    def test_a_sibling_or_custom_root_outside_every_worktree_still_works(self):
        repo, first, second = make_repo()
        for root in (repo.parent / 'wt', repo.parent / 'elsewhere' / 'deeper'):
            with self.subTest(root=str(root)):
                code, out, err = self.run_branch('fine-' + root.name, '--repo', str(repo), '--base', first, '--root', str(root))
                self.assertEqual(code, 0, err)
                self.assertTrue((root / ('fine-' + root.name) / 'a.txt').is_file())

    def test_it_also_works_from_a_repo_that_is_itself_a_worktree(self):
        repo, first, second = make_repo()
        code, out, err = self.run_branch('one', '--repo', str(repo), '--base', first, '--root', str(repo.parent / 'c'))
        self.assertEqual(code, 0, err)
        code, out, err = self.run_branch('two', '--repo', str(repo.parent / 'c' / 'one'), '--base', first,
                                         '--root', str(repo.parent / 'c'))
        self.assertEqual(code, 0, err)
        self.assertEqual(git(repo.parent / 'c' / 'two', 'rev-parse', 'HEAD'), first)

    def test_it_needs_no_room_and_never_reads_the_credential(self):
        repo, first, second = make_repo()
        nowhere = case_dir('branch-no-room') / 'nobody-credential.json'
        self.assertFalse(nowhere.exists())
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = crew.main(['--credential', str(nowhere), 'branch', 'offline', '--repo', str(repo), '--base', first,
                              '--root', str(repo.parent / 'c')])
        self.assertEqual(code, 0, err.getvalue())
        self.assertFalse(nowhere.exists())

    def test_branch_is_listed_in_help(self):
        parser = crew.build_parser()
        self.assertIn('branch', parser.format_help())
        helptext = parser._subparsers._group_actions[0].choices['branch'].format_help()
        for flag in ('--repo', '--base', '--root'):
            self.assertIn(flag, helptext)


if __name__ == '__main__':
    unittest.main(argv=sys.argv[:1])    # evidence.py passes the tree root as an argument; it names no test
