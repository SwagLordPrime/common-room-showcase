"""File relay: a Cowork chat acts as a named room seat through request/response files.

Real room via common_room.activation.start_room; the room's own store, protocol and
crew_client boundaries are exercised through the relay, not re-implemented here.
"""
import json, os, shutil, unittest
from tests.helpers import case_dir, symlinks_or_skip

CONFIG = {'version': 1, 'actors': [
    {'id': 'relay-writer', 'role': 'worker', 'label': 'Fictional writer', 'session': 'fixture-writer'},
    {'id': 'relay-reader', 'role': 'reviewer', 'label': 'Fictional reviewer', 'session': 'fixture-reviewer'},
    {'id': 'relay-other', 'role': 'worker', 'label': 'Other scope worker', 'session': 'fixture-other'},
    {'id': 'relay-reviewer-two', 'role': 'reviewer', 'label': 'Second scope reviewer', 'session': 'fixture-reviewer-two'}],
    'authorities': [
    {'id': 'relay-one', 'members': ['keeper', 'relay-writer', 'relay-reader'], 'scope': 'Fictional first scope'},
    {'id': 'relay-two', 'members': ['keeper', 'relay-other', 'relay-reviewer-two'], 'scope': 'Fictional second scope'}]}


class RelayTestCase(unittest.TestCase):
    def start(self, root=None, selected=('relay-writer', 'relay-reader', 'relay-other'), config=CONFIG):
        from common_room.activation import start_room
        room = start_room(root or case_dir('relay'), enrollment=config, selected=list(selected))
        self.addCleanup(room.close)
        return room

    def intake_task(self, room, recipient='relay-writer', authority='relay-one', ref='req-1', **over):
        msg = dict(title='Fixture task', body='Do the fictional thing', acceptance='Inspect the result',
                   recipient=recipient, authority_id=authority, kind='handoff.record')
        msg.update(over)
        owner = room.store.people['keeper']
        result = room.store.execute(owner, 'intake', {'source_namespace': 'estate', 'external_reference': ref, 'message': msg}, 'intake-' + ref)
        task_id = result['task']['id']
        room.store.execute(owner, 'deliver', {'task_id': task_id}, 'deliver-' + ref)
        return task_id

    def relay(self, room, actors, **kw):
        from common_room.relay import Relay
        return Relay(room.root, actors, clock=room.server.clock, **kw)

    def write_request(self, room, actor, name, body):
        path = room.root / 'relay' / actor / 'requests' / name
        path.write_text(json.dumps(body), encoding='utf-8')
        return path

    def response(self, room, actor, request_id):
        path = room.root / 'relay' / actor / 'responses' / ('resp-' + request_id + '.json')
        return json.loads(path.read_text(encoding='utf-8'))

    def events(self, room):
        return room.store._snapshot(room.store.people['keeper'])[0]


class RelayFlowTests(RelayTestCase):
    def test_worker_ack_claim_stage_finish_through_files(self):
        room = self.start()
        task = self.intake_task(room)
        relay = self.relay(room, ['relay-writer'])

        self.write_request(room, 'relay-writer', 'ack1.json',
            {'request_id': 'ack-1', 'kind': 'command', 'action': 'ack', 'payload': {'task_id': task}})
        relay.process_once()
        ack = self.response(room, 'relay-writer', 'ack-1')
        self.assertTrue(ack['ok'])
        self.assertEqual(ack['result']['task']['status'], 'acknowledged')

        self.write_request(room, 'relay-writer', 'claim1.json',
            {'request_id': 'claim-1', 'kind': 'command', 'action': 'claim', 'payload': {'task_id': task, 'seconds': 900}})
        relay.process_once()
        claim = self.response(room, 'relay-writer', 'claim-1')
        self.assertTrue(claim['ok'])
        fence = claim['result']['task']['lease']['fence']

        self.write_request(room, 'relay-writer', 'stage1.json',
            {'request_id': 'stage-1', 'kind': 'handoff', 'operation': 'stage_text',
             'payload': {'task_id': task, 'fence': fence, 'text': 'Fictional result body'}})
        relay.process_once()
        stage = self.response(room, 'relay-writer', 'stage-1')
        self.assertTrue(stage['ok'])
        ref = {'path': stage['result']['path'], 'sha256': stage['result']['sha256']}

        self.write_request(room, 'relay-writer', 'finish1.json',
            {'request_id': 'finish-1', 'kind': 'command', 'action': 'finish',
             'payload': {'task_id': task, 'fence': fence, 'body': 'Done', 'refs': [ref]}})
        relay.process_once()
        finish = self.response(room, 'relay-writer', 'finish-1')
        self.assertTrue(finish['ok'])
        self.assertEqual(finish['result']['task']['status'], 'ready_for_review')

        # 2026-09-24 (gate R3): the relay never moves requests any more (the move was the N1
        # primitive); it answers each file once and the chat tidies its own folder.
        requests_dir = room.root / 'relay' / 'relay-writer' / 'requests'
        self.assertEqual({p.name for p in requests_dir.iterdir()}, {'ack1.json', 'claim1.json', 'stage1.json', 'finish1.json'})
        events = len(self.events(room))
        relay.process_once()
        self.assertEqual(len(self.events(room)), events)

    def test_reviewer_accepts_through_files(self):
        room = self.start()
        task = self.intake_task(room)
        worker = room.store.people['relay-writer']
        room.store.execute(worker, 'ack', {'task_id': task}, 'ack')
        lease = room.store.execute(worker, 'claim', {'task_id': task, 'seconds': 900}, 'claim')['task']['lease']
        from common_room.handoff_api import handoff_operation
        staged = handoff_operation(room.server, worker, 'stage_text', {'task_id': task, 'fence': lease['fence'], 'text': 'Fictional body'})
        room.store.execute(worker, 'finish', {'task_id': task, 'fence': lease['fence'], 'body': 'Done', 'refs': [staged]}, 'finish')

        relay = self.relay(room, ['relay-reader'])
        self.write_request(room, 'relay-reader', 'review1.json',
            {'request_id': 'review-1', 'kind': 'command', 'action': 'review',
             'payload': {'task_id': task, 'accepted': True, 'note': 'Looks right'}})
        relay.process_once()
        resp = self.response(room, 'relay-reader', 'review-1')
        self.assertTrue(resp['ok'])
        self.assertEqual(resp['result']['task']['status'], 'done')

    def test_resent_request_is_idempotent(self):
        room = self.start()
        relay = self.relay(room, ['relay-writer'])
        before = len(self.events(room))
        self.write_request(room, 'relay-writer', 'note1.json',
            {'request_id': 'note-1', 'kind': 'command', 'action': 'note',
             'payload': {'authority_id': 'relay-one', 'body': 'hello'}})
        relay.process_once()
        after_first = len(self.events(room))
        self.assertEqual(after_first, before + 1)
        self.assertTrue(self.response(room, 'relay-writer', 'note-1')['ok'])

        self.write_request(room, 'relay-writer', 'note1-again.json',
            {'request_id': 'note-1', 'kind': 'command', 'action': 'note',
             'payload': {'authority_id': 'relay-one', 'body': 'hello'}})
        relay.process_once()
        after_second = len(self.events(room))
        self.assertEqual(after_second, after_first)
        self.assertTrue(self.response(room, 'relay-writer', 'note-1')['ok'])

    def test_state_json_is_scoped_to_the_actor(self):
        room = self.start()
        task_one = self.intake_task(room, recipient='relay-writer', authority='relay-one', ref='scope-one')
        task_two = self.intake_task(room, recipient='relay-other', authority='relay-two', ref='scope-two')
        relay = self.relay(room, ['relay-writer'], state_every=0)
        relay.process_once()
        state = json.loads((room.root / 'relay' / 'relay-writer' / 'relay-state.json').read_text(encoding='utf-8'))
        ids = {t['id'] for t in state['tasks']}
        self.assertIn(task_one, ids)
        self.assertNotIn(task_two, ids)
        self.assertEqual(state['relay_actor'], 'relay-writer')
        self.assertIn('relay_written_at', state)
        self.assertEqual(state['relay_written_at'], state['server_time'])


