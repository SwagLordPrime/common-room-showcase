"""Bounded, opt-in fictional document.count worker and separately registered reviewer."""
from pathlib import Path
import hashlib,json,math,re,subprocess,sys,uuid
from .protocol import RoomError,canonical,decode_request,safe_artifact
ROOT=Path(__file__).resolve().parents[1]

def fixture_context(store,actor,role):
    store._actor(actor)
    if actor.role!=role:raise RoomError('forbidden','This fixture identity has the wrong role.')
    allowed=[(ROOT/name).resolve() for name in ('.checks','.runtime')]
    for path in [store.path,*map(Path,store.roots.values())]:
        if not any(Path(path).resolve().is_relative_to(base) for base in allowed):
            raise RoomError('config','Adapter accepts only this worktree fixture directories.')

def command(store,actor,action,payload):
    return store.execute(actor,action,payload,'fixture-'+uuid.uuid4().hex)

def view_result(task):return {'task_id':task['id'],'status':task['status']}

def active_authority(store,actor,task):
    _,authorities,_=store._snapshot(actor)
    authority=authorities[task['authority_id']];store._member(actor,authority,True)
    if 'document.count' not in authority['capabilities']:raise RoomError('forbidden','Fixed document capability required.')
    return authority

class FixtureWorker:
    def __init__(self,store,actor,timeout=30):
        fixture_context(store,actor,'worker')
        if type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0<timeout<=30:
            raise RoomError('config','Worker timeout must be positive and at most thirty seconds.')
        self.store=store;self.actor=actor;self.timeout=timeout
        self.metrics={'processes':0,'model_calls':0}

    def run_once(self):
        # No loop or sleep: at most one child per explicit tick; completed cards cannot hide later work.
        for task in self.store.list_tasks(self.actor):
            if task['recipient']!=self.actor.id or task['kind']!='document.count' or task['status'] not in {'delivered','acknowledged','claimed'}:continue
            try:
                active_authority(self.store,self.actor,task)
                if task['status']=='delivered':command(self.store,self.actor,'ack',{'task_id':task['id']})
                claim=command(self.store,self.actor,'claim',{'task_id':task['id'],'seconds':300})['task']
            except RoomError as exc:
                if exc.code in {'held','claimed','state','budget'}:continue
                raise
            if claim['status']!='claimed' or claim['lease'] is None:return view_result(claim)
            return self._work(claim)
        return {'status':'idle'}

    def _current(self,task):
        fresh=self.store.task(self.actor,task['id'])
        authority=active_authority(self.store,self.actor,fresh)
        self.store._lease(self.actor,fresh,task['lease']['fence'],self.store.clock())
        return fresh,authority

    def _work(self,task):
        fence=task['lease']['fence']
        try:
            fresh,authority=self._current(task)
            folder=safe_artifact(authority['artifact_root'],'fixture-count',require_file=False)
            folder.mkdir(exist_ok=True)
            relative='fixture-count/'+task['id']+'-f'+str(fence)+'.json'
            target=safe_artifact(authority['artifact_root'],relative,require_file=False)
            if target.exists():raise RoomError('artifact','Existing fixture output is never overwritten.')
            self.metrics['processes']+=1
            result=subprocess.run([sys.executable,'-I','-B',str(ROOT/'common_room/count_job.py')],
                input=canonical({'body':fresh['body']}).encode('utf-8'),capture_output=True,
                timeout=self.timeout,cwd=folder,shell=False)
            if result.returncode or len(result.stdout)>2048:raise RoomError('artifact','Count process failed or exceeded output budget.')
            data=decode_request(result.stdout)
            if (set(data)!={'words','characters','input_sha256'} or type(data['words']) is not int
                or not 0<=data['words']<=len(fresh['body']) or type(data['characters']) is not int
                or data['characters']!=len(fresh['body'])
                or data['input_sha256']!=hashlib.sha256(fresh['body'].encode('utf-8')).hexdigest()):
                raise RoomError('artifact','Invalid fixture process result.')
            # Revocation/pause during the child cancels publication; the store checks again at finish.
            self._current(task)
            target=safe_artifact(authority['artifact_root'],relative,require_file=False)
            data.update(kind='document.count',task_id=task['id'],fixture_only=True)
            raw=(canonical(data)+'\n').encode('utf-8')
            with target.open('xb') as stream:stream.write(raw)
            ref={'path':relative,'sha256':hashlib.sha256(raw).hexdigest()}
            done=command(self.store,self.actor,'finish',{'task_id':task['id'],'fence':fence,
                'body':'Fictional word count completed; separate review required.','refs':[ref]})['task']
            return view_result(done)
        except Exception as exc:
            fresh=self.store.task(self.actor,task['id'])
            # A committed response may have been lost. Never repeat work that is already submitted.
            if fresh['status'] in {'ready_for_review','done'}:return view_result(fresh)
            try:
                failed=command(self.store,self.actor,'fail',{'task_id':task['id'],'fence':fence,
                    'body':'Fixture count stopped: '+type(exc).__name__})['task']
                return view_result(failed)
            except RoomError as refusal:
                if refusal.code in {'lease','held','state','budget'}:return {'status':'stale','task_id':task['id']}
                raise

class FixtureReviewer:
    def __init__(self,store,actor):
        fixture_context(store,actor,'reviewer');self.store=store;self.actor=actor
    def run_once(self):
        for task in self.store.list_tasks(self.actor):
            if task['kind']!='document.count' or task['status']!='ready_for_review':continue
            try:authority=active_authority(self.store,self.actor,task)
            except RoomError as exc:
                if exc.code=='held':continue
                raise
            accepted=False
            try:
                if len(task['result_refs'])!=1:raise ValueError('One fixture result required')
                ref=task['result_refs'][0]
                expected_path='fixture-count/'+task['id']+'-f'+str(task['fence']-1)+'.json'
                if ref['path']!=expected_path:raise ValueError('Not a fixture result')
                path=safe_artifact(authority['artifact_root'],ref['path'],ref['sha256'])
                data=decode_request(path.read_bytes())
                expected={'kind':'document.count','task_id':task['id'],'fixture_only':True,
                    'words':len(re.findall(r'\S+',task['body'])),'characters':len(task['body']),
                    'input_sha256':hashlib.sha256(task['body'].encode('utf-8')).hexdigest()}
                accepted=(canonical(data)==canonical(expected))
            except (RoomError,OSError,ValueError):pass
            try:
                result=command(self.store,self.actor,'review',{'task_id':task['id'],'accepted':accepted,
                    'note':'Separate fixture reviewer: '+('count, input digest and artifact verified.' if accepted else 'artifact did not match the independently computed count.')})['task']
                return view_result(result)
            except RoomError as exc:
                if exc.code in {'held','state'}:continue
                raise
        return {'status':'idle'}
