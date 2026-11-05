"""Tests for common_room/join_cards.py (pure stdlib, no other common_room
imports, Python 3.8 compatible) and the thin crew/join_card.py script that
wraps it.

Written before join_cards.py exists: the expected RED is ImportError on
`import common_room.join_cards`.
"""
import ast
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))      # lets `python tests/test_join_cards.py` run from a tree root (tools/evidence.py does)

from tests.helpers import case_dir

ENROLLMENT = {
    'version': 1,
    'actors': [
        {'id': 'local-a', 'role': 'worker', 'label': 'Local strong model', 'session': 'x'},
        {'id': 'local-b', 'role': 'worker', 'label': 'Local light model', 'session': 'x'},
        {'id': 'hermes', 'role': 'worker', 'label': 'Hermes Agent', 'session': 'x'},
        {'id': 'astra-room', 'role': 'worker', 'label': 'Astra', 'session': 'x'},
        {'id': 'astra-audit', 'role': 'reviewer', 'label': 'Astra reviewer', 'session': 'x'},
        {'id': 'claude-code', 'role': 'worker', 'label': 'Claude Code', 'session': 'x'},
        {'id': 'claude-code-b', 'role': 'worker', 'label': 'Claude Code B', 'session': 'x'},
        {'id': 'claude-room', 'role': 'worker', 'label': 'Orchestrator', 'session': 'x'},
        {'id': 'ferrule', 'role': 'worker', 'label': 'Ferrule', 'session': 'x'},
        {'id': 'tinker', 'role': 'reviewer', 'label': 'Reviewer tinker', 'session': 'x'},
        {'id': 'vesper', 'role': 'reviewer', 'label': 'Reviewer vesper', 'session': 'x'},
        {'id': 'crew-audit', 'role': 'reviewer', 'label': 'Fresh audit', 'session': 'x'},
        {'id': 'awl-0921', 'role': 'worker', 'label': 'Awl', 'session': 'x'},
        {'id': 'gauge-0921', 'role': 'reviewer', 'label': 'Gauge', 'session': 'x'},
        {'id': 'mystery-seat', 'role': 'worker', 'label': 'Mystery', 'session': 'x'},
    ],
    'authorities': [
        {'id': 'orchestra', 'members': ['keeper', 'local-a', 'local-b', 'hermes', 'astra-room', 'astra-audit',
                                         'claude-code', 'claude-code-b', 'claude-room', 'ferrule', 'tinker',
                                         'vesper', 'crew-audit', 'awl-0921', 'gauge-0921', 'mystery-seat'],
         'scope': 'Fixture orchestra scope'},
    ],
}

CRON_BLOCK_TEXT = 'You are the Common Room seat "hermes" on Shane\'s PC.'


def _import_join_cards():
    import common_room.join_cards as join_cards
    return join_cards


