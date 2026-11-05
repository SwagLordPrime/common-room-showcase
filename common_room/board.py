"""Authenticated, loopback-only fixture board. No provider or generic file endpoint."""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from http.cookies import SimpleCookie, CookieError
from pathlib import Path
import hashlib, json, secrets, threading, time
from .protocol import RoomError, authenticate, canonical, decode_request
from .store import FIELDS, fold_questions, question_state
from .build_identity import source_identity
from .crew_client import CrewError
from .plans import OwnerRoomClient, board_apply_decisions, board_approve, board_listing
from .needs import needs_shane
from . import chat as chat_rules
WEB=Path(__file__).resolve().parents[1]/'web'
STATIC={'/status':('status.html','text/html; charset=utf-8'),'/static/status.js':('status.js','text/javascript; charset=utf-8'),'/static/recovery.js':('recovery.js','text/javascript; charset=utf-8'),'/':('index.html','text/html; charset=utf-8'),'/static/board.js':('board.js','text/javascript; charset=utf-8'),'/static/board.css':('board.css','text/css; charset=utf-8'),'/static/fonts/oswald-latin-var.woff2':('fonts/oswald-latin-var.woff2','font/woff2'),'/static/fonts/noto-serif-jp-900-hub-subset.woff2':('fonts/noto-serif-jp-900-hub-subset.woff2','font/woff2'),'/static/fonts/share-tech-mono-latin.woff2':('fonts/share-tech-mono-latin.woff2','font/woff2')}
COMMANDS=(set(FIELDS)|{'create'})-{'grant','grant_handoff'}
CSP="default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; font-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
PLAN_ROUTES={'/api/plans','/api/plan-approve','/api/decisions-apply'}
GET_ROUTES={'/api/state','/api/plans','/api/usage'}
KEY_ROUTES={'/api/command','/api/handoff'}
KEY_REFUSAL='Room text may not contain a room key.'

STATE_QUIET_SECONDS=3*86400
STATE_TEXT_LIMIT=300
STATE_DETAIL_LIMIT=200
STATE_CLIP_FIELDS=('body','result_body','acceptance','review_note','failure_note','decision','decision_response')

def bounded_task_views(events,tasks,allowed,now):
    """Copy read-side task rows; persistent fold/log and open tasks keep all bytes."""
    latest={}
    for event in events:
        task_id=event['task_id']
        if task_id in tasks:latest[task_id]=max(latest.get(task_id,event['timestamp']),event['timestamp'])
    quiet={task_id for task_id,task in tasks.items()
           if task['authority_id'] in allowed and task['status'] in {'done','cancelled'}
           and now-latest.get(task_id,now)>STATE_QUIET_SECONDS}
    views=[]
    for task_id,task in tasks.items():
        if task['authority_id'] not in allowed:continue
        view=dict(task)
        if task_id in quiet:
            for field in STATE_CLIP_FIELDS:
                if isinstance(view.get(field),str):view[field]=view[field][:STATE_TEXT_LIMIT]
            view['clipped']=True
        views.append(view)
    return views,quiet


def holds_room_key(value,keys):
    """True when any string anywhere in `value` (object keys included) holds one of `keys`, as written
    or with its whitespace removed (a key wrapped over two lines). Iterative, so depth is no limit."""
    keys=[k for k in keys if k];stack=[value]
    while keys and stack:
        item=stack.pop()
        if isinstance(item,str):
            joined=''.join(item.split())
            if any(k in item or k in joined for k in keys):return True
        elif isinstance(item,dict):stack.extend(item);stack.extend(item.values())
        elif isinstance(item,list):stack.extend(item)
    return False

def refuse_room_keys(server,value):
    """fix-board 2026-09-24 (VERIFY7B L1): the server knows every key it has issued, so no request may
    carry one into room text, whoever sends it (relay, runner, CLI, board) and whichever seat's key it
    is, the owner's included. A retired key is no longer in server.tokens and is ordinary text."""
    with server.lock:keys=list(server.tokens)
    if holds_room_key(value,keys):raise RoomError('forbidden',KEY_REFUSAL)

class LocalServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,store,tokens,port,clock,controller=None,credential_policy=None):
        self.controller=controller;self.credential_policy=credential_policy;self.instance_id=secrets.token_hex(16)
        self.store=store;self.tokens=dict(tokens);self.clock=clock or time.time
        self.started_at=self.clock()
        self.build_identity=source_identity()
        self.sessions={};self.contacts={};self.lock=threading.Lock();self.slots=threading.BoundedSemaphore(32)
        self.plans_root=None;self.plans_lock=threading.Lock()  # set by activation.start_room(plans_root=...)
        self.renewal=None;self.renewal_pending=set()           # A4: set by activation.start_room on a renewed root
        for actor in self.tokens.values():store._actor(actor)
        super().__init__(('127.0.0.1',port),Handler)
        self.host='127.0.0.1:'+str(self.server_address[1]);self.origin='http://'+self.host
    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False):self.shutdown_request(request);return
        try:super().process_request(request,address)
        except BaseException:self.slots.release();raise
    def process_request_thread(self,request,address):
        try:super().process_request_thread(request,address)
        finally:self.slots.release()

class Handler(BaseHTTPRequestHandler):
    server_version='CommonRoom';sys_version='';protocol_version='HTTP/1.0'
    def setup(self):super().setup();self.connection.settimeout(3)
    def log_message(self,*args):pass
    def reply(self,status,value,content_type='application/json; charset=utf-8',cookie=None):
        body=value if isinstance(value,bytes) else canonical(value).encode('utf-8')
        self.send_response(status)
        for name,value in {'Content-Type':content_type,'Content-Length':str(len(body)),'Cache-Control':'no-store','Content-Security-Policy':CSP,'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','X-Frame-Options':'DENY'}.items():self.send_header(name,value)
        if cookie:self.send_header('Set-Cookie',cookie)
        self.end_headers();self.wfile.write(body)
    def guard(self):
        if self.headers.get_all('Host')!=[self.server.host]:raise RoomError('forbidden','Unexpected Host.')
        origins=self.headers.get_all('Origin') or []
        if origins and origins!=[self.server.origin]:raise RoomError('forbidden','Unexpected Origin.')
        if self.command=='POST' and origins!=[self.server.origin]:raise RoomError('forbidden','An exact local Origin is required.')
    def body(self):
        lengths=self.headers.get_all('Content-Length') or []
        if self.headers.get_all('Transfer-Encoding') or len(lengths)!=1 or not lengths[0].isascii() or not lengths[0].isdigit():
            raise RoomError('invalid','An unambiguous Content-Length is required.')
        n=int(lengths[0])
        if n>16384:raise RoomError('too_large','Request exceeds the byte budget.')
        if self.headers.get_all('Content-Type')!=['application/json']:raise RoomError('media','Use application/json.')
        raw=self.rfile.read(n)
        if len(raw)!=n:raise RoomError('invalid','Incomplete request.')
        return decode_request(raw)
    def session(self):
        try:
            headers=self.headers.get_all('Cookie') or []
            if len(headers)!=1:raise CookieError('Missing session')
            jar=SimpleCookie();jar.load(headers[0]);key=jar['room_session'].value
        except (CookieError,KeyError):raise RoomError('unauthenticated','Sign in to the fixture board.')
        with self.server.lock:
            found=self.server.sessions.get(key);now=self.server.clock();window=None
            if self.server.credential_policy:
                window=self.server.credential_policy.session_window(found[2],now) if found and len(found)==3 else None
                if window is None:
                    self.server.sessions.pop(key,None);raise RoomError('unauthenticated','Participant key expired or was revoked.')
                now=window[0]
            if not found or now>=found[1]:
                self.server.sessions.pop(key,None);raise RoomError('unauthenticated','Session expired; sign in again.')
            actor=found[0];self.server.store._actor(actor);self.server.contacts[actor.id]=now
            if window:self.server.sessions[key]=(actor,window[1],found[2])
        return actor,key
    def dispatch(self):
        self.guard()
        if self.command=='GET' and self.path in STATIC:
            name,typ=STATIC[self.path];return self.reply(200,(WEB/name).read_bytes(),typ)
        if self.command=='GET' and self.path=='/api/instance' and self.server.credential_policy:
            return self.reply(200,{'protocol':'common-room-handoffs-v1','instance_id':self.server.instance_id,'build':self.server.build_identity})
        if self.command=='GET' and self.path=='/api/status' and self.server.credential_policy:
            owner=next(a for a in self.server.store.people.values() if a.role=='owner')
            events,_,tasks=self.server.store._snapshot(owner)
            now=self.server.clock();counts={};active=expired=0
            for task in tasks.values():
                status=task['status'];counts[status]=counts.get(status,0)+1
                lease=task.get('lease')
                if lease:
                    if lease['until']>now:active+=1
                    else:expired+=1
            with self.server.lock:sessions=list(self.server.sessions.values())
            live_sessions=sum(1 for value in sessions if value[1]>now and (len(value)<3 or self.server.credential_policy.valid(value[2],now)))
            state_bytes=len(canonical(state_body(self.server,owner)).encode('utf-8'))
            return self.reply(200,{'protocol':'common-room-status-v1','instance_id':self.server.instance_id,
                'server_time':now,'last_event_seq':events[-1]['seq'] if events else 0,'counts':counts,
                'active_claims':active,'expired_claims':expired,'state_bytes':state_bytes,'live_sessions':live_sessions,'build':self.server.build_identity})
        routes={'/api/login','/api/logout','/api/state','/api/usage','/api/command','/api/key-check'}|PLAN_ROUTES
        if self.server.controller:routes.add('/api/run')
        if self.server.credential_policy:routes.add('/api/handoff')
        if self.path not in routes:return self.reply(404,{'error':'not_found'})
        if (self.command=='GET')!=(self.path in GET_ROUTES):return self.reply(405,{'error':'method'})
        if self.path=='/api/login':
            p=self.body()
            if set(p)!={'token'}:raise RoomError('invalid','Only a fixture access key is accepted.')
            actor=authenticate(p['token'],self.server.tokens);now=self.server.clock()
            if self.server.credential_policy and not self.server.credential_policy.valid(p['token'],now):raise RoomError('unauthenticated','Participant key expired or was revoked.')
            with self.server.lock:
                self.server.sessions={k:v for k,v in self.server.sessions.items() if v[1]>now}
                if len(self.server.sessions)>=256:return self.reply(429,{'error':'session_limit'})
                window=self.server.credential_policy.session_window(p['token'],now) if self.server.credential_policy else None
                if self.server.credential_policy and window is None:raise RoomError('unauthenticated','Participant key expired or was revoked.')
                key=secrets.token_urlsafe(32);self.server.sessions[key]=(actor,window[1],p['token']) if window else (actor,now+900);self.server.contacts[actor.id]=window[0] if window else now
            return self.reply(200,{'actor':actor.id},cookie='room_session='+key+'; Path=/; HttpOnly; SameSite=Strict')
        actor,key=self.session()
        if self.path=='/api/state':return self.reply(200,state_body(self.server,actor))
        if self.path=='/api/usage':return self.reply(200,usage_body(self.server,actor))
        if self.path in PLAN_ROUTES:return self.reply(200,self.plans(actor))
        p=self.body()
        if self.path=='/api/key-check':return self.reply(200,self.key_check(actor,p))
        if self.path in KEY_ROUTES:refuse_room_keys(self.server,p)
        if self.path=='/api/handoff':
            if set(p)!={'operation','payload'}:raise RoomError('invalid','Exact handoff envelope required.')
            from .handoff_api import handoff_operation
            return self.reply(200,handoff_operation(self.server,actor,p['operation'],p['payload']))
        if self.path=='/api/run':return self.reply(200,self.server.controller.submit(actor,p))
        if self.path=='/api/logout':
            if p:raise RoomError('invalid','Logout has no fields.')
            with self.server.lock:self.server.sessions.pop(key,None)
            return self.reply(200,{'ok':True},cookie='room_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict')
        if set(p)!={'action','payload','request_id'}:raise RoomError('invalid','Command envelope fields must match.')
        chat_on=getattr(self.server.store,'chat',None) is not None   # A3: the chat commands exist only while ON
        if type(p['action']) is not str or (p['action'] not in COMMANDS and not (chat_on and p['action'] in chat_rules.ACTIONS)):raise RoomError('forbidden','This board does not expose that action.')
        result=self.server.store.execute(actor,p['action'],p['payload'],p['request_id'])
        # A4: the seat's first command on a renewed instance clears its renewal note (it was in every state it read until
        # then). The reply itself is unchanged, so a retried request is still answered byte for byte from its receipt.
        # A1's automatic read mark (question_mark, sent by a watch or relay for the seat) is not the seat's own call.
        if p['action']!='question_mark':renewal_clear(self.server,actor)
        return self.reply(200,result)
    def plans(self,actor):
        """Owner only, every other role 403: the plans folder listing, plan approval and decisions,
        through plans.py -- the same implementation, request ids and receipts as the CLI."""
        p=self.body() if self.command=='POST' else None
        if actor.role!='owner':raise RoomError('forbidden','Only the room owner may review or approve plans.')
        client=OwnerRoomClient(lambda action,payload,request_id:self.owner_execute(actor,action,payload,request_id),lambda:state_body(self.server,actor),
                               lambda value:refuse_room_keys(self.server,value))
        try:
            with self.server.plans_lock:
                if self.path=='/api/plans':return board_listing(self.server.plans_root,client.state())
                if self.path=='/api/plan-approve':return board_approve(self.server.plans_root,p,client)
                return board_apply_decisions(self.server.plans_root,p,client)
        except CrewError as exc:raise RoomError(exc.code,str(exc)) from exc
    def key_check(self,actor,p):
        """fix2-board (VERIFY8A N3): POST /api/key-check {text}: the owner's CLI asks the room, which alone
        holds every key, whether a plan holds one, before it sends any of it. Owner only; writes nothing."""
        if actor.role!='owner':raise RoomError('forbidden','Only the room owner may check plan text for room keys.')
        if set(p)!={'text'}:raise RoomError('invalid','Send exactly: text.')
        refuse_room_keys(self.server,p['text'])
        return {'ok':True}
    def owner_execute(self,actor,action,payload,request_id):
        """A board approval reaches the store without /api/command: the same room key rule applies."""
        refuse_room_keys(self.server,{'action':action,'payload':payload,'request_id':request_id})
        return self.server.store.execute(actor,action,payload,request_id)
    def handle_request(self):
        try:self.dispatch()
        except RoomError as exc:
            status={'unauthenticated':401,'forbidden':403,'too_large':413,'media':415,'idempotency':409,'lease':409,'state':409,'claimed':409,'budget':409,'held':409,'revoked':409}.get(exc.code,400)
            self.reply(status,{'error':exc.code,'message':str(exc)})
        except (BrokenPipeError,ConnectionResetError,TimeoutError):pass
        except Exception:self.reply(500,{'error':'internal','message':'Request could not be completed. Retry with the same request key.'})
    do_GET=handle_request
    do_POST=handle_request

