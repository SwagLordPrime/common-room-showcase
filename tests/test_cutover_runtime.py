"""Cutover synthetic regression classes, split without changing test bodies."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import builtins, contextlib, hashlib, importlib.util, io, json, os, shutil, subprocess, unittest
from contextlib import redirect_stdout
from unittest import mock
from tests.helpers import case_dir
from tests.test_cutover_upgrade import (GIT, GIT_ARGS, load_module, git, sha, write,
                                       PASS, FAIL_ONE, FAIL_TWO, World, CutoverBase, FakeCrewTree, crlf,
                                       OLD_ENROLLMENT, GROWN_ENROLLMENT, CHANGED_ENROLLMENT)

class CutoverSafetyTests(CutoverBase):
    def test_under_runtime_only_the_key_files_are_opened_and_only_to_read_them(self):
        # core-cutover-fix: the .runtime check hashes the *-credential.json files; nothing else there is ever opened,
        # and nothing there is ever opened for writing.
        real_open = builtins.open
        touched = []
        def guarded(file, *a, **k):
            if '.runtime' in str(file).replace('\\', '/').split('/'):
                touched.append((str(file).replace('\\', '/'), a[0] if a else k.get('mode', 'r')))
            return real_open(file, *a, **k)
        before = self.state()                      # the test itself reads .runtime to prove it unchanged: not guarded
        with mock.patch.object(builtins, 'open', guarded):
            world = World(suite=(1, FAIL_ONE))
            self.assertEqual(self.cutover(world).run(), 2)
            self.assertEqual(self.cutover(World()).run(), 0)
        self.assertTrue(touched, 'the key file is hashed')
        for path, mode in touched:
            self.assertTrue(path.endswith('-credential.json'), path)
            self.assertEqual(mode, 'rb', path)
        self.assert_runtime_untouched(before)

    def test_the_helper_never_runs_git_clean_or_a_hard_reset(self):
        source = (ROOT / 'crew' / 'cutover_upgrade.py').read_text(encoding='utf-8')
        code_lines = [l for l in source.splitlines() if not l.lstrip().startswith(('#', '"""')) and 'never' not in l.lower()]
        joined = '\n'.join(code_lines)
        for banned in ("'clean'", '"clean"', "'--hard'", '"--hard"', "'rm'", 'rmtree'):
            self.assertNotIn(banned, joined)

    def test_the_helper_imports_only_the_standard_library(self):
        import ast
        tree = ast.parse((ROOT / 'crew' / 'cutover_upgrade.py').read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or '']
                for name in names:
                    self.assertNotIn('common_room', name)
                    self.assertTrue(name.split('.')[0] in sys.stdlib_module_names, name)

    def test_the_default_allowed_file_is_empty_and_ships(self):
        path = ROOT / 'crew' / 'CUTOVER_ALLOWED.txt'
        self.assertTrue(path.is_file())
        self.assertEqual(self.mod.parse_allowed(path), set())
        self.assertEqual(path.read_bytes().strip(), b'')


