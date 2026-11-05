"""SQLite event log for the offline Common Room fixture.

Configuration and Actor objects arrive from a trusted operator/transport. This
module exposes no HTTP, SQL, shell, model, or generic file-browsing endpoint.
Every successful command appends one event and one receipt in one transaction.
"""
from contextlib import closing
from pathlib import Path
import datetime, hashlib, json, math, os, re, sqlite3, time, uuid
from .protocol import Actor, RoomError, canonical, fingerprint, make_event, safe_artifact, validate_message
from . import chat as chat_rules   # A3: the claude-room / dot chat, consulted only while its flag is ON

SCHEMA_VERSION = 2
LEASE_EXPIRY_POLICY = 'release-v1'
ROOT_JOB_CAP = 60  # jobs that can grow under one approved (root) task, however split; see request_help
SCHEMA = (
    "CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, actor TEXT NOT NULL, task_id TEXT, payload_json TEXT NOT NULL, timestamp REAL NOT NULL)",
    "CREATE TABLE receipts(actor TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, response_json TEXT NOT NULL, event_seq INTEGER NOT NULL REFERENCES events(seq), PRIMARY KEY(actor,request_id))",
    "CREATE TABLE clock_state(singleton INTEGER PRIMARY KEY CHECK(singleton=1), observed REAL NOT NULL)",
    "INSERT INTO clock_state(singleton,observed) VALUES(1,0)",
    "CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'immutable events'); END",
    "CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'immutable events'); END",
    "CREATE TRIGGER receipts_no_update BEFORE UPDATE ON receipts BEGIN SELECT RAISE(ABORT,'immutable receipts'); END",
    "CREATE TRIGGER receipts_no_delete BEFORE DELETE ON receipts BEGIN SELECT RAISE(ABORT,'immutable receipts'); END",
)
FIELDS = {
    'grant': {'id','members','capabilities','root_id','scope'},
    'grant_handoff': {'id','members','capabilities','root_id','scope','source_namespaces'},
    'intake': {'source_namespace','external_reference','message'},
    'renew': {'task_id','fence','seconds'},
    'request_help': {'task_id','fence','recipient','title','body','acceptance'},
    'deliver': {'task_id'}, 'ack': {'task_id'},
    'claim': {'task_id','seconds'}, 'assign': {'task_id','recipient'},
    'finish': {'task_id','fence','body','refs'},
    'fail': {'task_id','fence','body'},
    'wait_decision': {'task_id','fence','question'},
    'resolve_decision': {'task_id','body'},
    'reinstate': {'task_id','body'},
    'cancel': {'task_id','body'},
    'review': {'task_id','accepted','note'},
    'pause': {'authority_id'}, 'resume': {'authority_id'}, 'revoke': {'authority_id'},
    'note': {'authority_id','body'},
    'question_mark': {'note_seq','mark'},
    'stop_all': {'reason'}, 'resume_all': set(),
}
# 2026-10-01 (cr-upgrade, core-strikes): fields a command MAY add to its required set. FIELDS stays
# the required contract (board and relay build their action lists from it); a client that never
# sends these gets exactly yesterday's behavior.
OPTIONAL_FIELDS = {'review': {'kind'}, 'resolve_decision': {'refund'}, 'resume_all': {'reason'},
                   'intake': {'undoable','tier','effort'}, 'request_help': {'undoable','tier','effort'}}   # A2 D10: the undoable mark; A4: the tier tag; both set only at creation
# A4 (POST_SWITCH_PLAN_2026-10-22, "Tier tag on every task"): the model tier a task is meant for and the effort to
# run it at (the levels Claude Code offers). Shown to every seat, never enforced: a seat may run any task.
TIERS = ('haiku','sonnet','opus')
EFFORTS = ('low','medium','high','xhigh','max')

def tier_tags(p):
    """The tier and effort a command names ({} when it names neither), or RoomError 'invalid' with the allowed values."""
    tags={}
    for field,allowed in (('tier',TIERS),('effort',EFFORTS)):
        if field in p:
            if type(p[field]) is not str or p[field] not in allowed:
                raise RoomError('invalid','The '+field+' must be one of '+', '.join(allowed)+'.')
            tags[field]=p[field]
    return tags

def undoable_mark(p):
    """A2 D10: the undoable mark a command names ({} when it names none), or RoomError 'invalid'. A2b: one rule for
    intake, request_help and plan files (plans.validate_plan), so a plan is refused with the room's own words."""
    if 'undoable' not in p:return {}
    if type(p['undoable']) is not bool:raise RoomError('invalid','The undoable mark must be true or false.')
    return {'undoable':p['undoable']}

def seat_tier_labels(labels, people):
    """A4: the tier label each listed seat runs on, checked: an enrolled worker or reviewer, and a known tier."""
    if labels is None:return {}
    if type(labels) is not dict or len(labels)>64:raise RoomError('config','Seat tier labels must be a map of at most 64 seats.')
    for seat,tier in labels.items():
        if type(seat) is not str or seat not in people or people[seat].role not in {'worker','reviewer'}:
            raise RoomError('config','A seat tier label names an enrolled worker or reviewer: '+str(seat)+'.')
        if tier not in TIERS:raise RoomError('config','Seat '+seat+' has an unknown tier label; use one of '+', '.join(TIERS)+'.')
    return dict(labels)
# 2026-10-05 (turnons, seat-dot; ruling #1136, approved by Shane): the one way an existing authority's
# membership changes. Members are only ever appended, never removed or reordered. It is NOT in FIELDS on
# purpose: no client command, relay request or board form can reach it. Only the operator's launcher calls
# Store.add_members, at room start, when the shipped enrollment grew a scope (activation.start_room).
MEMBERS_ADDED = 'authority_members_added'

NEVER_REVIEW = frozenset({'duet'})   # duet (2026-10-30): a seat that never reviews, whatever a later stand-in file says

def review_standins(entries, people):
    """A2 D10 (Shane, 2026-10-19: "Yes, undoable only"): the review stand-ins the room was given at start (crew/review-standins.json for the live room), keyed by
    (authority, actor). A stand-in is a worker that may accept or reject a task in that authority only when the task
    was marked undoable at creation and was built by one of `builders`; it records which reviewer it stands in for.
    `markers` are the workers that may set the mark on a helper they ask for (request_help); the owner sets it at
    intake. The stand-in is never a marker or a builder on its own list. Nothing here is an event: the stand-in
    exists only while that file names it, so removing it from the file and restarting undoes it."""
    found={}
    if type(entries) not in (list,tuple) or len(entries)>4:raise RoomError('config','Review stand-ins must be a list of at most four.')
    for e in entries:
        if type(e) is not dict or set(e)!={'actor','stands_for','authority','builders','markers'}:raise RoomError('config','Exact review stand-in fields required.')
        actor,stands_for=e['actor'],e['stands_for']
        if any(type(n) is str and n in NEVER_REVIEW for n in (actor,stands_for)):raise RoomError('config','This seat never reviews and cannot be a review stand-in.')
        if type(actor) is not str or actor not in people or people[actor].role!='worker':raise RoomError('config','A review stand-in must be an enrolled worker.')
        if type(stands_for) is not str or stands_for not in people or people[stands_for].role!='reviewer':raise RoomError('config','A stand-in stands in for an enrolled reviewer.')
        for field,low in (('builders',1),('markers',0)):
            names=e[field]
            if (type(names) is not list or not low<=len(names)<=16 or len(set(names))!=len(names) or actor in names
                    or any(type(x) is not str or x not in people or people[x].role!='worker' for x in names)):
                raise RoomError('config','Stand-in '+field+' must be distinct enrolled workers other than the stand-in.')
        key=(bounded_id(e['authority'],'Authority',32),actor)
        if key in found:raise RoomError('config','One review stand-in per seat and authority.')
        found[key]=json.loads(json.dumps(e))
    return found