def history_marks(event):
    """Extra keys for one board history row: the review kind, the paperwork count and any refund the
    event recorded (cr-upgrade, core-strikes). Rows from old events get no extra keys at all."""
    changes=event['payload'].get('changes',{});marks={}
    if 'review_kind' in changes:marks['review_kind']=changes['review_kind']
    if 'paperwork_returns' in changes:marks['paperwork_returns']=changes['paperwork_returns']
    if changes.get('refunds'):marks['refund']=changes['refunds'][-1]
    return marks

QUESTION_VIEW_LIMIT=50

def question_views(questions,actor_id,allowed,side='to'):
    """A1: side 'to' is this actor's inbox, the open questions addressed to it ('questions' in state and relay-state);
    side 'from' is its outbox, the open questions it asked, with their marks so it can check them ('asked'). A question
    addressed to one seat therefore enters that seat's inbox and no other seat's; the asker sees it only in its own
    outbox. A checked question is settled and leaves both. At most QUESTION_VIEW_LIMIT each, newest last; in the inbox
    the unread ones are kept first, so a busy night cannot push an unread question out of view."""
    rows=[dict(q,state=question_state(q)) for seq,q in sorted(questions.items())
          if q['authority_id'] in allowed and q[side]==actor_id and q['marks']['checked'] is None]
    unread=[r for r in rows if side=='to' and r['marks']['read'] is None][-QUESTION_VIEW_LIMIT:]
    kept={r['seq'] for r in unread}
    rest=[r for r in rows if r['seq'] not in kept][-(QUESTION_VIEW_LIMIT-len(unread)):] if len(unread)<QUESTION_VIEW_LIMIT else []
    return sorted(unread+rest,key=lambda r:r['seq'])

