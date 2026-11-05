"""Fictional collaboration behavior; no provider or estate workload."""
import hashlib,threading,unittest
from common_room.board import make_server
from common_room.handoff_api import CredentialPolicy
from common_room.participant import Participant
from common_room.protocol import RoomError
from tests.test_store import StoreFixture

class CollaborationTests(StoreFixture):
    def setUp(self):
        super().setUp()
        self.call(self.owner,'grant_handoff',{'id':'shared','members':['owner','builder','second','reviewer'],'capabilities':['handoff.record'],'root_id':'fixture','scope':'Fictional collaboration only','source_namespaces':['estate']})
    def parent(self):
        msg=self.message(authority='shared',kind='handoff.record')
        t=self.call(self.owner,'intake',{'source_namespace':'estate','external_reference':'parent','message':msg})['task']['id']
        self.call(self.owner,'deliver',{'task_id':t});self.call(self.builder,'ack',{'task_id':t});lease=self.claim(t)
        return t,{'task_id':t,'fence':lease['fence'],'recipient':'second','title':'Check one piece','body':'Check only the assigned fictional piece.','acceptance':'Saved result with separate review.'}
    def test_helper_has_separate_ownership_idempotent_receipt_and_review(self):
        t,p=self.parent();before=self.store.task(self.owner,t)
        first=self.call(self.builder,'request_help',p,'helper-1');second=self.call(self.builder,'request_help',p,'helper-1')
        self.assertEqual(first,second);c=first['task'];self.assertEqual(c['status'],'delivered');self.assertEqual(c['parent_id'],t);self.assertEqual(c['chain_depth'],1)
        self.assertEqual(c['authority_id'],'shared');self.assertEqual(self.store.task(self.owner,t),before)
        self.call(self.second,'ack',{'task_id':c['id']});lease=self.claim(c['id'],self.second)
        data=b'Checked the fictional helper piece';(self.art/'helper.txt').write_bytes(data)
        self.call(self.second,'finish',{'task_id':c['id'],'fence':lease['fence'],'body':'Done','refs':[{'path':'helper.txt','sha256':hashlib.sha256(data).hexdigest()}]})
        with self.assertRaises(RoomError):self.call(self.second,'review',{'task_id':c['id'],'accepted':True,'note':'self'})
        self.assertEqual(self.call(self.reviewer,'review',{'task_id':c['id'],'accepted':True,'note':'Read saved piece'})['task']['status'],'done')
    def test_helper_refuses_wrong_holder_stale_fence_scope_and_nonworker(self):
        t,p=self.parent()
        for actor,change in [(self.owner,{}),(self.second,{}),(self.builder,{'fence':p['fence']+1}),(self.builder,{'recipient':'builder'}),(self.builder,{'recipient':'reviewer'}),(self.builder,{'recipient':'visitor'}),(self.builder,{'authority_id':'work'})]:
            with self.assertRaises(RoomError):self.call(actor,'request_help',dict(p,**change))
        self.clock.now+=301
        with self.assertRaises(RoomError):self.call(self.builder,'request_help',p)
    def test_helper_child_budget_and_pause(self):
        t,p=self.parent()
        for i in range(8):self.call(self.builder,'request_help',dict(p,title='Piece '+str(i)))
        with self.assertRaises(RoomError):self.call(self.builder,'request_help',p)
        self.call(self.owner,'pause',{'authority_id':'shared'})
        with self.assertRaises(RoomError):self.call(self.builder,'request_help',p)

    def test_legacy_count_parent_can_finish_with_linked_draft(self):
        parent=self.ready();lease=self.claim(parent)
        self.create(parent_id=parent,chain_depth=1,recipient='second')
        result=self.finish(parent,lease)
        self.assertEqual(result['task']['status'],'ready_for_review')
    def test_portable_helper_receipt_keeps_parent_link(self):
        from types import SimpleNamespace
        from common_room.handoff_api import handoff_operation
        t,p=self.parent();child=self.call(self.builder,'request_help',p)['task']
        receipt=handoff_operation(SimpleNamespace(store=self.store),self.builder,'receipt',{'task_id':child['id']})
        self.assertEqual(receipt['parent_id'],t);self.assertEqual(receipt['chain_depth'],1)

class ConnectedSessionTests(StoreFixture):
    def setUp(self):
        super().setUp();self.policy=CredentialPolicy({'owner-key':self.owner},self.clock.now+28800,self.clock)
        self.server=make_server(self.store,{'owner-key':self.owner},clock=self.clock,credential_policy=self.policy)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.c=Participant(self.server.origin,'owner-key');self.c.login()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)
    def test_authenticated_polling_keeps_connection_but_idle_expires(self):
        for _ in range(8):self.clock.now+=600;self.c.state()
        self.clock.now+=901
        with self.assertRaises(ValueError):self.c.state()
    def test_polling_cannot_pass_key_expiry_or_restore_after_clock_reversal(self):
        start=self.clock.now
        for _ in range(47):self.clock.now+=600;self.c.state()
        self.clock.now=start+28800
        with self.assertRaises(ValueError):self.c.state()
        self.clock.now=start+200
        with self.assertRaises(ValueError):self.c.login()
    def test_revocation_and_logout_invalidate_refreshed_sessions(self):
        self.clock.now+=800;self.c.state();self.policy.revoke('owner')
        with self.assertRaises(ValueError):self.c.state()
    def test_backward_clock_does_not_shorten_or_reanimate_idle_session(self):
        self.clock.now+=800;self.c.state();self.clock.now-=700;self.c.state();self.clock.now+=1000;self.c.state()
        self.clock.now+=901
        with self.assertRaises(ValueError):self.c.state()
