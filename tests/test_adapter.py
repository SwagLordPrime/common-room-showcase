"""Actual bounded fixture work, safe restart, review and idle behavior."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock
import hashlib,json,subprocess,sys,threading,time
from common_room.adapter import FixtureWorker,FixtureReviewer
from common_room.protocol import RoomError
from common_room.store import Store
from tests.test_store import StoreFixture

class AdapterTests(StoreFixture):
    def worker(self):return FixtureWorker(self.store,self.builder)
    def artifact(self,task):
        t=self.store.task(self.owner,task);return self.art/t['result_refs'][0]['path']
    def test_real_count_and_separate_review(self):
        task=self.ready(body='One cafÃ©\nthree; $(never execute)')
        w=self.worker();result=w.run_once();self.assertEqual(result['status'],'ready_for_review')
        data=json.loads(self.artifact(task).read_text());self.assertEqual(data['words'],5)
        self.assertEqual(data['characters'],len('One cafÃ©\nthree; $(never execute)'))
        self.assertEqual(w.metrics['processes'],1)
        self.assertEqual(FixtureReviewer(self.store,self.reviewer).run_once()['status'],'done')
        self.assertEqual(self.store.task(self.owner,task)['reviewer'],'reviewer')
    def test_idle_and_lounge_start_no_process_or_events(self):
        w=self.worker();before=self.store.poll(self.owner)['cursor']
        with mock.patch('common_room.adapter.subprocess.run',side_effect=AssertionError('No child allowed')) as child:
            for _ in range(10):self.assertEqual(w.run_once()['status'],'idle')
            self.call(self.owner,'note',{'authority_id':'work','body':'Company only'})
            for _ in range(10):self.assertEqual(w.run_once()['status'],'idle')
        self.assertEqual(child.call_count,0);self.assertEqual(w.metrics,{'processes':0,'model_calls':0})
        self.assertEqual(self.store.poll(self.owner)['cursor'],before+1)
    def test_drafts_other_workers_and_owner_decisions_are_skipped(self):
        self.create();self.create(recipient='second');t=self.ready();lease=self.claim(t)
        self.call(self.builder,'wait_decision',{'task_id':t,'fence':lease['fence'],'question':'Wait'})
        with mock.patch('common_room.adapter.subprocess.run',side_effect=AssertionError('No child')):
            self.assertEqual(self.worker().run_once()['status'],'idle')
    def test_paused_scope_does_not_block_unrelated_work(self):
        blocked=self.ready();self.call(self.owner,'pause',{'authority_id':'work'})
        self.grant('other');good=self.ready(authority='other')
        self.assertEqual(self.worker().run_once()['task_id'],good)
        self.assertEqual(self.store.task(self.owner,blocked)['status'],'acknowledged')
    def test_timeout_twice_exhausts_shared_budget(self):
        t=self.ready();w=self.worker()
        with mock.patch('common_room.adapter.subprocess.run',side_effect=subprocess.TimeoutExpired('fixture',30)) as child:
            self.assertEqual(w.run_once()['status'],'acknowledged')
            self.assertEqual(w.run_once()['status'],'waiting_decision')
            self.assertEqual(w.run_once()['status'],'idle');self.assertEqual(child.call_count,2)
        self.assertEqual(self.store.task(self.owner,t)['failed_attempts'],2)
    def test_active_restart_waits_then_expiry_reclaims(self):
        t=self.ready();old=self.claim(t);self.assertEqual(self.worker().run_once()['status'],'idle')
        self.clock.now+=301;self.assertEqual(self.worker().run_once()['status'],'ready_for_review')
        current=self.store.task(self.owner,t);self.assertEqual(current['failed_attempts'],0)
        self.error('lease',lambda:self.finish(t,old))
    def test_two_expired_claims_allow_work_without_false_failure_strikes(self):
        t=self.ready();self.claim(t);self.clock.now+=301;self.claim(t);self.clock.now+=301
        self.assertEqual(self.worker().run_once()['status'],'ready_for_review')
        self.assertEqual(self.store.task(self.owner,t)['failed_attempts'],0)
    def test_lost_finish_response_is_not_duplicate_work(self):
        t=self.ready();original=self.store.execute
        def lost(actor,action,payload,key):
            result=original(actor,action,payload,key)
            if action=='finish':raise ConnectionError('Committed response lost')
            return result
        with mock.patch.object(self.store,'execute',side_effect=lost):self.assertEqual(self.worker().run_once()['status'],'ready_for_review')
        refs=self.store.task(self.owner,t)['result_refs'];self.assertEqual(self.worker().run_once()['status'],'idle')
        self.assertEqual(len(list(self.art.rglob('*.json'))),1);self.assertEqual(refs,self.store.task(self.owner,t)['result_refs'])
    def test_process_interrupt_after_claim_resumes_safely(self):
        t=self.ready()
        code="""import os
