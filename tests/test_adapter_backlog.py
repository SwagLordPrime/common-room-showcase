from unittest import mock
from common_room.adapter import FixtureWorker,FixtureReviewer
from tests.test_store import StoreFixture

class BacklogTests(StoreFixture):
    def test_old_cards_do_not_hide_runnable_work_or_review(self):
        task=self.ready();original=self.store.list_tasks
        def backlog(actor):
            old=[dict(original(actor)[0],id='old'+str(i),status='done') for i in range(100)]
            return old+original(actor)
        with mock.patch.object(self.store,'list_tasks',side_effect=backlog):
            self.assertEqual(FixtureWorker(self.store,self.builder).run_once()['status'],'ready_for_review')
            self.assertEqual(FixtureReviewer(self.store,self.reviewer).run_once()['status'],'done')
