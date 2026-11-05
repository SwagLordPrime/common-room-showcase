"""Explicit, journaled text-only participant runs. No task text is executable."""
from pathlib import Path
import ctypes,hashlib,json,math,os,re,signal,subprocess,threading,time,uuid
from .protocol import RoomError,canonical,decode_request,safe_artifact
from .adapter import fixture_context,active_authority,view_result

def _job_process(argv,cwd,env):
    """Suspend before assigning a kill-on-close Windows job: no child escape race."""
    from ctypes import wintypes as w
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    class Basic(ctypes.Structure):
        _fields_=[('process_time',ctypes.c_int64),('job_time',ctypes.c_int64),('flags',w.DWORD),('minimum',ctypes.c_size_t),('maximum',ctypes.c_size_t),('active',w.DWORD),('affinity',ctypes.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
    class IO(ctypes.Structure):_fields_=[(name,ctypes.c_uint64) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
    class Limits(ctypes.Structure):_fields_=[('basic',Basic),('io',IO),('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
    k.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR];k.CreateJobObjectW.restype=w.HANDLE
    k.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD];k.SetInformationJobObject.restype=w.BOOL
    k.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE];k.AssignProcessToJobObject.restype=w.BOOL
    k.CloseHandle.argtypes=[w.HANDLE];k.CloseHandle.restype=w.BOOL
    job=k.CreateJobObjectW(None,None)
    if not job:raise ctypes.WinError(ctypes.get_last_error())
    proc=None
    try:
        limits=Limits();limits.basic.flags=0x2000 # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; breakaway disabled.
        if not k.SetInformationJobObject(job,9,ctypes.byref(limits),ctypes.sizeof(limits)):raise ctypes.WinError(ctypes.get_last_error())
        proc=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=cwd,env=env,shell=False,creationflags=0x4|0x08000000)
        if not k.AssignProcessToJobObject(job,int(proc._handle)):raise ctypes.WinError(ctypes.get_last_error())
        # Popen closes the primary thread handle. Resume the still-suspended process only after assignment.
        resume=ctypes.WinDLL('ntdll').NtResumeProcess;resume.argtypes=[w.HANDLE];resume.restype=ctypes.c_long
        if resume(int(proc._handle))!=0:raise OSError('Could not resume confined process')
        return proc,lambda:k.CloseHandle(job)
    except BaseException:
        if proc is not None:proc.kill();proc.wait()
        k.CloseHandle(job);raise

def bounded_process(argv,input_bytes,cwd,timeout=30,output_limit=131072,env=None):
    """Fixed argv supplied by trusted transport; bounded pipes and entire process lifetime."""
    if type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0<timeout<=30:raise RoomError('config','Deadline must be positive and at most thirty seconds.')
    if not argv or not Path(argv[0]).is_absolute():raise RoomError('config','Use an absolute trusted executable.')
    if os.name=='nt':proc,close_tree=_job_process(argv,cwd,env)
    else:
        proc=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=cwd,env=env,shell=False,start_new_session=True)
        def close_tree():
            try:os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError:pass
    overflow=threading.Event();buffers=[bytearray(),bytearray()]
    def read(stream,target):
        try:
            while True:
                data=stream.read(4096)
                if not data:break
                if len(target)+len(data)>output_limit:overflow.set();break
                target.extend(data)
        finally:stream.close()
    def feed():
        try:proc.stdin.write(input_bytes);proc.stdin.flush()
        except (BrokenPipeError,OSError):pass
        finally:proc.stdin.close()
    readers=[threading.Thread(target=read,args=(stream,target),daemon=True) for stream,target in zip((proc.stdout,proc.stderr),buffers)]
    writer=threading.Thread(target=feed,daemon=True)
    for thread in [*readers,writer]:thread.start()
    deadline=time.monotonic()+timeout
    try:
        while proc.poll() is None:
            if overflow.is_set():raise RoomError('transport','Transport exceeded its output budget.')
            if time.monotonic()>=deadline:raise TimeoutError('Participant deadline exceeded.')
            time.sleep(.01)
    finally:
        close_tree();proc.wait(timeout=5)
        for thread in [*readers,writer]:thread.join(2)
    if overflow.is_set():raise RoomError('transport','Transport exceeded its output budget.')
    if any(t.is_alive() for t in [*readers,writer]):raise RoomError('transport','Transport pipes did not close.')
    return {'returncode':proc.returncode,'stdout':bytes(buffers[0]),'stderr':bytes(buffers[1])}

