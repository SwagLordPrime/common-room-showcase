"""Piece V (autonomy build, improvement #8): the owner key leaves the shared folder.

With a keeper store the room writes keeper's key to the store (Windows Credential Manager
on the PC, an in-process MemoryStore here) and leaves only a stub with no key in
keeper-credential.json. Every other reader of that file refuses the stub with one clear
message; only `crew --as keeper` (crew-shane) reads the store. If the store refuses, the
room falls back to today's file and says so loudly.

Fictional seats only, under .checks. Tokens are read from the store or from the room object,
never printed, and every test that runs a command asserts no token appears in its output.
The real advapi32 calls can only run on Windows; here the ctypes marshalling is exercised
against a fake advapi32 built from CFUNCTYPE callbacks, and the real round trip is skipped.
"""
import contextlib, ctypes, io, json, os, sys, threading, time, unittest, uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.helpers import case_dir, read_unless_locked
from common_room import activation, crew, keeper_vault
from common_room.crew_client import CrewClient, CrewError, load_credential
from common_room.keeper_vault import MemoryStore, VaultError, target_for

CONFIG = {
    'version': 1,
    'actors': [
        {'id': 'vault-writer', 'role': 'worker', 'label': 'Vault writer', 'session': 'fixture-vault-w'},
        {'id': 'vault-reader', 'role': 'reviewer', 'label': 'Vault reviewer', 'session': 'fixture-vault-r'},
    ],
    'authorities': [
        {'id': 'vault-one', 'members': ['keeper', 'vault-writer', 'vault-reader'], 'scope': 'Fictional vault scope'},
    ],
}
SEATS = ['vault-writer', 'vault-reader']
STUB_REFUSAL = ('The owner key is kept in Windows Credential Manager, not in this folder. '
                'Shane: run crew-shane on the PC.')
VAULT_LINE = 'Owner key: Windows Credential Manager (sign in to the board with: crew-shane login)'
UNAVAILABLE = 'The owner key is in Windows Credential Manager; run crew-shane on the PC.'


class Clock:
    def __init__(self):
        self.now = float(int(time.time()))
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.now

    def advance(self, seconds):
        with self.lock:
            self.now += seconds


def files_containing(root, secret):
    """Byte scan of every file under the room root (sqlite, WAL, relay folders, all of it)."""
    needle = secret.encode('utf-8')
    return sorted(p.relative_to(root).as_posix() for p in Path(root).rglob('*') if p.is_file() and needle in read_unless_locked(p))


def login_refusal(origin, token):
    from common_room.participant import Participant
    try:
        Participant(origin, token).login()
    except ValueError as exc:
        return str(exc)
    return None


def reader_for(calls=None):
    def store_for_reading(root):
        if calls is not None:
            calls.append(Path(root))
        return MemoryStore(target_for(root))
    return store_for_reading


class OSErrorStore:
    kind = 'memory'

    def __init__(self, target):
        self.target = target

    def write(self, config):
        raise PermissionError('fixture: access denied')


class VaultTestCase(unittest.TestCase):
    def vault_room(self, store=None, root=None, selected=SEATS):
        root = root or case_dir('vault')
        store = store if store is not None else MemoryStore(target_for(root))
        room = activation.start_room(root, CONFIG, selected, keeper_store=store)
        self.addCleanup(room.close)
        return room, store

    def read_json(self, path):
        return json.loads(Path(path).read_text(encoding='utf-8'))

    def assert_vault_state(self, room, store):
        """Stub in the folder, key only in the store, status file honest, key live."""
        config = store.read()
        self.assertEqual(config, room.credentials['keeper'])
        self.assertEqual(config['actor_id'], 'keeper')
        self.assertEqual(files_containing(room.root, config['token']), [], 'the owner key must be in no file')
        self.assertEqual(self.read_json(room.root / 'keeper-credential.json'), keeper_vault.stub_for(store.target))
        status = {'version': 1, 'store': 'memory', 'target': store.target, 'fallback_reason': None}
        self.assertEqual(self.read_json(room.root / 'keeper-store.json'), status)
        self.assertEqual(room.keeper_store_status, status)
        self.assertIsNone(login_refusal(room.server.origin, config['token']), 'the stored key must be the live key')
        return config

    def keeper_cli(self, room, *args, reader=None, stdin=None, json_mode=True, argv_head=None):
        argv = list(argv_head or ['--as', 'keeper', '--room-root', str(room.root)])
        if json_mode:
            argv.append('--json')
        argv += list(args)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {'COMMON_ROOM_SEAT': 'keeper'}))
            stack.enter_context(mock.patch('sys.stdin', new=stdin or SimpleNamespace(isatty=lambda: True,
                                                                                       readline=lambda: '\n')))
            if reader is not None:
                stack.enter_context(mock.patch.object(keeper_vault, 'store_for_reading', reader))
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            code = crew.main(argv)
        return code, out.getvalue(), err.getvalue()

    def last_line(self, err):
        lines = [line for line in err.strip().splitlines() if line]
        self.assertTrue(lines, 'expected an error line on stderr')
        return lines[-1]


