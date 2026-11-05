"""Real authenticated HTTP controls; provider substitute remains offline."""
import json,time,uuid,threading
from tests.test_store import StoreFixture
from tests import test_board
from tests.test_connection import FakeTransport
from common_room.connection import Dispatcher,RunController

class ConnectionBoardTests(StoreFixture):
    request=test_board.BoardTests.request
    login=test_board.BoardTests.login
    command=test_board.BoardTests.command
    def setUp(self):
        super().setUp()
        from common_room.board import make_server
        self.server=make_server(self.store,{'fixture-owner':self.owner,'fixture-builder':self.builder,'fixture-reviewer':self.reviewer},clock=self.clock)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.host='127.0.0.1:'+str(self.server.server_address[1]);self.origin='http://'+self.host;self.cookie=None
        self.addCleanup(lambda:(self.server.shutdown(),self.server.server_close(),self.thread.join(2)))
        self.transport=FakeTransport()
        self.dispatcher=Dispatcher(self.store,self.builder,self.reviewer,self.transport,self.db.parent/'transport')
        self.server.controller=RunController(self.dispatcher)
    def tearDown(self):
        self.server.controller.close()
        test_board.BoardTests.tearDown(self)
    def run_request(self,t,role='worker',key=None):return self.request('POST','/api/run',{'task_id':t,'role':role,'request_id':key or uuid.uuid4().hex})
    def settled(self):
        for _ in range(100):
            state=json.loads(self.request('GET','/api/state')[2])
            if not state['connection']['busy']:return state
            time.sleep(.02)
        self.fail('Run did not settle')
    def test_owner_browser_can_finish_both_steps_and_retry_click(self):
        t=self.ready();self.login();key=uuid.uuid4().hex
        self.assertEqual(self.run_request(t,key=key)[0],200);state=self.settled()
        self.assertEqual(state['tasks'][0]['status'],'ready_for_review')
        self.assertEqual(self.run_request(t,key=key)[0],200);self.assertEqual(len(self.transport.calls),1)
        self.assertEqual(self.run_request(t,'reviewer')[0],200);state=self.settled()
        self.assertEqual(state['tasks'][0]['status'],'done');self.assertEqual(len(self.transport.calls),2)
        self.assertTrue(state['fixture_only']);self.assertEqual(state['connection']['mode'],'offline-test')
    def test_auth_origin_role_and_payload_cannot_start_provider(self):
        t=self.ready();self.assertEqual(self.run_request(t)[0],401)
        self.login('fixture-builder');self.assertEqual(self.run_request(t)[0],403)
        self.login();self.assertEqual(self.request('POST','/api/run',{'task_id':t,'role':'worker','request_id':'a'},headers={'Origin':'http://evil.test'})[0],403)
        self.assertEqual(self.run_request(t,'shell')[0],403)
        self.assertEqual(self.request('POST','/api/run',{'task_id':t,'role':'worker','request_id':'x','command':'bad'})[0],400)
        self.assertEqual(self.transport.calls,[])
    def test_retry_key_cannot_be_reused_for_another_action(self):
        t=self.ready();self.login();key=uuid.uuid4().hex
        self.assertEqual(self.run_request(t,key=key)[0],200);self.settled()
        self.assertEqual(self.run_request(t,'reviewer',key)[0],409);self.assertEqual(len(self.transport.calls),1)
    def test_restart_preserves_request_and_does_not_repeat(self):
        t=self.ready();self.login();key=uuid.uuid4().hex
        self.run_request(t,key=key);self.settled();self.server.controller.close()
        self.server.controller=RunController(self.dispatcher)
        self.assertEqual(self.run_request(t,key=key)[0],200);self.assertEqual(len(self.transport.calls),1)
    def test_browser_remains_responsive_while_run_active(self):
        t=self.ready();self.login();self.transport.hook=lambda:time.sleep(.4)
        self.run_request(t);state=json.loads(self.request('GET','/api/state')[2])
        self.assertTrue(state['connection']['busy']);self.settled()

    def test_unaccepted_connection_is_visible_but_cannot_run(self):
        self.server.controller.close();self.server.controller=RunController(self.dispatcher,enabled=False,blocked_reason='Connection check pending')
        t=self.ready();self.login();state=json.loads(self.request('GET','/api/state')[2])
        self.assertFalse(state['connection']['can_run']);self.assertEqual(state['connection']['blocked_reason'],'Connection check pending')
        self.assertEqual(self.run_request(t)[0],409);self.assertEqual(self.transport.calls,[])
