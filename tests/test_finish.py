"""Continuation behavior witnesses: durable explicit workflow and visible history."""
import importlib.util, json, unittest
from unittest.mock import patch
from tests.helpers import case_dir
from tests.test_board import BoardTests

class RoomTests(unittest.TestCase):
    def room(self):
        self.assertIsNotNone(importlib.util.find_spec('common_room.room'), 'Persistent room launcher missing')
        from common_room.room import open_room
        return open_room
    def test_explicit_work_review_restart_and_quiet_notes(self):
        open_room=self.room(); root=case_dir('finish'); store,people=open_room(root)
        from common_room.adapter import FixtureWorker,FixtureReviewer
        owner=people['keeper']
        def call(action,payload,key):return store.execute(owner,action,payload,key)
        with patch('common_room.adapter.subprocess.run',side_effect=AssertionError('Unexpected child')):
            call('note',{'authority_id':'fixture','body':'Do not execute this text'},'note')
            self.assertEqual(FixtureWorker(store,people['scribe']).run_once(),{'status':'idle'})
            task=call('create',{'title':'A quiet task','body':'One quiet orchard','recipient':'scribe','authority_id':'fixture','kind':'document.count','acceptance':'Three words and separate review'},'create')['task']['id']
            call('deliver',{'task_id':task},'deliver')
        self.assertEqual(FixtureWorker(store,people['scribe']).run_once()['status'],'ready_for_review')
        reopened,actors=open_room(root)
        self.assertEqual(reopened.task(actors['keeper'],task)['status'],'ready_for_review')
        self.assertEqual(FixtureReviewer(reopened,actors['reader']).run_once()['status'],'done')
        self.assertEqual(reopened.task(actors['keeper'],task)['reviewer'],'reader')
        self.assertEqual(call('deliver',{'task_id':task},'deliver')['task']['status'],'delivered')
    def test_room_rejects_outside_runtime(self):
        open_room=self.room()
        with self.assertRaises(ValueError):open_room(case_dir('finish').parent.parent/'outside-fixture')

class BoardFinishTests(BoardTests):
    def test_visible_history_and_server_time(self):
        task=self.ready();lease=self.call(self.builder,'claim',{'task_id':task,'seconds':1})['task']['lease']
        self.call(self.builder,'wait_decision',{'task_id':task,'fence':lease['fence'],'question':'Choose the fictional wording'})
        self.call(self.owner,'resolve_decision',{'task_id':task,'body':'Use the existing text'})
        self.login();state=json.loads(self.request('GET','/api/state')[2])
        self.assertIn('server_time',state,'Server freshness timestamp missing')
        self.assertIn('history',state,'Durable visible history missing')
        history=[e for e in state['history'] if e['task_id']==task]
        self.assertTrue(any(e.get('detail')=='Choose the fictional wording' for e in history))
        self.assertTrue(any(e.get('detail')=='Use the existing text' for e in history))
        self.assertTrue(all('sha256' in e and 'seq' in e for e in history))
