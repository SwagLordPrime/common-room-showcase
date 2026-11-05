"""Reproducible local CLI handoff acceptance; no provider calls or real credentials."""
from pathlib import Path
import hashlib,json,secrets,subprocess,sys,threading,time,uuid
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from common_room.handoff_room import open_handoff_room
from common_room.handoff_api import CredentialPolicy
from common_room.board import make_server

def main():
    fixture=ROOT/'.checks'/('handoff-selftest-'+uuid.uuid4().hex[:8])
    store,people=open_handoff_room(fixture,True)
    tokens={secrets.token_urlsafe(32):a for a in people.values()};expiry=time.time()+600
    server=make_server(store,tokens,credential_policy=CredentialPolicy(tokens,expiry,time.time))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();steps=[]
    try:
        for token,actor in tokens.items():
            (fixture/(actor.id+'-credential.json')).write_text(json.dumps({'origin':server.origin,'token':token,'actor_id':actor.id,'expires_at':expiry}),encoding='utf-8')
        def call(actor,op,payload=None,action=None,key=None,task_id=None):
            cmd=[sys.executable,'-B','-m','common_room.participant','--credential',str(fixture/(actor+'-credential.json')),op]
            if action:cmd+=['--action',action,'--request-id',key]
            if task_id:cmd+=['--task-id',task_id]
            proc=subprocess.run(cmd,cwd=ROOT,input=json.dumps(payload).encode() if payload is not None else None,capture_output=True,timeout=10)
            if proc.returncode:raise AssertionError(proc.stderr.decode())
            result=json.loads(proc.stdout);steps.append({'actor':actor,'operation':op,'action':action,'result':result});return result
        message={'authority_id':'handoffs','recipient':'fixture-worker','kind':'handoff.record','title':'Fictional orchard note','body':'Prepare a fictional note.','acceptance':'A saved note with separate fixture review.'}
        intake={'source_namespace':'estate','external_reference':'fictional-package-selftest','message':message}
        task=call('keeper','command',intake,'intake','intake-one')['task']['id']
        assert call('keeper','command',intake,'intake','intake-repeat')['task']['id']==task
        call('keeper','command',{'task_id':task},'deliver','deliver')
        call('fixture-worker','inbox')
        call('fixture-worker','command',{'task_id':task},'ack','ack')
        lease=call('fixture-worker','command',{'task_id':task,'seconds':300},'claim','claim')['task']['lease']
        renew={'task_id':task,'fence':lease['fence'],'seconds':300}
        first=call('fixture-worker','command',renew,'renew','renew');assert call('fixture-worker','command',renew,'renew','renew')==first
        data=b'Fictional orchard note.\n';(fixture/'artifacts/note.txt').write_bytes(data)
        ref=call('fixture-worker','import',{'task_id':task,'fence':lease['fence'],'path':'note.txt','sha256':hashlib.sha256(data).hexdigest()})
        call('fixture-worker','command',{'task_id':task,'fence':lease['fence'],'body':'Saved fictional note','refs':[ref]},'finish','finish')
        done=call('fixture-reviewer','command',{'task_id':task,'accepted':True,'note':'Separate fixture review matched the note and hash.'},'review','review')['task']
        assert done['status']=='done' and done['builder']!=done['reviewer']
        receipt=call('keeper','receipt',task_id=task)
        assert not any(k in json.dumps(receipt) for k in ['Prepare a fictional','Separate fixture review','token'])
    finally:server.shutdown();server.server_close();thread.join(3)
    reopened,_=open_handoff_room(fixture,True)
    assert reopened.task(people['keeper'],task)['status']=='done'
    assert len(reopened.list_tasks(people['keeper']))==1
    record={'status':'PASS','fixture_only':True,'provider_calls':0,'cli_processes':len(steps),'task_id':task,'reopened_done':True,'server_stopped':True,'steps':steps}
    output=fixture/'result.json';output.write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':'PASS','evidence':str(output),'cli_processes':len(steps),'provider_calls':0}))
if __name__=='__main__':main()