class JoinCardKindTests(unittest.TestCase):
    def setUp(self):
        self.join_cards = _import_join_cards()

    def test_process_seat_card(self):
        card = self.join_cards.render_card('local-a', ENROLLMENT)
        self.assertIn('No join message is needed', card)
        self.assertIn('Start Common Room Crew.cmd', card)
        self.assertIn('Role: worker', card)
        self.assertIn('orchestra', card)

    def test_cowork_worker_seat_card(self):
        card = self.join_cards.render_card('ferrule', ENROLLMENT)
        self.assertIn('relay_ask.py', card)
        self.assertIn('.runtime/handoffs/relay/ferrule', card)
        self.assertIn('Role: worker', card)

    def test_cowork_reviewer_seat_gets_reviewing_procedure(self):
        card = self.join_cards.render_card('tinker', ENROLLMENT)
        self.assertIn('relay_ask.py', card)
        self.assertIn('Role: reviewer', card)
        self.assertIn('acceptance', card.lower())
        self.assertNotIn('Ask a question through `ask`', card)

    def test_orchestrator_seat_mentions_orchestra_guide(self):
        card = self.join_cards.render_card('claude-room', ENROLLMENT)
        self.assertIn('ORCHESTRA_2026-09-24.md', card)
        self.assertIn('CREW_SEAT_GUIDE_2026-09-23.md', card)

    def test_non_orchestrator_cowork_seat_does_not_mention_orchestra_guide(self):
        card = self.join_cards.render_card('ferrule', ENROLLMENT)
        self.assertNotIn('ORCHESTRA_2026-09-24.md', card)

    def test_codex_seat_card_exact_seats(self):
        card = self.join_cards.render_card('astra-room', ENROLLMENT)
        self.assertIn('crew.cmd', card)
        self.assertIn('astra-room-credential.json', card)
        self.assertIn('whoami', card)
        self.assertIn('tasks', card)

    def test_codex_reviewer_seat_gets_review_queue_next_command(self):
        card = self.join_cards.render_card('astra-audit', ENROLLMENT)
        self.assertIn('review-queue', card)

    def test_codex_prefix_seats(self):
        for seat in ('awl-0921', 'gauge-0921'):
            card = self.join_cards.render_card(seat, ENROLLMENT)
            self.assertIn('crew.cmd', card)
            self.assertIn(seat + '-credential.json', card)

    def test_claude_code_seats(self):
        for seat in ('claude-code', 'claude-code-b'):
            card = self.join_cards.render_card(seat, ENROLLMENT)
            self.assertIn('crew.cmd', card)
            self.assertIn(seat + '-credential.json', card)

    def test_hermes_seat_reads_cron_block_from_tree(self):
        tree = case_dir('join-card-tree')
        (tree / 'HERMES_SEAT_2026-09-23.md').write_text(
            '# Hermes\n\nPaste into cron:\n\n'
            '    ' + CRON_BLOCK_TEXT + '\n'
            '    Run only your own tasks.\n\n'
            'After the block, not part of the prompt.\n', encoding='utf-8')
        card = self.join_cards.render_card('hermes', ENROLLMENT, tree=tree)
        self.assertIn(CRON_BLOCK_TEXT, card)
        self.assertIn('Run only your own tasks.', card)
        self.assertNotIn('After the block', card)

    def test_hermes_seat_without_tree_still_points_to_guide(self):
        card = self.join_cards.render_card('hermes', ENROLLMENT)
        self.assertIn('HERMES_SEAT_2026-09-23.md', card)
        self.assertNotIn(CRON_BLOCK_TEXT, card)

    def test_hermes_seat_with_tree_but_missing_file(self):
        tree = case_dir('join-card-tree-missing')
        card = self.join_cards.render_card('hermes', ENROLLMENT, tree=tree)
        self.assertIn('HERMES_SEAT_2026-09-23.md', card)
        self.assertNotIn(CRON_BLOCK_TEXT, card)

    def test_unknown_seat_kind_says_ask_shane(self):
        card = self.join_cards.render_card('mystery-seat', ENROLLMENT)
        self.assertIn('Ask Shane', card)

    def test_seat_not_enrolled_raises_clear_error(self):
        with self.assertRaises(self.join_cards.JoinCardError):
            self.join_cards.render_card('totally-unknown-seat', ENROLLMENT)

    def test_malformed_enrollment_raises_clear_error(self):
        with self.assertRaises(self.join_cards.JoinCardError):
            self.join_cards.render_card('local-a', {'nonsense': True})

    def test_plan_and_scope_mentioned_when_given(self):
        card = self.join_cards.render_card('local-a', ENROLLMENT, plan_path='/tmp/plan.json', scope='orchestra')
        self.assertIn('/tmp/plan.json', card)
        self.assertIn('orchestra', card)

    def test_no_scopes_enrolled_does_not_crash(self):
        enrollment = {'version': 1, 'actors': [{'id': 'lonely', 'role': 'worker', 'label': 'L', 'session': 'x'}],
                      'authorities': []}
        card = self.join_cards.render_card('lonely', enrollment)
        self.assertIn('lonely', card)

    def test_worker_seat_never_gets_reviewer_only_house_rules(self):  # P18
        card = self.join_cards.render_card('ferrule', ENROLLMENT)
        self.assertIn('Role: worker', card)
        self.assertNotIn('Take one task from your review queue at a time.', card)
        self.assertIn('Ask a question through `ask`', card)

    def test_reviewer_seat_never_gets_worker_only_house_rules(self):  # P18
        card = self.join_cards.render_card('tinker', ENROLLMENT)
        self.assertIn('Role: reviewer', card)
        self.assertIn('Take one task from your review queue at a time.', card)
        self.assertNotIn('Ask a question through `ask`', card)

    def test_reviewer_card_states_the_real_strike_rule(self):  # seat-truth, store.py PAPERWORK_STRIKE_AT
        for seat in ('tinker', 'astra-audit', 'vesper', 'crew-audit'):
            with self.subTest(seat=seat):
                card = self.join_cards.render_card(seat, ENROLLMENT)
                self.assertNotIn('it is a strike, and two stop', card)
                self.assertIn('paperwork', card)
                self.assertIn('free until the third', card)
                self.assertIn('substance', card)
                self.assertIn('two strikes stop the task', card)
                self.assertIn('refunds', card)

    def test_credential_file_is_never_read_even_when_one_exists_at_the_conventional_path(self):  # P17
        # join_cards must never look at a credential file, no matter what
        # `tree` is passed or what happens to exist under it. Build the exact
        # layout a real tree root has (tree/.runtime/handoffs/<seat>-credential.json)
        # with a real-looking secret in it, and confirm it never surfaces.
        tree = case_dir('join-card-credential-leak')
        handoffs = tree / '.runtime' / 'handoffs'
        handoffs.mkdir(parents=True)
        secret_token = 'super-secret-token-value-should-never-appear'
        (handoffs / 'local-a-credential.json').write_text(
            json.dumps({'token': secret_token}), encoding='utf-8')
        card = self.join_cards.render_card('local-a', ENROLLMENT, tree=tree)
        self.assertNotIn(secret_token, card)

    def test_launcher_default_seat_gets_no_add_to_seats_line(self):  # F18
        card = self.join_cards.render_card('claude-code', ENROLLMENT)
        self.assertNotIn('Shane must add this seat to SEATS in Start Common Room Crew.cmd', card)

    def test_seat_missing_from_launcher_default_seats_gets_the_add_to_seats_line(self):  # F18
        for seat in ('awl-0921', 'gauge-0921', 'mystery-seat'):
            card = self.join_cards.render_card(seat, ENROLLMENT)
            self.assertIn('Shane must add this seat to SEATS in Start Common Room Crew.cmd before it gets a key.',
                          card)

    def test_local_b_card_says_the_launcher_gives_it_no_key(self):  # seat-truth, D9
        card = self.join_cards.render_card('local-b', ENROLLMENT)
        self.assertIn('Shane must add this seat to SEATS in Start Common Room Crew.cmd before it gets a key.', card)

    def test_shell_route_card_offers_a_powershell_fallback_for_git_bash(self):  # F18
        card = self.join_cards.render_card('astra-room', ENROLLMENT)
        self.assertIn('Git Bash', card)
        self.assertIn('PowerShell', card)
        self.assertIn("& '" + self.join_cards.WINDOWS_TREE, card)

    def test_cowork_route_uses_auto_request_id_and_warns_against_reuse(self):  # F9
        card = self.join_cards.render_card('ferrule', ENROLLMENT)
        self.assertIn('"request_id":"auto"', card)
        self.assertNotIn('"request_id":"ack-1"', card)
        self.assertIn('Never reuse a request id another chat used; use "auto"', card)