class RelayRefusalTests(RelayTestCase):
    def test_owner_actions_refused_before_sending(self):
        # M9(d): prove ZERO requests reached the room for intake and pause —
        # not just that the response happens to read "forbidden", since the
        # room refuses those with the same code on its own (a relay-side gate
        # that silently did nothing would be indistinguishable by response
        # alone). A spy on the client's own `command` method, plus the event
        # count, together prove the relay itself never sent them.
        room = self.start()
        relay = self.relay(room, ['relay-writer'])
        calls = []
        real_command = relay.clients['relay-writer'].command

        def spy(action, payload, request_id):
            calls.append(action)
            return real_command(action, payload, request_id)
        relay.clients['relay-writer'].command = spy

        before = len(self.events(room))
        for action, payload in [
            ('intake', {'source_namespace': 'estate', 'external_reference': 'sneaky', 'message': {}}),
            ('pause', {'authority_id': 'relay-one'}),
            ('grant', {'id': 'x', 'members': [], 'capabilities': [], 'root_id': 'x', 'scope': 'x'}),
        ]:
            name = action + '.json'
            self.write_request(room, 'relay-writer', name,
                {'request_id': action + '-req', 'kind': 'command', 'action': action, 'payload': payload})
        relay.process_once()
        for action in ['intake', 'pause', 'grant']:
            resp = self.response(room, 'relay-writer', action + '-req')
            self.assertFalse(resp['ok'])
            self.assertEqual(resp['error'], 'forbidden')
        after = len(self.events(room))
        self.assertEqual(after, before)
        self.assertEqual(calls, [], 'client.command must never be called for an owner action')

    def test_malformed_oversize_duplicate_key_and_bad_id_get_error_responses_and_nothing_is_sent(self):
        room = self.start()
        relay = self.relay(room, ['relay-writer'])
        before = len(self.events(room))
        reqdir = room.root / 'relay' / 'relay-writer' / 'requests'
        (reqdir / 'garbage.json').write_bytes(b'not json at all {')
        (reqdir / 'huge.json').write_bytes(json.dumps({'request_id': 'huge-1', 'kind': 'state', 'pad': 'x' * 20000}).encode('utf-8'))
        (reqdir / 'dupkeys.json').write_bytes(b'{"request_id":"dup-1","request_id":"dup-2","kind":"state"}')
        (reqdir / 'badid.json').write_bytes(json.dumps({'request_id': 'bad id!', 'kind': 'state'}).encode('utf-8'))
        relay.process_once()
        for stem in ['garbage', 'huge', 'dupkeys', 'badid']:
            resp_path = room.root / 'relay' / 'relay-writer' / 'responses' / ('resp-invalid-' + stem + '.json')
            self.assertTrue(resp_path.exists(), stem)
            resp = json.loads(resp_path.read_text(encoding='utf-8'))
            self.assertFalse(resp['ok'])
            self.assertIn('error', resp)
        self.assertEqual(len(list(reqdir.iterdir())), 4, 'answered, never moved')
        after = len(self.events(room))
        self.assertEqual(after, before)

    def test_keeper_and_bad_names_refused_at_startup(self):
        from common_room.relay import Relay, RelayError
        room = self.start()
        for bad_actors in (['keeper'], ['Bad-Name'], ['1abc'], ['a/b'], ['relay-writer', 'relay-writer'], ['keeper', 'relay-writer']):
            with self.assertRaises(RelayError):
                Relay(room.root, bad_actors)

    def test_symlinked_request_or_folder_refused(self):
        symlinks_or_skip()
        from common_room.relay import Relay, RelayError
        room = self.start()
        relay_dir = room.root / 'relay'
        relay_dir.mkdir(exist_ok=True)
        elsewhere = case_dir('relay-elsewhere')
        (relay_dir / 'relay-writer').symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(RelayError):
            Relay(room.root, ['relay-writer'])
        (relay_dir / 'relay-writer').unlink()

        relay = self.relay(room, ['relay-writer'])
        outside = case_dir('relay-outside-target')
        secret = outside / 'secret.json'
        secret.write_text(json.dumps({'request_id': 'x', 'kind': 'state'}), encoding='utf-8')
        link = room.root / 'relay' / 'relay-writer' / 'requests' / 'link1.json'
        link.symlink_to(secret)
        relay.process_once()
        resp_path = room.root / 'relay' / 'relay-writer' / 'responses' / 'resp-invalid-link1.json'
        self.assertTrue(resp_path.exists())
        resp = json.loads(resp_path.read_text(encoding='utf-8'))
        self.assertFalse(resp['ok'])
        self.assertTrue(link.is_symlink(), 'the link is answered and left where the chat put it')
        self.assertEqual(list((room.root / 'relay' / 'relay-writer' / 'archive').iterdir()), [])
        self.assertTrue(secret.exists())


class RelayResilienceTests(RelayTestCase):
    def test_missing_credential_is_skipped_then_picked_up(self):
        root = case_dir('relay')
        room = self.start(root, selected=('relay-writer',))
        relay = self.relay(room, ['relay-writer', 'relay-reader'])
        self.write_request(room, 'relay-reader', 'note1.json',
            {'request_id': 'note-1', 'kind': 'command', 'action': 'note',
             'payload': {'authority_id': 'relay-one', 'body': 'hi'}})
        relay.process_once()
        self.assertTrue((room.root / 'relay' / 'relay-reader' / 'requests' / 'note1.json').exists())
        self.assertFalse((room.root / 'relay' / 'relay-reader' / 'responses' / 'resp-note-1.json').exists())
        room.close()
        room2 = self.start(root, selected=('relay-writer', 'relay-reader'), config=None)
        relay.process_once()
        resp = self.response(room2, 'relay-reader', 'note-1')
        self.assertTrue(resp['ok'])

    def test_survives_key_rotation(self):
        room = self.start()
        relay = self.relay(room, ['relay-writer'])
        self.write_request(room, 'relay-writer', 'note1.json',
            {'request_id': 'note-1', 'kind': 'command', 'action': 'note',
             'payload': {'authority_id': 'relay-one', 'body': 'before rotation'}})
        relay.process_once()
        self.assertTrue(self.response(room, 'relay-writer', 'note-1')['ok'])
        room.rotate('relay-writer')
        self.write_request(room, 'relay-writer', 'note2.json',
            {'request_id': 'note-2', 'kind': 'command', 'action': 'note',
             'payload': {'authority_id': 'relay-one', 'body': 'after rotation'}})
        relay.process_once()
        self.assertTrue(self.response(room, 'relay-writer', 'note-2')['ok'])

    def test_tmp_files_ignored_until_renamed(self):
        room = self.start()
        relay = self.relay(room, ['relay-writer'])
        reqdir = room.root / 'relay' / 'relay-writer' / 'requests'
        tmp = reqdir / 'note1.json.abc123.tmp'
        tmp.write_text(json.dumps({'request_id': 'note-1', 'kind': 'command', 'action': 'note',
                                    'payload': {'authority_id': 'relay-one', 'body': 'draft'}}), encoding='utf-8')
        relay.process_once()
        self.assertTrue(tmp.exists())
        self.assertEqual(list((room.root / 'relay' / 'relay-writer' / 'responses').iterdir()), [])
        final = reqdir / 'note1.json'
        os.replace(tmp, final)
        relay.process_once()
        self.assertTrue(final.exists(), '2026-09-24 (gate R3): answered and left in place')
        resp = self.response(room, 'relay-writer', 'note-1')
        self.assertTrue(resp['ok'])

    def test_one_bad_request_does_not_block_others(self):
        room = self.start()
        relay = self.relay(room, ['relay-writer'])
        reqdir = room.root / 'relay' / 'relay-writer' / 'requests'
        (reqdir / 'bad.json').write_bytes(b'{not valid json')
        self.write_request(room, 'relay-writer', 'good.json',
            {'request_id': 'note-ok', 'kind': 'command', 'action': 'note',
             'payload': {'authority_id': 'relay-one', 'body': 'still works'}})
        relay.process_once()
        self.assertTrue((room.root / 'relay' / 'relay-writer' / 'responses' / 'resp-invalid-bad.json').exists())
        resp = self.response(room, 'relay-writer', 'note-ok')
        self.assertTrue(resp['ok'])