from common_room.store import Store
from common_room.protocol import Actor
from common_room.adapter import FixtureWorker
people={x.id:x for x in [Actor('owner','owner'),Actor('builder','worker'),Actor('second','worker'),Actor('reviewer','reviewer'),Actor('visitor','observer')]}
store=Store(DB,people,{'fixture':ART},clock=lambda:1000.0)
original=store.execute
def interrupted(actor,action,payload,key):
 result=original(actor,action,payload,key)
 if action=='claim':os._exit(23)
 return result
store.execute=interrupted
FixtureWorker(store,people['builder']).run_once()
""".replace('DB',repr(str(self.db))).replace('ART',repr(str(self.art)))
        p=subprocess.run([sys.executable,'-B','-c',code],cwd=Path(__file__).resolve().parents[1],capture_output=True,timeout=10)
        self.assertEqual(p.returncode,23,p.stderr);self.assertEqual(self.store.task(self.owner,t)['status'],'claimed')
        self.assertEqual(self.worker().run_once()['status'],'idle');self.clock.now+=301
        self.assertEqual(self.worker().run_once()['status'],'ready_for_review')
    def test_simultaneous_runners_start_one_process(self):
        self.ready();barrier=threading.Barrier(2);workers=[self.worker(),self.worker()]
        def run(w):barrier.wait();return w.run_once()['status']
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,workers))
        self.assertEqual(results.count('ready_for_review'),1);self.assertEqual(sum(w.metrics['processes'] for w in workers),1)
    def test_tampered_result_is_rejected(self):
        t=self.ready();self.worker().run_once();p=self.artifact(t);data=json.loads(p.read_text());data['words']=999;p.write_text(json.dumps(data))
        self.assertEqual(FixtureReviewer(self.store,self.reviewer).run_once()['status'],'waiting_decision')
    def test_output_collision_is_preserved_and_failed(self):
        t=self.ready();(self.art/'fixture-count').write_text('Do not replace')
        w=self.worker();self.assertEqual(w.run_once()['status'],'acknowledged')
        self.assertEqual((self.art/'fixture-count').read_text(),'Do not replace');self.assertEqual(w.metrics['processes'],0)
    def test_revocation_during_child_prevents_publication(self):
        t=self.ready();real=subprocess.run
        def revoke(*args,**kwargs):
            result=real(*args,**kwargs);self.call(self.owner,'revoke',{'authority_id':'work'});return result
        with mock.patch('common_room.adapter.subprocess.run',side_effect=revoke):self.assertEqual(self.worker().run_once()['status'],'stale')
        self.assertFalse(list(self.art.rglob('*.json')));self.assertNotEqual(self.store.task(self.owner,t)['status'],'ready_for_review')
    def test_identity_and_timeout_configuration_refused(self):
        with self.assertRaises(RoomError):FixtureWorker(self.store,self.owner)
        with self.assertRaises(RoomError):FixtureReviewer(self.store,self.builder)
        for n in [0,31,True,float('nan')]:
            with self.assertRaises(RoomError):FixtureWorker(self.store,self.builder,timeout=n)
