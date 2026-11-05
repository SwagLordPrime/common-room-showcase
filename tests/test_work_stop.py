"""Pause everything reaches the workers: the STOP flag (<room runtime>/STOP, beside the seat's credential).

While the file exists neither worker takes a job, and a running headless agent's whole process tree is killed
and the task is left to resume with no strike. When the file is gone work resumes at the next poll. A flag that
cannot be read counts as present.

RED note: written before the change, so on the base commit the flag is ignored: the workers take their jobs
anyway, and the agent is not stopped.
Every model is a fake Ollama on 127.0.0.1 and every agent is tests/fake_agent.py.
"""
import sys
from pathlib import Path
if __name__ == '__main__':          # runnable as a script too (tools/evidence.py runs `python tests/<file> .`)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import json, os, subprocess, threading, time, unittest, uuid
from unittest import mock
from tests.helpers import case_dir
from tests.test_agent_worker import FAKE as FAKE_AGENT, gone
from tests.test_ollama_worker import FakeOllama, Clock, WED_0900
from common_room.crew_client import CrewClient
from common_room import agent_worker as aw, ollama_worker as ow

CONFIG = {'version': 1, 'actors': [
    {'id': 'local-a', 'role': 'worker', 'label': 'Fictional local model', 'session': 'fixture-local-a'},
    {'id': 'agent-a', 'role': 'worker', 'label': 'Fictional headless agent', 'session': 'fixture-agent-a'},
    {'id': 'test-reviewer', 'role': 'reviewer', 'label': 'Fictional reviewer', 'session': 'fixture-reviewer'}],
    'authorities': [
    {'id': 'crew-one', 'members': ['keeper', 'local-a', 'agent-a', 'test-reviewer'], 'scope': 'Fictional crew scope'}]}
REASON = 'stopped: STOP flag'


class StopCase(unittest.TestCase):
    def setUp(self):
        from common_room.activation import start_room
        self.room = start_room(case_dir('stop'), enrollment=CONFIG, selected=['local-a', 'agent-a', 'test-reviewer'])
        self.addCleanup(self.room.close)
        self.flag = self.room.root / 'STOP'
        self.fake = FakeOllama()
        self.addCleanup(self.fake.close)
        roots = case_dir('stop-roots')
        self.art, self.pi = roots / 'Artifacts', roots / 'Lab Notes'
        self.art.mkdir()
        self.pi.mkdir()
        (self.art / 'notes.md').write_text('alpha\nbeta\n', encoding='utf-8')
        self.work = case_dir('stop-work')
        self.runs = case_dir('stop-runs') / 'runs'
        self.clock = Clock(WED_0900)
        self.logs = []

    # -- helpers -------------------------------------------------------------------------
    def client(self, actor):
        return CrewClient(self.room.root / (actor + '-credential.json'), sleep=lambda s: None)

    def local(self, **options):
        settings = dict(timeout=600, num_ctx=4096, num_predict=500, keep_alive=600, max_per_hour=12)
        settings.update(options)
        return ow.Worker(self.client('local-a'), 'fixture-model', ow.OllamaClient(self.fake.url), [self.art, self.pi],
                         clock=self.clock, sleep=lambda s: None, log=self.logs.append, **settings)

    def agent(self, **options):
        settings = dict(timeout=120, slice_seconds=0.05, max_runs_per_hour=12, max_runs_per_day=48)
        settings.update(options)
        return aw.Runner(self.client('agent-a'), [sys.executable, FAKE_AGENT, '{task_file}', '{run_dir}'], self.work,
                         self.runs, clock=self.clock, sleep=lambda s: None, log=self.logs.append, **settings)

    def task(self, body, recipient):
        keeper = self.room.store.people['keeper']
        ref = 'ref-' + uuid.uuid4().hex[:16]
        message = {'title': 'Fixture task', 'body': body, 'recipient': recipient, 'authority_id': 'crew-one',
                   'kind': 'handoff.record', 'acceptance': 'Cites notes.md'}
        task_id = self.room.store.execute(keeper, 'intake', {'source_namespace': 'estate', 'external_reference': ref,
                                                             'message': message}, 'intake-' + ref)['task']['id']
        self.room.store.execute(keeper, 'deliver', {'task_id': task_id}, 'deliver-' + ref)
        return task_id

    def local_task(self):
        return self.task('Summarize the notes.\nINPUT: notes.md', 'local-a')

    def agent_task(self, behaviour):
        return self.task('Do the fixture thing.\nAGENT: ' + behaviour, 'agent-a')

    def get(self, task_id):
        return self.room.store.task(self.room.store.people['keeper'], task_id)

    def kinds(self, task_id, actor):
        rows = self.room.store._snapshot(self.room.store.people['keeper'])[0]
        return [e['kind'] for e in rows if e['task_id'] == task_id and e['actor'] == actor]

    def status(self, seat):
        return json.loads((self.room.root / 'workers' / (seat + '-status.json')).read_text(encoding='utf-8'))

    def events(self):
        return [json.loads(line) for line in self.logs]

    def raise_flag(self, text='{"by": "fixture", "reason": "test"}'):
        self.flag.write_text(text, encoding='utf-8')

    def lower_flag(self):
        if self.flag.is_dir():
            self.flag.rmdir()
        elif self.flag.exists():
            self.flag.unlink()

    def assert_says_stopped(self, seat):
        status = self.status(seat)
        self.assertEqual((status['last_result'], status['last_reason']), ('stopped', REASON))

    @staticmethod
    def denied(error):
        """Patch the OS call underneath (os.lstat) so that asking about the STOP file raises `error`, and nothing
        else is affected: every other path is answered by the real call. (Patching os.path.lexists instead would
        bypass the very thing under test: lexists swallows an OSError from lstat and answers False.)"""
        real = os.lstat

        def lstat(path, *args, **kwargs):
            if os.path.basename(os.fspath(path)) == 'STOP':
                raise error
            return real(path, *args, **kwargs)
        return mock.patch.object(os, 'lstat', lstat)


