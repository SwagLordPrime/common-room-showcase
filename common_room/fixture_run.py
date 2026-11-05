"""Run a fresh fictional workflow and measure empty/lounge idle behavior; then exit."""
from pathlib import Path
import hashlib,json,sys,time,uuid
from .adapter import FixtureWorker,FixtureReviewer
from .protocol import Actor
from .store import Store
ROOT=Path(__file__).resolve().parents[1]

def main():
    root=ROOT/'.runtime'/('adapter-'+uuid.uuid4().hex);root.mkdir(parents=True)
    art=root/'artifacts';art.mkdir()
    people={a.id:a for a in [Actor('keeper','owner'),Actor('scribe','worker'),Actor('reader','reviewer')]}
    store=Store(root/'room.sqlite',people,{'fixture':art});owner=people['keeper']
    def call(action,payload):return store.execute(owner,action,payload,'demo-'+uuid.uuid4().hex)
    call('grant',{'id':'fixture','members':list(people),'capabilities':['document.count'],'root_id':'fixture','scope':'Fictional word-count demonstration only'})
    worker=FixtureWorker(store,people['scribe']);observed={'process_starts':0,'network_attempts':0,'shell_calls':0}
    def audit(event,args):
        if event=='subprocess.Popen':observed['process_starts']+=1
        if event in {'socket.connect','socket.getaddrinfo'}:observed['network_attempts']+=1
        if event=='os.system':observed['shell_calls']+=1
    sys.addaudithook(audit)
    def idle(label):
        before=dict(observed);seq=store.poll(owner)['cursor'];start=time.perf_counter();cpu=time.process_time()
        for _ in range(100):assert worker.run_once()=={'status':'idle'}
        result={'phase':label,'ticks':100,'wall_seconds':time.perf_counter()-start,'cpu_seconds':time.process_time()-cpu,
            'observed':{k:observed[k]-before[k] for k in observed},'events_added':store.poll(owner)['cursor']-seq}
        assert result['events_added']==0 and not any(result['observed'].values())
        return result
    phases=[idle('empty_inbox')]
    call('note',{'authority_id':'fixture','body':'Quiet company without worker execution.'});phases.append(idle('lounge_only'))
    task=call('create',{'title':'Count a fictional note','body':'One quiet orchard','recipient':'scribe','authority_id':'fixture','kind':'document.count','acceptance':'Exactly three whitespace-separated words, checked by a separate fixture reviewer.'})['task']['id']
    call('deliver',{'task_id':task});worked=worker.run_once();reviewed=FixtureReviewer(store,people['reader']).run_once()
    assert worked['status']=='ready_for_review' and reviewed['status']=='done'
    state=store.task(owner,task);ref=state['result_refs'][0];raw=(art/ref['path']).read_bytes();assert hashlib.sha256(raw).hexdigest()==ref['sha256']
    print(json.dumps({'fixture_only':True,'runtime':str(root),'idle':phases,'worked':worked,'reviewed':reviewed,'reviewer':state['reviewer'],
        'artifact':json.loads(raw),'artifact_sha256':ref['sha256'],'observed':observed,'worker_metrics':worker.metrics,
        'model_calls_evidence':'No model client or invocation exists in worker/count child; idle audit observed zero process, network or shell starts.'},indent=2))
if __name__=='__main__':main()