RENEWAL_OPEN=('delivered','acknowledged','claimed','waiting_decision','ready_for_review')

def _plan_ids(server,tasks,actor_id):
    """The plan ids behind this seat's open work: each open task's root task carries the intake external reference
    `<plan_id>-<key>` that plan approval gave it (plans.plan_external_reference). The plan id is the longest plan folder
    name in the owner's plans folder that prefixes it; a room with no plans folder, or a root no plan made, gives none."""
    names=[]
    root=getattr(server,'plans_root',None)
    if root is not None:
        try:
            from .plans import PLAN_ID_RE
            names=sorted((p.name for p in Path(root).iterdir() if p.is_dir() and not p.is_symlink() and PLAN_ID_RE.fullmatch(p.name)),key=len,reverse=True)
        except OSError:names=[]
    found=set()
    for task in tasks.values():
        if actor_id not in (task.get('recipient'),task.get('builder')) or task['status'] not in RENEWAL_OPEN:continue
        root_task=task;seen=set()
        while root_task.get('parent_id') in tasks and root_task['id'] not in seen:
            seen.add(root_task['id']);root_task=tasks[root_task['parent_id']]
        reference=(root_task.get('intake') or {}).get('external_reference') or ''
        match=next((n for n in names if reference.startswith(n+'-')),None)
        if match:found.add(match)
    return sorted(found)

