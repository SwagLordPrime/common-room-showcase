"""Explicit loopback participant client. Every write uses a caller-kept request ID."""
import argparse, http.client, json, re, sys
from pathlib import Path
from .protocol import canonical, decode_request
from .keeper_vault import STUB_REFUSAL, is_stub

# The largest room response a seat will read (state-wall hotfix, 2026-10-15). It was 2 MiB; on 2026-10-15 the
# scoped /api/state of the busiest seats was about 1.75-1.8 MB and growing, and a seat past the cap can send but
# not read, so crew watch and the relay stall. 16 MiB buys weeks at today's growth until S0's bounded state ships.
# Requests stay capped at 16 KiB below.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024

class Participant:
    def __init__(self,origin,token):
        if type(origin) is not str or not re.fullmatch(r'http://127[.]0[.]0[.]1:[0-9]{1,5}',origin):
            raise ValueError('Use the exact loopback room origin without paths or credentials.')
        port=int(origin.rsplit(':',1)[1])
        if not 1<=port<=65535 or type(token) is not str or not 0<len(token)<=512:raise ValueError('Invalid local connection.')
        self.origin=origin;self.port=port;self.token=token;self.cookie=None
    def _request(self,path,payload=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        headers={'Origin':self.origin,'Host':self.origin[7:]}
        if self.cookie:headers['Cookie']=self.cookie
        data=None
        if payload is not None:
            data=canonical(payload).encode()
            if len(data)>16384:raise ValueError('Request exceeds the room limit.')
            headers['Content-Type']='application/json'
        try:
            connection.request('GET' if payload is None else 'POST',path,body=data,headers=headers)
            response=connection.getresponse();raw=response.read(MAX_RESPONSE_BYTES+1)
            if len(raw)>MAX_RESPONSE_BYTES:raise ValueError('Room response exceeds the client limit.')
            result=json.loads(raw)
            if response.status!=200:raise ValueError(result.get('error','request_failed')+': '+result.get('message','Request refused'))
            cookie=response.getheader('Set-Cookie')
            if cookie:self.cookie=cookie.split(';',1)[0]
            return result
        except (OSError,http.client.HTTPException,json.JSONDecodeError) as exc:
            raise ValueError('Local room unavailable or response lost; retain the same request ID for retry.') from exc
        finally:connection.close()
    def login(self):return self._request('/api/login',{'token':self.token})
    def state(self):return self._request('/api/state')
    def command(self,action,payload,request_id):
        if action not in {'request_help','intake','deliver','ack','claim','renew','assign','finish','fail','wait_decision','resolve_decision','reinstate','cancel','review','pause','resume','revoke','note','question_mark','stop_all','resume_all','chat_mark','chat_status','chat_close'}:   # A3: the room refuses the chat_* names unless its chat is ON
            raise ValueError('Unsupported participant operation.')
        return self._request('/api/command',{'action':action,'payload':payload,'request_id':request_id})
    def handoff(self,operation,payload):return self._request('/api/handoff',{'operation':operation,'payload':payload})
    def key_check(self,value):return self._request('/api/key-check',{'text':value})

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--credential',type=Path,required=True)
    p.add_argument('operation',choices=['inbox','command','receipt','import','revoke-key']);p.add_argument('--action');p.add_argument('--request-id');p.add_argument('--task-id');p.add_argument('--actor-id')
    args=p.parse_args();root=Path(__file__).resolve().parents[1]
    credential=args.credential.absolute()
    if not any(credential.resolve().is_relative_to((root/n).resolve()) for n in ('.runtime','.checks')):raise ValueError('Credential must be in this package runtime or checks directory.')
    if any(x.is_symlink() or x.is_junction() for x in [credential,*credential.parents] if x!=root and x.is_relative_to(root)):raise ValueError('Linked credential paths refused.')
    if credential.stat().st_size>4096:raise ValueError('Oversized credential file.')
    config=decode_request(credential.read_bytes())
    if is_stub(config):raise ValueError(STUB_REFUSAL)
    if set(config)!={'origin','token','actor_id','expires_at'}:raise ValueError('Invalid credential file.')
    c=Participant(config['origin'],config['token']);identity=c.login()
    if identity['actor']!=config['actor_id']:raise ValueError('Credential identity mismatch.')
    if args.operation=='inbox':result=c.state()
    elif args.operation=='receipt':result=c.handoff('receipt',{'task_id':args.task_id})
    elif args.operation=='revoke-key':result=c.handoff('revoke',{'actor_id':args.actor_id})
    else:
        payload=decode_request(sys.stdin.buffer.read(16385))
        if args.operation=='import':result=c.handoff('import',payload)
        else:
            if not args.request_id:raise ValueError('Keep an explicit request ID for each action and its retries.')
            result=c.command(args.action,payload,args.request_id)
    print(canonical(result))
if __name__=='__main__':
    try:main()
    except (ValueError,OSError) as exc:print(str(exc),file=sys.stderr);raise SystemExit(1)
