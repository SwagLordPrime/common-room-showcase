"""Open the connected board; unaccepted connections remain visibly disabled."""
from pathlib import Path
import argparse,hashlib,json,os,secrets,threading
from .protocol import Actor,RoomError
from .store import Store
from .board import make_server
from .connection import Dispatcher,RunController
from .codex_transport import CodexTransport
ROOT=Path(__file__).resolve().parents[1]

def open_connected_room(root):
    root=Path(root).absolute()
    if not any(root.resolve().is_relative_to((ROOT/name).resolve()) and root.resolve()!=(ROOT/name).resolve() for name in ('.checks','.runtime')):raise RoomError('config','Use a child directory of this package .runtime or .checks.')
    for part in [root,*root.parents]:
        if part==ROOT:break
        if part.is_symlink() or (hasattr(part,'is_junction') and part.is_junction()):raise RoomError('config','Linked room paths are refused.')
    root.mkdir(parents=True,exist_ok=True);art=root/'artifacts'
    if art.is_symlink() or (hasattr(art,'is_junction') and art.is_junction()):raise RoomError('config','Linked artifact roots are refused.')
    art.mkdir(exist_ok=True)
    people={a.id:a for a in (Actor('keeper','owner'),Actor('codex-worker','worker'),Actor('codex-reviewer','reviewer'))}
    store=Store(root/'room.sqlite',people,{'codex-text':art})
    store.execute(people['keeper'],'grant',{'id':'codex-text','members':list(people),'capabilities':['document.count'],'root_id':'codex-text','scope':'Fictional text word-counts explicitly submitted to Codex; separate review; no model tools.'},'codex-text-grant-v1')
    return store,people

def connection_accepted(executable,cli_home):
    """A local release review alone cannot unlock provider use."""
    path=ROOT/'CONNECTION_ACCEPTANCE.json'
    if not path.is_file():return False,'Connection check pending. This board is available, but real Codex runs are disabled.'
    try:
        proof=json.loads(path.read_text(encoding='utf-8'))
        if proof.get('status')!='PASS' or proof.get('profile_proof')!='normal-home-pass':raise ValueError()
        if Path(proof['cli_home']).resolve()!=cli_home.resolve():raise ValueError()
        if proof['executable_sha256']!=hashlib.sha256(executable.read_bytes()).hexdigest():raise ValueError()
        required={p.relative_to(ROOT).as_posix() for p in [*ROOT.glob('common_room/*.py'),*ROOT.glob('web/*')] if p.is_file()}
        if not required<=set(proof['source_sha256']):raise ValueError()
        for relative,digest in proof['source_sha256'].items():
            target=(ROOT/relative).resolve()
            if not target.is_relative_to(ROOT.resolve()) or hashlib.sha256(target.read_bytes()).hexdigest()!=digest:raise ValueError()
        return True,None
    except (OSError,KeyError,TypeError,ValueError):return False,'The client, profile or build differs from its connection proof. Real runs are disabled until it is checked again.'

def find_codex():
    base=Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData'/'Local')))/'OpenAI'/'Codex'/'bin'
    matches=list(base.glob('*/codex.exe'))
    if not matches:raise RoomError('config','No installed Codex client found. The offline room remains available.')
    return max(matches,key=lambda p:p.stat().st_mtime)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT/'.runtime'/'codex-common-room')
    parser.add_argument('--codex',type=Path)
    parser.add_argument('--cli-home',type=Path,default=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex'))))
    parser.add_argument('--connection-file',type=Path)
    args=parser.parse_args()
    if args.connection_file:
        output=args.connection_file.absolute()
        if not output.resolve().is_relative_to((ROOT/'.checks').resolve()) or output.exists():raise RoomError('config','Use a new .checks connection file.')
    executable=(args.codex or find_codex()).absolute();home=args.cli_home.absolute()
    accepted,reason=connection_accepted(executable,home)
    store,people=open_connected_room(args.root)
    controller=RunController(Dispatcher(store,people['codex-worker'],people['codex-reviewer'],CodexTransport(executable,home),store.path.parent/'transport'),enabled=accepted,blocked_reason=reason)
    key=secrets.token_urlsafe(32);server=make_server(store,{key:people['keeper']},controller=controller)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    if args.connection_file:output.write_text(json.dumps({'origin':server.origin,'key':key}),encoding='utf-8')
    print('Common Room: '+server.origin+'\nAccess key: '+key+'\nSaved room: '+str(store.path.parent),flush=True)
    print(reason or 'Ready. Deliver a fictional word-count task, then use Run worker and Run reviewer on its card. Each explicit run can use your Codex allowance.',flush=True)
    print('Opening this room does not start a model. Type stop to close it. Data and receipts remain saved.',flush=True)
    try:
        while True:
            try:choice=input('room> ').strip().lower()
            except EOFError:break
            if choice in ('stop','quit','exit'):break
            print('Use the board buttons for work, or type stop here. Other text is never executed.',flush=True)
    finally:controller.close();server.shutdown();server.server_close();thread.join(3)
if __name__=='__main__':main()
