"""Owner recovery of an exhausted task, and the result that an owner decision must not destroy.

Added 2026-09-13 (plumb) on Shane's instruction, with Astra unavailable. These tests cover
two changes and one deliberate non-change:

  * new action `reinstate` - a separate, written, logged owner act that clears the failure
    counter on a task the budget has stopped. It exists for the case the budget was never
    designed to cover: a chat-driven worker whose lease lapsed because nobody was running
    a turn.
  * `resolve_decision` no longer erases `result_refs` and `result_body`.
  * the budget rule itself is UNCHANGED. `resolve_decision`, `claim` and `assign` still
    refuse at two recorded failures, and the last test here guards that they still do.
"""
import unittest
from tests.test_store import StoreFixture

class ReinstateTests(StoreFixture):
    def exhaust(self):
        """Two actual failures still require an explicit owner reinstatement."""
        task=self.ready()
        for _ in (1,2):
            lease=self.claim(task)
            stopped=self.call(self.builder,'fail',{'task_id':task,'fence':lease['fence'],'body':'Fixture actual failure'})['task']
        self.assertEqual(stopped['failed_attempts'],2)
        self.error('budget',lambda:self.claim(task))
        return task

    def test_owner_reinstates_a_task_the_budget_has_stopped(self):
        task=self.exhaust()
        back=self.call(self.owner,'reinstate',{'task_id':task,'body':'The worker was idle, not failing.'})['task']
        self.assertEqual(back['failed_attempts'],0)
        self.assertEqual(back['status'],'delivered')
        self.assertIsNone(back['lease']);self.assertIsNone(back['builder']);self.assertIsNone(back['decision'])
        self.call(self.builder,'ack',{'task_id':task})
        self.assertIsNotNone(self.claim(task))

    def test_reinstatement_records_its_reason_and_keeps_the_failure_history(self):
        task=self.exhaust()
        before=self.store.task(self.owner,task)
        reason='UNIQUE_REINSTATE_REASON_FIXTURE'
        back=self.call(self.owner,'reinstate',{'task_id':task,'body':reason})['task']
        self.assertIn(reason,back['decision_response'])
        self.assertIn('2 recorded failure',back['decision_response'])
        # clearing the counter must not rewrite what happened
        self.assertEqual(back['failure_note'],before['failure_note'])
        self.assertEqual(back['failed_actor'],before['failed_actor'])

    def test_only_the_authority_owner_may_reinstate(self):
        task=self.exhaust()
        for actor in (self.builder,self.second,self.reviewer):
            self.error('forbidden',lambda a=actor:self.call(a,'reinstate',{'task_id':task,'body':'Not mine to give'}))
        self.assertEqual(self.store.task(self.owner,task)['failed_attempts'],2)

    def test_reinstatement_needs_failures_and_a_written_reason(self):
        clean=self.ready()
        self.error('state',lambda:self.call(self.owner,'reinstate',{'task_id':clean,'body':'Nothing to undo'}))
        task=self.exhaust()
        self.error('invalid',lambda:self.call(self.owner,'reinstate',{'task_id':task,'body':'   '}))
        self.assertEqual(self.store.task(self.owner,task)['failed_attempts'],2)

    def test_reinstatement_will_not_interrupt_live_work(self):
        task=self.ready();lease=self.claim(task)
        self.call(self.builder,'fail',{'task_id':task,'fence':lease['fence'],'body':'actual failed work'})
        self.claim(task)
        self.error('claimed',lambda:self.call(self.owner,'reinstate',{'task_id':task,'body':'Not while someone holds it'}))
        self.clock.now+=301
        self.assertEqual(self.call(self.owner,'reinstate',{'task_id':task,'body':'Lease lapsed, taking it back'})['task']['failed_attempts'],0)

    def test_an_owner_decision_no_longer_destroys_the_submitted_result(self):
        task=self.ready();ref=self.ref()
        self.finish(task,self.claim(task),refs=[ref])
        submitted=self.store.task(self.owner,task)
        self.assertTrue(submitted['result_body']);self.assertTrue(submitted['result_refs'])
        self.call(self.reviewer,'review',{'task_id':task,'accepted':False,'note':'Three wording fixes, no rework'})
        after=self.call(self.owner,'resolve_decision',{'task_id':task,'body':'Make the three fixes'})['task']
        self.assertEqual(after['status'],'delivered')
        self.assertEqual(after['result_body'],submitted['result_body'])
        self.assertEqual(after['result_refs'],submitted['result_refs'])

    def test_the_failure_budget_itself_is_unchanged(self):
        """Guard: reinstate is an addition, not a loosening of the existing rule."""
        task=self.exhaust()
        self.error('budget',lambda:self.call(self.owner,'resolve_decision',{'task_id':task,'body':'Reset the counter'}))
        self.error('budget',lambda:self.call(self.owner,'assign',{'task_id':task,'recipient':'second'}))
        self.error('budget',lambda:self.claim(task))

if __name__=='__main__':unittest.main()