class RelayStateHonestyTests(RelayTestCase):
    def test_two_actor_state_files_never_cross_contaminate(self):
        # M9(b): each actor's own identity and own tasks must land in that
        # actor's own folder. Order matters (relay-writer is actors[0]): this
        # is written to fail if either folder is ever answered from, or
        # written into, the FIRST actor's client/folder instead of its own.
        room = self.start()
        task_writer = self.intake_task(room, recipient='relay-writer', authority='relay-one', ref='m9-writer')
        task_other = self.intake_task(room, recipient='relay-other', authority='relay-two', ref='m9-other')
        relay = self.relay(room, ['relay-writer', 'relay-other'], state_every=0)
        relay.process_once()
        writer_state = json.loads((room.root / 'relay' / 'relay-writer' / 'relay-state.json').read_text(encoding='utf-8'))
        other_state = json.loads((room.root / 'relay' / 'relay-other' / 'relay-state.json').read_text(encoding='utf-8'))
        self.assertEqual(writer_state['relay_actor'], 'relay-writer')
        self.assertEqual(writer_state['actor']['id'], 'relay-writer')
        self.assertEqual(other_state['relay_actor'], 'relay-other')
        self.assertEqual(other_state['actor']['id'], 'relay-other')
        writer_ids = {t['id'] for t in writer_state['tasks']}
        other_ids = {t['id'] for t in other_state['tasks']}
        self.assertIn(task_writer, writer_ids)
        self.assertNotIn(task_other, writer_ids)
        self.assertIn(task_other, other_ids)
        self.assertNotIn(task_writer, other_ids)

    def test_state_request_reports_failure_when_nothing_was_written(self):
        # M2: a `state` request must answer ok:false (with an error code)
        # when the room could not actually be asked, and must never claim
        # `written: true` while state.json is untouched.
        room = self.start()
        relay = self.relay(room, ['relay-writer'])
        state_path = room.root / 'relay' / 'relay-writer' / 'relay-state.json'
        self.assertFalse(state_path.exists())
        self.write_request(room, 'relay-writer', 'state1.json', {'request_id': 'state-now', 'kind': 'state'})
        room.close()
        relay.process_once()
        resp = self.response(room, 'relay-writer', 'state-now')
        self.assertFalse(resp['ok'])
        self.assertIn('error', resp)
        self.assertNotIn('result', resp)
        self.assertFalse(state_path.exists())

    def test_state_request_reports_ok_false_when_disk_write_fails(self):
        # R-M2a: the test above closes the room, so the fault is the room
        # being unreachable (fresh is None) and never reaches _persist_state
        # at all. This test keeps the room reachable (fresh state fetch
        # succeeds) but makes the ACTUAL DISK WRITE of state.json fail, so
        # _persist_state returns False and _handle_request_file's `elif
        # self._persist_state(...)` must be the thing standing between that
        # and a false `written: true`.
        import common_room.relay as relay_module
        room = self.start()
        relay = self.relay(room, ['relay-writer'])
        state_path = room.root / 'relay' / 'relay-writer' / 'relay-state.json'
        self.assertFalse(state_path.exists())
        self.write_request(room, 'relay-writer', 'state1.json', {'request_id': 'state-fail', 'kind': 'state'})
        real_write_file = relay._write_file

        def flaky(pin, where, name, value):
            if pin['actor'] == 'relay-writer' and name == 'relay-state.json':
                raise PermissionError(13, 'The process cannot access the file because it is being used by another process')
            return real_write_file(pin, where, name, value)
        relay._write_file = flaky

        relay.process_once()
        resp = self.response(room, 'relay-writer', 'state-fail')
        self.assertFalse(resp['ok'])
        self.assertEqual(resp.get('error'), 'io')
        self.assertNotIn('result', resp)
        self.assertFalse(state_path.exists())


class RelayFolderSafetyTests(RelayTestCase):
    def test_requests_folder_swapped_for_a_link_after_startup_moves_nothing(self):
        symlinks_or_skip()
        # H1: a chat-side swap of requests/ for a link, after the relay
        # already started, must never be followed — nothing outside the
        # room is read, and nothing is moved into archive/.
        room = self.start()
        relay = self.relay(room, ['relay-writer'])
        private_dir = case_dir('relay-private')
        secret = private_dir / 'id_rsa'
        secret.write_text('PRIVATE KEY MATERIAL', encoding='utf-8')
        requests_dir = room.root / 'relay' / 'relay-writer' / 'requests'
        shutil.rmtree(requests_dir)
        requests_dir.symlink_to(private_dir, target_is_directory=True)
        relay.process_once()  # must not raise
        self.assertTrue(secret.exists())
        self.assertEqual(secret.read_text(encoding='utf-8'), 'PRIVATE KEY MATERIAL')
        archive_dir = room.root / 'relay' / 'relay-writer' / 'archive'
        self.assertEqual(list(archive_dir.iterdir()), [])

    def test_archive_folder_swapped_for_a_link_after_startup_refuses_the_cycle(self):
        symlinks_or_skip()
        room = self.start()
        relay = self.relay(room, ['relay-writer'])
        self.write_request(room, 'relay-writer', 'evil.json',
            {'request_id': 'evil-1', 'kind': 'command', 'action': 'note',
             'payload': {'authority_id': 'relay-one', 'body': 'hi'}})
        outside = case_dir('relay-outside')
        archive_dir = room.root / 'relay' / 'relay-writer' / 'archive'
        shutil.rmtree(archive_dir)
        archive_dir.symlink_to(outside, target_is_directory=True)
        relay.process_once()  # must not raise
        request_path = room.root / 'relay' / 'relay-writer' / 'requests' / 'evil.json'
        self.assertTrue(request_path.exists(), 'the request must never be moved out of requests/')
        self.assertEqual(list(outside.iterdir()), [], 'nothing may land outside the room')
        # 2026-09-24 (gate R3): the relay no longer touches archive/ at all (the chat tidies its
        # own requests there), so a linked archive/ no longer needs to refuse the cycle.

    def test_responses_folder_swapped_for_a_link_after_startup_does_not_crash(self):
        symlinks_or_skip()
        room = self.start()
        relay = self.relay(room, ['relay-writer'])
        self.write_request(room, 'relay-writer', 'note1.json',
            {'request_id': 'note-1', 'kind': 'command', 'action': 'note',
             'payload': {'authority_id': 'relay-one', 'body': 'hi'}})
        outside = case_dir('relay-outside2')
        responses_dir = room.root / 'relay' / 'relay-writer' / 'responses'
        shutil.rmtree(responses_dir)
        responses_dir.symlink_to(outside, target_is_directory=True)
        before = len(self.events(room))
        relay.process_once()  # must not raise
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(len(self.events(room)), before)

    def test_hardlinked_request_file_refused(self):
        # H1: a hardlink (st_nlink != 1) into requests/ is not a symlink, so
        # a plain is_symlink() check would miss it; it must still be refused
        # before its content is trusted, and archiving it must never disturb
        # the file it was linked from.
        room = self.start()
        relay = self.relay(room, ['relay-writer'])
        outside = case_dir('relay-hardlink-target')
        secret = outside / 'secret.json'
        secret.write_text(json.dumps({'request_id': 'x', 'kind': 'state'}), encoding='utf-8')
        link = room.root / 'relay' / 'relay-writer' / 'requests' / 'hard1.json'
        os.link(secret, link)
        relay.process_once()
        resp_path = room.root / 'relay' / 'relay-writer' / 'responses' / 'resp-invalid-hard1.json'
        self.assertTrue(resp_path.exists())
        resp = json.loads(resp_path.read_text(encoding='utf-8'))
        self.assertFalse(resp['ok'])
        self.assertTrue(link.exists(), 'answered and left in place; never moved')
        self.assertTrue(secret.exists())


