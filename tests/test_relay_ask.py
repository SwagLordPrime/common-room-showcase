"""crew/relay_ask.py: the file-only chat's helper, against a real room and a running relay."""
import hashlib, importlib.util, io, json, re, threading, time, unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from tests.helpers import case_dir
from tests.test_relay import RelayTestCase as RelayTests
from common_room.relay import Relay

SPEC = importlib.util.spec_from_file_location('relay_ask', Path(__file__).resolve().parents[1] / 'crew' / 'relay_ask.py')
relay_ask = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(relay_ask)


class RelayAskTests(unittest.TestCase):
    start = RelayTests.start
    intake_task = RelayTests.intake_task

    def running_relay(self, room, actors):
        relay = Relay(room.root, actors)
        relay._log = lambda *args, **kwargs: None   # keep the relay's own log lines out of captured stdout
        stop = threading.Event()
        def loop():
            while not stop.is_set():
                relay.process_once()
                stop.wait(0.2)
        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        self.addCleanup(lambda: (stop.set(), thread.join(3)))
        return relay

    def ask(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = relay_ask.main(list(argv))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_claim_and_queue_through_helper(self):
        room = self.start()
        task = self.intake_task(room)
        self.running_relay(room, ['relay-writer'])
        folder = str(room.root / 'relay' / 'relay-writer')
        code, out, err = self.ask(folder, json.dumps({'request_id': 'ack-1', 'kind': 'command', 'action': 'ack',
                                                      'payload': {'task_id': task}}))
        self.assertEqual(code, 0, err)
        code, out, err = self.ask(folder, json.dumps({'request_id': 'claim-1', 'kind': 'command', 'action': 'claim',
                                                      'payload': {'task_id': task, 'seconds': 1800}}))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['result']['task']['status'], 'claimed')
        code, out, err = self.ask(folder, 'state')
        self.assertEqual(code, 0, err)
        rows = json.loads(out)['tasks']
        self.assertEqual([(r['task_id'], r['status'], r['queue']) for r in rows], [(task, 'claimed', 'mine')])

    def test_refusal_exits_one_and_bad_id_exits_two(self):
        room = self.start()
        self.running_relay(room, ['relay-writer'])
        folder = str(room.root / 'relay' / 'relay-writer')
        code, out, err = self.ask(folder, json.dumps({'request_id': 'claim-x', 'kind': 'command', 'action': 'claim',
                                                      'payload': {'task_id': 'tmissing', 'seconds': 1800}}))
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)['ok'])
        code, out, err = self.ask(folder, json.dumps({'request_id': 'bad:id', 'kind': 'state'}))
        self.assertEqual(code, 2)
        for trailing in ('note-7-', 'note-7.'):                               # N2: relay trims these
            code, out, err = self.ask(folder, json.dumps({'request_id': trailing, 'kind': 'state'}))
            self.assertEqual(code, 2)
            self.assertIn('starting and ending', err)                         # refused up front, not a timeout
            self.assertFalse(any(Path(folder, sub, trailing + '.json').exists() for sub in ('requests', 'archive')))
        # A request the relay cannot parse is answered as invalid-<id>.json; the helper reads that too.
        code, out, err = self.ask(folder, json.dumps({'request_id': 'odd-1', 'kind': 'teleport'}))
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)['ok'])

    def test_refused_resend_never_shows_the_older_success(self):              # N3
        room = self.start()
        self.running_relay(room, ['relay-writer'])
        folder = str(room.root / 'relay' / 'relay-writer')
        self.assertEqual(self.ask(folder, json.dumps({'request_id': 'same-1', 'kind': 'state'}))[0], 0)
        code, out, err = self.ask(folder, json.dumps({'request_id': 'same-1', 'kind': 'teleport'}))
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)['ok'])

    def test_resend_same_id_reads_the_new_answer(self):
        room = self.start()
        task = self.intake_task(room)
        self.running_relay(room, ['relay-writer'])
        folder = str(room.root / 'relay' / 'relay-writer')
        request = json.dumps({'request_id': 'ack-2', 'kind': 'command', 'action': 'ack', 'payload': {'task_id': task}})
        self.assertEqual(self.ask(folder, request)[0], 0)
        code, out, err = self.ask(folder, request)
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)['ok'])


