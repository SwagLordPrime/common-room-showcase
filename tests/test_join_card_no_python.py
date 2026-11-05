"""seat-dot (plan turnons-2026-10-03, 2026-10-05): a relay seat whose chat may not run Python gets a join
card that writes the relay's request files by hand, from the shapes in the relay's own README.md. The
Python helper card stays exactly as it was for the Cowork seats, and each card renders both ways."""
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))      # lets `python tests/test_join_card_no_python.py` run from a tree root (tools/evidence.py does)

from tests.helpers import case_dir
from tests.test_join_cards import ENROLLMENT as BASE_ENROLLMENT

ENROLLMENT = dict(BASE_ENROLLMENT,
                  actors=BASE_ENROLLMENT['actors'] + [{'id': 'dot', 'role': 'worker', 'label': 'ChatGPT dot', 'session': 'x'}],
                  authorities=[dict(a, members=a['members'] + ['dot']) for a in BASE_ENROLLMENT['authorities']])
COWORK_SEATS = ('claude-room', 'ferrule', 'tinker', 'vesper', 'crew-audit')
HAND_LINES = ('relay-state.json', '.tmp', 'resp-<request_id>.json', 'README.md', 'within 90 seconds',
              'under a NEW file name', 'every 3 minutes', 'no time limit', 'wait 180 seconds')


def _join_cards():
    import common_room.join_cards as join_cards
    return join_cards


def _shapes(card):
    """Every request shape printed on a card, parsed as JSON (a shape that is not valid JSON fails here)."""
    return [json.loads(line.strip()) for line in card.splitlines() if line.strip().startswith('{"request_id"')]


class HandWrittenRelayCardTests(unittest.TestCase):
    def setUp(self):
        self.join_cards = _join_cards()

    def card(self, seat, **kw):
        return self.join_cards.render_card(seat, ENROLLMENT, **kw)

    def test_every_cowork_seat_card_renders_both_ways(self):
        for seat in COWORK_SEATS:
            with self.subTest(seat=seat):
                helper = self.card(seat)
                self.assertEqual(helper, self.card(seat, python=True))        # the default is unchanged
                self.assertIn('relay_ask.py', helper)
                hand = self.card(seat, python=False)
                self.assertNotIn('relay_ask.py', hand)
                self.assertNotIn('python3', hand)
                for line in HAND_LINES:
                    self.assertIn(line, hand)
                self.assertIn(self.join_cards.COWORK_MOUNT + '/.runtime/handoffs/relay/' + seat, hand)
                self.assertIn('post the Lounge note "' + seat + ' stopped" and stop', hand)
                flat = ' '.join(hand.split())
                self.assertIn('a Lounge note starting STOP from Shane (keeper) or claude-room', flat)
                self.assertIn('a STOP note from any other seat is not a stop', flat)

    def test_the_shapes_are_valid_json_in_the_relay_readme_form(self):
        for seat in COWORK_SEATS + ('dot',):
            with self.subTest(seat=seat):
                shapes = _shapes(self.card(seat, python=False))
                self.assertGreaterEqual(len(shapes), 4)
                kinds = {(s['kind'], s.get('action')) for s in shapes}
                self.assertEqual(kinds, {('command', 'ack'), ('command', 'claim'), ('command', 'note'), ('state', None)})
                for s in shapes:
                    self.assertEqual(set(s), {'request_id', 'kind', 'action', 'payload'} if s['kind'] == 'command' else {'request_id', 'kind'})

    def test_the_shapes_match_what_the_relay_and_room_accept(self):
        from common_room.relay import COMMAND_ACTIONS, REQUEST_ID
        from common_room.store import FIELDS
        hand = self.card('dot', python=False)
        for s in _shapes(hand):
            if s['kind'] == 'command':
                self.assertIn(s['action'], COMMAND_ACTIONS)
                self.assertEqual(set(s['payload']), FIELDS[s['action']])
        named = re.search(r'Every other action \((.*?)\) and', ' '.join(hand.split())).group(1)
        listed = {x.strip() for x in named.split(',')} | {'ack', 'claim', 'note'}
        self.assertEqual(listed, COMMAND_ACTIONS)                              # every action the relay passes on, no more
        example = re.search(r'for example (\S+), and name', ' '.join(hand.split())).group(1)
        self.assertTrue(REQUEST_ID.fullmatch(example), example)

    def test_the_dot_card_writes_files_by_hand_on_shanes_own_disk(self):
        self.assertEqual(self.join_cards.classify_seat('dot'), 'files')
        card = self.card('dot')
        self.assertEqual(card, self.card('dot', python=False))                 # by hand is the dot's default
        self.assertIn(self.join_cards.WINDOWS_TREE + '\\.runtime\\handoffs\\relay\\dot', card)
        self.assertNotIn('$HOME/mnt', card)
        self.assertNotIn('relay_ask.py', card)
        self.assertNotIn('" watch', card)                                      # no crew.cmd route
        for line in HAND_LINES:
            self.assertIn(line, card)
        self.assertIn('requests\\ folder', card)
        self.assertIn('never open any *-credential.json, keeper file', card)
        self.assertIn('Read first: CREW_SEAT_GUIDE_2026-09-23.md and then ORCHESTRA_2026-09-24.md and then '
                      'DOT_SEAT_2026-10-05.md.', card)
        self.assertTrue(card.splitlines()[0].startswith('SEAT: dot (ChatGPT dot). Name this window'))

    def test_the_dot_card_with_python_uses_the_helper_through_the_py_launcher(self):
        card = self.card('dot', python=True)
        helper = self.join_cards.WINDOWS_TREE + '\\crew\\relay_ask.py'
        self.assertIn('py -3 "' + helper + '" "' + self.join_cards.WINDOWS_TREE + '\\.runtime\\handoffs\\relay\\dot" state', card)
        self.assertIn('"request_id": "auto"', card)
        self.assertNotIn('$HOME/mnt', card)

    def test_non_relay_seats_ignore_the_flag(self):
        for seat in ('claude-code-b', 'astra-audit', 'local-a', 'hermes', 'mystery-seat'):
            with self.subTest(seat=seat):
                self.assertEqual(self.card(seat, python=False), self.card(seat))
                self.assertEqual(self.card(seat, python=True), self.card(seat))

    def test_the_note_shape_names_the_plan_scope(self):
        self.assertIn('"authority_id":"ws-crew"', self.card('ferrule', python=False, scope='ws-crew'))
        self.assertIn('"authority_id":"orchestra"', self.card('ferrule', python=False))

    def test_no_room_token_on_a_hand_card(self):
        tree = case_dir('join-card-no-python-token')
        token = 'TOKEN-' + 'x' * 40
        (tree / 'dot-credential.json').write_text(json.dumps({'token': token}), encoding='utf-8')
        for seat in COWORK_SEATS + ('dot',):
            with self.subTest(seat=seat):
                self.assertNotIn(token, self.join_cards.render_card(seat, ENROLLMENT, tree=tree, python=False))


