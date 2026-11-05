"""Prepared fictional board states, not an automated assistant or adapter."""
from pathlib import Path
import argparse, hashlib, json, secrets, sys, threading, uuid
from .protocol import Actor
from .store import Store
from .board import make_server
ROOT=Path(__file__).resolve().parents[1]

def prepare(root):
    art=root/'artifacts';art.mkdir()
    people={a.id:a for a in [Actor('keeper','owner'),Actor('scribe','worker'),Actor('reader','reviewer')]}
    store=Store(root/'room.sqlite',people,{'fixture':art});n=0
    def command(who,action,payload):
        nonlocal n
        n+=1;return store.execute(people[who],action,payload,'seed-'+str(n))
    command('keeper','grant',{'id':'demo','members':list(people),'capabilities':['document.count'],'root_id':'fixture','scope':'Prepared fictional word-count examples only'})
    content=b'Prepared fixture: three words.\n';(art/'prepared.txt').write_bytes(content)
    ref={'path':'prepared.txt','sha256':hashlib.sha256(content).hexdigest()}
    def task(title,end):
        t=command('keeper','create',{'title':title,'body':'One quiet orchard','recipient':'scribe','authority_id':'demo','kind':'document.count','acceptance':'A reproducible word count'})['task']['id']
        command('keeper','deliver',{'task_id':t})
        if end=='delivered':return
        command('scribe','ack',{'task_id':t});lease=command('scribe','claim',{'task_id':t,'seconds':300})['task']['lease']
        if end=='claimed':return
        if end=='waiting_decision':command('scribe','wait_decision',{'task_id':t,'fence':lease['fence'],'question':'Which fictional note should this use?'});return
        command('scribe','finish',{'task_id':t,'fence':lease['fence'],'refs':[ref],'body':'Prepared fixture result, not a worker execution.'})
        if end=='done':command('reader','review',{'task_id':t,'accepted':True,'note':'Prepared fixture acceptance for the board demonstration.'})
    for title,status in [('Count the orchard note','delivered'),('A note at the workbench','claimed'),('Ready for a second pair of eyes','ready_for_review'),('A little quest completed','done'),('A question for the Keeper','waiting_decision')]:task(title,status)
    command('keeper','note',{'authority_id':'demo','body':'Welcome to the Common Room. Clear work, quiet company.'})
    return store,people

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--connection-file');parser.add_argument('--port',type=int,default=0);args=parser.parse_args()
    if args.connection_file:
        connection=Path(args.connection_file).absolute();connection.resolve().relative_to((ROOT/'.checks').resolve())
        if connection.exists():raise SystemExit('Use a new fixture connection file.')
        root=connection.parent
    else:
        root=ROOT/'.runtime'/('board-'+uuid.uuid4().hex);root.mkdir(parents=True);connection=root/'connection.json'
    store,people=prepare(root);key=secrets.token_urlsafe(32)
    server=make_server(store,{key:people['keeper']},port=args.port)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    connection.write_text(json.dumps({'origin':server.origin,'key':key}),encoding='utf-8')
    if not args.connection_file:print('Common Room fixture: '+server.origin+'\nAccess key: '+key+'\nPress Enter here to stop. No real assistants are connected.',flush=True)
    try:sys.stdin.readline()
    finally:server.shutdown();server.server_close();worker.join(3)
if __name__=='__main__':main()
