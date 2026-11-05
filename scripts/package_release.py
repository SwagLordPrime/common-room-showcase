"""Deterministic offline runtime export and exact fresh-extraction verification."""
from pathlib import Path, PurePosixPath
import argparse, hashlib, re, zipfile
ROOT=Path(__file__).resolve().parents[1]
DOCS=['README.md','README_2026-09-12.md','INSTALL_RUNBOOK.md','INSTALL_RUNBOOK_2026-09-12.md','LIVE_CONNECTION_PLAN_2026-09-12.md','BUILD_PLAN_COMMON_ROOM_2026-09-11.md','STORE_CONTRACT_2026-09-11.md','BOARD_CONTRACT_2026-09-11.md','ADAPTER_CONTRACT_2026-09-11.md','CONTINUATION_2026-09-12.md','Start Common Room.cmd','protocol-schema.json']
def digest(b):return hashlib.sha256(b).hexdigest()
def payload():
    paths=[ROOT/p for p in DOCS]+list((ROOT/'common_room').glob('*.py'))+list((ROOT/'web').glob('*'))+list((ROOT/'tests').glob('*.py'))+[ROOT/'scripts/package_release.py']
    if any(not p.is_file() or p.is_symlink() or p.is_junction() for p in paths):raise ValueError('Missing or linked release input')
    return {p.relative_to(ROOT).as_posix():p.read_bytes() for p in sorted(paths)}
def verify(root):
    root=Path(root).resolve(); expected={}
    for line in (root/'PACKAGE_SHA256SUMS.txt').read_text(encoding='utf-8').splitlines():
        if not re.fullmatch(r'[0-9a-f]{64}  .+',line):raise ValueError('Malformed manifest')
        h,name=line.split('  ',1);p=PurePosixPath(name)
        if p.is_absolute() or '..' in p.parts or ':' in name or '\\' in name or p.as_posix()!=name or name in expected or name=='PACKAGE_SHA256SUMS.txt':raise ValueError('Unsafe or duplicate manifest path')
        expected[name]=h
    actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and not any(x in ('.runtime','.checks','__pycache__') for x in p.relative_to(root).parts)}
    if actual!=set(expected)|{'PACKAGE_SHA256SUMS.txt'}:raise ValueError('Package file set differs from manifest')
    for name,h in expected.items():
        p=root/name
        if not p.resolve().is_relative_to(root) or any(q.is_symlink() or q.is_junction() for q in [p,*p.parents] if q!=root and q.is_relative_to(root)):raise ValueError('Linked package input')
        if digest(p.read_bytes())!=h:raise ValueError('Package hash mismatch: '+name)
    return len(expected)
def build(output):
    output=Path(output).absolute()
    if not output.resolve().is_relative_to(ROOT.resolve()):raise ValueError('Export must stay inside this package worktree')
    if output.exists():raise ValueError('Preserve existing export; use a new filename')
    files=payload();manifest=''.join(digest(b)+'  '+n+'\n' for n,b in sorted(files.items()))
    files['PACKAGE_SHA256SUMS.txt']=manifest.encode('utf-8')
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'x',compression=zipfile.ZIP_DEFLATED) as z:
        for name,data in sorted(files.items()):
            info=zipfile.ZipInfo(name,date_time=(2026,9,12,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o100644<<16;z.writestr(info,data)
    (ROOT/'PACKAGE_SHA256SUMS.txt').write_text(manifest,encoding='utf-8',newline='\n')
    return digest(output.read_bytes())
def main():
    p=argparse.ArgumentParser(description=__doc__);g=p.add_mutually_exclusive_group(required=True);g.add_argument('--verify',type=Path);g.add_argument('--build',type=Path);a=p.parse_args()
    print('Verified payload files: '+str(verify(a.verify)) if a.verify else 'Archive SHA-256: '+build(a.build))
if __name__=='__main__':main()