class RelayFaultIsolationTests(RelayTestCase):
    def test_one_actor_permission_error_does_not_stop_others(self):
        # M1: a Windows-style sharing violation (PermissionError) while
        # persisting one actor's state.json must never stop another actor's
        # request in the same cycle, and must never kill process_once.
        import common_room.relay as relay_module
        room = self.start()
        relay = self.relay(room, ['relay-writer', 'relay-other'], state_every=0)
        self.write_request(room, 'relay-other', 'note1.json',
            {'request_id': 'note-1', 'kind': 'command', 'action': 'note',
             'payload': {'authority_id': 'relay-two', 'body': 'still works'}})
        real_write_file = relay._write_file

        def flaky(pin, where, name, value):
            if pin['actor'] == 'relay-writer' and name == 'relay-state.json':
                raise PermissionError(13, 'The process cannot access the file because it is being used by another process')
            return real_write_file(pin, where, name, value)
        relay._write_file = flaky

        relay.process_once()  # must not raise
        resp = self.response(room, 'relay-other', 'note-1')
        self.assertTrue(resp['ok'])

    def test_non_oserror_fault_in_one_actors_request_does_not_stop_others(self):
        # R-M1ab: the test above injects a PermissionError (an OSError) that
        # is caught two levels down, inside _persist_state's own
        # `except (OSError, ValueError)`. That alone leaves the two isolation
        # try/excepts in process_once and _process_actor free to be deleted
        # with the suite still green (both only ever see an OSError that
        # never reaches them). This test injects a RuntimeError instead, on
        # the CrewClient call made while HANDLING A REQUEST for actor A
        # (never inside _persist_state), so it can only be stopped by one of
        # those two per-request/per-actor `except Exception` layers.
        # relay-writer is actors[0], so if both layers are gone the fault
        # aborts process_once before relay-other (actors[1]) is ever reached.
        room = self.start()
        relay = self.relay(room, ['relay-writer', 'relay-other'], state_every=0)
        self.write_request(room, 'relay-writer', 'note1.json',
            {'request_id': 'note-1', 'kind': 'command', 'action': 'note',
             'payload': {'authority_id': 'relay-one', 'body': 'boom'}})
        self.write_request(room, 'relay-other', 'note2.json',
            {'request_id': 'note-1', 'kind': 'command', 'action': 'note',
             'payload': {'authority_id': 'relay-two', 'body': 'still works'}})

        def boom(action, payload, request_id):
            raise RuntimeError('unexpected failure, not an OSError')
        relay.clients['relay-writer'].command = boom

        relay.process_once()  # must not raise
        resp = self.response(room, 'relay-other', 'note-1')
        self.assertTrue(resp['ok'])


class RelayFilenameAndIdTests(RelayTestCase):
    def test_odd_filenames_get_a_response_and_are_never_moved(self):
        # L1: a request file whose name does not match the required pattern
        # (space, accent, leading dot, missing .json) must be answered, not left
        # silently pending. 2026-09-24 (gate R3): it is answered once and left in
        # place; the relay never moves a file it did not accept (that move was N1).
        room = self.start()
        relay = self.relay(room, ['relay-writer'])
        reqdir = room.root / 'relay' / 'relay-writer' / 'requests'
        names = ['claim 1.json', 'résumé.json', '.hidden.json', 'noext']
        body = json.dumps({'request_id': 'odd-1', 'kind': 'state'}).encode('utf-8')
        for name in names:
            (reqdir / name).write_bytes(body)
        relay.process_once()
        self.assertEqual({p.name for p in reqdir.iterdir()}, set(names), 'answered and left in place')
        archive_dir = room.root / 'relay' / 'relay-writer' / 'archive'
        self.assertEqual(list(archive_dir.iterdir()), [])
        responses_dir = room.root / 'relay' / 'relay-writer' / 'responses'
        first = {p.name: p.stat().st_mtime_ns for p in responses_dir.iterdir()}
        relay.process_once()
        self.assertEqual({p.name: p.stat().st_mtime_ns for p in responses_dir.iterdir()}, first, 'answered once only')
        invalid_files = [p for p in responses_dir.iterdir() if p.name.startswith('resp-invalid-')]
        self.assertGreaterEqual(len(invalid_files), len(names))
        for p in invalid_files:
            self.assertFalse(json.loads(p.read_text(encoding='utf-8'))['ok'])

    def test_colon_in_request_id_is_refused(self):
        # L2: ':' in a relay request id must be refused outright (not just
        # sanitized), so 'x:1' and 'x-1' can never collide on one response
        # file, and nothing with a colon id is ever sent to the room.
        room = self.start()
        relay = self.relay(room, ['relay-writer'])
        before = len(self.events(room))
        self.write_request(room, 'relay-writer', 'colon1.json',
            {'request_id': 'x:1', 'kind': 'command', 'action': 'note',
             'payload': {'authority_id': 'relay-one', 'body': 'hi'}})
        relay.process_once()
        resp_path = room.root / 'relay' / 'relay-writer' / 'responses' / 'resp-invalid-colon1.json'
        self.assertTrue(resp_path.exists())
        self.assertFalse(json.loads(resp_path.read_text(encoding='utf-8'))['ok'])
        self.assertEqual(len(self.events(room)), before)

    def test_long_request_id_is_capped_at_64_chars(self):
        room = self.start()
        relay = self.relay(room, ['relay-writer'])
        self.write_request(room, 'relay-writer', 'toolong.json',
            {'request_id': 'a' * 65, 'kind': 'state'})
        relay.process_once()
        resp_path = room.root / 'relay' / 'relay-writer' / 'responses' / 'resp-invalid-toolong.json'
        self.assertTrue(resp_path.exists())
        self.assertFalse(json.loads(resp_path.read_text(encoding='utf-8'))['ok'])

    def test_reserved_windows_device_names_refused(self):
        room = self.start()
        relay = self.relay(room, ['relay-writer'])
        self.write_request(room, 'relay-writer', 'NUL.json', {'request_id': 'ok-1', 'kind': 'state'})
        self.write_request(room, 'relay-writer', 'plain.json', {'request_id': 'CON', 'kind': 'state'})
        relay.process_once()
        for stem in ['NUL', 'plain']:
            resp_path = room.root / 'relay' / 'relay-writer' / 'responses' / ('resp-invalid-' + stem + '.json')
            self.assertTrue(resp_path.exists(), stem)
            self.assertFalse(json.loads(resp_path.read_text(encoding='utf-8'))['ok'])