STOP_FLAG = 'STOP'  # (cr-upgrade, core-stop) the room-wide stop file, written beside room.sqlite (.runtime/handoffs/STOP)
REVIEW_KINDS = ('substance','paperwork')
PAPERWORK_STRIKE_AT = 3  # the third paperwork return on one task, and each later one, counts as a strike
# 2026-10-24 (A1 comms fix, task t0d912d8c35b7): a note whose first line starts `QUESTION FOR <seat>:` is a question
# addressed to that seat. The store resolves the address once, when the note is written, and keeps it on the note's
# own event as `to`; notes written before this change never gain one. Only a member of the note's scope who is not
# the owner (keeper) and not the sender can be addressed, so `QUESTION FOR SHANE` and every unaddressed note stay
# exactly as they were. The marks after `delivered` (the committed note itself) are `question_marked` events with no
# task id, which fold() skips, so older code replays a store holding them unchanged.
QUESTION_FORM = re.compile(r'QUESTION FOR ([a-z][a-z0-9_-]{0,63}):')
QUESTION_MARK_EVENT = 'question_marked'
QUESTION_MARKS = ('read','accepted','checked')   # in order; delivered is the note itself
QUESTION_EXCERPT = 160

def bounded_id(value, name='Identifier', max_length=80):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,'+str(max_length-1)+'}',value):
        raise RoomError('invalid', name+' must be a bounded plain identifier.')
    return value

def text(value, limit, name=None):
    name = name or 'Text'
    if type(value) is not str:
        raise RoomError('invalid', name + ' must be text.')
    if not value.strip():
        raise RoomError('invalid', name + ' is empty.')
    if len(value)>limit:
        raise RoomError('invalid', name + ' is ' + str(len(value)) + ' characters; the limit is ' + str(limit) + '.')
    if any(ord(c)<32 and c not in '\n\r\t' for c in value):
        raise RoomError('invalid', name + ' has a control character.')
    canonical(value)
    return value

def clone(value): return json.loads(canonical(value))

def read_events(conn):
    return [{'seq':r[0],'kind':r[1],'actor':r[2],'task_id':r[3],
             'payload':json.loads(r[4]),'timestamp':r[5]}
            for r in conn.execute('SELECT seq,kind,actor,task_id,payload_json,timestamp FROM events ORDER BY seq')]

def expiry_facts(task, now):
    lease=task['lease']
    return {'policy':LEASE_EXPIRY_POLICY,'holder':lease['holder'],'fence':lease['fence'],
            'expired_at':lease['until'],'observed_at':now}


def fold(events):
    authorities={};tasks={}
    for expected,event in enumerate(events,1):
        if event['seq']!=expected:
            raise RoomError('corrupt','Event sequence is not continuous.')
        p=event['payload'];kind=event['kind']
        if kind=='authority_granted':
            authorities[p['authority_id']]=clone(p['authority'])
        elif kind==MEMBERS_ADDED:
            authorities[p['authority_id']]['members'].extend(clone(p['members']))  # seat-dot: append-only growth
        elif kind in {'authority_paused','authority_resumed','authority_revoked'}:
            policy=p.get('lease_expiry_policy','legacy-strike-v1')
            if type(policy) is not str or policy not in {'legacy-strike-v1',LEASE_EXPIRY_POLICY}:
                raise RoomError('corrupt','Unknown lease-expiry policy; refusing replay.')
            a=authorities[p['authority_id']]
            if kind=='authority_resumed': a['paused']=False;a.pop('stopped_all',None)
            elif kind=='authority_paused':
                a['paused']=True
                if p.get('room_stop') is not None:a['stopped_all']=clone(p['room_stop'])  # core-stop: paused by stop_all
            else: a['revoked']=True
            if kind!='authority_resumed':
                for task in tasks.values():
                    if task['authority_id']==a['id'] and task['lease'] is not None:
                        expired=task['lease']['until']<=event['timestamp']
                        if expired:
                            if policy=='legacy-strike-v1':
                                # Missing policy marks historical events, whose meaning stays fixed.
                                task['failed_attempts']+=1
                                task['failure_note']='Lease expired before the authority was paused or revoked.'
                                task['failed_actor']=task['lease']['holder']
                                task['failed_fence']=task['lease']['fence']
                            else:
                                task['lease_expiry']=expiry_facts(task,event['timestamp'])
                        task['lease']=None;task['fence']+=1
                        if task['status']=='claimed':
                            task['status']='waiting_decision' if task['failed_attempts']>=2 else 'acknowledged'
                            if task['failed_attempts']>=2:task['decision']='Failure budget exhausted.'
        elif kind=='note' and p.get('room_stop') is not None:
            authorities[p['authority_id']]['room_stopped']=clone(p['room_stop'])  # core-stop: the room-wide stop is on
        elif kind=='note' and p.get('room_resume') is not None:
            authorities[p['authority_id']].pop('room_stopped',None)               # core-stop: the room-wide stop is off
        elif kind=='task_created': tasks[event['task_id']]=clone(p['task'])
        elif event['task_id'] is not None:
            tasks[event['task_id']].update(clone(p['changes']))
    return authorities,tasks

def question_address(body):
    """The seat a note's first line addresses (`QUESTION FOR <seat>:`), or None. Whether that seat can be
    addressed at all is the store's decision when the note is written (Store._apply, 'note')."""
    first=body.split('\n',1)[0]
    match=QUESTION_FORM.match(first)
    return match.group(1) if match else None

