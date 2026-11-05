"""Fictional browser acceptance server; deliberately lose one finish reply."""
import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from common_room.activation import start_room
from common_room.board import Handler
from tests.test_activation import CONFIG
root=ROOT/'.checks'/'activation-ui-2'
if root.exists():raise ValueError('Use a fresh fixture path.')
room=start_room(root,CONFIG,['test-writer','test-reader'])
class LostReply(Handler):
    def reply(self,status,value,*args,**kwargs):
        if not getattr(self.server,'dropped',False) and status==200 and self.path=='/api/command' and isinstance(value,dict) and isinstance(value.get('task'),dict) and value['task']['status']=='ready_for_review':
            self.server.dropped=True;self.close_connection=True;self.connection.close();return
        return super().reply(status,value,*args,**kwargs)
room.server.RequestHandlerClass=LostReply
s=room.store;owner=s.people['keeper']
msg={'title':'Browser connection rehearsal','body':'Fictional connection check. Write the exact full report, separately from its summary. No project work.','recipient':'test-writer','authority_id':'test-one','kind':'handoff.record','acceptance':'Independent reviewer reads the saved report: THE SAVED REPORT HAS A BLUE DOOR. The summary must say only: Report ready.'}
t=s.execute(owner,'intake',{'source_namespace':'estate','external_reference':'browser-rehearsal','message':msg},'intake')['task']['id']
s.execute(owner,'deliver',{'task_id':t},'deliver')
print(json.dumps({'origin':room.server.origin,'task_id':t,'fixture_credentials':room.credentials}),flush=True)
try:
    while input('fixture> ').strip()!='stop':pass
finally:room.close()