def renewal_note(server,actor,authorities,tasks):
    """A4: what a seat is told once after the room starts a new key cycle (a renewal or a restart on the same root).
    Its crew tools and relay already re-read its own credential file and carry on; this only points it to its current
    join card. Never a key: instance ids, times, scope ids, plan ids and a command name only."""
    renewal=getattr(server,'renewal',None)
    if renewal is None or actor.id not in getattr(server,'renewal_pending',()):return None
    scopes=sorted(a for a,auth in authorities.items() if actor.id in auth['members'] and not auth['revoked'])
    plans=_plan_ids(server,tasks,actor.id)
    until=time.strftime('%Y-%m-%d %H:%M UTC%z',time.localtime(renewal['expires_at']))   # the room host's own clock
    return dict(renewal,scopes=scopes,plans=plans,card='crew card '+actor.id,
                note=('The room started a new key cycle (instance '+renewal['instance_id'][:8]+', keys valid until '+until+
                      '). Your new key is already in effect: your crew tools re-read your own credential file, so there is '
                      'nothing to do. Scope '+(', '.join(scopes) or 'none')+'; plan '+(', '.join(plans) or 'not known to the room')+
                      '. Your current join card: crew card '+actor.id+' (a relay seat asks '+
                      ('Shane' if actor.id=='claude-room' else 'claude-room or Shane')+' to run it).'))

def renewal_clear(server,actor):
    """A4: the seat's first command on this instance means it has carried on with the new key: its note is done."""
    if getattr(server,'renewal',None) is not None:
        with server.lock:server.renewal_pending.discard(actor.id)