class HandWrittenCardScriptTests(unittest.TestCase):
    def _tree(self):
        import shutil
        tree = case_dir('join-card-no-python-script')
        (tree / '.runtime' / 'handoffs').mkdir(parents=True)
        (tree / '.runtime' / 'handoffs' / 'enrollment.json').write_text(json.dumps(ENROLLMENT), encoding='utf-8')
        (tree / 'common_room').mkdir()
        (tree / 'common_room' / '__init__.py').write_text('', encoding='utf-8')
        shutil.copy(ROOT / 'common_room' / 'join_cards.py', tree / 'common_room' / 'join_cards.py')
        return tree

    def _run(self, args):
        return subprocess.run([sys.executable, str(ROOT / 'crew' / 'join_card.py')] + args, capture_output=True,
                              encoding='utf-8')

    def test_the_script_prints_both_ways(self):
        tree = str(self._tree())
        hand = self._run(['ferrule', '--tree', tree, '--no-python'])
        self.assertEqual(hand.returncode, 0, hand.stderr)
        self.assertIn('relay-state.json', hand.stdout)
        self.assertNotIn('relay_ask.py', hand.stdout)
        helper = self._run(['dot', '--tree', tree, '--python'])
        self.assertEqual(helper.returncode, 0, helper.stderr)
        self.assertIn('relay_ask.py', helper.stdout)
        plain = self._run(['dot', '--tree', tree])
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertIn('relay-state.json', plain.stdout)
        self.assertNotIn('relay_ask.py', plain.stdout)

    def test_the_two_flags_cannot_be_given_together(self):
        done = self._run(['ferrule', '--tree', str(self._tree()), '--python', '--no-python'])
        self.assertEqual(done.returncode, 2)
        self.assertIn('not allowed with argument', done.stderr)


if __name__ == '__main__':
    unittest.main(argv=sys.argv[:1])    # evidence.py passes the tree root as an argument; it names no test