class RelayAskTidyTests(unittest.TestCase):
    start = RelayAskTests.start
    intake_task = RelayAskTests.intake_task
    running_relay = RelayAskTests.running_relay
    ask = RelayAskTests.ask
    """2026-09-24 (gate R3): the relay never moves requests; the helper tidies its own."""
    def test_answered_request_is_tidied_into_archive_by_the_helper(self):
        room = self.start()
        self.running_relay(room, ['relay-writer'])
        folder = room.root / 'relay' / 'relay-writer'
        code, out, err = self.ask(str(folder), json.dumps({'request_id': 'tidy-1', 'kind': 'state'}))
        self.assertEqual(code, 0, err)
        self.assertEqual([p.name for p in (folder / 'requests').iterdir()], [])
        self.assertEqual(len([p for p in (folder / 'archive').iterdir() if p.name.startswith('tidy-1.')]), 1)
        self.assertEqual(json.loads(out)['request_file'], 'tidy-1.json')


class RelayAskShaMismatchTests(unittest.TestCase):
    """R20: relay_ask must never accept an answer whose request_sha256 does not
    match the request it actually sent -- pins the check at crew/relay_ask.py's
    `if body.get('request_sha256') == digest:`. Exercises send() directly (no
    relay process) so a wrong-content answer can be placed deterministically."""

    def test_an_answer_with_the_wrong_request_sha256_is_never_accepted(self):
        folder = case_dir('relay-ask-sha-mismatch')
        (folder / 'requests').mkdir(parents=True)
        (folder / 'responses').mkdir()
        (folder / 'archive').mkdir()
        request = {'request_id': 'sha-check-1', 'kind': 'state'}
        real_digest = hashlib.sha256(json.dumps(request, ensure_ascii=False).encode('utf-8')).hexdigest()
        resp_path = folder / 'responses' / 'resp-sha-check-1.json'
        request_path = folder / 'requests' / 'sha-check-1.json'

        def racer():
            # Wait until relay_ask has actually written its request file (so
            # this never races the "set aside any pre-existing answer" step
            # send() does before it writes that file), then plant a WRONG
            # answer -- ok, but with a sha256 that does not match the request
            # actually sent -- and only much later replace it with the real one.
            deadline = time.time() + 5
            while not request_path.exists() and time.time() < deadline:
                time.sleep(0.01)
            wrong = {'ok': True, 'request_file': 'sha-check-1.json', 'request_sha256': '0' * 64,
                     'result': {'poisoned': True}}
            resp_path.write_text(json.dumps(wrong), encoding='utf-8')
            time.sleep(1.5)
            real = {'ok': True, 'request_file': 'sha-check-1.json', 'request_sha256': real_digest,
                    'result': {'poisoned': False}}
            resp_path.write_text(json.dumps(real), encoding='utf-8')

        thread = threading.Thread(target=racer, daemon=True)
        thread.start()
        self.addCleanup(lambda: thread.join(3))
        body = relay_ask.send(str(folder), request, timeout=6)
        self.assertFalse(body['result']['poisoned'], body)


class RelayAskAutoRequestIdTests(unittest.TestCase):
    """F9: request_id "auto" is replaced with a unique <seat>-<timestamp>-<hex>
    id before sending, and the id used is printed."""
    start = RelayAskTests.start
    intake_task = RelayAskTests.intake_task
    running_relay = RelayAskTests.running_relay
    ask = RelayAskTests.ask

    def test_auto_is_replaced_by_a_unique_seat_prefixed_id_and_printed(self):
        room = self.start()
        self.running_relay(room, ['relay-writer'])
        folder = str(room.root / 'relay' / 'relay-writer')
        code, out, err = self.ask(folder, json.dumps({'request_id': 'auto', 'kind': 'state'}))
        self.assertEqual(code, 0, err)
        match = re.search(r'request_id: (relay-writer-\d{14}-[0-9a-f]{6})\s*$', err.strip())
        self.assertIsNotNone(match, err)
        used_id = match.group(1)
        self.assertEqual(json.loads(out)['request_file'], used_id + '.json')

    def test_two_auto_requests_never_collide(self):
        room = self.start()
        self.running_relay(room, ['relay-writer'])
        folder = str(room.root / 'relay' / 'relay-writer')
        code1, out1, err1 = self.ask(folder, json.dumps({'request_id': 'auto', 'kind': 'state'}))
        code2, out2, err2 = self.ask(folder, json.dumps({'request_id': 'auto', 'kind': 'state'}))
        self.assertEqual(code1, 0, err1)
        self.assertEqual(code2, 0, err2)
        self.assertNotEqual(json.loads(out1)['request_file'], json.loads(out2)['request_file'])

    def test_a_literal_non_auto_request_id_is_left_alone(self):
        room = self.start()
        self.running_relay(room, ['relay-writer'])
        folder = str(room.root / 'relay' / 'relay-writer')
        code, out, err = self.ask(folder, json.dumps({'request_id': 'exactly-mine-1', 'kind': 'state'}))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['request_file'], 'exactly-mine-1.json')
        self.assertNotIn('request_id:', err)


if __name__ == '__main__':
    unittest.main()
