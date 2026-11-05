"""Fresh-package activation acceptance; fictional actors and no provider dispatch."""
from pathlib import Path
import hashlib,json,sys,uuid
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from common_room.activation import start_room
from common_room.participant import Participant
def main():
    home=ROOT/'.checks'/('ac-'+uuid.uuid4().hex[:8]);steps=[]
    config={'version':1,'actors':[{'id':'package-worker','role':'worker','label':'Fictional worker','session':'selftest'},{'id':'package-reviewer','role':'reviewer','label':'Fictional reviewer','session':'separate-selftest'}],
        'authorities':[{'id':'package-check','members':['keeper','package-worker','package-reviewer'],'scope':'Only this fictional activation check'}]}
    room=start_room(home,config,['package-worker','package-reviewer'])
    def client(actor):
        c=room.credentials[actor];p=Participant(c['origin'],c['token']);p.login();return p
    try:
        o=client('keeper');w=client('package-worker');r=client('package-reviewer')
        assert room.server.controller is None
        msg={'title':'Fictional activation receipt','body':'Write a harmless browser-compatible report.','recipient':'package-worker','authority_id':'package-check','kind':'handoff.record','acceptance':'Read exact saved report separately from summary.'}
        intake={'source_namespace':'estate','external_reference':'selftest','message':msg}
        t=o.command('intake',intake,'intake')['task']['id']
        assert o.command('intake',intake,'intake-repeat')['task']['id']==t
        o.command('deliver',{'task_id':t},'deliver');w.command('ack',{'task_id':t},'ack')
        f=w.command('claim',{'task_id':t,'seconds':300},'claim')['task']['lease']['fence']
        report='Exact saved report: a fictional blue door.'
        ref=w.handoff('stage_text',{'task_id':t,'fence':f,'text':report})
        assert ref['sha256']==hashlib.sha256(report.encode()).hexdigest()
        finish={'task_id':t,'fence':f,'body':'Summary only','refs':[ref]}
        saved=w.command('finish',finish,'same-finish')
        assert w.command('finish',finish,'same-finish')==saved
        assert r.handoff('view_text',{'task_id':t,**ref})['text']==report
        assert r.command('review',{'task_id':t,'accepted':True,'note':'Checked exact report and digest'},'review')['task']['status']=='done'
        old_token=room.credentials['package-worker']['token'];origin=room.server.origin;instance=room.descriptor['instance_id']
        o.handoff('revoke',{'actor_id':'package-worker'})
        try:w.state();raise AssertionError('Revoked browser/client session survived')
        except ValueError:pass
        steps.extend(['separate roles','exact report view','same-request retry','distinct review','revoked session refused'])
    finally:room.close()
    room=start_room(home,selected=['package-worker','package-reviewer'])
    try:
        assert room.server.origin==origin and room.descriptor['instance_id']!=instance
        assert 'package-worker' not in room.credentials
        assert room.store.task(room.store.people['keeper'],t)['status']=='done'
        assert len(room.store.list_tasks(room.store.people['keeper']))==1
        try:Participant(origin,old_token).login();raise AssertionError('Old key accepted')
        except ValueError:pass
        steps.extend(['stable endpoint','new instance','revocation survives restart','Done survives restart','old key refused'])
        receipt=client('keeper').handoff('receipt',{'task_id':t})
        assert sum(x['kind']=='task_finish' for x in receipt['events'])==1
        assert 'token' not in json.dumps(receipt)
    finally:room.close()
    out={'status':'PASS','fixture_only':True,'provider_dispatch':False,'checks':steps,'receipt':receipt,'runtime':str(home)}
    (home/'acceptance.json').write_text(json.dumps(out,indent=2),encoding='utf-8')
    print(json.dumps({'status':'PASS','fixture_only':True,'evidence':str(home/'acceptance.json'),'checks':len(steps)}))
if __name__=='__main__':main()
