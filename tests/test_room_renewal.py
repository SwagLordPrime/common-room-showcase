"""Self-renewing room: fresh keys before the eight-hour cap, same port, same history.

Fictional participants only. A mutable fake clock is threaded through start_room (store,
server, credential policy) and an injected wait advances it, so no test sleeps for real
except the one that proves a real stop event is honored promptly.
"""
import contextlib, hashlib, http.client, inspect, io, json, os, socket, sqlite3, subprocess, sys, threading, time, unittest
from contextlib import closing
from unittest.mock import patch
from tests.helpers import case_dir, ROOT
from tests.test_activation import CONFIG

BASELINE_ACTIVATION_TESTS_SHA256 = '70516048f41b4591698ca5fc6264d39521b8278af48f449a76b5682f24a3ffe0'  # git show 6b945a2:tests/test_activation.py | sha256sum
LOG_FIELDS = {'cycle', 'instance_id', 'started_at', 'expires_at', 'origin'}
SEATS = ('test-writer', 'test-reader')


class FakeClock:
    def __init__(self, start=None):
        self.now = float(int(time.time())) if start is None else float(start)
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.now

    def advance(self, seconds):
        with self.lock:
            self.now += seconds


def live_instance(origin):
    c = http.client.HTTPConnection('127.0.0.1', int(origin.rsplit(':', 1)[1]), timeout=5)
    try:
        c.request('GET', '/api/instance')
        response = c.getresponse()
        return json.loads(response.read())['instance_id']
    finally:
        c.close()


def event_count(root):
    with closing(sqlite3.connect((root / 'room.sqlite').as_uri() + '?mode=ro', uri=True)) as c:
        return c.execute('SELECT count(*) FROM events').fetchone()[0]


def login_refusal(origin, token):
    from common_room.participant import Participant
    try:
        Participant(origin, token).login()
    except ValueError as exc:
        return str(exc)
    return None