class RelayRoleGuardTests(RelayTestCase):
    def test_owner_role_is_checked_before_any_request_is_sent(self):
        # L3: if the connected identity ever resolves to role 'owner' (the
        # way a credential/identity mix-up would surface), the relay must
        # refuse that actor's whole cycle before any command or handoff is
        # sent — not discover it only when writing state.json afterward.
        room = self.start()
        relay = self.relay(room, ['relay-writer'])
        calls = []
        real_command = relay.clients['relay-writer'].command

        def spy(action, payload, request_id):
            calls.append(action)
            return real_command(action, payload, request_id)
        relay.clients['relay-writer'].command = spy

        real_state = relay.clients['relay-writer'].state

        def fake_state():
            state = real_state()
            state['actor'] = {'id': 'relay-writer', 'role': 'owner'}
            return state
        relay.clients['relay-writer'].state = fake_state

        self.write_request(room, 'relay-writer', 'note1.json',
            {'request_id': 'note-1', 'kind': 'command', 'action': 'note',
             'payload': {'authority_id': 'relay-one', 'body': 'should never be sent'}})
        relay.process_once()
        self.assertEqual(calls, [], 'no command may be sent once the role resolves to owner')
        self.assertFalse((room.root / 'relay' / 'relay-writer' / 'responses' / 'resp-note-1.json').exists())


class RelayRaceTests(RelayTestCase):
    """Gate R, third attempt (2026-09-24, authorized by Shane: "fix the relay").

    Finding N1/N18: a chat can swap its own relay folders for links at any moment,
    including while the relay is waiting on the room. These tests perform the swap from
    inside the room call (the widest real window) or simulate a lost race, and pin the
    invariant: nothing outside the seat's real relay folders is ever read, moved,
    created or overwritten because of a link."""

    def _swap_during_room_call(self, relay, actor, folder_name, target):
        client = relay.clients[actor]
        real = client.command
        seat = relay.folders[actor]['root']

        def swapping(*args, **kwargs):
            result = real(*args, **kwargs)
            path = seat if folder_name is None else seat / folder_name
            path.rename(path.with_name(path.name + '-real'))
            path.symlink_to(target, target_is_directory=True)
            return result
        client.command = swapping

    def _outside(self, name, files):
        outside = case_dir(name)
        for rel, text in files.items():
            (outside / rel).parent.mkdir(parents=True, exist_ok=True)
            (outside / rel).write_text(text, encoding='utf-8')
        return outside

    def _snapshot(self, folder):
        return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob('*')) if p.is_file()}

    def test_requests_swapped_during_room_call_moves_nothing_outside(self):
        room = self.start()
        relay = self.relay(room, ['relay-writer'])
        self.write_request(room, 'relay-writer', 'keeper-credential.json',
            {'request_id': 'note-1', 'kind': 'command', 'action': 'note',
             'payload': {'authority_id': 'relay-one', 'body': 'decoy'}})
        outside = self._outside('relay-race-req', {'keeper-credential.json': 'PRIVATE KEY MATERIAL'})
        before = self._snapshot(outside)
        self._swap_during_room_call(relay, 'relay-writer', 'requests', outside)
        relay.process_once()
        self.assertEqual(self._snapshot(outside), before)
        archive = room.root / 'relay' / 'relay-writer' / 'archive'
        self.assertEqual(list(archive.iterdir()), [])

    def test_seat_folder_swapped_during_room_call_writes_nothing_outside(self):
        room = self.start()
        relay = self.relay(room, ['relay-writer'])
        self.write_request(room, 'relay-writer', 'note1.json',
            {'request_id': 'note-1', 'kind': 'command', 'action': 'note',
             'payload': {'authority_id': 'relay-one', 'body': 'hi'}})
        outside = self._outside('relay-race-seat', {'requests/placeholder.txt': 'x', 'responses/placeholder.txt': 'x',
                                                    'archive/placeholder.txt': 'x', 'notes.txt': 'keep me'})
        before = self._snapshot(outside)
        self._swap_during_room_call(relay, 'relay-writer', None, outside)
        relay.process_once()
        self.assertEqual(self._snapshot(outside), before)

    def test_responses_swapped_during_room_call_writes_nothing_outside(self):
        room = self.start()
        relay = self.relay(room, ['relay-writer'])
        self.write_request(room, 'relay-writer', 'note1.json',
            {'request_id': 'note-1', 'kind': 'command', 'action': 'note',
             'payload': {'authority_id': 'relay-one', 'body': 'hi'}})
        outside = self._outside('relay-race-resp', {'enrollment.json': '{"real": true}'})
        before = self._snapshot(outside)
        self._swap_during_room_call(relay, 'relay-writer', 'responses', outside)
        relay.process_once()
        self.assertEqual(self._snapshot(outside), before)

    def test_race_won_on_responses_never_overwrites_a_sensitive_name(self):
        symlinks_or_skip()
        # Defense in depth, tested in isolation: switch off every other layer (directory
        # handles, identity pins, re-checks) so the write really follows a linked
        # responses/ folder, the way a race won on Windows would. Relay-written files carry
        # relay-only names, so an existing file like enrollment.json is still never touched.
        import common_room.relay as relay_module
        room = self.start()
        relay = self.relay(room, ['relay-writer'])
        self.write_request(room, 'relay-writer', 'enroll.json',
            {'request_id': 'enrollment', 'kind': 'command', 'action': 'note',
             'payload': {'authority_id': 'relay-one', 'body': 'hi'}})
        outside = self._outside('relay-race-won', {'enrollment.json': '{"real": true}'})
        responses = room.root / 'relay' / 'relay-writer' / 'responses'
        shutil.rmtree(responses)
        responses.symlink_to(outside, target_is_directory=True)
        original = relay_module.DIR_FD
        relay_module.DIR_FD = False
        self.addCleanup(setattr, relay_module, 'DIR_FD', original)
        relay._dir_identity = lambda path: (0, 0)
        relay.startup = {key: (0, 0) for key in relay.startup}     # and the startup-identity layer
        relay._recheck = lambda pin: None
        relay.process_once()
        self.assertEqual((outside / 'enrollment.json').read_text(encoding='utf-8'), '{"real": true}')
        self.assertTrue((outside / 'resp-enrollment.json').exists(), 'the layers really were off: the write followed the link')

    def test_request_files_are_never_moved_and_never_handled_twice(self):
        room = self.start()
        task = self.intake_task(room)
        relay = self.relay(room, ['relay-writer'])
        path = self.write_request(room, 'relay-writer', 'ack1.json',
            {'request_id': 'ack-1', 'kind': 'command', 'action': 'ack', 'payload': {'task_id': task}})
        relay.process_once()
        self.assertTrue(path.exists(), 'the relay never moves request files; the chat tidies its own folder')
        events = len(self.events(room))
        relay.process_once()
        self.assertEqual(len(self.events(room)), events)
        self.assertTrue(self.response(room, 'relay-writer', 'ack-1')['ok'])

    def test_entry_replaced_by_link_after_listing_is_not_read(self):
        room = self.start()
        relay = self.relay(room, ['relay-writer'])
        self.write_request(room, 'relay-writer', 'a1.json',
            {'request_id': 'a-1', 'kind': 'command', 'action': 'note',
             'payload': {'authority_id': 'relay-one', 'body': 'first'}})
        second = self.write_request(room, 'relay-writer', 'b1.json',
            {'request_id': 'b-1', 'kind': 'command', 'action': 'note',
             'payload': {'authority_id': 'relay-one', 'body': 'second'}})
        os.utime(second, (os.stat(second).st_atime, os.stat(second).st_mtime + 5))
        outside = self._outside('relay-race-entry', {'b1.json': json.dumps(
            {'request_id': 'b-1', 'kind': 'command', 'action': 'note',
             'payload': {'authority_id': 'relay-one', 'body': 'EXFIL-FROM-OUTSIDE'}})})
        client = relay.clients['relay-writer']
        real = client.command

        def swapping(*args, **kwargs):
            result = real(*args, **kwargs)
            if second.exists() and not second.is_symlink():
                second.rename(second.with_name('b1.json.orig'))
                second.symlink_to(outside / 'b1.json')
            return result
        client.command = swapping
        relay.process_once()
        bodies = [e['payload'].get('body') for e in self.events(room) if e['kind'] == 'note']
        self.assertNotIn('EXFIL-FROM-OUTSIDE', bodies)
        self.assertTrue((outside / 'b1.json').exists())

    def test_every_relay_written_file_has_a_relay_only_name(self):
        room = self.start()
        relay = self.relay(room, ['relay-writer'], state_every=0)
        seat = room.root / 'relay' / 'relay-writer'
        before = {p for p in seat.rglob('*') if p.is_file()}
        self.write_request(room, 'relay-writer', 'note1.json',
            {'request_id': 'note-1', 'kind': 'command', 'action': 'note',
             'payload': {'authority_id': 'relay-one', 'body': 'hi'}})
        (seat / 'requests' / 'bad.json').write_text('{not json', encoding='utf-8')
        written = {seat / 'requests' / 'note1.json', seat / 'requests' / 'bad.json'}
        relay.process_once()
        created = {p for p in seat.rglob('*') if p.is_file()} - before - written
        self.assertTrue(created)
        for path in created:
            rel = path.relative_to(seat).as_posix()
            self.assertTrue(rel == 'relay-state.json' or (rel.startswith('responses/resp-') and rel.endswith('.json')), rel)


