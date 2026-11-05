"""Capture test output and exact source/test snapshots for activation gates."""
from pathlib import Path
import hashlib,json,subprocess,sys
ROOT=Path(__file__).resolve().parents[1]
def run(label,modules):
    out=ROOT/'evidence/activation-2026-09-12'/label
    out.mkdir(parents=True,exist_ok=False)
    inputs={}
    for folder in ['common_room','web','tests']:
        for p in sorted((ROOT/folder).glob('*')):
            if p.suffix not in {'.py','.js','.css','.html'}:continue
            name=p.relative_to(ROOT).as_posix();data=p.read_bytes()
            dest=out/'source'/name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(data)
            inputs[name]=hashlib.sha256(data).hexdigest()
    result=subprocess.run([sys.executable,'-B','-m','unittest',*modules,'-v'],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    (out/'output.txt').write_bytes(result.stdout)
    record={'label':label,'command':['unittest',*modules,'-v'],'exit_code':result.returncode,'inputs':inputs,'output_sha256':hashlib.sha256(result.stdout).hexdigest()}
    (out.parent/(label+'.json')).write_text(json.dumps(record,indent=2))
    print(result.stdout.decode(errors='replace'));print(json.dumps({'label':label,'exit_code':result.returncode}))
if __name__=='__main__':run(sys.argv[1],sys.argv[2:])
