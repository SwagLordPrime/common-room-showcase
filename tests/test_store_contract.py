"""Use the already accepted protocol's kind through the actual store boundary."""
import json, unittest
from common_room.protocol import Actor, RoomError
from common_room.store import Store
from tests.helpers import ROOT, case_dir

class StoreContractTests(unittest.TestCase):
    def setUp(self):
        self.home=case_dir('store-contract');self.root=self.home/'artifacts';self.root.mkdir()
        self.owner=Actor('owner','owner');self.worker=Actor('worker','worker')
        self.people={a.id:a for a in (self.owner,self.worker)}
        self.store=Store(self.home/'room.sqlite',self.people,{'fixture':self.root},clock=lambda:1000)
        self.kind=json.loads((ROOT/'protocol-schema.json').read_text())['properties']['kind']['const']
    def grant(self, kind):
        return self.store.execute(self.owner,'grant',{'id':'grant','members':['owner','worker'],'capabilities':[kind],'root_id':'fixture','scope':'Count a fictional note'},'grant')
    def test_accepted_schema_kind_round_trips(self):
        self.assertEqual(self.kind,'document.count');self.grant(self.kind)
        result=self.store.execute(self.owner,'create',{'title':'Count a note','body':'One quiet orchard','recipient':'worker','authority_id':'grant','kind':self.kind,'acceptance':'A reproducible count'},'task')
        self.assertEqual(result['task']['kind'],self.kind)
    def test_undeclared_document_alias_is_refused(self):
        with self.assertRaises(RoomError) as error:self.grant('document')
        self.assertEqual(error.exception.code,'forbidden')

if __name__=='__main__':unittest.main()
