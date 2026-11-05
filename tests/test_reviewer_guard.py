"""Refuse jobs nobody could ever review (cr-upgrade-2026-10-01, task core-reviewer-guard).

The gap (found by proof-silent, Lounge #553): Store.intake accepted a root job into an authority whose
members were only the owner and the worker, so the job could never be reviewed (a builder cannot review
its own work and only a reviewer-role member can). What the store does now, and what these tests hold it to:

  * `intake` (a new job) and `request_help` are refused `forbidden`, with nothing created, when the
    authority has no reviewer-role member other than the assigned worker and, for a helper, its sender.
    The message names the authority and says a reviewer must be enrolled.
  * authorities that have a distinct reviewer are unaffected.
  * existing jobs are untouched, and a repeat of an existing brief still answers as a duplicate.
  * a log written before this change replays to the same states (fold() is not touched).
"""
import copy, json, pathlib, sys, unittest
from unittest import mock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # so evidence.py can run this file as a script in a clean copy
from common_room.protocol import RoomError
from common_room.store import Store, fold
from tests.helpers import case_dir
from tests.test_cancel_and_delegated_decisions import (
    DelegationFixture, LEGACY_EVENTS_JSON, LEGACY_STATE_SHA256, legacy_scenario, state_digest)
from tests.test_store_strike_kinds import drive_old_style_commands, load_store_at

BASELINE_COMMIT = 'eacc128'  # the submitted core-stop commit: this task's RED base


class GuardFixture(DelegationFixture):
    """The delegation fixture (owner, builder, second, third, reviewer, visitor) plus an authority with no reviewer."""
    def setUp(self):
        super().setUp()
        self.call(self.owner, 'grant_handoff', {'id': 'bare', 'members': ['owner', 'builder', 'second'],
            'capabilities': ['handoff.record'], 'root_id': 'fixture', 'scope': 'Fictional work with nobody to review it',
            'source_namespaces': ['estate']})

    def intake(self, authority, ref, recipient='builder', title=None):
        msg = self.message(authority, kind='handoff.record', recipient=recipient, title=title or 'Job ' + ref)
        return self.call(self.owner, 'intake', {'source_namespace': 'estate', 'external_reference': ref, 'message': msg})

    def old_style_job(self, ref):
        """A job in the reviewer-less authority exactly as the store queued it before this change."""
        with mock.patch.object(Store, '_reviewer_guard', create=True, new=lambda *args, **kwargs: None):
            return self.intake('bare', ref)['task']['id']

    def refused(self, fn):
        with self.assertRaises(RoomError) as caught: fn()
        self.assertEqual(caught.exception.code, 'forbidden')
        return caught.exception.args[0] if caught.exception.args else str(caught.exception)

    def counts(self):
        return len(self.store.list_tasks(self.owner)), len(self.store.poll(self.owner, 0, 100)['events'])