class RoomRenewalTests(unittest.TestCase):
    def renew(self, root, cycles=3, selected=SEATS, lifetime=600, margin=60, enrollment=CONFIG, during=None, clock=None):
        """Run run_renewing; while each cycle's room is live, snapshot it, then advance the clock."""
        from common_room.activation import run_renewing
        clock = clock or FakeClock()
        seen, lines = [], []

        def wait(seconds):
            descriptor = json.loads((root / 'connection.json').read_text(encoding='utf-8'))
            snap = {'clock': clock(), 'seconds': seconds, 'descriptor': descriptor,
                    'live_instance': live_instance(descriptor['origin']),
                    'credentials': {p.name[:-len('-credential.json')]: json.loads(p.read_text(encoding='utf-8'))
                                    for p in root.glob('*-credential.json')},
                    'enrollment': (root / 'enrollment.json').read_bytes(),
                    'background': json.loads((root / 'background-process.json').read_text(encoding='utf-8'))}
            seen.append(snap)
            if during:
                during(len(seen), snap)
            clock.advance(seconds)

        summaries = run_renewing(root, list(selected), enrollment, lifetime=lifetime, margin=margin, clock=clock,
                                 wait=wait, max_cycles=cycles, log=lines.append)
        return summaries, seen, lines

    def test_lifetime_bounds_and_default_unchanged(self):
        from common_room.activation import start_room
        self.assertEqual(inspect.signature(start_room).parameters['lifetime'].default, 28800)
        root = case_dir('renew-lifetime')
        for bad in [59, 28801, 0, -60, True, 600.0, '600', None]:
            with self.assertRaises(ValueError):
                start_room(root, CONFIG, ['test-writer'], clock=FakeClock(), lifetime=bad)
        self.assertEqual(list(root.iterdir()), [], 'an invalid lifetime must be refused before anything is written')
        for lifetime in (60, 28800, None):
            clock = FakeClock()
            kwargs = {} if lifetime is None else {'lifetime': lifetime}
            r = start_room(case_dir('renew-lifetime'), CONFIG, ['test-writer'], clock=clock, **kwargs)
            self.addCleanup(r.close)
            expected = 28800 if lifetime is None else lifetime
            started = r.descriptor['started_at']
            self.assertEqual(started, clock())
            self.assertEqual(r.expiry, started + expected)
            self.assertEqual(r.descriptor['expires_at'], started + expected)
            credential = json.loads((r.root / 'test-writer-credential.json').read_text(encoding='utf-8'))
            self.assertEqual(credential['expires_at'], started + expected)
            token = credential['token']
            self.assertTrue(r.server.credential_policy.valid(token, started + expected - 1))
            self.assertFalse(r.server.credential_policy.valid(token, started + expected))
            r.close()

    def test_margin_bounds(self):
        from common_room.activation import run_renewing
        root = case_dir('renew-margin')

        def never(seconds):
            raise AssertionError('invalid settings must be refused before any room starts')

        for lifetime, margin, cycles in [(600, 29, 1), (600, 570, 1), (600, 571, 1), (600, True, 1), (600, 60.0, 1),
                                         (600, '60', 1), (600, None, 1), (60, 30, 1), (59, 30, 1), (28801, 300, 1),
                                         (600, 60, 0), (600, 60, True), (600, 60, 1.0)]:
            with self.assertRaises(ValueError):
                run_renewing(root, ['test-writer'], CONFIG, lifetime=lifetime, margin=margin, clock=FakeClock(),
                             wait=never, max_cycles=cycles, log=lambda line: None)
        self.assertEqual(list(root.iterdir()), [])
        with self.assertRaises(ValueError):
            run_renewing(root, 'test-writer', CONFIG, lifetime=600, margin=60, clock=FakeClock(), wait=never,
                         max_cycles=1, log=lambda line: None)
        self.assertEqual(list(root.iterdir()), [])
        for margin, expected_wait in [(30, 570), (569, 31)]:
            summaries, seen, _ = self.renew(case_dir('renew-margin'), cycles=1, margin=margin)
            self.assertEqual(len(summaries), 1)
            self.assertEqual([s['seconds'] for s in seen], [expected_wait])

    def test_three_cycles_same_port_new_instance_and_keys(self):
        clock = FakeClock()
        t0 = clock()
        summaries, seen, lines = self.renew(case_dir('renew-cycles'), clock=clock)
        self.assertEqual(len(seen), 3)
        instances = [s['descriptor']['instance_id'] for s in seen]
        self.assertEqual(len(set(instances)), 3)
        self.assertEqual([s['live_instance'] for s in seen], instances, 'the live server must be that cycle\'s room')
        self.assertEqual([s['instance_id'] for s in summaries], instances)
        self.assertEqual(len({s['descriptor']['origin'] for s in seen}), 1, 'every cycle must reuse the same port')
        self.assertEqual([s['clock'] for s in seen], [t0, t0 + 540, t0 + 1080])
        self.assertEqual([s['seconds'] for s in seen], [540, 540, 540])
        for seat in ('keeper',) + SEATS:
            tokens = [s['credentials'][seat]['token'] for s in seen]
            self.assertEqual(len(set(tokens)), 3, seat + ' must receive a fresh key every cycle')
            for s in seen:
                self.assertEqual(s['credentials'][seat]['expires_at'], s['clock'] + 600)
                self.assertEqual(s['credentials'][seat]['origin'], s['descriptor']['origin'])
        for s in seen:
            self.assertEqual(s['descriptor']['started_at'], s['clock'])
            self.assertEqual(s['descriptor']['expires_at'], s['clock'] + 600)
        self.assertEqual(lines.count('Keys rotate every 540s; clients re-read their credential files.'), 3)
        self.assertEqual(sum(line.startswith('Common Room: ' + seen[0]['descriptor']['origin']) for line in lines), 3)
        self.assertEqual(sum(line.startswith('Issued: keeper, test-writer, test-reader.') for line in lines), 3)

    def test_old_keys_invalid_after_renewal(self):
        issued, refused = [], []

        def during(cycle, snap):
            origin = snap['descriptor']['origin']
            for seat, credential in snap['credentials'].items():
                self.assertIsNone(login_refusal(origin, credential['token']), seat + ' current key must sign in')
            for old_cycle, seat, token in issued:
                reason = login_refusal(origin, token)
                self.assertIsNotNone(reason, 'cycle %d key for %s still works in cycle %d' % (old_cycle, seat, cycle))
                self.assertTrue(reason.startswith('unauthenticated'), reason)
                refused.append((old_cycle, seat, cycle))
            issued.extend((cycle, seat, c['token']) for seat, c in snap['credentials'].items())

        self.renew(case_dir('renew-oldkeys'), during=during)
        self.assertEqual(len(refused), 3 + 6, 'three stale keys in cycle 2 and six in cycle 3')

    def test_history_and_enrollment_preserved_across_cycles(self):
        from common_room.crew_client import CrewClient
        root = case_dir('renew-history')
        kept = {}
        message = {'title': 'Fixture', 'body': 'Survives renewal', 'recipient': 'test-writer', 'authority_id': 'test-one',
                   'kind': 'handoff.record', 'acceptance': 'Inspect report'}
        intake = {'source_namespace': 'estate', 'external_reference': 'renew-fixture', 'message': message}

        def during(cycle, snap):
            if cycle == 1:
                kept['keeper'] = CrewClient(root / 'keeper-credential.json', sleep=lambda s: None)
                writer = CrewClient(root / 'test-writer-credential.json', sleep=lambda s: None)
                kept['intake'] = kept['keeper'].command('intake', intake, 'renew-intake-1')
                task_id = kept['intake']['task']['id']
                kept['keeper'].command('deliver', {'task_id': task_id}, 'renew-deliver-1')
                writer.command('ack', {'task_id': task_id}, 'renew-ack-1')
                kept['task'] = kept['keeper'].task_in(kept['keeper'].state(), task_id)
                self.assertEqual(kept['task']['status'], 'acknowledged')
                kept['events'] = event_count(root)
                kept['enrollment'] = snap['enrollment']
                self.assertEqual(json.loads(snap['enrollment']), CONFIG)
                return
            self.assertEqual(event_count(root), kept['events'], 'renewal must not append events')
            self.assertEqual(snap['enrollment'], kept['enrollment'], 'enrollment.json bytes must not change')
            state = kept['keeper'].state()
            self.assertEqual(state['instance_id'], snap['descriptor']['instance_id'])
            task = kept['keeper'].task_in(state, kept['task']['id'])
            self.assertEqual(task, kept['task'])
            if cycle == 3:
                self.assertEqual(kept['keeper'].command('intake', intake, 'renew-intake-1'), kept['intake'],
                                 'stored receipts must answer a retried request after renewal')
                self.assertEqual(event_count(root), kept['events'])
                kept['checked'] = True

        self.renew(root, during=during)
        self.assertTrue(kept.get('checked'))
        self.assertEqual(event_count(root), kept['events'])

    def test_crew_client_survives_renewal(self):
        from common_room.crew_client import CrewClient
        root = case_dir('renew-client')
        holder, observed = [], []

        def during(cycle, snap):
            if not holder:
                holder.append(CrewClient(root / 'test-writer-credential.json', sleep=lambda s: None))
            client = holder[0]
            state = client.state()
            self.assertEqual(state['actor'], {'id': 'test-writer', 'role': 'worker'})
            receipt = client.command('note', {'authority_id': 'test-one', 'body': 'cycle %d' % cycle}, 'renew-note-%d' % cycle)
            observed.append((state['instance_id'], client._participant.token, receipt['seq']))
            self.assertEqual(state['instance_id'], snap['descriptor']['instance_id'])
            self.assertEqual(client._participant.token, snap['credentials']['test-writer']['token'])

        self.renew(root, during=during)
        self.assertEqual(len(holder), 1, 'the client must never be recreated')
        self.assertEqual(len({o[0] for o in observed}), 3)
        self.assertEqual(len({o[1] for o in observed}), 3)
        self.assertEqual([o[2] for o in observed], sorted({o[2] for o in observed}), 'each cycle wrote a new note')

    def test_revocation_persists_across_cycles(self):
        from common_room.crew_client import CrewClient, CrewError
        from common_room.participant import Participant
        root = case_dir('renew-revoke')
        kept = {}

        def during(cycle, snap):
            origin = snap['descriptor']['origin']
            if cycle == 1:
                keeper = Participant(origin, snap['credentials']['keeper']['token'])
                keeper.login()
                self.assertEqual(keeper.handoff('revoke', {'actor_id': 'test-reader'}), {'revoked': 'test-reader'})
                kept['reader_file'] = (root / 'test-reader-credential.json').read_bytes()
                self.assertIsNotNone(login_refusal(origin, snap['credentials']['test-reader']['token']))
                return
            self.assertEqual((root / 'test-reader-credential.json').read_bytes(), kept['reader_file'],
                             'a revoked seat must not be re-issued a key')
            state = json.loads((root / 'activation-state.json').read_text(encoding='utf-8'))
            self.assertEqual(state['revoked'], ['test-reader'])
            reader = CrewClient(root / 'test-reader-credential.json', sleep=lambda s: None)
            with self.assertRaises(CrewError) as ctx:
                reader.state()
            self.assertEqual(ctx.exception.code, 'unauthenticated')
            self.assertIsNone(login_refusal(origin, snap['credentials']['test-writer']['token']))
            kept.setdefault('checked', []).append(cycle)

        _, _, lines = self.renew(root, during=during)
        self.assertEqual(kept['checked'], [2, 3])
        issued = [line for line in lines if line.startswith('Issued: ')]
        self.assertEqual(len(issued), 3)
        self.assertIn('test-reader', issued[0])
        self.assertTrue(all('test-reader' not in line for line in issued[1:]), issued)

    def test_background_process_and_renewal_log_written_without_tokens(self):
        root = case_dir('renew-records')
        tokens = set()

        def during(cycle, snap):
            self.assertEqual(snap['background'], {'version': 1, 'pid': os.getpid(), 'instance_id': snap['descriptor']['instance_id']})
            tokens.update(c['token'] for c in snap['credentials'].values())

        summaries, seen, lines = self.renew(root, during=during)
        self.assertEqual(len(tokens), 9)
        background = (root / 'background-process.json').read_text(encoding='utf-8')
        self.assertEqual(json.loads(background)['instance_id'], seen[-1]['descriptor']['instance_id'])
        log_text = (root / 'renewal-log.jsonl').read_text(encoding='utf-8')
        records = [json.loads(line) for line in log_text.splitlines()]
        self.assertEqual(len(records), 3)
        for n, (record, snap) in enumerate(zip(records, seen), 1):
            self.assertEqual(set(record), LOG_FIELDS)
            self.assertEqual(record, {'cycle': n, 'instance_id': snap['descriptor']['instance_id'],
                                      'started_at': snap['clock'], 'expires_at': snap['clock'] + 600,
                                      'origin': snap['descriptor']['origin']})
        self.assertEqual(summaries, records)
        console = '\n'.join(lines)
        for token in tokens:
            for name, body in [('background-process.json', background), ('renewal-log.jsonl', log_text),
                               ('console', console), ('summaries', json.dumps(summaries))]:
                self.assertNotIn(token, body, 'a key leaked into ' + name)
        self.assertNotIn('token', log_text + background)

    def test_stop_event_ends_loop_promptly(self):
        from common_room.activation import run_renewing, start_room
        # Real clock and the default wait: a set stop event ends the loop within a second or so.
        root = case_dir('renew-stop')
        stop, result, errors, lines = threading.Event(), [], [], []

        def run():
            try:
                result.append(run_renewing(root, ['test-writer'], CONFIG, lifetime=3600, margin=300, stop=stop, log=lines.append))
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not (root / 'renewal-log.jsonl').exists() and worker.is_alive():
            time.sleep(0.05)
        self.assertTrue((root / 'renewal-log.jsonl').exists(), errors)
        descriptor = json.loads((root / 'connection.json').read_text(encoding='utf-8'))
        self.assertEqual(live_instance(descriptor['origin']), descriptor['instance_id'])
        started = time.monotonic()
        stop.set()
        worker.join(5)
        elapsed = time.monotonic() - started
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertLess(elapsed, 2.0)
        self.assertEqual(len(result[0]), 1)
        with self.assertRaises(ConnectionRefusedError):
            live_instance(descriptor['origin'])
        again = start_room(root, selected=['test-writer'])  # launcher lock was released
        again.close()
        # Injected wait: stop set during cycle 2 ends the loop without a third start.
        root = case_dir('renew-stop')
        stop, clock, calls = threading.Event(), FakeClock(), []

        def wait(seconds):
            calls.append(seconds)
            if len(calls) == 2:
                stop.set()
            else:
                clock.advance(seconds)

        summaries = run_renewing(root, ['test-writer'], CONFIG, lifetime=600, margin=60, clock=clock, wait=wait,
                                 stop=stop, log=lambda line: None)
        self.assertEqual(len(summaries), 2)
        self.assertEqual(calls, [540, 540])
        self.assertEqual(len((root / 'renewal-log.jsonl').read_text(encoding='utf-8').splitlines()), 2)

    def test_cli_renew_requires_headless(self):
        from common_room import activation
        root = case_dir('renew-cli')
        refused = [['--renew'], ['--renew', '--lifetime', '600'], ['--headless', '--renew-margin', '60'],
                   ['--headless', '--renew', '--lifetime', '59'], ['--lifetime', '28801'],
                   ['--headless', '--renew', '--lifetime', '600', '--renew-margin', '570'],
                   ['--headless', '--renew', '--renew-margin', '29']]
        for argv in refused:
            with patch.object(activation, 'run_renewing') as renew, patch.object(activation, 'start_room') as start, \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(SystemExit) as ctx:
                    activation.main(['--root', str(root)] + argv)
            self.assertEqual(ctx.exception.code, 2, argv)
            renew.assert_not_called()
            start.assert_not_called()
            if argv == ['--renew']:
                self.assertIn('--headless', err.getvalue())
        self.assertEqual(list(root.iterdir()), [])
        # The real command line refuses too, before any room starts.
        done = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-m', 'common_room.activation', '--root', str(root), '--renew'],
                              cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 2)
        self.assertIn('--headless', done.stderr)
        self.assertEqual(list(root.iterdir()), [])
        # Wiring: --headless --renew hands parsed participants and enrollment to run_renewing.
        source = root / 'enrollment-input.json'
        source.write_text(json.dumps(CONFIG), encoding='utf-8')
        with patch.object(activation, 'run_renewing') as renew, patch.object(activation, 'start_room') as start:
            activation.main(['--root', str(root), '--enrollment', str(source), '--participants', *SEATS,
                             '--headless', '--renew', '--lifetime', '7200', '--renew-margin', '600'])
            activation.main(['--root', str(root), '--headless', '--renew'])
        start.assert_not_called()
        self.assertEqual(renew.call_count, 2)
        first, second = renew.call_args_list
        self.assertEqual(first.args[:3], (root, list(SEATS), CONFIG))
        self.assertEqual((first.kwargs['lifetime'], first.kwargs['margin']), (7200, 600))
        self.assertEqual(second.args[:3], (root, [], None))
        self.assertEqual((second.kwargs['lifetime'], second.kwargs['margin']), (28800, 300))
        # Console mode and plain --headless are unchanged: one start, default lifetime, run_console.
        for argv, headless in [([], False), (['--headless'], True)]:
            with patch.object(activation, 'start_room') as start, patch.object(activation, 'run_console') as console, \
                    patch.object(activation, 'run_renewing') as renew:
                activation.main(['--root', str(root)] + argv)
            start.assert_called_once_with(root, None, [], lifetime=28800)
            console.assert_called_once_with(start.return_value, headless=headless)
            renew.assert_not_called()

    def test_start_failure_retries_then_raises_keeping_descriptor(self):
        from common_room.activation import run_renewing, start_room
        root = case_dir('renew-retry')
        r = start_room(root, CONFIG, ['test-writer'])
        port, origin = r.server.server_address[1], r.server.origin
        r.close()
        names = ['connection.json', 'keeper-credential.json', 'test-writer-credential.json', 'activation-state.json']
        before = {n: (root / n).read_bytes() for n in names}
        clock, waits = FakeClock(), []

        def wait(seconds):
            waits.append(seconds)
            clock.advance(seconds)

        with socket.socket() as held:
            held.bind(('127.0.0.1', port))
            held.listen()
            with self.assertRaises(OSError):
                run_renewing(root, ['test-writer'], clock=clock, wait=wait, max_cycles=1, log=lambda line: None)
        self.assertEqual(len(waits), 5, 'five retries after the first attempt, each after a short wait')
        self.assertTrue(all(0 < w <= 5 for w in waits), waits)
        self.assertEqual({n: (root / n).read_bytes() for n in names}, before, 'a failed start must not rewrite published files')
        self.assertFalse((root / 'renewal-log.jsonl').exists())
        self.assertFalse((root / 'background-process.json').exists())
        # The port frees up during the retries: the room comes back on the same port.
        held = socket.socket()
        held.bind(('127.0.0.1', port))
        held.listen()
        self.addCleanup(held.close)
        waits.clear()

        def freeing_wait(seconds):
            waits.append(seconds)
            if len(waits) == 2:
                held.close()
            clock.advance(seconds)

        summaries = run_renewing(root, ['test-writer'], clock=clock, wait=freeing_wait, max_cycles=1, log=lambda line: None)
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]['origin'], origin)
        self.assertEqual(len(waits), 3)
        self.assertEqual(waits[2], 28800 - 300, 'defaults: eight-hour keys renewed five minutes early')

    def test_existing_activation_tests_untouched(self):
        digest = hashlib.sha256((ROOT / 'tests' / 'test_activation.py').read_bytes()).hexdigest()
        self.assertEqual(digest, BASELINE_ACTIVATION_TESTS_SHA256)


if __name__ == '__main__':
    unittest.main()
