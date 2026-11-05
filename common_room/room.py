"""Explicit operator-controlled, persisted fictional room; no automatic scheduler."""
from pathlib import Path
import argparse, json, secrets, threading, uuid
from .protocol import Actor
from .store import Store
from .board import make_server
from .adapter import FixtureWorker, FixtureReviewer
ROOT=Path(__file__).resolve().parents[1]

def open_room(root):
    root=Path(root).absolute()
    if not any(root.resolve().is_relative_to((ROOT/n).resolve()) and root.resolve()!=(ROOT/n).resolve() for n in ('.checks','.runtime')):
        raise ValueError('Use a child directory under this package .runtime or .checks.')
    # Refuse links in the operator-selected fixture path before creating any data.
    for part in [root,*root.parents]:
        if part==ROOT:break
        if part.is_symlink() or (hasattr(part,'is_junction') and part.is_junction()):raise ValueError('Linked fixture directories are refused.')
    root.mkdir(parents=True,exist_ok=True)
    art=root/'artifacts'
    if art.is_symlink() or (hasattr(art,'is_junction') and art.is_junction()):raise ValueError('Linked artifacts are refused.')
    art.mkdir(exist_ok=True)
    people={a.id:a for a in [Actor('keeper','owner'),Actor('scribe','worker'),Actor('reader','reviewer')]}
    store=Store(root/'room.sqlite',people,{'fixture':art})
    store.execute(people['keeper'],'grant',{'id':'fixture','members':list(people),'capabilities':['document.count'],'root_id':'fixture','scope':'Fictional word-count demonstration only'},'room-fixture-grant-v1')
    return store,people

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT/'.runtime'/'common-room')
    parser.add_argument('--connection-file',type=Path)
    args=parser.parse_args()
    if args.connection_file:
        connection=args.connection_file.absolute()
        if not connection.resolve().is_relative_to((ROOT/'.checks').resolve()) or connection.exists():raise ValueError('Use a new .checks connection file.')
    store,people=open_room(args.root)
    key=secrets.token_urlsafe(32);server=make_server(store,{key:people['keeper']})
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    worker=FixtureWorker(store,people['scribe']);reviewer=FixtureReviewer(store,people['reader'])
    if args.connection_file:connection.write_text(json.dumps({'origin':server.origin,'key':key}),encoding='utf-8')
    print('Common Room: '+server.origin+'\nAccess key: '+key+'\nFixture data: '+str(store.path.parent),flush=True)
    print('Open the address and sign in. Add a draft, then Deliver.\nCommands: work = one fixed count; review = separate fixture check; stop = close.\nNo real assistants connected. Posting never starts work. Data survives restart.',flush=True)
    try:
        while True:
            try:choice=input('room> ').strip().lower()
            except EOFError:break
            if choice in ('stop','quit','exit'):break
            if choice in ('work','review'):
                try:print(json.dumps((worker if choice=='work' else reviewer).run_once()),flush=True)
                except Exception as exc:print('Step refused: '+str(exc),flush=True)
            else:print('Use work, review or stop. Message text is never executed.',flush=True)
    finally:server.shutdown();server.server_close();thread.join(3)
if __name__=='__main__':main()
