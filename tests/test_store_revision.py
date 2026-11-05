"""Independent expected outcomes for the first store review's budget/history/clock findings."""
import json, sqlite3, unittest
from common_room.protocol import RoomError
from common_room.store import Store
from tests.test_store import StoreFixture

class StoreRevisionTests(StoreFixture):
    def test_rejected_work_consumes_shared_failure_budget(self):
        task=self.ready()
        for count in (1,2):
            self.finish(task,self.claim(task))
            result=self.call(self.reviewer,'review',{'task_id':task,'accepted':False,'note':'Handwritten acceptance not met'})
            self.assertEqual(result['task']['failed_attempts'],count)
            if count==1:
                self.call(self.owner,'resolve_decision',{'task_id':task,'body':'One allowed revision'})
                self.call(self.builder,'ack',{'task_id':task})
        self.error('budget',lambda:self.call(self.owner,'resolve_decision',{'task_id':task,'body':'Third attempt is refused'}))
        self.error('budget',lambda:self.claim(task))

    def test_expired_attempts_release_without_consuming_failure_budget(self):
        task=self.ready();old=self.claim(task)
        for _ in range(3):
            self.clock.now+=301
            state=self.call(self.builder,'claim',{'task_id':task,'seconds':300})['task']
            self.assertEqual(state['failed_attempts'],0)
            self.assertEqual(state['status'],'claimed');self.assertIsNotNone(state['lease'])
        self.error('lease',lambda:self.finish(task,old))

    def test_owner_reassignment_preserves_actual_failures_without_expiry_strikes(self):
        task=self.ready();lease=self.claim(task)
        self.call(self.builder,'fail',{'task_id':task,'fence':lease['fence'],'body':'real prior failure'})
        self.claim(task);self.clock.now+=301
        state=self.call(self.owner,'assign',{'task_id':task,'recipient':'second'})['task']
        self.assertEqual(state['failed_attempts'],1);self.assertEqual(state['failure_note'],'real prior failure')
        self.call(self.second,'ack',{'task_id':task});self.claim(task,self.second);self.clock.now+=301
        state=self.call(self.owner,'assign',{'task_id':task,'recipient':'builder'})['task']
        self.assertEqual(state['failed_attempts'],1);self.assertEqual(state['status'],'delivered')
        self.assertIsNone(state['lease']);self.assertEqual(state['recipient'],'builder')

    def test_explicit_failure_then_expiry_preserves_single_failure(self):
        task=self.ready();lease=self.claim(task)
        self.call(self.builder,'fail',{'task_id':task,'fence':lease['fence'],'body':'First known failure'})
        self.claim(task);self.clock.now+=301
        current=self.call(self.builder,'claim',{'task_id':task,'seconds':300})['task']
        self.assertEqual(current['failed_attempts'],1);self.assertIsNotNone(current['lease'])
        self.assertEqual(current['status'],'claimed')

    def test_first_failure_diagnostic_survives_restart_and_next_failure(self):
        task=self.ready();lease=self.claim(task);diagnostic='UNIQUE_FIRST_FAILURE_CAUSE_FIXTURE'
        self.call(self.builder,'fail',{'task_id':task,'fence':lease['fence'],'body':diagnostic})
        reopened=Store(self.db,self.people,self.roots,clock=self.clock)
        self.assertIn(diagnostic,json.dumps(reopened.poll(self.owner)))
        lease=self.claim(task);self.call(self.builder,'fail',{'task_id':task,'fence':lease['fence'],'body':'Different second cause'})
        self.assertIn(diagnostic,json.dumps(self.store.poll(self.owner)))
        self.assertIn('Different second cause',json.dumps(self.store.poll(self.owner)))

    def test_observed_expiry_cannot_revive_on_clock_rollback_or_restore(self):
        task=self.ready();lease=self.claim(task);before=self.store.poll(self.owner)
        with sqlite3.connect(self.db) as db:receipts=db.execute('SELECT count(*) FROM receipts').fetchone()[0]
        self.clock.now=1301
        self.error('lease',lambda:self.finish(task,lease))
        self.assertEqual(self.store.poll(self.owner),before)
        with sqlite3.connect(self.db) as db:self.assertEqual(db.execute('SELECT count(*) FROM receipts').fetchone()[0],receipts)
        self.clock.now=1001
        reopened=Store(self.db,self.people,self.roots,clock=self.clock)
        self.error('lease',lambda:reopened.execute(self.builder,'finish',{'task_id':task,'fence':lease['fence'],'body':'Still expired','refs':[self.ref()]},'clock-reopen'))
        snapshot=self.home/'clock-snapshot.sqlite';self.store.backup(snapshot)
        restored=Store.restore(snapshot,self.home/'clock-restored.sqlite',self.people,self.roots,clock=self.clock)
        self.error('lease',lambda:restored.execute(self.builder,'finish',{'task_id':task,'fence':lease['fence'],'body':'Expired after restore','refs':[self.ref()]},'clock-restore'))

    def test_old_claim_retry_is_a_receipt_not_a_renewal(self):
        task=self.ready();payload={'task_id':task,'seconds':300}
        first=self.call(self.builder,'claim',payload,'old-claim')
        self.clock.now+=301;second=self.call(self.builder,'claim',payload,'new-claim')
        self.assertEqual(second['task']['failed_attempts'],0)
        replay=self.call(self.builder,'claim',payload,'old-claim')
        self.assertEqual(replay,first)
        self.error('lease',lambda:self.finish(task,replay['task']['lease']))
        self.assertGreater(second['task']['lease']['fence'],first['task']['lease']['fence'])

if __name__=='__main__':unittest.main()
