"""crew/cite_check.py: the mechanical citation check (piece C / improvement #6 of
BUILD_PLAN_COMMON_ROOM_AUTONOMY_2026-09-24.md).

crew/cite_check.py must run under python3.10, Python 3.8 grammar, stdlib only, against a
Windows-mounted folder, so it is loaded here the way tests/test_relay_ask.py loads
crew/relay_ask.py: importlib.util.spec_from_file_location, never a package import. Fixtures are
fake estate roots built with tests.helpers.case_dir, each with its own Artifacts/ and
Lab Notes/ folders (never anything under the real worktree).

RED note: this file was written before crew/cite_check.py existed; its first run failed at
spec_from_file_location / exec_module ("FileNotFoundError"). See
evidence/autonomy-2026-09-24/cite/RED.txt.
"""
import importlib.util, io, json, os, subprocess, sys, unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tests.helpers import case_dir
from tests.test_relay import RelayTestCase
from common_room import ollama_worker as ow

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'crew' / 'cite_check.py'
SPEC = importlib.util.spec_from_file_location('cite_check', SCRIPT)
cite_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cite_check)


def build_estate(name):
    """A fake estate root: Artifacts/ and Lab Notes/ folders with one known document each,
    line numbers chosen so a report can cite an exact, checkable line."""
    root = case_dir(name)
    (root / 'Artifacts' / '04_Log').mkdir(parents=True)
    (root / 'Lab Notes' / '04_Log').mkdir(parents=True)
    decisions = ('Line one\nLine two\nThe board approved the budget for next quarter.\nLine four\nLine five\n')
    (root / 'Artifacts' / '04_Log' / 'Decisions.md').write_text(decisions, encoding='utf-8')
    spaced = ('Spaced line one\nSpaced line two\nThe committee tabled the proposal until spring.\n')
    (root / 'Lab Notes' / '04_Log' / 'Decisions With Spaces.md').write_text(spaced, encoding='utf-8')
    return root


def scratch_file(root, name, content):
    """A file for report text or a relay answer: inside the estate root's own unique test
    folder, but in a '_scratch' subfolder so it is never itself mistaken for a citation target
    (it names no Artifacts/ or Lab Notes/ path)."""
    folder = root / '_scratch'
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_text(content, encoding='utf-8')
    return path


class CiteCheckModuleCompatibilityTests(unittest.TestCase):
    """Gate C: 'Python 3.8 grammar', checked the exact way the house rules specify."""

    def test_ast_parses_at_feature_version_3_8(self):
        import ast
        source = SCRIPT.read_text(encoding='utf-8')
        ast.parse(source, feature_version=(3, 8))  # raises SyntaxError if it does not hold

    def test_compiles_under_python310(self):
        try:
            result = subprocess.run(['python3.10', '-m', 'py_compile', str(SCRIPT)],
                                     capture_output=True, text=True)
        except FileNotFoundError:
            self.skipTest('python3.10 is not available in this environment')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_39_plus_only_syntax(self):
        import ast
        tree = ast.parse(SCRIPT.read_text(encoding='utf-8'))
        match_node = getattr(ast, 'Match', None)
        for node in ast.walk(tree):
            if match_node is not None and isinstance(node, match_node):
                self.fail('cite_check.py must stay 3.8-compatible: no match statements.')
            annotation = None
            if isinstance(node, ast.AnnAssign):
                annotation = node.annotation
            elif isinstance(node, ast.arg):
                annotation = node.annotation
            if annotation is not None and isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
                self.fail('cite_check.py must stay 3.8-compatible: no `X | Y` annotations.')

    def test_never_imports_common_room(self):
        import ast
        tree = ast.parse(SCRIPT.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.level, 1, 'cite_check.py must not import sibling packages')
                if node.module:
                    self.assertFalse(node.module.startswith('common_room'),
                                      'cite_check.py must not import common_room (it needs Python 3.12)')
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(alias.name.startswith('common_room'),
                                      'cite_check.py must not import common_room (it needs Python 3.12)')

    def test_runs_under_python310_on_a_fixture(self):
        """Gate C's own 'runs under python3.10': the actual script, as a subprocess, against a
        real fixture -- not just the compile check above."""
        root = build_estate('cite-py310-subprocess')
        report = scratch_file(root, 'report.txt', 'MEASURED: Artifacts/04_Log/Decisions.md:3 confirms it.\n')
        try:
            result = subprocess.run(['python3.10', str(SCRIPT), '--root', str(root), str(report)],
                                     capture_output=True, text=True)
        except FileNotFoundError:
            self.skipTest('python3.10 is not available in this environment')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('OK', result.stdout)
        self.assertIn('Overall: OK', result.stdout)


class CiteCheckDenyListsMatchOllamaWorkerTests(unittest.TestCase):
    """crew/cite_check.py copies these three sets as literals (it cannot import common_room);
    this pins them to the live module so a change on one side is caught on the other."""

    def test_text_extensions_equal(self):
        self.assertEqual(cite_check.TEXT_EXTENSIONS, ow.TEXT_EXTENSIONS)

    def test_denied_components_equal(self):
        self.assertEqual(cite_check.DENIED_COMPONENTS, ow.DENIED_COMPONENTS)

    def test_denied_names_equal(self):
        self.assertEqual(cite_check.DENIED_NAMES, ow.DENIED_NAMES)