class VaultModuleTests(VaultTestCase):
    def test_target_is_one_per_room_root(self):
        root = case_dir('vault-target')
        target = target_for(root)
        self.assertRegex(target, r'^CommonRoom/keeper/[0-9a-f]{16}$')
        self.assertEqual(target_for(root / 'sub' / '..'), target, 'one room root, one target, however it is spelled')
        self.assertEqual(target_for(str(root)), target)
        self.assertNotEqual(target_for(case_dir('vault-target')), target)

    def test_stub_shape_holds_no_key(self):
        stub = keeper_vault.stub_for('CommonRoom/keeper/0123456789abcdef')
        self.assertEqual(stub, {'stored_in': 'windows-credential-manager', 'target': 'CommonRoom/keeper/0123456789abcdef',
                                'note': 'The owner key is not kept in this folder. Shane: run crew-shane on the PC.'})
        self.assertEqual(keeper_vault.STUB_KEY, 'stored_in')
        self.assertTrue(keeper_vault.is_stub(stub))
        self.assertFalse(keeper_vault.is_stub({'origin': 'http://127.0.0.1:1', 'token': 'x', 'actor_id': 'keeper',
                                               'expires_at': 1}))
        for other in (None, [], 'stored_in', ['stored_in']):
            self.assertFalse(keeper_vault.is_stub(other))
        self.assertEqual(keeper_vault.STUB_REFUSAL, STUB_REFUSAL)

    def test_memory_store_round_trip_and_refusals(self):
        store = MemoryStore('CommonRoom/test/' + uuid.uuid4().hex)
        config = {'origin': 'http://127.0.0.1:50263', 'token': 'fixture-' + uuid.uuid4().hex, 'actor_id': 'keeper',
                  'expires_at': 1790000000.0}
        with self.assertRaises(VaultError) as ctx:
            store.read()
        self.assertEqual(ctx.exception.code, 'missing')
        store.write(config)
        self.assertEqual(store.read(), config)
        self.assertEqual(MemoryStore(store.target).read(), config, 'one registry per target')
        for bad, code in [(dict(config, actor_id='vault-writer'), 'invalid'), (dict(config, extra=1), 'invalid'),
                          ({k: v for k, v in config.items() if k != 'token'}, 'invalid'),
                          (dict(config, token=''), 'invalid'), (dict(config, expires_at=True), 'invalid'),
                          (dict(config, token='x' * 2600), 'too_large'), ('not a dict', 'invalid')]:
            with self.assertRaises(VaultError) as ctx:
                store.write(bad)
            self.assertEqual(ctx.exception.code, code, bad if type(bad) is str else sorted(bad))
            self.assertNotIn(config['token'], ctx.exception.message)
        self.assertEqual(store.read(), config, 'a refused write leaves the stored key alone')
        failing = MemoryStore(store.target, fail_writes=True)
        with self.assertRaises(VaultError) as ctx:
            failing.write(dict(config, token='other-' + uuid.uuid4().hex))
        self.assertEqual(ctx.exception.code, 'write_failed')
        self.assertEqual(store.read(), config)
        store.delete()
        with self.assertRaises(VaultError) as ctx:
            store.read()
        self.assertEqual(ctx.exception.code, 'missing')
        with self.assertRaises(VaultError) as ctx:
            store.delete()
        self.assertEqual(ctx.exception.code, 'missing')
        # What comes back out of a store is checked as strictly as what goes in.
        from common_room.protocol import canonical
        for raw, code in [(canonical(dict(config, actor_id='vault-writer')).encode(), 'invalid'),
                          (b'{"token": "a", "token": "b"}', 'invalid'), (b'not json', 'invalid')]:
            MemoryStore.entries[store.target] = raw
            with self.assertRaises(VaultError) as ctx:
                store.read()
            self.assertEqual(ctx.exception.code, code, raw[:40])
        for bad_target in ('', 'x' * 257, 'Common\x00Room', None, b'CommonRoom/test'):
            with self.assertRaises(VaultError) as ctx:
                MemoryStore(bad_target)
            self.assertEqual(ctx.exception.code, 'invalid')

    def test_open_store_kinds(self):
        root = case_dir('vault-open')
        self.assertIsNone(keeper_vault.open_store('file', root))
        with self.assertRaises(ValueError):
            keeper_vault.open_store('registry', root)
        if os.name == 'nt':
            self.assertEqual(keeper_vault.open_store('wincred', root).target, target_for(root))

    @unittest.skipIf(os.name == 'nt', 'off-Windows behaviour')
    def test_imports_off_windows_and_wincred_is_unavailable(self):
        import subprocess
        from tests.helpers import ROOT
        done = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c',
                               'import common_room.keeper_vault as v; print(v.WinCredStore.__name__)'],
                              cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        with self.assertRaises(keeper_vault.VaultError) as ctx:
            keeper_vault.WinCredStore('CommonRoom/test/' + uuid.uuid4().hex)
        self.assertEqual(ctx.exception.code, 'unavailable')
        root = case_dir('vault-open')
        with self.assertRaises(keeper_vault.VaultError) as ctx:
            keeper_vault.open_store('wincred', root)
        self.assertEqual(ctx.exception.code, 'unavailable')
        with self.assertRaises(keeper_vault.VaultError) as ctx:
            keeper_vault.store_for_reading(root)
        self.assertEqual((ctx.exception.code, ctx.exception.message), ('unavailable', UNAVAILABLE))
        # --keeper-store wincred where the vault cannot even be opened: writes fail, so the room falls back.
        store = keeper_vault.store_for_writing('wincred', root)
        self.assertEqual((store.kind, store.target), ('wincred', target_for(root)))
        with self.assertRaises(keeper_vault.VaultError) as ctx:
            store.write({'origin': 'http://127.0.0.1:1', 'token': 't', 'actor_id': 'keeper', 'expires_at': 1})
        self.assertEqual(ctx.exception.code, 'unavailable')
        self.assertIsNone(keeper_vault.store_for_writing('file', root))

    @unittest.skipUnless(ctypes.sizeof(ctypes.c_void_p) == 8, 'checks the 64-bit wincred.h layout')
    def test_credentialw_matches_the_x64_wincred_layout(self):
        C = keeper_vault.CREDENTIALW
        self.assertEqual(ctypes.sizeof(C), 80)
        self.assertEqual([name for name, _ in C._fields_],
                         ['Flags', 'Type', 'TargetName', 'Comment', 'LastWritten', 'CredentialBlobSize',
                          'CredentialBlob', 'Persist', 'AttributeCount', 'Attributes', 'TargetAlias', 'UserName'])
        self.assertEqual({name: getattr(C, name).offset for name, _ in C._fields_},
                         {'Flags': 0, 'Type': 4, 'TargetName': 8, 'Comment': 16, 'LastWritten': 24,
                          'CredentialBlobSize': 32, 'CredentialBlob': 40, 'Persist': 48, 'AttributeCount': 52,
                          'Attributes': 56, 'TargetAlias': 64, 'UserName': 72})
        self.assertEqual(ctypes.sizeof(keeper_vault.FILETIME), 8)
        self.assertEqual((keeper_vault.CRED_TYPE_GENERIC, keeper_vault.CRED_PERSIST_LOCAL_MACHINE,
                          keeper_vault.ERROR_NOT_FOUND, keeper_vault.MAX_BLOB), (1, 2, 1168, 2560))


