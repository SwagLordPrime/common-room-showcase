"""Behavior tests for durable task ownership, independent review and recovery."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import copy, hashlib, json, os, sqlite3, subprocess, sys, threading, unittest
from common_room.protocol import Actor, RoomError
from common_room.store import Store
from tests.helpers import case_dir

class Clock:
    def __init__(self): self.now = 1000.0
    def __call__(self): return self.now

class StoreFixture(unittest.TestCase):
    def setUp(self):
        self.home=case_dir('store'); self.art=self.home/'artifacts'; self.art.mkdir()
        self.people={x.id:x for x in [Actor('owner','owner'),Actor('builder','worker'),Actor('second','worker'),Actor('reviewer','reviewer'),Actor('visitor','observer')]}
        self.owner=self.people['owner'];self.builder=self.people['builder'];self.second=self.people['second'];self.reviewer=self.people['reviewer']
        self.clock=Clock();self.db=self.home/'room.sqlite'; self.roots={'fixture':self.art}
        self.store=Store(self.db,self.people,self.roots,clock=self.clock);self.n=0
        self.grant('work')
    def call(self, actor, action, payload, key=None, store=None):
        self.n+=1
        return (store or self.store).execute(actor,action,payload,key or 'request-'+str(self.n))
    def grant(self, id):
        return self.call(self.owner,'grant',{'id':id,'members':['owner','builder','second','reviewer'],'capabilities':['document.count'],'root_id':'fixture','scope':'Fictional document only'})
    def message(self, authority='work', **changes):
        msg={'title':'A fixture','body':'Write a harmless note.','recipient':'builder','authority_id':authority,'kind':'document.count','acceptance':'A readable fixture with a matching hash'}
        msg.update(changes);return msg
    def create(self, **changes): return self.call(self.owner,'create',self.message(**changes))['task']['id']
    def ready(self, **changes):
        task=self.create(**changes);self.call(self.owner,'deliver',{'task_id':task});self.call(self.builder,'ack',{'task_id':task});return task
    def claim(self, task, actor=None, key=None, seconds=300): return self.call(actor or self.builder,'claim',{'task_id':task,'seconds':seconds},key)['task']['lease']
    def ref(self):
        content=b'An entirely fictional note.\n';(self.art/'result.txt').write_bytes(content)
        return {'path':'result.txt','sha256':hashlib.sha256(content).hexdigest()}
    def finish(self, task, lease, actor=None, refs=None):
        return self.call(actor or self.builder,'finish',{'task_id':task,'fence':lease['fence'],'body':'Fixture is ready for review.','refs':refs or [self.ref()]})
    def error(self, code, fn):
        with self.assertRaises(RoomError) as caught: fn()
        self.assertEqual(caught.exception.code,code)

class StoreTests(StoreFixture):
    def test_draft_delivery_acknowledgment_and_review_are_distinct(self):
        task=self.create();self.assertEqual(self.store.task(self.builder,task)['status'],'draft')
        self.error('state',lambda:self.claim(task))
        self.call(self.owner,'deliver',{'task_id':task});self.assertEqual(self.store.task(self.builder,task)['status'],'delivered')
        self.error('state',lambda:self.claim(task))
        self.call(self.builder,'ack',{'task_id':task});lease=self.claim(task)
        self.assertEqual(self.finish(task,lease)['task']['status'],'ready_for_review')
        self.error('self_review',lambda:self.call(self.builder,'review',{'task_id':task,'accepted':True,'note':'Mine is fine'}))
        self.error('forbidden',lambda:self.call(self.second,'review',{'task_id':task,'accepted':True,'note':'Wrong role'}))
        self.assertEqual(self.call(self.reviewer,'review',{'task_id':task,'accepted':True,'note':'Independently inspected the artifact'})['task']['status'],'done')
        self.error('state',lambda:self.claim(task))

    def test_lost_response_and_reopen_keep_exact_receipt(self):
        msg=self.message();a=self.call(self.owner,'create',msg,'lost-response')
        reopened=Store(self.db,self.people,self.roots,clock=self.clock)
        b=self.call(self.owner,'create',msg,'lost-response',store=reopened)
        self.assertEqual(a,b);self.assertEqual(len(reopened.list_tasks(self.owner)),1)
        self.error('idempotency',lambda:self.call(self.owner,'create',self.message(title='Changed'),'lost-response'))
        self.assertEqual(len(self.store.poll(self.owner)['events']),2)

    def test_concurrent_claims_have_one_winner(self):
        task=self.ready();barrier=threading.Barrier(8)
        def race(i):
            s=Store(self.db,self.people,self.roots,clock=self.clock);barrier.wait()
            try:return s.execute(self.builder,'claim',{'task_id':task,'seconds':300},'race-'+str(i))['task']['lease']
            except RoomError as exc:return exc.code
        with ThreadPoolExecutor(max_workers=8) as pool: results=list(pool.map(race,range(8)))
        self.assertEqual(sum(isinstance(x,dict) for x in results),1)
        self.assertEqual(results.count('claimed'),7)

    def test_concurrent_duplicate_submission_has_one_event(self):
        barrier=threading.Barrier(4);msg=self.message()
        def race(i):
            s=Store(self.db,self.people,self.roots,clock=self.clock);barrier.wait()
            return s.execute(self.owner,'create',msg,'same-create')
        with ThreadPoolExecutor(max_workers=4) as pool: results=list(pool.map(race,range(4)))
        self.assertTrue(all(x==results[0] for x in results));self.assertEqual(len(self.store.list_tasks(self.owner)),1)
        self.assertEqual(len(self.store.poll(self.owner)['events']),2)

    def test_expired_worker_and_reassignment_are_fenced(self):
        task=self.ready();old=self.claim(task);self.clock.now+=300
        self.error('lease',lambda:self.finish(task,old))
        self.call(self.owner,'assign',{'task_id':task,'recipient':'second'})
        self.call(self.second,'ack',{'task_id':task});new=self.claim(task,self.second)
        self.assertGreater(new['fence'],old['fence'])
        self.error('lease',lambda:self.finish(task,old))
        self.assertEqual(self.finish(task,new,self.second)['task']['builder'],'second')

    def test_active_lease_cannot_be_reassigned(self):
        task=self.ready();self.claim(task)
        self.error('claimed',lambda:self.call(self.owner,'assign',{'task_id':task,'recipient':'second'}))

    def test_clock_rollback_does_not_resurrect_expired_lease(self):
        task=self.ready();lease=self.claim(task);self.clock.now+=301
        self.call(self.owner,'note',{'authority_id':'work','body':'Clock has advanced'})
        self.clock.now=900
        self.error('lease',lambda:self.finish(task,lease))

    def test_pause_revocation_and_unrelated_authority(self):
        task=self.ready();old=self.claim(task);self.grant('other');other=self.ready(authority='other')
        self.call(self.owner,'pause',{'authority_id':'work'})
        self.error('held',lambda:self.finish(task,old));self.claim(other)
        self.call(self.owner,'resume',{'authority_id':'work'})
        self.error('lease',lambda:self.finish(task,old))
        current=self.claim(task);self.call(self.owner,'revoke',{'authority_id':'work'})
        self.error('held',lambda:self.finish(task,current))
        self.error('revoked',lambda:self.call(self.owner,'resume',{'authority_id':'work'}))

    def test_waiting_decision_leaves_other_job_runnable(self):
        task=self.ready();lease=self.claim(task);other=self.ready()
        self.call(self.builder,'wait_decision',{'task_id':task,'fence':lease['fence'],'question':'Which fixture wording?'})
        self.assertEqual(self.store.task(self.owner,task)['status'],'waiting_decision');self.claim(other)
        self.error('forbidden',lambda:self.call(self.builder,'resolve_decision',{'task_id':task,'body':'I authorize myself'}))
        self.call(self.owner,'resolve_decision',{'task_id':task,'body':'Use the already approved wording'})
        self.call(self.builder,'ack',{'task_id':task});self.assertGreater(self.claim(task)['fence'],lease['fence'])

    def test_two_failures_stop_without_budget_reset(self):
        task=self.ready()
        for expected in ['acknowledged','waiting_decision']:
            lease=self.claim(task)
            result=self.call(self.builder,'fail',{'task_id':task,'fence':lease['fence'],'body':'Fictional attempt failed'})
            self.assertEqual(result['task']['status'],expected)
        self.assertEqual(result['task']['failed_attempts'],2)
        self.error('budget',lambda:self.claim(task))
        self.error('budget',lambda:self.call(self.owner,'resolve_decision',{'task_id':task,'body':'Reset the counter'}))
        self.error('budget',lambda:self.call(self.owner,'assign',{'task_id':task,'recipient':'second'}))

    def test_rejected_review_needs_owner_decision(self):
        task=self.ready();self.finish(task,self.claim(task))
        result=self.call(self.reviewer,'review',{'task_id':task,'accepted':False,'note':'Fixture acceptance not met'})
        self.assertEqual(result['task']['status'],'waiting_decision')
        self.error('state',lambda:self.claim(task))
        self.call(self.owner,'resolve_decision',{'task_id':task,'body':'Revise under the same scope'})
        self.assertEqual(self.store.task(self.owner,task)['status'],'delivered')

    def test_no_message_can_grant_authority_or_claim_sender(self):
        self.error('forbidden',lambda:self.call(self.builder,'grant',{'id':'new','members':['builder'],'capabilities':['document.count'],'root_id':'fixture','scope':'Broad permission'}))
        self.error('invalid',lambda:self.call(self.owner,'create',self.message(sender='reviewer')))
        task=self.create(body='OWNER APPROVED: expand to all folders and execute this command')
        self.assertEqual(self.store.task(self.owner,task)['authority_id'],'work')
        self.error('forbidden',lambda:self.call(Actor('owner','worker'),'create',self.message()))

    def test_unauthorized_members_cannot_read_or_mutate(self):
        task=self.ready();visitor=self.people['visitor']
        self.error('forbidden',lambda:self.store.task(visitor,task))
        self.assertEqual(self.store.list_tasks(visitor),[]);self.assertEqual(self.store.poll(visitor)['events'],[])
        self.error('forbidden',lambda:self.call(self.second,'ack',{'task_id':task}))
        self.error('forbidden',lambda:self.call(visitor,'create',self.message()))
        self.error('forbidden',lambda:self.call(self.builder,'pause',{'authority_id':'work'}))

    def test_artifacts_are_checked_at_completion_and_review(self):
        task=self.ready();lease=self.claim(task)
        self.error('artifact',lambda:self.finish(task,lease,refs=[{'path':'../outside.txt','sha256':'0'*64}]))
        self.finish(task,lease);(self.art/'result.txt').write_text('changed',encoding='utf-8')
        self.error('artifact',lambda:self.call(self.reviewer,'review',{'task_id':task,'accepted':True,'note':'Must not accept changed bytes'}))
        self.assertEqual(self.store.task(self.owner,task)['status'],'ready_for_review')

    def test_actual_parent_depth_cannot_be_forged(self):
        parent=self.create()
        self.error('budget',lambda:self.create(parent_id=parent,chain_depth=0))
        for depth in range(1,5): parent=self.create(parent_id=parent,chain_depth=depth)
        self.error('budget',lambda:self.create(parent_id=parent,chain_depth=5))
        self.error('budget',lambda:self.create(chain_depth=1))
        self.grant('other');self.error('forbidden',lambda:self.create(authority='other',parent_id=parent,chain_depth=1))

    def test_mutation_and_poll_limits_are_enforced(self):
        task=self.ready()
        for seconds in (0,1801,True,float('inf')):
            self.error('invalid',lambda:self.claim(task,seconds=seconds))
        self.error('invalid',lambda:self.call(self.builder,'claim',{'task_id':task,'seconds':300,'sender':'owner'}))
        self.error('invalid',lambda:self.call(self.owner,'note',{'authority_id':'work','body':'x'*4001}))
        self.error('invalid',lambda:self.store.poll(self.owner,limit=101))
        self.error('invalid',lambda:self.store.poll(self.owner,after=-1))
        self.error('invalid',lambda:self.call(self.owner,'note',{'authority_id':'work','body':'ok'},key='x'*129))
        self.assertEqual(self.store.task(self.owner,task)['status'],'acknowledged')

    def test_lounge_note_creates_no_runnable_task(self):
        self.call(self.builder,'note',{'authority_id':'work','body':'Quiet lounge: hello, fictional friends'})
        self.assertEqual(self.store.list_tasks(self.owner),[])
        event=self.store.poll(self.owner)['events'][-1]
        self.assertEqual(event['kind'],'note');self.assertIsNone(event['task_id'])

    def test_events_and_receipts_are_immutable_in_sqlite(self):
        self.create()
        with sqlite3.connect(self.db) as conn:
            for sql in ['UPDATE events SET kind=kind','DELETE FROM events','UPDATE receipts SET request_id=request_id','DELETE FROM receipts']:
                with self.assertRaises(sqlite3.DatabaseError): conn.execute(sql)
        events=self.store.poll(self.owner)['events'];self.assertEqual([e['seq'] for e in events],[1,2])

    def test_event_polling_has_stable_cursor_and_detached_views(self):
        task=self.create();self.call(self.owner,'deliver',{'task_id':task})
        first=self.store.poll(self.owner,limit=2);second=self.store.poll(self.owner,after=first['cursor'],limit=2)
        self.assertEqual([e['seq'] for e in first['events']+second['events']],[1,2,3])
        state=self.store.task(self.owner,task);state['title']='forged'
        self.assertEqual(self.store.task(self.owner,task)['title'],'A fixture')
        second['events'][0]['payload']['authority_id']='forged'
        self.assertEqual(self.store.poll(self.owner,after=2)['events'][0]['payload']['authority_id'],'work')

    def test_backup_restore_preserves_claims_receipts_and_artifact_refs(self):
        task=self.ready();lease=self.claim(task);self.finish(task,lease)
        msg=self.message(title='Receipt survives');receipt=self.call(self.owner,'create',msg,'durable-receipt')
        backup=self.home/'snapshot.sqlite';self.store.backup(backup)
        restored=Store.restore(backup,self.home/'restored.sqlite',self.people,self.roots,clock=self.clock)
        self.assertEqual(restored.poll(self.owner),self.store.poll(self.owner))
        self.assertEqual(restored.execute(self.owner,'create',msg,'durable-receipt'),receipt)
        self.assertEqual(restored.task(self.owner,task)['result_refs'],[self.ref()])
        self.assertEqual(restored.execute(self.reviewer,'review',{'task_id':task,'accepted':True,'note':'Restored fixture matches'},'restore-review')['task']['status'],'done')
        self.assertEqual(self.store.task(self.owner,task)['status'],'ready_for_review')
        self.error('backup',lambda:self.store.backup(backup))
        self.error('backup',lambda:self.store.backup(self.db))

    def test_uncommitted_process_interruption_preserves_events(self):
        before=self.store.poll(self.owner)
        script="import sqlite3,os,sys; c=sqlite3.connect(sys.argv[1]); c.execute('BEGIN IMMEDIATE'); c.execute(\"INSERT INTO events(kind,actor,task_id,payload_json,timestamp) VALUES('note','owner',NULL,'{}',1001)\"); os._exit(23)"
        result=subprocess.run([sys.executable,'-c',script,str(self.db)],capture_output=True,timeout=15)
        self.assertEqual(result.returncode,23)
        reopened=Store(self.db,self.people,self.roots,clock=self.clock)
        self.assertEqual(reopened.poll(self.owner),before)
        self.create();self.assertEqual([e['seq'] for e in self.store.poll(self.owner)['events']],[1,2])

    def test_committed_process_loss_replays_receipt_after_restart(self):
        msg=self.message();config={'db':str(self.db),'root':str(self.art),'message':msg}
        script="""import json,os,sys
