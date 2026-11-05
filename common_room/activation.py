"""Explicit owner-operated browser enrollment; loopback only, no model dispatch."""
from pathlib import Path
import argparse, hashlib, json, os, re, secrets, sys, threading, time, uuid
from .protocol import Actor, RoomError, canonical, decode_request
from .store import Store, bounded_id, review_standins, text
from .handoff_room import ROOT, open_handoff_room
from .handoff_api import CredentialPolicy
from .board import make_server
from . import keeper_vault
from .plans import resolve_plans_root
from . import chat as chat_rules

LEGACY={'keeper','astra','reviewer','claude-estate'}
EMPTY={'version':1,'actors':[],'authorities':[]}

def safe_path(root,path):
    root=Path(root).absolute();p=Path(path).absolute()
    if '..' in root.parts or '..' in p.parts or not p.is_relative_to(root) or not p.resolve().is_relative_to(root.resolve()):raise ValueError('Path outside the room.')
    for part in [p,*p.parents]:
        if part.is_symlink() or part.is_junction():raise ValueError('Linked room path refused.')
        if part.exists() and part.is_file() and part.stat().st_nlink!=1:raise ValueError('Shared file refused.')
        if part==root:break
    return p

def read_json(root,name,default):
    p=safe_path(root,Path(root)/name)
    if not p.exists():return json.loads(canonical(default))
    data=p.read_bytes()
    if len(data)>16384:raise ValueError('Configuration exceeds 16 KiB.')
    return decode_request(data)

def write_json(root,name,value):
    target=safe_path(root,Path(root)/name)
    temporary=safe_path(root,target.with_name(target.name+'.'+uuid.uuid4().hex+'.tmp'))
    try:
        with temporary.open('x',encoding='utf-8',newline='\n') as f:
            f.write(canonical(value));f.flush();os.fsync(f.fileno())
        safe_path(root,target);os.replace(temporary,target)
    finally:
        if temporary.exists():temporary.unlink()

def validate_enrollment(value):
    if type(value) is not dict or set(value)!={'version','actors','authorities'} or type(value['version']) is not int or value['version']!=1:raise ValueError('Invalid enrollment schema.')
    if type(value['actors']) is not list or len(value['actors'])>24 or type(value['authorities']) is not list or len(value['authorities'])>24:raise ValueError('Enrollment is too large.')
    ids=set(LEGACY);aids={'handoffs'}
    for row in value['actors']:
        if type(row) is not dict or set(row)!={'id','role','label','session'}:raise ValueError('Exact participant fields required.')
        actor=Actor(row['id'],row['role'])
        if actor.id in ids or actor.role not in {'worker','reviewer','observer'}:raise ValueError('Duplicate or owner enrollment refused.')
        text(row['label'],100);text(row['session'],200);ids.add(actor.id)
    for row in value['authorities']:
        if type(row) is not dict or set(row)!={'id','members','scope'}:raise ValueError('Exact scope fields required; filesystem roots are derived.')
        aid=bounded_id(row['id'],max_length=32)
        if aid in aids or aid.lower() in {x.lower() for x in aids}:raise ValueError('Duplicate scope.')
        aids.add(aid);text(row['scope'],2000)
        members=row['members']
        if type(members) is not list or not 1<=len(members)<=32 or any(type(x) is not str or x not in ids for x in members) or len(set(members))!=len(members) or 'keeper' not in members:raise ValueError('Scope needs registered members and keeper.')
    if len(canonical(value).encode())>16384:raise ValueError('Enrollment exceeds 16 KiB.')
    return json.loads(canonical(value))

def grown_scope(old,new):
    """True when scope row `new` is `old` with members appended only (seat-dot, ruling #1136): the same id
    and scope text, every old member first and in the same order, at least one new member after them."""
    return (new is not None and new['id']==old['id'] and new['scope']==old['scope'] and
            len(new['members'])>len(old['members']) and new['members'][:len(old['members'])]==old['members'])

STANDINS_FILE=ROOT/'crew'/'review-standins.json'   # A2 D10: shipped with the tree, read only for the live room

