"""Fictional fixtures, always inside this approved worktree."""
from pathlib import Path
import os, uuid, sys, unittest
ROOT = Path(__file__).resolve().parents[1]
CHECKS = ROOT / ".checks"
CHECKS.mkdir(exist_ok=True)

def case_dir(name):
    path = CHECKS / (name + "-" + uuid.uuid4().hex)
    path.mkdir()
    return path

def network_guard(event, args):
    if event == "socket.connect":
        address = args[1]
        if not isinstance(address, tuple) or address[0] not in ("127.0.0.1", "::1", "localhost"):
            raise RuntimeError("Offline fixture attempted a non-loopback connection")
sys.addaudithook(network_guard)


WINDOWS_NO_SYMLINK = 1314   # ERROR_PRIVILEGE_NOT_HELD
_SYMLINKS = []              # the probe's answer, once asked
NO_SYMLINKS = ('this Windows host cannot make symbolic links (no Developer Mode or admin rights), '
               'so no process here can plant one either')


def symlinks_available():
    """False only on a Windows host that refuses symbolic links (WinError 1314: no Developer Mode and
    no admin rights). Everywhere else True, so a POSIX failure to link still fails its test loudly."""
    if os.name != 'nt':
        return True
    if not _SYMLINKS:
        probe = case_dir('symlink-probe')
        try:
            os.symlink(probe, probe / 'link', target_is_directory=True)
            _SYMLINKS.append(True)
        except OSError as exc:
            if getattr(exc, 'winerror', None) != WINDOWS_NO_SYMLINK:
                raise
            _SYMLINKS.append(False)
    return _SYMLINKS[0]


def symlinks_or_skip():
    """Skip the calling test (or subTest) where symlinks_available() is False: the link refusals it
    checks are unreachable there, since nothing on that host can plant a link."""
    if not symlinks_available():
        raise unittest.SkipTest(NO_SYMLINKS)


def read_unless_locked(path):
    """path.read_bytes(), except that a Windows byte-range lock (the room's .launcher.lock, a
    worker's <seat>.lock) refuses reads while its holder runs; those files only ever hold '0'.
    Only names ending in .lock get this allowance: any other unreadable file still raises."""
    try:
        return Path(path).read_bytes()
    except PermissionError:
        if os.name == 'nt' and Path(path).name.endswith('.lock'):
            return b''
        raise