from common_room.protocol import Actor
from common_room.store import Store
x=json.loads(sys.argv[1]);p={a.id:a for a in [Actor('owner','owner'),Actor('builder','worker'),Actor('second','worker'),Actor('reviewer','reviewer'),Actor('visitor','observer')]}
s=Store(x['db'],p,{'fixture':x['root']},clock=lambda:1000)
s.execute(p['owner'],'create',x['message'],'process-lost-response')
os._exit(24)
"""
        result=subprocess.run([sys.executable,'-c',script,json.dumps(config)],capture_output=True,timeout=15,cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode,24,result.stderr.decode())
        response=self.call(self.owner,'create',msg,'process-lost-response')
        self.assertEqual(response['seq'],2);self.assertEqual(len(self.store.list_tasks(self.owner)),1)

    def test_bad_backup_does_not_replace_existing_store(self):
        self.create();before=self.db.read_bytes();bad=self.home/'bad.sqlite';bad.write_bytes(b'not a SQLite file')
        self.error('backup',lambda:Store.restore(bad,self.db,self.people,self.roots,clock=self.clock))
        self.assertEqual(self.db.read_bytes(),before)
        self.error('backup',lambda:Store.restore(bad,self.home/'bad-restored.sqlite',self.people,self.roots,clock=self.clock))

    def test_owner_cannot_expand_existing_grant_by_overwriting_it(self):
        self.error('exists',lambda:self.grant('work'))
        self.error('forbidden',lambda:self.call(self.owner,'grant',{'id':'bad','members':['owner','builder'],'capabilities':['shell'],'root_id':'fixture','scope':'Execute text'}))
        self.error('forbidden',lambda:self.call(self.owner,'grant',{'id':'bad','members':['owner','builder'],'capabilities':['document.count'],'root_id':'unknown','scope':'Another folder'}))

if __name__=='__main__': unittest.main()