class FakeAdvapi:
    """advapi32 stand-in with the real prototypes (CFUNCTYPE), so WinCredStore's ctypes marshalling runs here."""

    def __init__(self):
        C = keeper_vault.CREDENTIALW
        self.vault, self.freed, self.writes, self.keep = {}, [], [], []
        self.error = 0
        self.fail_write = None
        self.oversize = False
        write = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(C), ctypes.c_uint32)
        read = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                ctypes.POINTER(ctypes.POINTER(C)))
        delete = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32)
        free = ctypes.CFUNCTYPE(None, ctypes.c_void_p)
        self.api = SimpleNamespace(CredWriteW=write(self._write), CredReadW=read(self._read),
                                   CredDeleteW=delete(self._delete), CredFree=free(self._free),
                                   get_last_error=lambda: self.error)

    def _write(self, pcred, flags):
        if self.fail_write:
            self.error = self.fail_write
            return 0
        c = pcred.contents
        record = {'Flags': c.Flags, 'Type': c.Type, 'TargetName': c.TargetName, 'Persist': c.Persist,
                  'AttributeCount': c.AttributeCount, 'UserName': c.UserName, 'call_flags': flags,
                  'blob': ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize)}
        self.writes.append(record)
        self.vault[(c.TargetName, c.Type)] = record
        return 1

    def _read(self, target, cred_type, flags, out):
        record = self.vault.get((target, cred_type))
        if record is None:
            self.error = 1168
            return 0
        blob = record['blob'] + (b' ' * 3000 if self.oversize else b'')
        buffer = ctypes.create_string_buffer(blob, len(blob))
        cred = keeper_vault.CREDENTIALW()
        cred.Type = cred_type
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        self.keep.append((buffer, cred))
        out[0] = ctypes.pointer(cred)
        return 1

    def _delete(self, target, cred_type, flags):
        if self.vault.pop((target, cred_type), None) is None:
            self.error = 1168
            return 0
        return 1

    def _free(self, pointer):
        self.freed.append(pointer)


class WinCredMarshallingTests(unittest.TestCase):
    """The ctypes code path of WinCredStore against a fake advapi32. NOT the real Credential Manager."""

    def test_write_read_delete_through_the_prototypes(self):
        fake = FakeAdvapi()
        target = 'CommonRoom/test/' + uuid.uuid4().hex
        store = keeper_vault.WinCredStore(target, _api=fake.api)
        config = {'origin': 'http://127.0.0.1:50263', 'token': 'fixture-' + uuid.uuid4().hex, 'actor_id': 'keeper',
                  'expires_at': 1790000000.5}
        store.write(config)
        record = fake.writes[-1]
        self.assertEqual((record['Flags'], record['Type'], record['TargetName'], record['Persist'],
                          record['AttributeCount'], record['call_flags']), (0, 1, target, 2, 0, 0))
        from common_room.protocol import canonical
        self.assertEqual(record['blob'], canonical(config).encode('utf-8'))
        self.assertEqual(store.read(), config)
        self.assertEqual(len(fake.freed), 1, 'every successful CredReadW is paired with CredFree')
        fake.oversize = True
        with self.assertRaises(VaultError) as ctx:
            store.read()
        self.assertEqual(ctx.exception.code, 'too_large')
        self.assertEqual(len(fake.freed), 2, 'CredFree runs even when the stored blob is refused')
        fake.oversize = False
        store.delete()
        for call in (store.read, store.delete):
            with self.assertRaises(VaultError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, 'missing')
        fake.fail_write = 5
        with self.assertRaises(VaultError) as ctx:
            store.write(config)
        self.assertEqual(ctx.exception.code, 'write_failed')
        self.assertIn('5', ctx.exception.message)
        self.assertNotIn(config['token'], ctx.exception.message)
        with self.assertRaises(VaultError) as ctx:
            store.write(dict(config, token='x' * 2600))
        self.assertEqual(ctx.exception.code, 'too_large')

        def broken(*args):
            raise OSError('fixture: advapi32 call failed')
        for name, call, code in [('CredWriteW', lambda: store.write(config), 'write_failed'),
                                 ('CredReadW', store.read, 'read_failed'),
                                 ('CredDeleteW', store.delete, 'write_failed')]:
            with mock.patch.object(fake.api, name, broken):
                with self.assertRaises(VaultError) as ctx:
                    call()
            self.assertEqual(ctx.exception.code, code, name)

    def test_read_failure_other_than_not_found_is_read_failed(self):
        fake = FakeAdvapi()
        store = keeper_vault.WinCredStore('CommonRoom/test/' + uuid.uuid4().hex, _api=fake.api)

        def refuse(target, cred_type, flags, out):
            fake.error = 1312
            return 0
        fake.api.CredReadW = type(fake.api.CredReadW)(refuse)
        with self.assertRaises(VaultError) as ctx:
            store.read()
        self.assertEqual(ctx.exception.code, 'read_failed')
        self.assertIn('1312', ctx.exception.message)


