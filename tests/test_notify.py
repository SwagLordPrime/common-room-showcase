"""notify (plan turnons-2026-10-03): a phone push when something lands on the Needs Shane list. Ships OFF.

A fake ntfy server on 127.0.0.1 stands in for ntfy.sh; needs_shane is a stub (the real one comes with the panels
job). Covered: missing file, new ids only, de-duplication across a restart, quiet hours, batching, the observer
seat cannot post, and no topic, key, path or file content in any push body or log line."""
import io, json, sys, threading, types, unittest
from contextlib import redirect_stdout
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))      # lets `python tests/test_notify.py` run from a tree root (tools/evidence.py does)

from tests.helpers import case_dir
from common_room import notify
from common_room.crew_client import CrewClient, CrewError

TOPIC = 'fixture-topic-7Qx'          # stands in for Shane's private topic: it must appear nowhere but the URL path
TOKEN = 'Zq' * 22                    # 44 url-safe characters, shaped like a room key
SHIPPED = json.loads((ROOT / 'crew' / 'enrollment-crew-2026-09-24.json').read_text(encoding='utf-8'))
SEATS = ['claude-room', 'ferrule', 'tinker', 'vesper', 'crew-audit', 'hermes', 'local-a', 'local-b',
         'astra-room', 'astra-audit', 'claude-code', 'claude-code-b', 'dot', 'notify']


class FakeNtfy:
    """A loopback HTTP server that records every POST and answers with `status`."""
    def __init__(self, status=200):
        self.posts, self.status = [], status
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length') or 0)).decode('utf-8')
                outer.posts.append({'path': self.path, 'title': self.headers.get('Title'), 'body': body})
                self.send_response(outer.status)
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'{}')

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FixedCentral:
    """US Central wall clock fixed at `hour:minute`, whatever the epoch."""
    def __init__(self, hour, minute=0):
        self.wall = datetime(2026, 10, 5, hour, minute)

    def local(self, epoch):
        return self.wall


class Clock:
    def __init__(self, now=1000000.0):
        self.now = now

    def __call__(self):
        return self.now


def item(i, summary=None):
    return {'id': 'decision:t' + str(i) + ':f2', 'kind': 'decision', 'since': 1.0,
            'summary': summary or ('seat asks on job ' + str(i)), 'action': 'crew-shane decide t' + str(i) + ' --response "x"'}


class Harness:
    """A Notifier with a stub room, a stub needs list, a fake ntfy and a fake clock."""
    def __init__(self, test, quiet=None, batch=10, status=200, state_dir=None, server=None):
        self.ntfy = server or FakeNtfy(status)
        if server is None:
            test.addCleanup(self.ntfy.close)
        self.items, self.lines, self.clock = [], [], Clock()
        self.state_dir = state_dir or case_dir('notify-state')
        self.config = {'server': self.ntfy.url, 'topic': TOPIC, 'quiet_hours': quiet, 'batch_minutes': batch}
        self.central = FixedCentral(12)
        client = types.SimpleNamespace(state=lambda: {'server_time': self.clock.now})
        self.notifier = notify.Notifier(self.config, client, lambda snapshot: list(self.items), self.state_dir,
                                        clock=self.clock, central=self.central, log=self.lines.append)

    def cycle(self):
        return self.notifier.cycle()


