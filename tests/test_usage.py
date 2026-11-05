"""The usage meter (common_room/usage.py) and the two workers that feed it.

RED note: written before the module existed, so its first run on the base commit fails at
import ("cannot import name 'usage' from 'common_room'").
Every model is a fake Ollama on 127.0.0.1 and every agent is tests/fake_agent.py; tests.helpers
refuses any other socket.
"""
import sys
from pathlib import Path
if __name__ == '__main__':          # runnable as a script too (tools/evidence.py runs `python tests/<file> .`)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import json, os, threading, unittest, uuid
from datetime import datetime, timezone
from unittest import mock
from tests.helpers import case_dir
from tests.test_agent_worker import FAKE as FAKE_AGENT
from tests.test_ollama_worker import FakeOllama, Clock, cdt, REPLY, WED_0900
from common_room.crew_client import CrewClient, CrewError
from common_room import agent_worker as aw, ollama_worker as ow, usage

CONFIG = {'version': 1, 'actors': [
    {'id': 'local-a', 'role': 'worker', 'label': 'Fictional local model', 'session': 'fixture-local-a'},
    {'id': 'agent-a', 'role': 'worker', 'label': 'Fictional headless agent', 'session': 'fixture-agent-a'},
    {'id': 'test-reviewer', 'role': 'reviewer', 'label': 'Fictional reviewer', 'session': 'fixture-reviewer'}],
    'authorities': [
    {'id': 'crew-one', 'members': ['keeper', 'local-a', 'agent-a', 'test-reviewer'], 'scope': 'Fictional crew scope'}]}
NOON_UTC = datetime(2026, 10, 1, 17, 0, 0, tzinfo=timezone.utc).timestamp()


def local_record(**changes):
    record = {'kind': 'local', 'task_id': 't0123abcd', 'model': 'fixture-model:latest', 'seconds': 12.5,
              'prompt_tokens': 300, 'output_tokens': 40, 'outcome': 'finished', 'ts': NOON_UTC}
    record.update(changes)
    return record


def headless_record(**changes):
    record = {'kind': 'headless', 'task_id': 't0123abcd', 'model': 'claude', 'seconds': 90.0, 'exit': 0,
              'budget_cap_usd': 5, 'outcome': 'finished', 'ts': NOON_UTC}
    record.update(changes)
    return record


