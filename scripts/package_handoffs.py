"""Exact deterministic handoff runtime archive; no credentials or enabling marker."""
from pathlib import Path
import argparse,hashlib,sys,zipfile
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.package_release import DOCS,verify
EXTRA=['HANDOFF_README_2026-09-12.md','HANDOFF_RUNBOOK_2026-09-12.md','PARTICIPANT_CONNECTION_CARD_2026-09-12.md','BUILD_PLAN_COMMON_ROOM_HANDOFFS_2026-09-12.md','Start Common Room Handoffs.cmd','Start Connected Common Room.cmd','CONNECTION_RUNBOOK_2026-09-12.md','evidence/handoffs-2026-09-12/approval.json','scripts/package_handoffs.py','scripts/check_handoff_package.py','scripts/package_release.py','scripts/gate.py']
def payload():
    names=set(DOCS+EXTRA)
    names.update(p.relative_to(ROOT).as_posix() for folder in ['common_room','tests','web'] for p in (ROOT/folder).glob('*') if p.suffix in {'.py','.css','.js','.html'})
    files={}
    for name in sorted(names):
        p=ROOT/name
        if not p.is_file() or p.is_symlink() or p.is_junction():raise ValueError('Missing or linked package input: '+name)
        files[name]=p.read_bytes()
    # Intentional package-entry README mapping; source's earlier release README stays historical.
    files['README.md']=files['HANDOFF_README_2026-09-12.md']
    return files

def build(output):
    output=Path(output).absolute()
    if not output.resolve().is_relative_to(ROOT.resolve()) or output.exists():raise ValueError('Use a new archive inside this worktree.')
    files=payload();files['PACKAGE_SHA256SUMS.txt']=''.join(hashlib.sha256(data).hexdigest()+'  '+name+'\n' for name,data in sorted(files.items())).encode()
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'x',compression=zipfile.ZIP_DEFLATED) as z:
        for name,data in sorted(files.items()):
            info=zipfile.ZipInfo(name,date_time=(2026,9,12,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o100644<<16;z.writestr(info,data)
    return hashlib.sha256(output.read_bytes()).hexdigest()
if __name__=='__main__':
    p=argparse.ArgumentParser();g=p.add_mutually_exclusive_group(required=True);g.add_argument('--build',type=Path);g.add_argument('--verify',type=Path);a=p.parse_args()
    print('Verified payload files: '+str(verify(a.verify)) if a.verify else 'Archive SHA-256: '+build(a.build))