class FlagReaderTests(StopCase):
    def test_present_means_stop_whatever_it_holds_or_whether_it_can_be_read(self):
        self.assertFalse(ow.stop_flag_present(self.room.root))
        for label, make in [('JSON', lambda: self.raise_flag()), ('empty', lambda: self.flag.write_bytes(b'')),
                            ('binary junk', lambda: self.flag.write_bytes(bytes(range(256)) * 20)),
                            ('not UTF-8', lambda: self.flag.write_bytes(b'\xff\xfe\x00bad')),
                            ('a folder', lambda: self.flag.mkdir())]:
            with self.subTest(flag=label):
                make()
                self.assertTrue(ow.stop_flag_present(self.room.root))
                self.lower_flag()
                self.assertFalse(ow.stop_flag_present(self.room.root))
        self.assertIs(aw.stop_flag_present, ow.stop_flag_present)               # one reader for both workers
        self.assertEqual((ow.STOP_FLAG, ow.STOP_REASON), ('STOP', REASON))


    def test_a_flag_the_os_will_not_show_counts_as_present_absent_only_when_plainly_not_there(self):
        # astra-audit #842: the OS refusing to answer is NOT "no flag". The call under test is os.lstat itself.
        for error in (PermissionError(13, 'cannot stat'), OSError(5, 'input/output error'), OSError('odd failure'),
                      PermissionError(5, 'access denied'), ValueError('embedded null byte')):
            with self.subTest(error=repr(error)):
                with self.denied(error):
                    self.assertTrue(ow.stop_flag_present(self.room.root))
        self.assertTrue(ow.stop_flag_present(str(self.room.root) + '\x00x'))      # a path the OS rejects outright
        # Plainly not there: no file, a runtime folder that does not exist, a parent that is a file.
        self.assertFalse(ow.stop_flag_present(self.room.root))
        self.assertFalse(ow.stop_flag_present(self.room.root / 'no-such-folder'))
        self.assertFalse(ow.stop_flag_present(self.room.root / 'workers' / 'no-such-folder' / 'deeper'))
        parent_is_a_file = self.room.root / 'a-file-not-a-folder'
        parent_is_a_file.write_text('x', encoding='utf-8')
        self.assertFalse(ow.stop_flag_present(parent_is_a_file))
        for error in (FileNotFoundError(2, 'not found'), NotADirectoryError(20, 'not a directory')):
            with self.subTest(error=repr(error)):
                with self.denied(error):
                    self.assertFalse(ow.stop_flag_present(self.room.root))         # the OS says plainly: nothing there
        # The premise: with the same denial os.path.lexists answers False, which is why it is not used.
        self.raise_flag()
        with self.denied(PermissionError(13, 'cannot stat')):
            self.assertFalse(os.path.lexists(str(self.flag)))
            self.assertTrue(ow.stop_flag_present(self.room.root))


