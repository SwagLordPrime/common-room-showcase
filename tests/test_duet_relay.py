"""The duet relay (plan duet-seat-2026-10-30): Claude Duet reaches the room through a Drive-synced folder.

The room is faked (FakeClient stands in for CrewClient), as the question here is the folder: layout, Drive lag
(half-written files, duplicate names, reused ids), temp files kept out of the synced folder, the mirror, the lock,
and the refusals (review, owner actions, a task addressed to someone else). Fictional fixtures in .checks only.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # lets `python tests/test_duet_relay.py` run from a tree root

import contextlib, hashlib, io, json, os, shutil, unittest
from unittest import mock

from tests.helpers import case_dir, symlinks_or_skip
from common_room.crew_client import CrewError
from common_room import duet_relay
from common_room.duet_relay import DuetRelay, DuetRelayLock, SETTLE_SECONDS

OWN_TOKEN = 'DuetOwnTokenAbCdEfGhIjKlMnOpQrStUvWxYz0123456'   # 44 characters, fictional
TASK_MINE = 't' + 'a1' * 16
TASK_OTHER = 't' + 'b2' * 16
TASK_FROM_ME = 't' + 'd4' * 16       # sender duet, recipient someone else: duet may read it, not act on it


def make_junction(link, target):
    """A Windows directory junction at `link` pointing at `target` (no admin rights needed), else skip the test."""
    try:
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    except (ImportError, AttributeError, OSError) as exc:
        raise unittest.SkipTest('this host cannot make directory junctions: ' + type(exc).__name__)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeClient:
    """Stands in for CrewClient: records what would reach the room."""

    def __init__(self):
        self.calls = []                  # (kind, name, payload, request_id)
        self.state_calls = 0
        self.command_error = None
        self.command_result = None
        self.handoff_result = None
        self.state_body = {
            'server_time': 1000.0, 'actor': {'id': 'duet', 'role': 'worker'}, 'stop': {'active': False},
            'tasks': [{'id': TASK_MINE, 'title': 'For duet', 'status': 'delivered', 'sender': 'claude-room',
                       'recipient': 'duet', 'builder': None, 'parent_id': None, 'authority_id': 'orchestra',
                       'failed_attempts': 0, 'lease': None, 'body': 'PRIVATE BODY'},
                      {'id': TASK_OTHER, 'title': 'For someone else', 'status': 'delivered', 'sender': 'claude-room',
                       'recipient': 'vesper', 'builder': None, 'parent_id': None, 'authority_id': 'orchestra',
                       'failed_attempts': 0, 'lease': None},
                      {'id': TASK_FROM_ME, 'title': 'Asked by duet', 'status': 'delivered', 'sender': 'duet',
                       'recipient': 'vesper', 'builder': None, 'parent_id': None, 'authority_id': 'orchestra',
                       'failed_attempts': 0, 'lease': None}],
            'notes': [], 'questions': [], 'asked': [], 'needs': [],
            'participants': [{'id': 'duet', 'role': 'worker', 'contact_age_s': 4}]}

    def state(self):
        self.state_calls += 1
        return json.loads(json.dumps(self.state_body))

    def command(self, action, payload, request_id):
        self.calls.append(('command', action, payload, request_id))
        if self.command_error is not None:
            raise self.command_error
        if self.command_result is not None:
            return self.command_result
        return {'task': {'id': payload.get('task_id'), 'status': 'acknowledged'}}

    def handoff(self, operation, payload):
        self.calls.append(('handoff', operation, payload, None))
        return self.handoff_result if self.handoff_result is not None else {'ok': True}

    def actions(self):
        return [c[1] for c in self.calls]


class DuetTestCase(unittest.TestCase):
    def make(self, mirror_every=300.0, token=OWN_TOKEN):
        self.root = case_dir('duet-root')
        self.drive = case_dir('duet-drive') / 'DuetRelay'
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(shutil.rmtree, self.drive.parent, True)
        self.write_credential(token)
        self.clock = Clock()
        self.logs = []
        self.created = []
        self.relay = self.start_relay(mirror_every)
        self.client = FakeClient()
        self.relay.clients['duet'] = self.client
        return self.relay

    def write_credential(self, token):
        (self.root / 'duet-credential.json').write_text(json.dumps(
            {'origin': 'http://127.0.0.1:50263', 'token': token, 'actor_id': 'duet', 'expires_at': 99999999999}),
            encoding='utf-8')

    def start_relay(self, mirror_every=300.0):
        real_open = os.open

        def watching_open(path, flags, *args, **kwargs):
            if flags & os.O_CREAT:
                self.created.append(str(path))
            return real_open(path, flags, *args, **kwargs)
        with mock.patch('os.open', watching_open):
            return DuetRelay(self.root, self.drive, clock=self.clock, mirror_every=mirror_every)

    def cycle(self):
        """One process_once; returns the relay's log lines. Every file creation is recorded in self.created."""
        real_open = os.open

        def watching_open(path, flags, *args, **kwargs):
            if flags & os.O_CREAT:
                self.created.append(str(path))
            return real_open(path, flags, *args, **kwargs)
        out = io.StringIO()
        with mock.patch('os.open', watching_open), contextlib.redirect_stdout(out):
            self.relay.process_once()
        lines = [json.loads(line) for line in out.getvalue().splitlines() if line.startswith('{')]
        self.logs.extend(lines)
        return lines

    def request(self, name, body):
        data = body if isinstance(body, bytes) else json.dumps(body).encode('utf-8')
        path = self.drive / 'requests' / name
        path.write_bytes(data)
        return path

    def response_path(self, request_id):
        return self.drive / 'responses' / ('resp-' + request_id + '.json')

    def response(self, request_id):
        return json.loads(self.response_path(request_id).read_text(encoding='utf-8'))

    def responses(self):
        return sorted(p.name for p in (self.drive / 'responses').iterdir())

    def note_request(self, request_id='note-1', body='hello'):
        return {'request_id': request_id, 'kind': 'command', 'action': 'note',
                'payload': {'authority_id': 'orchestra', 'body': body}}

    def assert_nothing_created_in_drive(self):
        inside = [p for p in self.created if str(self.drive.parent) in p]
        self.assertEqual(inside, [], 'a file was created inside the synced folder tree by os.open')