class CiteCheckResultKindTests(unittest.TestCase):
    def test_ok_citation_with_line_and_matching_quote(self):
        root = build_estate('cite-ok')
        report = scratch_file(root, 'report.txt',
            'See `Artifacts/04_Log/Decisions.md:3` where it says '
            '"The board approved the budget for next quarter" -- MEASURED.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_OK)
        self.assertIsNone(payload['citations'][0]['detail'])
        self.assertTrue(payload['ok'])

    def test_ok_bare_file_citation_with_no_line(self):
        root = build_estate('cite-ok-bare')
        report = scratch_file(root, 'report.txt', 'MEASURED: see Artifacts/04_Log/Decisions.md for background.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_OK)
        self.assertIsNone(payload['citations'][0]['locator'] if 'locator' in payload['citations'][0] else None)

    def test_missing_file(self):
        root = build_estate('cite-missing')
        report = scratch_file(root, 'report.txt', 'MEASURED: Artifacts/04_Log/NoSuchFile.md:1 says so.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_MISSING)
        self.assertFalse(payload['ok'])

    def test_out_of_range_line(self):
        root = build_estate('cite-range')
        report = scratch_file(root, 'report.txt', 'MEASURED: Artifacts/04_Log/Decisions.md:99 says so.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_RANGE)
        self.assertIn('5 line', payload['citations'][0]['detail'])

    def test_out_of_range_reversed_range(self):
        root = build_estate('cite-range-reversed')
        report = scratch_file(root, 'report.txt', 'MEASURED: Artifacts/04_Log/Decisions.md:4-2 says so.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_RANGE)

    def test_quote_not_found(self):
        root = build_estate('cite-quote-miss')
        report = scratch_file(root, 'report.txt',
            'MEASURED: Artifacts/04_Log/Decisions.md:3 "this text is nowhere in the file at all"\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_QUOTE)

    def test_quote_found_within_two_lines(self):
        root = build_estate('cite-quote-near')
        # The quote is on line 3; the citation points at line 5 -- within the +/-2 window.
        report = scratch_file(root, 'report.txt',
            'MEASURED: Artifacts/04_Log/Decisions.md:5 "The board approved the budget for next quarter"\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_OK)

    def test_short_quoted_string_is_ignored(self):
        """Fewer than 12 characters: not treated as a quote to verify at all."""
        root = build_estate('cite-quote-short')
        report = scratch_file(root, 'report.txt', 'MEASURED: Artifacts/04_Log/Decisions.md:1 "nope"\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_OK)

    def test_file_over_the_1mb_read_cap_is_out_of_range(self):
        root = build_estate('cite-oversize')
        big = root / 'Artifacts' / '04_Log' / 'Big.md'
        with open(str(big), 'w', encoding='utf-8') as handle:
            handle.write('x' * (cite_check.MAX_FILE_BYTES + 1))
        report = scratch_file(root, 'report.txt', 'MEASURED: see Artifacts/04_Log/Big.md for background.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_RANGE)
        self.assertIn('1 MB', payload['citations'][0]['detail'])

    def test_non_utf8_file_is_missing(self):
        root = build_estate('cite-binary')
        binary = root / 'Artifacts' / '04_Log' / 'Binary.md'
        binary.write_bytes(b'\xff\xfe\x00\x01not utf-8')
        report = scratch_file(root, 'report.txt', 'MEASURED: see Artifacts/04_Log/Binary.md for background.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_MISSING)

    def test_refused_secret_looking_denied_component(self):
        root = build_estate('cite-denied-component')
        (root / 'Artifacts' / '.runtime').mkdir()
        (root / 'Artifacts' / '.runtime' / 'state.md').write_text('classified\n', encoding='utf-8')
        report = scratch_file(root, 'report.txt', 'MEASURED: Artifacts/.runtime/state.md:1 says so.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_SECRET)
        self.assertFalse(payload['ok'])

    def test_refused_secret_looking_denied_name_pattern(self):
        root = build_estate('cite-denied-name')
        (root / 'Artifacts' / 'my-credential-notes.md').write_text('classified\n', encoding='utf-8')
        report = scratch_file(root, 'report.txt', 'MEASURED: Artifacts/my-credential-notes.md:1 says so.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_SECRET)

    def test_refused_parent_traversal(self):
        root = build_estate('cite-traversal')
        # A real file sits exactly where 'Artifacts/../outside.md' would resolve to (root itself,
        # a sibling of Artifacts): the refusal must be the '..' component itself, not merely
        # that nothing is there, or a broken guard would silently read this file as OK.
        (root / 'outside.md').write_text('reachable only by escaping Artifacts\n', encoding='utf-8')
        report = scratch_file(root, 'report.txt', 'MEASURED: Artifacts/../outside.md:1 says so.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_LINKED)
        self.assertIn('traversal', payload['citations'][0]['detail'])

    def test_refused_symlink_on_the_path(self):
        root = build_estate('cite-symlink')
        (root / '_scratch').mkdir(exist_ok=True)
        target = root / '_scratch' / 'outside-the-estate.md'
        target.write_text('secret content elsewhere\n', encoding='utf-8')
        link = root / 'Artifacts' / '04_Log' / 'Linked.md'
        try:
            os.symlink(str(target), str(link))
        except (OSError, NotImplementedError):
            self.skipTest('symlinks are not available in this environment')
        report = scratch_file(root, 'report.txt', 'MEASURED: Artifacts/04_Log/Linked.md:1 says so.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_LINKED)
        self.assertIn('symlink', payload['citations'][0]['detail'])

    def test_refused_symlinked_directory_on_the_path(self):
        """A linked directory partway down the path is refused too, not just a linked leaf file."""
        root = build_estate('cite-symlink-dir')
        (root / '_scratch').mkdir(exist_ok=True)
        outside = root / '_scratch' / 'outside-dir'
        outside.mkdir()
        (outside / 'Notes.md').write_text('x\n', encoding='utf-8')
        try:
            os.symlink(str(outside), str(root / 'Artifacts' / 'Linked'))
        except (OSError, NotImplementedError):
            self.skipTest('symlinks are not available in this environment')
        report = scratch_file(root, 'report.txt', 'MEASURED: Artifacts/Linked/Notes.md:1 says so.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_LINKED)


class CiteCheckDeniedPathsNeverOpenedTests(unittest.TestCase):
    """A denied path is refused purely by string matching, before any filesystem read -- proven
    here by patching open() and os.open() and never seeing the denied path pass through them."""

    def test_denied_component_path_is_never_opened(self):
        root = build_estate('cite-deny-open-component')
        (root / 'Artifacts' / '_Private').mkdir()
        target = root / 'Artifacts' / '_Private' / 'notes.txt'
        target.write_text('classified\n', encoding='utf-8')
        report = scratch_file(root, 'report.txt', 'MEASURED: Artifacts/_Private/notes.txt:1 says so.\n')
        real_open = io.open

        def guarded_open(file, *args, **kwargs):
            if '_Private' in str(file) or 'notes.txt' in str(file):
                raise AssertionError('a denied path must never be opened: %r' % (file,))
            return real_open(file, *args, **kwargs)

        with mock.patch('builtins.open', side_effect=guarded_open), mock.patch('os.open') as os_open:
            payload = cite_check.run(str(root), str(report), False)
        os_open.assert_not_called()
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_SECRET)

    def test_denied_name_pattern_path_is_never_opened(self):
        root = build_estate('cite-deny-open-name')
        target = root / 'Artifacts' / 'service-account-info.md'
        target.write_text('classified\n', encoding='utf-8')
        report = scratch_file(root, 'report.txt', 'MEASURED: Artifacts/service-account-info.md:1 says so.\n')
        real_open = io.open

        def guarded_open(file, *args, **kwargs):
            if 'service-account-info.md' in str(file):
                raise AssertionError('a denied path must never be opened: %r' % (file,))
            return real_open(file, *args, **kwargs)

        with mock.patch('builtins.open', side_effect=guarded_open), mock.patch('os.open') as os_open:
            payload = cite_check.run(str(root), str(report), False)
        os_open.assert_not_called()
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_SECRET)


class CiteCheckCaseInsensitiveRootTests(unittest.TestCase):
    """M5 fix: the estate roots (Artifacts, Lab Notes, D:\\Work Area\\) are recognised in any
    case, and resolved against the real, correctly-cased folder on disk."""

    def test_lowercase_artifacts_root_is_recognised_and_checked(self):
        root = build_estate('cite-lower-artifacts')
        report = scratch_file(root, 'report.txt', 'MEASURED: artifacts/04_Log/Decisions.md:3 confirms it.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(len(payload['citations']), 1)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_OK)
        self.assertTrue(payload['ok'])

    def test_shouting_pi_projects_root_is_recognised(self):
        root = build_estate('cite-shout-pi-projects')
        report = scratch_file(root, 'report.txt',
            'MEASURED: LAB NOTES/04_Log/Decisions With Spaces.md#L1-L3 covers it.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_OK)

    def test_lowercase_drive_ai_access_prefix_is_recognised(self):
        root = build_estate('cite-lower-ai-access')
        report = scratch_file(root, 'report.txt',
            r'MEASURED: d:\work area\Artifacts\04_Log\Decisions.md:3 says so.' + '\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_OK)

    def test_lowercase_root_with_denied_component_is_still_refused(self):
        """Case-insensitivity must not weaken the deny list: lowercasing the root shouldn't let a
        secret-looking path through."""
        root = build_estate('cite-lower-denied')
        (root / 'Artifacts' / '.runtime').mkdir()
        (root / 'Artifacts' / '.runtime' / 'state.md').write_text('classified\n', encoding='utf-8')
        report = scratch_file(root, 'report.txt', 'MEASURED: artifacts/.runtime/state.md:1 says so.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_SECRET)


class CiteCheckBroaderCharacterSetTests(unittest.TestCase):
    """M5 fix: characters ollama_worker's own INPUT resolver accepts (apostrophe, ampersand,
    non-ASCII letters) must not make a citation invisible."""

    def test_apostrophe_and_ampersand_in_path_are_recognised(self):
        root = build_estate('cite-apostrophe-ampersand')
        target = root / 'Artifacts' / "O'Brien & Co Notes.md"
        target.write_text('Line one\nLine two\nThe deal closed on schedule.\n', encoding='utf-8')
        report = scratch_file(root, 'report.txt', "MEASURED: Artifacts/O'Brien & Co Notes.md:3 says so.\n")
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(len(payload['citations']), 1)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_OK)

    def test_non_ascii_characters_in_path_are_recognised(self):
        root = build_estate('cite-non-ascii')
        target = root / 'Artifacts' / 'Caf\u00e9 Notes.md'
        target.write_text('Line one\nLine two\nEspresso sales rose in autumn.\n', encoding='utf-8')
        report = scratch_file(root, 'report.txt', 'MEASURED: Artifacts/Caf\u00e9 Notes.md:3 says so.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(len(payload['citations']), 1)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_OK)


class CiteCheckUnrecognisedLocationTests(unittest.TestCase):
    """M5 fix: a path-like citation with a line reference that this tool cannot resolve is
    reported as NOT CHECKED (a problem, exit 1) instead of silently vanishing."""

    def test_tree_relative_path_with_line_locator_is_not_checked(self):
        root = build_estate('cite-tree-relative')
        report = scratch_file(root, 'report.txt', 'MEASURED: common_room/relay.py:615 handles it.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(len(payload['citations']), 1)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_NOT_CHECKED)
        self.assertFalse(payload['ok'])

    def test_root_level_ai_access_file_with_no_artifacts_or_pi_projects_is_not_checked(self):
        root = build_estate('cite-root-level-ai-access')
        report = scratch_file(root, 'report.txt', r'MEASURED: D:\Work Area\CREW_BRIEF.md:3 says so.' + '\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(len(payload['citations']), 1)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_NOT_CHECKED)

    def test_unlisted_extension_under_a_recognised_root_is_not_checked(self):
        root = build_estate('cite-unlisted-extension')
        (root / 'Artifacts' / '04_Log' / 'notes.rs').write_text('fn main() {}\n', encoding='utf-8')
        report = scratch_file(root, 'report.txt', 'MEASURED: Artifacts/04_Log/notes.rs:5 says so.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(len(payload['citations']), 1)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_NOT_CHECKED)

    def test_recognised_citation_is_not_also_reported_as_unrecognised(self):
        """Overlap dedup: a fully valid Artifacts/ citation must produce exactly one row, not a
        second NOT CHECKED row from the generic detector."""
        root = build_estate('cite-no-double-count')
        report = scratch_file(root, 'report.txt', 'MEASURED: Artifacts/04_Log/Decisions.md:1 is fine.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(len(payload['citations']), 1)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_OK)

    def test_recognised_and_unrecognised_citations_both_counted_on_one_line(self):
        root = build_estate('cite-mixed-line')
        report = scratch_file(root, 'report.txt',
            'MEASURED: see Artifacts/04_Log/Decisions.md:1 and also common_room/relay.py:615.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(len(payload['citations']), 2)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_OK)
        self.assertEqual(payload['citations'][1]['result'], cite_check.RESULT_NOT_CHECKED)

    def test_cli_exit_one_and_markdown_shows_not_checked(self):
        root = build_estate('cite-cli-not-checked')
        report = scratch_file(root, 'report.txt', 'MEASURED: common_room/relay.py:615 handles it.\n')
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cite_check.main(['--root', str(root), str(report)])
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(code, 1)
        self.assertIn('NOT CHECKED', out.getvalue())

    def test_json_output_includes_not_checked_result(self):
        root = build_estate('cite-json-not-checked')
        report = scratch_file(root, 'report.txt', 'MEASURED: common_room/relay.py:615 handles it.\n')
        payload = cite_check.run(str(root), str(report), False)
        rendered = json.dumps(payload)
        self.assertIn('NOT CHECKED (unrecognised location)', rendered)


class CiteCheckDotSpaceAndHardLinkTests(unittest.TestCase):
    """LOW fix: mirror ollama_worker's remaining path refusals (trailing dot/space component,
    hard-linked file), refused before any read."""

    @unittest.skipIf(os.name == 'nt', 'Windows drops a trailing space from a new folder name, so no such folder '
                                      'can be made there to cite')
    def test_trailing_space_directory_component_is_refused(self):
        root = build_estate('cite-trailing-space')
        (root / 'Artifacts' / 'Trailing ').mkdir()
        (root / 'Artifacts' / 'Trailing ' / 'file.md').write_text('x\n', encoding='utf-8')
        report = scratch_file(root, 'report.txt', 'MEASURED: Artifacts/Trailing /file.md:1 says so.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_LINKED)
        self.assertIn('dot or space', payload['citations'][0]['detail'])

    def test_trailing_dot_directory_component_is_refused(self):
        root = build_estate('cite-trailing-dot')
        (root / 'Artifacts' / 'Trailing.').mkdir()
        (root / 'Artifacts' / 'Trailing.' / 'file.md').write_text('x\n', encoding='utf-8')
        report = scratch_file(root, 'report.txt', 'MEASURED: Artifacts/Trailing./file.md:1 says so.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_LINKED)
        self.assertIn('dot or space', payload['citations'][0]['detail'])

    def test_hard_linked_file_is_refused(self):
        root = build_estate('cite-hard-link')
        original = root / 'Artifacts' / '04_Log' / 'Original.md'
        original.write_text('Line one\nLine two\n', encoding='utf-8')
        linked = root / 'Artifacts' / '04_Log' / 'Linked.md'
        try:
            os.link(str(original), str(linked))
        except OSError:
            self.skipTest('hard links are not available in this environment')
        report = scratch_file(root, 'report.txt', 'MEASURED: Artifacts/04_Log/Linked.md:1 says so.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_LINKED)
        self.assertIn('hard-linked', payload['citations'][0]['detail'])


class CiteCheckMutantSurvivorTests(unittest.TestCase):
    """Regression tests for the mutant survivors named in VERIFY7B_2026-09-24.md (C07, C08, C09):
    each test fails if the named control is disabled, and passes against the real code."""

    def test_citation_in_backticks_does_not_shadow_a_more_distant_real_quote(self):
        """C07: _nearby_quote's overlap-skip. The backtick-fenced citation itself is also a
        12+ char QUOTE_RE match; its "distance" under the min(...) formula for an overlapping
        span comes out around the citation's own length (here ~32), not 0. So the skip only
        shows up once a genuine quote sits FARTHER from the citation than the citation is long:
        without the skip, the mutant picks the citation's own text as the 'quote', checks it
        against the file (where it obviously never appears) and wrongly reports QUOTE NOT FOUND;
        with the skip, the real, distant quote is found and matches."""
        root = build_estate('cite-c07-backtick-overlap')
        filler = ' -- ' + ('padding ' * 10) + '-- '
        report = scratch_file(root, 'report.txt',
            'MEASURED: `Artifacts/04_Log/Decisions.md:3`' + filler +
            '"The board approved the budget for next quarter".\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_OK)

    def test_label_word_boundary_excludes_superstrings(self):
        """C08: LABEL_RE's \\b word boundaries. Without them, 'UNMEASURED', 'PREMEASURED' and
        'MEASUREDLY' would each also count as a MEASURED label."""
        root = build_estate('cite-c08-label-boundary')
        report = scratch_file(root, 'report.txt',
            'UNMEASURED and PREMEASURED and MEASUREDLY are not evidence labels, but MEASURED is.\n'
            'Artifacts/04_Log/Decisions.md:1 is fine.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['evidence_labels']['MEASURED'], 1)

    def test_quote_matches_despite_whitespace_differences(self):
        """C09: _normalize_ws collapses whitespace runs before comparing. Without it, the file's
        irregular spacing (a double space and a tab) would make an otherwise-matching quote fail."""
        root = build_estate('cite-c09-whitespace-normalize')
        (root / 'Artifacts' / '04_Log' / 'Spaced.md').write_text(
            'Line one\nLine two\nThe   board approved\tthe budget for next quarter.\nLine four\n',
            encoding='utf-8')
        report = scratch_file(root, 'report.txt',
            'MEASURED: Artifacts/04_Log/Spaced.md:3 "The board approved the budget for next quarter"\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_OK)


class CiteCheckWrappedCitationTests(unittest.TestCase):
    """VERIFY8B N2 (MEDIUM regression): a recognised citation directly wrapped in a parenthesis,
    a Markdown link, bold/italic markers, quotes or similar must still be recognised and checked
    -- the wrapping character must not make the generic/bare detector's own match start one
    character earlier and win the overlap. A fabricated line inside the same wrapping must stay
    OUT OF RANGE, not fall back to NOT CHECKED (a MISSING/RANGE/QUOTE problem is grounds to reject
    on its own; NOT CHECKED is merely "check it by hand")."""

    def test_citation_in_parentheses_is_recognised_and_checked(self):
        root = build_estate('cite-n2-parens')
        report = scratch_file(root, 'report.txt', 'MEASURED: see (Artifacts/04_Log/Decisions.md:3) for it.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(len(payload['citations']), 1)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_OK)

    def test_fabricated_citation_in_parentheses_is_out_of_range_not_not_checked(self):
        root = build_estate('cite-n2-parens-fabricated')
        report = scratch_file(root, 'report.txt', 'MEASURED: see (Artifacts/04_Log/Decisions.md:99) for it.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(len(payload['citations']), 1)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_RANGE)

    def test_citation_in_markdown_link_is_recognised_and_checked(self):
        root = build_estate('cite-n2-markdown-link')
        report = scratch_file(root, 'report.txt', 'MEASURED: [see it](Artifacts/04_Log/Decisions.md#L3) confirms.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(len(payload['citations']), 1)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_OK)

    def test_citation_in_bold_markdown_is_recognised_and_checked(self):
        root = build_estate('cite-n2-bold')
        report = scratch_file(root, 'report.txt', 'MEASURED: **Artifacts/04_Log/Decisions.md:3** confirms it.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(len(payload['citations']), 1)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_OK)

    def test_citation_in_single_quotes_is_recognised_and_checked(self):
        root = build_estate('cite-n2-single-quotes')
        report = scratch_file(root, 'report.txt', "MEASURED: 'Artifacts/04_Log/Decisions.md:3' confirms it.\n")
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(len(payload['citations']), 1)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_OK)

    def test_citation_after_an_equals_sign_is_recognised_and_checked(self):
        root = build_estate('cite-n2-equals')
        report = scratch_file(root, 'report.txt', 'MEASURED: file=Artifacts/04_Log/Decisions.md:3 confirms it.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(len(payload['citations']), 1)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_OK)

    def test_windows_drive_form_wrapped_in_parentheses_is_recognised(self):
        root = build_estate('cite-n2-drive-parens')
        report = scratch_file(root, 'report.txt',
            r'MEASURED: (D:\Work Area\Artifacts\04_Log\Decisions.md:3) confirms it.' + '\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(len(payload['citations']), 1)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_OK)


class CiteCheckBareCitationTests(unittest.TestCase):
    """VERIFY8B N3 (MEDIUM, M5 residual): a bare 'file.ext:N' or 'file.ext#LN' citation with no
    path separator at all -- exactly the shape a report cites a source file by -- must be reported
    NOT CHECKED (a problem, exit 1) instead of being invisible (found 0, exit 0, "Overall: OK").
    Obvious non-citations (a time, a URL's host:port, a version string) must not be flagged."""

    def test_bare_filename_with_colon_locator_is_not_checked(self):
        root = build_estate('cite-n3-bare-colon')
        report = scratch_file(root, 'report.txt', 'MEASURED: relay.py:9999 never moves requests.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(len(payload['citations']), 1)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_NOT_CHECKED)
        self.assertFalse(payload['ok'])

    def test_bare_filename_with_hash_locator_is_not_checked(self):
        root = build_estate('cite-n3-bare-hash')
        report = scratch_file(root, 'report.txt', 'MEASURED: agent_worker.py#L873 sets the workdir.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(len(payload['citations']), 1)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_NOT_CHECKED)

    def test_bare_names_and_a_fabricated_bare_line_together_are_all_not_checked(self):
        """The exact N3 repro: a report whose only citations are bare file names, one of them a
        fabricated line, still gives NOT CHECKED (exit 1), never "Overall: OK"."""
        root = build_estate('cite-n3-report')
        report = scratch_file(root, 'report.txt',
            'VERIFIED IN LOCAL SOURCE: tick.py:318-321 ignores the answer; relay.py:9999 says X.\n')
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cite_check.main(['--root', str(root), str(report)])
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(code, 1)
        self.assertIn('PROBLEMS FOUND', out.getvalue())
        self.assertNotIn('Overall: OK', out.getvalue())

    def test_bare_citation_alongside_a_real_root_citation_both_counted(self):
        root = build_estate('cite-n3-mixed')
        report = scratch_file(root, 'report.txt',
            'MEASURED: compare Artifacts/04_Log/Decisions.md:1 and relay.py:9999.\n')
        payload = cite_check.run(str(root), str(report), False)
        results = sorted(row['result'] for row in payload['citations'])
        self.assertEqual(results, sorted([cite_check.RESULT_OK, cite_check.RESULT_NOT_CHECKED]))

    def test_unrecognised_citation_before_a_root_citation_keeps_appearance_order(self):
        """The root-priority overlap fix (N2) must not reorder non-overlapping citations: root
        priority only decides which of two OVERLAPPING matches is kept, and the surviving matches
        are still reported in the order they appear on the line."""
        root = build_estate('cite-n3-order')
        report = scratch_file(root, 'report.txt',
            'MEASURED: relay.py:9999 then see Artifacts/04_Log/Decisions.md:1.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(len(payload['citations']), 2)
        self.assertEqual(payload['citations'][0]['citation'], 'relay.py:9999')
        self.assertEqual(payload['citations'][1]['citation'], 'Artifacts/04_Log/Decisions.md:1')

    def test_bare_name_inside_a_tree_relative_path_is_not_double_counted(self):
        """'relay.py:615' is a valid bare shape on its own, but here it's the tail of a
        tree-relative path GENERIC_CITATION_RE already reports -- exactly one row, not two."""
        root = build_estate('cite-n3-no-double-count')
        report = scratch_file(root, 'report.txt', 'MEASURED: common_room/relay.py:615 handles it.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(len(payload['citations']), 1)
        self.assertEqual(payload['citations'][0]['citation'], 'common_room/relay.py:615')

    def test_bare_filename_with_no_locator_is_not_a_citation(self):
        """A locator is required, exactly as GENERIC_CITATION_RE already requires: a bare file
        name mentioned in prose with no line reference at all is not, by itself, a citation."""
        root = build_estate('cite-n3-no-locator')
        report = scratch_file(root, 'report.txt', 'MEASURED: see relay.py for the full logic.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'], [])

    def test_a_time_is_not_flagged_as_a_citation(self):
        root = build_estate('cite-n3-time')
        report = scratch_file(root, 'report.txt', 'MEASURED: the standup is at 12:30 every day.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'], [])
        self.assertTrue(payload['ok'])

    def test_a_urls_host_port_is_not_flagged_as_a_citation(self):
        root = build_estate('cite-n3-hostport')
        report = scratch_file(root, 'report.txt', 'MEASURED: connect to example.com:8080 for the API.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'], [])
        self.assertTrue(payload['ok'])

    def test_a_version_string_is_not_flagged_as_a_citation(self):
        root = build_estate('cite-n3-version')
        report = scratch_file(root, 'report.txt', 'MEASURED: upgraded to v2.4.0:1 in this release.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'], [])
        self.assertTrue(payload['ok'])


class CiteCheckSpacesAndPathFormsTests(unittest.TestCase):
    def test_spaces_in_path_with_hash_range_locator(self):
        root = build_estate('cite-spaces')
        report = scratch_file(root, 'report.txt',
            'MEASURED: Lab Notes/04_Log/Decisions With Spaces.md#L1-L3 covers it.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(len(payload['citations']), 1)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_OK)

    def test_backslash_separated_path(self):
        root = build_estate('cite-backslash')
        report = scratch_file(root, 'report.txt', r'MEASURED: Artifacts\04_Log\Decisions.md:3 says so.' + '\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_OK)

    def test_windows_drive_absolute_backslash_form(self):
        root = build_estate('cite-drive-backslash')
        report = scratch_file(root, 'report.txt', r'MEASURED: D:\Work Area\Artifacts\04_Log\Decisions.md:3 says so.' + '\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_OK)

    def test_windows_drive_absolute_forward_slash_form(self):
        root = build_estate('cite-drive-forward')
        report = scratch_file(root, 'report.txt', 'MEASURED: D:/Work Area/Artifacts/04_Log/Decisions.md:3 says so.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_OK)

    def test_hash_single_line_locator(self):
        root = build_estate('cite-hash-single')
        report = scratch_file(root, 'report.txt', 'MEASURED: Artifacts/04_Log/Decisions.md#L3 says so.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_OK)

    def test_multiple_citations_on_one_line(self):
        root = build_estate('cite-multi')
        report = scratch_file(root, 'report.txt',
            'MEASURED: compare Artifacts/04_Log/Decisions.md:1 and Artifacts/04_Log/NoSuchFile.md:1.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(len(payload['citations']), 2)
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_OK)
        self.assertEqual(payload['citations'][1]['result'], cite_check.RESULT_MISSING)

    def test_extensionless_path_is_not_a_citation(self):
        """No recognised extension: not a citation at all, per TEXT_EXTENSIONS, same as a local
        worker would refuse to read it."""
        root = build_estate('cite-no-ext')
        report = scratch_file(root, 'report.txt', 'MEASURED: see Artifacts/04_Log/README for background.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['citations'], [])


class CiteCheckEvidenceLabelTests(unittest.TestCase):
    def test_labels_are_counted(self):
        root = build_estate('cite-labels')
        report = scratch_file(root, 'report.txt',
            'MEASURED: Artifacts/04_Log/Decisions.md:1 is fine. INFERENCE: it probably continues. '
            'MISSING: no page 2. MEASURED: also this.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['evidence_labels']['MEASURED'], 2)
        self.assertEqual(payload['evidence_labels']['INFERENCE'], 1)
        self.assertEqual(payload['evidence_labels']['MISSING'], 1)
        self.assertEqual(payload['evidence_labels']['VERIFIED'], 0)
        self.assertFalse(payload['no_evidence_labels'])

    def test_no_labels_is_flagged_and_fails_even_with_clean_citations(self):
        root = build_estate('cite-no-labels')
        report = scratch_file(root, 'report.txt', 'Artifacts/04_Log/Decisions.md:1 looks fine.\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertTrue(payload['no_evidence_labels'])
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_OK)
        self.assertFalse(payload['ok'])  # no evidence labels is itself a problem


class CiteCheckCapTests(unittest.TestCase):
    def test_at_most_200_citations_are_checked(self):
        root = build_estate('cite-cap')
        lines = ['MEASURED: Artifacts/04_Log/Decisions.md:1 (%d)' % n for n in range(205)]
        report = scratch_file(root, 'report.txt', '\n'.join(lines) + '\n')
        payload = cite_check.run(str(root), str(report), False)
        self.assertEqual(payload['totals']['found'], 205)
        self.assertEqual(payload['totals']['checked'], 200)
        self.assertTrue(payload['totals']['capped'])
        self.assertEqual(len(payload['citations']), 200)
        self.assertTrue(payload['ok'])  # every checked citation is OK and labels are present


class CiteCheckCliAndJsonTests(unittest.TestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cite_check.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_exit_zero_all_ok(self):
        root = build_estate('cite-cli-ok')
        report = scratch_file(root, 'report.txt', 'MEASURED: Artifacts/04_Log/Decisions.md:1 is fine.\n')
        code, out, err = self.run_cli(['--root', str(root), str(report)])
        self.assertEqual(code, 0, err)
        self.assertIn('Overall: OK', out)

    def test_exit_one_on_a_problem(self):
        root = build_estate('cite-cli-problem')
        report = scratch_file(root, 'report.txt', 'MEASURED: Artifacts/04_Log/Gone.md:1 is fine.\n')
        code, out, err = self.run_cli(['--root', str(root), str(report)])
        self.assertEqual(code, 1)
        self.assertIn('MISSING FILE', out)

    def test_json_output_is_one_object(self):
        root = build_estate('cite-cli-json')
        report = scratch_file(root, 'report.txt', 'MEASURED: Artifacts/04_Log/Decisions.md:1 is fine.\n')
        code, out, err = self.run_cli(['--root', str(root), str(report), '--json'])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload['citations'][0]['result'], 'OK')
        self.assertTrue(payload['ok'])

    def test_stdin_report_with_dash(self):
        root = build_estate('cite-cli-stdin')
        stdin = io.StringIO('MEASURED: Artifacts/04_Log/Decisions.md:1 is fine.\n')
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), mock.patch('sys.stdin', stdin):
            try:
                code = cite_check.main(['--root', str(root), '-'])
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(code, 0, err.getvalue())

    def test_exit_two_on_bad_root(self):
        root = build_estate('cite-cli-badroot')
        report = scratch_file(root, 'report.txt', 'MEASURED: fine.\n')
        code, out, err = self.run_cli(['--root', str(root / 'does-not-exist'), str(report)])
        self.assertEqual(code, 2)
        self.assertIn('is not a directory', err)

    def test_exit_two_on_missing_report_file(self):
        root = build_estate('cite-cli-badfile')
        code, out, err = self.run_cli(['--root', str(root), str(root / 'no-such-report.txt')])
        self.assertEqual(code, 2)


class CiteCheckRelayAnswerTests(RelayTestCase):
    """Builds a real relay view_text answer -- ack, claim, stage_text, finish, view_text, all
    through Relay.process_once() exactly as tests/test_relay.py does -- and feeds the resulting
    response dict to cite_check --relay-answer exactly as relay_ask.py would have printed it."""

    def _view_text_answer(self, report_text):
        room = self.start()
        task = self.intake_task(room)
        relay = self.relay(room, ['relay-writer'])

        self.write_request(room, 'relay-writer', 'ack1.json',
            {'request_id': 'ack-1', 'kind': 'command', 'action': 'ack', 'payload': {'task_id': task}})
        relay.process_once()
        self.assertTrue(self.response(room, 'relay-writer', 'ack-1')['ok'])

        self.write_request(room, 'relay-writer', 'claim1.json',
            {'request_id': 'claim-1', 'kind': 'command', 'action': 'claim', 'payload': {'task_id': task, 'seconds': 900}})
        relay.process_once()
        claim = self.response(room, 'relay-writer', 'claim-1')
        self.assertTrue(claim['ok'])
        fence = claim['result']['task']['lease']['fence']

        self.write_request(room, 'relay-writer', 'stage1.json',
            {'request_id': 'stage-1', 'kind': 'handoff', 'operation': 'stage_text',
             'payload': {'task_id': task, 'fence': fence, 'text': report_text}})
        relay.process_once()
        stage = self.response(room, 'relay-writer', 'stage-1')
        self.assertTrue(stage['ok'])
        ref = {'path': stage['result']['path'], 'sha256': stage['result']['sha256']}

        self.write_request(room, 'relay-writer', 'finish1.json',
            {'request_id': 'finish-1', 'kind': 'command', 'action': 'finish',
             'payload': {'task_id': task, 'fence': fence, 'body': 'Done', 'refs': [ref]}})
        relay.process_once()
        self.assertTrue(self.response(room, 'relay-writer', 'finish-1')['ok'])

        self.write_request(room, 'relay-writer', 'view1.json',
            {'request_id': 'view-1', 'kind': 'handoff', 'operation': 'view_text',
             'payload': {'task_id': task, 'path': ref['path'], 'sha256': ref['sha256']}})
        relay.process_once()
        view = self.response(room, 'relay-writer', 'view-1')
        self.assertTrue(view['ok'])
        self.assertEqual(view['result']['text'], report_text)
        return view  # exactly the JSON object relay_ask.py's main() would print

    def test_relay_answer_ok_citation(self):
        estate = build_estate('cite-relay-answer-ok')
        report_text = 'MEASURED: Artifacts/04_Log/Decisions.md:3 confirms the point.\n'
        answer = self._view_text_answer(report_text)
        answer_path = scratch_file(estate, 'relay-answer.json', json.dumps(answer))
        payload = cite_check.run(str(estate), str(answer_path), True)
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_OK)

    def test_relay_answer_carries_through_a_citation_problem(self):
        estate = build_estate('cite-relay-answer-problem')
        report_text = 'MEASURED: Artifacts/04_Log/NoSuchFile.md:3 confirms the point.\n'
        answer = self._view_text_answer(report_text)
        answer_path = scratch_file(estate, 'relay-answer.json', json.dumps(answer))
        payload = cite_check.run(str(estate), str(answer_path), True)
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['citations'][0]['result'], cite_check.RESULT_MISSING)

    def test_relay_answer_not_ok_is_refused_clearly(self):
        estate = build_estate('cite-relay-answer-refused')
        answer = {'ok': False, 'error': 'forbidden', 'request_id': 'view-1',
                  'message': "Only this task's saved result references can be viewed."}
        answer_path = scratch_file(estate, 'relay-answer.json', json.dumps(answer))
        with self.assertRaises(cite_check.UsageError) as caught:
            cite_check.run(str(estate), str(answer_path), True)
        self.assertIn('not ok', str(caught.exception))

    def test_relay_answer_with_no_text_is_refused_clearly(self):
        estate = build_estate('cite-relay-answer-no-text')
        answer = {'ok': True, 'request_id': 'state-1', 'kind': 'state', 'result': {'written': True}}
        answer_path = scratch_file(estate, 'relay-answer.json', json.dumps(answer))
        with self.assertRaises(cite_check.UsageError) as caught:
            cite_check.run(str(estate), str(answer_path), True)
        self.assertIn('no report text', str(caught.exception))

    def test_relay_answer_cli_exit_two_when_not_ok(self):
        estate = build_estate('cite-relay-answer-cli-refused')
        answer = {'ok': False, 'error': 'forbidden', 'message': 'nope'}
        answer_path = scratch_file(estate, 'relay-answer.json', json.dumps(answer))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cite_check.main(['--root', str(estate), '--relay-answer', str(answer_path)])
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(code, 2)
        self.assertIn('not ok', err.getvalue())


class CiteCheckPrefixedRootTests(unittest.TestCase):
    """VERIFY9 N2: a path that merely CONTAINS Artifacts/ or Lab Notes/ after another folder (a backup
    copy, another drive's user folder, a web URL) is not the estate's file: it is NOT CHECKED, not
    checked against the estate. The estate's own absolute forms (D:\\Work Area\\..., the Cowork
    mount .../Work Area/...) and wrapped citations are still checked."""
    def kinds(self, line):
        return [(c['kind'], c['citation']) for c in cite_check.find_citations(line)]

    def test_prefixed_paths_are_not_checked_against_the_estate(self):
        for line in ('backup/Artifacts/x/notes.md:3', 'C:\\Users\\me\\Artifacts\\x\\notes.md:3',
                     'https://github.com/org/repo/blob/main/Artifacts/x/notes.md#L3',
                     '\\\\server\\share\\Artifacts\\x\\notes.md:3'):
            kinds = self.kinds(line)
            self.assertTrue(kinds, line)
            self.assertNotIn('root', [k for k, _ in kinds], (line, kinds))

    def test_estate_absolute_forms_and_wrapped_citations_are_still_checked(self):
        for line in ('$HOME/mnt/Work Area/Artifacts/x/notes.md:3', '/home/u/mnt/Work Area/Artifacts/x/notes.md:3',
                     'D:\\Work Area\\Artifacts\\x\\notes.md:3', '(Artifacts/x/notes.md:3)',
                     '[see](Artifacts/x/notes.md#L2-L4)', '**Artifacts/x/notes.md:3**', 'Artifacts/x/notes.md:3'):
            kinds = self.kinds(line)
            self.assertEqual([k for k, _ in kinds], ['root'], (line, kinds))

if __name__ == '__main__':
    unittest.main()
