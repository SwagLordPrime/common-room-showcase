"""Read-side S0 contracts; fictional rooms and the recorded field-size benchmark."""
import copy, hashlib, json, threading, unittest, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common_room import board
from common_room.protocol import canonical
from tests.test_store import StoreFixture
from tests import test_handoff_client, test_relay

QUIET=3*86400
FIELDS=('body','result_body','acceptance','review_note','failure_note','decision','decision_response')
# MEASURED own scoped state3055, receipt under room/s0/evidence; no source text copied.
SIZES={'body':1578,'result_body':576,'acceptance':323,'review_note':1026,'failure_note':1130,'decision':0,'decision_response':559}
DETAIL_SIZE=818

class DietStateTests(StoreFixture):
    def setUp(self):
        super().setUp()
        self.clock.now=1000000.0
        self.server=board.make_server(self.store,{'fixture-owner':self.owner,'fixture-builder':self.builder},clock=self.clock)
        self.addCleanup(self.server.server_close)
    def state(self, actor=None):
        return board.state_body(self.server,actor or self.builder)
    def snapshot(self,status='done',age=QUIET+1):
        task=self.ready(body='B'*700,acceptance='A'*700)
        events,auth,tasks=self.store._snapshot(self.owner)
        t=tasks[task];t['status']=status
        for key in FIELDS:t[key]=(key+'🙂')*150
        for e in events:
            if e['task_id']==task:
                e['timestamp']=self.clock.now-age
                e['payload']['changes']={'status':status,'result_body':'H'*700}
        return task,events,auth,tasks
    def view_snapshot(self,snapshot):
        task,events,auth,tasks=snapshot
        with patch.object(self.store,'_snapshot',return_value=(events,auth,tasks)):
            return self.state()
    def test_renew_history_omitted_while_all_lease_fields_survive(self):
        test_handoff_client.HandoffHTTPTests.grant_handoff(self)
        task=self.call(self.owner,'intake',test_handoff_client.HandoffHTTPTests.intake(self))['task']['id']
        self.call(self.owner,'deliver',{'task_id':task});self.call(self.builder,'ack',{'task_id':task})
        lease=self.claim(task,seconds=300)
        self.call(self.builder,'renew',{'task_id':task,'fence':lease['fence'],'seconds':400})
        before=self.store.task(self.builder,task)
        state=self.state();got=next(t for t in state['tasks'] if t['id']==task)
        self.assertNotIn('task_renew',[h['kind'] for h in state['history']])
        for key in ('lease','fence','status','failed_attempts'):self.assertEqual(got[key],before[key])
    def test_done_quiet_strictly_more_than_three_days_clips_named_fields(self):
        s=self.snapshot();before=copy.deepcopy(s)
        state=self.view_snapshot(s);t=state['tasks'][0]
        self.assertIs(t.get('clipped'),True)
        for key in FIELDS:self.assertEqual(t[key],before[3][s[0]][key][:300])
        for h in state['history']:self.assertLessEqual(len(h['detail']),200)
        self.assertEqual(s,before,'Serialization must not mutate the snapshot')
    def test_cancelled_quiet_task_uses_same_clipping(self):
        s=self.snapshot('cancelled');t=self.view_snapshot(s)['tasks'][0]
        self.assertIs(t.get('clipped'),True)
        self.assertEqual(t['body'],s[3][s[0]]['body'][:300])
    def test_exactly_three_days_preserves_all_text(self):
        s=self.snapshot(age=QUIET);t=self.view_snapshot(s)['tasks'][0]
        self.assertNotIn('clipped',t)
        for key in FIELDS:self.assertEqual(t[key],s[3][s[0]][key])
        self.assertTrue(any(len(h['detail'])>200 for h in self.view_snapshot(s)['history']))
    def test_recent_terminal_task_preserves_text(self):
        s=self.snapshot(age=QUIET-1);t=self.view_snapshot(s)['tasks'][0]
        self.assertNotIn('clipped',t);self.assertEqual(t['body'],s[3][s[0]]['body'])
    def test_open_tasks_never_clip_even_when_old(self):
        for status in ('delivered','acknowledged','claimed','waiting_decision','ready_for_review'):
            with self.subTest(status=status):
                s=self.snapshot(status);t=self.view_snapshot(s)['tasks'][-1]
                self.assertNotIn('clipped',t)
                for key in FIELDS:self.assertEqual(t[key],s[3][s[0]][key])
    def test_newest_task_activity_controls_quiet_age(self):
        s=self.snapshot(age=QUIET*2)
        s[1][-1]['timestamp']=self.clock.now-1
        t=self.view_snapshot(s)['tasks'][0]
        self.assertNotIn('clipped',t);self.assertEqual(t['body'],s[3][s[0]]['body'])
    def test_unicode_limits_are_characters_and_null_values_survive(self):
        s=self.snapshot();s[3][s[0]]['body']='漢🙂'*400;s[3][s[0]]['decision']=None
        t=self.view_snapshot(s)['tasks'][0]
        self.assertEqual(t['body'],('漢🙂'*400)[:300]);self.assertEqual(len(t['body']),300)
        self.assertIsNone(t['decision'])
    def test_age_and_uptime_use_exact_fake_clock_without_changing_sentences(self):
        self.server.contacts['builder']=self.clock.now-70
        self.server.contacts['reviewer']=self.clock.now-170
        self.clock.now+=25
        state=self.state();people={p['id']:p for p in state['participants']}
        self.assertEqual(people['builder'].get('contact_age_s'),95)
        self.assertEqual(people['reviewer'].get('contact_age_s'),195)
        self.assertIsNone(people['second'].get('contact_age_s'))
        self.assertEqual(state.get('uptime_s'),25)
        self.assertEqual(people['builder']['contact'],'Fixture contact stale')
        self.assertEqual(people['second']['contact'],'No fixture contact')
    def test_seventy_and_one_seventy_are_distinct(self):
        self.server.contacts['builder']=self.clock.now-70;self.server.contacts['reviewer']=self.clock.now-170
        people={p['id']:p for p in self.state()['participants']}
        self.assertEqual(people['builder'].get('contact_age_s'),70)
        self.assertEqual(people['reviewer'].get('contact_age_s'),170)
    def test_contact_age_whole_seconds_and_active_sentence(self):
        self.server.contacts['builder']=self.clock.now-59.9
        p=next(p for p in self.state()['participants'] if p['id']=='builder')
        self.assertEqual(p.get('contact_age_s'),59);self.assertEqual(p['contact'],'Fixture browser active')
    def test_real_store_log_and_full_task_unchanged_after_state(self):
        task=self.ready(body='B'*700);lease=self.claim(task);self.finish(task,lease)
        self.call(self.reviewer,'review',{'task_id':task,'accepted':True,'note':'R'*600})
        self.clock.now+=QUIET+1
        before=self.store._snapshot(self.owner);self.state();after=self.store._snapshot(self.owner)
        self.assertEqual(before,after);self.assertEqual(self.store.task(self.builder,task)['body'],'B'*700)
    def test_synthetic400_measured_field_sizes_at_most65_percent(self):
        _,auth,_=self.store._snapshot(self.owner);base=self.ready()
        _,_,original=self.store._snapshot(self.owner);template=original[base]
        tasks={};events=[]
        for i in range(400):
            tid='t'+format(i+1,'032x');t=copy.deepcopy(template);t.update(id=tid,status='done')
            for key,size in SIZES.items():t[key]='X'*size if size else None
            tasks[tid]=t
            events.append({'seq':i+1,'kind':'task_review','actor':'reviewer','task_id':tid,'timestamp':self.clock.now-QUIET-1,'payload':{'authority_id':'work','changes':{'status':'done','review_note':'H'*DETAIL_SIZE}}})
        with patch.object(self.store,'_snapshot',return_value=(events,auth,tasks)):
            with patch.object(self.server,'clock',return_value=self.clock.now-QUIET):
                full=self.state()
            bounded=self.state()
        before=len(canonical(full).encode());after=len(canonical(bounded).encode())
        self.assertLessEqual(after/before,.65,(before,after))
        self.assertEqual(sum(t.get('clipped') is True for t in bounded['tasks']),400)

