"""Hash only shipped engine and web source; never runtime or participant data."""
from pathlib import Path
import hashlib
from .store import LEASE_EXPIRY_POLICY
ROOT=Path(__file__).resolve().parents[1]

def source_identity(root=ROOT):
    root=Path(root)
    paths=sorted([* (root/"common_room").glob("*.py"),
                  *(p for p in (root/"web").iterdir() if p.suffix in {".js",".css",".html"})],
                 key=lambda p:p.relative_to(root).as_posix())
    if not paths:raise ValueError("No Common Room source to identify.")
    digest=hashlib.sha256()
    for p in paths:
        if p.is_symlink() or p.is_junction() or not p.is_file():raise ValueError("Linked or non-file source refused.")
        name=p.relative_to(root).as_posix()
        file_hash=hashlib.sha256(p.read_bytes()).hexdigest()
        digest.update((name+"\0"+file_hash+"\n").encode("utf-8"))
    return {"source_sha256":digest.hexdigest(),"hash_scheme":"path-nul-sha256-lf-v1",
            "lease_expiry_policy":LEASE_EXPIRY_POLICY}