def load_standins(path):
    """The review stand-ins in `path` ({'version':1,'review_standins':[...]}), or [] when the file is absent."""
    path=Path(path)
    try:data=decode_request(path.read_bytes())
    except FileNotFoundError:return []
    except OSError as exc:raise ValueError('The review stand-ins file could not be read: '+str(path)+' ('+type(exc).__name__+').') from exc
    if type(data) is not dict or set(data)!={'version','review_standins'} or data['version']!=1:raise ValueError('Invalid review stand-ins file.')
    return data['review_standins']

def check_standins(standins,config,people):
    """A2 D10: the stand-ins the store will enforce, each checked against this room's enrollment: every seat it names
    is enrolled with the right role (store.review_standins) and is a member of the stand-in's scope."""
    try:found=review_standins(list(standins),people)
    except RoomError as exc:raise ValueError(str(exc)) from exc
    scopes={row['id']:row['members'] for row in config['authorities']}
    for (aid,actor),e in found.items():
        if aid not in scopes or any(x not in scopes[aid] for x in [actor,e['stands_for']]+e['builders']+e['markers']):
            raise ValueError('A review stand-in and every seat it names must be members of its scope ('+aid+').')
    return [found[k] for k in found]

SEAT_TIERS_FILE=ROOT/'crew'/'seat-tiers.json'   # A4: written at the switch, never in git; read only for the live room
CHAT_PAIR_FILE=ROOT/chat_rules.FLAG_PATH   # A3: absent (OFF) by default, never in git; read only for the live room, at start

def load_seat_tiers(path):
    """A4: the seat tier labels in `path` ({'version':1,'seat_tiers':{seat:tier}}), or None when the file is absent."""
    path=Path(path)
    try:raw=path.read_bytes()
    except FileNotFoundError:return None
    except OSError as exc:raise ValueError('The seat tier file could not be read: '+str(path)+' ('+type(exc).__name__+').') from exc
    try:data=decode_request(raw)
    except RoomError as exc:raise ValueError('The seat tier file is not valid JSON: '+str(path)+' ('+str(exc)+')') from exc
    if type(data) is not dict or set(data)!={'version','seat_tiers'} or data['version']!=1 or type(data['seat_tiers']) is not dict:
        raise ValueError('Invalid seat tier file: '+str(path)+' (expected {"version": 1, "seat_tiers": {seat: tier}}).')
    return data['seat_tiers']

def merge_enrollment(old,new):
    old=validate_enrollment(old);new=validate_enrollment(new)
    for field in ['actors','authorities']:
        rows={x['id']:x for x in new[field]}
        for x in old[field]:
            y=rows.get(x['id'])
            if y==x or (field=='authorities' and grown_scope(x,y)):continue   # a scope may only gain members
            raise ValueError('Existing identities and grants are immutable; append a new one.')
    return new

class PersistentPolicy(CredentialPolicy):
    def __init__(self,tokens,expires,clock,room):
        super().__init__(tokens,expires,clock);self.room=room
    def revoke(self,actor_id):
        with self.lock:
            updated=dict(self.room.settings)
            updated['revoked']=sorted(set(updated['revoked'])|{actor_id})
            write_json(self.room.root,'activation-state.json',updated);self.room.settings=updated
            super().revoke(actor_id)
    def issue(self,token,actor):
        with self.lock:
            self.records[hashlib.sha256(token.encode()).hexdigest()]={'actor':actor.id,'expires':self.room.expiry,'revoked':False}

class ActivationRoom:
    def close(self):
        if getattr(self,'closed',False):return
        self.closed=True
        if getattr(self,'thread',None):
            self.server.shutdown();self.thread.join(3)
        if getattr(self,'server',None):self.server.server_close()
        if getattr(self,'lock_file',None):self.lock_file.close()
    def rotate(self,actor_id):
        if actor_id not in self.credentials:raise ValueError('Rotate only an issued participant; new enrollment requires explicit restart selection.')
        if self.server.clock()>=self.expiry:raise ValueError('Room keys expired; restart to issue fresh keys.')
        with self.server.lock:
            self.server.credential_policy.revoke(actor_id)
            token=secrets.token_urlsafe(32);actor=self.store.people[actor_id]
            config=self.config_for(actor_id,token)
            self.publish(actor_id,config)
            updated=dict(self.settings);updated['revoked']=[x for x in updated['revoked'] if x!=actor_id]
            write_json(self.root,'activation-state.json',updated);self.settings=updated
            self.server.credential_policy.issue(token,actor)
            self.server.tokens={k:v for k,v in self.server.tokens.items() if v.id!=actor_id}
            self.server.tokens[token]=actor;self.credentials[actor_id]=config
    def config_for(self,actor_id,token):
        return {'origin':self.server.origin,'token':token,'actor_id':actor_id,'expires_at':self.expiry}
    def publish(self,actor_id,config):
        """Write one seat's key file; with a keeper store the owner key goes to the store instead."""
        if actor_id=='keeper' and getattr(self,'keeper_store',None) is not None:self.keeper_store_status=store_keeper(self.root,self.keeper_store,config)
        else:write_json(self.root,actor_id+'-credential.json',config)

