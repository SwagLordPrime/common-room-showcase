"""Activation behavior, using fictional participants in package checks only."""
import copy, hashlib, json, socket, unittest
from tests.helpers import case_dir
from common_room.protocol import RoomError

CONFIG = {'version':1, 'actors':[
    {'id':'test-writer','role':'worker','label':'Fictional writer','session':'fixture-writer'},
    {'id':'test-reader','role':'reviewer','label':'Fictional reviewer','session':'fixture-reviewer'},
    {'id':'test-other','role':'worker','label':'Other scope','session':'fixture-other'}],
    'authorities':[
    {'id':'test-one','members':['keeper','test-writer','test-reader'],'scope':'Fictional first scope'},
    {'id':'test-two','members':['keeper','test-other'],'scope':'Fictional second scope'}]}

class ActivationIdentityTests(unittest.TestCase):
    def start(self, root=None, config=None, selected=()):
        from common_room.activation import start_room
        r=start_room(root or case_dir('activation'),enrollment=config,selected=selected)
        self.addCleanup(r.close);return r
    def test_owner_default_and_explicit_scoped_selection(self):
        r=self.start(config=CONFIG)
        self.assertEqual(set(r.credentials),{'keeper'})
        self.assertEqual(r.store.list_tasks(r.store.people['test-writer']),[])
        authorities=r.store._snapshot(r.store.people['keeper'])[1]
        self.assertNotIn('test-writer',authorities['handoffs']['members'])
        self.assertNotEqual(authorities['test-one']['artifact_root'],authorities['test-two']['artifact_root'])
        self.assertNotIn('token',r.descriptor)
    def test_restart_stable_port_rotates_keys_and_instance(self):
        root=case_dir('activation')
        r=self.start(root,CONFIG,['test-writer']);old=r.credentials['test-writer'];origin=r.server.origin;instance=r.descriptor['instance_id']
        r.close()
        new=self.start(root,selected=['test-writer'])
        self.assertEqual(new.server.origin,origin)
        self.assertNotEqual(new.credentials['test-writer']['token'],old['token'])
        self.assertNotEqual(new.descriptor['instance_id'],instance)
        self.assertFalse(new.server.credential_policy.valid(old['token'],new.server.clock()))
        self.assertLessEqual(new.credentials['test-writer']['expires_at']-new.descriptor['started_at'],28800)
    def test_revocation_persists_and_rotation_invalidates_session_key(self):
        root=case_dir('activation');r=self.start(root,CONFIG,['test-writer'])
        old=r.credentials['test-writer']['token'];r.rotate('test-writer')
        self.assertFalse(r.server.credential_policy.valid(old,r.server.clock()))
        new=r.credentials['test-writer']['token'];r.server.credential_policy.revoke('test-writer')
        self.assertFalse(r.server.credential_policy.valid(new,r.server.clock()))
        r.close();again=self.start(root,selected=['test-writer'])
        self.assertNotIn('test-writer',again.credentials)
    def test_config_identity_scope_and_roots_cannot_be_rewritten(self):
        root=case_dir('activation');r=self.start(root,CONFIG);r.close()
        for mutate in [
            lambda c:c['actors'][0].update(role='reviewer'),
            lambda c:c['authorities'][0].update(scope='Changed'),
            lambda c:c['authorities'][0].update(root='C:/elsewhere'),
            lambda c:c['actors'].append({'id':'keeper','role':'owner','label':'Fake','session':'fake'})]:
            bad=copy.deepcopy(CONFIG);mutate(bad)
            with self.assertRaises((ValueError,RoomError)):self.start(root,bad)
    def test_conflicting_port_does_not_rotate_descriptor(self):
        root=case_dir('activation');r=self.start(root,CONFIG);port=r.server.server_address[1];r.close()
        before=(root/'connection.json').read_bytes()
        with socket.socket() as held:
            held.bind(('127.0.0.1',port));held.listen()
            with self.assertRaises(OSError):self.start(root)
        self.assertEqual((root/'connection.json').read_bytes(),before)
    def test_known_foreign_artifact_refused(self):
        from common_room.handoff_api import handoff_operation
        r=self.start(config=CONFIG);owner=r.store.people['keeper'];worker=r.store.people['test-writer'];s=r.store
        msg={'title':'Fixture','body':'Only first scope','recipient':worker.id,'authority_id':'test-one','kind':'handoff.record','acceptance':'Inspect report'}
        t=s.execute(owner,'intake',{'source_namespace':'estate','external_reference':'fixture','message':msg},'intake')['task']['id']
        s.execute(owner,'deliver',{'task_id':t},'deliver');s.execute(worker,'ack',{'task_id':t},'ack')
        lease=s.execute(worker,'claim',{'task_id':t,'seconds':300},'claim')['task']['lease']
        other=r.store._snapshot(owner)[1]['test-two']['artifact_root']
        from pathlib import Path
        data=b'Other scope only';(Path(other)/'foreign.txt').write_bytes(data)
        with self.assertRaises(RoomError):
            handoff_operation(r.server,worker,'import',{'task_id':t,'fence':lease['fence'],'path':'foreign.txt','sha256':hashlib.sha256(data).hexdigest()})
    def test_concurrent_launcher_and_unknown_selection_refused(self):
        root=case_dir('activation');r=self.start(root,CONFIG)
        with self.assertRaises((OSError,ValueError,RoomError)):self.start(root,selected=['test-writer'])
        with self.assertRaises((ValueError,RoomError)):self.start(config=CONFIG,selected=['unknown'])

    def test_parent_paths_refused_without_reading_or_writing_sibling(self):
        from common_room.activation import safe_path,read_json,write_json
        root=case_dir('activation');sibling=root.parent/(root.name+'-sentinel.json')
        sibling.write_text('{"outside":true}')
        relative='../'+sibling.name
        for fn in [lambda:safe_path(root,root/relative),lambda:read_json(root,relative,{}),lambda:write_json(root,relative,{'changed':True})]:
            with self.assertRaises(ValueError):fn()
        self.assertEqual(sibling.read_text(),'{"outside":true}')