def state_body(server,actor):
    """The signed-in actor's /api/state body; the board's plan endpoints reuse it as the owner's state()."""
    events,authorities,tasks=server.store._snapshot(actor)
    allowed={id for id,a in authorities.items() if actor.id in a['members']}
    pair=getattr(server.store,'chat',None)   # A3: None (OFF) leaves this body exactly as the base head built it
    hide_chat=chat_rules.hidden_from(pair,actor)   # ruling 1: chat rows reach only the pair and keeper
    members={id for aid in allowed for id in authorities[aid]['members']}
    now=server.clock()
    with server.lock:contact=dict(server.contacts)
    task_views,quiet=bounded_task_views(events,tasks,allowed,now)
    labels=getattr(server.store,'seat_tiers',{}) or {}
    for view in task_views:   # A4: shown, never refused: a tagged task on a seat labelled otherwise
        label=labels.get(view.get('recipient'))
        if view.get('tier') and label and label!=view['tier']:view['tier_mismatch']=label
    people=[{'id':id,'role':server.store.people[id].role,'contact_age_s':None if id not in contact else max(0,int(now-contact[id])),'contact':('No fixture contact' if id not in contact else 'Fixture browser active' if now-contact[id]<=60 else 'Fixture contact stale')} for id in sorted(members)]
    for person in people:   # A4: each seat's own tier label, when the room was given one
        if person['id'] in labels:person['tier']=labels[person['id']]
    if server.credential_policy:
        for person in people:
            issued=any(a.id==person['id'] and server.credential_policy.valid(k,now) for k,a in server.tokens.items())
            person['key_active']=issued  # turnons panels: the Needs Shane list reads this, not the contact wording
            # A4: an idle seat whose key is fine used to read "stale or key inactive"; the two cases are now told apart.
            person['contact']=('Key issued; handshake pending' if issued else 'No active key') if person['id'] not in contact else ('Credential contact active; assistant identity unverified' if issued and now-contact[person['id']]<=60 else 'Credential contact stale; key active' if issued else 'Key inactive since last contact')
            declaration=getattr(server,'enrollment',{}).get(person['id'])
            if declaration:person['label']=declaration['label'];person['session']=declaration['session']
            person['legacy']=bool(getattr(server,'enrollment',{})) and not declaration and person['id']!='keeper'
            if person['legacy']:person['label']=person['id']+' (historical identity)'
    connection=server.controller.state(actor,{t['id'] for t in tasks.values() if t['authority_id'] in allowed}) if server.controller else None
    if connection and connection['mode']=='codex':
        for person in people:
            role='worker' if person['id']==connection['worker'] else 'reviewer' if person['id']==connection['reviewer'] else None
            person['contact']=('Provider result recorded' if any(r['role']==role and r.get('provider_run_id') for r in connection['recent']) else 'Configured; no successful provider result recorded') if role else ('Browser active; not a provider connection' if person['id'] in contact and now-contact[person['id']]<=60 else 'No recent browser contact')
    body={'mode':'handoff' if server.credential_policy else 'default','connection':connection,'instance_id':server.instance_id,'actor':{'id':actor.id,'role':actor.role},'fixture_only':not server.controller or server.controller.dispatcher.transport.fixture_only,'server_time':now,'history':[{'seq':e['seq'],'task_id':e['task_id'],'kind':e['kind'],'actor':e['actor'],'timestamp':e['timestamp'],'detail':next((e['payload'].get('changes',{}).get(k) for k in ('decision','decision_response','review_note','failure_note','result_body') if e['payload'].get('changes',{}).get(k)),''),'sha256':hashlib.sha256(canonical(e).encode('utf-8')).hexdigest(),**history_marks(e)} for e in events if e['task_id'] in tasks and tasks[e['task_id']]['authority_id'] in allowed and e['kind']!='task_renew'],'stop':next(({'active':True,**{k:a['room_stopped'][k] for k in ('by','at','reason')}} for id,a in authorities.items() if id in allowed and not a['revoked'] and 'room_stopped' in a),{'active':False}),'tasks':task_views,
        'authorities':[dict({k:a[k] for k in ['id','owner','scope','paused','revoked','capabilities']},source_namespaces=a.get('source_namespaces',[]),members=list(a['members']),
            # A2 D10: the review stand-ins this room was given for the scope, so every seat can see dot's grant
            review_standins=[dict(e) for (aid,_),e in sorted(getattr(server.store,'standins',{}).items()) if aid==id]) for id,a in authorities.items() if id in allowed],
        'participants':people,'notes':[{'sender':e['actor'],'authority_id':e['payload']['authority_id'],'body':e['payload']['body'],'seq':e['seq'],'timestamp':e['timestamp'],'sha256':hashlib.sha256(canonical(e).encode('utf-8')).hexdigest()} for e in events if e['kind']=='note' and e['payload']['authority_id'] in allowed and not (hide_chat and 'chat' in e['payload'])][-100:]}
    body['uptime_s']=max(0,int(now-getattr(server,'started_at',now)))
    questions=fold_questions(events)
    body['questions']=question_views(questions,actor.id,allowed,'to')
    body['asked']=question_views(questions,actor.id,allowed,'from')
    renewed=renewal_note(server,actor,authorities,tasks)   # A4: present only for a seat not yet told of a new key cycle
    if renewed is not None:body['room_renewed']=renewed
    # S0 F4: needs reads #seq citations in history details, so it is worked out before those details are clipped.
    # A1: needs also learns which notes are questions addressed to a seat, from the events, not from this body.
    body['needs']=needs_shane(body,addressed={seq:q['to'] for seq,q in questions.items()})  # turnons panels: everything waiting on Shane, from this same view (common_room/needs.py)
    for row in body['history']:
        if row['task_id'] in quiet and isinstance(row['detail'],str):row['detail']=row['detail'][:STATE_DETAIL_LIMIT]
    if pair is not None and not hide_chat and pair['scope'] in allowed:body['chat']=chat_rules.state_view(events,pair,actor,now)   # A3: ON only
    return body