class FileTests(unittest.TestCase):
    """append_usage, read_usage and the refusal of anything that could carry task text."""

    def setUp(self):
        self.runtime = case_dir('usage-file')

    def lines(self, seat='local-a'):
        return (self.runtime / 'usage' / (seat + '.jsonl')).read_text(encoding='utf-8').splitlines()

    def test_one_json_line_per_append_with_the_fixed_fields(self):
        first = usage.append_usage(self.runtime, 'local-a', local_record())
        usage.append_usage(self.runtime, 'local-a', local_record(task_id='t9', outcome='question', prompt_tokens=None,
                                                                 output_tokens=None, seconds=0))
        lines = self.lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(list(json.loads(lines[0])), ['ts', 'seat', 'kind', 'task_id', 'model', 'seconds',
                                                      'prompt_tokens', 'output_tokens', 'outcome'])
        self.assertEqual(json.loads(lines[0]), first)
        self.assertEqual(first['ts'], '2026-10-01T17:00:00Z')
        self.assertEqual((first['seat'], first['seconds'], first['prompt_tokens']), ('local-a', 12.5, 300))
        self.assertEqual(json.loads(lines[1])['prompt_tokens'], None)
        usage.append_usage(self.runtime, 'agent-a', headless_record())
        head = json.loads(self.lines('agent-a')[0])
        self.assertEqual(list(head), ['ts', 'seat', 'kind', 'task_id', 'model', 'seconds', 'exit',
                                      'budget_cap_usd', 'outcome'])
        self.assertEqual((head['exit'], head['budget_cap_usd']), (0, 5.0))
        raw = (self.runtime / 'usage' / 'local-a.jsonl').read_bytes()
        self.assertNotIn(b'\r', raw)                                   # LF lines, whatever the platform
        self.assertTrue(raw.endswith(b'\n'))
        self.assertFalse(any(len(line) > usage.MAX_LINE for line in raw.split(b'\n')))

    def test_a_record_cannot_carry_task_text_prompts_results_paths_or_keys(self):
        for field in ('prompt', 'result', 'text', 'body', 'title', 'path', 'token', 'task_text', 'cost_usd'):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    usage.append_usage(self.runtime, 'local-a', local_record(**{field: 'Summarize the secret plan'}))
        for changes in ({'task_id': 'Summarize the notes'}, {'task_id': 'a\nb'}, {'model': 'two words'},
                        {'model': 'x' * 400}, {'model': 'line\nbreak'}, {'model': ''}, {'model': 5}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    usage.append_usage(self.runtime, 'local-a', local_record(**changes))
        self.assertFalse((self.runtime / 'usage' / 'local-a.jsonl').exists())      # nothing was written at all

    def test_each_kind_takes_only_its_own_fields_and_valid_values(self):
        bad = [('local-a', local_record(exit=0)), ('local-a', local_record(budget_cap_usd=5)),
               ('agent-a', headless_record(prompt_tokens=5)), ('agent-a', headless_record(output_tokens=5)),
               ('local-a', local_record(outcome='done')), ('local-a', local_record(outcome=None)),
               ('local-a', local_record(kind='remote')), ('local-a', local_record(seconds=-1)),
               ('local-a', local_record(seconds='12')), ('local-a', local_record(seconds=float('nan'))),
               ('local-a', local_record(seconds=True)), ('local-a', local_record(prompt_tokens=-3)),
               ('local-a', local_record(output_tokens=1.5)), ('local-a', local_record(prompt_tokens=True)),
               ('agent-a', headless_record(exit='0')), ('agent-a', headless_record(budget_cap_usd=-1)),
               ('agent-a', headless_record(budget_cap_usd=True)), ('Local-A', local_record()), ('../x', local_record()),
               ('', local_record()), ('local-a', local_record(seat='local-b')), ('local-a', 'a line of text'),
               ('local-a', local_record(ts='yesterday')), ('local-a', local_record(ts='2026-10-01T17:00:00'))]
        for seat, record in bad:
            with self.subTest(seat=seat, record=record):
                with self.assertRaises(ValueError):
                    usage.append_usage(self.runtime, seat, record)
        for outcome in usage.OUTCOMES:
            usage.append_usage(self.runtime, 'local-a', local_record(outcome=outcome))
        self.assertEqual(len(self.lines()), 4)
        self.assertEqual(usage.OUTCOMES, ('finished', 'question', 'failed', 'skipped_quiet'))

    def test_ts_accepts_epoch_datetime_and_iso_and_defaults_to_now(self):
        want = '2026-10-01T17:00:00Z'
        for value in (NOON_UTC, int(NOON_UTC), datetime.fromtimestamp(NOON_UTC, timezone.utc), want,
                      '2026-10-01T12:00:00-05:00'):
            with self.subTest(value=value):
                self.assertEqual(usage.append_usage(self.runtime, 'local-a', local_record(ts=value))['ts'], want)
        record = local_record()
        del record['ts']
        self.assertRegex(usage.append_usage(self.runtime, 'local-a', record)['ts'], r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ')

    def test_each_line_is_one_write_and_concurrent_appends_stay_whole(self):
        real, sizes = usage._append_bytes, []

        def counting(path, data):
            sizes.append(len(data))
            return real(path, data)
        with mock.patch.object(usage, '_append_bytes', counting):
            usage.append_usage(self.runtime, 'local-a', local_record())
        self.assertEqual(len(sizes), 1)                                  # the whole line goes out in one append
        self.assertEqual(sizes[0], len(self.lines()[0]) + 1)

        def worker(n):
            for i in range(40):
                usage.append_usage(self.runtime, 'local-a', local_record(task_id='t%d-%d' % (n, i), seconds=n + i / 100))
        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        problems = []
        rows = usage.read_usage(self.runtime, problems=problems)
        self.assertEqual((len(rows), problems), (1 + 6 * 40, []))        # every line whole, none torn or lost
        self.assertEqual(len({r['task_id'] for r in rows}), 1 + 6 * 40)

    def test_separate_processes_appending_at_once_lose_nothing(self):
        script = '\n'.join([
            'import sys',
            'sys.path.insert(0, sys.argv[1])',
            'from common_room import usage',
            'n = sys.argv[3]',
            'for i in range(60):',
            "    usage.append_usage(sys.argv[2], 'local-a', {'kind': 'local', 'task_id': 'p%s-%d' % (n, i), 'model': 'm',",
            "                                                'seconds': i / 7, 'prompt_tokens': i, 'output_tokens': None,",
            "                                                'outcome': 'finished'})"])
        package = str(Path(usage.__file__).resolve().parents[1])
        import subprocess
        procs = [subprocess.Popen([sys.executable, '-B', '-c', script, package, str(self.runtime), str(n)])
                 for n in range(4)]
        self.assertEqual([p.wait(120) for p in procs], [0, 0, 0, 0])
        problems = []
        rows = usage.read_usage(self.runtime, problems=problems)
        self.assertEqual((len(rows), problems), (4 * 60, []))
        self.assertEqual(len({r['task_id'] for r in rows}), 4 * 60)

    def test_read_usage_filters_by_time_and_seat(self):
        base = NOON_UTC
        for seat, kind, offset in [('local-a', 'local', 0), ('local-a', 'local', 3600), ('local-b', 'local', 1800),
                                   ('agent-a', 'headless', 7200)]:
            record = (local_record if kind == 'local' else headless_record)(task_id='t-%s-%d' % (seat, offset),
                                                                           ts=base + offset)
            usage.append_usage(self.runtime, seat, record)
        everything = usage.read_usage(self.runtime)
        self.assertEqual([(r['seat'], r['ts']) for r in everything],
                         [('local-a', '2026-10-01T17:00:00Z'), ('local-b', '2026-10-01T17:30:00Z'),
                          ('local-a', '2026-10-01T18:00:00Z'), ('agent-a', '2026-10-01T19:00:00Z')])   # oldest first
        self.assertEqual([r['seat'] for r in usage.read_usage(self.runtime, seat='local-a')], ['local-a', 'local-a'])
        for since in (base + 1800, '2026-10-01T17:30:00Z', datetime.fromtimestamp(base + 1800, timezone.utc)):
            with self.subTest(since=since):
                self.assertEqual([r['seat'] for r in usage.read_usage(self.runtime, since=since)],
                                 ['local-b', 'local-a', 'agent-a'])                     # at or after, not before
        self.assertEqual([r['task_id'] for r in usage.read_usage(self.runtime, since=base + 1801, seat='local-a')],
                         ['t-local-a-3600'])
        self.assertEqual(usage.read_usage(self.runtime, since=base + 99999), [])
        self.assertEqual(usage.read_usage(self.runtime, seat='nobody'), [])
        self.assertEqual(usage.read_usage(case_dir('usage-empty')), [])                  # no usage folder yet
        with self.assertRaises(ValueError):
            usage.read_usage(self.runtime, seat='../x')

    def test_read_usage_skips_and_reports_bad_lines_and_files(self):
        usage.append_usage(self.runtime, 'local-a', local_record())
        path = self.runtime / 'usage' / 'local-a.jsonl'
        good = path.read_text(encoding='utf-8')
        extra = json.loads(good)
        extra['prompt'] = 'the whole prompt'
        wrong_seat = dict(json.loads(good), seat='local-b')
        path.write_text(good + 'not json\n[1, 2]\n\n' + json.dumps(extra) + '\n' + json.dumps(wrong_seat) + '\n'
                        + '[' * 5000 + '\n' + good, encoding='utf-8')
        (self.runtime / 'usage' / 'notes.txt').write_text('ignored', encoding='utf-8')
        (self.runtime / 'usage' / 'Bad Name.jsonl').write_text(good, encoding='utf-8')
        problems = []
        rows = usage.read_usage(self.runtime, problems=problems)
        self.assertEqual(len(rows), 2)                                   # the two good lines, nothing else
        self.assertEqual(len(problems), 5)
        self.assertTrue(all(p.startswith('local-a.jsonl line') for p in problems))
        self.assertEqual(usage.read_usage(self.runtime), rows)           # problems=None is allowed

    def test_a_linked_usage_folder_is_refused(self):
        from tests.helpers import symlinks_available
        if not symlinks_available():
            self.skipTest('this host cannot make symbolic links')
        elsewhere = case_dir('usage-elsewhere')
        os.symlink(elsewhere, self.runtime / 'usage', target_is_directory=True)
        with self.assertRaises(ValueError):
            usage.append_usage(self.runtime, 'local-a', local_record())
        self.assertEqual(list(elsewhere.iterdir()), [])
        problems = []
        self.assertEqual(usage.read_usage(self.runtime, problems=problems), [])
        self.assertEqual(len(problems), 1)

    def test_try_append_never_raises_and_says_why(self):
        (self.runtime / 'usage').write_text('a file where the folder should be', encoding='utf-8')
        logged = []
        self.assertFalse(usage.try_append(self.runtime, 'local-a', local_record(), lambda event, **f: logged.append((event, f))))
        self.assertEqual(logged[0][0], 'usage_unwritable')
        self.assertEqual(logged[0][1]['seat'], 'local-a')
        self.assertFalse(usage.try_append(self.runtime, 'local-a', local_record(prompt='x'), None))      # no log: still quiet
        other = case_dir('usage-ok')
        self.assertTrue(usage.try_append(other, 'local-a', local_record(), None))

    def test_module_is_offline_and_standard_library_only(self):
        source = Path(usage.__file__).read_text(encoding='utf-8')
        for forbidden in ['import subprocess', 'socket', 'urllib', 'http', 'os.system', 'eval(', 'exec(', 'pickle']:
            self.assertNotIn(forbidden, source)


class WorkerMeterTests(unittest.TestCase):
    """Each worker writes exactly one line per job, and the line holds no text from the job."""

    def setUp(self):
        from common_room.activation import start_room
        self.room = start_room(case_dir('usage-worker'), enrollment=CONFIG,
                               selected=['local-a', 'agent-a', 'test-reviewer'])
        self.addCleanup(self.room.close)
        self.fake = FakeOllama()
        self.addCleanup(self.fake.close)
        roots = case_dir('usage-roots')
        self.art, self.pi = roots / 'Artifacts', roots / 'Lab Notes'
        self.art.mkdir()
        self.pi.mkdir()
        (self.art / 'notes.md').write_text('alpha secretword\nbeta\n', encoding='utf-8')
        self.clock = Clock(WED_0900)
        self.mono = Clock(1000.0)
        self.work = case_dir('usage-work')
        self.runs = case_dir('usage-runs') / 'runs'
        self.logs = []

    # -- helpers -------------------------------------------------------------------------
    def client(self, actor):
        return CrewClient(self.room.root / (actor + '-credential.json'), sleep=lambda s: None)

    def local(self, **options):
        settings = dict(timeout=600, num_ctx=4096, num_predict=500, keep_alive=600, max_per_hour=12)
        settings.update(options)
        return ow.Worker(self.client('local-a'), 'fixture-model', ow.OllamaClient(self.fake.url), [self.art, self.pi],
                         clock=self.clock, monotonic=self.mono, sleep=lambda s: None, log=self.logs.append, **settings)

    def agent(self, command=None, **options):
        settings = dict(timeout=60, slice_seconds=0.05, max_runs_per_hour=12, max_runs_per_day=48)
        settings.update(options)
        command = command or [sys.executable, FAKE_AGENT, '{task_file}', '{run_dir}']
        return aw.Runner(self.client('agent-a'), command, self.work, self.runs, clock=self.clock,
                         sleep=lambda s: None, log=self.logs.append, **settings)

    def task(self, body, recipient='local-a', title='Fixture task secretword'):
        keeper = self.room.store.people['keeper']
        ref = 'ref-' + uuid.uuid4().hex[:16]
        message = {'title': title, 'body': body, 'recipient': recipient, 'authority_id': 'crew-one',
                   'kind': 'handoff.record', 'acceptance': 'Every claim cites notes.md secretword'}
        task_id = self.room.store.execute(keeper, 'intake', {'source_namespace': 'estate', 'external_reference': ref,
                                                             'message': message}, 'intake-' + ref)['task']['id']
        self.room.store.execute(keeper, 'deliver', {'task_id': task_id}, 'deliver-' + ref)
        return task_id

    def local_task(self, body='Summarize the notes secretword.\nINPUT: notes.md'):
        return self.task(body)

    def agent_task(self, behaviour='RESULT'):
        return self.task('Do the fixture thing secretword.\nAGENT: ' + behaviour, recipient='agent-a')

    def rows(self, seat='local-a'):
        problems = []
        rows = usage.read_usage(self.room.root, seat=seat, problems=problems)
        self.assertEqual(problems, [])
        return rows

    def events(self):
        return [json.loads(line) for line in self.logs]

    def assert_no_job_text(self, seat):
        path = self.room.root / 'usage' / (seat + '.jsonl')
        text = path.read_text(encoding='utf-8')
        for secret in ('secretword', 'Summarize', 'alpha', 'notes.md', REPLY, 'Fake result', 'fixture thing',
                       'Fixture task', str(self.art), str(self.work), 'PRIVATE-THOUGHTS',
                       *[c['token'] for c in self.room.credentials.values()]):
            self.assertNotIn(secret, text)

    # -- local model worker --------------------------------------------------------------
    def test_local_job_writes_exactly_one_line_with_measured_numbers(self):
        self.fake.on_chat = lambda body: setattr(self.mono, 't', self.mono.t + 7.5)
        t = self.local_task()
        worker = self.local()
        self.assertEqual(worker.run_once()['status'], 'submitted')
        (row,) = self.rows()
        self.assertEqual((row['kind'], row['task_id'], row['model'], row['outcome']), ('local', t, 'fixture-model', 'finished'))
        self.assertEqual((row['prompt_tokens'], row['output_tokens']), (314, 42))     # as Ollama reported them
        self.assertEqual(row['seconds'], 7.5)
        self.assertEqual(row['ts'], '2026-09-23T14:00:00Z')                            # 09:00 CDT from the injected clock
        self.assertEqual(worker.run_once()['status'], 'idle')                          # no job, no line
        self.assertEqual(len(self.rows()), 1)
        t2 = self.local_task()
        self.assertEqual(worker.run_once()['status'], 'submitted')
        self.assertEqual([r['task_id'] for r in self.rows()], [t, t2])                 # one more line for one more job
        self.assert_no_job_text('local-a')

    def test_local_outcomes_question_failed_and_skipped_quiet(self):
        worker = self.local()
        t1 = self.local_task('Summarize.\nINPUT: missing-file.md')                     # refused before any model call
        self.assertEqual(worker.run_once()['status'], 'asked')
        (row,) = self.rows()
        self.assertEqual((row['task_id'], row['outcome'], row['seconds'], row['prompt_tokens'], row['output_tokens']),
                         (t1, 'question', 0.0, None, None))
        self.assertEqual(self.fake.requests, [])
        self.fake.reply = ' '                                                          # empty model output is a failure
        self.fake.done_reason = 'stop'
        self.local_task()
        self.assertEqual(worker.run_once()['status'], 'failed')
        self.assertEqual([r['outcome'] for r in self.rows()], ['question', 'failed'])
        self.assertEqual(self.rows()[1]['output_tokens'], 42)                          # the model did run
        self.assert_no_job_text('local-a')

    def test_quiet_window_holds_write_one_skipped_line_a_day_not_one_per_poll(self):
        t = self.local_task()
        worker = self.local()
        self.clock.t = cdt(2026, 9, 23, 11, 40)                                         # inside the midday GPU window
        for _ in range(5):
            self.assertEqual(worker.run_once()['reason'], 'quiet_window')
            self.clock.t += 60
        (row,) = self.rows()
        self.assertEqual((row['task_id'], row['outcome'], row['seconds']), (t, 'skipped_quiet', 0.0))
        self.clock.t = cdt(2026, 9, 24, 11, 40)                                         # the next Central day
        self.assertEqual(worker.run_once()['reason'], 'quiet_window')
        self.assertEqual([r['outcome'] for r in self.rows()], ['skipped_quiet', 'skipped_quiet'])
        self.clock.t = cdt(2026, 9, 24, 13, 0)                                          # window over: the job runs
        self.assertEqual(worker.run_once()['status'], 'submitted')
        self.assertEqual([r['outcome'] for r in self.rows()], ['skipped_quiet', 'skipped_quiet', 'finished'])

    def test_unusable_gpu_calendar_counts_as_a_quiet_skip(self):
        self.local_task()
        worker = self.local(gpu_calendar=self.room.root / 'no-such-calendar.json')
        self.assertEqual(worker.run_once()['reason'], 'gpu_calendar_unusable')
        self.assertEqual([r['outcome'] for r in self.rows()], ['skipped_quiet'])

    def test_other_deferrals_ran_nothing_and_write_no_line(self):
        self.local_task()
        self.fake.models = ['other:latest']                                             # model missing: nothing ran
        worker = self.local()
        self.assertEqual(worker.run_once()['reason'], 'model_missing')
        self.fake.models = ['fixture-model:latest']
        self.fake.script['/api/tags'] = [('status', 500)]                               # Ollama unreachable: nothing ran
        self.assertEqual(self.local().run_once()['reason'], 'ollama_unavailable')
        self.assertEqual(self.rows(), [])
        self.assertEqual(worker.run_once()['status'], 'submitted')
        self.assertEqual(len(self.rows()), 1)

    def test_a_meter_that_cannot_write_never_stops_the_job(self):
        (self.room.root / 'usage').write_text('a file where the folder should be', encoding='utf-8')
        t = self.local_task()
        self.assertEqual(self.local().run_once()['status'], 'submitted')
        task = self.room.store.task(self.room.store.people['keeper'], t)
        self.assertEqual(task['status'], 'ready_for_review')
        failed = [e for e in self.events() if e['event'] == 'usage_unwritable']
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]['seat'], 'local-a')

    def refusing(self, worker, command=None, handoff=None, error=None):
        """Make the room refuse one client call: a command by action name, or a handoff by action name. The
        refusal is a CrewError (as the room's own refusal would be), or `error` when one is given."""
        real_command, real_handoff = worker.client.command, worker.client.handoff

        def refuse():
            raise (error or CrewError('fence', 'The room refused this (fixture).'))

        def guarded_command(action, payload, request_id):
            if action == command:
                refuse()
            return real_command(action, payload, request_id)

        def guarded_handoff(action, payload, *args, **kwargs):
            if action == handoff:
                refuse()
            return real_handoff(action, payload, *args, **kwargs)
        worker.client.command, worker.client.handoff = guarded_command, guarded_handoff

    def test_local_job_the_room_refuses_after_the_model_ran_is_one_failed_line(self):
        # crew-audit #698: the model ran and used tokens, then the room refused the staged draft or the finish.
        for label, kwargs in [('stage_text refused', {'handoff': 'stage_text'}), ('finish refused', {'command': 'finish'})]:
            with self.subTest(case=label):
                before = len(self.rows())
                self.fake.on_chat = lambda body: setattr(self.mono, 't', self.mono.t + 7.5)
                t = self.local_task()
                worker = self.local()
                self.refusing(worker, **kwargs)
                result = worker.run_once()
                self.assertEqual((result['status'], result['reason']), ('error', 'room_refused'))
                rows = self.rows()
                self.assertEqual(len(rows), before + 1)                                  # exactly one line for this job
                row = rows[-1]
                self.assertEqual((row['task_id'], row['outcome'], row['seconds']), (t, 'failed', 7.5))
                self.assertEqual((row['prompt_tokens'], row['output_tokens']), (314, 42))   # measured, not lost
                self.assertEqual(self.local().run_once()['status'], 'submitted')            # the lapsed claim is resumed later
                self.assertEqual(rows[-1], self.rows()[before])                              # and the failed line is not rewritten
        self.assert_no_job_text('local-a')

    def test_local_ask_the_room_refuses_after_a_generation_is_one_failed_line(self):
        self.fake.script['/api/chat'] = [('status', 500), ('status', 500)]                  # unavailable mid-task: asks
        self.fake.on_chat = lambda body: setattr(self.mono, 't', self.mono.t + 7.5)
        t = self.local_task()
        worker = self.local()
        self.refusing(worker, command='wait_decision')
        result = worker.run_once()
        self.assertEqual((result['status'], result['reason']), ('error', 'room_refused'))
        (row,) = self.rows()
        self.assertEqual((row['task_id'], row['outcome'], row['seconds']), (t, 'failed', 15.0))   # both attempts counted
        self.assertEqual((row['prompt_tokens'], row['output_tokens']), (None, None))               # no reply, no numbers

    def test_local_unexpected_error_after_the_model_ran_is_one_failed_line_and_still_raises(self):
        self.fake.on_chat = lambda body: setattr(self.mono, 't', self.mono.t + 2.0)
        t = self.local_task()
        worker = self.local()
        self.refusing(worker, command='finish', error=RuntimeError('something broke'))
        with self.assertRaises(RuntimeError):
            worker.run_once()
        (row,) = self.rows()
        self.assertEqual((row['task_id'], row['outcome'], row['seconds'], row['output_tokens']), (t, 'failed', 2.0, 42))

    def test_a_room_refusal_before_any_model_call_writes_no_line_however_often_it_repeats(self):
        self.local_task()
        for refused in ('ack', 'claim'):
            worker = self.local()
            self.refusing(worker, command=refused)
            for _ in range(3):                                                              # a paused room refuses every poll
                self.assertEqual(worker.run_once()['reason'], 'room_refused')
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.fake.chats(), [])
        self.assertEqual(self.local().run_once()['status'], 'submitted')                    # then one normal job, one line
        self.assertEqual([r['outcome'] for r in self.rows()], ['finished'])

    def test_an_idle_cycle_after_a_refused_job_adds_nothing(self):
        self.local_task()
        worker = self.local()
        self.refusing(worker, handoff='stage_text')
        self.assertEqual(worker.run_once()['reason'], 'room_refused')
        self.assertEqual(len(self.rows()), 1)
        worker.client.command, worker.client.handoff = (CrewClient.command.__get__(worker.client),
                                                        CrewClient.handoff.__get__(worker.client))
        self.assertEqual(worker.run_once()['status'], 'submitted')
        self.assertEqual(worker.run_once()['status'], 'idle')
        self.assertEqual([r['outcome'] for r in self.rows()], ['failed', 'finished'])        # each job once, idle nothing

    # -- headless agent runner -----------------------------------------------------------
    def test_headless_job_writes_one_line_with_exit_code_and_budget_cap(self):
        command = [sys.executable, FAKE_AGENT, '{task_file}', '{run_dir}', '--max-budget-usd', '5',
                   '--model', 'claude-fixture']
        t = self.agent_task('RESULT')
        runner = self.agent(command)
        self.assertEqual(runner.run_once()['status'], 'submitted')
        (row,) = self.rows('agent-a')
        self.assertEqual((row['kind'], row['task_id'], row['model'], row['exit'], row['budget_cap_usd'], row['outcome']),
                         ('headless', t, 'claude-fixture', 0, 5.0, 'finished'))
        self.assertGreaterEqual(row['seconds'], 0)
        self.assertNotIn('total_cost_usd', row)                                         # the agent's own figure is CLAIMED, not metered
        self.assertEqual(runner.run_once()['status'], 'idle')
        self.assertEqual(len(self.rows('agent-a')), 1)
        self.assert_no_job_text('agent-a')

    def test_headless_without_a_budget_flag_or_model_flag(self):
        self.agent_task('RESULT')
        self.assertEqual(self.agent().run_once()['status'], 'submitted')
        (row,) = self.rows('agent-a')
        self.assertEqual((row['budget_cap_usd'], row['model']), (None, Path(sys.executable).name))
        other = self.agent([sys.executable, FAKE_AGENT, '{task_file}', '{run_dir}', '--max-budget-usd=2.5'])
        self.agent_task('RESULT')
        self.assertEqual(other.run_once()['status'], 'submitted')
        self.assertEqual(self.rows('agent-a')[1]['budget_cap_usd'], 2.5)
        broken = self.agent([sys.executable, FAKE_AGENT, '{task_file}', '{run_dir}', '--max-budget-usd', 'lots'])
        self.agent_task('RESULT')
        self.assertEqual(broken.run_once()['status'], 'submitted')
        self.assertEqual(self.rows('agent-a')[2]['budget_cap_usd'], None)               # an odd value is not metered, not fatal

    def test_headless_question_crash_and_blocked_are_one_line_each(self):
        runner = self.agent()
        t1 = self.agent_task('QUESTION')
        self.assertEqual(runner.run_once()['reason'], 'question')
        t2 = self.agent_task('CRASH')
        self.assertEqual(runner.run_once()['reason'], 'crashed')
        t3 = self.agent_task('BLOCKED')
        self.assertEqual(runner.run_once()['reason'], 'blocked')
        self.assertEqual([(r['task_id'], r['outcome'], r['exit']) for r in self.rows('agent-a')],
                         [(t1, 'question', 0), (t2, 'failed', 3), (t3, 'question', 0)])
        self.assert_no_job_text('agent-a')

    def test_headless_deferral_runs_no_agent_and_writes_no_line(self):
        self.agent_task('RESULT')
        runner = self.agent(['definitely-not-installed-xyz', '{task_file}'])
        self.assertEqual(runner.run_once()['reason'], 'executable_missing')
        self.assertEqual(self.rows('agent-a'), [])

    def test_a_headless_meter_that_cannot_write_never_stops_the_job(self):
        (self.room.root / 'usage').write_text('a file where the folder should be', encoding='utf-8')
        self.agent_task('RESULT')
        self.assertEqual(self.agent().run_once()['status'], 'submitted')
        self.assertEqual(len([e for e in self.events() if e['event'] == 'usage_unwritable']), 1)

    def test_old_history_still_reads_the_same_with_the_meter_present(self):
        # The meter is a side file: the room's own task states for a job are what they were without it.
        t = self.local_task()
        self.local().run_once()
        kinds = [e['kind'] for e in self.room.store._snapshot(self.room.store.people['keeper'])[0]
                 if e['task_id'] == t and e['actor'] == 'local-a']
        self.assertEqual(kinds, ['task_ack', 'task_claim', 'task_finish'])


if __name__ == '__main__':
    unittest.main(argv=sys.argv[:1])
