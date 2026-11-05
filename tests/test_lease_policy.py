"""Prospective chat-lease policy, without rewriting historical event meaning."""
import copy, unittest
from common_room.protocol import RoomError
from common_room.store import fold
from tests.test_store import StoreFixture

class LeasePolicyTests(StoreFixture):
    def test_reclaim_expiry_has_no_strike_and_fences(self):
        task=self.ready();old=self.claim(task)
        for _ in range(3):
            self.clock.now+=301
            current=self.call(self.builder,'claim',{'task_id':task,'seconds':300})['task']
            self.assertEqual(current['failed_attempts'],0)
            self.assertEqual(current['lease_expiry']['policy'],'release-v1')
            self.assertEqual(current['lease_expiry']['holder'],'builder')
        self.error('lease',lambda:self.finish(task,old))
        self.assertGreater(current['fence'],old['fence'])

    def test_assign_expiry_and_failure_are_distinct(self):
        task=self.ready();lease=self.claim(task)
        self.call(self.builder,'fail',{'task_id':task,'fence':lease['fence'],'body':'actual failed work'})
        self.claim(task);self.clock.now+=301
        state=self.call(self.owner,'assign',{'task_id':task,'recipient':'second'})['task']
        self.assertEqual(state['failed_attempts'],1)
        self.assertEqual(state['failure_note'],'actual failed work')
        self.assertEqual(state['lease_expiry']['policy'],'release-v1')
        self.assertIsNone(state['lease'])

    def test_pause_revoke_expiry_release(self):
        for action in ('pause','revoke'):
            with self.subTest(action=action):
                aid='authority-'+action;self.grant(aid);task=self.ready(authority=aid);old=self.claim(task)
                self.clock.now+=301
                self.call(self.owner,action,{'authority_id':aid})
                state=self.store.task(self.owner,task)
                self.assertEqual(state['failed_attempts'],0)
                self.assertEqual(state['lease_expiry']['policy'],'release-v1')
                self.assertIsNone(state['lease']);self.assertGreater(state['fence'],old['fence'])

    def test_legacy_pause_replay_retains_strike(self):
        task=self.ready();self.claim(task);self.clock.now+=301
        self.call(self.owner,'pause',{'authority_id':'work'})
        events=copy.deepcopy(self.store.poll(self.owner)['events'])
        self.assertEqual(events[-1]['payload']['lease_expiry_policy'],'release-v1')
        events[-1]['payload'].pop('lease_expiry_policy')
        _,tasks=fold(events)
        self.assertEqual(tasks[task]['failed_attempts'],1)
        self.assertIn('Lease expired',tasks[task]['failure_note'])
        self.assertNotIn('lease_expiry',tasks[task])
        _,new_tasks=fold(self.store.poll(self.owner)['events'])
        self.assertEqual(new_tasks[task]['failed_attempts'],0)

    def test_unknown_policy_refuses(self):
        task=self.ready();self.claim(task);self.clock.now+=301
        self.call(self.owner,'pause',{'authority_id':'work'})
        events=copy.deepcopy(self.store.poll(self.owner)['events'])
        events[-1]['payload']['lease_expiry_policy']='future-unknown'
        with self.assertRaises(RoomError) as caught: fold(events)
        self.assertEqual(caught.exception.code,'corrupt')

    def test_two_explicit_failures_still_stop(self):
        task=self.ready()
        for count in (1,2):
            lease=self.claim(task)
            state=self.call(self.builder,'fail',{'task_id':task,'fence':lease['fence'],'body':'actual failure '+str(count)})['task']
            self.assertEqual(state['failed_attempts'],count)
        self.error('budget',lambda:self.claim(task))

if __name__=='__main__':unittest.main()
