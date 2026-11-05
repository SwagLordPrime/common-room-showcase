"""The owner (keeper) key vault: keeper's key lives in Windows Credential Manager, not in
the shared room folder every Cowork chat can read.

With a store, the room writes keeper's credential ({origin, token, actor_id, expires_at})
to the store and leaves only a stub with no key in keeper-credential.json. Every reader of
that file refuses the stub with one clear message (crew_client.load_credential); only
`crew --as keeper` (the crew-shane launcher: COMMON_ROOM_SEAT=keeper and an interactive
terminal) reads the store, through keeper_loader.

Honest limit: Credential Manager keeps the key out of the folder and away from the chats,
not away from Shane's own Windows user. A program running as that user can still ask for it.

This module imports cleanly anywhere; only WinCredStore needs Windows. MemoryStore is the
in-process stand-in the tests use.
"""
from pathlib import Path
import ctypes, hashlib, os
from types import SimpleNamespace
from .protocol import canonical, decode_request

STUB_KEY = 'stored_in'
STUB_VALUE = 'windows-credential-manager'
STUB_NOTE = 'The owner key is not kept in this folder. Shane: run crew-shane on the PC.'
STUB_REFUSAL = ('The owner key is kept in Windows Credential Manager, not in this folder. '
                'Shane: run crew-shane on the PC.')
UNAVAILABLE = 'The owner key is in Windows Credential Manager; run crew-shane on the PC.'
MISSING = 'No owner key is stored in Windows Credential Manager for this room; start the room again.'
CREDENTIAL_FIELDS = {'origin', 'token', 'actor_id', 'expires_at'}
CODES = {'unavailable', 'missing', 'too_large', 'write_failed', 'read_failed', 'invalid'}

# wincred.h / winerror.h
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168
MAX_BLOB = 2560          # CRED_MAX_CREDENTIAL_BLOB_SIZE (5 * 512)
MAX_TARGET = 256


class VaultError(Exception):
    """A refusal with a stable code; the message never holds a key."""
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(code + ': ' + message)


def target_for(root):
    """One Credential Manager target per room root, so a test room never touches the real one."""
    key = os.path.normcase(str(Path(root).resolve())).encode()
    return 'CommonRoom/keeper/' + hashlib.sha256(key).hexdigest()[:16]


def stub_for(target):
    return {STUB_KEY: STUB_VALUE, 'target': target, 'note': STUB_NOTE}


def is_stub(obj):
    """Any object carrying `stored_in` is a stub: never a key, never read as one."""
    return type(obj) is dict and STUB_KEY in obj


def check_target(target):
    if type(target) is not str or not 0 < len(target) <= MAX_TARGET or '\x00' in target:
        raise VaultError('invalid', 'The Credential Manager target must be a short name.')
    return target


def check_config(config):
    """Exactly keeper's four credential fields, bounded types."""
    if (type(config) is not dict or set(config) != CREDENTIAL_FIELDS or config['actor_id'] != 'keeper'
            or type(config['origin']) is not str or type(config['token']) is not str or not config['token']
            or type(config['expires_at']) not in (int, float)):
        raise VaultError('invalid', 'The owner key record must hold exactly origin, token, actor_id (keeper) '
                                    'and expires_at.')
    return config


def encode(config):
    check_config(config)
    try:
        blob = canonical(config).encode('utf-8')
    except ValueError as exc:
        raise VaultError('invalid', 'The owner key record is not valid JSON.') from exc
    if len(blob) > MAX_BLOB:
        raise VaultError('too_large', 'The owner key record exceeds the %d-byte Credential Manager limit.' % MAX_BLOB)
    return blob


def decode(blob):
    """Size is checked where the bytes come from (WinCredStore.read); MemoryStore holds only encode()d blobs."""
    try:
        config = decode_request(bytes(blob))
    except ValueError as exc:
        raise VaultError('invalid', 'The stored owner key record is not valid JSON.') from exc
    return check_config(config)


