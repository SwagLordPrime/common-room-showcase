"""Deterministic activation release, preserving previous archives."""
from pathlib import Path
import argparse,hashlib,sys,zipfile
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.package_handoffs import payload as handoff_payload
from scripts.package_release import verify
EXTRA=['ACTIVATION_README_2026-09-12.md','ACTIVATION_RUNBOOK_2026-09-12.md','ACTIVATION_PARTICIPANT_GUIDE_2026-09-12.md','BUILD_PLAN_COMMON_ROOM_ACTIVATION_2026-09-12.md','Start Common Room Participants.cmd','scripts/check_activation_package.py','scripts/package_activation.py','scripts/activation_gate.py','evidence/activation-2026-09-12/approval.json']
EXTRA += ['Open Common Room.cmd','COLLABORATION_README_2026-09-12.md','COLLABORATION_RUNBOOK_2026-09-12.md','BUILD_PLAN_COMMON_ROOM_COLLABORATION_2026-09-12.md','COLLABORATION_GATES.json','COLLABORATION_ENROLLMENT_2026-09-12.json','COLLABORATION_SELECTION_2026-09-12.json','scripts/package_collaboration.py','scripts/collaboration_gate.py']

def build(output):
    output=Path(output).absolute()
    if not output.resolve().is_relative_to(ROOT.resolve()) or output.exists():raise ValueError('Use a new archive in this worktree.')
    files=handoff_payload()
    for name in EXTRA:
        p=ROOT/name
        if not p.is_file() or p.is_symlink() or p.is_junction():raise ValueError('Missing or linked input: '+name)
        files[name]=p.read_bytes()
    files['README.md']=files['COLLABORATION_README_2026-09-12.md']
    files['PACKAGE_SHA256SUMS.txt']=''.join(hashlib.sha256(data).hexdigest()+'  '+name+'\n' for name,data in sorted(files.items())).encode()
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'x',compression=zipfile.ZIP_DEFLATED) as z:
        for name,data in sorted(files.items()):
            info=zipfile.ZipInfo(name,date_time=(2026,9,12,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o100644<<16;z.writestr(info,data)
    return hashlib.sha256(output.read_bytes()).hexdigest()
if __name__=='__main__':
    p=argparse.ArgumentParser();g=p.add_mutually_exclusive_group(required=True);g.add_argument('--build',type=Path);g.add_argument('--verify',type=Path);a=p.parse_args()
    print('Verified files: '+str(verify(a.verify)) if a.verify else 'Archive SHA-256: '+build(a.build))
