"""A headless run the STOP flag stopped still shows up in the usage log (task usage-stop, crew-audit #864).

The flag kills the agent mid-run (task work-stop); the usage meter (task work-usage) must then write exactly one
failed line with the seconds the agent really ran and the exit code it left, and leave every other path as it was.

RED note: on the base commit (work-stop, 1787ee1) a stopped run writes no line at all.
Every agent is tests/fake_agent.py; tests.helpers refuses any non-loopback socket.
"""
import sys
from pathlib import Path
if __name__ == '__main__':          # runnable as a script too (tools/evidence.py runs `python tests/<file> .`)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import json, threading, time, unittest
from tests.test_work_stop import StopCase
from common_room import usage


class StoppedRunUsageTests(StopCase):
    def rows(self, seat='agent-a'):
        problems = []
        rows = usage.read_usage(self.room.root, seat=seat, problems=problems)
        self.assertEqual(problems, [])
        return rows

    def flag_when_agent_runs(self, task_id):
        def work():
            deadline = time.monotonic() + 60
            folder = self.runs / 'agent-a' / ('%s-f1' % task_id)
            while not (folder / 'agent-pid.txt').exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            time.sleep(0.4)                          # let the agent run a measurable moment before the flag
            self.raise_flag()
        thread = threading.Thread(target=work)
        thread.start()
        self.addCleanup(thread.join, 60)

    def test_a_run_the_flag_stopped_writes_exactly_one_failed_line_with_seconds_and_exit_code(self):
        t = self.agent_task('SLEEP 8')
        self.flag_when_agent_runs(t)
        runner = self.agent()
        result = runner.run_once()
        self.assertEqual((result['status'], result['task_id']), ('stopped', t))
        (row,) = self.rows()                                                        # exactly one line
        self.assertEqual((row['kind'], row['task_id'], row['outcome']), ('headless', t, 'failed'))
        self.assertGreater(row['seconds'], 0.3)                                     # it really ran before the kill
        self.assertIsNotNone(row['exit'])                                           # the code the killed agent left
        self.assertNotEqual(row['exit'], 0)
        self.assertNotIn('prompt_tokens', row)                                      # headless: no tokens
        self.assertEqual(self.get(t)['failed_attempts'], 0)                         # the usage line is not a room strike
        # Still stopped on later polls: nothing runs, nothing more is written.
        for _ in range(3):
            self.assertEqual(runner.run_once()['status'], 'stopped')
        self.assertEqual(len(self.rows()), 1)
        # The flag goes, the job runs to its end: one more line, finished. Two jobs' attempts, two lines.
        self.lower_flag()
        self.assertEqual(runner.run_once()['status'], 'submitted')
        self.assertEqual([r['outcome'] for r in self.rows()], ['failed', 'finished'])
        text = (self.room.root / 'usage' / 'agent-a.jsonl').read_text(encoding='utf-8')
        for secret in ('Do the fixture thing', 'Fake result', 'Fixture task', str(self.work)):
            self.assertNotIn(secret, text)                                          # still numbers and names only

    def test_a_flag_found_before_any_agent_starts_writes_no_line(self):
        self.agent_task('RESULT')
        self.raise_flag()
        runner = self.agent()
        for _ in range(3):
            self.assertEqual(runner.run_once()['status'], 'stopped')
        self.assertEqual(self.rows(), [])                                           # nothing ran: nothing metered
        self.lower_flag()
        self.assertEqual(runner.run_once()['status'], 'submitted')
        self.assertEqual([r['outcome'] for r in self.rows()], ['finished'])

    def test_a_normal_run_is_unchanged_one_finished_line(self):
        t = self.agent_task('RESULT')
        self.assertEqual(self.agent().run_once()['status'], 'submitted')
        (row,) = self.rows()
        self.assertEqual((row['task_id'], row['outcome'], row['exit']), (t, 'finished', 0))

    def test_a_run_the_room_refuses_is_unchanged_one_failed_line(self):
        keeper = self.room.store.people['keeper']
        t = self.agent_task('SLEEP 8')

        def pause_once_the_agent_runs():
            deadline = time.monotonic() + 60
            folder = self.runs / 'agent-a' / ('%s-f1' % t)
            while not (folder / 'agent-pid.txt').exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.room.store.execute(keeper, 'pause', {'authority_id': 'crew-one'}, 'pause-mid')
        pauser = threading.Thread(target=pause_once_the_agent_runs)
        pauser.start()
        self.addCleanup(pauser.join, 60)
        result = self.agent(renew_below=1799, slice_seconds=0.1).run_once()
        self.assertEqual((result['status'], result['code']), ('error', 'lease'))      # the room's own refusal, not the flag
        (row,) = self.rows()
        self.assertEqual((row['task_id'], row['outcome']), (t, 'failed'))

    def test_an_agent_question_is_unchanged_one_question_line(self):
        t = self.agent_task('QUESTION')
        self.assertEqual(self.agent().run_once()['reason'], 'question')
        (row,) = self.rows()
        self.assertEqual((row['task_id'], row['outcome']), (t, 'question'))

    def test_the_local_worker_writes_no_line_for_a_stopped_cycle(self):
        self.local_task()
        self.raise_flag()
        self.assertEqual(self.local().run_once()['status'], 'stopped')
        self.assertEqual(self.rows('local-a'), [])
        self.assertFalse((self.room.root / 'usage' / 'local-a.jsonl').exists())

    def test_the_format_doc_says_so(self):
        doc = (Path(usage.__file__).resolve().parents[1] / 'USAGE_FORMAT.md').read_text(encoding='utf-8')
        self.assertIn('A headless run the STOP flag stopped', doc)
        self.assertIn('exactly one line, outcome `failed`', doc)
        self.assertIn('seconds it really ran and the exit code', doc)


if __name__ == '__main__':
    unittest.main(argv=sys.argv[:1])