# ---------------------------------------------------------------------------
# Windows Credential Manager through advapi32 (ctypes). Plain ctypes types, so the
# structures also exist (and their layout can be checked) off Windows.
# ---------------------------------------------------------------------------

class FILETIME(ctypes.Structure):
    _fields_ = [('dwLowDateTime', ctypes.c_uint32), ('dwHighDateTime', ctypes.c_uint32)]


class CREDENTIALW(ctypes.Structure):
    _fields_ = [('Flags', ctypes.c_uint32),
                ('Type', ctypes.c_uint32),
                ('TargetName', ctypes.c_wchar_p),
                ('Comment', ctypes.c_wchar_p),
                ('LastWritten', FILETIME),
                ('CredentialBlobSize', ctypes.c_uint32),
                ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)),
                ('Persist', ctypes.c_uint32),
                ('AttributeCount', ctypes.c_uint32),
                ('Attributes', ctypes.c_void_p),
                ('TargetAlias', ctypes.c_wchar_p),
                ('UserName', ctypes.c_wchar_p)]


def windows_api():
    if os.name != 'nt':
        raise VaultError('unavailable', 'Windows Credential Manager exists only on Windows.')
    try:
        dll = ctypes.WinDLL('advapi32', use_last_error=True)
        write, read, delete, free = dll.CredWriteW, dll.CredReadW, dll.CredDeleteW, dll.CredFree
    except (OSError, AttributeError) as exc:
        raise VaultError('unavailable', 'Windows Credential Manager (advapi32) could not be loaded.') from exc
    write.argtypes = [ctypes.POINTER(CREDENTIALW), ctypes.c_uint32]
    write.restype = ctypes.c_int
    read.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
    read.restype = ctypes.c_int
    delete.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
    delete.restype = ctypes.c_int
    free.argtypes = [ctypes.c_void_p]
    free.restype = None
    return SimpleNamespace(CredWriteW=write, CredReadW=read, CredDeleteW=delete, CredFree=free,
                           get_last_error=ctypes.get_last_error)


class WinCredStore:
    """One generic credential, persisted for this Windows user on this machine."""
    kind = 'wincred'

    def __init__(self, target, _api=None):
        self.target = check_target(target)
        self.api = _api if _api is not None else windows_api()

    def write(self, config):
        blob = encode(config)
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        cred = CREDENTIALW()
        cred.Flags = 0
        cred.Type = CRED_TYPE_GENERIC
        cred.TargetName = self.target
        cred.Comment = 'Common Room owner key (written by the room; read by crew-shane)'
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        cred.Persist = CRED_PERSIST_LOCAL_MACHINE
        cred.AttributeCount = 0
        cred.Attributes = None
        cred.TargetAlias = None
        cred.UserName = 'keeper'
        try:
            ok = self.api.CredWriteW(ctypes.byref(cred), 0)
            error = 0 if ok else self.api.get_last_error()
        except (OSError, ValueError, TypeError, ctypes.ArgumentError) as exc:
            raise VaultError('write_failed', 'CredWriteW could not be called (' + type(exc).__name__ + ').') from exc
        finally:
            ctypes.memset(buffer, 0, len(blob))
        if not ok:
            raise VaultError('write_failed', 'CredWriteW failed with Windows error %d.' % error)

    def read(self):
        pointer = ctypes.POINTER(CREDENTIALW)()
        try:
            ok = self.api.CredReadW(self.target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer))
            error = 0 if ok else self.api.get_last_error()
        except (OSError, ValueError, TypeError, ctypes.ArgumentError) as exc:
            raise VaultError('read_failed', 'CredReadW could not be called (' + type(exc).__name__ + ').') from exc
        if not ok:
            if error == ERROR_NOT_FOUND:
                raise VaultError('missing', MISSING)
            raise VaultError('read_failed', 'CredReadW failed with Windows error %d.' % error)
        try:
            size = pointer.contents.CredentialBlobSize
            if size > MAX_BLOB:
                raise VaultError('too_large', 'The stored owner key record exceeds %d bytes.' % MAX_BLOB)
            blob = ctypes.string_at(pointer.contents.CredentialBlob, size) if size else b''
        finally:
            self.api.CredFree(pointer)
        return decode(blob)

    def delete(self):
        try:
            ok = self.api.CredDeleteW(self.target, CRED_TYPE_GENERIC, 0)
            error = 0 if ok else self.api.get_last_error()
        except (OSError, ValueError, TypeError, ctypes.ArgumentError) as exc:
            raise VaultError('write_failed', 'CredDeleteW could not be called (' + type(exc).__name__ + ').') from exc
        if not ok:
            if error == ERROR_NOT_FOUND:
                raise VaultError('missing', MISSING)
            raise VaultError('write_failed', 'CredDeleteW failed with Windows error %d.' % error)


