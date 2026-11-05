"""Local handoff board. No model dispatcher; only owner credentials by default."""
from pathlib import Path
import argparse, json, secrets, threading, time, os
from .protocol import Actor, safe_artifact
from .store import Store
from .board import make_server
from .handoff_api import CredentialPolicy
ROOT=Path(__file__).resolve().parents[1]

def open_handoff_room(root,fixture=False,activation=False):
    root=Path(root).absolute()
    if not any(root.resolve().is_relative_to((ROOT/n).resolve()) and root.resolve()!=(ROOT/n).resolve() for n in ('.runtime','.checks')):
        raise ValueError('Use a child directory of this package runtime or checks.')
    for part in [root,*root.parents]:
        if part==ROOT:break
        if part.is_symlink() or part.is_junction():raise ValueError('Linked runtime path refused.')
    if fixture and not root.resolve().is_relative_to((ROOT/'.checks').resolve()):raise ValueError('Fixture participant credentials require a checks directory.')
    if os.name=='nt' and len(str(root/'artifacts'/'results'/('0'*48+'.txt')))>=260:
        raise ValueError('Runtime path is too deep for Windows artifact imports; choose a shorter empty package path.')
    if (root/'enrollment.json').exists() and not activation:raise ValueError('This room uses the activation launcher; use common_room.activation.')
    root.mkdir(parents=True,exist_ok=True)
    marker=root/'room-mode.json'
    mode='fixture' if fixture else 'coordination'
    if marker.exists():
        if marker.is_symlink() or marker.is_junction() or json.loads(marker.read_text(encoding='utf-8'))!={'mode':mode}:raise ValueError('Runtime mode mismatch.')
    else:marker.write_text(json.dumps({'mode':mode}),encoding='utf-8')
    art=root/'artifacts'
    if art.is_symlink() or art.is_junction():raise ValueError('Linked artifact root refused.')
    art.mkdir(exist_ok=True)
    ids=['keeper','fixture-worker','fixture-reviewer'] if fixture else ['keeper','astra','reviewer','claude-estate']
    roles=['owner','worker','reviewer'] if fixture else ['owner','worker','reviewer','worker']
    people={name:Actor(name,role) for name,role in zip(ids,roles)}
    store=Store(root/'room.sqlite',people,{'handoffs':art})
    store.execute(people['keeper'],'grant_handoff',{'id':'handoffs','members':list(people),'capabilities':['handoff.record'],
        'root_id':'handoffs','scope':'Local coordination records only. A handoff records responsibility; it does not grant build or deployment permission.',
        'source_namespaces':['estate']},'handoff-authority-v1')
    return store,people

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,default=ROOT/'.runtime'/'handoffs');p.add_argument('--fixture-credentials',action='store_true');p.add_argument('--connection-file',type=Path)
    a=p.parse_args()
    if a.connection_file:
        connection=a.connection_file.absolute()
        if connection.exists() or not connection.resolve().is_relative_to((ROOT/'.checks').resolve()):raise ValueError('Use a new checks connection file.')
    store,people=open_handoff_room(a.root,a.fixture_credentials)
    active=people if a.fixture_credentials else {'keeper':people['keeper']}
    tokens={secrets.token_urlsafe(32):actor for actor in active.values()};expiry=time.time()+28800
    policy=CredentialPolicy(tokens,expiry,time.time)
    server=make_server(store,tokens,credential_policy=policy)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    connections={}
    try:
        for token,actor in tokens.items():
            config={'origin':server.origin,'token':token,'actor_id':actor.id,'expires_at':expiry}
            path=a.root/(actor.id+'-credential.json')
            if path.is_symlink() or path.is_junction():raise ValueError('Linked credential destination refused.')
            path.write_text(json.dumps(config),encoding='utf-8');connections[actor.id]=config
        if a.connection_file:connection.write_text(json.dumps(connections),encoding='utf-8')
        owner=next(k for k,v in tokens.items() if v.id=='keeper')
        print('Common Room handoffs: '+server.origin+'\nOwner access key: '+owner+'\nSaved room: '+str(a.root),flush=True)
        print('Opening the room starts no model. Participant names are assignments, not verified connections. Type stop to close.',flush=True)
        while True:
            try:choice=input('room> ').strip().lower()
            except EOFError:break
            if choice in ('stop','quit','exit'):break
            print('Use the board or explicit participant client. Type stop to close.',flush=True)
    finally:server.shutdown();server.server_close();thread.join(3)
if __name__=='__main__':main()
