"""Real localhost requests against authenticated store-backed endpoints."""
import http.client, json, threading, unittest
from common_room.board import make_server
from tests.test_store import StoreFixture

class BoardTests(StoreFixture):
    def setUp(self):
        super().setUp()
        self.server=make_server(self.store,{'fixture-owner':self.owner,'fixture-builder':self.builder,'fixture-reviewer':self.reviewer},clock=self.clock)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.host='127.0.0.1:'+str(self.server.server_address[1]);self.origin='http://'+self.host
        self.cookie=None
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)
    def request(self,method,path,payload=None,headers=None):
        h={'Host':self.host,'Origin':self.origin}
        if self.cookie:h['Cookie']=self.cookie
        if payload is not None:h['Content-Type']='application/json'
        if headers:h.update(headers)
        c=http.client.HTTPConnection(*self.server.server_address,timeout=3)
        c.request(method,path,body=json.dumps(payload).encode() if payload is not None else None,headers=h)
        res=c.getresponse();data=res.read();status=res.status;rh=dict(res.getheaders());c.close()
        return status,rh,data
    def login(self,token='fixture-owner'):
        status,h,data=self.request('POST','/api/login',{'token':token});self.assertEqual(status,200)
        self.cookie=h['Set-Cookie'].split(';')[0];return h,data
    def command(self,action,payload,key='http-request'):
        return self.request('POST','/api/command',{'action':action,'payload':payload,'request_id':key})
    def test_session_required_and_logout_invalidates(self):
        self.assertEqual(self.request('GET','/api/state')[0],401)
        h,body=self.login();self.assertIn('HttpOnly',h['Set-Cookie']);self.assertIn('SameSite=Strict',h['Set-Cookie'])
        self.assertNotIn(b'fixture-owner',body)
        self.assertEqual(self.request('GET','/api/state')[0],200)
        self.assertEqual(self.request('POST','/api/logout',{})[0],200)
        self.assertEqual(self.request('GET','/api/state')[0],401)
    def test_host_and_origin_are_exact_not_prefix_matches(self):
        for host in ('evil.test',self.host+'.evil.test','localhost.evil.test'):
            self.assertEqual(self.request('GET','/',headers={'Host':host})[0],403)
        for origin in ('http://evil.test',self.origin+'.evil.test','null',''):
            self.assertEqual(self.request('POST','/api/login',{'token':'fixture-owner'},headers={'Origin':origin})[0],403)
        self.login();self.assertEqual(self.request('GET','/api/state',headers={'Origin':'http://evil.test'})[0],403)
    def test_invalid_login_and_caller_identity_refused(self):
        self.assertEqual(self.request('POST','/api/login',{'token':'bad'})[0],401)
        self.assertEqual(self.request('POST','/api/login',{'token':'fixture-owner','role':'owner'})[0],400)
        self.login('fixture-builder')
        self.assertEqual(self.command('grant',{})[0],403)
        self.assertEqual(self.command('create',self.message(sender='owner'))[0],400)
        self.assertEqual(self.command('pause',{'authority_id':'work'})[0],403)
    def test_http_retries_use_store_receipts(self):
        self.login();p=self.message();a=self.command('create',p);b=self.command('create',p)
        self.assertEqual(a[0],200);self.assertEqual(a[2],b[2]);self.assertEqual(len(self.store.list_tasks(self.owner)),1)
        self.assertEqual(self.command('create',self.message(title='changed'))[0],409)
    def test_json_size_type_and_duplicate_keys_refused(self):
        self.assertEqual(self.request('POST','/api/login',{'token':'x'*17000})[0],413)
        self.assertEqual(self.request('POST','/api/login',{'token':'fixture-owner'},headers={'Content-Type':'text/plain'})[0],415)
        c=http.client.HTTPConnection(*self.server.server_address,timeout=3)
        c.request('POST','/api/login',b'{"token":"fixture-owner","token":"bad"}',{'Host':self.host,'Origin':self.origin,'Content-Type':'application/json'})
        res=c.getresponse();self.assertEqual(res.status,400);res.read();c.close()
    def test_no_file_browsing_urls_or_token_in_state(self):
        for path in ('/../REPORT.md','/static/../../REPORT.md','/api/state?token=fixture-owner','/static/missing.js'):
            self.assertEqual(self.request('GET',path)[0],404)
        self.login();status,headers,data=self.request('GET','/api/state')
        self.assertNotIn(b'fixture-owner',data);self.assertNotIn(self.cookie.encode(),data)
        self.assertEqual(headers['Cache-Control'],'no-store')
    def test_static_security_headers_and_loopback_binding(self):
        self.assertEqual(self.server.server_address[0],'127.0.0.1')
        for path in ('/','/static/board.js','/static/board.css'):
            status,h,_=self.request('GET',path);self.assertEqual(status,200)
            self.assertIn("default-src 'none'",h['Content-Security-Policy']);self.assertEqual(h['X-Content-Type-Options'],'nosniff')
            self.assertEqual(h['Referrer-Policy'],'no-referrer')
    def test_session_expiry_and_fixture_presence_are_honest(self):
        self.login('fixture-builder');self.login()
        state=json.loads(self.request('GET','/api/state')[2]);people={p['id']:p for p in state['participants']}
        self.assertEqual(people['builder']['contact'],'Fixture browser active');self.assertEqual(people['reviewer']['contact'],'No fixture contact')
        self.clock.now+=61;state=json.loads(self.request('GET','/api/state')[2]);people={p['id']:p for p in state['participants']}
        self.assertEqual(people['builder']['contact'],'Fixture contact stale')
        self.clock.now+=901;self.assertEqual(self.request('GET','/api/state')[0],401)
    def test_authenticated_member_state_comes_from_single_store(self):
        task=self.ready();self.login();state=json.loads(self.request('GET','/api/state')[2])
        self.assertEqual(state['tasks'][0]['id'],task);self.assertEqual(state['tasks'][0]['status'],'acknowledged')
        self.command('note',{'authority_id':'work','body':'Quiet fixture lounge'})
        state=json.loads(self.request('GET','/api/state')[2]);self.assertEqual(len(state['tasks']),1)
        self.assertEqual(state['notes'][-1]['body'],'Quiet fixture lounge')

if __name__=='__main__':unittest.main()
