"""Connection boundary tests use fictional transports, never a provider."""
from pathlib import Path
from unittest import mock
from concurrent.futures import ThreadPoolExecutor
import hashlib,json,re,sys,time,unittest,threading,uuid,subprocess,os
from tests.test_store import StoreFixture
from common_room.connection import Dispatcher,bounded_process
from common_room.protocol import RoomError

class FakeTransport:
    fixture_only=True
    def __init__(self):self.calls=[];self.hook=None;self.response=None
    def invoke(self,role,body,artifact,folder,invocation_id):
        self.calls.append((role,body,artifact,invocation_id))
        if self.hook:self.hook()
        if self.response is not None:return self.response
        return {'output':{'words':len(re.findall(r'\S+',body)),'characters':len(body)} if role=='worker' else {'accepted':True,'note':'Counts checked independently.'},'provider_run_id':'fake-'+uuid.uuid4().hex}

class ConnectionTests(StoreFixture):
    def setUp(self):super().setUp();self.transport=FakeTransport()
    def dispatcher(self):return Dispatcher(self.store,self.builder,self.reviewer,self.transport,self.db.parent/'transport')
    def run_task(self,t,role='worker'):return self.dispatcher().run_task(self.owner,t,role)
    def test_separate_runs_end_to_end_and_no_replay(self):
        t=self.ready(body='One café\nthree');self.assertEqual(self.run_task(t)['status'],'ready_for_review')
        task=self.store.task(self.owner,t);data=json.loads((self.art/task['result_refs'][0]['path']).read_text())
        self.assertEqual(data['words'],3);self.assertTrue(data['fixture_only'])
        self.assertEqual(self.run_task(t,'reviewer')['status'],'done')
        self.assertEqual(self.run_task(t)['status'],'idle');self.assertEqual(self.run_task(t,'reviewer')['status'],'idle')
        self.assertEqual(len(self.transport.calls),2);self.assertNotEqual(self.transport.calls[0][3],self.transport.calls[1][3])
        self.assertEqual(self.store.task(self.owner,t)['reviewer'],self.reviewer.id)
    def test_idle_lounge_draft_and_unassigned_start_nothing(self):
        d=self.create();other=self.create(recipient='second');self.call(self.owner,'note',{'authority_id':'work','body':'hello'})
        for t in (d,other):self.assertEqual(self.run_task(t)['status'],'idle')
        self.assertEqual(self.transport.calls,[])
    def test_owner_and_separate_roles_required(self):
        t=self.ready();d=self.dispatcher()
        for actor in (self.builder,self.reviewer,self.people['visitor']):
            with self.assertRaises(RoomError):d.run_task(actor,t,'worker')
        with self.assertRaises(RoomError):Dispatcher(self.store,self.builder,self.builder,self.transport,self.db.parent/'bad')
        with self.assertRaises(RoomError):d.run_task(self.owner,t,'shell')
        self.assertEqual(self.transport.calls,[])
    def test_paused_and_revoked_refuse_without_start(self):
        t=self.ready();self.call(self.owner,'pause',{'authority_id':'work'})
        self.assertEqual(self.run_task(t)['status'],'held');self.call(self.owner,'resume',{'authority_id':'work'})
        self.call(self.owner,'revoke',{'authority_id':'work'});self.assertEqual(self.run_task(t)['status'],'held')
        self.assertEqual(self.transport.calls,[])
    def test_revoked_during_provider_never_publishes(self):
        t=self.ready();self.transport.hook=lambda:self.call(self.owner,'revoke',{'authority_id':'work'})
        self.assertEqual(self.run_task(t)['status'],'stale');self.assertFalse(list(self.art.rglob('*.json')))
    def test_invalid_counts_and_extra_fields_fail(self):
        for data in ({'words':999,'characters':3},{'words':1,'characters':3,'command':'bad'},{'words':True,'characters':3}):
            t=self.ready(body='abc');self.transport.response={'output':data,'provider_run_id':'fake'}
            self.assertEqual(self.run_task(t)['status'],'acknowledged');self.assertEqual(self.store.task(self.owner,t)['failed_attempts'],1)
    def test_timeout_two_attempt_budget_and_no_third(self):
        t=self.ready();self.transport.hook=lambda:(_ for _ in ()).throw(TimeoutError())
        self.assertEqual(self.run_task(t)['status'],'acknowledged');self.assertEqual(self.run_task(t)['status'],'waiting_decision')
        self.assertEqual(self.run_task(t)['status'],'idle');self.assertEqual(len(self.transport.calls),2)
    def test_ambiguous_interruption_blocks_after_restart_and_expiry(self):
        t=self.ready();self.transport.hook=lambda:(_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):self.run_task(t)
        self.clock.now+=301;self.transport.hook=None
        self.assertEqual(self.run_task(t)['status'],'interrupted');self.assertEqual(len(self.transport.calls),1)
    def test_simultaneous_owner_clicks_launch_once(self):
        t=self.ready();self.transport.hook=lambda:time.sleep(.1)
        with ThreadPoolExecutor(max_workers=2) as p:results=list(p.map(lambda _:self.run_task(t),range(2)))
        self.assertEqual(len(self.transport.calls),1);self.assertIn('ready_for_review',[r['status'] for r in results])
    def test_lost_finish_response_does_not_repeat_provider(self):
        t=self.ready();original=self.store.execute
        def lost(actor,action,payload,key):
            result=original(actor,action,payload,key)
            if action=='finish':raise ConnectionError('lost')
            return result
        with mock.patch.object(self.store,'execute',side_effect=lost):self.assertEqual(self.run_task(t)['status'],'ready_for_review')
        self.assertEqual(self.run_task(t)['status'],'idle');self.assertEqual(len(self.transport.calls),1)
    def test_tampered_artifact_never_reaches_reviewer(self):
        t=self.ready();self.run_task(t);task=self.store.task(self.owner,t)
        (self.art/task['result_refs'][0]['path']).write_text('{}')
        self.assertEqual(self.run_task(t,'reviewer')['status'],'waiting_decision');self.assertEqual(len(self.transport.calls),1)
    def test_review_rejection_and_malformed_output_cannot_accept(self):
        for data in ({'accepted':False,'note':'incorrect'},{'accepted':True,'note':'ok','extra':1}):
            t=self.ready();self.transport.response=None;self.run_task(t)
            self.transport.response={'output':data,'provider_run_id':'fake'}
            self.assertEqual(self.run_task(t,'reviewer')['status'],'waiting_decision')

class ProcessTests(unittest.TestCase):
    def test_bounded_output_and_exit(self):
        from tests.helpers import case_dir
        root=case_dir('connected-process')
        result=bounded_process([sys.executable,'-I','-c','import sys; print(sys.stdin.read())'],b'fixture',root,5)
        self.assertEqual(result['returncode'],0);self.assertIn(b'fixture',result['stdout'])
        with self.assertRaises(RoomError):bounded_process([sys.executable,'-I','-c','print("x"*200000)'],b'',root,5,output_limit=1024)
    def test_deadline_kills_descendant_before_it_writes(self):
        from tests.helpers import case_dir
        root=case_dir('connected-timeout');marker=root/'escaped.txt'
        child='import time,pathlib; time.sleep(2); pathlib.Path('+repr(str(marker))+').write_text("escaped")'
        parent='import subprocess,sys,time; subprocess.Popen([sys.executable,"-I","-c",'+repr(child)+']); time.sleep(20)'
        with self.assertRaises(TimeoutError):bounded_process([sys.executable,'-I','-c',parent],b'',root,.4)
        time.sleep(2.1);self.assertFalse(marker.exists())
