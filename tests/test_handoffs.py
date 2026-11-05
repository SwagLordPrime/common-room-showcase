"""Handoff contract tests: ownership, deduplication and bounded renewal."""
import copy, hashlib
from concurrent.futures import ThreadPoolExecutor
from common_room.protocol import RoomError
from common_room.store import Store
from tests.test_store import StoreFixture

class HandoffTests(StoreFixture):
    def grant_handoff(self, name='handoffs'):
        return self.call(self.owner,'grant_handoff',{'id':name,'members':['owner','builder','second','reviewer'],'capabilities':['handoff.record'],'root_id':'fixture','scope':'Fictional handoff records only','source_namespaces':['estate']})
    def intake(self, ref='request-one', authority='handoffs', **changes):
        message=self.message(authority,kind='handoff.record',**changes)
        return {'source_namespace':'estate','external_reference':ref,'message':message}
    def handoff(self):
        self.grant_handoff()
        return self.call(self.owner,'intake',self.intake())['task']['id']
    def test_duplicate_reference_survives_reassignment_and_restart(self):
        task=self.handoff(); original=self.intake()
        self.call(self.owner,'assign',{'task_id':task,'recipient':'second'})
        store=Store(self.db,self.people,self.roots,clock=self.clock)
        duplicate=self.call(self.owner,'intake',original,store=store)
        self.assertEqual(duplicate['task']['id'],task)
        self.assertEqual(duplicate['task']['recipient'],'second')
        self.assertEqual(len(store.list_tasks(self.owner)),1)
        changed=copy.deepcopy(original);changed['message']['acceptance']='Different acceptance'
        self.error('idempotency',lambda:self.call(self.owner,'intake',changed))
    def test_concurrent_intake_one_task_and_cross_scope_independent(self):
        self.grant_handoff()
        def intake(i):
            s=Store(self.db,self.people,self.roots,clock=self.clock)
            return s.execute(self.owner,'intake',self.intake(),'parallel-'+str(i))['task']['id']
        with ThreadPoolExecutor(max_workers=4) as pool:ids=list(pool.map(intake,range(4)))
        self.assertEqual(len(set(ids)),1)
        self.grant_handoff('other')
        other=self.call(self.owner,'intake',self.intake(authority='other'))['task']['id']
        self.assertNotEqual(other,ids[0])
    def test_intake_requires_owner_namespace_and_exact_capability(self):
        self.grant_handoff()
        self.error('forbidden',lambda:self.call(self.builder,'intake',self.intake()))
        p=self.intake();p['source_namespace']='unknown'
        self.error('forbidden',lambda:self.call(self.owner,'intake',p))
        self.error('forbidden',lambda:self.call(self.owner,'create',self.message('handoffs',kind='handoff.record')))
        self.error('forbidden',lambda:self.call(self.owner,'intake',self.intake(authority='work')))
        p=self.intake();p['message']['sender']='reviewer'
        self.error('invalid',lambda:self.call(self.owner,'intake',p))
    def test_renewal_uses_now_and_replay_cannot_extend(self):
        task=self.handoff();self.call(self.owner,'deliver',{'task_id':task});self.call(self.builder,'ack',{'task_id':task})
        lease=self.claim(task);self.clock.now+=20
        p={'task_id':task,'fence':lease['fence'],'seconds':1800}
        first=self.call(self.builder,'renew',p,'renew-one')
        self.assertEqual(first['task']['lease']['until'],self.clock.now+1800)
        self.clock.now+=100
        self.assertEqual(self.call(self.builder,'renew',p,'renew-one'),first)
        self.error('invalid',lambda:self.call(self.builder,'renew',dict(p,seconds=1801)))
        self.error('lease',lambda:self.call(self.second,'renew',p))
        self.error('lease',lambda:self.call(self.builder,'renew',dict(p,fence=lease['fence']+1)))
        self.call(self.owner,'pause',{'authority_id':'handoffs'})
        self.error('held',lambda:self.call(self.builder,'renew',p))
    def test_expired_renewal_and_two_failures_stay_blocked(self):
        task=self.handoff();self.call(self.owner,'deliver',{'task_id':task});self.call(self.builder,'ack',{'task_id':task})
        lease=self.claim(task);self.clock.now+=301
        self.error('lease',lambda:self.call(self.builder,'renew',{'task_id':task,'fence':lease['fence'],'seconds':300}))
        lease=self.claim(task)
        self.call(self.builder,'fail',{'task_id':task,'fence':lease['fence'],'body':'Fictional failure'})
        self.assertEqual(self.store.task(self.owner,task)['failed_attempts'],1)
        lease=self.claim(task)
        self.call(self.builder,'fail',{'task_id':task,'fence':lease['fence'],'body':'Second actual failure'})
        self.assertEqual(self.store.task(self.owner,task)['failed_attempts'],2)
        self.error('budget',lambda:self.call(self.owner,'resolve_decision',{'task_id':task,'body':'Try again'}))
    def test_separate_handoff_review_and_digest_validation(self):
        task=self.handoff();self.call(self.owner,'deliver',{'task_id':task});self.call(self.builder,'ack',{'task_id':task});lease=self.claim(task)
        self.finish(task,lease)
        self.error('self_review',lambda:self.call(self.builder,'review',{'task_id':task,'accepted':True,'note':'Self'}))
        (self.art/'result.txt').write_text('Changed',encoding='utf-8')
        self.error('artifact',lambda:self.call(self.reviewer,'review',{'task_id':task,'accepted':True,'note':'Inspect'}))
        self.ref()
        self.assertEqual(self.call(self.reviewer,'review',{'task_id':task,'accepted':True,'note':'Checked independently'})['task']['status'],'done')
    def test_count_capability_cannot_renew_or_run_handoff(self):
        from common_room.adapter import FixtureWorker,FixtureReviewer
        task=self.handoff();self.call(self.owner,'deliver',{'task_id':task})
        self.assertEqual(FixtureWorker(self.store,self.builder).run_once()['status'],'idle')
        self.assertEqual(FixtureReviewer(self.store,self.reviewer).run_once()['status'],'idle')
        count=self.ready();lease=self.claim(count)
        self.error('forbidden',lambda:self.call(self.builder,'renew',{'task_id':count,'fence':lease['fence'],'seconds':300}))