def fold_questions(events):
    """Addressed questions by note seq, with their marks: {seq: {seq, authority_id, from, to, asked_at, excerpt,
    marks: {delivered, read, accepted, checked}}}. A mark is the time it was recorded, or None. Kept apart from
    fold() so fold's (authorities, tasks) contract, which older code and every caller rely on, never changes."""
    questions={}
    for event in events:
        p=event['payload']
        if event['kind']=='note' and type(p.get('to')) is str:
            questions[event['seq']]={'seq':event['seq'],'authority_id':p['authority_id'],'from':event['actor'],'to':p['to'],
                'asked_at':event['timestamp'],'excerpt':p['body'][:QUESTION_EXCERPT],
                'marks':{'delivered':event['timestamp'],'read':None,'accepted':None,'checked':None}}
        elif event['kind']==QUESTION_MARK_EVENT and p.get('note_seq') in questions:
            questions[p['note_seq']]['marks'][p['mark']]=event['timestamp']
    return questions

def question_state(question):
    """The furthest mark a question has reached: delivered, read, accepted or checked (never 'sent')."""
    reached='delivered'
    for mark in QUESTION_MARKS:
        if question['marks'][mark] is not None:reached=mark
    return reached

class Store:
    def __init__(self, path, participants, artifact_roots, clock=None, standins=(), seat_tiers=None, chat=None):
        self.path=Path(path).absolute()
        if self.path.is_symlink() or self.path.is_junction() or not self.path.parent.is_dir():
            raise RoomError('config','Store needs an operator-owned local path.')
        if not participants or any(not isinstance(a,Actor) or a.id!=key for key,a in participants.items()):
            raise RoomError('config','Configured identities must be registered Actor objects.')
        self.people=dict(participants);self.roots={}
        for name,value in artifact_roots.items():
            bounded_id(name)
            root=Path(value).absolute()
            if root.is_symlink() or root.is_junction() or not root.is_dir():
                raise RoomError('config','Only existing, non-linked artifact roots may be configured.')
            self.roots[name]=str(root.resolve())
        self.clock=clock or time.time
        self.standins=review_standins(list(standins),self.people)
        self.seat_tiers=seat_tier_labels(seat_tiers,self.people)   # A4: shown, never enforced
        self.chat=chat_rules.pair_value(chat)   # A3: None is OFF, exactly the base head; set only at start
        with closing(self._connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                version=conn.execute('PRAGMA user_version').fetchone()[0]
                if version==0:
                    if conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]:
                        raise RoomError('config','Refusing an unrelated database.')
                    for statement in SCHEMA: conn.execute(statement)
                    conn.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
                elif version!=SCHEMA_VERSION: raise RoomError('config','Unsupported store schema.')
                required={'events_no_update','events_no_delete','receipts_no_update','receipts_no_delete'}
                present={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
                if not required<=present: raise RoomError('corrupt','Store immutability guards are missing.')
                conn.commit()
            except BaseException:
                conn.rollback();raise

    def _connect(self):
        conn=sqlite3.connect(str(self.path),timeout=10,isolation_level=None)
        conn.execute('PRAGMA foreign_keys=ON');conn.execute('PRAGMA synchronous=FULL')
        return conn

    def _actor(self, actor):
        if not isinstance(actor,Actor) or self.people.get(actor.id)!=actor:
            raise RoomError('forbidden','Actor must come from registered authentication context.')

    def _member(self, actor, authority, active=False):
        if actor.id not in authority['members']:
            raise RoomError('forbidden','This authority does not include the participant.')
        if active and (authority['paused'] or authority['revoked']):
            raise RoomError('held','This authority is paused or revoked.')
        if self.roots.get(authority['root_id'])!=authority['artifact_root']:
            raise RoomError('config','Authority artifact-root configuration changed; stop for the operator.')

    def _owner(self, actor, authority=None):
        if actor.role!='owner' or (authority and authority['owner']!=actor.id):
            raise RoomError('forbidden','Only this authority owner may make this decision.')

    def _now(self, conn, events):
        value=self.clock()
        try:valid=type(value) in (int,float) and math.isfinite(value) and value>=0
        except OverflowError:valid=False
        if not valid:raise RoomError('clock','A finite nonnegative clock reading is required.')
        observed=conn.execute('SELECT observed FROM clock_state WHERE singleton=1').fetchone()[0]
        now=max(value,observed,events[-1]['timestamp'] if events else 0)
        conn.execute('UPDATE clock_state SET observed=? WHERE singleton=1',(now,))
        return now

    def execute(self, actor, action, payload, request_id):
        self._actor(actor)
        chat_actions=chat_rules.ACTIONS if self.chat is not None else {}   # A3: unknown actions while OFF
        if type(action) is not str or action not in set(FIELDS)|{'create'}|set(chat_actions) or type(payload) is not dict:
            raise RoomError('invalid','Unsupported command or payload.')
        required=chat_actions[action] if action in chat_actions else FIELDS.get(action)
        optional=(chat_rules.OPTIONAL if action in chat_actions else OPTIONAL_FIELDS).get(action,set())
        if action!='create' and not (required<=set(payload)<=required|optional):
            raise RoomError('invalid','Command fields must match the supported contract exactly.')
        if type(request_id) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}',request_id):
            raise RoomError('invalid','A bounded idempotency key is required.')
        if action in {'claim','renew'} and (type(payload['seconds']) is not int or not 1<=payload['seconds']<=1800):
            raise RoomError('invalid','Lease duration must be a whole number from 1 through 1800 seconds.')
        if 'fence' in payload and (type(payload['fence']) is not int or payload['fence']<1):
            raise RoomError('invalid','A positive integer lease fence is required.')
        if len(canonical(payload).encode('utf-8'))>16384:
            raise RoomError('too_large','Command exceeds the request budget.')
        return self._commit(actor,action,payload,request_id)

    def add_members(self, actor, authority_id, members, request_id):
        """Append `members` to an existing authority: keeper only, append-only (seat-dot, ruling #1136).
        The operator's launcher calls it at room start when the shipped enrollment grew a scope; it is not
        a client command (see MEMBERS_ADDED). One event and one receipt, like a command: a repeat under the
        same request id answers from its receipt, and replaying the log rebuilds the same members."""
        self._actor(actor)
        if type(request_id) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}',request_id):
            raise RoomError('invalid','A bounded idempotency key is required.')
        return self._commit(actor,'add_members',{'authority_id':authority_id,'members':members},request_id)

    def _commit(self, actor, action, payload, request_id):
        payload=clone(payload);digest=fingerprint(action,payload)
        with closing(self._connect()) as conn:
            conn.execute('BEGIN IMMEDIATE');command_started=False;flag_job=None
            try:
                events=read_events(conn);authorities,tasks=fold(events)
                # Preserve trusted clock observations even when a command is refused.
                # The savepoint keeps the event/receipt pair atomic independently.
                now=self._now(conn,events)
                conn.execute('SAVEPOINT command');command_started=True
                old=conn.execute('SELECT fingerprint,response_json FROM receipts WHERE actor=? AND request_id=?',(actor.id,request_id)).fetchone()
                if old:
                    if old[0]!=digest:raise RoomError('idempotency','This key already names a different request.')
                    response=json.loads(old[1])
                else:
                    if action=='question_mark':result=self._question_mark(actor,payload,authorities,fold_questions(events))
                    elif self.chat is not None and action in chat_rules.ACTIONS:
                        result=self._chat_command(actor,action,payload,authorities,events,now)
                    else:result=self._apply(actor,action,payload,authorities,tasks,now)
                    if self.chat is not None and action=='note':result=self._chat_note(actor,result,events,now)
                    # core-stop: the room-wide switches append several events (a pause or resume and a note
                    # for each authority) under ONE receipt, the last event's; every other command is one event.
                    batch=result if type(result) is list else [result]
                    log=list(events)
                    for kind,task_id,event_payload in batch:
                        event=make_event(len(log)+1,kind,actor,task_id,event_payload,now)
                        conn.execute('INSERT INTO events(seq,kind,actor,task_id,payload_json,timestamp) VALUES(?,?,?,?,?,?)',
                            (event.seq,event.kind,event.actor,event.task_id,event.payload_json,event.timestamp))
                        log.append(event.as_dict())
                    _,updated=fold(log)
                    response={'seq':log[-1]['seq'],'task':updated.get(task_id)}
                    if type(result) is list:response['seqs']=[e['seq'] for e in log[len(events):]]
                    if action in {'note','question_mark'}:
                        asked=fold_questions(log).get(payload['note_seq'] if action=='question_mark' else log[-1]['seq'])
                        if asked is not None:response['question']=dict(asked,state=question_state(asked))  # A1: what was recorded
                    if action=='note' and 'chat' in log[-1]['payload']:response['chat']=clone(log[-1]['payload']['chat'])  # A3
                    conn.execute('INSERT INTO receipts(actor,request_id,fingerprint,response_json,event_seq) VALUES(?,?,?,?,?)',
                        (actor.id,request_id,digest,canonical(response),log[-1]['seq']))
                    if action=='stop_all':flag_job=('stop',{'by':actor.id,'at':self._iso(now),'reason':payload['reason']})
                    elif action=='resume_all':flag_job=('resume',None)
                conn.execute('RELEASE command');command_started=False
                conn.commit();out=clone(response)
                if flag_job:out['flag']=self._write_flag(*flag_job)  # after the commit; only for a command applied just now
                return out
            except BaseException:
                if command_started:
                    conn.execute('ROLLBACK TO command');conn.execute('RELEASE command')
                    conn.commit()  # Clock floor only; no failed event or receipt.
                else:conn.rollback()
                raise

    @staticmethod
    def _iso(now):
        return datetime.datetime.fromtimestamp(now,datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    def _write_flag(self, which, record):
        """The STOP file beside room.sqlite: JSON {by, at, reason} while the room is stopped, absent otherwise.
        The stop itself is the committed events; this file is the signal other processes read. A failure here
        is reported back as the command's flag status, it never undoes the committed stop."""
        path=self.path.parent/STOP_FLAG
        try:
            if which=='stop':
                temp=path.with_name(STOP_FLAG+'.tmp')
                temp.write_text(json.dumps(record,indent=2,sort_keys=True)+chr(10),encoding='utf-8',newline=chr(10))
                os.replace(temp,path);return 'written'
            path.unlink(missing_ok=True);return 'removed'
        except OSError as exc:
            return 'failed: '+type(exc).__name__

    def _room_switch(self, actor, action, p, authorities, now):
        """stop_all / resume_all (cr-upgrade, core-stop): keeper only. Reuses the room's own pause and resume
        events, one per authority. Two facts are kept apart on purpose:
          * which authorities the stop paused: the `room_stop` marker on each pause event it writes, kept by
            fold() as `stopped_all` until that authority is resumed (by resume_all or by hand);
          * that the room IS stopped: a `room_stop` record on each STOP: note, kept by fold() as `room_stopped`
            until a RESUME: note clears it. It lists which authorities the stop paused and which were already
            paused. The banner, the STOP file and resume_all follow this record, so a stop in which every
            authority was already paused, or one whose authorities were resumed one by one, can still be undone.
        resume_all resumes only the authorities that still carry `stopped_all` and leaves any authority paused
        by hand, or revoked, as it was. Membership and artifact-root checks are skipped on purpose: an
        emergency stop must not fail because one authority's configuration drifted."""
        self._owner(actor)
        mine=[a for a in authorities.values() if a['owner']==actor.id and not a['revoked']]
        if not mine:raise RoomError('state','There is no active authority to '+('stop.' if action=='stop_all' else 'resume.'))
        stopped=[a for a in mine if 'room_stopped' in a]
        events=[]
        if action=='stop_all':
            reason=text(p['reason'],500,'Room reason')
            if stopped:raise RoomError('state','The room is already stopped; resume it first.')
            record={'by':actor.id,'at':now,'reason':reason,
                    'paused':[a['id'] for a in mine if not a['paused']],'already_paused':[a['id'] for a in mine if a['paused']]}
            marker={'by':actor.id,'at':now,'reason':reason}
            for a in mine:
                if not a['paused']:events.append(('authority_paused',None,{'authority_id':a['id'],'lease_expiry_policy':LEASE_EXPIRY_POLICY,'room_stop':marker}))
                events.append(('note',None,{'authority_id':a['id'],'body':text('STOP: '+reason,4000,'Stop note'),'room_stop':record}))
            return events
        reason=text(p['reason'],500,'Room reason') if 'reason' in p else 'Resumed by the keeper.'
        if not stopped:raise RoomError('state','The room is not stopped.')
        for a in mine:
            if 'stopped_all' in a:events.append(('authority_resumed',None,{'authority_id':a['id'],'lease_expiry_policy':LEASE_EXPIRY_POLICY}))
        for a in mine:events.append(('note',None,{'authority_id':a['id'],'body':text('RESUME: '+reason,4000,'Resume note'),'room_resume':{'by':actor.id,'at':now}}))
        return events

    def _question_mark(self, actor, p, authorities, questions):
        """A1: record one mark on an addressed question, in order (read, then accepted, then checked), each once.
        read and accepted are the addressed seat's own marks, checked is the asker's. A mark is an event of its own
        with no task id; it posts no note, addresses no one and wakes no one, so a reply can never start a loop."""
        seq=p['note_seq'];mark=p['mark']
        if type(seq) is not int or seq<1:raise RoomError('invalid','note_seq must be the positive number of a note.')
        if type(mark) is not str or mark not in QUESTION_MARKS:raise RoomError('invalid','mark must be read, accepted or checked.')
        question=questions.get(seq)
        if question is None:raise RoomError('forbidden','Note #'+str(seq)+' is not a question addressed to a seat.')
        a=authorities[question['authority_id']];self._member(actor,a,True)
        if actor.role=='observer':raise RoomError('forbidden','Observers cannot post commands.')
        if actor.id!=(question['from'] if mark=='checked' else question['to']):
            raise RoomError('forbidden','Only '+('the asker, '+question['from'] if mark=='checked' else 'the addressed seat, '+question['to'])+', may mark question #'+str(seq)+' '+mark+'.')
        if question['marks'][mark] is not None:raise RoomError('state','Question #'+str(seq)+' is already marked '+mark+'.')
        before=QUESTION_MARKS[QUESTION_MARKS.index(mark)-1] if mark!='read' else None
        if before is not None and question['marks'][before] is None:
            raise RoomError('state','Question #'+str(seq)+' must be marked '+before+' before '+mark+'.')
        return QUESTION_MARK_EVENT,None,{'authority_id':a['id'],'note_seq':seq,'mark':mark}

    def _chat_note(self, actor, result, events, now):
        """A3, ON only: a note from one of the pair to the other, in the pair's scope, whose first line is the chat
        form, gets its `chat` key now, inside this command's transaction (caps and dedupe count the store's own log).
        Every other note is returned exactly as _apply made it."""
        kind,task_id,note=result
        address=chat_rules.chat_address(note['body'])
        if ('to' in note or address is None or actor.id not in self.chat['seats'] or note['authority_id']!=self.chat['scope']
                or address[0]!=chat_rules.other(self.chat,actor.id)):
            return result
        note=dict(note,chat=chat_rules.check_line(actor.id,address[0],address[1],note['body'],self.chat,events,now))
        return kind,task_id,note

    def _chat_command(self, actor, action, p, authorities, events, now):
        """A3, ON only: chat_mark, chat_status, chat_close. A member of the pair's scope, in it while it is active
        (a paused scope answers 'held'), and one of the pair; the event has no task id, posts no note, wakes no one."""
        authority_id=bounded_id(p.get('authority_id'),'Authority')
        if authority_id not in authorities:raise RoomError('forbidden','No such authority is granted.')
        self._member(actor,authorities[authority_id],True)
        if actor.role=='observer':raise RoomError('forbidden','Observers cannot post commands.')
        if authority_id!=self.chat['scope'] or actor.id not in self.chat['seats']:
            raise RoomError('forbidden','Chat is only between '+' and '.join(self.chat['seats'])+' in '+self.chat['scope']+'.')
        return chat_rules.command(actor.id,action,p,self.chat,events,now)

    def _expired(self, task, now):
        if task['status']!='claimed' or task['lease']['until']>now:return {}
        return {'lease_expiry':expiry_facts(task,now)}

    def _expired_stop(self, task, changes):
        changes.update(status='waiting_decision',lease=None,fence=task['fence']+1,
                       decision='Failure budget exhausted by an expired attempt.')
        return 'task_expired',task['id'],{'authority_id':task['authority_id'],'changes':changes}

    def _apply(self, actor, action, p, authorities, tasks, now, intake_metadata=None, undoable=None, tags=None):
        if action in {'grant','grant_handoff'}:
            self._owner(actor);id=bounded_id(p['id']);members=p['members'];caps=p['capabilities']
            if id in authorities:raise RoomError('exists','Authorities cannot be overwritten.')
            if (not isinstance(members,list) or not 1<=len(members)<=32 or
                any(type(x) is not str or x not in self.people for x in members) or
                len(set(members))!=len(members) or actor.id not in members):
                raise RoomError('forbidden','Authority membership must name registered participants and its owner.')
            if not isinstance(caps,list) or caps!=(['handoff.record'] if action=='grant_handoff' else ['document.count']) or type(p['root_id']) is not str or p['root_id'] not in self.roots:
                raise RoomError('forbidden','Only the configured fixture document capability/root may be granted.')
            a={'id':id,'owner':actor.id,'members':list(members),'capabilities':list(caps),'root_id':p['root_id'],
               'artifact_root':self.roots[p['root_id']],'scope':text(p['scope'],2000,'Authority scope'),'paused':False,'revoked':False}
            if action=='grant_handoff':
                namespaces=p['source_namespaces']
                if type(namespaces) is not list or not 1<=len(namespaces)<=32:
                    raise RoomError('invalid','Configure bounded source namespaces.')
                for namespace in namespaces:bounded_id(namespace,'Source namespace',64)
                if len(set(namespaces))!=len(namespaces):raise RoomError('invalid','Duplicate source namespace.')
                a['source_namespaces']=list(namespaces)
            return 'authority_granted',None,{'authority_id':id,'authority':a}

        if action=='add_members':   # reached only through Store.add_members, never through execute (not in FIELDS)
            self._owner(actor);aid=bounded_id(p['authority_id'],'Authority');members=p['members']
            if aid not in authorities:raise RoomError('forbidden','No such authority is granted.')
            a=authorities[aid];self._owner(actor,a)
            if a['revoked']:raise RoomError('held','A revoked authority gains no members.')
            if (not isinstance(members,list) or not members or any(type(x) is not str or x not in self.people or x=='keeper' for x in members)
                    or len(set(members))!=len(members) or any(x in a['members'] for x in members) or len(a['members'])+len(members)>32):
                raise RoomError('forbidden','Only new, registered, distinct participants may be appended, up to 32 members in all.')
            return MEMBERS_ADDED,None,{'authority_id':aid,'members':list(members)}

        if action=='intake':
            self._owner(actor)
            if type(p['message']) is not dict:raise RoomError('invalid','Intake needs a message object.')
            aid=bounded_id(p['message'].get('authority_id'),'Authority')
            if aid not in authorities:raise RoomError('forbidden','No such authority is granted.')
            a=authorities[aid];self._owner(actor,a);self._member(actor,a,True)
            namespace=bounded_id(p['source_namespace'],'Source namespace',64)
            reference=bounded_id(p['external_reference'],'External reference',80)
            if a['capabilities']!=['handoff.record'] or namespace not in a.get('source_namespaces',[]):
                raise RoomError('forbidden','Intake needs the granted handoff capability and source namespace.')
            checked=validate_message(actor,p['message'],a,self.people)
            undoable_mark(p)   # A2b: the same rule plan files use
            tags=tier_tags(p)
            # A2 D10: the owner's mark is part of the brief, so a repeat with a different mark is a different brief.
            # A4: a brief's tier tag is part of the brief, so a repeat with another tag is a different brief.
            # Both: the mark, then the tags (fingerprint is canonical JSON with sorted keys, so their order cannot matter).
            extra=dict({'undoable':p['undoable']} if 'undoable' in p else {},**tags)
            digest=fingerprint('handoff-intake',dict(checked,**extra) if extra else checked)
            for task in tasks.values():
                meta=task.get('intake')
                if task['authority_id']==aid and meta and meta['source_namespace']==namespace and meta['external_reference']==reference:
                    if meta['fingerprint']!=digest:raise RoomError('idempotency','This external reference has a different original brief.')
                    return 'task_intake_duplicate',task['id'],{'authority_id':aid,'changes':{}}
            self._reviewer_guard(a,checked['recipient'])  # only a NEW job: a repeat of an existing brief is answered above
            return self._apply(actor,'create',p['message'],authorities,tasks,now,
                {'source_namespace':namespace,'external_reference':reference,'fingerprint':digest},undoable=p.get('undoable'),tags=tags)

        if action in {'stop_all','resume_all'}:return self._room_switch(actor,action,p,authorities,now)

        if action in {'create','pause','resume','revoke','note'}:
            authority_id=bounded_id(p.get('authority_id'),'Authority')
            if authority_id not in authorities:raise RoomError('forbidden','No such authority is granted.')
            a=authorities[authority_id];self._member(actor,a)
            if action in {'pause','resume','revoke'}:
                self._owner(actor,a)
                if a['revoked']:raise RoomError('revoked','Revocation cannot be undone.')
                return {'pause':'authority_paused','resume':'authority_resumed','revoke':'authority_revoked'}[action],None,{'authority_id':a['id'],'lease_expiry_policy':LEASE_EXPIRY_POLICY}
            self._member(actor,a,True)
            if actor.role=='observer':raise RoomError('forbidden','Observers cannot post commands.')
            if action=='note':
                note={'authority_id':a['id'],'body':text(p['body'],4000,'Note body')}
                seat=question_address(note['body'])
                if seat is not None and seat!=actor.id and seat in a['members'] and seat in self.people and self.people[seat].role!='owner':
                    note['to']=seat   # A1: delivered to that seat; a name that is not addressable leaves a plain note
                return 'note',None,note
            msg=validate_message(actor,p,a,self.people)
            if msg['kind']=='handoff.record' and intake_metadata is None:
                raise RoomError('forbidden','Handoffs must enter through owner intake with an external reference.')
            if self.people[msg['recipient']].role!='worker':raise RoomError('forbidden','Document tasks need a registered worker recipient.')
            parent=msg['parent_id'];depth=msg['chain_depth']
            if parent is not None:
                if parent not in tasks or tasks[parent]['authority_id']!=a['id']:
                    raise RoomError('forbidden','Parent must be an existing task under this authority.')
                if depth!=tasks[parent]['chain_depth']+1:raise RoomError('budget','Chain depth must follow the stored parent.')
            elif depth!=0:raise RoomError('budget','Root tasks must have depth zero.')
            id='t'+uuid.uuid4().hex
            task=dict(msg,id=id,status='draft',lease=None,fence=0,builder=None,
                      failed_attempts=0,result_refs=[],result_body='',decision=None,reviewer=None)
            if intake_metadata is not None:task['intake']=clone(intake_metadata)
            if undoable is not None:task['undoable']=undoable   # A2 D10: set here, at creation, and by no later command
            if tags:task.update(tags)   # A4: tier and effort, at creation only
            return 'task_created',id,{'authority_id':a['id'],'task':task}

        task_id=bounded_id(p['task_id'],'Task')
        if task_id not in tasks:raise RoomError('forbidden','Task is not available. A full task id has 33 characters; the supplied id has '+str(len(task_id))+'.')
        t=tasks[task_id];a=authorities[t['authority_id']];self._member(actor,a,True)
        status=t['status'];changes={}
        if action=='request_help':
            if t['kind']!='handoff.record':raise RoomError('forbidden','Only a handoff may request a helper.')
            self._lease(actor,t,p['fence'],now)
            recipient=p['recipient']
            if type(recipient) is not str or recipient==actor.id or recipient not in a['members'] or self.people[recipient].role!='worker':
                raise RoomError('forbidden','Choose a different worker in this project.')
            self._reviewer_guard(a,recipient,actor.id)
            if undoable_mark(p):   # A2b: the same rule plan files use
                if not any(aid==a['id'] and actor.id in s['markers'] for (aid,_),s in self.standins.items()):
                    raise RoomError('forbidden','Only the owner at intake, or a marker the review stand-ins name, may mark a task undoable or not.')
            tags=tier_tags(p)   # A4: checked before anything is created
            # 2026-09-24 (orchestra, gate K): cancelled helpers free a live slot so a bad
            # delegation can be withdrawn and redone; the total ceiling stops endless churn.
            children=[x for x in tasks.values() if x['parent_id']==task_id]
            if sum(x['status'] not in {'done','cancelled'} for x in children)>=8:raise RoomError('budget','At most eight open helper assignments per task.')
            if len(children)>=24:raise RoomError('budget','At most twenty-four helper assignments per task, cancelled ones included.')
            # 2026-09-24 (autonomy build B, budget envelopes): the whole tree under one approved
            # (root) task is capped, every status counted, cancelled included, so the envelope
            # a reserve task shows Shane is real however the work is split. Refusal only: no
            # event, and fold() is untouched.
            root=t
            while root['parent_id'] is not None:root=tasks[root['parent_id']]
            tree={root['id']};grown=True
            while grown:
                grown=False
                for x in tasks.values():
                    if x['parent_id'] in tree and x['id'] not in tree:tree.add(x['id']);grown=True
            if len(tree)-1>=ROOT_JOB_CAP:raise RoomError('budget','At most sixty jobs can grow under one approved task.')
            msg=validate_message(actor,dict(title=p['title'],body=p['body'],acceptance=p['acceptance'],recipient=recipient,
                authority_id=a['id'],kind='handoff.record',parent_id=task_id,chain_depth=t['chain_depth']+1),a,self.people)
            child_id='t'+uuid.uuid4().hex
            child=dict(msg,id=child_id,status='delivered',lease=None,fence=0,builder=None,failed_attempts=0,result_refs=[],result_body='',decision=None,reviewer=None,
                intake={'source_namespace':'room-helper','external_reference':child_id,'fingerprint':fingerprint('room-helper',msg)})
            if 'undoable' in p:child['undoable']=p['undoable']   # A2 D10: the marker's mark, at creation only
            child.update(tags)   # A4: the asker's tier and effort for the helper
            return 'task_created',child_id,{'authority_id':a['id'],'task':child,'requested_from':task_id,'parent_fence':p['fence']}
        if t['kind']=='handoff.record' and action in {'finish','assign'} and any(x['parent_id']==task_id and x['status'] not in {'done','cancelled'} for x in tasks.values()):
            raise RoomError('helpers_pending','Resolve and review helper assignments before completing or reassigning the parent.')
        if action=='deliver':
            if actor.id not in {t['sender'],a['owner']}:raise RoomError('forbidden','Only sender or owner may deliver a draft.')
            if status!='draft':raise RoomError('state','Only a draft can be delivered.')
            changes={'status':'delivered'}
        elif action=='ack':
            self._recipient(actor,t)
            if status!='delivered':raise RoomError('state','Acknowledgment follows delivery.')
            changes={'status':'acknowledged'}
        elif action=='assign':
            self._owner(actor,a);recipient=p['recipient']
            if t['failed_attempts']>=2:raise RoomError('budget','Failure budget is exhausted; this task cannot be reset.')
            if type(recipient) is not str or recipient not in a['members'] or self.people[recipient].role!='worker':
                raise RoomError('forbidden','Recipient must be an authorized worker.')
            if status=='claimed' and t['lease']['until']>now:raise RoomError('claimed','Wait for expiry or explicitly pause the authority before reassignment.')
            if status not in {'draft','delivered','acknowledged','claimed'}:raise RoomError('state','This task cannot be reassigned in its current state.')
            changes=self._expired(t,now)
            if changes.get('failed_attempts',0)>=2:return self._expired_stop(t,changes)
            changes.update(recipient=recipient,status='delivered',lease=None,fence=t['fence']+1,builder=None)
        elif action=='claim':
            self._recipient(actor,t)
            if t['failed_attempts']>=2:raise RoomError('budget','Two failed attempts exhaust this task budget.')
            if status=='claimed' and t['lease']['until']>now:raise RoomError('claimed','There is already a current lease.')
            if status not in {'acknowledged','claimed'}:raise RoomError('state','Only acknowledged or expired work may be claimed.')
            changes=self._expired(t,now)
            if changes.get('failed_attempts',0)>=2:return self._expired_stop(t,changes)
            fence=t['fence']+1
            changes.update(status='claimed',fence=fence,builder=actor.id,
                           lease={'holder':actor.id,'fence':fence,'until':now+p['seconds']})
        elif action=='renew':
            if t['kind']!='handoff.record':raise RoomError('forbidden','Only handoff leases may renew.')
            self._lease(actor,t,p['fence'],now)
            changes={'lease':dict(t['lease'],until=now+p['seconds'])}
        elif action in {'finish','fail','wait_decision'}:
            self._lease(actor,t,p['fence'],now)
            changes={'lease':None,'fence':t['fence']+1}
            if action=='finish':
                refs=self._refs(a,p['refs'])
                if not refs:raise RoomError('artifact','A document result must include an artifact.')
                changes.update(status='ready_for_review',result_refs=refs,result_body=text(p['body'],4000,'Finish summary'))
            elif action=='fail':
                count=t['failed_attempts']+1;body=text(p['body'],4000,'Failure explanation')
                changes.update(failed_attempts=count,status='waiting_decision' if count>=2 else 'acknowledged',
                               failure_note=body,failed_actor=actor.id,failed_fence=p['fence'],
                               decision='Failure budget exhausted: '+body if count>=2 else None)
            else:changes.update(status='waiting_decision',decision=text(p['question'],2000,'Decision question'))
        elif action=='resolve_decision':
            self._owner_or_helper_sender(actor,a,t)
            refund=p.get('refund',False)
            if type(refund) is not bool:raise RoomError('invalid','The refund flag must be a boolean.')
            if t['failed_attempts']>=2 and not refund:raise RoomError('budget','A decision cannot erase recorded failures.')
            # 2026-10-01 (cr-upgrade): a refund is the one way a decision lowers the count, and a
            # stopped task (two strikes) stays the owner's alone, as for cancel.
            if t['failed_attempts']>=2 and actor.id!=a['owner']:raise RoomError('budget','Only the owner may refund a task stopped by two strikes.')
            if status!='waiting_decision':raise RoomError('state','No owner decision is waiting.')
            # 2026-09-13: the submitted result is no longer erased here. Returning work for
            # revision destroyed the only board-side copy of what had been handed in, and
            # nothing in the review path needs it gone - finish overwrites both fields on
            # the next submission. Keeping them means an owner decision costs no evidence.
            changes={'status':'delivered','decision':None,'decision_response':text(p['body'],4000,'Decision response'),
                     'lease':None,'fence':t['fence']+1,'builder':None,'reviewer':None}
            if refund:
                after=max(0,t['failed_attempts']-1)
                changes['failed_attempts']=after
                changes['refunds']=list(t.get('refunds',[]))+[{'by':actor.id,'reason':changes['decision_response'],'strikes_before':t['failed_attempts'],'strikes_after':after}]
        elif action=='reinstate':
            # 2026-09-13, added on Shane's instruction while Astra was unavailable.
            # The failure budget is deliberately one-way: resolve_decision refuses to erase
            # recorded failures, and three tests defend that. This does not change it. It
            # adds a separate, deliberate owner act, with its own written reason and its own
            # event, for the case the budget was never designed to cover: a chat-driven
            # worker whose lease lapsed because nobody was running a turn. failure_note,
            # failed_actor and failed_fence are left in place on purpose - this clears the
            # counter, it does not rewrite what happened.
            self._owner(actor,a)
            if status=='cancelled':raise RoomError('state','Cancelled work is closed and cannot be reinstated.')
            if t['failed_attempts']<1:raise RoomError('state','This task has no recorded failures to reinstate from.')
            if status=='claimed' and t['lease'] is not None and t['lease']['until']>now:
                raise RoomError('claimed','Wait for expiry or pause the authority before reinstating live work.')
            changes={'status':'delivered','decision':None,'lease':None,'fence':t['fence']+1,
                     'builder':None,'reviewer':None,'failed_attempts':0,
                     'decision_response':'Owner reinstated after '+str(t['failed_attempts'])+' recorded failure(s): '+text(p['body'],4000,'Reinstatement reason')}
        elif action=='cancel':
            # 2026-09-24 (orchestra, gate K): withdraw a task. Terminal. A live lease does not
            # stop it; the fence moves, so the worker's later finish/fail is refused.
            # failed_attempts is left exactly as recorded.
            self._owner_or_helper_sender(actor,a,t)
            if status in {'done','cancelled'}:raise RoomError('state','Done or cancelled work cannot be cancelled.')
            # 2026-09-24 (after verification F3): stopped work (two strikes) is the owner's alone to
            # withdraw, so a sender cannot re-issue the same work with a fresh budget without Shane.
            if t['failed_attempts']>=2 and actor.id!=a['owner']:raise RoomError('budget','Only the owner may withdraw a task stopped by two strikes.')
            changes={'status':'cancelled','lease':None,'fence':t['fence']+1,'decision':None,
                     'cancel_note':text(p['body'],2000,'Cancellation reason'),'cancelled_by':actor.id}
        elif action=='review':
            if actor.id==t['builder']:raise RoomError('self_review','The builder cannot accept its own work.')
            standin=self._standin(actor,a,t) if actor.role!='reviewer' else None
            if status!='ready_for_review':raise RoomError('state','Only submitted work may be reviewed.')
            if type(p['accepted']) is not bool:raise RoomError('invalid','The review decision must be a boolean.')
            kind=p.get('kind','substance')
            if kind not in REVIEW_KINDS:raise RoomError('invalid','The review kind must be substance or paperwork.')
            note=text(p['note'],2000,'Review note')
            if p['accepted']:self._refs(a,t['result_refs'])
            changes={'status':'done' if p['accepted'] else 'waiting_decision','reviewer':actor.id,'review_note':note,
                     'decision':None if p['accepted'] else 'Review requires an owner decision: '+note}
            if standin is not None:changes['standin_for']=standin['stands_for']   # A2 D10: whom this review stood in for
            if not p['accepted']:
                # 2026-10-01 (cr-upgrade): a paperwork return sends the task back like any rejection
                # but costs no strike until the third one on this task (and each after it). Accepted
                # reviews ignore the kind. A review with no kind is a substance review, as before.
                strike=True
                if 'kind' in p:changes['review_kind']=kind
                if kind=='paperwork':
                    returns=t.get('paperwork_returns',0)+1
                    changes['paperwork_returns']=returns
                    strike=returns>=PAPERWORK_STRIKE_AT
                if strike:
                    changes.update(failed_attempts=t['failed_attempts']+1,failure_note='Review rejected: '+note,
                                   failed_actor=t['builder'],failed_fence=t['fence'])
        else:raise RoomError('invalid','Unsupported task command.')
        return 'task_'+action,task_id,{'authority_id':a['id'],'changes':changes}

    def _standin(self, actor, authority, task):
        """A2 D10: a worker reviews only as an enrolled stand-in, and only a task marked undoable at creation, built
        by one of the stand-in's builders, whose brief it did not write. Anything else refuses, as before."""
        entry=self.standins.get((authority['id'],actor.id))
        if entry is None:raise RoomError('forbidden','Completion needs a separately registered reviewer.')
        if task.get('undoable') is not True:
            raise RoomError('forbidden',actor.id+' may review only a task marked undoable when it was created; this one is '+
                            ('marked can\'t-undo.' if task.get('undoable') is False else 'unmarked.'))
        if task.get('builder') not in entry['builders']:
            raise RoomError('forbidden',actor.id+' stands in for '+entry['stands_for']+' only on work built by '+', '.join(entry['builders'])+'.')
        if task.get('sender')==actor.id:raise RoomError('forbidden',actor.id+' wrote this brief, so it cannot review the work.')
        return entry

    def _reviewer_guard(self, authority, recipient, sender=None):
        """cr-upgrade, core-reviewer-guard (gap found by proof-silent, Lounge #553): refuse to queue a job nobody
        could ever review. The builder cannot review its own work and only a reviewer-role member can, so the
        authority needs a reviewer who is neither the assigned worker nor (for a helper) the sender. Refused
        `forbidden` with nothing created. Jobs that already exist are not touched."""
        out={recipient}|({sender} if sender else set())
        if not any(self.people[x].role=='reviewer' and x not in out for x in authority['members']):
            raise RoomError('forbidden','Authority '+authority['id']+' has no reviewer other than the assigned worker, so this job could never be reviewed. Enroll a reviewer in it first.')

    def _owner_or_helper_sender(self, actor, authority, task):
        # The worker that delegated a helper task (request_help) may answer or withdraw that
        # helper; everything else stays with the authority owner.
        if (task.get('intake') or {}).get('source_namespace')=='room-helper' and actor.id==task['sender']:return
        self._owner(actor,authority)

    def _recipient(self, actor, task):
        if actor.id!=task['recipient'] or actor.role!='worker':raise RoomError('forbidden','Only the assigned worker may acknowledge or claim.')

    def _lease(self, actor, task, fence, now):
        lease=task['lease']
        if task['status']!='claimed' or lease is None or lease['holder']!=actor.id or lease['fence']!=fence or now>=lease['until']:
            raise RoomError('lease','This worker lease is missing, expired or fenced out.')
        self._recipient(actor,task)

    def _refs(self, authority, refs):
        if not isinstance(refs,list) or len(refs)>8:raise RoomError('artifact','At most eight artifact references are allowed.')
        result=[]
        for ref in refs:
            if not isinstance(ref,dict) or set(ref)!={'path','sha256'}:raise RoomError('artifact','Artifacts need a relative path and SHA-256.')
            safe_artifact(authority['artifact_root'],ref['path'],ref['sha256']);result.append(dict(ref))
        return result

    def _snapshot(self, actor):
        self._actor(actor)
        with closing(self._connect()) as conn:events=read_events(conn)
        authorities,tasks=fold(events)
        return events,authorities,tasks

    def task(self, actor, task_id):
        bounded_id(task_id,'Task');_,authorities,tasks=self._snapshot(actor)
        if task_id not in tasks:raise RoomError('forbidden','Task is not available. A full task id has 33 characters; the supplied id has '+str(len(task_id))+'.')
        task=tasks[task_id];self._member(actor,authorities[task['authority_id']])
        return clone(task)

    def list_tasks(self, actor):
        _,authorities,tasks=self._snapshot(actor)
        return [clone(t) for t in tasks.values() if actor.id in authorities[t['authority_id']]['members']]

    def poll(self, actor, after=0, limit=100):
        if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=100:
            raise RoomError('invalid','Polling needs a nonnegative cursor and a limit from 1 through 100.')
        events,authorities,_=self._snapshot(actor)
        page=[e for e in events if e['seq']>after][:limit]
        visible=[e for e in page if actor.id in authorities[e['payload']['authority_id']]['members']]
        if chat_rules.hidden_from(self.chat,actor):visible=[e for e in visible if not chat_rules.is_chat_event(e)]   # A3, ruling 1
        return {'events':clone(visible),'cursor':page[-1]['seq'] if page else after}

    @staticmethod
    def _copy_backup(source, destination, root):
        source=Path(source).absolute();destination=Path(destination).absolute();root=Path(root).resolve()
        try:
            if source.is_symlink() or source.is_junction() or destination.exists() or destination.is_symlink() or destination.is_junction():
                raise RoomError('backup','Use a new non-linked destination; never overwrite an existing store.')
            source.resolve(strict=True).relative_to(root)
            destination.parent.resolve(strict=True).relative_to(root)
            with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True,timeout=10)) as src:
                if src.execute('PRAGMA user_version').fetchone()[0]!=SCHEMA_VERSION or src.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                    raise RoomError('backup','Backup is not a healthy supported store.')
                fd=os.open(str(destination),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
                with closing(sqlite3.connect(str(destination))) as dst:
                    src.backup(dst)
                    if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise RoomError('backup','Restored store failed its integrity check.')
        except (sqlite3.Error,OSError,ValueError) as exc:
            if isinstance(exc,RoomError):raise
            raise RoomError('backup','Backup or restore refused; existing data was not overwritten.') from exc
        return destination

    def backup(self, destination):
        """Operator-only consistent SQLite backup into this fixture's runtime folder."""
        return self._copy_backup(self.path,destination,self.path.parent)

    @classmethod
    def restore(cls, source, destination, participants, artifact_roots, clock=None):
        """Restore a trusted local fixture backup to a new sibling/subfolder path."""
        source=Path(source).absolute()
        target=cls._copy_backup(source,destination,source.parent)
        return cls(target,participants,artifact_roots,clock=clock)
