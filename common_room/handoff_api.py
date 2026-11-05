"""Scoped local handoff operations; no provider, arbitrary file or command execution."""
import hashlib, math, threading, os
from contextlib import closing
from .store import read_events, text
from .protocol import RoomError, canonical, safe_artifact

class CredentialPolicy:
    """Per-launch bounded credentials. Restart rotates keys; revocation kills sessions."""
    def __init__(self,tokens,expires_at,clock):
        now=clock()
        if type(expires_at) not in (int,float) or not math.isfinite(expires_at) or not now<expires_at<=now+28800:
            raise RoomError('config','Credential lifetime must be positive and at most eight hours.')
        self.records={hashlib.sha256(k.encode()).hexdigest():{'actor':a.id,'expires':expires_at,'revoked':False} for k,a in tokens.items()}
        self.lock=threading.RLock();self.clock=clock;self.observed=now
    def valid(self,token,now):
        with self.lock:
            self.observed=max(self.observed,now)
            row=self.records.get(hashlib.sha256(token.encode()).hexdigest())
            return bool(row and not row['revoked'] and self.observed<row['expires'])
    def session_window(self,token,now):
        """Trusted inactivity deadline; polling is connection traffic, not presence."""
        with self.lock:
            self.observed=max(self.observed,now)
            row=self.records.get(hashlib.sha256(token.encode()).hexdigest())
            if not row or row['revoked'] or self.observed>=row['expires']:return None
            return self.observed,min(self.observed+900,row['expires'])
    def revoke(self,actor_id):
        with self.lock:
            for row in self.records.values():
                if row['actor']==actor_id:row['revoked']=True

def trusted_now(store):
    with closing(store._connect()) as conn:
        conn.execute('BEGIN IMMEDIATE')
        now=store._now(conn,read_events(conn));conn.commit()
        return now

def handoff_operation(server,actor,operation,payload):
    if type(payload) is not dict:raise RoomError('invalid','An object payload is required.')
    store=server.store
    if operation=='revoke':
        if set(payload)!={'actor_id'} or type(payload['actor_id']) is not str:raise RoomError('invalid','Name one registered participant.')
        store._owner(actor)
        _,authorities,_=store._snapshot(actor)
        target=payload['actor_id']
        if not any(a['owner']==actor.id and a['capabilities']==['handoff.record'] and target in a['members'] for a in authorities.values()):
            raise RoomError('forbidden','Participant is outside your handoff authority.')
        server.credential_policy.revoke(target)
        return {'revoked':target}
    expected={'receipt':{'task_id'},'import':{'task_id','fence','path','sha256'},
              'stage_text':{'task_id','fence','text'},'view_text':{'task_id','path','sha256'},'task_view':{'task_id'}}.get(operation)
    if expected is None or set(payload)!=expected:raise RoomError('invalid','Unsupported handoff operation or fields.')
    task=store.task(actor,payload['task_id'])
    _,authorities,_=store._snapshot(actor);authority=authorities[task['authority_id']]
    store._member(actor,authority)
    if operation=='task_view':
        events,_,_=store._snapshot(actor)
        return {'task':task,'events':[event for event in events if event['task_id']==task['id']]}
    if task['kind']!='handoff.record':raise RoomError('forbidden','Only handoff records use this operation.')
    if operation=='receipt':
        events,_,_=store._snapshot(actor)
        rows=[e for e in events if e['task_id']==task['id']]
        return {'version':1,'task_id':task['id'],'authority_id':task['authority_id'],
                'parent_id':task['parent_id'],'chain_depth':task['chain_depth'],
                'source_namespace':task['intake']['source_namespace'],'external_reference':task['intake']['external_reference'],
                'sender':task['sender'],'owner':task['owner'],'recipient':task['recipient'],'builder':task['builder'],
                'reviewer':task['reviewer'],'status':task['status'],'fence':task['fence'],'lease':task['lease'],
                'artifacts':task['result_refs'],'events':[{'seq':e['seq'],'actor':e['actor'],'kind':e['kind'],
                'timestamp':e['timestamp'],'sha256':hashlib.sha256(canonical(e).encode()).hexdigest()} for e in rows]}
    if operation=='view_text':
        ref={'path':payload['path'],'sha256':payload['sha256']}
        if ref not in task['result_refs']:raise RoomError('forbidden',"Only this task's saved result references can be viewed.")
        source=safe_artifact(authority['artifact_root'],ref['path'],ref['sha256'])
        data=source.read_bytes()
        if len(data)>12000 or hashlib.sha256(data).hexdigest()!=ref['sha256']:raise RoomError('artifact','Saved report is changed or exceeds the text-view budget.')
        try:report=data.decode('utf-8')
        except UnicodeError:raise RoomError('artifact','Report is not UTF-8 text.')
        text(report,12000)
        return dict(ref,text=report)
    store._member(actor,authority,True)
    if type(payload['fence']) is not int or payload['fence']<1:raise RoomError('invalid','A positive fence is required.')
    store._lease(actor,task,payload['fence'],trusted_now(store))
    if operation=='stage_text':
        data=text(payload['text'],12000).encode('utf-8')
        if len(data)>12000:raise RoomError('too_large','Report exceeds 12000 UTF-8 bytes.')
        digest=hashlib.sha256(data).hexdigest()
    else:
        source=safe_artifact(authority['artifact_root'],payload['path'],payload['sha256'])
        data=source.read_bytes();digest=payload['sha256']
        if len(data)>1048576 or hashlib.sha256(data).hexdigest()!=digest:raise RoomError('artifact','Source changed during import.')
    # Compact content-addressed name; full digest verification refuses any prefix collision.
    relative='results/'+digest[:48]+'.txt'
    target=safe_artifact(authority['artifact_root'],relative,require_file=False)
    if os.name=='nt' and len(str(target))>=260:raise RoomError('artifact','Artifact root is too deep for this Windows runtime; use a shorter empty package path.')
    target.parent.mkdir(parents=True,exist_ok=True)
    target=safe_artifact(authority['artifact_root'],relative,require_file=False)
    try:
        with target.open('xb') as stream:stream.write(data)
    except FileExistsError:
        safe_artifact(authority['artifact_root'],relative,digest)
    current=store.task(actor,task['id'])
    _,current_authorities,_=store._snapshot(actor);store._member(actor,current_authorities[current['authority_id']],True)
    store._lease(actor,current,payload['fence'],trusted_now(store))
    return {'path':relative,'sha256':digest}