def _without_dir_fd(cls):
    """The same tests on the Windows code path (no directory handles), run here by
    switching the relay's DIR_FD off for the duration of each test."""
    import common_room.relay as relay_module

    class Variant(cls):
        def setUp(self):
            super().setUp()
            original = relay_module.DIR_FD
            relay_module.DIR_FD = False
            self.addCleanup(setattr, relay_module, 'DIR_FD', original)
    Variant.__name__ = Variant.__qualname__ = cls.__name__ + 'WindowsPath'
    return Variant


RelayFlowTestsWindowsPath = _without_dir_fd(RelayFlowTests)
RelayFolderSafetyTestsWindowsPath = _without_dir_fd(RelayFolderSafetyTests)
RelayRaceTestsWindowsPath = _without_dir_fd(RelayRaceTests)
RelayFilenameAndIdTestsWindowsPath = _without_dir_fd(RelayFilenameAndIdTests)


class RelayDelegatedDecisionTests(RelayTestCase):
    """2026-09-24 orchestra build: a helper task's sender (the orchestrator, a relay seat)
    answers or cancels its own helpers through the relay; the room decides who may."""
    CONFIG = {'version': 1, 'actors': [
        {'id': 'orch', 'role': 'worker', 'label': 'Fictional orchestrator', 'session': 'fixture-orch'},
        {'id': 'helper', 'role': 'worker', 'label': 'Fictional helper', 'session': 'fixture-helper'},
        {'id': 'other', 'role': 'worker', 'label': 'Fictional bystander', 'session': 'fixture-other'},
        {'id': 'judge', 'role': 'reviewer', 'label': 'Fictional reviewer', 'session': 'fixture-judge'}],
        'authorities': [{'id': 'band', 'members': ['keeper', 'orch', 'helper', 'other', 'judge'], 'scope': 'Fictional orchestra'}]}

    def _setup(self):
        room = self.start(selected=('orch', 'helper', 'other', 'judge'), config=self.CONFIG)
        owner, orch, helper = room.store.people['keeper'], room.store.people['orch'], room.store.people['helper']
        msg = dict(title='Workstream', body='Root', acceptance='Done', recipient='orch', authority_id='band', kind='handoff.record')
        root = room.store.execute(owner, 'intake', {'source_namespace': 'estate', 'external_reference': 'ws', 'message': msg}, 'i')['task']['id']
        room.store.execute(owner, 'deliver', {'task_id': root}, 'd')
        room.store.execute(orch, 'ack', {'task_id': root}, 'a')
        fence = room.store.execute(orch, 'claim', {'task_id': root, 'seconds': 1800}, 'c')['task']['fence']
        child = room.store.execute(orch, 'request_help', {'task_id': root, 'fence': fence, 'recipient': 'helper',
                                   'title': 'Piece', 'body': 'Piece body', 'acceptance': 'Piece done'}, 'h')['task']['id']
        room.store.execute(helper, 'ack', {'task_id': child}, 'ha')
        hf = room.store.execute(helper, 'claim', {'task_id': child, 'seconds': 1800}, 'hc')['task']['fence']
        room.store.execute(helper, 'wait_decision', {'task_id': child, 'fence': hf, 'question': 'Which file?'}, 'hq')
        return room, child

    def test_helper_sender_answers_its_helpers_question_through_the_relay(self):
        room, child = self._setup()
        relay = self.relay(room, ['orch', 'other'])
        self.write_request(room, 'orch', 'answer.json', {'request_id': 'answer-1', 'kind': 'command',
                           'action': 'resolve_decision', 'payload': {'task_id': child, 'body': 'Use notes.md'}})
        self.write_request(room, 'other', 'meddle.json', {'request_id': 'meddle-1', 'kind': 'command',
                           'action': 'resolve_decision', 'payload': {'task_id': child, 'body': 'Not mine to answer'}})
        relay.process_once()
        self.assertTrue(self.response(room, 'orch', 'answer-1')['ok'])
        refused = self.response(room, 'other', 'meddle-1')
        self.assertFalse(refused['ok'])
        self.assertEqual(refused['error'], 'forbidden')       # refused by the room, not the relay

    def test_helper_sender_cancels_its_helper_through_the_relay(self):
        room, child = self._setup()
        relay = self.relay(room, ['orch'])
        self.write_request(room, 'orch', 'cancel.json', {'request_id': 'cancel-1', 'kind': 'command',
                           'action': 'cancel', 'payload': {'task_id': child, 'body': 'Superseded'}})
        relay.process_once()
        resp = self.response(room, 'orch', 'cancel-1')
        self.assertTrue(resp['ok'], resp)
        self.assertEqual(resp['result']['task']['status'], 'cancelled')