class LayoutTests(DuetTestCase):
    def test_layout_has_no_archive_no_relay_state_and_nothing_under_root_relay(self):
        self.make()
        self.assertEqual(sorted(p.name for p in self.drive.iterdir()), ['README.md', 'mirror', 'requests', 'responses'])
        self.cycle()
        names = {p.name for p in self.drive.rglob('*')}
        self.assertNotIn('relay-state.json', names)
        self.assertNotIn('archive', names)
        self.assertFalse((self.root / 'relay').exists())
        self.assertFalse((self.root / 'relay' / 'duet').exists())
        self.assertEqual(sorted(p.name for p in (self.drive / 'mirror').iterdir()), ['board.json'])
        self.assertEqual(list(p.name for p in (self.root / 'duet-tmp').iterdir()), [], 'temp files do not linger')

    def test_readme_is_written_at_start_and_survives_the_filter_unchanged(self):
        from common_room.duet_mirror import scrub, guard
        self.make()
        text = (self.drive / 'README.md').read_text(encoding='utf-8')
        self.assertEqual(text, duet_relay.DUET_README_TEMPLATE)
        self.assertEqual(scrub(text), text)
        guard(text.encode('utf-8'))
        for needle in ('requests/', 'resp-<request_id>.json', 'resp-invalid-', 'mirror/board.json', '150', '6 minutes',
                       'SHANE UPDATE: answering #<n> (by text, via duet): ', 'review', 'request_id_reused', 'not_yours',
                       '--reused-<first 8 characters of request_sha256>.json', 'resp-invalid-req--1.json',
                       'ALWAYS check that `request_file` and `request_sha256`', '"performed": true', '"kind"',
                       'Questions addressed to duet', 'invalid-` or `qmark-`', 'Windows device name'):
            self.assertIn(needle, text)
        self.assertNotIn('never overwritten', text)

    def test_readme_names_every_payload_field_the_room_requires(self):
        from common_room.store import FIELDS
        text = duet_relay.DUET_README_TEMPLATE
        for action in sorted(duet_relay.COMMAND_ACTIONS):
            self.assertIn(action, text)
            for field in FIELDS[action]:
                self.assertIn('"' + field + '"', text, action + ' needs ' + field)
        for operation, fields in {'stage_text': {'task_id', 'fence', 'text'}, 'view_text': {'task_id', 'path', 'sha256'},
                                  'receipt': {'task_id'}, 'task_view': {'task_id'}}.items():
            self.assertIn(operation, text)
            for field in fields:
                self.assertIn('"' + field + '"', text, operation + ' needs ' + field)

    def test_readme_carries_the_shane_desk_block_word_for_word(self):
        guide = (Path(__file__).resolve().parents[1] / 'DUET_SEAT_2026-10-30.md').read_text(encoding='utf-8')
        block = guide.split('## The Shane desk\n\n', 1)[1].split('\n\n## ', 1)[0]
        self.assertIn('CARRYING #<n>', block)
        self.assertIn(block, duet_relay.DUET_README_TEMPLATE)
        self.assertNotIn('never overwrites', guide)
        self.assertNotIn('never overwritten', guide)

    def test_folder_inside_root_or_root_inside_folder_or_a_link_is_refused(self):
        from common_room.relay import RelayError
        root = case_dir('duet-nest')
        self.addCleanup(shutil.rmtree, root, True)
        with self.assertRaises(RelayError):
            DuetRelay(root, root / 'DuetRelay')
        with self.assertRaises(RelayError):
            DuetRelay(root / 'inner', root)
        with self.assertRaises(RelayError):
            DuetRelay(root, root / 'x' / 'DuetRelay', tmp_dir=root / 'x' / 'DuetRelay' / 'tmp')

    def test_folder_that_is_a_junction_is_refused(self):
        from common_room.relay import RelayError
        root = case_dir('duet-junc-root')
        elsewhere = case_dir('duet-junc-elsewhere')
        holder = case_dir('duet-junc-holder')
        for path in (root, elsewhere, holder):
            self.addCleanup(shutil.rmtree, path, True)
        link = holder / 'DuetRelay'
        make_junction(link, elsewhere)
        self.addCleanup(os.rmdir, link)
        with self.assertRaises(RelayError):
            DuetRelay(root, link)
        self.assertEqual(list(elsewhere.iterdir()), [], 'nothing was created through the junction')

    def test_a_parent_junction_into_the_room_root_is_refused(self):
        from common_room.relay import RelayError
        root = case_dir('duet-junc-root2')
        holder = case_dir('duet-junc-holder2')
        for path in (root, holder):
            self.addCleanup(shutil.rmtree, path, True)
        link = holder / 'plink'
        make_junction(link, root)
        self.addCleanup(os.rmdir, link)
        with self.assertRaises(RelayError):
            DuetRelay(root, link / 'DuetRelay')

    def test_responses_swapped_for_a_junction_mid_run_is_not_written_to(self):
        self.make()
        elsewhere = case_dir('duet-junc-target')
        self.addCleanup(shutil.rmtree, elsewhere, True)
        shutil.rmtree(self.drive / 'responses')
        make_junction(self.drive / 'responses', elsewhere)
        self.addCleanup(os.rmdir, self.drive / 'responses')
        self.request('a.json', self.note_request())
        lines = self.cycle()
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertIn('linked_folder', [line['error'] for line in lines])
        self.assertEqual(self.client.calls, [])
        for _ in range(30):
            self.cycle()
        self.assertFalse(self.relay.restart_wanted, 'a junction stays refused; it never asks for a restart')

    def test_requests_replaced_by_a_new_plain_folder_asks_for_a_restart_after_20_cycles(self):
        from common_room.duet_relay import LINKED_RESTART_CYCLES, run_loop
        self.make()
        shutil.rmtree(self.drive / 'requests')
        (self.drive / 'requests').mkdir()
        for i in range(LINKED_RESTART_CYCLES - 1):
            lines = self.cycle()
            self.assertIn('linked_folder', [line['error'] for line in lines])
            self.assertFalse(self.relay.restart_wanted, i)
        self.cycle()
        self.assertTrue(self.relay.restart_wanted)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
            run_loop(self.relay, 0, sleep=lambda s: None)
        self.assertEqual(raised.exception.code, 3)
        self.assertIn('restarting', err.getvalue())
        # a restart pins the folders as they are now and the relay carries on
        self.relay = self.start_relay()
        self.relay.clients['duet'] = self.client
        self.request('after.json', self.note_request('after-1'))
        self.cycle()
        self.assertTrue(self.response('after-1')['ok'])

    def test_a_good_cycle_resets_the_linked_count(self):
        from common_room.duet_relay import LINKED_RESTART_CYCLES
        self.make()
        requests = self.drive / 'requests'
        away = self.drive / 'requests-away'
        requests.rename(away)
        requests.mkdir()
        for _ in range(LINKED_RESTART_CYCLES - 2):
            self.cycle()
        requests.rmdir()
        away.rename(requests)
        self.cycle()
        self.assertEqual(self.relay.linked_cycles, 0)
        self.assertFalse(self.relay.restart_wanted)

    def test_folder_that_is_a_link_is_refused(self):
        symlinks_or_skip()
        from common_room.relay import RelayError
        root = case_dir('duet-link-root')
        elsewhere = case_dir('duet-link-elsewhere')
        self.addCleanup(shutil.rmtree, root, True)
        self.addCleanup(shutil.rmtree, elsewhere, True)
        link = case_dir('duet-link-holder') / 'DuetRelay'
        self.addCleanup(shutil.rmtree, link.parent, True)
        os.symlink(elsewhere, link, target_is_directory=True)
        with self.assertRaises(RelayError):
            DuetRelay(root, link)

    def test_a_folder_swapped_for_a_link_mid_run_is_not_written_to(self):
        symlinks_or_skip()
        self.make()
        elsewhere = case_dir('duet-swap-target')
        self.addCleanup(shutil.rmtree, elsewhere, True)
        shutil.rmtree(self.drive / 'responses')
        os.symlink(elsewhere, self.drive / 'responses', target_is_directory=True)
        self.request('a.json', self.note_request())
        lines = self.cycle()
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertIn('linked_folder', [line['error'] for line in lines])
        self.assertEqual(self.client.calls, [])