SHELL_WORKERS = ('claude-code', 'claude-code-b', 'astra-room', 'awl-0921')
SHELL_REVIEWERS = ('astra-audit', 'gauge-0921')
COWORK_SEATS = ('claude-room', 'ferrule', 'tinker', 'vesper', 'crew-audit')
NOT_WAITING_CHATS = ('local-a', 'local-b', 'hermes', 'mystery-seat')
EVERY_SEAT = SHELL_WORKERS + SHELL_REVIEWERS + COWORK_SEATS + NOT_WAITING_CHATS
DOT = chr(0xB7)    # the middle dot of the window title, spelled by code so no editor can change it
SEAT_RULE = ('. Name this window "CR ' + DOT + ' {seat}". '
             'If this window already has another seat\'s name, stop and tell Shane.')
OLD_WAITING_TEXT = ('30 minutes', 'every few minutes', 'up to 30')


class JoinCardSeatNameTests(unittest.TestCase):
    """2026-10-01 (plan cr-upgrade-2026-10-01, crew-names): every card opens with the seat's name and the
    window title, and chats wait with `crew watch`, not a time-limited loop."""
    def setUp(self):
        self.join_cards = _import_join_cards()
        self.labels = {row['id']: row['label'] for row in ENROLLMENT['actors']}

    def card(self, seat, **kw):
        return self.join_cards.render_card(seat, ENROLLMENT, **kw)

    def test_line_one_of_every_card_is_the_seat_and_the_window_title(self):
        for seat in EVERY_SEAT:
            with self.subTest(seat=seat):
                first = self.card(seat).splitlines()[0]
                self.assertEqual(first, 'SEAT: ' + seat + ' (' + self.labels[seat] + ')' + SEAT_RULE.format(seat=seat))

    def test_a_seat_with_no_label_still_gets_the_line(self):
        enrollment = {'version': 1, 'actors': [{'id': 'lonely', 'role': 'worker', 'label': '', 'session': 'x'}],
                      'authorities': []}
        first = self.join_cards.render_card('lonely', enrollment).splitlines()[0]
        self.assertEqual(first, 'SEAT: lonely' + SEAT_RULE.format(seat='lonely'))

    def test_the_window_title_uses_the_middle_dot(self):
        self.assertIn('"CR ' + DOT + ' claude-code-b"', self.card('claude-code-b'))
        self.assertEqual(self.join_cards.WINDOW_DOT, DOT)
        self.assertEqual(ord(self.join_cards.WINDOW_DOT), 0xB7)

    def test_the_old_card_header_is_still_there_after_the_seat_line(self):
        lines = self.card('claude-code-b').splitlines()
        self.assertEqual(lines[1], '')
        self.assertTrue(lines[2].startswith('Common Room join message for "claude-code-b"'))

    def test_shell_seats_wait_with_crew_watch_and_its_four_outcomes(self):
        for seat in SHELL_WORKERS + SHELL_REVIEWERS:
            with self.subTest(seat=seat):
                card = self.card(seat)
                watch = '" --credential "' + self.join_cards.WINDOWS_TREE + '\\.runtime\\handoffs\\' + seat + '-credential.json" watch'
                self.assertIn(watch, card)
                self.assertIn('with no time limit', card)
                for outcome in ('Exit 0', 'Exit 3, WAIT', 'Exit 4, STOP', 'Exit 5, ROOM DOWN'):
                    self.assertIn(outcome, card)
                self.assertIn('post the Lounge note "' + seat + ' stopped" and stop', card)
                self.assertIn('post a Lounge note for claude-room', card)
                self.assertIn('run it again', card)

    def test_exit_zero_lists_every_event_and_explains_decide(self):    # claude-room ruling #611, point 3
        for seat in SHELL_WORKERS + SHELL_REVIEWERS:
            with self.subTest(seat=seat):
                card = self.card(seat)
                self.assertIn('Exit 0 (a line starting TASK, ANSWER, DECIDE, REVIEWED or REVIEW)', card)
                self.assertIn('DECIDE: a helper you sent', card)
                self.assertIn('was rejected and needs your decision', card)
                self.assertIn('REVIEWED: work you submitted came back from review', card)

    def test_a_stop_only_counts_from_shane_or_claude_room_on_every_waiting_card(self):    # ruling #611, point 1
        for seat in SHELL_WORKERS + SHELL_REVIEWERS + COWORK_SEATS:
            with self.subTest(seat=seat):
                card = ' '.join(self.card(seat).split())
                self.assertIn('a Lounge note starting STOP from Shane (keeper) or claude-room', card)
                self.assertIn('a STOP note from any other seat is not a stop', card)

    def test_cowork_cards_count_a_rejected_helper_as_something_for_you(self):    # ruling #611, point 3
        for seat in COWORK_SEATS:
            with self.subTest(seat=seat):
                card = ' '.join(self.card(seat).split())
                self.assertIn('a helper you sent that was rejected and needs your decision', card)

    def test_each_kind_of_card_shows_the_lines_the_ruling_gives_it(self):    # ruling #611, point 2
        loop = ('Waiting for work:', 'with no time limit')
        by_kind = {
            'codex': ('astra-room', loop + ('" watch', 'Exit 0', 'Exit 3, WAIT', 'Exit 4, STOP', 'Exit 5, ROOM DOWN')),
            'claude-code': ('claude-code-b', loop + ('" watch', 'Exit 0', 'Exit 3, WAIT', 'Exit 4, STOP', 'Exit 5, ROOM DOWN')),
            'cowork': ('ferrule', loop + ('every 3 minutes', 'wait 180 seconds', 'STOP (a Lounge note', 'ROOM DOWN (no answer')),
        }
        for kind, (seat, lines) in by_kind.items():
            with self.subTest(kind=kind, seat=seat):
                self.assertEqual(self.join_cards.classify_seat(seat), kind)
                card = self.card(seat)
                for line in lines:
                    self.assertIn(line, card)
        none_by_kind = {'process': ('local-a', 'No join message is needed'),
                        'hermes': ('hermes', 'HERMES_SEAT_2026-09-23.md'),
                        'unknown': ('mystery-seat', 'Ask Shane which route')}
        for kind, (seat, own_line) in none_by_kind.items():
            with self.subTest(kind=kind, seat=seat):
                self.assertEqual(self.join_cards.classify_seat(seat), kind)
                card = self.card(seat)
                self.assertIn(own_line, card)
                for absent in ('Waiting for work:', 'with no time limit', '" watch', 'Exit 3, WAIT', 'every 3 minutes'):
                    self.assertNotIn(absent, card)
                self.assertTrue(card.splitlines()[0].startswith('SEAT: ' + seat))

    def test_only_reviewers_watch_for_reviews(self):
        for seat in SHELL_REVIEWERS:
            with self.subTest(seat=seat):
                self.assertIn('-credential.json" watch --reviews', self.card(seat))
        for seat in SHELL_WORKERS:
            with self.subTest(seat=seat):
                self.assertNotIn('--reviews', self.card(seat))

    def test_the_waiting_note_goes_to_the_plan_scope_when_one_is_given(self):
        self.assertIn('post one Lounge note "claude-code-b waiting" in scope ws-crew', self.card('claude-code-b', scope='ws-crew'))
        self.assertIn('post one Lounge note "claude-code-b waiting" in scope orchestra', self.card('claude-code-b'))

    def test_no_card_keeps_the_old_time_limited_waiting_text(self):
        for seat in EVERY_SEAT:
            with self.subTest(seat=seat):
                card = self.card(seat).lower()
                for old in OLD_WAITING_TEXT:
                    self.assertNotIn(old, card)

    def test_cowork_seats_cannot_run_crew_so_they_wait_through_the_relay(self):
        for seat in COWORK_SEATS:
            with self.subTest(seat=seat):
                card = self.card(seat)
                self.assertNotIn('" watch', card)       # no crew.cmd line: a Cowork VM cannot reach the room
                self.assertIn('every 3 minutes', card)
                self.assertIn('no time limit', card)
                self.assertIn('wait 180 seconds', card)
                self.assertIn('STOP', card)
                self.assertIn('ROOM DOWN', card)
                self.assertIn('post the Lounge note "' + seat + ' stopped" and stop', card)
                self.assertIn('relay_ask.py', card)

    def test_a_cowork_reviewer_waits_for_its_review_queue(self):
        self.assertIn('a task in your review queue', self.card('tinker'))
        self.assertNotIn('a task in your review queue', self.card('ferrule'))

    def test_launcher_and_schedule_seats_get_no_waiting_loop(self):
        for seat in NOT_WAITING_CHATS:
            with self.subTest(seat=seat):
                card = self.card(seat)
                self.assertNotIn('Waiting for work:', card)
                self.assertNotIn('" watch', card)

    def test_house_rules_and_first_commands_are_unchanged(self):
        card = self.card('claude-code-b')
        rules = self.join_cards._worker_house_rules()
        self.assertEqual(len(rules), 7)
        for i, rule in enumerate(rules, 1):
            self.assertIn('\n' + str(i) + '. ' + rule, card)
        self.assertTrue(card.endswith('7. Never work a file another seat is already working.'))
        tree = self.join_cards.WINDOWS_TREE
        credential = tree + '\\.runtime\\handoffs\\claude-code-b-credential.json'
        self.assertIn('"' + tree + '\\crew.cmd" --credential "' + credential + '" whoami', card)
        self.assertIn('"' + tree + '\\crew.cmd" --credential "' + credential + '" tasks', card)
        self.assertLess(card.index('First commands:'), card.index('Waiting for work:'))
        self.assertLess(card.index('Waiting for work:'), card.index('House rules:'))

    def test_the_script_prints_the_same_first_line(self):
        tree = case_dir('join-card-seat-line')
        (tree / '.runtime' / 'handoffs').mkdir(parents=True)
        (tree / '.runtime' / 'handoffs' / 'enrollment.json').write_text(json.dumps(ENROLLMENT), encoding='utf-8')
        (tree / 'common_room').mkdir()
        (tree / 'common_room' / '__init__.py').write_text('', encoding='utf-8')
        shutil.copy(ROOT / 'common_room' / 'join_cards.py', tree / 'common_room' / 'join_cards.py')
        result = subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'crew' / 'join_card.py'), 'claude-code-b',
                                 '--tree', str(tree)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        first = result.stdout.decode('utf-8').splitlines()[0]
        self.assertEqual(first, 'SEAT: claude-code-b (Claude Code B)' + SEAT_RULE.format(seat='claude-code-b'))