class LocalWorkerStopTests(StopCase):
    def test_flag_present_before_a_job_takes_no_job_and_status_says_so(self):
        t = self.local_task()
        self.raise_flag()
        worker = self.local()
        for _ in range(3):
            result = worker.run_once()
            self.assertEqual((result['status'], result['reason']), ('stopped', REASON))
        self.assertEqual(self.get(t)['status'], 'delivered')                      # not even acknowledged
        self.assertEqual(self.kinds(t, 'local-a'), [])
        self.assertEqual(self.fake.requests, [])                                  # no health check, no model call
        self.assert_says_stopped('local-a')
        self.assertEqual(self.get(t)['failed_attempts'], 0)

    def test_idle_worker_also_reports_stopped_and_flag_removed_resumes_at_next_poll(self):
        worker = self.local()
        self.raise_flag()
        self.assertEqual(worker.run_once()['status'], 'stopped')                  # nothing to do, still says so
        self.assert_says_stopped('local-a')
        t = self.local_task()
        self.assertEqual(worker.run_once()['status'], 'stopped')
        self.lower_flag()
        self.assertEqual(worker.run_once()['status'], 'submitted')                # the very next poll carries on
        self.assertEqual(self.get(t)['status'], 'ready_for_review')
        self.assertEqual(self.status('local-a')['last_result'], 'submitted')
        self.assertEqual(worker.run_once()['status'], 'idle')

    def test_unreadable_flag_counts_as_present(self):
        self.local_task()
        worker = self.local()
        self.flag.mkdir()                                                          # a folder cannot be read as a file
        self.assertEqual(worker.run_once()['status'], 'stopped')
        self.assertEqual(self.fake.requests, [])
        self.lower_flag()
        self.flag.write_bytes(b'\xff\xfe\x00junk')
        self.assertEqual(worker.run_once()['status'], 'stopped')
        self.lower_flag()
        with self.denied(PermissionError(13, 'cannot stat')):                      # the OS refuses to show the flag
            self.assertEqual(worker.run_once()['status'], 'stopped')
            self.assertEqual(self.fake.requests, [])
        self.assertEqual(worker.run_once()['status'], 'submitted')

    def test_flag_raised_after_the_claim_stops_before_the_model_call_and_resumes_with_no_strike(self):
        t = self.local_task()
        worker = self.local()
        self.fake.on_tags = self.raise_flag                                        # appears while the worker preflights
        result = worker.run_once()
        self.assertEqual((result['status'], result['reason']), ('stopped', REASON))
        self.assertEqual(self.fake.chats(), [])
        task = self.get(t)
        self.assertEqual((task['status'], task['failed_attempts'], task['lease']['holder']), ('claimed', 0, 'local-a'))
        self.fake.on_tags = None
        self.assertEqual(worker.run_once()['status'], 'stopped')                  # still raised
        self.lower_flag()
        self.assertEqual(worker.run_once()['status'], 'submitted')
        self.assertEqual(self.kinds(t, 'local-a'), ['task_ack', 'task_claim', 'task_finish'])   # resumed, not re-claimed
        self.assertEqual(self.get(t)['failed_attempts'], 0)

    def test_main_exit_code_for_a_stopped_once_run_is_the_deferred_one(self):
        self.assertEqual(ow.EXIT_CODES['stopped'], ow.EXIT_CODES['deferred'])
        self.assertEqual(aw.EXIT_CODES['stopped'], aw.EXIT_CODES['deferred'])


