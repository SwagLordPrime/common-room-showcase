"""State Wall hotfix (task t228a7eed5, 2026-10-15): the client's room response cap is 16 MiB, not 2 MiB.

On 2026-10-15 the busiest seats' scoped /api/state was about 1.75-1.8 MB, close to the 2 MiB read cap in
common_room/participant.py, and a seat past the cap can still send but can no longer read. Written before the code:
on 223eda6 a 3 MiB response is refused, so test_three_mib_response_is_read fails there. A local stub HTTP server
on 127.0.0.1 serves a JSON body of a chosen size; no room, no network.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # lets `python tests/test_state_wall_cap.py` run from a tree root

import json, threading, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from common_room import participant
from common_room.participant import Participant

MIB = 1024 * 1024


def body_of(size):
    """A JSON object whose encoded length is exactly size bytes."""
    head = b'{"ok":true,"pad":"'
    tail = b'"}'
    return head + b'x' * (size - len(head) - len(tail)) + tail


class StubRoom:
    def __init__(self):
        self.size = 0
        self.seen = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                stub.seen.append(('GET', self.path, 0))
                self.reply()

            def do_POST(self):
                n = int(self.headers.get('Content-Length', 0))
                stub.seen.append(('POST', self.path, len(self.rfile.read(n))))
                self.reply()

            def reply(self):
                data = body_of(stub.size)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except OSError:
                    pass    # the client may close early once it has read past its cap

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.origin = 'http://127.0.0.1:%d' % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class StateWallCapTests(unittest.TestCase):
    def setUp(self):
        self.room = StubRoom()
        self.client = Participant(self.room.origin, 'stub-token')

    def tearDown(self):
        self.room.close()

    def test_three_mib_response_is_read(self):
        # RED on 223eda6: the 2 MiB cap refused this with 'Room response exceeds the client limit.'
        self.room.size = 3 * MIB
        result = self.client.state()
        self.assertTrue(result['ok'])
        self.assertEqual(len(result['pad']), 3 * MIB - len(b'{"ok":true,"pad":"') - len(b'"}'))

    def test_cap_is_sixteen_mib(self):
        self.assertEqual(participant.MAX_RESPONSE_BYTES, 16 * MIB)

    def test_exactly_the_cap_is_read(self):
        self.room.size = participant.MAX_RESPONSE_BYTES
        self.assertTrue(self.client.state()['ok'])

    def test_over_the_cap_is_refused(self):
        self.room.size = participant.MAX_RESPONSE_BYTES + 1
        with self.assertRaisesRegex(ValueError, 'Room response exceeds the client limit.'):
            self.client.state()

    def test_request_cap_is_unchanged(self):
        # the request side stays at 16 KiB: a body one byte over is refused before anything is sent
        self.room.size = 64
        overhead = len(participant.canonical({'action': 'note', 'payload': {'text': ''}, 'request_id': 'r'}).encode())
        with self.assertRaisesRegex(ValueError, 'Request exceeds the room limit.'):
            self.client.command('note', {'text': 'x' * (16384 - overhead + 1)}, 'r')
        self.assertEqual(self.room.seen, [])
        self.assertTrue(self.client.command('note', {'text': 'x' * (16384 - overhead)}, 'r')['ok'])
        self.assertEqual(self.room.seen, [('POST', '/api/command', 16384)])


if __name__ == '__main__':
    unittest.main(argv=sys.argv[:1])    # evidence.py passes the tree root as an argument; it names no test