class DietHTTPTests(StoreFixture):
    grant_handoff=test_handoff_client.HandoffHTTPTests.grant_handoff
    intake=test_handoff_client.HandoffHTTPTests.intake
    request=test_handoff_client.HandoffHTTPTests.request
    login=test_handoff_client.HandoffHTTPTests.login
    client=test_handoff_client.HandoffHTTPTests.client
    def setUp(self):
        super().setUp();self.grant_handoff()
        from common_room.handoff_api import CredentialPolicy
        self.tokens={'owner-key':self.owner,'builder-key':self.builder,'review-key':self.reviewer,'visitor-key':self.people['visitor']}
        self.policy=CredentialPolicy(self.tokens,self.clock.now+2000,self.clock)
        self.server=board.make_server(self.store,self.tokens,clock=self.clock,credential_policy=self.policy)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.host=self.server.host;self.origin=self.server.origin;self.cookie=None
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)
    def view(self,task,token='builder-key',extra=None):
        self.login(token);p={'task_id':task};p.update(extra or {})
        status,_,raw=self.request('POST','/api/handoff',{'operation':'task_view','payload':p})
        return status,json.loads(raw)
    def create_task(self):
        return self.call(self.owner,'intake',self.intake(body='PRIVATE_FIXTURE_BODY_'*50))['task']['id']
    def test_task_view_member_gets_full_task_and_all_events_without_writes(self):
        task=self.create_task();self.call(self.owner,'deliver',{'task_id':task});self.call(self.builder,'ack',{'task_id':task})
        lease=self.claim(task);self.call(self.builder,'renew',{'task_id':task,'fence':lease['fence'],'seconds':300})
        before=self.store._snapshot(self.owner);status,value=self.view(task)
        self.assertEqual(status,200,value);self.assertEqual(value['task'],self.store.task(self.builder,task))
        self.assertEqual(value['events'],[e for e in before[0] if e['task_id']==task])
        self.assertIn('task_renew',[e['kind'] for e in value['events']]);self.assertEqual(before,self.store._snapshot(self.owner))
    def test_task_view_refuses_nonmember_and_extra_fields(self):
        task=self.create_task()
        self.assertEqual(self.view(task,'visitor-key')[0],403)
        self.assertEqual(self.view(task,extra={'other':True})[0],400)
        self.assertEqual(self.view('t'+'0'*32)[0],403)
    def test_public_status_reports_exact_owner_state_bytes_and_live_sessions_only(self):
        task=self.create_task();owner=self.client();worker=self.client('builder-key')
        self.server.sessions['expired-fixture']=(self.owner,self.clock.now-1,'owner-key')
        status,_,raw=self.request('GET','/api/status')
        self.assertEqual(status,200);v=json.loads(raw)
        self.assertEqual(v.get('state_bytes'),len(canonical(board.state_body(self.server,self.owner)).encode()))
        self.assertEqual(v.get('live_sessions'),2)
        for x in [task,'PRIVATE_FIXTURE_BODY','owner-key','builder-key']:self.assertNotIn(x,raw.decode())
        owner._request('/api/logout',{});worker._request('/api/logout',{})