def store_keeper(root,store,config):
    """Owner key to the vault and a stub with no key to keeper-credential.json. If the vault
    refuses, the key goes to the file as before and the reason is recorded, never hidden.
    keeper-store.json is the non-secret record of which it was."""
    reason=None
    try:store.write(config)
    except keeper_vault.VaultError as exc:reason=str(exc).rstrip('.')[:300]
    except OSError as exc:reason=(type(exc).__name__+': '+str(exc)).rstrip('.')[:300]
    write_json(root,'keeper-credential.json',keeper_vault.stub_for(store.target) if reason is None else config)
    status={'version':1,'store':store.kind,'target':store.target,'fallback_reason':reason}
    write_json(root,'keeper-store.json',status)
    return status

STORE_NAMES={'wincred':'Windows Credential Manager','unknown':'a key store (its record here was unreadable)'}

def downgraded_from(root):
    """Which key store an earlier start used, when this file-mode start is putting the owner key back in
    the folder: the last record's store, or the downgrade it already carried; None when no start did."""
    try:previous=read_json(root,'keeper-store.json',None)
    except (ValueError,OSError):return 'unknown'
    if type(previous) is not dict:return 'unknown'
    kind=previous.get('downgraded_from') or previous.get('store')
    if kind=='file':return None
    return kind if type(kind) is str and re.fullmatch('[a-z][a-z0-9_-]{0,31}',kind) else 'unknown'

def downgrade_warning(kind,root):
    """fix2-board (VERIFY8A INFO): "was set to keep", which is true also when that start's vault write fell
    back to the file, and the one file to delete when the vault was turned off on purpose."""
    return ('WARNING: the owner key is back in keeper-credential.json in this folder: this room was started without the '
            'owner key vault, and an earlier start was set to keep the key in '+STORE_NAMES.get(kind,'the '+kind+' key store')+
            '. To keep it out of the folder, close this room and start it with Start Common Room Crew.cmd. If you turned '
            'the vault off on purpose, delete '+str(Path(root)/'keeper-store.json')+' and this warning stops.')

def check_lifetime(lifetime):
    """Keys stay hard-capped at eight hours; a shorter life is allowed, a longer one never."""
    if type(lifetime) is not int or not 60<=lifetime<=28800:raise ValueError('Key lifetime must be a whole number of seconds from 60 through 28800.')
    return lifetime

def check_margin(margin,lifetime):
    check_lifetime(lifetime)
    if type(margin) is not int or not 30<=margin<lifetime-30:raise ValueError('Renewal margin must be a whole number of seconds, at least 30 and less than the key lifetime minus 30.')
    return margin