class ConfigTests(unittest.TestCase):
    def write(self, data):
        path = case_dir('notify-config') / 'notify.json'
        path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding='utf-8')
        return path

    def test_missing_file_is_off_and_exits_0_with_one_line(self):
        out = []
        missing = case_dir('notify-missing') / 'notify.json'
        self.assertEqual(notify.main(['--config', str(missing), '--state-dir', str(case_dir('notify-s'))], out=out.append), 0)
        self.assertEqual(out, ['notify off: no crew\\notify.json'])
        self.assertIsNone(notify.load_config(missing))

    def test_defaults_and_a_good_file(self):
        cfg = notify.load_config(self.write({'topic': TOPIC}))
        self.assertEqual(cfg, {'server': 'https://ntfy.sh', 'topic': TOPIC, 'quiet_hours': None, 'batch_minutes': 10})
        cfg = notify.load_config(self.write({'server': 'https://ntfy.example.org/', 'topic': TOPIC,
                                             'quiet_hours': '23:00-08:00', 'batch_minutes': 5}))
        self.assertEqual((cfg['server'], cfg['quiet_hours'], cfg['batch_minutes']), ('https://ntfy.example.org', (1380, 480), 5))

    def test_the_example_file_ships_with_the_placeholder_and_is_refused_as_is(self):
        example = json.loads((ROOT / 'crew' / 'notify.example.json').read_text(encoding='utf-8'))
        self.assertEqual(example['topic'], 'CHANGE-ME')
        with self.assertRaisesRegex(notify.ConfigError, 'example value'):
            notify.load_config(ROOT / 'crew' / 'notify.example.json')
        self.assertFalse((ROOT / 'crew' / 'notify.json').exists())      # the build never creates Shane's file

    def test_notify_json_is_ignored_by_git(self):
        lines = (ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()
        self.assertIn('crew/notify.json', lines)

    def test_bad_files_are_refused_without_ever_naming_the_topic(self):
        bad = [{'topic': TOPIC, 'server': 'http://ntfy.sh'}, {'topic': TOPIC, 'server': 'https://ntfy.sh/path'},
               {'topic': TOPIC, 'server': 'ftp://ntfy.sh'}, {'topic': TOPIC, 'server': 'https://u:p@ntfy.sh'},
               {'topic': TOPIC + '/x'}, {'topic': ''}, {}, {'topic': TOPIC, 'quiet_hours': '23-08'},
               {'topic': TOPIC, 'quiet_hours': '08:00-08:00'}, {'topic': TOPIC, 'batch_minutes': 0},
               {'topic': TOPIC, 'batch_minutes': '10'}, {'topic': TOPIC, 'email': 'x'}, [TOPIC], 'not json ' + TOPIC]
        for data in bad:
            with self.subTest(data=data):
                path = self.write(data)
                with self.assertRaises(notify.ConfigError) as raised:
                    notify.load_config(path)
                self.assertNotIn(TOPIC, str(raised.exception))
                out = []
                self.assertEqual(notify.main(['--config', str(path)], out=out.append), 2)
                self.assertNotIn(TOPIC, ' '.join(out))

    def test_plain_http_only_to_this_pc(self):
        self.assertEqual(notify.load_config(self.write({'topic': TOPIC, 'server': 'http://127.0.0.1:8080'}))['server'],
                         'http://127.0.0.1:8080')


class MessageTests(unittest.TestCase):
    def test_count_up_to_three_summaries_and_the_closing_line(self):
        body = notify.message([item(i) for i in range(5)])
        lines = body.splitlines()
        self.assertEqual(lines[0], '5 things need you:')
        self.assertEqual(lines[1:4], ['- seat asks on job 0', '- seat asks on job 1', '- seat asks on job 2'])
        self.assertEqual(lines[4], '...and 2 more on the board.')
        self.assertEqual(lines[-1], 'Answer in claude-room or reply to the email.')
        self.assertEqual(notify.message([item(1)]).splitlines()[0], '1 thing needs you:')

    def test_never_an_action_a_path_a_key_or_file_contents(self):
        nasty = item(1, 'QUESTION about D:\\Work Area\\Artifacts\\common-room\\tree\\.runtime\\handoffs\\'
                        'keeper-credential.json and key ' + TOKEN + ' see Artifacts/orchestra/x.md and C:secret commit '
                        'c4462c3ab18ff235aa7f1dc89747060bd3fb2978')
        body = notify.message([nasty])
        for leak in ('\\', '/', 'credential.json', TOKEN, 'crew-shane', '--response', 'C:secret'):
            self.assertNotIn(leak, body)
        self.assertIn('[path]', body)
        self.assertIn('[hidden]', body)
        self.assertIn('c4462c3ab18ff235aa7f1dc89747060bd3fb2978'[:30], body)       # a commit stays readable
        self.assertLessEqual(max(len(line) for line in body.splitlines()), notify.SUMMARY_LIMIT + 2)


class CycleTests(unittest.TestCase):
    def test_pushes_only_new_ids(self):
        h = Harness(self)
        h.items = [item(1), item(2)]
        self.assertEqual(h.cycle()['pushed'], 2)
        self.assertEqual(len(h.ntfy.posts), 1)
        post = h.ntfy.posts[0]
        self.assertEqual((post['path'], post['title']), ('/' + TOPIC, 'Common Room needs you'))
        self.assertTrue(post['body'].startswith('2 things need you:'))
        h.clock.now += 3600
        self.assertEqual(h.cycle()['pushed'], 0)                      # the same two: nothing new
        h.items.append(item(3))
        self.assertEqual(h.cycle()['pushed'], 1)
        self.assertTrue(h.ntfy.posts[1]['body'].startswith('1 thing needs you:\n- seat asks on job 3'))
        self.assertEqual(len(h.ntfy.posts), 2)

    def test_de_duplicated_across_a_restart(self):
        h = Harness(self)
        h.items = [item(1)]
        h.cycle()
        again = Harness(self, state_dir=h.state_dir, server=h.ntfy)    # a new process, the same state folder
        again.items = [item(1)]
        again.clock.now = h.clock.now + 7200
        self.assertEqual(again.cycle()['pushed'], 0)
        self.assertEqual(len(h.ntfy.posts), 1)
        saved = json.loads((h.state_dir / 'state.json').read_text(encoding='utf-8'))
        self.assertIn(item(1)['id'], saved['seen'])
        self.assertNotIn(TOPIC, json.dumps(saved))

    def test_quiet_hours_hold_the_push_until_they_end(self):
        h = Harness(self, quiet=(23 * 60, 8 * 60))
        h.items = [item(1)]
        for hour, minute in ((23, 30), (2, 0), (7, 59)):
            h.central.wall = datetime(2026, 10, 5, hour, minute)
            self.assertEqual(h.cycle()['held'], 'quiet hours')
        self.assertEqual(h.ntfy.posts, [])
        h.items.append(item(2))
        h.central.wall = datetime(2026, 10, 6, 8, 0)
        self.assertEqual(h.cycle()['pushed'], 2)                      # one push for everything that waited
        self.assertEqual(len(h.ntfy.posts), 1)

    def test_quiet_hours_inside_one_day(self):
        self.assertTrue(notify.in_quiet_hours((13 * 60, 14 * 60), datetime(2026, 1, 1, 13, 30)))
        self.assertFalse(notify.in_quiet_hours((13 * 60, 14 * 60), datetime(2026, 1, 1, 14, 0)))
        self.assertFalse(notify.in_quiet_hours(None, datetime(2026, 1, 1, 3, 0)))

    def test_at_most_one_push_per_batch_window(self):
        h = Harness(self, batch=10)
        h.items = [item(1)]
        h.cycle()
        for minute in (1, 4, 9):
            h.clock.now += 60 * (minute - (0 if minute == 1 else prev))
            prev = minute
            h.items.append(item(10 + minute))
            self.assertEqual(h.cycle()['held'], 'batch window')
        self.assertEqual(len(h.ntfy.posts), 1)
        h.clock.now += 61                                               # 10 minutes and a second after the push
        result = h.cycle()
        self.assertEqual(result['pushed'], 3)
        self.assertTrue(h.ntfy.posts[1]['body'].startswith('3 things need you:'))

    def test_an_item_gone_before_the_push_is_not_pushed(self):
        h = Harness(self, quiet=(0, 23 * 60 + 59))
        h.items = [item(1), item(2)]
        h.cycle()
        h.items = [item(2)]
        h.notifier.config['quiet_hours'] = None
        self.assertEqual(h.cycle()['pushed'], 1)
        self.assertIn('job 2', h.ntfy.posts[0]['body'])
        self.assertNotIn('job 1', h.ntfy.posts[0]['body'])

    def test_a_failed_push_is_tried_again_and_says_only_the_status(self):
        h = Harness(self, status=500)
        h.items = [item(1)]
        self.assertEqual(h.cycle()['held'], 'push failed')
        self.assertIn('notify: push failed (HTTP 500); trying again next cycle.', h.lines)
        h.ntfy.status = 200
        self.assertEqual(h.cycle()['pushed'], 1)
        self.assertEqual(len(h.ntfy.posts), 2)

    def test_an_unreachable_server_never_prints_the_url_or_topic(self):
        ok, reason = notify.post_ntfy('http://127.0.0.1:9', TOPIC, notify.TITLE, 'x', timeout=3)
        self.assertFalse(ok)
        self.assertNotIn(TOPIC, reason)
        self.assertNotIn('127.0.0.1', reason)

    def test_no_secret_in_any_body_or_log_line(self):
        h = Harness(self)
        h.items = [item(1, 'see ' + TOKEN + ' in D:\\Work Area\\x\\.runtime\\handoffs\\keeper-credential.json')]
        h.cycle()
        everything = ' '.join(p['body'] + p['title'] for p in h.ntfy.posts) + ' '.join(h.lines)
        for leak in (TOPIC, TOKEN, 'credential', '\\', h.ntfy.url, 'crew-shane'):
            self.assertNotIn(leak, everything)

    def test_a_damaged_state_file_is_set_aside_not_trusted(self):
        h = Harness(self)
        (h.state_dir).mkdir(parents=True, exist_ok=True)
        (h.state_dir / 'state.json').write_text('{oops', encoding='utf-8')
        h.items = [item(1)]
        self.assertEqual(h.cycle()['pushed'], 1)
        self.assertTrue((h.state_dir / 'state.json.bad').exists())
        self.assertTrue(any('damaged' in line for line in h.lines))


class ObserverSeatTests(unittest.TestCase):
    """The notify seat in a real room: an observer in orchestra only; it reads, and every command is refused."""
    def setUp(self):
        from common_room.activation import start_room
        self.room = start_room(case_dir('notify-room'), enrollment=SHIPPED, selected=SEATS)
        self.addCleanup(self.room.close)

    def test_enrolled_as_an_observer_in_orchestra_only(self):
        self.assertEqual([a['role'] for a in SHIPPED['actors'] if a['id'] == 'notify'], ['observer'])
        self.assertEqual([a['id'] for a in SHIPPED['authorities'] if 'notify' in a['members']], ['orchestra'])

    def test_it_reads_the_room_and_cannot_post(self):
        client = CrewClient(self.room.root / 'notify-credential.json', expected_actor='notify')
        state = client.state()
        self.assertIn('server_time', state)
        for action, payload in (('note', {'authority_id': 'orchestra', 'body': 'hello'}),
                                ('ack', {'task_id': 't' + '0' * 31}), ('stop_all', {'reason': 'x'})):
            with self.subTest(action=action):
                with self.assertRaises(CrewError):
                    client.command(action, payload, 'notify-' + action)
        grants = self.room.store._snapshot(self.room.store.people['keeper'])
        self.assertFalse(any(e['actor'] == 'notify' for e in grants[0]))      # not one event by the observer

    def test_one_real_cycle_end_to_end_then_nothing_new(self):
        ntfy = FakeNtfy()
        self.addCleanup(ntfy.close)
        cfg = case_dir('notify-e2e') / 'notify.json'
        cfg.write_text(json.dumps({'server': ntfy.url, 'topic': TOPIC}), encoding='utf-8')
        seen = []

        def needs_shane(snapshot):          # stands in for common_room.needs (the panels job)
            seen.append(snapshot['server_time'])
            return [item(1)]
        fake = types.ModuleType('common_room.needs')
        fake.needs_shane = needs_shane
        sys.modules['common_room.needs'] = fake
        self.addCleanup(sys.modules.pop, 'common_room.needs', None)
        state_dir = case_dir('notify-e2e-state')
        args = ['--config', str(cfg), '--credential', str(self.room.root / 'notify-credential.json'),
                '--state-dir', str(state_dir)]
        out = []
        self.assertEqual(notify.main(args, out=out.append), 0)
        self.assertEqual(notify.main(args, out=out.append), 0)
        self.assertEqual(len(seen), 2)
        self.assertEqual(len(ntfy.posts), 1)
        self.assertEqual(out, ['notify: 1 new item(s) on the Needs Shane list.', 'notify: pushed 1 item(s).'])

    def test_without_needs_py_it_says_so_and_sends_nothing(self):
        ntfy = FakeNtfy()
        self.addCleanup(ntfy.close)
        cfg = case_dir('notify-noneeds') / 'notify.json'
        cfg.write_text(json.dumps({'server': ntfy.url, 'topic': TOPIC}), encoding='utf-8')
        saved = sys.modules.pop('common_room.needs', None)
        sys.modules['common_room.needs'] = None                # makes the import fail like a missing file
        self.addCleanup(sys.modules.pop, 'common_room.needs', None)
        if saved is not None:
            self.addCleanup(sys.modules.__setitem__, 'common_room.needs', saved)
        out = []
        self.assertEqual(notify.main(['--config', str(cfg)], out=out.append), 2)
        self.assertIn('needs.py', out[0])
        self.assertEqual(ntfy.posts, [])


if __name__ == '__main__':
    unittest.main(argv=sys.argv[:1])    # evidence.py passes the tree root as an argument; it names no test