@unittest.skipUnless(os.name == 'nt', 'Windows Credential Manager exists only on Windows')
class WinCredRoundTripTests(unittest.TestCase):
    def test_round_trip_on_a_throwaway_target(self):
        target = 'CommonRoom/test/' + uuid.uuid4().hex
        store = keeper_vault.WinCredStore(target)

        def cleanup():
            try:
                store.delete()
            except VaultError:
                pass
        self.addCleanup(cleanup)
        config = {'origin': 'http://127.0.0.1:50263', 'token': 'fixture-' + uuid.uuid4().hex, 'actor_id': 'keeper',
                  'expires_at': 1790000000.0}
        store.write(config)
        self.assertEqual(store.read(), config)
        store.delete()
        with self.assertRaises(VaultError) as ctx:
            store.read()
        self.assertEqual(ctx.exception.code, 'missing')


class VaultRoomTests(VaultTestCase):
    def test_start_room_keeps_the_owner_key_out_of_every_file(self):
        room, store = self.vault_room()
        config = self.assert_vault_state(room, store)
        for seat in SEATS:
            seat_file = self.read_json(room.root / (seat + '-credential.json'))
            self.assertEqual(seat_file, room.credentials[seat], 'other seats keep today\'s real credential files')
            self.assertEqual(files_containing(room.root, seat_file['token']), [seat + '-credential.json'])
        lines = activation.room_lines(room)
        text = '\n'.join(lines)
        self.assertIn(VAULT_LINE, lines[0].splitlines())
        self.assertNotIn('Owner login file', text)
        self.assertNotIn(config['token'], text)
        out = io.StringIO()
        with mock.patch('builtins.input', side_effect=EOFError), redirect_stdout(out):
            activation.run_console(room)
        self.assertIn(VAULT_LINE, out.getvalue().splitlines())
        self.assertNotIn('Owner login file', out.getvalue())
        self.assertNotIn(config['token'], out.getvalue())

    def test_file_store_default_is_unchanged_and_the_scan_sees_a_file_key(self):
        root = case_dir('vault-file')
        room = activation.start_room(root, CONFIG, SEATS)
        self.addCleanup(room.close)
        token = room.credentials['keeper']['token']
        self.assertEqual(files_containing(root, token), ['keeper-credential.json'], 'the byte scan must see a key in a file')
        self.assertEqual(self.read_json(root / 'keeper-credential.json'), room.credentials['keeper'])
        self.assertFalse((root / 'keeper-store.json').exists())
        self.assertIsNone(room.keeper_store_status)
        self.assertIn('Owner login file: ' + str(root / 'keeper-credential.json'), activation.room_lines(room)[0].splitlines())

    def test_rotate_keeper_follows_the_same_rule(self):
        room, store = self.vault_room()
        first = self.assert_vault_state(room, store)
        seat_before = (room.root / 'vault-writer-credential.json').read_bytes()
        room.rotate('keeper')
        second = self.assert_vault_state(room, store)
        self.assertNotEqual(second['token'], first['token'])
        self.assertEqual(files_containing(room.root, first['token']), [])
        self.assertTrue(login_refusal(room.server.origin, first['token']).startswith('unauthenticated'))
        self.assertEqual((room.root / 'vault-writer-credential.json').read_bytes(), seat_before)
        # A non-owner rotation still rewrites that seat's own file, as before.
        room.rotate('vault-writer')
        self.assertEqual(self.read_json(room.root / 'vault-writer-credential.json'), room.credentials['vault-writer'])
        self.assert_vault_state(room, store)

    def test_rotate_falls_back_when_the_vault_refuses_then_recovers(self):
        room, store = self.vault_room()
        self.assert_vault_state(room, store)
        store.fail_writes = True
        room.rotate('keeper')
        config = room.credentials['keeper']
        self.assertEqual(self.read_json(room.root / 'keeper-credential.json'), config)
        status = self.read_json(room.root / 'keeper-store.json')
        self.assertEqual(status['store'], 'memory')
        self.assertIn('write_failed', status['fallback_reason'])
        self.assertEqual(room.keeper_store_status, status)
        self.assertIn('WARNING: the owner key could not be stored in Windows Credential Manager (' +
                      status['fallback_reason'] + '); it is in keeper-credential.json as before.',
                      activation.room_lines(room)[0].splitlines())
        store.fail_writes = False
        room.rotate('keeper')
        self.assert_vault_state(room, store)
        self.assertEqual(files_containing(room.root, config['token']), [], 'recovery replaces the fallback file')

    def test_failing_vault_falls_back_to_the_file_with_a_warning(self):
        for store_class in (lambda t: MemoryStore(t, fail_writes=True), OSErrorStore):
            root = case_dir('vault-fallback')
            store = store_class(target_for(root))
            room, _ = self.vault_room(store=store, root=root)
            config = room.credentials['keeper']
            self.assertEqual(self.read_json(root / 'keeper-credential.json'), config)
            self.assertEqual(files_containing(root, config['token']), ['keeper-credential.json'])
            status = self.read_json(root / 'keeper-store.json')
            self.assertEqual(set(status), {'version', 'store', 'target', 'fallback_reason'})
            self.assertEqual((status['version'], status['store'], status['target']), (1, 'memory', store.target))
            self.assertIsInstance(status['fallback_reason'], str)
            self.assertTrue(status['fallback_reason'])
            self.assertEqual(room.keeper_store_status, status)
            warning = ('WARNING: the owner key could not be stored in Windows Credential Manager (' +
                       status['fallback_reason'] + '); it is in keeper-credential.json as before.')
            lines = activation.room_lines(room)
            self.assertIn(warning, lines[0].splitlines())
            self.assertNotIn(VAULT_LINE, '\n'.join(lines))
            self.assertNotIn(config['token'], '\n'.join(lines))
            out = io.StringIO()
            with mock.patch('builtins.input', side_effect=EOFError), redirect_stdout(out):
                activation.run_console(room)
            self.assertIn(warning, out.getvalue().splitlines())
            self.assertNotIn(config['token'], out.getvalue())
            with self.assertRaises(VaultError):
                MemoryStore(store.target).read()
        self.assertIn('PermissionError', status['fallback_reason'])

    def test_linked_status_file_is_refused_before_any_key_is_published(self):
        root = case_dir('vault-linked-status')
        elsewhere = case_dir('vault-elsewhere') / 'status.json'
        elsewhere.write_text('{}', encoding='utf-8')
        try:
            os.symlink(elsewhere, root / 'keeper-store.json')
        except (OSError, NotImplementedError):
            self.skipTest('this host cannot create a file symlink')
        store = MemoryStore(target_for(root))
        with self.assertRaises(ValueError):
            activation.start_room(root, CONFIG, SEATS, keeper_store=store)
        self.assertEqual(sorted(p.name for p in root.glob('*-credential.json')), [])
        with self.assertRaises(VaultError):
            store.read()
        self.assertEqual(elsewhere.read_text(encoding='utf-8'), '{}')

    def test_crew_shane_works_in_fallback_mode_without_the_vault(self):
        root = case_dir('vault-fallback-cli')
        room, _ = self.vault_room(store=MemoryStore(target_for(root), fail_writes=True), root=root)

        def never(root):
            raise AssertionError('a real keeper file must not send the loader to the vault')
        code, out, err = self.keeper_cli(room, 'whoami', reader=never)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['actor'], 'keeper')
        self.assertNotIn(room.credentials['keeper']['token'], out + err)

    def downgrade_warning(self, where, root):
        # fix2-board (VERIFY8A INFO): "was set to keep", true after a vault start that fell back to the
        # file too, and the way to stop the warning when the vault was turned off on purpose.
        return ('WARNING: the owner key is back in keeper-credential.json in this folder: this room was started '
                'without the owner key vault, and an earlier start was set to keep the key in ' + where + '. To keep '
                'it out of the folder, close this room and start it with Start Common Room Crew.cmd. If you turned '
                'the vault off on purpose, delete ' + str(Path(root) / 'keeper-store.json') + ' and this warning '
                'stops.')

    def assert_downgrade_said(self, room, where):
        """The warning is the first thing under the origin in room_lines and in the run_console window."""
        warning = self.downgrade_warning(where, room.root)
        token = room.credentials['keeper']['token']
        first = activation.room_lines(room)[0].splitlines()
        self.assertEqual(first[1:], [warning, 'Owner login file: ' + str(room.root / 'keeper-credential.json')])
        self.assertNotIn(token, '\n'.join(activation.room_lines(room)))
        out = io.StringIO()
        with mock.patch('builtins.input', side_effect=EOFError), redirect_stdout(out):
            activation.run_console(room)
        self.assertIn(warning, out.getvalue().splitlines())
        self.assertNotIn(token, out.getvalue())

    def test_file_mode_after_a_vault_run_rewrites_the_status(self):
        root = case_dir('vault-then-file')
        room, store = self.vault_room(root=root)
        self.assert_vault_state(room, store)
        room.close()
        again = activation.start_room(root, selected=SEATS)
        self.addCleanup(again.close)
        self.assertEqual(self.read_json(root / 'keeper-credential.json'), again.credentials['keeper'])
        # fix-board L3: it still starts, but the status file and the window both say what happened.
        self.assertEqual(self.read_json(root / 'keeper-store.json'),
                         {'version': 1, 'store': 'file', 'target': None, 'fallback_reason': None,
                          'downgraded_from': 'memory'})
        self.assert_downgrade_said(again, 'the memory key store')

    def test_the_downgrade_warning_names_credential_manager_and_stays_until_a_vault_start(self):
        root = case_dir('vault-then-file-wincred')
        activation.start_room(root, CONFIG, SEATS).close()
        # What a real vault start on the PC leaves behind (the kind is all that matters here).
        (root / 'keeper-store.json').write_text(json.dumps({'version': 1, 'store': 'wincred', 'target': target_for(root),
                                                            'fallback_reason': None}), encoding='utf-8')
        for _ in range(2):          # a second file-mode start still says it: the key is still in the folder
            room = activation.start_room(root, selected=SEATS)
            try:
                self.assertEqual(self.read_json(root / 'keeper-store.json')['downgraded_from'], 'wincred')
                self.assert_downgrade_said(room, 'Windows Credential Manager')
            finally:
                room.close()
        room, store = self.vault_room(root=root)
        self.assert_vault_state(room, store)
        self.assertNotIn('WARNING', '\n'.join(activation.room_lines(room)))

    def test_after_a_vault_start_that_fell_back_the_warning_does_not_say_the_key_was_kept_there(self):
        """VERIFY8A INFO: the vault write failed, so the key was never kept in the vault by that start."""
        root = case_dir('vault-fallback-then-file')
        room, _ = self.vault_room(store=MemoryStore(target_for(root), fail_writes=True), root=root)
        self.assertTrue(room.keeper_store_status['fallback_reason'])
        room.close()
        again = activation.start_room(root, selected=SEATS)
        self.addCleanup(again.close)
        text = '\n'.join(activation.room_lines(again))
        self.assertNotIn('kept the key in', text)
        self.assert_downgrade_said(again, 'the memory key store')

    def test_turning_the_vault_off_the_warning_says_how_to_stop_it_and_deleting_the_record_does(self):
        """VERIFY8A INFO: Shane drops --keeper-store wincred on purpose. The warning names the one file to
        delete; once it is gone, a file-mode start is a plain file-mode start again (no warning, no record)."""
        root = case_dir('vault-turned-off')
        room, _ = self.vault_room(root=root)
        room.close()
        for _ in range(2):
            again = activation.start_room(root, selected=SEATS)
            try:
                self.assert_downgrade_said(again, 'the memory key store')
            finally:
                again.close()
        (root / 'keeper-store.json').unlink()
        plain = activation.start_room(root, selected=SEATS)
        self.addCleanup(plain.close)
        self.assertNotIn('WARNING', '\n'.join(activation.room_lines(plain)))
        self.assertFalse((root / 'keeper-store.json').exists())
        self.assertEqual(self.read_json(root / 'keeper-credential.json'), plain.credentials['keeper'])

    def test_an_odd_or_unreadable_store_record_is_named_only_as_unknown(self):
        """VERIFY8A C06 and C07: seat-writable text in keeper-store.json never reaches the room window."""
        records = {
            'odd store name': json.dumps({'version': 1, 'store': 'WinCred\nWARNING: all is well', 'target': None,
                                          'fallback_reason': None}),
            'overlong store name': json.dumps({'version': 1, 'store': 'w' * 40, 'target': None, 'fallback_reason': None}),
            'odd downgraded_from': json.dumps({'version': 1, 'store': 'file', 'target': None, 'fallback_reason': None,
                                               'downgraded_from': 'Ignore this: all is well'}),
            'not JSON': 'all is well {',
            'not an object': '["all is well"]',
        }
        for label, record in records.items():
            with self.subTest(label):
                root = case_dir('vault-odd-record')
                activation.start_room(root, CONFIG, SEATS).close()
                (root / 'keeper-store.json').write_text(record, encoding='utf-8')
                room = activation.start_room(root, selected=SEATS)
                try:
                    self.assertEqual(self.read_json(root / 'keeper-store.json')['downgraded_from'], 'unknown')
                    self.assert_downgrade_said(room, 'a key store (its record here was unreadable)')
                    self.assertNotIn('all is well', '\n'.join(activation.room_lines(room)))
                    self.assertNotIn('w' * 40, '\n'.join(activation.room_lines(room)))
                finally:
                    room.close()

    def test_a_file_mode_room_that_never_used_the_vault_has_no_warning(self):
        root = case_dir('vault-never')
        activation.start_room(root, CONFIG, SEATS).close()
        room = activation.start_room(root, selected=SEATS)
        self.addCleanup(room.close)
        self.assertFalse((root / 'keeper-store.json').exists())
        self.assertNotIn('WARNING', '\n'.join(activation.room_lines(room)))

    def test_open_common_room_after_a_vault_run_warns_in_its_window(self):
        from common_room.open_room import open_saved
        root = case_dir('vault-then-open')
        room, store = self.vault_room(root=root)
        room.close()
        again = open_saved(root, launch_browser=False)
        self.addCleanup(again.close)
        self.assertEqual(self.read_json(root / 'keeper-store.json')['downgraded_from'], 'memory')
        self.assert_downgrade_said(again, 'the memory key store')

    def test_two_renewal_cycles_keep_the_key_out_of_every_file(self):
        root = case_dir('vault-renew')
        store = MemoryStore(target_for(root))
        clock, seen, lines = Clock(), [], []

        def wait(seconds):
            descriptor = self.read_json(root / 'connection.json')
            config = store.read()
            seen.append(config)
            self.assertEqual(files_containing(root, config['token']), [])
            self.assertEqual(self.read_json(root / 'keeper-credential.json'), keeper_vault.stub_for(store.target))
            self.assertEqual(self.read_json(root / 'keeper-store.json')['fallback_reason'], None)
            self.assertEqual(config['origin'], descriptor['origin'])
            self.assertIsNone(login_refusal(descriptor['origin'], config['token']))
            clock.advance(seconds)

        cycles = activation.run_renewing(root, SEATS, CONFIG, lifetime=600, margin=60, clock=clock, wait=wait,
                                         max_cycles=2, log=lines.append, keeper_store=store)
        self.assertEqual(len(cycles), 2)
        self.assertEqual(len({c['token'] for c in seen}), 2, 'each cycle issues a fresh owner key into the vault')
        for config in seen:
            self.assertEqual(files_containing(root, config['token']), [])
            self.assertNotIn(config['token'], '\n'.join(lines))
        self.assertEqual(sum(VAULT_LINE in line.splitlines() for line in lines), 2)
        self.assertNotIn('Owner login file', '\n'.join(lines))

    def test_cli_keeper_store_option(self):
        root = case_dir('vault-cli')
        with mock.patch.object(activation, 'start_room') as start, mock.patch.object(activation, 'run_console'):
            activation.main(['--root', str(root), '--keeper-store', 'wincred'])
        store = start.call_args.kwargs['keeper_store']
        self.assertEqual((store.kind, store.target), ('wincred', target_for(root)))
        if os.name != 'nt':
            with self.assertRaises(VaultError) as ctx:
                store.write({'origin': 'http://127.0.0.1:1', 'token': 't', 'actor_id': 'keeper', 'expires_at': 1})
            self.assertEqual(ctx.exception.code, 'unavailable')
        with mock.patch.object(activation, 'run_renewing') as renew:
            activation.main(['--root', str(root), '--headless', '--renew', '--keeper-store', 'wincred'])
        self.assertEqual(renew.call_args.kwargs['keeper_store'].kind, 'wincred')
        with mock.patch.object(activation, 'start_room') as start, mock.patch.object(activation, 'run_console'):
            activation.main(['--root', str(root), '--keeper-store', 'file'])
            activation.main(['--root', str(root)])
        for call in start.call_args_list:
            self.assertNotIn('keeper_store', call.kwargs)
        with mock.patch.object(activation, 'start_room') as start, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                activation.main(['--root', str(root), '--keeper-store', 'registry'])
        self.assertEqual(ctx.exception.code, 2)
        start.assert_not_called()
        self.assertEqual(list(root.iterdir()), [])