class DietRelayTests(test_relay.RelayTestCase):
    def test_relay_forwards_task_view_and_preserves_full_text(self):
        room=self.start();task=self.intake_task(room,body='FULL_TEXT_'*80)
        relay=self.relay(room,['relay-writer'])
        self.write_request(room,'relay-writer','view.json',{'request_id':'full-view','kind':'handoff','operation':'task_view','payload':{'task_id':task}})
        before=self.events(room);relay.process_once()
        response_path=room.root/'relay/relay-writer/responses/resp-full-view.json'
        self.assertTrue(response_path.exists(),'task_view must be forwarded with a correlated response')
        response=self.response(room,'relay-writer','full-view')
        self.assertTrue(response['ok'],response)
        self.assertEqual(response['result']['task']['body'],'FULL_TEXT_'*80)
        self.assertEqual(before,self.events(room))
    def test_relay_state_preserves_numeric_age_and_uptime(self):
        room=self.start();relay=self.relay(room,['relay-writer']);relay.process_once()
        state=json.loads((room.root/'relay/relay-writer/relay-state.json').read_text())
        self.assertIn('uptime_s',state)
        self.assertTrue(all('contact_age_s' in p for p in state['participants']))

if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='.':sys.argv.pop(1)
    unittest.main(verbosity=2)