class GuardTests(GuardFixture):
    def test_intake_into_an_authority_with_no_reviewer_is_refused_and_nothing_is_created(self):
        before = self.counts()
        message = self.refused(lambda: self.intake('bare', 'bare-1'))
        self.assertEqual(self.counts(), before)
        self.assertIn('Authority bare ', message)
        self.assertIn('no reviewer', message); self.assertIn('Enroll a reviewer', message)
        # the same refusal every time, for any recipient the authority has
        self.refused(lambda: self.intake('bare', 'bare-2', recipient='second'))
        self.assertEqual(self.counts(), before)

    def test_request_help_is_refused_when_the_authority_has_no_reviewer(self):
        parent = self.old_style_job('bare-parent')
        self.call(self.owner, 'deliver', {'task_id': parent}); self.call(self.builder, 'ack', {'task_id': parent})
        lease = self.claim(parent)
        before = self.counts()
        message = self.refused(lambda: self.helper(parent, lease))
        self.assertEqual(self.counts(), before)
        self.assertIn('bare', message); self.assertIn('Enroll a reviewer', message)

    def test_the_guard_leaves_out_the_assigned_worker_and_the_sender(self):
        authority = {'id': 'x', 'members': ['owner', 'builder', 'second', 'reviewer']}
        self.store._reviewer_guard(authority, 'builder')                       # reviewer is distinct: passes
        self.store._reviewer_guard(authority, 'builder', 'second')
        for recipient, sender in (('reviewer', None), ('builder', 'reviewer'), ('reviewer', 'reviewer')):
            with self.assertRaises(RoomError) as caught: self.store._reviewer_guard(authority, recipient, sender)
            self.assertEqual(caught.exception.code, 'forbidden')

    def test_an_authority_with_a_distinct_reviewer_still_accepts_intake_and_request_help(self):
        task = self.root('with-reviewer'); self.call(self.builder, 'ack', {'task_id': task}); lease = self.claim(task)
        child = self.helper(task, lease)
        self.assertEqual(self.store.task(self.owner, child)['parent_id'], task)
        # a repeat of the same brief is still answered as a duplicate, not refused
        again = self.intake('orch', 'with-reviewer', title='Workstream with-reviewer')  # the brief self.root() sent
        self.assertEqual(again['task']['id'], task)

    def test_existing_jobs_are_untouched(self):
        job = self.old_style_job('bare-old')
        self.call(self.owner, 'deliver', {'task_id': job}); self.call(self.builder, 'ack', {'task_id': job})
        lease = self.claim(job)
        self.assertEqual(self.store.task(self.owner, job)['status'], 'claimed')
        self.assertEqual(lease['holder'], 'builder')
        # the same brief sent again is the same job, answered as before, not refused by the new rule
        events_before = self.counts()[1]
        self.assertEqual(self.intake('bare', 'bare-old')['task']['id'], job)
        self.assertEqual(self.counts()[1], events_before + 1)
        self.assertEqual(self.store.poll(self.owner, 0, 100)['events'][-1]['kind'], 'task_intake_duplicate')
        # a brief with a different reference is a new job and is refused
        self.refused(lambda: self.intake('bare', 'bare-new'))

    def test_a_document_task_created_by_the_owner_is_not_covered_by_this_guard(self):
        """Scope pin: the brief covers intake and request_help only; `create` for a document.count authority is unchanged."""
        bare_documents = self.call(self.owner, 'grant', {'id': 'bare-doc', 'members': ['owner', 'builder'],
            'capabilities': ['document.count'], 'root_id': 'fixture', 'scope': 'Fictional document only'})
        task = self.call(self.owner, 'create', self.message('bare-doc'))['task']['id']
        self.assertEqual(self.store.task(self.owner, task)['status'], 'draft')


class ReplayIdentityTests(unittest.TestCase):
    """GUARDS, green on the base by design: the new refusal must not change what old logs mean."""
    def test_a_recorded_log_folds_to_the_recorded_state(self):
        self.assertEqual(state_digest(fold, json.loads(LEGACY_EVENTS_JSON)), LEGACY_STATE_SHA256)

    def test_the_recorded_scenario_still_writes_the_recorded_log(self):
        self.assertEqual(legacy_scenario(Store, case_dir('guard-legacy')), json.loads(LEGACY_EVENTS_JSON))

    def test_old_style_commands_write_the_same_events_as_the_base_store(self):
        base = load_store_at(BASELINE_COMMIT)
        if base is None: self.skipTest('base commit ' + BASELINE_COMMIT + ' is not reachable through git here')
        self.assertEqual(drive_old_style_commands(Store, case_dir('guard-now')), drive_old_style_commands(base.Store, case_dir('guard-then')))

    def test_a_log_written_by_the_base_store_folds_the_same_under_both(self):
        base = load_store_at(BASELINE_COMMIT)
        if base is None: self.skipTest('base commit ' + BASELINE_COMMIT + ' is not reachable through git here')
        events = drive_old_style_commands(base.Store, case_dir('guard-fold'))
        self.assertEqual(json.dumps(base.fold(copy.deepcopy(events)), sort_keys=True), json.dumps(fold(copy.deepcopy(events)), sort_keys=True))


if __name__ == '__main__':
    unittest.main()