class StubRefusalTests(VaultTestCase):
    def test_crew_credential_on_the_stub_is_refused_clearly(self):
        room, store = self.vault_room()
        token = store.read()['token']
        code, out, err = self.keeper_cli(room, 'whoami', argv_head=['--credential', str(room.root / 'keeper-credential.json')])
        self.assertEqual(code, 1)
        self.assertEqual(self.last_line(err), 'config: ' + STUB_REFUSAL)
        self.assertNotIn(token, out + err)

    def test_load_credential_crew_client_relay_and_worker_refuse_the_stub(self):
        room, store = self.vault_room()
        token = store.read()['token']
        stub = room.root / 'keeper-credential.json'
        with self.assertRaises(CrewError) as ctx:
            load_credential(stub)
        self.assertEqual((ctx.exception.code, str(ctx.exception)), ('config', STUB_REFUSAL))
        with self.assertRaises(CrewError) as ctx:
            CrewClient(stub, sleep=lambda s: None).state()
        self.assertEqual((ctx.exception.code, str(ctx.exception)), ('config', STUB_REFUSAL))
        from common_room.relay import Relay, RelayError
        with self.assertRaises(RelayError):
            Relay(room.root, ['keeper'])
        from common_room import ollama_worker
        logs = []
        settings = ollama_worker.parse_settings(['--credential', str(stub), '--model', 'fixture-model',
                                                 '--read-root', str(case_dir('vault-read-root'))])
        result = ollama_worker.build_worker(settings, log=logs.append).run_once()
        self.assertEqual((result['status'], result['code']), ('error', 'config'))
        self.assertIn(STUB_REFUSAL, repr(logs))
        self.assertNotIn(token, repr(logs))

    def test_participant_cli_refuses_the_stub(self):
        room, store = self.vault_room()
        from common_room import participant
        with mock.patch.object(sys, 'argv', ['participant', '--credential', str(room.root / 'keeper-credential.json'),
                                             'inbox']):
            with self.assertRaises(ValueError) as ctx:
                participant.main()
        self.assertEqual(str(ctx.exception), STUB_REFUSAL)


