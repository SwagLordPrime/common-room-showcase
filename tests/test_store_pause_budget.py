"""Pause must preserve expired failures while distinguishing an intentional live cancellation."""
import unittest
from tests.test_store import StoreFixture

class StorePauseBudgetTests(StoreFixture):
    def test_expired_pause_releases_without_failure_and_keeps_fencing(self):
        task=self.ready();self.claim(task)
        for _ in (1,2):
            self.clock.now+=301
            self.call(self.owner,'pause',{'authority_id':'work'})
            self.call(self.owner,'resume',{'authority_id':'work'})
            state=self.store.task(self.owner,task)
            self.assertEqual(state['failed_attempts'],0)
            self.assertEqual(state['status'],'acknowledged');self.assertIsNone(state['lease'])
            self.claim(task)
    def test_intentional_pause_of_current_work_is_not_a_failure(self):
        task=self.ready();old=self.claim(task)
        self.call(self.owner,'pause',{'authority_id':'work'})
        self.call(self.owner,'resume',{'authority_id':'work'})
        self.assertEqual(self.store.task(self.owner,task)['failed_attempts'],0)
        self.error('lease',lambda:self.finish(task,old))
        self.assertIsNotNone(self.claim(task))

if __name__=='__main__':unittest.main()
