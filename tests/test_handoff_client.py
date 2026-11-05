"""Actual loopback handoff client and credential boundary tests."""
import json, threading, hashlib
from pathlib import Path
from common_room.board import make_server
from common_room.handoff_api import CredentialPolicy
from common_room.participant import Participant
from tests import test_handoffs
from tests.test_store import StoreFixture
from tests import test_board

class HandoffHTTPTests(StoreFixture):
    grant_handoff=test_handoffs.HandoffTests.grant_handoff
    intake=test_handoffs.HandoffTests.intake
    # Run this class directly so the separately captured store cases are not duplicated.
    def setUp(self):
        super().setUp()
        self.grant_handoff()
        self.tokens={'owner-key':self.owner,'builder-key':self.builder,'review-key':self.reviewer}
        self.policy=CredentialPolicy(self.tokens,self.clock.now+2000,self.clock)
        self.server=make_server(self.store,self.tokens,clock=self.clock,credential_policy=self.policy)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.host=self.server.host;self.origin=self.server.origin;self.cookie=None
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)
    request=test_board.BoardTests.request
    login=test_board.BoardTests.login
    def client(self,token='owner-key'):
        c=Participant(self.origin,token);c.login();return c
    def test_client_inbox_export_is_metadata_only(self):
        c=self.client();task=c.command('intake',self.intake(body='private-looking fictional content C:/Hidden/source.txt'),'intake')['task']['id']
        self.assertEqual(c.state()['tasks'][0]['id'],task)
        receipt=c.handoff('receipt',{'task_id':task})
        encoded=json.dumps(receipt)
        for forbidden in ['owner-key','private-looking','C:/Hidden','acceptance','body','scope']:
            self.assertNotIn(forbidden,encoded)
        self.assertEqual(receipt['task_id'],task)
        self.assertEqual(c.command('intake',self.intake(body='private-looking fictional content C:/Hidden/source.txt'),'new-request')['task']['id'],task)
    def test_import_finish_and_separate_review(self):
        owner=self.client();worker=self.client('builder-key');reviewer=self.client('review-key')
        task=owner.command('intake',self.intake(),'intake')['task']['id']
        owner.command('deliver',{'task_id':task},'deliver');worker.command('ack',{'task_id':task},'ack')
        lease=worker.command('claim',{'task_id':task,'seconds':300},'claim')['task']['lease']
        ref=self.ref();payload={'task_id':task,'fence':lease['fence'],**ref}
        imported=worker.handoff('import',payload)
        self.assertEqual(imported,worker.handoff('import',payload))
        self.assertNotEqual(imported['path'],ref['path'])
        worker.command('finish',{'task_id':task,'fence':lease['fence'],'body':'Checked local result','refs':[imported]},'finish')
        self.assertEqual(reviewer.command('review',{'task_id':task,'accepted':True,'note':'Independent fixture review'},'review')['task']['status'],'done')
    def test_key_expiry_and_revocation_invalidate_existing_sessions(self):
        c=self.client('builder-key');self.policy.revoke(self.builder.id)
        with self.assertRaisesRegex(ValueError,'unauthenticated'):c.state()
        owner=self.client();self.clock.now+=2000
        with self.assertRaisesRegex(ValueError,'unauthenticated'):owner.login()
        with self.assertRaisesRegex(ValueError,'unauthenticated'):owner.state()
    def test_owner_revoke_endpoint_and_wrong_actor_refused(self):
        worker=self.client('builder-key')
        with self.assertRaisesRegex(ValueError,'forbidden'):worker.handoff('revoke',{'actor_id':'reviewer'})
        owner=self.client();owner.handoff('revoke',{'actor_id':'builder'})
        with self.assertRaisesRegex(ValueError,'unauthenticated'):worker.state()
    def test_import_escape_and_unclaimed_owner_refused(self):
        c=self.client();task=c.command('intake',self.intake(),'intake')['task']['id']
        with self.assertRaises(ValueError):c.handoff('import',{'task_id':task,'fence':1,'path':'../outside','sha256':'0'*64})
        with self.assertRaises(ValueError):c.command('grant_handoff',{},'grant')
    def test_cli_rejects_non_loopback_and_credentials_in_url(self):
        for origin in ['http://localhost:1234','https://example.com','http://127.0.0.1:1234/?key=x','http://x@127.0.0.1:1234','http://127.0.0.1:1234/path']:
            with self.assertRaises(ValueError):Participant(origin,'key')

class HandoffLongPathTests(StoreFixture):
    def test_deep_supported_root_imports_with_compact_digest_name(self):
        from types import SimpleNamespace
        from common_room.store import Store
        from common_room.handoff_api import handoff_operation
        suffix='x'*max(1,180-len(str(self.home))-1)
        deep=self.home/suffix;deep.mkdir();art=deep/'artifacts';art.mkdir()
        store=Store(deep/'room.sqlite',self.people,{'fixture':art},clock=self.clock)
        old=self.store;self.store=store
        try:
            test_handoffs.HandoffTests.grant_handoff(self)
            msg=test_handoffs.HandoffTests.intake(self)
            task=self.call(self.owner,'intake',msg)['task']['id'];self.call(self.owner,'deliver',{'task_id':task});self.call(self.builder,'ack',{'task_id':task});lease=self.claim(task)
            data=b'Fictional deep-root result';(art/'result.txt').write_bytes(data);digest=hashlib.sha256(data).hexdigest()
            ref=handoff_operation(SimpleNamespace(store=store),self.builder,'import',{'task_id':task,'fence':lease['fence'],'path':'result.txt','sha256':digest})
            self.assertEqual((art/ref['path']).read_bytes(),data)
        finally:self.store=old
