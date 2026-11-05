"""Capture only this change's relevant source and explicit test output."""
from pathlib import Path
import hashlib,json,subprocess,sys
R=Path(__file__).resolve().parents[1]
def run(label,modules):
 out=R/'evidence/collaboration-2026-09-12'/label;out.mkdir(parents=True,exist_ok=False)
 names=['common_room/board.py','common_room/handoff_api.py','common_room/store.py','common_room/activation.py','common_room/open_room.py','common_room/participant.py','web/board.js','web/index.html','tests/test_collaboration.py','tests/test_open_room.py']
 inputs={}
 for name in names:
  p=R/name
  if p.exists():
   data=p.read_bytes();dest=out/name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(data);inputs[name]=hashlib.sha256(data).hexdigest()
  else:inputs[name]=None
 result=subprocess.run([sys.executable,'-X','utf8','-B','-m','unittest',*modules,'-v'],cwd=R,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
 (out/'output.txt').write_bytes(result.stdout)
 record={'label':label,'modules':modules,'exit_code':result.returncode,'inputs':inputs,'output_sha256':hashlib.sha256(result.stdout).hexdigest()}
 (out.parent/(label+'.json')).write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
 print(result.stdout.decode('utf-8',errors='replace')[-2200:]);print(json.dumps({'label':label,'exit_code':result.returncode}))
if __name__=='__main__':run(sys.argv[1],sys.argv[2:])