class RelayOrchestraFixTests(RelayTestCase):
    """2026-09-24, after the orchestra verification (F1, F2, F10, F12, F13, relay lock)."""

    def _note(self, room, actor, name, rid, body='hi', scope='relay-one'):
        return self.write_request(room, actor, name, {'request_id': rid, 'kind': 'command', 'action': 'note',
                                                      'payload': {'authority_id': scope, 'body': body}})

    def test_restart_does_not_resend_an_answered_or_refused_request(self):          # F1
        room = self.start()
        task = self.intake_task(room)
        relay = self.relay(room, ['relay-reader'])
        # A review sent too early is refused by the room (nothing to review yet) and leaves no receipt.
        self.write_request(room, 'relay-reader', 'early.json', {'request_id': 'early-accept', 'kind': 'command',
                           'action': 'review', 'payload': {'task_id': task, 'accepted': True, 'note': 'looks fine'}})
        relay.process_once()
        self.assertFalse(self.response(room, 'relay-reader', 'early-accept')['ok'])
        # The worker then submits real work...
        worker = room.store.people['relay-writer']
        room.store.execute(worker, 'ack', {'task_id': task}, 'a')
        lease = room.store.execute(worker, 'claim', {'task_id': task, 'seconds': 900}, 'c')['task']['lease']
        from common_room.handoff_api import handoff_operation
        staged = handoff_operation(room.server, worker, 'stage_text', {'task_id': task, 'fence': lease['fence'], 'text': 'Body'})
        room.store.execute(worker, 'finish', {'task_id': task, 'fence': lease['fence'], 'body': 'Done', 'refs': [staged]}, 'f')
        # ...and the relay restarts. The old refused request must not be replayed onto the new submission.
        again = self.relay(room, ['relay-reader'])
        again.process_once()
        status = room.store.task(room.store.people['keeper'], task)['status']
        self.assertEqual(status, 'ready_for_review', 'a stale request must never accept work nobody read')

    def test_fault_before_reading_leaves_the_request_to_be_answered_later(self):    # F12
        room = self.start()
        relay = self.relay(room, ['relay-writer'])
        self._note(room, 'relay-writer', 'n1.json', 'n-1')
        real = relay._recheck
        calls = {'n': 0}

        def flaky(pin):
            calls['n'] += 1
            if calls['n'] == 2:                     # the re-check just before reading the request
                from common_room.relay import RelayFault
                raise RelayFault('linked_folder', 'simulated')
            return real(pin)
        relay._recheck = flaky
        relay.process_once()
        relay._recheck = real
        relay.process_once()
        self.assertTrue(self.response(room, 'relay-writer', 'n-1')['ok'])

    def test_one_linked_seat_at_startup_does_not_stop_the_other_seats(self):        # F13
        symlinks_or_skip()
        from common_room.relay import Relay
        room = self.start()
        relay_dir = room.root / 'relay'
        relay_dir.mkdir(exist_ok=True)
        (relay_dir / 'relay-writer').symlink_to(case_dir('relay-elsewhere'), target_is_directory=True)
        relay = Relay(room.root, ['relay-writer', 'relay-other'])
        self.assertEqual(relay.actors, ['relay-other'])
        self._note(room, 'relay-other', 'n1.json', 'n-1', scope='relay-two')
        relay.process_once()
        self.assertTrue(self.response(room, 'relay-other', 'n-1')['ok'])

    def test_relay_folder_replaced_between_cycles_is_refused(self):                 # F10 / R2
        symlinks_or_skip()
        room = self.start()
        relay = self.relay(room, ['relay-writer'])
        relay.process_once()
        outside = case_dir('relay-ancestor')
        for sub in ('requests', 'responses', 'archive'):
            (outside / 'relay-writer' / sub).mkdir(parents=True)
        (outside / 'relay-writer' / 'requests' / 'x.json').write_text(json.dumps(
            {'request_id': 'x-1', 'kind': 'command', 'action': 'note',
             'payload': {'authority_id': 'relay-one', 'body': 'FROM-OUTSIDE'}}), encoding='utf-8')
        before = {p.relative_to(outside).as_posix() for p in outside.rglob('*')}
        real_relay = room.root / 'relay'
        real_relay.rename(room.root / 'relay-real')
        real_relay.symlink_to(outside, target_is_directory=True)
        relay.process_once()
        bodies = [e['payload'].get('body') for e in self.events(room) if e['kind'] == 'note']
        self.assertNotIn('FROM-OUTSIDE', bodies)
        self.assertEqual({p.relative_to(outside).as_posix() for p in outside.rglob('*')}, before)

    def test_seat_swapped_while_its_folders_are_being_opened_is_refused(self):     # F2 / R3
        import common_room.relay as relay_module
        if not relay_module.DIR_FD:
            self.skipTest('directory handles only')
        room = self.start()
        relay = self.relay(room, ['relay-writer'])
        outside = case_dir('relay-seat-swap')
        for sub in ('requests', 'responses', 'archive'):
            (outside / sub).mkdir()
        (outside / 'requests' / 'x.json').write_text(json.dumps(
            {'request_id': 'x-1', 'kind': 'command', 'action': 'note',
             'payload': {'authority_id': 'relay-one', 'body': 'FROM-OUTSIDE'}}), encoding='utf-8')
        seat = room.root / 'relay' / 'relay-writer'
        real_open = os.open
        state = {'done': False}

        def swapping_open(path, flags, *args, **kwargs):
            # Toggle: swap the seat for a link only while requests/ is being opened, then put it
            # back, so every later path check sees the real folder and only the handle is wrong.
            if not state['done'] and str(path).endswith(os.path.join('relay-writer', 'requests')):
                state['done'] = True
                seat.rename(seat.with_name('relay-writer-real'))
                seat.symlink_to(outside, target_is_directory=True)
                try:
                    return real_open(path, flags, *args, **kwargs)
                finally:
                    seat.unlink()
                    seat.with_name('relay-writer-real').rename(seat)
            return real_open(path, flags, *args, **kwargs)
        relay_module.os.open = swapping_open
        self.addCleanup(setattr, relay_module.os, 'open', real_open)
        relay.process_once()
        relay_module.os.open = real_open
        bodies = [e['payload'].get('body') for e in self.events(room) if e['kind'] == 'note']
        self.assertTrue(state['done'])
        self.assertNotIn('FROM-OUTSIDE', bodies)

    def test_windows_path_swap_after_the_first_write_recheck_writes_nothing_outside(self):   # F2 / R11
        import common_room.relay as relay_module
        original = relay_module.DIR_FD
        relay_module.DIR_FD = False
        self.addCleanup(setattr, relay_module, 'DIR_FD', original)
        room = self.start()
        relay = self.relay(room, ['relay-writer'])
        self._note(room, 'relay-writer', 'n1.json', 'in-1')
        outside = case_dir('relay-r11')
        responses = room.root / 'relay' / 'relay-writer' / 'responses'
        real_write = relay._write_file
        real_recheck = relay._recheck
        state = {'armed': False, 'done': False}

        def arming_write(pin, where, name, value):
            state['armed'] = where == 'responses'
            try:
                return real_write(pin, where, name, value)
            finally:
                state['armed'] = False

        def swapping_recheck(pin):
            real_recheck(pin)
            if state['armed'] and not state['done']:
                state['done'] = True
                responses.rename(responses.with_name('responses-real'))
                responses.symlink_to(outside, target_is_directory=True)
        relay._write_file = arming_write
        relay._recheck = swapping_recheck
        relay.process_once()
        self.assertTrue(state['done'])
        self.assertEqual(list(outside.iterdir()), [])

    def test_a_second_relay_for_the_same_room_refuses_to_start(self):              # two relays
        import io
        from contextlib import redirect_stderr
        from common_room import relay as relay_module
        room = self.start()
        lock = relay_module.RelayLock(room.root)
        self.assertTrue(lock.acquire())
        self.addCleanup(lock.release)
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            relay_module.main(['--actors', 'relay-writer', '--root', str(room.root), '--once'])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn('already running', err.getvalue())