class RoundTripTests(DuetTestCase):
    def test_note_round_trip_with_receipt_fields_and_the_request_left_alone(self):
        self.make()
        path = self.request('note-1.json', self.note_request('note-1', 'hello'))
        self.cycle()
        body = self.response('note-1')
        self.assertTrue(body['ok'])
        self.assertEqual(body['request_id'], 'note-1')
        self.assertEqual(body['request_file'], 'note-1.json')
        import hashlib
        self.assertEqual(body['request_sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(self.client.calls, [('command', 'note', {'authority_id': 'orchestra', 'body': 'hello'}, 'note-1')])
        self.assertTrue(path.exists(), 'never moved or deleted')
        self.cycle()
        self.assertEqual(len(self.client.calls), 1, 'answered once')
        self.assert_nothing_created_in_drive()

    def test_stage_text_answer_keeps_path_and_digest_for_finish(self):
        self.make()
        digest = 'ab' * 32
        self.client.handoff_result = {'path': 'results/' + digest[:48] + '.txt', 'sha256': digest}
        self.request('stage.json', {'request_id': 'stage-1', 'kind': 'handoff', 'operation': 'stage_text',
                                    'payload': {'task_id': TASK_MINE, 'fence': 2, 'text': 'report'}})
        self.cycle()
        result = self.response('stage-1')['result']
        self.assertEqual(result, {'path': 'results/' + digest[:48] + '.txt', 'sha256': digest})

    def test_review_is_refused_and_never_sent(self):
        self.make()
        self.request('rev.json', {'request_id': 'rev-1', 'kind': 'command', 'action': 'review',
                                  'payload': {'task_id': TASK_MINE, 'accepted': True, 'note': 'ok'}})
        self.cycle()
        body = self.response('rev-1')
        self.assertFalse(body['ok'])
        self.assertEqual(body['error'], 'forbidden')
        self.assertIn('never reviews', body['message'])
        self.assertEqual(self.client.calls, [])

    def test_owner_actions_are_refused_and_never_sent(self):
        self.make()
        for action in ('intake', 'pause', 'grant', 'stop_all', 'assign'):
            self.request(action + '.json', {'request_id': action + '-req', 'kind': 'command', 'action': action,
                                            'payload': {}})
        self.cycle()
        for action in ('intake', 'pause', 'grant', 'stop_all', 'assign'):
            body = self.response(action + '-req')
            self.assertEqual((body['ok'], body['error']), (False, 'forbidden'))
        self.assertEqual(self.client.calls, [])

    def test_state_request_writes_the_mirror_and_marks_questions_read(self):
        self.make()
        self.client.state_body['questions'] = [{'seq': 7, 'to': 'duet', 'from': 'claude-room', 'authority_id': 'orchestra',
                                                'marks': {'read': None}}]
        self.request('state.json', {'request_id': 'state-1', 'kind': 'state'})
        self.cycle()
        body = self.response('state-1')
        self.assertTrue(body['ok'])
        self.assertEqual(body['result'], {'written': True, 'mirror': 'mirror/board.json', 'questions_read': [7]})
        self.assertEqual(self.client.calls, [('command', 'question_mark', {'note_seq': 7, 'mark': 'read'}, 'qmark-read-7')])
        mirror = json.loads((self.drive / 'mirror' / 'board.json').read_text(encoding='utf-8'))
        self.assertEqual(mirror['format'], 'duet-mirror/1')
        mine = [t for t in mirror['tasks'] if t['id'] == TASK_MINE][0]
        self.assertNotIn('body', mine)
        self.assertEqual(mine['title'], 'For duet')

    def test_state_request_exact_answer_shape(self):
        self.make()
        self.request('s.json', {'request_id': 's-1', 'kind': 'state'})
        self.cycle()
        body = self.response('s-1')
        self.assertEqual(body['result'], {'written': True, 'mirror': 'mirror/board.json'})

    def test_names_with_drive_duplicates_are_accepted_and_junk_names_ignored(self):
        self.make()
        self.request('req 1 (1).json', self.note_request('spaced-1'))
        for junk in ('desktop.ini', '~$lock.json', 'x.json.tmp', 'notes.txt', '.hidden', 'a.JSON'):
            (self.drive / 'requests' / junk).write_bytes(b'{{ not a request')
        self.cycle()
        self.assertEqual(self.responses(), ['resp-spaced-1.json'])
        self.assertEqual(self.response('spaced-1')['request_file'], 'req 1 (1).json')

    def test_bad_and_reserved_names_are_answered_invalid_at_once(self):
        self.make()
        self.request('CON.json', self.note_request('con-1'))
        self.request('a&b.json', self.note_request('amp-1'))
        self.cycle()
        self.assertEqual(self.responses(), ['resp-invalid-CON.json', 'resp-invalid-a-b.json'])
        self.assertEqual(self.client.calls, [])

    def test_unreachable_room_waits_then_is_answered_unavailable(self):
        self.make()
        self.client.command_error = CrewError('unavailable', 'Local room unavailable')
        self.request('n.json', self.note_request('wait-1'))
        self.cycle()
        self.clock.t += 59
        self.cycle()
        self.assertFalse(self.response_path('wait-1').exists())
        self.clock.t += 2
        self.cycle()
        body = self.response('wait-1')
        self.assertEqual((body['ok'], body['error']), (False, 'unavailable'))

    def test_unreachable_room_then_back_is_answered_normally(self):
        self.make()
        self.client.command_error = CrewError('unavailable', 'Local room unavailable')
        self.request('n.json', self.note_request('back-1'))
        self.cycle()
        self.client.command_error = None
        self.clock.t += 10
        self.cycle()
        self.assertTrue(self.response('back-1')['ok'])


class NotYoursTests(DuetTestCase):
    def test_task_bound_actions_on_a_task_for_someone_else_are_refused_unsent(self):
        self.make()
        for action in ('ack', 'claim', 'renew', 'finish', 'fail', 'wait_decision'):
            self.request(action + '.json', {'request_id': action + '-x', 'kind': 'command', 'action': action,
                                            'payload': {'task_id': TASK_OTHER}})
        self.cycle()
        for action in ('ack', 'claim', 'renew', 'finish', 'fail', 'wait_decision'):
            body = self.response(action + '-x')
            self.assertEqual((body['ok'], body['error']), (False, 'not_yours'), action)
            self.assertEqual(body['message'], 'This task is not addressed to duet.')
        self.assertEqual(self.client.calls, [], 'nothing was sent to the room')

    def test_the_same_actions_on_a_task_for_duet_are_sent(self):
        self.make()
        self.request('claim.json', {'request_id': 'claim-1', 'kind': 'command', 'action': 'claim',
                                    'payload': {'task_id': TASK_MINE, 'seconds': 600}})
        self.cycle()
        self.assertTrue(self.response('claim-1')['ok'])
        self.assertEqual(self.client.actions(), ['claim'])

    def test_a_task_that_is_not_visible_is_refused_unsent(self):
        self.make()
        self.request('ack.json', {'request_id': 'ack-x', 'kind': 'command', 'action': 'ack',
                                  'payload': {'task_id': 't' + 'c3' * 16}})
        self.cycle()
        body = self.response('ack-x')
        self.assertEqual((body['ok'], body['error']), (False, 'forbidden'))
        self.assertEqual(self.client.calls, [])

    def test_a_note_or_a_decision_on_someone_elses_task_is_not_checked_here(self):
        self.make()
        self.request('dec.json', {'request_id': 'dec-1', 'kind': 'command', 'action': 'resolve_decision',
                                  'payload': {'task_id': TASK_OTHER, 'body': 'go'}})
        self.cycle()
        self.assertEqual(self.client.actions(), ['resolve_decision'], 'the room decides who may')


HANDOFF_PAYLOADS = {
    'stage_text': lambda t: {'task_id': t, 'fence': 2, 'text': 'report'},
    'view_text': lambda t: {'task_id': t, 'path': 'results/' + 'ab' * 24 + '.txt', 'sha256': 'ab' * 32},
    'receipt': lambda t: {'task_id': t},
    'task_view': lambda t: {'task_id': t}}


class RequestIdTests(DuetTestCase):
    """N1: Duet's own request_id and request_file come back exactly as sent, whatever they look like to the filter."""

    IDS = ('note-1730329200', 'acct1234', '2026.10.30.1', 'card-4242', 'inv-12345.67')

    def test_ids_that_look_like_numbers_round_trip_unchanged_and_their_drive_copies_are_skipped(self):
        self.make()
        for request_id in self.IDS:
            with self.subTest(request_id=request_id):
                body = self.note_request(request_id, 'once ' + request_id)
                self.request(request_id + '.json', body)
                self.cycle()
                answer = self.response(request_id)
                self.assertTrue(answer['ok'])
                self.assertEqual(answer['request_id'], request_id)
                self.assertEqual(answer['request_file'], request_id + '.json')
                calls = len(self.client.calls)
                self.request(request_id + ' (1).json', body)           # Drive's copy: same content, another name
                lines = self.cycle()
                self.assertEqual(len(self.client.calls), calls, 'one room call in all')
                self.assertIn('duplicate', [line['error'] for line in lines])
                self.assertEqual([n for n in self.responses() if '--reused-' in n], [], 'no refusal file')
                self.assertFalse(any(n.startswith('resp-invalid-') for n in self.responses()))
        self.assertEqual(len(self.client.calls), len(self.IDS))

    def test_a_file_name_that_looks_like_a_number_is_kept_in_request_file(self):
        self.make()
        self.request('req 1730329200 (1).json', self.note_request('plain-1'))
        self.cycle()
        self.assertEqual(self.response('plain-1')['request_file'], 'req 1730329200 (1).json')

    def test_an_id_that_looks_like_a_key_is_refused_up_front(self):
        self.make()
        key_id = 'AbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCdEfGhI'      # 45 characters, mixed case: key-shaped
        self.assertGreaterEqual(len(key_id), 43)
        self.request('k.json', self.note_request(key_id))
        self.cycle()
        self.assertEqual(self.responses(), ['resp-invalid-k.json'])
        body = json.loads((self.drive / 'responses' / 'resp-invalid-k.json').read_text(encoding='utf-8'))
        self.assertEqual((body['ok'], body['error']), (False, 'invalid'))
        self.assertIn('looks like a key', body['message'])
        self.assertNotIn('request_id', body)
        self.assertNotIn(key_id, json.dumps(body))
        self.assertEqual(self.client.calls, [])
        self.assertIn('43', duet_relay.DUET_README_TEMPLATE)


class HandoffTests(DuetTestCase):
    def ask(self, operation, task, request_id=None):
        request_id = request_id or operation + '-' + task[:4]
        self.request(request_id + '.json', {'request_id': request_id, 'kind': 'handoff', 'operation': operation,
                                            'payload': HANDOFF_PAYLOADS[operation](task)})
        return request_id

    def test_all_four_operations_are_refused_unsent_on_a_task_for_and_from_someone_else(self):
        self.make()
        ids = [self.ask(op, TASK_OTHER) for op in HANDOFF_PAYLOADS]
        self.cycle()
        for request_id in ids:
            body = self.response(request_id)
            self.assertEqual((body['ok'], body['error']), (False, 'not_yours'), request_id)
        self.assertEqual(self.client.calls, [], 'nothing was sent to the room')

    def test_a_task_that_is_not_visible_is_refused_unsent(self):
        self.make()
        request_id = self.ask('task_view', 't' + 'c3' * 16)
        self.cycle()
        self.assertEqual(self.response(request_id)['error'], 'forbidden')
        self.assertEqual(self.client.calls, [])

    def test_task_view_on_duets_own_and_duets_sent_tasks_is_cut_to_the_whitelist(self):
        self.make()
        for task in (TASK_MINE, TASK_FROM_ME):
            self.client.handoff_result = {
                'task': {'id': task, 'title': 'T', 'status': 'delivered', 'sender': 'claude-room', 'recipient': 'duet',
                         'builder': None, 'parent_id': None, 'authority_id': 'orchestra', 'fence': 1,
                         'failed_attempts': 0, 'body': 'do it', 'acceptance': 'check it', 'kind': 'handoff.record',
                         'lease': {'holder': 'duet', 'fence': 1, 'until': 5.0, 'extra': 'x'},
                         'intake': {'source_namespace': 'shane', 'external_reference': 'REF-SECRET-CHAT'},
                         'result_body': 'OTHER BODY', 'decision': 'D', 'review_note': 'R', 'owner': 'keeper'},
                'events': [{'seq': 3, 'kind': 'task_finished', 'actor': 'vesper', 'timestamp': 9.0,
                            'payload': {'changes': {'result_body': 'EVENT PAYLOAD'}}, 'sha256': 'f' * 64}]}
            request_id = self.ask('task_view', task)
            self.cycle()
            result = self.response(request_id)['result']
            self.assertEqual(sorted(result), ['events', 'task'])
            self.assertEqual(sorted(result['task']), ['acceptance', 'authority_id', 'body', 'builder', 'failed_attempts',
                                                      'fence', 'id', 'lease', 'parent_id', 'recipient', 'sender', 'status',
                                                      'title'])
            self.assertEqual(result['task']['lease'], {'holder': 'duet', 'fence': 1, 'until': 5.0})
            self.assertEqual(result['events'], [{'seq': 3, 'kind': 'task_finished', 'actor': 'vesper', 'timestamp': 9.0}])
            text = self.response_path(request_id).read_text(encoding='utf-8')
            for banned in ('REF-SECRET-CHAT', 'OTHER BODY', 'EVENT PAYLOAD', 'intake', 'owner'):
                self.assertNotIn(banned, text)

    def test_receipt_has_no_intake_reference_and_no_events(self):
        self.make()
        self.client.handoff_result = {'version': 1, 'task_id': TASK_MINE, 'authority_id': 'orchestra', 'parent_id': None,
                                      'chain_depth': 0, 'source_namespace': 'shane', 'external_reference': 'REF-SECRET',
                                      'sender': 'claude-room', 'owner': 'keeper', 'recipient': 'duet', 'builder': None,
                                      'reviewer': None, 'status': 'done', 'fence': 3, 'lease': None,
                                      'artifacts': [{'path': 'results/' + 'ab' * 24 + '.txt', 'sha256': 'ab' * 32}],
                                      'events': [{'seq': 1, 'actor': 'x', 'kind': 'y', 'timestamp': 1.0, 'sha256': 'f' * 64}]}
        request_id = self.ask('receipt', TASK_MINE)
        self.cycle()
        result = self.response(request_id)['result']
        self.assertEqual(result['artifacts'], [{'path': 'results/' + 'ab' * 24 + '.txt', 'sha256': 'ab' * 32}])
        self.assertEqual(result['task_id'], TASK_MINE)
        text = self.response_path(request_id).read_text(encoding='utf-8')
        for banned in ('REF-SECRET', 'external_reference', 'source_namespace', 'events', 'keeper'):
            self.assertNotIn(banned, text)

    def test_view_text_returns_the_text_scrubbed_and_stage_text_returns_path_and_digest(self):
        self.make()
        self.client.handoff_result = {'path': 'results/' + 'ab' * 24 + '.txt', 'sha256': 'ab' * 32,
                                      'text': 'Report. Contact a@b.com about 12,345.67', 'task_body': 'NO'}
        request_id = self.ask('view_text', TASK_MINE)
        self.cycle()
        result = self.response(request_id)['result']
        self.assertEqual(result, {'path': 'results/' + 'ab' * 24 + '.txt', 'sha256': 'ab' * 32,
                                  'text': 'Report. Contact [email] about [amount]'})
        self.client.handoff_result = {'path': 'results/' + 'ab' * 24 + '.txt', 'sha256': 'ab' * 32, 'text': 'NO'}
        request_id = self.ask('stage_text', TASK_MINE)
        self.cycle()
        self.assertEqual(self.response(request_id)['result'], {'path': 'results/' + 'ab' * 24 + '.txt', 'sha256': 'ab' * 32})


class DriveLagTests(DuetTestCase):
    PARTIAL = b'{"request_id": "late-1", "kind": "comm'

    def test_partial_file_is_held_then_answered_invalid_only_after_150_seconds_unchanged(self):
        self.make()
        self.assertEqual(SETTLE_SECONDS, 150)
        self.request('late.json', self.PARTIAL)
        self.request('good.json', self.note_request('good-1'))
        self.cycle()                                    # t = 1000: first sighting
        self.assertEqual(self.responses(), ['resp-good-1.json'], 'a held file never blocks the others')
        for step in (50, 99, 149):                      # 1050, 1099, 1149: still within 150 s of 1000
            self.clock.t = 1000 + step
            self.cycle()
            self.assertEqual(self.responses(), ['resp-good-1.json'], step)
        self.clock.t = 1150
        self.cycle()
        self.assertEqual(self.responses(), ['resp-good-1.json', 'resp-invalid-late.json'])
        body = json.loads((self.drive / 'responses' / 'resp-invalid-late.json').read_text(encoding='utf-8'))
        self.assertFalse(body['ok'])
        self.assertEqual(body['request_file'], 'late.json')
        self.assertEqual(len(body['request_sha256']), 64)
        self.assertEqual(self.client.actions(), ['note'], 'the partial file was never sent')
        self.clock.t = 1400
        self.cycle()
        self.assertEqual(len(self.responses()), 2, 'answered once')

    def test_partial_file_that_becomes_complete_is_answered_normally(self):
        self.make()
        path = self.request('late.json', self.PARTIAL)
        self.cycle()
        self.clock.t = 1100
        self.cycle()
        self.assertEqual(self.responses(), [])
        path.write_bytes(json.dumps(self.note_request('late-1', 'whole now')).encode('utf-8'))
        self.clock.t = 1101
        self.cycle()
        self.assertTrue(self.response('late-1')['ok'])
        self.assertEqual(self.responses(), ['resp-late-1.json'])
        self.assertEqual(self.client.actions(), ['note'])

    def test_a_file_that_keeps_changing_restarts_the_settle_timer(self):
        self.make()
        path = self.request('late.json', self.PARTIAL)
        self.cycle()                                    # 1000
        self.clock.t = 1140
        path.write_bytes(self.PARTIAL + b'and": "no')    # still partial, bytes changed
        self.cycle()                                    # timer restarts at 1140
        self.clock.t = 1200                             # 60 s since the change, 200 s since the first sighting
        self.cycle()
        self.assertEqual(self.responses(), [])
        self.clock.t = 1290                             # exactly 150 s since the change
        self.cycle()
        self.assertEqual(self.responses(), ['resp-invalid-late.json'])

    def test_many_held_files_do_not_starve_new_requests(self):
        self.make()
        for i in range(25):
            self.request('half%02d.json' % i, b'{"request_id": "h%d", ' % i)
        self.cycle()
        self.request('fresh.json', self.note_request('fresh-1'))
        self.cycle()
        self.assertTrue(self.response('fresh-1')['ok'])

    def test_duplicate_copy_with_a_drive_name_and_same_content_is_skipped_silently(self):
        self.make()
        body = self.note_request('dup-1', 'once')
        self.request('dup-1.json', body)
        self.cycle()
        first = self.response_path('dup-1').read_bytes()
        self.assertEqual(len(self.client.calls), 1)
        self.request('dup-1 (1).json', body)
        lines = self.cycle()
        self.assertEqual(len(self.client.calls), 1, 'one room call in all')
        self.assertEqual(self.response_path('dup-1').read_bytes(), first, 'the answer was not rewritten')
        self.assertEqual(self.responses(), ['resp-dup-1.json'])
        self.assertIn('duplicate', [line['error'] for line in lines])

    def test_reuploaded_same_file_after_a_restart_is_skipped(self):
        self.make()
        path = self.request('again.json', self.note_request('again-1'))
        self.cycle()
        path.write_bytes(path.read_bytes())            # Drive re-uploads: new mtime, same bytes
        self.cycle()
        self.relay = self.start_relay()
        self.relay.clients['duet'] = self.client
        self.cycle()
        self.assertEqual(len(self.client.calls), 1)

    def test_same_request_id_with_different_content_is_refused_and_the_first_answer_stands(self):
        self.make()
        self.request('one.json', self.note_request('same-id', 'first'))
        self.cycle()
        first = self.response_path('same-id').read_bytes()
        calls = list(self.client.calls)
        second = self.request('two.json', self.note_request('same-id', 'DIFFERENT'))
        self.cycle()
        self.assertEqual(self.response_path('same-id').read_bytes(), first, 'the first answer is untouched')
        self.assertEqual(self.client.calls, calls, 'nothing sent')
        digest = hashlib.sha256(second.read_bytes()).hexdigest()
        refusal = self.drive / 'responses' / ('resp-same-id--reused-' + digest[:8] + '.json')
        refused = json.loads(refusal.read_text(encoding='utf-8'))
        self.assertEqual((refused['ok'], refused['error']), (False, 'request_id_reused'))
        self.assertEqual(refused['request_id'], 'same-id')
        self.assertEqual(refused['request_file'], 'two.json')
        self.assertEqual(refused['request_sha256'], digest)
        self.assertEqual(self.responses(), ['resp-same-id--reused-' + digest[:8] + '.json', 'resp-same-id.json'])
        self.cycle()                                       # answered once
        self.assertEqual(self.client.calls, calls)

    def test_ids_that_sanitize_to_the_same_answer_name_are_refused_as_reused(self):
        self.make()
        self.request('one.json', self.note_request('ab-', 'first'))      # answered as resp-ab.json
        self.cycle()
        self.assertEqual(self.responses(), ['resp-ab.json'])
        first = (self.drive / 'responses' / 'resp-ab.json').read_bytes()
        same_bytes = self.request('two.json', self.note_request('ab', 'first'))
        self.cycle()
        digest = hashlib.sha256(same_bytes.read_bytes()).hexdigest()
        self.assertEqual((self.drive / 'responses' / 'resp-ab.json').read_bytes(), first)
        refused = json.loads((self.drive / 'responses' / ('resp-ab--reused-' + digest[:8] + '.json')).read_text(encoding='utf-8'))
        self.assertEqual(refused['error'], 'request_id_reused')
        self.assertEqual(len(self.client.calls), 1)

    def test_ids_that_would_collide_with_answer_names_are_refused(self):
        self.make()
        for request_id in ('invalid-x', 'Invalid-y', 'qmark-read-5'):
            self.request(request_id + '.json', self.note_request(request_id))
        self.cycle()
        self.assertEqual(self.responses(), ['resp-invalid-Invalid-y.json', 'resp-invalid-invalid-x.json',
                                            'resp-invalid-qmark-read-5.json'])
        self.assertEqual(self.client.calls, [])
        body = json.loads((self.drive / 'responses' / 'resp-invalid-invalid-x.json').read_text(encoding='utf-8'))
        self.assertEqual(body['error'], 'invalid')


class MirrorTests(DuetTestCase):
    def test_periodic_mirror_with_no_requests_after_mirror_every(self):
        self.make(mirror_every=300)
        self.cycle()                                    # first cycle writes at once
        board = self.drive / 'mirror' / 'board.json'
        self.assertEqual(json.loads(board.read_text(encoding='utf-8'))['generated_at'], 1000.0)
        self.clock.t = 1100
        self.client.state_body['server_time'] = 1100.0
        self.cycle()
        self.assertEqual(json.loads(board.read_text(encoding='utf-8'))['generated_at'], 1000.0, 'not due yet')
        self.clock.t = 1299
        self.client.state_body['server_time'] = 1299.0
        self.cycle()
        self.assertEqual(json.loads(board.read_text(encoding='utf-8'))['generated_at'], 1000.0, 'not due yet')
        self.clock.t = 1300
        self.client.state_body['server_time'] = 1300.0
        self.cycle()
        self.assertEqual(json.loads(board.read_text(encoding='utf-8'))['generated_at'], 1300.0)
        self.assertEqual(self.responses(), [])
        self.assertEqual(self.client.calls, [])

    def test_mirror_is_a_whitelist_and_never_raw_state(self):
        self.make()
        state = self.client.state_body
        state.update(instance_id='deadbeef' * 4, origin='http://127.0.0.1:50263', connection={'mode': 'x'},
                     history=[{'seq': 1, 'detail': 'secret detail', 'sha256': 'f' * 64}], room_renewed={'note': 'n'})
        state['participants'] = [{'id': 'duet', 'role': 'worker', 'contact_age_s': 4, 'session': 'chat-session-77',
                                  'label': 'Duet app', 'contact': 'x', 'key_active': True}]
        state['notes'] = [{'seq': n, 'sender': 'claude-room', 'authority_id': 'orchestra', 'body': 'note %d' % n,
                           'timestamp': 1000.0 + n, 'sha256': 'e' * 64} for n in range(1, 61)]
        state['tasks'][0]['lease'] = {'holder': 'duet', 'fence': 3, 'until': 1500.0}
        state['tasks'][0]['status'] = 'waiting_decision'
        self.cycle()
        text = (self.drive / 'mirror' / 'board.json').read_text(encoding='utf-8')
        mirror = json.loads(text)
        self.assertEqual(sorted(mirror), ['asked', 'format', 'generated_at', 'needs', 'notes', 'participants',
                                          'questions', 'stop', 'tasks', 'written_at'])
        self.assertEqual(len(mirror['notes']), 50)
        self.assertEqual(mirror['notes'][0]['seq'], 11)
        self.assertEqual(sorted(mirror['notes'][0]), ['authority_id', 'body', 'sender', 'seq', 'timestamp'])
        self.assertEqual(mirror['participants'], [{'id': 'duet', 'role': 'worker', 'contact_age_s': 4}])
        mine = [t for t in mirror['tasks'] if t['id'] == TASK_MINE][0]
        self.assertEqual(mine['lease'], {'holder': 'duet', 'until': 1500.0})
        self.assertTrue(mine['needs_shane'])
        self.assertEqual(mine['authority_id'], 'orchestra')
        for banned in ('deadbeef', '50263', 'session', 'secret detail', 'room_renewed', 'sha256', 'PRIVATE BODY'):
            self.assertNotIn(banned, text)

    def test_nothing_ever_writes_raw_state(self):
        self.make()
        self.assertEqual(self.relay._persist_state('duet', self.relay._pin('duet'), self.client.state_body), True)
        self.assertFalse(list(self.drive.rglob('relay-state.json')))
        self.assertTrue((self.drive / 'mirror' / 'board.json').exists())


class TempAndLockTests(DuetTestCase):
    def test_no_temp_file_is_ever_created_in_the_synced_folder(self):
        self.make()
        self.request('a.json', self.note_request('a-1'))
        self.request('s.json', {'request_id': 's-1', 'kind': 'state'})
        self.request('half.json', b'{"request_id"')
        self.cycle()
        self.clock.t += 400
        self.cycle()
        self.assert_nothing_created_in_drive()
        tmp_created = [p for p in self.created if p.endswith('.tmp')]
        self.assertTrue(tmp_created, 'temp files are made, outside the synced folder')
        for p in tmp_created:
            self.assertIn('duet-tmp', p)
        self.assertEqual([p for p in self.drive.rglob('*') if p.name.endswith('.tmp')], [])

    def test_cross_volume_replace_failure_writes_nothing_and_is_logged(self):
        self.make()
        self.request('a.json', self.note_request('a-1'))
        with mock.patch('os.replace', side_effect=OSError(18, 'cross-device')):
            lines = self.cycle()
        self.assertEqual(self.responses(), [])
        self.assertFalse((self.drive / 'mirror' / 'board.json').exists())
        self.assertTrue(any(str(line['error']).startswith('replace_failed') for line in lines))
        self.assertEqual(list((self.root / 'duet-tmp').iterdir()), [])
        self.assertEqual(len(self.client.calls), 1, 'the note was sent once')
        self.clock.t += 5
        self.cycle()
        self.assertEqual(len(self.client.calls), 1, 'backed off: nothing sent again 5 s later')
        self.clock.t += 6                               # 11 s after the failure; the disk is fine again
        self.cycle()
        self.assertTrue(self.response('a-1')['ok'])

    def test_failed_writes_back_off_by_doubling_up_to_300_seconds(self):
        self.make()
        self.request('a.json', self.note_request('a-1'))
        waits = []
        with mock.patch('os.replace', side_effect=OSError(5, 'denied')):
            for _ in range(8):
                self.cycle()
                waits.append(self.relay.write_wait)
                self.clock.t = self.relay.backoff_until
        self.assertEqual(waits, [10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0, 300.0])
        self.cycle()
        self.assertEqual(self.relay.write_wait, 0.0, 'a good write ends the back-off')
        self.assertTrue(self.response('a-1')['ok'])

    def test_lock_refuses_a_second_duet_relay(self):
        self.make()
        first = DuetRelayLock(self.root)
        second = DuetRelayLock(self.root)
        self.assertTrue(first.acquire())
        try:
            self.assertFalse(second.acquire())
            self.assertEqual(second.reason, 'busy')
        finally:
            first.release()
        self.assertTrue(second.acquire())
        second.release()
        self.assertEqual(first.path.name, 'duet-relay.lock')
        self.assertEqual(first.path.parent, self.root)

    def test_cli_refuses_a_busy_lock_and_a_bad_config_with_exit_2(self):
        self.make()
        config = self.root / 'duet.json'
        config.write_text(json.dumps({'folder': str(self.drive), 'mirror_every': 300}), encoding='utf-8')
        lock = DuetRelayLock(self.root)
        self.assertTrue(lock.acquire())
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
                duet_relay.main(['--config', str(config), '--root', str(self.root), '--once'])
            self.assertEqual(raised.exception.code, 2)
            self.assertIn('already running', err.getvalue())
        finally:
            lock.release()
        for bad in ('{not json', json.dumps({'mirror_every': 5}), json.dumps({'folder': 3}),
                    json.dumps({'folder': 'x', 'mirror_every': 'soon'}), json.dumps({'folder': 'x', 'extra': 1})):
            config.write_text(bad, encoding='utf-8')
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
                duet_relay.main(['--config', str(config), '--root', str(self.root), '--once'])
            self.assertEqual(raised.exception.code, 2, bad)
            self.assertIn('common_room.duet_relay:', err.getvalue())
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            duet_relay.main(['--config', str(self.root / 'missing.json'), '--root', str(self.root), '--once'])
        self.assertEqual(raised.exception.code, 2)

    def test_config_loads(self):
        self.make()
        config = self.root / 'duet.json'
        config.write_text(json.dumps({'folder': 'D:\\Work Area\\DuetRelay', 'mirror_every': 300,
                                      'tmp_dir': 'C:\\t\\x'}), encoding='utf-8')
        self.assertEqual(duet_relay.load_config(config), ('D:\\Work Area\\DuetRelay', 300.0, 'C:\\t\\x'))

    def test_missing_credential_means_no_cycle_and_no_crash(self):
        self.make()
        (self.root / 'duet-credential.json').unlink()
        self.request('a.json', self.note_request('a-1'))
        lines = self.cycle()
        self.assertEqual(self.responses(), [])
        self.assertIn('credential_missing', [line['error'] for line in lines])


class FixOneTests(DuetTestCase):
    """DUET-FIX1 (astra-audit's F1 and F2, note 5288; ruling 5356): a state request that marked questions read
    answers honestly when the mirror is not written, and _put writes every byte or nothing."""

    def unread_question(self, seq=7, text='QUESTION FOR duet: a made-up question'):
        return {'seq': seq, 'to': 'duet', 'from': 'claude-room', 'authority_id': 'orchestra', 'text': text,
                'marks': {'read': None}}

    def walk_drive_bytes(self):
        return b''.join(p.read_bytes() for p in self.drive.rglob('*') if p.is_file())

    def emit_refusing_the_mirror(self):
        real = self.relay._emit

        def emit(pin, where, name, value, as_text=False, scrubbed=False):
            if where == 'mirror':
                raise duet_relay.BlockedSecret('made-up refusal of the mirror by the final check')
            return real(pin, where, name, value, as_text=as_text, scrubbed=scrubbed)
        return mock.patch.object(self.relay, '_emit', emit)

    def assert_partial(self, body, reason, read):
        self.assertEqual((body['ok'], body['error'], body['performed'], body['partial'], body['mirror_written']),
                         (False, reason, True, True, False))
        self.assertEqual(body['done'], {'questions_read': read})
        self.assertIn('marked %d question(s) read' % len(read), body['message'])
        self.assertIn('new request_id', body['retry'])
        self.assertEqual(body['kind'], 'state')

    def test_a_marks_done_then_the_guard_refuses_the_mirror(self):
        self.make()
        self.client.state_body['questions'] = [self.unread_question()]
        self.request('state.json', {'request_id': 'state-a', 'kind': 'state'})
        with self.emit_refusing_the_mirror():
            lines = self.cycle()
        body = self.response('state-a')
        self.assert_partial(body, 'blocked', [7])
        self.assertFalse((self.drive / 'mirror' / 'board.json').exists())
        self.assertEqual(self.client.calls, [('command', 'question_mark', {'note_seq': 7, 'mark': 'read'}, 'qmark-read-7')])
        self.assertIn('partial_blocked', [line['error'] for line in lines])

    def test_b_marks_done_then_the_mirror_write_fails_on_io(self):
        self.make()
        self.client.state_body['questions'] = [self.unread_question()]
        real_put = self.relay._put

        def put(pin, where, name, data):
            if where != 'mirror':
                return real_put(pin, where, name, data)
            with mock.patch('os.write', side_effect=OSError(5, 'made-up disk error')):
                return real_put(pin, where, name, data)
        self.request('state.json', {'request_id': 'state-b', 'kind': 'state'})
        with mock.patch.object(self.relay, '_put', put):
            self.cycle()
        self.assert_partial(self.response('state-b'), 'io', [7])
        self.assertEqual(list((self.drive / 'mirror').iterdir()), [], 'no temp file and no board in mirror')
        self.assertEqual(list((self.root / 'duet-tmp').iterdir()), [])

    def test_c_a_question_holding_the_test_token_is_left_out_and_counted_never_shown(self):
        """Ruling 5356: the filter case. The item is left out, the rest of the mirror is written, the answer reports
        the marks and a count of items left out, and the test token is nowhere under DuetRelay or in the answer."""
        self.make()
        self.client.state_body['questions'] = [self.unread_question(9, 'QUESTION FOR duet: use ' + OWN_TOKEN)]
        self.request('state.json', {'request_id': 'state-c', 'kind': 'state'})
        lines = self.cycle()
        body = self.response('state-c')
        self.assertTrue(body['ok'])
        self.assertEqual(body['result'], {'written': True, 'mirror': 'mirror/board.json', 'questions_read': [9],
                                          'items_left_out': 1})
        mirror = json.loads((self.drive / 'mirror' / 'board.json').read_text(encoding='utf-8'))
        self.assertEqual(mirror['questions'], [])
        self.assertIn('blocked_item', [line['error'] for line in lines])
        self.assertNotIn(OWN_TOKEN.encode(), self.walk_drive_bytes())
        self.assertNotIn(OWN_TOKEN, json.dumps(body))

    def test_d_the_same_request_again_writes_nothing_twice_and_a_new_one_writes_the_mirror(self):
        self.make()
        self.client.state_body['questions'] = [self.unread_question()]
        request = {'request_id': 'state-d', 'kind': 'state'}
        self.request('state.json', request)
        with self.emit_refusing_the_mirror():
            self.cycle()
        first = self.response_path('state-d').read_bytes()
        self.client.state_body['questions'][0]['marks']['read'] = 1000.0    # the room now shows the mark
        self.request('state (1).json', request)                              # Drive's copy of the same request
        lines = self.cycle()
        self.assertIn('duplicate', [line['error'] for line in lines])
        self.assertEqual(self.response_path('state-d').read_bytes(), first, 'the first answer stands')
        self.assertEqual(len([c for c in self.client.calls if c[1] == 'question_mark']), 1, 'no second room write')
        self.request('state-2.json', {'request_id': 'state-d2', 'kind': 'state'})
        self.cycle()
        body = self.response('state-d2')
        self.assertTrue(body['ok'])
        self.assertEqual(body['result'], {'written': True, 'mirror': 'mirror/board.json'})
        self.assertTrue((self.drive / 'mirror' / 'board.json').exists())
        self.assertEqual(len([c for c in self.client.calls if c[1] == 'question_mark']), 1)

    def put_board(self, data):
        pin = self.relay._pin('duet')
        try:
            return self.relay._put(pin, 'mirror', 'board.json', data)
        finally:
            self.relay._unpin(pin)

    def test_e_a_short_write_is_completed(self):
        self.make()
        data = b'{"board": "' + b'x' * 5000 + b'"}'
        real_write = os.write
        sizes = []

        def short_then_rest(fd, chunk):
            sizes.append(len(chunk))
            return real_write(fd, bytes(chunk[:1000]) if len(sizes) == 1 else bytes(chunk))
        with mock.patch('os.write', short_then_rest):
            self.assertTrue(self.put_board(data))
        self.assertEqual(sizes, [len(data), len(data) - 1000])
        self.assertEqual((self.drive / 'mirror' / 'board.json').read_bytes(), data)
        self.assertEqual(list((self.root / 'duet-tmp').iterdir()), [])

    def test_f_no_progress_is_a_failure(self):
        self.make()
        board = self.drive / 'mirror' / 'board.json'
        with mock.patch('os.write', return_value=0):
            self.assertFalse(self.put_board(b'{"new": true}'))
        self.assertFalse(board.exists(), 'a new target stays absent')
        self.assertTrue(self.put_board(b'{"old": true}'))
        with mock.patch('os.write', return_value=0):
            self.assertFalse(self.put_board(b'{"new": true}'))
        self.assertEqual(board.read_bytes(), b'{"old": true}', 'an existing target is unchanged')
        self.assertEqual(list((self.root / 'duet-tmp').iterdir()), [])
        self.assertEqual([p.name for p in (self.drive / 'mirror').iterdir()], ['board.json'])

    def test_g_an_error_after_a_partial_write_fails_cleanly_and_retries_back_off_as_before(self):
        self.make()
        board = self.drive / 'mirror' / 'board.json'
        self.assertTrue(self.put_board(b'{"old": true}'))
        real_write = os.write
        calls = []

        def partial_then_error(fd, chunk):
            calls.append(len(chunk))
            if len(calls) % 2 == 1:
                return real_write(fd, bytes(chunk[:3]))
            raise OSError(5, 'made-up disk error')
        with mock.patch('os.write', partial_then_error):
            self.assertFalse(self.put_board(b'{"new": true, "more": 1}'))
        self.assertEqual(board.read_bytes(), b'{"old": true}')
        self.assertEqual(list((self.root / 'duet-tmp').iterdir()), [])
        # A retrying caller: a note whose answer cannot be written. The relay's retry is its clock-based back-off
        # (_note_write_failure); it never sleeps. Same attempts and the same waits as for a refused replace.
        self.relay.write_wait, self.relay.backoff_until = 0.0, 0.0
        self.request('a.json', self.note_request('a-g'))
        waits = []
        with mock.patch('os.write', partial_then_error), mock.patch('time.sleep') as slept:
            for _ in range(8):
                self.cycle()
                waits.append(self.relay.write_wait)
                self.clock.t = self.relay.backoff_until
        self.assertEqual(waits, [10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0, 300.0])
        self.assertEqual(slept.call_count, 0)
        self.assertEqual(len([c for c in self.client.calls if c[1] == 'note']), 8, 'one attempt per cycle, as before')
        self.assertFalse(self.response_path('a-g').exists())
        self.cycle()
        self.assertEqual(self.relay.write_wait, 0.0, 'a good write ends the back-off')
        self.assertTrue(self.response('a-g')['ok'])


if __name__ == '__main__':
    unittest.main(argv=sys.argv[:1])    # evidence.py passes the tree root as an argument; it names no test