class AgentRunnerStopTests(StopCase):
    def flag_when_agent_runs(self, task_id, fence=1, ready='agent-pid.txt'):
        """A thread that raises the flag once the fixture agent has written `ready` (by default its pid)."""
        def work():
            deadline = time.monotonic() + 60
            folder = self.runs / 'agent-a' / ('%s-f%d' % (task_id, fence))
            while not (folder / ready).exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.raise_flag()
        thread = threading.Thread(target=work)
        thread.start()
        self.addCleanup(thread.join, 30)
        return thread

    def test_flag_present_before_a_job_starts_no_agent(self):
        t = self.agent_task('RESULT')
        self.raise_flag()
        runner = self.agent()
        for _ in range(2):
            result = runner.run_once()
            self.assertEqual((result['status'], result['reason']), ('stopped', REASON))
        self.assertEqual(self.get(t)['status'], 'delivered')
        self.assertEqual(self.kinds(t, 'agent-a'), [])
        self.assertFalse((self.runs / 'agent-a').exists() and any((self.runs / 'agent-a').iterdir()))   # no run folder
        self.assert_says_stopped('agent-a')
        self.lower_flag()
        self.assertEqual(runner.run_once()['status'], 'submitted')                # resumes at the next poll

    def test_a_flag_the_os_will_not_show_starts_no_agent_and_one_that_appears_that_way_mid_run_stops_it(self):
        t = self.agent_task('SLEEP 8')
        runner = self.agent()
        with self.denied(PermissionError(13, 'cannot stat')):
            result = runner.run_once()
        self.assertEqual((result['status'], result['reason']), ('stopped', REASON))
        self.assertEqual(self.kinds(t, 'agent-a'), [])
        self.assertFalse((self.runs / 'agent-a').exists() and any((self.runs / 'agent-a').iterdir()))     # no agent
        # Mid-run: the flag is readable until the agent is up, then the OS starts refusing to show it.
        state = {'deny': False}
        real = os.lstat

        def lstat(path, *args, **kwargs):
            if state['deny'] and os.path.basename(os.fspath(path)) == 'STOP':
                raise PermissionError(13, 'cannot stat')
            return real(path, *args, **kwargs)

        def deny_once_the_agent_runs():
            deadline = time.monotonic() + 60
            folder = self.runs / 'agent-a' / ('%s-f1' % t)
            while not (folder / 'agent-pid.txt').exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            state['deny'] = True
        thread = threading.Thread(target=deny_once_the_agent_runs)
        thread.start()
        self.addCleanup(thread.join, 60)
        with mock.patch.object(os, 'lstat', lstat):
            result = runner.run_once()
        self.assertEqual((result['status'], result['task_id']), ('stopped', t))
        run = self.runs / 'agent-a' / ('%s-f1.stopped-1' % t)
        self.assertTrue(gone(int((run / 'agent-pid.txt').read_text(encoding='utf-8'))))
        time.sleep(0.5)
        self.assertFalse((run / 'agent-done.txt').exists())
        self.assertEqual(self.get(t)['failed_attempts'], 0)

    def test_flag_mid_run_kills_the_whole_tree_leaves_the_task_resumable_with_no_strike(self):
        t = self.agent_task('SLEEP 8')
        self.flag_when_agent_runs(t)
        runner = self.agent()
        result = runner.run_once()
        self.assertEqual((result['status'], result['reason'], result['task_id']), ('stopped', REASON, t))
        run =self.runs / 'agent-a' / ('%s-f1.stopped-1' % t)                     # the dead run's folder, set aside
        self.assertTrue(run.is_dir())
        self.assertFalse((self.runs / 'agent-a' / ('%s-f1' % t)).exists())
        pid = int((run / 'agent-pid.txt').read_text(encoding='utf-8'))
        self.assertTrue(gone(pid))
        time.sleep(0.5)
        self.assertFalse((run / 'agent-done.txt').exists())                       # it never reached the end
        task = self.get(t)
        self.assertEqual((task['failed_attempts'], task['result_refs'], task['status']), (0, [], 'claimed'))
        self.assertEqual(self.kinds(t, 'agent-a'), ['task_ack', 'task_claim'])    # no finish, no fail, no question
        self.assert_says_stopped('agent-a')
        self.assertTrue([e for e in self.events() if e['event'] == 'stopped' and e.get('task_id') == t])
        self.assertEqual(runner.run_once()['status'], 'stopped')                  # the flag is still up: nothing starts
        self.assertFalse((self.runs / 'agent-a' / ('%s-f1' % t)).exists())
        # Flag removed: the next poll resumes on the same live lease, in a fresh run folder, and finishes.
        self.lower_flag()
        self.assertEqual(runner.run_once()['status'], 'submitted')
        done = self.get(t)
        self.assertEqual((done['status'], done['failed_attempts']), ('ready_for_review', 0))
        self.assertEqual(self.kinds(t, 'agent-a'), ['task_ack', 'task_claim', 'task_finish'])    # one claim only
        self.assertTrue((self.runs / 'agent-a' / ('%s-f1' % t) / 'RESULT.md').exists())

    def test_the_agents_own_child_dies_too(self):
        t = self.agent_task('SPAWN_CHILD_AND_SLEEP')
        self.flag_when_agent_runs(t, ready='pids.json')              # only once the agent has started its child
        result = self.agent().run_once()
        self.assertEqual(result['status'], 'stopped')
        run = self.runs / 'agent-a' / ('%s-f1.stopped-1' % t)
        deadline = time.monotonic() + 10
        while not (run / 'pids.json').exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        pids = json.loads((run / 'pids.json').read_text(encoding='utf-8'))
        self.assertTrue(gone(pids['agent']))
        self.assertTrue(gone(pids['child']))                                      # the whole process tree, not just the agent

    @unittest.skipUnless(os.name == 'nt', 'job objects are Windows only')
    def test_without_a_job_object_the_taskkill_fallback_still_stops_the_agent(self):
        t = self.agent_task('SLEEP 60')
        self.flag_when_agent_runs(t)
        with mock.patch.object(aw.AgentJob, '_join', side_effect=OSError('no job today')):
            result = self.agent().run_once()
        self.assertEqual(result['status'], 'stopped')
        self.assertTrue([e for e in self.events() if e['event'] == 'agent_job_unavailable'])
        run = self.runs / 'agent-a' / ('%s-f1.stopped-1' % t)
        self.assertTrue(gone(int((run / 'agent-pid.txt').read_text(encoding='utf-8'))))
        self.assertEqual(self.get(t)['failed_attempts'], 0)

    def test_a_run_folder_that_cannot_be_set_aside_falls_back_to_asking_the_owner(self):
        t = self.agent_task('SLEEP 20')
        self.flag_when_agent_runs(t)
        with mock.patch.object(aw.os, 'rename', side_effect=PermissionError('locked')):
            result = self.agent().run_once()
        self.assertEqual(result['status'], 'stopped')
        self.assertTrue([e for e in self.events() if e['event'] == 'run_folder_not_set_aside'])
        self.lower_flag()
        again = self.agent().run_once()
        self.assertEqual((again['status'], again['reason']), ('asked', 'interrupted'))       # safe, never a second agent
        self.assertEqual(self.get(t)['failed_attempts'], 0)

    def test_room_pause_mid_run_still_kills_the_agent_without_the_flag(self):
        keeper = self.room.store.people['keeper']
        t = self.agent_task('SLEEP 8')

        def pause_once_the_agent_runs():
            deadline = time.monotonic() + 30
            folder = self.runs / 'agent-a' / ('%s-f1' % t)
            while not (folder / 'agent-pid.txt').exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.room.store.execute(keeper, 'pause', {'authority_id': 'crew-one'}, 'pause-mid')
        pauser = threading.Thread(target=pause_once_the_agent_runs)
        pauser.start()
        self.addCleanup(pauser.join, 30)
        result = self.agent(renew_below=1799, slice_seconds=0.1).run_once()
        self.assertEqual((result['status'], result['code']), ('error', 'lease'))
        run =self.runs / 'agent-a' / ('%s-f1' % t)
        self.assertTrue(gone(int((run / 'agent-pid.txt').read_text(encoding='utf-8'))))
        time.sleep(0.5)
        self.assertFalse((run / 'agent-done.txt').exists())
        self.assertEqual(self.get(t)['failed_attempts'], 0)


class AgentJobTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'job objects are Windows only')
    def test_terminate_ends_the_process_and_its_child_at_once(self):
        code = ("import subprocess, sys, time\n"
                "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "print(c.pid, flush=True)\n"
                "time.sleep(60)\n")
        proc = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, **aw.launch_options())
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.kill)
        job = aw.AgentJob(proc)
        self.assertIsNone(job.error)
        self.assertIsNotNone(job.handle)
        child = int(proc.stdout.readline())
        started = time.monotonic()
        self.assertTrue(job.terminate())
        proc.wait(10)
        self.assertLess(time.monotonic() - started, 3)                             # not the ~6 s taskkill takes here
        self.assertTrue(gone(child))
        job.close()
        self.assertIsNone(job.handle)
        self.assertFalse(job.terminate())                                          # closed: nothing to ask
        job.close()                                                                # closing twice is harmless

    def test_no_job_off_windows_or_when_told_not_to(self):
        class Fake:
            pid = 1
        job = aw.AgentJob(Fake(), os_name='posix')
        self.assertEqual((job.handle, job.error, job.terminate()), (None, None, False))
        job.close()


if __name__ == '__main__':
    unittest.main(argv=sys.argv[:1])