class RuntimeCheckTests(CutoverBase):
    def without_runtime(self, state):
        return {k: v for k, v in state.items() if k != 'runtime'}

    def assert_rolled_back_except_runtime(self, world, before, code):
        self.assertEqual(code, 2, world.text)
        self.assertEqual(self.without_runtime(self.state()), self.without_runtime(before))
        self.assertIn('Rollback verified', world.text)
        self.assertEqual(world.calls[-1], 'start')                       # the old crew is started again

    def test_changed_runtime_sizes_and_times_and_new_runtime_files_do_not_fail_the_rollback(self):
        live = self.fx['live']
        def room_wrote_things(_tree):
            write(live, '.runtime/handoffs/room.sqlite', b'\x00room state, grown since the snapshot\x00')
            write(live, '.runtime/handoffs/renewal-log.jsonl', '{"renewed": true}\n')
        world, before = World(suite=(1, FAIL_ONE)), self.state()
        world.suite_hook = room_wrote_things
        code = self.cutover(world).run()
        self.assert_rolled_back_except_runtime(world, before, code)
        self.assertNotIn('ROLLBACK DID NOT FULLY RESTORE', world.text)

    def test_a_runtime_file_that_went_missing_still_fails_the_rollback_and_is_named(self):
        world, before = World(suite=(1, FAIL_ONE)), self.state()
        world.suite_hook = lambda tree: (Path(tree) / '.runtime' / 'handoffs' / 'room.sqlite').unlink()
        code = self.cutover(world).run()
        self.assertEqual(code, 3, world.text)
        self.assertIn('.runtime file missing (it existed before the cutover): handoffs/room.sqlite', world.text)
        self.assertEqual(world.calls[-1], 'suite', 'the old crew is not started over a lost room file')

    def test_a_changed_key_file_fails_the_rollback_when_no_crew_was_started(self):
        world, before = World(suite=(1, FAIL_ONE)), self.state()
        world.suite_hook = lambda tree: write(tree, '.runtime/handoffs/k-credential.json', '{"token": "someone else"}')
        code = self.cutover(world).run()
        self.assertEqual(code, 3, world.text)
        self.assertIn('.runtime key file changed although no crew was started: handoffs/k-credential.json', world.text)
        self.assertEqual(world.calls[-1], 'suite')

    def rotating_start(self, world):
        """The new crew's start: like a real room it rewrites every seat's key file and its database, then fails."""
        live, real_start = self.fx['live'], world.start
        def start():
            if 'start' not in world.calls:
                write(live, '.runtime/handoffs/k-credential.json', '{"token": "fresh-key-from-the-new-room"}')
                write(live, '.runtime/handoffs/room.sqlite', b'\x00opened by the new room\x00')
            return real_start()
        return start

    def test_key_files_rewritten_by_a_started_new_crew_are_reported_not_counted_as_a_failure(self):
        world, before = World(), self.state()
        world.start_errors = [RuntimeError('the start script finished but no crew window appeared'), None]
        world.partial_launch = True
        cut = self.cutover(world, start_crew=self.rotating_start(world))
        code = cut.run()
        self.assert_rolled_back_except_runtime(world, before, code)
        self.assertEqual(world.calls, ['confirm', 'stop', 'gone', 'suite', 'start', 'stop', 'gone', 'start'])
        self.assertIn('key files changed because the new crew was started', world.text)
        self.assertIn('handoffs/k-credential.json', world.text)
        self.assertTrue(world.lines[-1].startswith('Note: these .runtime key files changed'), 'repeated as the final message')
        self.assertIn('handoffs/k-credential.json', world.lines[-1])
        # never a key or a key's hash in what is printed or logged
        log = (cut.folder / 'cutover.log').read_text(encoding='utf-8')
        for secret in ('fake-test-token', 'fresh-key-from-the-new-room', sha('{"token": "fake-test-token"}'),
                       sha('{"token": "fresh-key-from-the-new-room"}')):
            self.assertNotIn(secret, world.text)
            self.assertNotIn(secret, log)

    def test_a_key_file_missing_after_a_started_new_crew_still_fails(self):
        world, before = World(), self.state()
        world.start_errors = [RuntimeError('no crew window appeared'), None]
        world.partial_launch = True
        live, real_start = self.fx['live'], world.start
        def start():
            if 'start' not in world.calls:
                (live / '.runtime' / 'handoffs' / 'k-credential.json').unlink()
            return real_start()
        code = self.cutover(world, start_crew=start).run()
        self.assertEqual(code, 3, world.text)
        self.assertIn('.runtime file missing (it existed before the cutover): handoffs/k-credential.json', world.text)

    def leave_file(self, rel, data):
        path = self.fx['live'] / '.runtime' / rel
        write(self.fx['live'], '.runtime/' + rel, data)
        return path.unlink

    def test_a_journal_that_vanishes_after_a_new_crew_start_is_not_a_loss(self):
        # ruling #1071 (1): only a room opening the store again removes it, and that happens once the new crew was started
        cleaned_up = self.leave_file('handoffs/room.sqlite-journal', b'hot journal left by a killed room')
        world, before = World(), self.state()
        world.start_errors = [RuntimeError('no crew window appeared'), None]
        world.partial_launch = True
        real_start = world.start
        def start():
            if 'start' not in world.calls:
                cleaned_up()
            return real_start()
        code = self.cutover(world, start_crew=start).run()
        self.assert_rolled_back_except_runtime(world, before, code)

    def test_a_journal_that_vanishes_with_no_crew_started_still_fails_and_is_named(self):
        # ruling #1071 (1), vesper #1069: with no start nothing opens the store, so its absence means something else removed it
        cleaned_up = self.leave_file('handoffs/room.sqlite-journal', b'hot journal left by a killed room')
        world = World(suite=(1, FAIL_ONE))
        world.suite_hook = lambda tree: cleaned_up()
        code = self.cutover(world).run()
        self.assertEqual(code, 3, world.text)
        self.assertIn('.runtime file missing (it existed before the cutover): handoffs/room.sqlite-journal', world.text)
        self.assertEqual(world.calls[-1], 'suite')

    def test_a_temp_file_that_vanishes_with_no_crew_started_is_not_a_loss(self):
        # ruling #1071 (2): temp files are written then renamed away (crew/relay_ask.py keeps running while the crew is down)
        cleaned_up = self.leave_file('handoffs/relay/claude-room/requests/req-1.0123abcd.tmp', '{}')
        world, before = World(suite=(1, FAIL_ONE)), self.state()
        world.suite_hook = lambda tree: cleaned_up()
        code = self.cutover(world).run()
        self.assert_rolled_back_except_runtime(world, before, code)

    def test_the_record_keeps_key_file_hashes_and_the_runtime_listing(self):
        world = World(suite=(1, FAIL_ONE))
        cut = self.cutover(world)
        cut.run()
        record = json.loads((cut.folder / 'record.json').read_text(encoding='utf-8'))
        self.assertEqual(record['runtime_credentials'], {'handoffs/k-credential.json': sha('{"token": "fake-test-token"}')})
        self.assertIn('handoffs/room.sqlite', record['runtime'])