class JoinCardTokenSafetyTests(unittest.TestCase):
    def test_no_real_room_token_ever_appears_in_any_card(self):
        join_cards = _import_join_cards()
        from common_room.activation import start_room
        config = {
            'version': 1,
            'actors': [
                {'id': 'local-a', 'role': 'worker', 'label': 'L', 'session': 'x'},
                {'id': 'astra-room', 'role': 'worker', 'label': 'A', 'session': 'x'},
                {'id': 'claude-room', 'role': 'worker', 'label': 'C', 'session': 'x'},
                {'id': 'tinker', 'role': 'reviewer', 'label': 'T', 'session': 'x'},
                {'id': 'hermes', 'role': 'worker', 'label': 'H', 'session': 'x'},
            ],
            'authorities': [
                {'id': 'orchestra', 'members': ['keeper', 'local-a', 'astra-room', 'claude-room', 'tinker', 'hermes'],
                 'scope': 'Fixture scope'},
            ],
        }
        seats = ['local-a', 'astra-room', 'claude-room', 'tinker', 'hermes']
        room = start_room(case_dir('join-card-room'), enrollment=config, selected=seats)
        self.addCleanup(room.close)
        enrollment = json.loads((room.root / 'enrollment.json').read_text(encoding='utf-8'))
        tokens = [json.loads((room.root / (a + '-credential.json')).read_text(encoding='utf-8'))['token']
                  for a in ['keeper'] + seats]
        cards = [join_cards.render_card(seat, enrollment, tree=room.root) for seat in seats]
        blob = '\n'.join(cards)
        for token in tokens:
            self.assertNotIn(token, blob)
        # shell seats DO mention the credential PATH, which is expected.
        self.assertIn('astra-room-credential.json', blob)