USAGE_WEEK=7*86400

def usage_totals(rows):
    """Runs, seconds and tokens for some usage lines; tokens stay None when no line reported any (headless jobs)."""
    total={'runs':len(rows),'seconds':round(sum(r['seconds'] for r in rows),1),'prompt_tokens':None,'output_tokens':None}
    for name in ('prompt_tokens','output_tokens'):
        known=[r[name] for r in rows if r.get(name) is not None]
        if known:total[name]=sum(known)
    return total

def usage_summary(rows,now,today_start):
    """Per seat: today (since Central midnight) and the last 7 days. Numbers and seat/model names only."""
    seats={}
    for r in rows:
        at=usage_epoch(r['ts'])
        if at>=now-USAGE_WEEK:seats.setdefault(r['seat'],[]).append((at,r))
    return [{'seat':seat,'kind':mine[-1][1]['kind'],'today':usage_totals([r for at,r in mine if at>=today_start]),
             'week':usage_totals([r for at,r in mine])} for seat,mine in sorted(seats.items())]

def usage_epoch(ts):
    from datetime import datetime,timezone
    return datetime.strptime(ts,'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()

def usage_body(server,actor):
    """GET /api/usage, owner only: the usage panel, read from <room runtime>/usage/*.jsonl (USAGE_FORMAT.md) beside
    room.sqlite. Read-only; a damaged line is counted in `skipped`, never shown (its text could hold anything)."""
    if actor.role!='owner':raise RoomError('forbidden','Only the room owner sees the usage panel.')
    from datetime import datetime,time as wall
    from .ollama_worker import CentralTime
    from .usage import read_usage
    now=server.clock();central=CentralTime()
    today_start=central.epoch(datetime.combine(central.local(now).date(),wall.min))
    problems=[];rows=read_usage(Path(server.store.path).parent,since=min(now-USAGE_WEEK,today_start),problems=problems)
    return {'server_time':now,'today_start':today_start,'seats':usage_summary(rows,now,today_start),'skipped':len(problems)}

def make_server(store,tokens,port=0,clock=None,controller=None,credential_policy=None):
    if type(port) is not int or not 0<=port<=65535:raise RoomError('config','Invalid local port.')
    return LocalServer(store,tokens,port,clock,controller,credential_policy)