def start_room(root,enrollment=None,selected=(),clock=None,lifetime=28800,keeper_store=None,plans_root=None,review_standins=(),seat_tiers=None,chat_pair=None):
    check_lifetime(lifetime)
    chat_pair=chat_rules.pair_value(chat_pair)   # A3: None (OFF) or the pair; the live root's value comes from chat_rules.chat_pair
    plans_root=resolve_plans_root(plans_root)  # the owner's plans folder for the board, or None; a linked one is refused
    root=Path(root).absolute()
    if not any(root.resolve().is_relative_to((ROOT/n).resolve()) and root.resolve()!=(ROOT/n).resolve() for n in ('.runtime','.checks')):raise ValueError('Use a child of package runtime or checks.')
    safe_path(ROOT,root);root.mkdir(parents=True,exist_ok=True)
    room=ActivationRoom();room.root=root;room.closed=False;room.keeper_store=keeper_store;room.keeper_store_status=None;room.keeper_downgraded_from=None
    try:
        lock=safe_path(root,root/'.launcher.lock')
        room.lock_file=open(lock,'a+b');room.lock_file.seek(0)
        if os.name=='nt':
            import msvcrt
            if lock.stat().st_size==0:room.lock_file.write(b'0');room.lock_file.flush();room.lock_file.seek(0)
            msvcrt.locking(room.lock_file.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(room.lock_file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        old=validate_enrollment(read_json(root,'enrollment.json',EMPTY))
        config=merge_enrollment(old,enrollment) if enrollment is not None else old
        people_ids=LEGACY|{x['id'] for x in config['actors']}
        if type(selected) not in (list,tuple) or any(type(x) is not str or x not in people_ids or x=='keeper' for x in selected) or len(set(selected))!=len(selected):raise ValueError('Select distinct registered non-owner participants.')
        settings=read_json(root,'activation-state.json',{'version':1,'port':0,'revoked':[]})
        if set(settings)!={'version','port','revoked'} or settings['version']!=1 or type(settings['port']) is not int or not 0<=settings['port']<=65535 or type(settings['revoked']) is not list or any(type(x) is not str or x not in people_ids for x in settings['revoked']):raise ValueError('Invalid activation state.')
        room.settings=settings
        # Preflight all derived roots before creating any new grant.
        roots={'handoffs':safe_path(root,root/'artifacts')}
        for a in config['authorities']:roots[a['id']]=safe_path(root,root/'scopes'/a['id'])
        for path in roots.values():
            if os.name=='nt' and len(str(path/'results'/('0'*48+'.txt')))>=260:raise ValueError('Room path too deep for Windows results.')
        for actor in people_ids:safe_path(root,root/(actor+'-credential.json'))
        for name in ['connection.json','activation-state.json','enrollment.json','participant-selection.json','keeper-store.json']:safe_path(root,root/name)
        _,people=open_handoff_room(root,activation=True)
        people.update({a['id']:Actor(a['id'],a['role']) for a in config['actors']})
        for path in roots.values():path.mkdir(parents=True,exist_ok=True)
        standins=check_standins(review_standins,config,people)   # A2 D10: its own error, before the store opens
        chat_rules.check_enrollment(chat_pair,config)   # A3: a pair this enrollment cannot carry stops the start
        try:room.store=Store(root/'room.sqlite',people,roots,clock=clock,standins=standins,seat_tiers=seat_tiers,**({'chat':chat_pair} if chat_pair is not None else {}))
        except RoomError as exc:
            if exc.code!='config' or seat_tiers is None:raise
            raise ValueError(str(exc)) from exc   # A4: a bad seat tier file stops the start, like a bad enrollment
        now=(clock or time.time)();room.expiry=now+lifetime
        active=['keeper']+[x for x in selected if x not in settings['revoked']]
        if 'keeper' in settings['revoked']:raise ValueError('Owner revoked; explicit owner recovery required.')
        tokens={secrets.token_urlsafe(32):people[x] for x in active}
        policy=PersistentPolicy(tokens,room.expiry,clock or time.time,room)
        room.server=make_server(room.store,tokens,port=settings['port'],clock=clock,credential_policy=policy)
        room.server.plans_root=plans_root
        # A bind conflict leaves published endpoint and credentials untouched.
        granted=room.store._snapshot(people['keeper'])[1]
        for a in config['authorities']:
            have=granted.get(a['id'])
            if have is not None:
                # seat-dot (ruling #1136): a granted scope is never granted again (its first grant's receipt holds
                # the members it started with). Unchanged: nothing to do. Grown: append the new members; the
                # request id names the full new list, so a repeat is a no-op. Anything else is refused.
                if have['scope']!=a['scope'] or len(a['members'])<len(have['members']) or a['members'][:len(have['members'])]!=have['members']:raise ValueError('The room\'s scope '+a['id']+' differs from the enrollment; a scope may only gain members, appended after the ones it has.')
                if len(a['members'])>len(have['members']):
                    digest=hashlib.sha256(canonical(a['members']).encode('utf-8')).hexdigest()[:24]
                    room.store.add_members(people['keeper'],a['id'],a['members'][len(have['members']):],'activation-members-'+a['id']+'-'+digest)
                continue
            room.store.execute(people['keeper'],'grant_handoff',{'id':a['id'],'members':a['members'],'capabilities':['handoff.record'],'root_id':a['id'],'scope':a['scope'],'source_namespaces':['estate']},'activation-grant-'+a['id'])
        room.descriptor={'version':1,'origin':room.server.origin,'instance_id':uuid.uuid4().hex,'started_at':now,'expires_at':room.expiry,'build':room.server.build_identity}
        room.server.instance_id=room.descriptor['instance_id'];room.server.enrollment={a['id']:a for a in config['actors']}
        # A4 (2026-10-24): a start on a root an earlier instance ran on (a renewal, or a restart) has issued every seat a
        # new key. Each seat gets one short note about it, held in memory only and cleared by its first command here.
        try:previous=read_json(root,'connection.json',{}).get('instance_id')
        except (ValueError,OSError,AttributeError):previous=None   # an unreadable old descriptor only means no note
        if type(previous) is str and previous!=room.descriptor['instance_id']:
            room.server.renewal={'previous_instance_id':previous,'instance_id':room.descriptor['instance_id'],
                                 'started_at':now,'expires_at':room.expiry}
            room.server.renewal_pending=set(active)
        settings['port']=room.server.server_address[1]
        write_json(root,'enrollment.json',config);write_json(root,'activation-state.json',settings)
        room.credentials={}
        for token,actor in tokens.items():
            c=room.config_for(actor.id,token);room.publish(actor.id,c);room.credentials[actor.id]=c
        # A file-store start after a vault run must not leave a status file claiming the vault. It still
        # starts (fix-board L3), but records and says loudly that the owner key is back in the folder.
        if keeper_store is None and (root/'keeper-store.json').exists():
            status={'version':1,'store':'file','target':None,'fallback_reason':None}
            room.keeper_downgraded_from=downgraded_from(root)
            if room.keeper_downgraded_from:status['downgraded_from']=room.keeper_downgraded_from
            write_json(root,'keeper-store.json',status)
        write_json(root,'participant-selection.json',{'version':1,'participants':list(selected)})
        write_json(root,'connection.json',room.descriptor)
        room.thread=threading.Thread(target=room.server.serve_forever,daemon=True);room.thread.start()
        return room
    except BaseException:
        room.close();raise

RENEW_RETRIES=5;RENEW_RETRY_WAIT=2.0

def append_line(root,name,value):
    """Append one canonical JSON line to a room file, with the same path and link checks as write_json."""
    target=safe_path(root,Path(root)/name)
    with target.open('a',encoding='utf-8',newline='\n') as f:
        f.write(canonical(value)+'\n');f.flush();os.fsync(f.fileno())

def owner_line(room):
    """Where the owner key is: the owner login file, the vault, or, loudly, the file the vault fell back to. Never a key."""
    status=getattr(room,'keeper_store_status',None);login='Owner login file: '+str(room.root/'keeper-credential.json')
    if status is None:
        downgraded=getattr(room,'keeper_downgraded_from',None)
        return downgrade_warning(downgraded,room.root)+'\n'+login if downgraded else login
    if status['fallback_reason'] is None:return 'Owner key: Windows Credential Manager (sign in to the board with: crew-shane login)'
    return 'WARNING: the owner key could not be stored in Windows Credential Manager ('+status['fallback_reason']+'); it is in keeper-credential.json as before.\n'+login

def room_lines(room):
    """The safe lines run_console prints for a headless room: origin, where the owner key is, issued seat ids. Never a key."""
    return ['Common Room: '+room.server.origin+'\n'+owner_line(room),
            'Issued: '+', '.join(room.credentials)+'. Keys are private runtime files. No assistant starts automatically.',
            'Explicit background room; no scheduled worker or automatic model runs. Stop this verified process to close.']

def run_renewing(root,selected,enrollment=None,lifetime=28800,margin=300,clock=None,wait=None,stop=None,max_cycles=None,log=print,keeper_store=None,plans_root=None,review_standins=(),seat_tiers=None,chat_pair=None):
    """Keep one explicitly started room alive past the eight-hour key cap without raising the cap.

    Each cycle starts the room on the same root, port and store (the explicit enrollment only
    on the first cycle; later cycles use the saved enrollment.json), records the process and
    the cycle, waits until `margin` seconds before the keys expire, then closes it. The next
    start issues fresh keys and rewrites each selected seat's credential file; revocations
    persist through activation-state.json. A start that fails with OSError (port or launcher
    lock briefly busy, a credential file briefly held open) is retried RENEW_RETRIES times.
    `wait(seconds)` may return early: deadlines are re-checked against `clock`, so an injected
    wait must let `clock` advance. `stop` is a threading.Event. Returns the cycle records."""
    check_margin(margin,lifetime)
    if max_cycles is not None and (type(max_cycles) is not int or max_cycles<1):raise ValueError('max_cycles must be a positive whole number or None.')
    if type(selected) not in (list,tuple):raise ValueError('Select distinct registered non-owner participants.')
    selected=list(selected);root=Path(root).absolute();clock=clock or time.time
    stop=threading.Event() if stop is None else stop
    say=(lambda line:print(line,flush=True)) if log is print else log
    if wait is None:
        def wait(seconds):stop.wait(min(max(seconds,0.0),1.0))
    def pause_until(deadline):
        while not stop.is_set() and clock()<deadline:wait(deadline-clock())
    cycles=[]
    while not stop.is_set() and (max_cycles is None or len(cycles)<max_cycles):
        cycle=len(cycles)+1
        for attempt in range(RENEW_RETRIES+1):
            if attempt:
                pause_until(clock()+RENEW_RETRY_WAIT)
                if stop.is_set():return cycles
            try:
                room=start_room(root,enrollment if cycle==1 else None,selected,clock,lifetime,keeper_store=keeper_store,plans_root=plans_root,review_standins=review_standins,seat_tiers=seat_tiers,chat_pair=chat_pair)
                try:
                    write_json(root,'background-process.json',{'version':1,'pid':os.getpid(),'instance_id':room.descriptor['instance_id']})
                    record={'cycle':cycle,'instance_id':room.descriptor['instance_id'],'started_at':room.descriptor['started_at'],'expires_at':room.expiry,'origin':room.server.origin}
                    append_line(root,'renewal-log.jsonl',record)
                except BaseException:
                    room.close();raise
                break
            except OSError:
                if attempt==RENEW_RETRIES:raise
        cycles.append(record)
        try:
            say('Renewal cycle '+str(cycle)+'; keys valid until '+time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(room.expiry))+'.')
            for line in room_lines(room):say(line)
            say('Keys rotate every '+str(lifetime-margin)+'s; clients re-read their credential files.')
            pause_until(room.expiry-margin)
        finally:room.close()
    return cycles

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=ROOT/'.runtime'/'handoffs')
    p.add_argument('--enrollment',type=Path,help='Explicit full enrollment JSON inside this room runtime')
    p.add_argument('--participants',nargs='*',default=[])
    p.add_argument('--headless',action='store_true',help='Keep this explicitly started room alive without console input')
    p.add_argument('--renew',action='store_true',help='With --headless: restart this room shortly before its keys expire; same port, history and selection, fresh keys')
    p.add_argument('--lifetime',type=int,default=28800,help='Participant key lifetime in seconds, 60 through 28800 (default 28800)')
    p.add_argument('--renew-margin',type=int,help='With --renew: seconds before key expiry to restart (default 300)')
    p.add_argument('--keeper-store',choices=['file','wincred'],default='file',help='Where the owner key goes: file (keeper-credential.json, default) or wincred (Windows Credential Manager; the file becomes a stub with no key, and a failed vault write falls back to the file with a warning)')
    p.add_argument('--plans-root',type=Path,help='The orchestra folder whose PLAN / DECISIONS files the owner may approve on the board (a missing folder lists nothing)')
    p.add_argument('--review-standins',type=Path,help='A2 D10: the review stand-ins file (default: crew/review-standins.json, read only for the default room root)')
    p.add_argument('--seat-tiers',type=Path,help='A4: the seat tier labels file (default: crew/seat-tiers.json, read only for the default room root)')
    a=p.parse_args(argv);config=None
    if a.renew and not a.headless:p.error('--renew requires --headless.')
    if a.renew_margin is not None and not a.renew:p.error('--renew-margin requires --headless --renew.')
    margin=300 if a.renew_margin is None else a.renew_margin
    try:check_margin(margin,a.lifetime) if a.renew else check_lifetime(a.lifetime)
    except ValueError as exc:p.error(str(exc))
    try:resolve_plans_root(a.plans_root)
    except ValueError as exc:p.error(str(exc))
    plans={'plans_root':a.plans_root} if a.plans_root is not None else {}  # without the flag, every call is exactly as before
    # A2 D10: the live room (the default root) takes the shipped stand-ins file; any other room only one named here.
    standins_file=a.review_standins or (STANDINS_FILE if a.root.absolute()==(ROOT/'.runtime'/'handoffs').absolute() else None)
    tiers_file=a.seat_tiers or (SEAT_TIERS_FILE if a.root.absolute()==(ROOT/'.runtime'/'handoffs').absolute() else None)
    # A3: the chat flag has no command-line route at all; the live room alone reads its file, through chat_rules.chat_pair.
    chat_file=CHAT_PAIR_FILE if a.root.absolute()==(ROOT/'.runtime'/'handoffs').absolute() else None
    try:
        if a.enrollment:
            source=safe_path(a.root,a.enrollment)
            # A missing or unreadable file is a configuration refusal too (exit 2, one line), so
            # keepalive stops instead of restarting the room forever (fix-board, VERIFY7A I3).
            try:data=source.read_bytes()
            except OSError as exc:raise ValueError('The enrollment file could not be read: '+str(source)+' ('+type(exc).__name__+').') from exc
            config=decode_request(data)
        store=keeper_vault.store_for_writing(a.keeper_store,a.root);vault={} if store is None else {'keeper_store':store}
        if standins_file is not None:plans['review_standins']=load_standins(standins_file)
        if tiers_file is not None:
            labels=load_seat_tiers(tiers_file)
            if labels is not None:plans['seat_tiers']=labels
        if chat_file is not None:
            pair=chat_rules.chat_pair(chat_file)   # a bad file is a ChatFlagError (a ValueError): exit 2 below
            if pair is not None:plans['chat_pair']=pair
        if a.renew:
            try:run_renewing(a.root,a.participants,config,lifetime=a.lifetime,margin=margin,log=lambda line:print(line,flush=True),**vault,**plans)
            except KeyboardInterrupt:print('Renewing room stopped.',flush=True)
            return
        room=start_room(a.root,config,a.participants,lifetime=a.lifetime,**vault,**plans)
    except ValueError as exc:
        # A configuration refusal (an enrollment that edits a recorded grant, a linked path, a bad
        # selection) exits 2 with one line, so common_room.keepalive stops instead of retrying.
        print('common_room.activation: '+str(exc),file=sys.stderr,flush=True);raise SystemExit(2)
    run_console(room,headless=a.headless)

def run_console(room,headless=False):
    try:
        print('Common Room: '+room.server.origin+'\n'+owner_line(room),flush=True)
        print('Issued: '+', '.join(room.credentials)+'. Keys are private runtime files. No assistant starts automatically.',flush=True)
        if headless:
            write_json(room.root,'background-process.json',{'version':1,'pid':os.getpid(),'instance_id':room.descriptor['instance_id']})
            print('Explicit background room; no scheduled worker or automatic model runs. Stop this verified process to close.',flush=True)
            threading.Event().wait()
            return
        print('Owner console: stop | rotate ACTOR | revoke ACTOR',flush=True)
        while True:
            try:line=input('room> ').strip().split()
            except EOFError:break
            if line in (['stop'],['quit'],['exit']):break
            try:
                if len(line)==2 and line[0]=='rotate':room.rotate(line[1]);print('Rotated; participant must sign in again.')
                elif len(line)==2 and line[0]=='revoke' and line[1] in room.store.people:room.server.credential_policy.revoke(line[1]);print('Revoked, including after restart.')
                else:print('Use stop, rotate ACTOR, or revoke ACTOR.')
            except (ValueError,RoomError,OSError) as exc:print(str(exc))
    finally:room.close()
if __name__=='__main__':main()