class EnrollmentRollbackTests(CutoverBase):
    def setUp(self):
        super().setUp()
        self.enrollment = self.fx['live'] / '.runtime' / 'handoffs' / 'enrollment.json'
        write(self.fx['live'], '.runtime/handoffs/enrollment.json', OLD_ENROLLMENT)
        self.old_crew_saw = []
        self.store_members = {'orchestra': ['keeper', 'claude-room']}   # the fake room.sqlite grant, shared by every World
        self.record_bytes = None

    def crew(self, world, new_start_fails=True, writes=GROWN_ENROLLMENT, new_crew=True):
        """Crews like 5d5ee83's. The first start is the new crew (when new_crew): it writes `writes` (GROWN by default) and,
        only when that is GROWN, grows the store's grant to GROWN's members (activation.py:244-253), then by default
        fails to verify. Every later start is the old crew: it records what it found and starts only when each scope's
        members in the file have the store's members as a prefix; otherwise it refuses as activation.py:250 does."""
        live, real_start = self.fx['live'], world.start
        if new_start_fails:
            world.start_errors = [RuntimeError('no crew window appeared'), None]
            world.partial_launch = True
        def start():
            if new_crew and 'start' not in world.calls:
                self.record_bytes = (self.cutover_folder() / 'record.json').read_bytes() if self.cutover_folder() else None
                write(live, '.runtime/handoffs/enrollment.json', writes)
                if writes == GROWN_ENROLLMENT:
                    self.store_members = {'orchestra': ['keeper', 'claude-room', 'dot']}
                write(live, '.runtime/handoffs/room.sqlite', b'\x00members event written by the new room\x00')
                return real_start()
            found = self.enrollment.read_bytes().decode('utf-8', 'replace')
            self.old_crew_saw.append(found)
            try:
                scopes = {row['id']: row['members'] for row in json.loads(found)['authorities']}
                fits = all(scopes.get(k, [])[:len(v)] == v for k, v in self.store_members.items())
            except Exception:
                fits = False
            if not fits:
                world.calls.append('start')
                raise RuntimeError("The room's scope orchestra differs from the enrollment; a scope may only gain members")
            return real_start()
        return start

    def cutover_folder(self):
        return getattr(self, '_cut', None) and self._cut.folder

    def run_cut(self, world, **kw):
        self._cut = self.cutover(world, **kw)
        return self._cut, self._cut.run()

    def outcome(self, cut):
        return json.loads((cut.folder / 'enrollment-rollback.json').read_text(encoding='utf-8'))

    def test_a_room_grown_by_the_new_crew_then_rolled_back_starts_on_the_old_code(self):
        world = World()
        cut, code = self.run_cut(world, start_crew=self.crew(world))
        self.assertEqual(code, 2, world.text)
        self.assertEqual(self.old_crew_saw, [GROWN_ENROLLMENT])
        self.assertEqual(self.enrollment.read_text(encoding='utf-8'), GROWN_ENROLLMENT)
        self.assertIn('Kept .runtime\\handoffs\\enrollment.json as the new crew left it (it only adds actors dot; members of orchestra: dot)', world.text)
        self.assertLess(world.text.index('Kept .runtime'), world.text.index('Rollback verified'))
        self.assertNotIn('Restored', world.text)
        self.assertEqual(self.outcome(cut)['outcome'], 'kept')
        self.assertEqual(self.outcome(cut)['added_actors'], ['dot'])
        self.assertEqual((cut.folder / 'record.json').read_bytes(), self.record_bytes)
        self.assertEqual(world.calls, ['confirm', 'stop', 'gone', 'suite', 'start', 'stop', 'gone', 'start'])
        self.assertEqual(self.state()['head'], self.fx['head'])

    def test_the_copy_is_saved_beside_the_backup_before_the_switch_and_named_in_the_record(self):
        world = World()
        cut = self.cutover(world, start_crew=self.crew(world))
        cut.run()
        saved = cut.folder / 'runtime-enrollment.json'
        self.assertEqual(saved.read_text(encoding='utf-8'), OLD_ENROLLMENT)
        record = json.loads((cut.folder / 'record.json').read_text(encoding='utf-8'))
        self.assertEqual(record['enrollment'], {'runtime_path': 'handoffs/enrollment.json', 'saved_as': 'runtime-enrollment.json',
                                                'sha256': sha(OLD_ENROLLMENT)})
        self.assertLess(world.text.index('saved .runtime\\handoffs\\enrollment.json'), world.text.index('Step 5: switching'))

    def test_a_success_keeps_the_copy_and_names_it_in_the_last_message(self):
        world = World()
        cut = self.cutover(world, start_crew=self.crew(world, new_start_fails=False))
        self.assertEqual(cut.run(), 0, world.text)
        self.assertEqual(self.enrollment.read_text(encoding='utf-8'), GROWN_ENROLLMENT, 'a success leaves the grown room alone')
        self.assertEqual((cut.folder / 'runtime-enrollment.json').read_text(encoding='utf-8'), OLD_ENROLLMENT)
        self.assertIn('runtime-enrollment.json', world.lines[-1])
        self.assertIn('runtime-enrollment.json', (cut.folder / 'cutover.log').read_text(encoding='utf-8'))

    def test_the_manual_rollback_keeps_a_grown_enrollment_too(self):
        world = World()
        cut, code = self.run_cut(world, start_crew=self.crew(world, new_start_fails=False))
        self.assertEqual(code, 0, world.text)
        record_bytes = (cut.folder / 'record.json').read_bytes()
        undo_world = World()
        undo = self.cutover(undo_world, start_crew=self.crew(undo_world))
        undo_world.calls.append('start')               # the new crew already ran: every start from here is the old crew
        undo_world.start_errors = []
        code = undo.rollback_from(cut.folder)
        self.assertEqual(code, 0, undo_world.text)
        self.assertEqual(self.old_crew_saw, [GROWN_ENROLLMENT])
        self.assertEqual(self.enrollment.read_text(encoding='utf-8'), GROWN_ENROLLMENT)
        self.assertIn('Kept .runtime\\handoffs\\enrollment.json as the new crew left it', undo_world.text)
        self.assertLess(undo_world.text.index('Kept .runtime'), undo_world.text.index('Rollback verified'))
        self.assertNotIn('Restored', undo_world.text)
        self.assertEqual(self.outcome(cut)['outcome'], 'kept')
        self.assertEqual((cut.folder / 'record.json').read_bytes(), record_bytes)

    def test_an_unchanged_enrollment_is_never_written(self):
        world, before = World(suite=(1, FAIL_ONE)), self.state()
        code = self.cutover(world).run()
        self.assertEqual(code, 2, world.text)
        self.assert_runtime_untouched(before)          # same size, same time, same bytes: the restore did not write
        self.assertNotIn('Restored .runtime', world.text)

    def test_a_damaged_saved_copy_stops_the_rollback_before_the_old_crew_starts(self):
        world = World()
        cut = self.cutover(world, start_crew=self.crew(world))
        real_stop = world.stop
        def stop():
            if cut.folder is not None and 'suite' in world.calls:
                (cut.folder / 'runtime-enrollment.json').write_text('{"tampered": true}\n', encoding='utf-8')
            return real_stop()
        cut.stop_crew = stop
        code = cut.run()
        self.assertEqual(code, 3, world.text)
        self.assertIn('runtime-enrollment.json', world.text)
        self.assertIn('does not match its recorded hash', world.text)
        self.assertEqual(self.old_crew_saw, [], 'the old crew is not started over a room it cannot open')

    def test_no_enrollment_file_means_nothing_is_saved_or_put_back(self):
        self.enrollment.unlink()
        world = World(suite=(1, FAIL_ONE))
        cut = self.cutover(world)
        self.assertEqual(cut.run(), 2, world.text)
        record = json.loads((cut.folder / 'record.json').read_text(encoding='utf-8'))
        self.assertIsNone(record['enrollment'])
        self.assertFalse((cut.folder / 'runtime-enrollment.json').exists())
        self.assertFalse(self.enrollment.exists())

    def test_a_backup_made_before_this_change_still_rolls_back(self):
        world = World()
        cut = self.cutover(world, start_crew=self.crew(world, new_start_fails=False))
        self.assertEqual(cut.run(), 0, world.text)
        record_path = cut.folder / 'record.json'
        record = json.loads(record_path.read_text(encoding='utf-8'))
        del record['enrollment']                       # the shape every earlier cutover wrote
        record_path.write_text(json.dumps(record), encoding='utf-8')
        undo_world = World()
        code = self.cutover(undo_world).rollback_from(cut.folder)
        self.assertEqual(code, 0, undo_world.text)
        self.assertEqual(self.enrollment.read_text(encoding='utf-8'), GROWN_ENROLLMENT, 'nothing saved, so nothing put back')

    # ---- SW3-RB: what is kept and what is restored -------------------------------------------------------------

    def assert_restored(self, world, cut, code, expected=2):
        self.assertEqual(code, expected, world.text)
        self.assertEqual(self.enrollment.read_text(encoding='utf-8'), OLD_ENROLLMENT)
        self.assertIn('Restored .runtime\\handoffs\\enrollment.json from the copy saved before the switch', world.text)
        self.assertNotIn('Kept .runtime', world.text)
        self.assertEqual(self.outcome(cut)['outcome'], 'restored')

    def test_a_changed_row_is_restored(self):
        world = World()
        cut, code = self.run_cut(world, start_crew=self.crew(world, writes=CHANGED_ENROLLMENT))
        self.assert_restored(world, cut, code)
        self.assertEqual(self.old_crew_saw, [OLD_ENROLLMENT])

    def test_bytes_that_do_not_decode_or_parse_are_restored(self):
        for bad in (b'\xff\xfe{', OLD_ENROLLMENT.encode('utf-8')[:-12]):
            with self.subTest(bad=bad[:12]):
                world = World()
                cut, code = self.run_cut(world, start_crew=self.crew(world, writes=bad))
                self.assert_restored(world, cut, code)
                self.assertIn('not unambiguous UTF-8 JSON', self.outcome(cut)['why_not_kept'])

    def test_a_manual_rollback_after_a_run_that_left_a_changed_file_restores_it(self):
        world = World()
        cut, code = self.run_cut(world, start_crew=self.crew(world, new_start_fails=False, writes=CHANGED_ENROLLMENT))
        self.assertEqual(code, 0, world.text)
        undo_world = World()
        undo = self.cutover(undo_world, start_crew=self.crew(undo_world))
        undo_world.calls.append('start')
        undo_world.start_errors = []
        code = undo.rollback_from(cut.folder)
        self.assertEqual(code, 0, undo_world.text)
        self.assertEqual(self.enrollment.read_bytes(), (cut.folder / 'runtime-enrollment.json').read_bytes())
        self.assertIn('Restored .runtime\\handoffs\\enrollment.json from the copy saved before the switch', undo_world.text)

    def test_a_grown_file_with_no_new_start_is_restored(self):
        world = World(suite=(1, FAIL_ONE))
        world.suite_hook = lambda live: write(live, '.runtime/handoffs/enrollment.json', GROWN_ENROLLMENT)
        cut, code = self.run_cut(world, start_crew=self.crew(world, new_start_fails=False, new_crew=False))
        self.assert_restored(world, cut, code)
        self.assertIn('no new crew was started', self.outcome(cut)['why_not_kept'])
        self.assertEqual(self.old_crew_saw, [OLD_ENROLLMENT])

    def test_wrong_type_shapes_are_restored_and_nothing_escapes(self):
        grown = json.loads(GROWN_ENROLLMENT)
        shapes = {'top-level list': [grown],
                  'actors as {}': dict(grown, actors={}),
                  'a member 1': dict(grown, authorities=[dict(grown['authorities'][0], members=['keeper', 'claude-room', 1])]),
                  'members as a string': dict(grown, authorities=[dict(grown['authorities'][0], members='keeper')]),
                  'version "1"': dict(grown, version='1')}
        for name, shape in shapes.items():
            with self.subTest(shape=name):
                world = World()
                cut, code = self.run_cut(world, start_crew=self.crew(world, writes=json.dumps(shape)))
                self.assert_restored(world, cut, code)

    def test_a_failed_outcome_write_is_said_and_the_rollback_still_keeps(self):
        real_replace = os.replace
        def replace(src, dst, *a, **k):
            if str(dst).endswith('enrollment-rollback.json'):
                raise OSError('synthetic outcome write failure')
            return real_replace(src, dst, *a, **k)
        world = World()
        with mock.patch.object(self.mod.os, 'replace', replace):
            cut, code = self.run_cut(world, start_crew=self.crew(world))
        self.assertEqual(code, 2, world.text)
        self.assertIn('Kept .runtime\\handoffs\\enrollment.json as the new crew left it', world.text)
        self.assertIn('The outcome could not be written', world.text)
        self.assertEqual(self.enrollment.read_text(encoding='utf-8'), GROWN_ENROLLMENT)