class KeeperThroughVaultTests(VaultTestCase):
    def test_as_keeper_reads_the_vault(self):
        room, store = self.vault_room()
        token = store.read()['token']
        calls = []
        code, out, err = self.keeper_cli(room, 'whoami', reader=reader_for(calls))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['actor'], 'keeper')
        self.assertEqual(calls, [room.root])
        transcript = [out, err]
        code, out, err = self.keeper_cli(room, 'intake', '--scope', 'vault-one', '--to', 'vault-writer',
                                         '--title', 'Vault fixture task', '--body', 'Do the fictional thing.',
                                         '--acceptance', 'Looks right.', reader=reader_for())
        self.assertEqual(code, 0, err)
        task_id = json.loads(out)['task_id']
        transcript += [out, err]
        code, out, err = self.keeper_cli(room, 'tasks', '--all', reader=reader_for())
        self.assertEqual(code, 0, err)
        self.assertIn(task_id, [t['id'] for t in json.loads(out)['tasks']])
        transcript += [out, err]
        plan = case_dir('vault-plan') / 'plan.json'
        plan.write_text(json.dumps({'protocol': 'crew-plan/1', 'plan_id': 'vault-smoke', 'title': 'Vault smoke plan',
                                    'scope': 'vault-one', 'tasks': [
                                        {'key': 'one', 'to': 'vault-writer', 'title': 'Smoke task',
                                         'body': 'Fictional smoke body.', 'acceptance': 'Looks right.'}]}),
                        encoding='utf-8')
        code, out, err = self.keeper_cli(room, 'plan-approve', str(plan), reader=reader_for(),
                                         stdin=SimpleNamespace(isatty=lambda: True, readline=lambda: 'yes\n'))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['tasks']['one']['status'], 'delivered')
        transcript += [out, err]
        self.assertNotIn(token, '\n'.join(transcript))

    def test_keeper_client_follows_a_rotation_through_the_vault(self):
        room, store = self.vault_room()
        path = room.root / 'keeper-credential.json'
        with mock.patch.object(keeper_vault, 'store_for_reading', reader_for()):
            client = CrewClient(path, expected_actor='keeper', loader=keeper_vault.keeper_loader(path, room.root),
                                sleep=lambda s: None)
            self.assertEqual(client.state()['actor']['id'], 'keeper')
            old = client._participant.token
            room.rotate('keeper')
            self.assertEqual(client.state()['actor']['id'], 'keeper')
        self.assertNotEqual(client._participant.token, old)
        self.assertEqual(client._participant.token, store.read()['token'])

    def test_vault_unavailable_or_empty_is_a_clear_refusal(self):
        room, store = self.vault_room()
        token = store.read()['token']
        if os.name != 'nt':
            code, out, err = self.keeper_cli(room, 'whoami')
            self.assertEqual(code, 1)
            self.assertEqual(self.last_line(err), 'config: ' + UNAVAILABLE)
            self.assertNotIn(token, out + err)
        empty = 'CommonRoom/test/' + uuid.uuid4().hex
        code, out, err = self.keeper_cli(room, 'whoami', reader=lambda root: MemoryStore(empty))
        self.assertEqual(code, 1)
        self.assertTrue(self.last_line(err).startswith('config: '), err)
        self.assertIn('Windows Credential Manager', err)
        self.assertNotIn(token, out + err)

    def test_loader_refuses_another_seats_key_and_paths_outside_the_package(self):
        room, store = self.vault_room()
        path = room.root / 'keeper-credential.json'
        forged = dict(room.credentials['vault-writer'])
        with mock.patch.object(keeper_vault, 'store_for_reading', lambda root: SimpleNamespace(read=lambda: forged)):
            with self.assertRaises(CrewError) as ctx:
                keeper_vault.keeper_loader(path, room.root)()
        self.assertEqual(ctx.exception.code, 'identity')
        self.assertNotIn(forged['token'], str(ctx.exception))
        outside = Path('/tmp' if os.name != 'nt' else os.environ.get('TEMP', 'C:\\')) / 'keeper-credential.json'
        with self.assertRaises(CrewError) as ctx:
            keeper_vault.keeper_loader(outside, room.root)()
        self.assertEqual(ctx.exception.code, 'config')
        import inspect
        self.assertEqual(len(inspect.signature(keeper_vault.keeper_loader(path, room.root)).parameters), 0)
        # The same boundary as every credential file: a linked folder is refused before anything is read.
        linked = case_dir('vault-linked')
        try:
            os.symlink(room.root, linked / 'room', target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('this host cannot create a directory symlink')
        with mock.patch.object(keeper_vault, 'store_for_reading', reader_for()):
            self.assertEqual(keeper_vault.keeper_loader(path, room.root)()['actor_id'], 'keeper')
            with self.assertRaises(CrewError) as ctx:
                keeper_vault.keeper_loader(linked / 'room' / 'keeper-credential.json', room.root)()
        self.assertEqual((ctx.exception.code, str(ctx.exception)), ('config', 'Linked credential paths refused.'))


class LoginTests(VaultTestCase):
    def test_login_copies_exactly_the_key_and_clears_it_after_enter(self):
        room, store = self.vault_room()
        token = store.read()['token']
        clip, at_enter = [], []
        stdin = SimpleNamespace(isatty=lambda: True, readline=lambda: at_enter.append(list(clip)) or '\n')
        with mock.patch.object(crew, 'copy_to_clipboard', clip.append):
            code, out, err = self.keeper_cli(room, 'login', reader=reader_for(), stdin=stdin, json_mode=False)
        self.assertEqual(code, 0, err)
        self.assertEqual(at_enter, [[token]], 'the key is on the clipboard while Shane pastes it')
        self.assertEqual(clip, [token, ''], 'and cleared after Enter')
        self.assertIn(room.server.origin, out + err)
        self.assertIn('paste it into Room access key', out + err)
        self.assertNotIn(token, out + err)

    def test_login_clears_the_clipboard_when_interrupted(self):
        room, store = self.vault_room()
        token = store.read()['token']
        clip = []

        def interrupted():
            raise KeyboardInterrupt
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(crew, 'copy_to_clipboard', clip.append):
            with self.assertRaises(KeyboardInterrupt):
                with redirect_stdout(out), redirect_stderr(err):
                    self.keeper_cli(room, 'login', reader=reader_for(),
                                    stdin=SimpleNamespace(isatty=lambda: True, readline=interrupted))
        self.assertEqual(clip, [token, ''])
        self.assertNotIn(token, out.getvalue() + err.getvalue())

    def test_login_is_for_the_owner_seat_only(self):
        room, store = self.vault_room()
        clip = []
        argv = ['--as', 'vault-writer', '--room-root', str(room.root), 'login']
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(crew, 'copy_to_clipboard', clip.append), \
                mock.patch.dict(os.environ, {'COMMON_ROOM_SEAT': 'vault-writer'}), \
                mock.patch('sys.stdin', new=SimpleNamespace(isatty=lambda: True, readline=lambda: '\n')), \
                redirect_stdout(out), redirect_stderr(err):
            code = crew.main(argv)
        self.assertEqual(code, 1)
        self.assertTrue(self.last_line(err.getvalue()).startswith('forbidden: '), err.getvalue())
        self.assertEqual(clip, [])
        self.assertNotIn(room.credentials['vault-writer']['token'], out.getvalue() + err.getvalue())

    def test_login_needs_an_interactive_terminal(self):
        root = case_dir('vault-login-tty')
        room = activation.start_room(root, CONFIG, SEATS)
        self.addCleanup(room.close)
        clip = []
        with mock.patch.object(crew, 'copy_to_clipboard', clip.append):
            code, out, err = self.keeper_cli(room, 'login', argv_head=['--credential', str(root / 'keeper-credential.json')],
                                             stdin=SimpleNamespace(isatty=lambda: False, readline=lambda: '\n'))
        self.assertEqual(code, 1)
        self.assertTrue(self.last_line(err).startswith('config: '), err)
        self.assertEqual(clip, [])
        self.assertNotIn(room.credentials['keeper']['token'], out + err)

    @unittest.skipIf(os.name == 'nt', 'off-Windows behaviour')
    def test_login_off_windows_is_a_config_refusal(self):
        off = 'login uses the Windows clipboard; run crew-shane login on the PC.'
        with self.assertRaises(CrewError) as ctx:
            crew.copy_to_clipboard('fixture')
        self.assertEqual((ctx.exception.code, str(ctx.exception)), ('config', off))
        room, store = self.vault_room()
        token = store.read()['token']
        code, out, err = self.keeper_cli(room, 'login', reader=reader_for(), json_mode=False)
        self.assertEqual(code, 1)
        self.assertEqual(self.last_line(err), 'config: ' + off)
        self.assertNotIn(token, out + err)

    def test_clipboard_goes_through_system32_clip_stdin(self):
        """The Windows branch with subprocess faked: full System32 path, key on stdin, never in argv."""
        calls, code = [], [0]

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return SimpleNamespace(returncode=code[0])
        secret = 'fixture-' + uuid.uuid4().hex
        with mock.patch.object(crew.os, 'name', 'nt'), mock.patch.dict(os.environ, {'SystemRoot': 'C:\\Windows'}), \
                mock.patch.object(crew.subprocess, 'run', fake_run):
            crew.copy_to_clipboard(secret)
            crew.copy_to_clipboard('')
            code[0] = 1
            with self.assertRaises(CrewError) as ctx:
                crew.copy_to_clipboard(secret)
            self.assertEqual(ctx.exception.code, 'config')
            with self.assertRaises(CrewError) as ctx:
                crew.copy_to_clipboard('caf\u00e9')
            self.assertEqual(ctx.exception.code, 'invalid')
        self.assertEqual(len(calls), 3)
        for argv, kwargs in calls:
            self.assertEqual(argv, [os.path.join('C:\\Windows', 'System32', 'clip.exe')])
            self.assertNotIn(secret, ' '.join(argv))
            self.assertIsNot(kwargs.get('shell'), True)
        self.assertEqual([kwargs['input'] for _, kwargs in calls], [secret.encode('ascii'), b'', secret.encode('ascii')])


if __name__ == '__main__':
    unittest.main()