class JoinCardsModuleCompatibilityTests(unittest.TestCase):
    def test_compiles_under_python311(self):
        module_path = ROOT / 'common_room' / 'join_cards.py'
        try:
            result = subprocess.run(['python3.11', '-m', 'py_compile', str(module_path)],
                                     capture_output=True, text=True)
        except FileNotFoundError:
            self.skipTest('python3.11 is not available in this environment')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_compiles_under_python310(self):
        module_path = ROOT / 'common_room' / 'join_cards.py'
        try:
            result = subprocess.run(['python3.10', '-m', 'py_compile', str(module_path)],
                                     capture_output=True, text=True)
        except FileNotFoundError:
            self.skipTest('python3.10 is not available in this environment')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_39_plus_only_syntax(self):
        module_path = ROOT / 'common_room' / 'join_cards.py'
        source = module_path.read_text(encoding='utf-8')
        tree = ast.parse(source)
        match_node = getattr(ast, 'Match', None)
        for node in ast.walk(tree):
            if match_node is not None and isinstance(node, match_node):
                self.fail('join_cards.py must stay 3.8-compatible: no match statements.')
            annotation = None
            if isinstance(node, ast.AnnAssign):
                annotation = node.annotation
            elif isinstance(node, ast.arg):
                annotation = node.annotation
            if annotation is not None and isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
                self.fail('join_cards.py must stay 3.8-compatible: no `X | Y` annotations.')

    def test_no_relative_or_common_room_sibling_imports(self):
        module_path = ROOT / 'common_room' / 'join_cards.py'
        source = module_path.read_text(encoding='utf-8')
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.level, 1, 'join_cards.py must not import sibling common_room modules')
                if node.module:
                    self.assertFalse(node.module.startswith('common_room'),
                                      'join_cards.py must not import other common_room modules')
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(alias.name.startswith('common_room.'),
                                      'join_cards.py must not import other common_room modules')