class EnrollmentRulePinTests(unittest.TestCase):
    """The tool's keep rule (enrollment_growth) against the room's own read path: common_room.activation read_json, then
    merge_enrollment(saved, it). Each fixture gets the same keep-or-refuse from both."""
    def setUp(self):
        self.mod = load_module()
        from common_room import activation
        self.activation = activation

    def room_accepts(self, folder, saved):
        try:
            current = self.activation.read_json(folder, 'enrollment.json', None)
            self.activation.merge_enrollment(saved, current)
            return True
        except Exception:
            return False

    def tool_keeps(self, saved_raw, path):
        try:
            self.mod.enrollment_growth(saved_raw, path)
            return True
        except Exception:
            return False

    def test_the_tool_and_the_room_agree_on_every_fixture(self):
        shipped_raw = (Path(__file__).resolve().parents[1] / 'crew' / 'enrollment-crew-2026-09-24.json').read_bytes()
        base = json.loads(OLD_ENROLLMENT)
        def grown(**kw): return json.dumps(dict(json.loads(GROWN_ENROLLMENT), **kw))
        g = json.loads(GROWN_ENROLLMENT)
        shipped = json.loads(shipped_raw)
        seat = {'id': 'test-seat', 'role': 'worker', 'label': 'Test seat', 'session': 'fixture'}
        shipped_plus = dict(shipped, actors=shipped['actors'] + [seat],
                            authorities=[dict(a, members=a['members'] + ['test-seat']) if a['id'] == 'orchestra' else a for a in shipped['authorities']])
        accept = {
            'members appended': (OLD_ENROLLMENT, GROWN_ENROLLMENT),
            'a new actor only': (OLD_ENROLLMENT, json.dumps(dict(base, actors=g['actors']))),
            'a new scope with keeper': (OLD_ENROLLMENT, json.dumps(dict(base, authorities=base['authorities'] + [{'id': 'extra', 'members': ['keeper'], 'scope': 'Extra'}]))),
            'reformatted and reordered': (OLD_ENROLLMENT, json.dumps(dict(reversed(list(g.items()))), indent=3)),
            'the shipped enrollment plus a test-made seat': (shipped_raw.decode('utf-8'), json.dumps(shipped_plus)),
        }
        refuse = {
            'a row removed': (GROWN_ENROLLMENT, OLD_ENROLLMENT),
            'a label changed': (OLD_ENROLLMENT, CHANGED_ENROLLMENT),
            'a role changed': (OLD_ENROLLMENT, json.dumps(dict(base, actors=[dict(base['actors'][0], role='reviewer')]))),
            'scope text changed': (OLD_ENROLLMENT, json.dumps(dict(base, authorities=[dict(base['authorities'][0], scope='Other')]))),
            'members inserted': (OLD_ENROLLMENT, grown(authorities=[dict(g['authorities'][0], members=['keeper', 'dot', 'claude-room'])])),
            'members reordered': (OLD_ENROLLMENT, json.dumps(dict(base, authorities=[dict(base['authorities'][0], members=['claude-room', 'keeper'])]))),
            'an extra field': (OLD_ENROLLMENT, grown(note='x')),
            'version 2': (OLD_ENROLLMENT, grown(version=2)),
            'an unenrolled member': (OLD_ENROLLMENT, json.dumps(dict(base, authorities=[dict(base['authorities'][0], members=['keeper', 'claude-room', 'nobody'])]))),
            'a legacy id': (OLD_ENROLLMENT, json.dumps(dict(base, actors=base['actors'] + [dict(seat, id='astra')]))),
            'over 16384 bytes': (OLD_ENROLLMENT, GROWN_ENROLLMENT + ' ' * 16400),
        }
        either = {
            'a BOM': (OLD_ENROLLMENT, '\ufeff' + GROWN_ENROLLMENT),
            'a duplicate key': (OLD_ENROLLMENT, GROWN_ENROLLMENT.replace('"version": 1', '"version": 1, "version": 1')),
            'NaN': (OLD_ENROLLMENT, GROWN_ENROLLMENT.replace('"version": 1', '"version": 1, "x": NaN')),
        }
        for group, cases in (('accept', accept), ('refuse', refuse), ('either', either)):
            for name, (saved_text, current_text) in cases.items():
                with self.subTest(group=group, case=name):
                    folder = case_dir('sw3rb-pin')
                    (folder / 'enrollment.json').write_bytes(current_text.encode('utf-8'))
                    saved = self.activation.validate_enrollment(json.loads(saved_text))
                    room = self.room_accepts(folder, saved)
                    tool = self.tool_keeps(saved_text.encode('utf-8'), folder / 'enrollment.json')
                    self.assertEqual(tool, room, name)
                    if group != 'either':
                        self.assertEqual(room, group == 'accept', name)


if __name__ == '__main__':
    unittest.main()