class RelayAncestorSwapTests(RelayTestCase):
    """2026-09-24, re-verification N-d: a folder ABOVE the room root swapped for a link. Every
    folder the relay checks is still a real directory (the link is higher up), so only the
    startup-identity compare can notice. The crafted tree even carries a copy of the seat's
    credential, so nothing else stops the request; checked on both paths."""
    def _swap_above(self, dir_fd):
        from common_room import relay as relay_module
        saved = relay_module.DIR_FD
        relay_module.DIR_FD = dir_fd and saved
        self.addCleanup(setattr, relay_module, 'DIR_FD', saved)
        base = case_dir('relay-ancestor-top')
        room = self.start(root=base / 'mid' / 'handoffs')
        relay = self.relay(room, ['relay-writer'])
        relay.process_once()
        outside = case_dir('relay-ancestor-outside')
        seat = outside / 'mid' / 'handoffs' / 'relay' / 'relay-writer'
        for sub in ('requests', 'responses', 'archive'):
            (seat / sub).mkdir(parents=True)
        shutil.copy(room.root / 'relay-writer-credential.json', outside / 'mid' / 'handoffs' / 'relay-writer-credential.json')
        (seat / 'requests' / 'x.json').write_text(json.dumps(
            {'request_id': 'x-1', 'kind': 'command', 'action': 'note',
             'payload': {'authority_id': 'relay-one', 'body': 'FROM-ABOVE'}}), encoding='utf-8')
        before = {p.relative_to(outside).as_posix() for p in outside.rglob('*')}
        symlinks_or_skip()
        try:
            (base / 'mid').rename(base / 'mid-real')
        except PermissionError:
            if os.name != 'nt':
                raise
            self.skipTest('Windows refuses to rename a folder above a room whose files are open, so this swap '
                          'cannot happen there')
        (base / 'mid').symlink_to(outside / 'mid', target_is_directory=True)
        try:
            relay.process_once()
        finally:
            (base / 'mid').unlink()
            (base / 'mid-real').rename(base / 'mid')
        bodies = [e['payload'].get('body') for e in self.events(room) if e['kind'] == 'note']
        self.assertNotIn('FROM-ABOVE', bodies)
        self.assertEqual({p.relative_to(outside).as_posix() for p in outside.rglob('*')}, before)

    def test_folder_above_the_room_swapped_between_cycles_is_refused(self):
        self._swap_above(True)

    def test_folder_above_the_room_swapped_between_cycles_is_refused_windows_path(self):
        self._swap_above(False)


class RelayReverifyLowTests(RelayTestCase):
    """2026-09-24, re-verification N-a, N-c, N-e, N-f."""
    def test_hand_retry_same_name_is_skipped_new_name_is_tried_again(self):         # N-a
        room = self.start()
        task = self.intake_task(room)
        relay = self.relay(room, ['relay-reader'])
        early = {'request_id': 'rv-1', 'kind': 'command', 'action': 'review',
                 'payload': {'task_id': task, 'accepted': True, 'note': 'checked every line'}}
        self.write_request(room, 'relay-reader', 'rv.json', early)
        relay.process_once()
        self.assertFalse(self.response(room, 'relay-reader', 'rv-1')['ok'])
        worker = room.store.people['relay-writer']
        room.store.execute(worker, 'ack', {'task_id': task}, 'a')
        lease = room.store.execute(worker, 'claim', {'task_id': task, 'seconds': 900}, 'c')['task']['lease']
        from common_room.handoff_api import handoff_operation
        staged = handoff_operation(room.server, worker, 'stage_text', {'task_id': task, 'fence': lease['fence'], 'text': 'Body'})
        room.store.execute(worker, 'finish', {'task_id': task, 'fence': lease['fence'], 'body': 'Done', 'refs': [staged]}, 'f')
        # Same file name, same content, written again: already answered, skipped (the README says so).
        path = self.write_request(room, 'relay-reader', 'rv.json', early)
        os.utime(path, ns=(1, 1))
        relay.process_once()
        self.assertFalse(self.response(room, 'relay-reader', 'rv-1')['ok'])
        self.assertEqual(room.store.task(room.store.people['keeper'], task)['status'], 'ready_for_review')
        # A new file name with the same request_id: tried again, and the room acts (a refusal left no receipt).
        self.write_request(room, 'relay-reader', 'rv-again.json', early)
        relay.process_once()
        self.assertTrue(self.response(room, 'relay-reader', 'rv-1')['ok'])
        self.assertEqual(room.store.task(room.store.people['keeper'], task)['status'], 'done')

    def test_readme_tells_a_hand_retry_to_use_a_new_file_name(self):                # N-a text
        room = self.start()
        self.relay(room, ['relay-writer'])
        text = (room.root / 'relay' / 'relay-writer' / 'README.md').read_text(encoding='utf-8')
        self.assertIn('new file name', text)
        self.assertIn('"auto"', text)

    def test_hardlinked_answer_file_is_not_taken_as_the_answer(self):                 # N-c
        room = self.start()
        relay = self.relay(room, ['relay-writer'])
        body = {'request_id': 'hl-1', 'kind': 'command', 'action': 'note',
                'payload': {'authority_id': 'relay-one', 'body': 'HARDLINK-CHECK'}}
        path = self.write_request(room, 'relay-writer', 'hl.json', body)
        import hashlib
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        outside = case_dir('relay-hardlink-outside') / 'keep.json'
        forged = json.dumps({'ok': False, 'request_file': 'hl.json', 'request_sha256': digest})
        outside.write_text(forged, encoding='utf-8')
        os.link(outside, room.root / 'relay' / 'relay-writer' / 'responses' / 'resp-hl-1.json')
        relay.process_once()
        bodies = [e['payload'].get('body') for e in self.events(room) if e['kind'] == 'note']
        self.assertIn('HARDLINK-CHECK', bodies, 'a shared answer file must not suppress the request')
        self.assertEqual(outside.read_text(encoding='utf-8'), forged, 'the outside file is never written')

    def test_linked_lock_file_is_refused_with_its_own_message(self):                   # N-e
        symlinks_or_skip()
        import io
        from contextlib import redirect_stderr
        from common_room import relay as relay_module
        room = self.start()
        outside = case_dir('relay-lock-outside') / 'target.txt'
        outside.write_text('untouched', encoding='utf-8')
        (room.root / 'relay.lock').symlink_to(outside)
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            relay_module.main(['--actors', 'relay-writer', '--root', str(room.root), '--once'])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn('not a plain file', err.getvalue())
        self.assertNotIn('already running', err.getvalue())
        self.assertEqual(outside.read_text(encoding='utf-8'), 'untouched')

    def test_linked_root_is_a_clean_refusal(self):                                     # N-f
        symlinks_or_skip()
        import io
        from contextlib import redirect_stderr
        from common_room import relay as relay_module
        room = self.start()
        link = case_dir('relay-root-link') / 'handoffs'
        link.symlink_to(room.root, target_is_directory=True)
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            relay_module.main(['--actors', 'relay-writer', '--root', str(link), '--once'])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn('common_room.relay:', err.getvalue())
        self.assertNotIn('Traceback', err.getvalue())


if __name__ == '__main__':
    unittest.main()
