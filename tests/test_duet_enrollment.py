"""The duet seat's enrollment and launcher (Claude Duet over the Drive-synced DuetRelay folder, 2026-10-30).

Duet is a worker in the `orchestra` scope and nothing else. The shipped enrollment only gained it at the end of
the actors and at the end of the orchestra members (append-only: the room refuses anything else), the launcher
gives it a key but no relay folder, and nothing gives it a review power: the store refuses a review by a worker,
the stand-in file does not name it, and its relay refuses `review` before sending. Fictional rooms in .checks only;
keys made here; no keeper vault; no real relay or credential file."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # lets `python tests/test_duet_enrollment.py` run from a tree root

import hashlib, json, re, subprocess, unittest

from common_room.handoff_api import handoff_operation
from common_room.protocol import RoomError
from tests.helpers import case_dir

TREE = Path(__file__).resolve().parents[1]
SHIPPED = json.loads((TREE / 'crew' / 'enrollment-crew-2026-09-24.json').read_text(encoding='utf-8'))
BASE_COMMIT = '5d5ee83'    # the switch-2 head this seat was cut from

# The enrollment as 5d5ee83 had it (VERIFIED IN LOCAL SOURCE: git show 5d5ee83:crew/enrollment-crew-2026-09-24.json).
OLD_ACTORS = ['astra-room', 'astra-audit', 'claude-room', 'gym-task', 'librarian-task', 'ferrule', 'tinker', 'vesper',
              'crew-audit', 'awl-0921', 'gauge-0921', 'hermes', 'local-a', 'local-b', 'claude-code', 'claude-code-b',
              'dot', 'notify']
OLD_ORCHESTRA = ['keeper', 'claude-room', 'astra-room', 'ferrule', 'claude-code', 'claude-code-b', 'hermes', 'local-a',
                 'local-b', 'tinker', 'vesper', 'crew-audit', 'astra-audit', 'dot', 'notify']
# sha256 of the orchestra scope text at 5d5ee83 (MEASURED 2026-10-30 from git show).
OLD_ORCHESTRA_SCOPE_SHA256 = '5901a70f861765e54e2bda26302480b5fcaebc0fa1cd963ea8b20d681cae572a'
LAUNCHER_SEATS = ['claude-room', 'ferrule', 'tinker', 'vesper', 'crew-audit', 'hermes', 'local-a', 'astra-room',
                  'astra-audit', 'claude-code', 'claude-code-b', 'dot', 'notify', 'duet']   # local-b: out of the run (D9)


def actors():
    return {a['id']: a for a in SHIPPED['actors']}


def scopes():
    return {a['id']: a for a in SHIPPED['authorities']}


def git_old_enrollment():
    """The 5d5ee83 file through git, or None when this folder cannot give it (a bundle without that commit)."""
    try:
        done = subprocess.run(['git', 'show', BASE_COMMIT + ':crew/enrollment-crew-2026-09-24.json'], cwd=str(TREE),
                              capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return json.loads(done.stdout.decode('utf-8'))


class ShippedEnrollmentTests(unittest.TestCase):
    def test_duet_is_a_worker_appended_last_and_a_member_of_orchestra_only(self):
        rows = [a for a in SHIPPED['actors'] if a['id'] == 'duet']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['role'], 'worker')
        self.assertEqual(set(rows[0]), {'id', 'label', 'role', 'session'})
        self.assertEqual(SHIPPED['actors'][-1]['id'], 'duet')
        self.assertEqual([name for name, scope in scopes().items() if 'duet' in scope['members']], ['orchestra'])
        self.assertEqual(scopes()['orchestra']['members'][-1], 'duet')

    def test_the_change_is_append_only_against_the_base_commit(self):
        self.assertEqual([a['id'] for a in SHIPPED['actors']][:-1], OLD_ACTORS)
        self.assertEqual(scopes()['orchestra']['members'], OLD_ORCHESTRA + ['duet'])
        self.assertEqual([a['id'] for a in SHIPPED['authorities']], [
            'activation', 'common-room-setup', 'gym-build', 'librarian-coordination', 'crew-coordination',
            'attended-0921', 'local-crew', 'orchestra'])
        for name, scope in scopes().items():
            if name != 'orchestra':
                self.assertNotIn('duet', scope['members'], name)

    def test_the_orchestra_scope_text_is_untouched(self):
        # A scope's words are part of the grant: the room accepts a new member only when the text is unchanged.
        text = scopes()['orchestra']['scope']
        self.assertNotIn('duet', text)
        self.assertEqual(hashlib.sha256(text.encode('utf-8')).hexdigest(), OLD_ORCHESTRA_SCOPE_SHA256)

    @unittest.skipIf(git_old_enrollment() is None, 'git cannot give %s here' % BASE_COMMIT)
    def test_every_old_row_is_byte_identical_to_the_base_commit_through_git(self):
        old = git_old_enrollment()
        self.assertEqual(SHIPPED['actors'][:len(old['actors'])], old['actors'])
        self.assertEqual(SHIPPED['actors'][len(old['actors']):], [actors()['duet']])
        for before, after in zip(old['authorities'], SHIPPED['authorities']):
            if before['id'] == 'orchestra':
                self.assertEqual(after['members'], before['members'] + ['duet'])
                self.assertEqual({k: v for k, v in after.items() if k != 'members'},
                                 {k: v for k, v in before.items() if k != 'members'})
            else:
                self.assertEqual(after, before)
        self.assertEqual(len(old['authorities']), len(SHIPPED['authorities']))
        self.assertEqual(old['version'], SHIPPED['version'])

    def test_orchestra_keeps_both_named_reviewers_and_no_duet_reviewer(self):
        members = scopes()['orchestra']['members']
        for name in ('vesper', 'astra-audit'):
            self.assertIn(name, members)
            self.assertEqual(actors()[name]['role'], 'reviewer')
        for name, scope in scopes().items():
            if 'duet' in scope['members']:
                reviewers = [m for m in scope['members'] if m != 'duet' and actors().get(m, {}).get('role') == 'reviewer']
                self.assertGreaterEqual(len(reviewers), 1, name)
        self.assertNotEqual(actors()['duet']['role'], 'reviewer')

    def test_the_stand_in_file_never_names_duet(self):
        text = (TREE / 'crew' / 'review-standins.json').read_text(encoding='utf-8')
        self.assertNotIn('duet', text)
        for row in json.loads(text)['review_standins']:
            for key in ('actor', 'stands_for', 'authority'):
                self.assertNotEqual(row[key], 'duet')
            self.assertNotIn('duet', row['builders'])
            self.assertNotIn('duet', row['markers'])

    def test_the_enrollment_check_accepts_the_new_seat_and_the_old_room_takes_it_at_its_next_start(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('enroll_check', str(TREE / 'crew' / 'enroll_check.py'))
        enroll_check = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(enroll_check)
        old = {'version': 1, 'actors': [a for a in SHIPPED['actors'] if a['id'] != 'duet'],
               'authorities': [dict(s, members=[m for m in s['members'] if m != 'duet']) for s in SHIPPED['authorities']]}
        code, line = enroll_check.decide(old, SHIPPED, LAUNCHER_SEATS)
        self.assertEqual(code, 3)
        self.assertIn('duet', line)
        self.assertEqual(enroll_check.decide(SHIPPED, SHIPPED, LAUNCHER_SEATS)[0], 0)


class LauncherTests(unittest.TestCase):
    """Start Common Room Crew.cmd, read and never run."""
    def setUp(self):
        self.raw = (TREE / 'Start Common Room Crew.cmd').read_bytes()
        self.text = self.raw.decode('ascii')

    def seat_list(self, name):
        found = re.findall(r'^set "' + name + r'=([^"]*)"\s*$', self.text, re.M)
        self.assertEqual(len(found), 1, found)
        return found[0].split()

    def test_duet_gets_a_key_and_no_relay_folder(self):
        self.assertEqual(self.seat_list('SEATS'), LAUNCHER_SEATS)
        self.assertEqual(self.seat_list('SEATS')[-1], 'duet')
        self.assertNotIn('duet', self.seat_list('RELAY_SEATS'))
        self.assertEqual(self.seat_list('RELAY_SEATS'), ['claude-room', 'ferrule', 'tinker', 'vesper', 'crew-audit', 'dot'])

    def test_the_duet_window_starts_only_while_crew_duet_json_exists(self):
        starts = [line for line in self.text.split('\r\n') if 'CRCREW duet' in line]
        self.assertEqual(len(starts), 1, starts)
        self.assertEqual(starts[0], 'if exist "crew\\duet.json" start "CRCREW duet" /min "%PY%" -X utf8 -B -m '
                                    'common_room.keepalive --name duet -- -X utf8 -B -m common_room.duet_relay '
                                    '--config crew\\duet.json')
        lines = self.text.split('\r\n')
        self.assertTrue(lines[lines.index(starts[0]) - 1].startswith('rem Claude Duet over Google Drive (seat duet)'))
        self.assertGreater(lines.index(starts[0]), lines.index('exit /b 1', lines.index(':roomup') - 1))   # after :roomup

    def test_the_file_keeps_crlf_and_ascii(self):
        self.assertTrue(all(b < 128 for b in self.raw))
        self.assertEqual(self.raw.count(b'\n'), self.raw.count(b'\r\n'))
        self.assertEqual(self.raw.count(b'\r'), self.raw.count(b'\r\n'))
        self.assertTrue(self.raw.endswith(b'\r\n'))

    def test_keepalive_takes_the_name_duet(self):
        from common_room import keepalive
        args = keepalive.build_parser().parse_args(['--name', 'duet'])
        self.assertEqual(args.name, 'duet')

    def test_every_launched_seat_is_enrolled(self):
        self.assertEqual([s for s in self.seat_list('SEATS') if s not in actors()], [])

    def test_the_join_cards_know_duet_as_a_launcher_seat_and_give_it_no_relay_kind(self):
        from common_room import join_cards
        self.assertIn('duet', join_cards.LAUNCHER_DEFAULT_SEATS)
        self.assertEqual(join_cards.LAUNCHER_DEFAULT_SEATS, frozenset(self.seat_list('SEATS')))
        # duet reaches the PC through DuetRelay, not through a seat folder under .runtime\handoffs\relay.
        self.assertNotIn('duet', join_cards.SEAT_KIND_EXACT)


class ConfigFileTests(unittest.TestCase):
    def test_the_tree_ships_the_example_and_not_the_switch(self):
        """The relay window starts only while crew\\duet.json exists; the tree ships the example, so the switch-on is
        Shane copying it (deleting the copy is the off switch)."""
        self.assertFalse((TREE / 'crew' / 'duet.json').exists())
        raw = (TREE / 'crew' / 'duet.example.json').read_text(encoding='utf-8')
        self.assertTrue(raw.endswith('\n'))
        config = json.loads(raw)
        self.assertEqual(config, {'folder': 'D:\\Work Area\\DuetRelay', 'mirror_every': 300})
        self.assertEqual(list(config), ['folder', 'mirror_every'])
        from common_room import duet_relay
        self.assertEqual(duet_relay.load_config(TREE / 'crew' / 'duet.example.json'),
                         ('D:\\Work Area\\DuetRelay', 300.0, None))


class StoreTests(unittest.TestCase):
    """A fictional room started from the SHIPPED enrollment: the store, not the text, is what refuses."""
    def setUp(self):
        from common_room.activation import start_room
        self.room = start_room(case_dir('duet-enrollment'), enrollment=SHIPPED,
                               selected=[s for s in LAUNCHER_SEATS if s != 'notify'] + ['notify'])
        self.addCleanup(self.room.close)
        self.store = self.room.store
        self.n = 0

    def who(self, actor):
        return self.store.people[actor]

    def run_(self, actor, action, payload):
        self.n += 1
        return self.store.execute(self.who(actor), action, payload, 'duet-%d' % self.n)

    def intake(self, to):
        self.n += 1
        payload = {'source_namespace': 'estate', 'external_reference': 'ref-%d' % self.n,
                   'message': {'title': 'Fictional task', 'body': 'Do the fictional thing.', 'recipient': to,
                               'authority_id': 'orchestra', 'kind': 'handoff.record', 'acceptance': 'A fictional check.'}}
        tid = self.run_('keeper', 'intake', payload)['task']['id']
        self.run_('keeper', 'deliver', {'task_id': tid})
        return tid

    def build(self, tid, builder):
        self.run_(builder, 'ack', {'task_id': tid})
        fence = self.run_(builder, 'claim', {'task_id': tid, 'seconds': 1800})['task']['lease']['fence']
        ref = handoff_operation(self.room.server, self.who(builder), 'stage_text',
                                {'task_id': tid, 'fence': fence, 'text': 'Fictional result.'})
        self.run_(builder, 'finish', {'task_id': tid, 'fence': fence, 'body': 'Done.',
                                      'refs': [{'path': ref['path'], 'sha256': ref['sha256']}]})
        return tid

    def status(self, tid):
        return self.store.task(self.who('keeper'), tid)['status']

    def test_duet_is_a_registered_worker(self):
        self.assertEqual(self.who('duet').role, 'worker')
        members = self.store._snapshot(self.who('keeper'))[1]['orchestra']['members']
        self.assertEqual(members, OLD_ORCHESTRA + ['duet'])

    def test_duet_can_be_the_recipient_and_do_the_work(self):
        tid = self.build(self.intake('duet'), 'duet')
        self.assertEqual(self.status(tid), 'ready_for_review')

    def test_duet_cannot_review_an_orchestra_task(self):
        tid = self.build(self.intake('claude-code'), 'claude-code')
        before = self.status(tid)
        for accepted in (True, False):
            with self.subTest(accepted=accepted):
                with self.assertRaises(RoomError) as caught:
                    self.run_('duet', 'review', {'task_id': tid, 'accepted': accepted, 'note': 'A fictional review.'})
                self.assertEqual(caught.exception.code, 'forbidden')     # a worker has no review right
                self.assertEqual(self.status(tid), before)
        # A named reviewer who did not build it still can: the seat is not the room's only way to a review.
        done = self.run_('vesper', 'review', {'task_id': tid, 'accepted': True, 'note': 'A fictional review.'})['task']
        self.assertEqual((done['status'], done['reviewer']), ('done', 'vesper'))

    def test_a_stand_in_file_naming_duet_is_refused_before_the_store_opens(self):
        """M2 (plan row 2): NEVER_REVIEW. A later review-standins file cannot give duet a review power."""
        from common_room import activation, store
        self.assertEqual(store.NEVER_REVIEW, frozenset({'duet'}))
        people = self.store.people
        good = {'actor': 'claude-code-b', 'stands_for': 'vesper', 'authority': 'orchestra', 'builders': ['claude-code'],
                'markers': []}
        self.assertEqual(len(activation.check_standins([good], SHIPPED, people)), 1)    # the control: this shape is fine
        for field in ('actor', 'stands_for'):
            with self.subTest(field=field):
                entry = dict(good, **{field: 'duet'})
                with self.assertRaises(RoomError) as caught:
                    store.review_standins([entry], people)
                self.assertEqual(caught.exception.code, 'config')
                self.assertIn('never reviews', str(caught.exception))
                with self.assertRaises(ValueError) as refused:
                    activation.check_standins([entry], SHIPPED, people)
                self.assertIn('never reviews', str(refused.exception))
                with self.assertRaises(ValueError) as at_start:
                    activation.start_room(case_dir('duet-standin'), enrollment=SHIPPED, selected=['duet', 'vesper'],
                                          review_standins=[entry])
                self.assertIn('never reviews', str(at_start.exception))

    def test_duet_cannot_review_its_own_work(self):
        tid = self.build(self.intake('duet'), 'duet')
        with self.assertRaises(RoomError) as caught:
            self.run_('duet', 'review', {'task_id': tid, 'accepted': True, 'note': 'A fictional review.'})
        self.assertEqual(caught.exception.code, 'self_review')
        self.assertEqual(self.status(tid), 'ready_for_review')
        done = self.run_('astra-audit', 'review', {'task_id': tid, 'accepted': True, 'note': 'A fictional review.'})['task']
        self.assertEqual((done['status'], done['reviewer']), ('done', 'astra-audit'))


if __name__ == '__main__':
    unittest.main(argv=sys.argv[:1])