class MemoryStore:
    """In-process stand-in for tests: the same interface, one shared registry per target."""
    kind = 'memory'
    entries = {}

    def __init__(self, target, fail_writes=False):
        self.target = check_target(target)
        self.fail_writes = fail_writes

    def write(self, config):
        if self.fail_writes:
            raise VaultError('write_failed', 'This test store was told to refuse writes.')
        MemoryStore.entries[self.target] = encode(config)

    def read(self):
        blob = MemoryStore.entries.get(self.target)
        if blob is None:
            raise VaultError('missing', MISSING)
        return decode(blob)

    def delete(self):
        if MemoryStore.entries.pop(self.target, None) is None:
            raise VaultError('missing', MISSING)


class UnavailableStore:
    """--keeper-store wincred where Credential Manager cannot even be opened: every call fails
    with that reason, so the room falls back to the file and says so instead of not starting."""

    def __init__(self, kind, target, error):
        self.kind = kind
        self.target = target
        self.code = error.code
        self.message = error.message

    def write(self, config):
        raise VaultError(self.code, self.message)

    def read(self):
        raise VaultError(self.code, self.message)

    def delete(self):
        raise VaultError(self.code, self.message)


def open_store(kind, root):
    if kind == 'file':
        return None
    if kind == 'wincred':
        return WinCredStore(target_for(root))
    raise ValueError('Keeper store must be file or wincred.')


def store_for_writing(kind, root):
    """The room's store for --keeper-store: None for file, otherwise the vault, or a store that
    refuses every write when the vault cannot be opened (the room then falls back loudly)."""
    try:
        return open_store(kind, root)
    except VaultError as exc:
        return UnavailableStore(kind, target_for(root), exc)


def store_for_reading(root):
    """The store crew-shane reads. Module level so tests can replace it."""
    if os.name != 'nt':
        raise VaultError('unavailable', UNAVAILABLE)
    return WinCredStore(target_for(root))


def keeper_loader(credential_path, room_root):
    """A zero-argument callable returning keeper's credential for CrewClient(loader=...).

    keeper-credential.json is read inside the same boundary as any credential file. A stub
    sends the loader to the vault; a real credential (the room fell back to the file) is read
    as before. Either way the result must be keeper's own key."""
    def load():
        from .crew_client import CrewError, check_credential_path, load_credential
        path = check_credential_path(credential_path)
        try:
            data = path.read_bytes() if path.stat().st_size <= 4096 else None
        except OSError as exc:
            raise CrewError('config', 'Owner credential file is missing or unreadable; start the room first.') from exc
        if data is None:
            raise CrewError('config', 'Oversized credential file.')
        try:
            found = decode_request(data)
        except ValueError as exc:
            raise CrewError('config', 'Credential file is not valid JSON.') from exc
        if is_stub(found):
            try:
                config = store_for_reading(room_root).read()
            except VaultError as exc:
                raise CrewError('config', exc.message) from exc
        else:
            config = load_credential(path)
        if type(config) is not dict or config.get('actor_id') != 'keeper':
            raise CrewError('identity', 'The owner key source holds a key for another seat; refusing to use it.')
        return config
    return load