def counts(body):return {'words':len(re.findall(r'\S+',body)),'characters':len(body)}
def save_new(path,data):
    temporary=path.with_name('.pending-'+uuid.uuid4().hex)
    try:
        with temporary.open('x',encoding='utf-8',newline='\n') as stream:
            stream.write(canonical(data)+'\n');stream.flush();os.fsync(stream.fileno())
        os.link(temporary,path) # Atomic publication, refusing an existing receipt.
    finally:
        temporary.unlink(missing_ok=True)

class Dispatcher:
    def __init__(self,store,worker,reviewer,transport,journal):
        fixture_context(store,worker,'worker');fixture_context(store,reviewer,'reviewer')
        if worker.id==reviewer.id:raise RoomError('forbidden','A separate reviewer identity is required.')
        journal=Path(journal).absolute()
        if not journal.resolve().is_relative_to(store.path.parent.resolve()):raise RoomError('config','Transport journal must be inside this room.')
        for part in [journal,*journal.parents]:
            if part==store.path.parent:break
            if part.is_symlink() or (hasattr(part,'is_junction') and part.is_junction()):raise RoomError('config','Linked transport directories are refused.')
        journal.mkdir(parents=True,exist_ok=True)
        self.store=store;self.worker=worker;self.reviewer=reviewer;self.transport=transport;self.journal=journal
    def _command(self,actor,action,payload):return self.store.execute(actor,action,payload,'connection-'+uuid.uuid4().hex)
    def _task(self,owner,task_id,role):
        self.store._actor(owner)
        if owner.role!='owner' or role not in ('worker','reviewer'):raise RoomError('forbidden','Only the owner may start a registered participant.')
        task=self.store.task(owner,task_id)
        if task['owner']!=owner.id:raise RoomError('forbidden','This task belongs to another owner.')
        return task
    def _current(self,task,role):
        actor=self.worker if role=='worker' else self.reviewer
        fresh=self.store.task(actor,task['id']);authority=active_authority(self.store,actor,fresh)
        if role=='worker':self.store._lease(actor,fresh,task['lease']['fence'],self.store.clock())
        elif fresh['status']!='ready_for_review' or fresh['fence']!=task['fence']:raise RoomError('state','Review state changed.')
        return fresh,authority
    def _incomplete(self,task_id,role):
        return any(not (p/'outcome.json').is_file() for p in self.journal.glob(role+'-'+task_id+'-*') if p.is_dir())
    def run_task(self,owner,task_id,role):
        task=self._task(owner,task_id,role)
        if task['recipient']!=self.worker.id or task['kind']!='document.count':return {'status':'idle','task_id':task_id}
        expected={'delivered','acknowledged','claimed'} if role=='worker' else {'ready_for_review'}
        if task['status'] not in expected:return {'status':'idle','task_id':task_id}
        actor=self.worker if role=='worker' else self.reviewer
        try:active_authority(self.store,actor,task)
        except RoomError as exc:
            if exc.code in {'held','revoked'}:return {'status':'held','task_id':task_id}
            raise
        if self._incomplete(task_id,role):return {'status':'interrupted','task_id':task_id,'message':'A prior invocation has no final receipt. It will not be repeated automatically.'}
        if role=='worker':
            try:
                if task['status']=='delivered':self._command(actor,'ack',{'task_id':task_id})
                task=self._command(actor,'claim',{'task_id':task_id,'seconds':300})['task']
            except RoomError as exc:
                if exc.code in {'held','claimed','state','budget'}:return {'status':'idle','task_id':task_id}
                raise
            if task['status']!='claimed' or task['lease'] is None:return view_result(task)
        slot=self.journal/(role+'-'+task_id+'-'+str(task['fence']))
        try:slot.mkdir()
        except FileExistsError:return {'status':'busy','task_id':task_id}
        invocation=uuid.uuid4().hex
        save_new(slot/'start.json',{'task_id':task_id,'role':role,'actor':actor.id,'fence':task['fence'],'invocation_id':invocation,'started':self.store.clock(),'fixture_only':self.transport.fixture_only})
        try:
            fresh,authority=self._current(task,role);artifact=None
            if role=='reviewer':
                if len(fresh['result_refs'])!=1:raise RoomError('artifact','One result is required.')
                ref=fresh['result_refs'][0]
                expected='connected-count/'+task_id+'-f'+str(task['fence']-1)+'.json'
                if ref['path']!=expected:raise RoomError('artifact','Result belongs to another transport.')
                path=safe_artifact(authority['artifact_root'],ref['path'],ref['sha256'])
                artifact=decode_request(path.read_bytes())
                required={'kind','task_id','fixture_only','words','characters','input_sha256','invocation_id','provider_run_id'}
                if set(artifact)!=required or artifact['kind']!='document.count' or artifact['task_id']!=task_id or artifact['fixture_only'] is not self.transport.fixture_only or any(type(artifact[k]) is not int or artifact[k]!=v for k,v in counts(task['body']).items()) or artifact['input_sha256']!=hashlib.sha256(task['body'].encode()).hexdigest():raise RoomError('artifact','Artifact failed independent local checks.')
            response=self.transport.invoke(role,fresh['body'],artifact,slot,invocation)
            if type(response) is not dict or set(response)!={'output','provider_run_id'} or not isinstance(response['provider_run_id'],str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',response['provider_run_id']):raise RoomError('transport','Invalid transport receipt.')
            data=response['output'];fresh,authority=self._current(task,role)
            if role=='worker':
                if type(data) is not dict or set(data)!={'words','characters'} or any(type(data[k]) is not int or data[k]!=v for k,v in counts(fresh['body']).items()):raise RoomError('artifact','Count result is invalid.')
                folder=safe_artifact(authority['artifact_root'],'connected-count',require_file=False);folder.mkdir(exist_ok=True)
                relative='connected-count/'+task_id+'-f'+str(task['lease']['fence'])+'.json'
                target=safe_artifact(authority['artifact_root'],relative,require_file=False)
                result=dict(data,kind='document.count',task_id=task_id,fixture_only=self.transport.fixture_only,input_sha256=hashlib.sha256(fresh['body'].encode()).hexdigest(),invocation_id=invocation,provider_run_id=response['provider_run_id'])
                raw=(canonical(result)+'\n').encode()
                with target.open('xb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
                done=self._command(actor,'finish',{'task_id':task_id,'fence':task['lease']['fence'],'body':('Offline transport test' if self.transport.fixture_only else 'Codex worker')+' completed; separate review required. Run '+invocation,'refs':[{'path':relative,'sha256':hashlib.sha256(raw).hexdigest()}]})['task']
            else:
                if type(data) is not dict or set(data)!={'accepted','note'} or type(data['accepted']) is not bool or type(data['note']) is not str or not 1<=len(data['note'])<=1000:raise RoomError('artifact','Review result is invalid.')
                if response['provider_run_id']==artifact['provider_run_id']:raise RoomError('forbidden','Review must use a separate provider run.')
                done=self._command(actor,'review',{'task_id':task_id,'accepted':data['accepted'],'note':('Offline transport test' if self.transport.fixture_only else 'Codex reviewer')+' run '+invocation+': '+data['note']})['task']
            outcome=dict(view_result(done),invocation_id=invocation,provider_run_id=response['provider_run_id'],finished=self.store.clock())
        except Exception as exc:
            fresh=self.store.task(actor,task_id)
            if fresh['status'] in ({'ready_for_review','done'} if role=='worker' else {'done','waiting_decision'}):outcome=view_result(fresh)
            else:
                try:
                    if role=='worker':done=self._command(actor,'fail',{'task_id':task_id,'fence':task['lease']['fence'],'body':'Participant stopped: '+type(exc).__name__+'. See the transport receipt.'})['task']
                    else:done=self._command(actor,'review',{'task_id':task_id,'accepted':False,'note':'Separate review stopped: '+type(exc).__name__+'. Owner decision required.'})['task']
                    outcome=view_result(done)
                except RoomError:outcome={'status':'stale','task_id':task_id}
            outcome.update(error=type(exc).__name__,invocation_id=invocation,finished=self.store.clock())
        save_new(slot/'outcome.json',outcome)
        return outcome

class RunController:
    """One explicit browser request, one run. Durable keys survive lost HTTP replies."""
    def __init__(self,dispatcher,enabled=True,blocked_reason=None):
        self.enabled=enabled;self.blocked_reason=blocked_reason
        self.dispatcher=dispatcher;self.lock=threading.Lock();self.thread=None;self.closed=False
        self.requests=dispatcher.journal/'requests';self.requests.mkdir(exist_ok=True)
    def submit(self,owner,payload):
        if type(payload) is not dict or set(payload)!={'task_id','role','request_id'}:raise RoomError('invalid','Run fields must match.')
        key=payload['request_id']
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',key):raise RoomError('invalid','Invalid run request key.')
        self.dispatcher._task(owner,payload['task_id'],payload['role'])
        fingerprint=hashlib.sha256(canonical({'owner':owner.id,**payload}).encode()).hexdigest()
        slot=self.requests/hashlib.sha256((owner.id+'\n'+key).encode()).hexdigest()
        with self.lock:
            if slot.exists():
                saved=decode_request((slot/'request.json').read_bytes())
                if saved['fingerprint']!=fingerprint:raise RoomError('idempotency','Run request key already belongs to another action.')
                return {'request_id':key,'status':'recorded','message':'This recorded request will not launch again.'}
            if not self.enabled:raise RoomError('held',self.blocked_reason or 'Connection check required before provider use.')
            if self.closed:raise RoomError('held','The connection is stopping.')
            if self.thread is not None and self.thread.is_alive():raise RoomError('held','A participant is already running. Wait for its receipt.')
            slot.mkdir()
            save_new(slot/'request.json',{'fingerprint':fingerprint,'owner':owner.id,**payload})
            self.thread=threading.Thread(target=self._run,args=(owner,payload,slot),daemon=True);self.thread.start()
        return {'request_id':key,'status':'started'}
    def _run(self,owner,payload,slot):
        try:outcome=self.dispatcher.run_task(owner,payload['task_id'],payload['role'])
        except Exception as exc:outcome={'task_id':payload['task_id'],'status':'stopped','error':type(exc).__name__}
        save_new(slot/'result.json',outcome)
    def state(self,actor,allowed_tasks):
        records=[]
        for path in sorted(self.requests.iterdir(),key=lambda p:p.stat().st_mtime,reverse=True):
            if not path.is_dir() or not (path/'request.json').is_file():continue
            request=decode_request((path/'request.json').read_bytes())
            if request['task_id'] not in allowed_tasks:continue
            result=decode_request((path/'result.json').read_bytes()) if (path/'result.json').is_file() else {'status':'running' if self.thread and self.thread.is_alive() else 'interrupted','message':'No final receipt; never replayed automatically.'}
            records.append({'task_id':request['task_id'],'role':request['role'],**result})
            if len(records)>=20:break
        return {'mode':'offline-test' if self.dispatcher.transport.fixture_only else 'codex','worker':self.dispatcher.worker.id,'reviewer':self.dispatcher.reviewer.id,'busy':bool(self.thread and self.thread.is_alive()),'recent':records,'can_run':actor.role=='owner' and not self.closed and self.enabled,'blocked_reason':self.blocked_reason if not self.enabled else None}
    def close(self):
        with self.lock:self.closed=True;thread=self.thread
        if thread:thread.join(36)