class JoinCardScriptTests(unittest.TestCase):
    def _fabricate_tree(self, name):
        tree = case_dir(name)
        (tree / '.runtime' / 'handoffs').mkdir(parents=True)
        (tree / '.runtime' / 'handoffs' / 'enrollment.json').write_text(json.dumps(ENROLLMENT), encoding='utf-8')
        (tree / 'common_room').mkdir()
        (tree / 'common_room' / '__init__.py').write_text('', encoding='utf-8')
        shutil.copy(ROOT / 'common_room' / 'join_cards.py', tree / 'common_room' / 'join_cards.py')
        return tree

    def _run_script(self, args):
        script = ROOT / 'crew' / 'join_card.py'
        # The script says UTF-8 on its own (it prints a middle dot); decode strictly so a code-page leak fails here.
        return subprocess.run([sys.executable, str(script)] + args, capture_output=True, encoding='utf-8')

    def test_script_prints_card_for_a_known_seat_using_default_tree(self):
        tree = self._fabricate_tree('join-card-script-tree')
        result = self._run_script(['local-a', '--tree', str(tree)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('No join message is needed', result.stdout)

    def test_script_reports_missing_enrollment_clearly(self):
        tree = case_dir('join-card-script-tree-missing')
        (tree / 'common_room').mkdir()
        (tree / 'common_room' / '__init__.py').write_text('', encoding='utf-8')
        shutil.copy(ROOT / 'common_room' / 'join_cards.py', tree / 'common_room' / 'join_cards.py')
        result = self._run_script(['local-a', '--tree', str(tree)])
        self.assertEqual(result.returncode, 1)
        self.assertIn('enrollment.json', result.stderr)

    def test_script_reports_unenrolled_seat_clearly(self):
        tree = self._fabricate_tree('join-card-script-tree-unenrolled')
        result = self._run_script(['nobody-here', '--tree', str(tree)])
        self.assertEqual(result.returncode, 1)
        self.assertIn('not enrolled', result.stderr)

    def test_script_default_tree_is_folder_above_crew(self):
        # Run against the real repo tree, with the real room not yet started:
        # enrollment.json is absent there until a room runs, so this exercises
        # the default --tree resolution (folder above crew/) via a clear error
        # rather than requiring a live room.
        script = ROOT / 'crew' / 'join_card.py'
        result = subprocess.run([sys.executable, str(script), 'local-a'], capture_output=True, encoding='utf-8',
                                 cwd=str(ROOT))
        # Either it finds a real enrollment.json from another test/process and
        # succeeds, or it clearly reports the file is missing -- either way it
        # must not crash with an uncaught traceback.
        self.assertIn(result.returncode, (0, 1))
        self.assertNotIn('Traceback', result.stderr)


class JoinCardScriptEncodingTests(unittest.TestCase):
    """Review #678: on Windows a child's pipe defaulted to the console code page, so the card's middle dot came out
    as byte 0xB7 where the reader expected UTF-8. The script now always writes UTF-8, whatever the environment says."""
    def _tree(self):
        tree = case_dir('join-card-encoding-tree')
        (tree / '.runtime' / 'handoffs').mkdir(parents=True)
        (tree / '.runtime' / 'handoffs' / 'enrollment.json').write_text(json.dumps(ENROLLMENT), encoding='utf-8')
        (tree / 'common_room').mkdir()
        (tree / 'common_room' / '__init__.py').write_text('', encoding='utf-8')
        shutil.copy(ROOT / 'common_room' / 'join_cards.py', tree / 'common_room' / 'join_cards.py')
        return tree

    @staticmethod
    def _env(**extra):
        env = {k: v for k, v in os.environ.items() if k not in ('PYTHONUTF8', 'PYTHONIOENCODING')}
        env.update(extra)
        return env

    def _assert_card_is_utf8(self, stdout):
        self.assertEqual(stdout.count(b'\xb7'), stdout.count(b'\xc2\xb7'), 'a lone 0xB7 byte means a code page leaked')
        first = stdout.decode('utf-8').splitlines()[0]
        self.assertEqual(first, 'SEAT: claude-code-b (Claude Code B). Name this window "CR ' + chr(0xB7) +
                         ' claude-code-b". If this window already has another seat\'s name, stop and tell Shane.')

    def test_the_card_is_utf8_whatever_code_page_the_environment_names(self):
        script = ROOT / 'crew' / 'join_card.py'
        tree = self._tree()
        for encoding in ('cp1252', 'cp437', 'ascii', 'latin-1'):
            with self.subTest(PYTHONIOENCODING=encoding):
                done = subprocess.run([sys.executable, str(script), 'claude-code-b', '--tree', str(tree)],
                                      capture_output=True, env=self._env(PYTHONIOENCODING=encoding, PYTHONUTF8='0'))
                self.assertEqual(done.returncode, 0, done.stderr)
                self._assert_card_is_utf8(done.stdout)

    def test_the_card_is_utf8_with_no_encoding_variables_at_all(self):
        done = subprocess.run([sys.executable, str(ROOT / 'crew' / 'join_card.py'), 'claude-code-b', '--tree',
                               str(self._tree())], capture_output=True, env=self._env())
        self.assertEqual(done.returncode, 0, done.stderr)
        self._assert_card_is_utf8(done.stdout)

    @unittest.skipUnless(os.name == 'nt', 'the official launcher is a Windows .cmd')
    def test_the_card_is_utf8_through_the_official_windows_launcher(self):
        # crew\\windows-tests.cmd finds its Python with crew\\find-python.cmd and runs it as `-X utf8 -B`: the same
        # chain here, in a clean environment, reading the output as strict UTF-8 like the reviewer's parent process.
        tree = self._tree()
        launcher = case_dir('join-card-launcher') / 'run_card.cmd'
        launcher.write_text('@echo off\r\ncall "' + str(ROOT / 'crew' / 'find-python.cmd') + '" || exit /b 1\r\n'
                            '"%PY%" -X utf8 -B "' + str(ROOT / 'crew' / 'join_card.py') + '" claude-code-b --tree "' +
                            str(tree) + '"\r\n', encoding='ascii')
        done = subprocess.run(['cmd', '/c', str(launcher)], capture_output=True, env=self._env())
        self.assertEqual(done.returncode, 0, done.stderr)
        self._assert_card_is_utf8(done.stdout)


if __name__ == '__main__':
    unittest.main(argv=sys.argv[:1])    # evidence.py passes the tree root as an argument; it names no test
