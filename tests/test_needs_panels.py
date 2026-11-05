"""Needs Shane and the usage panel (plan turnons-2026-10-03, job panels).

  * common_room/needs.py: needs_shane(snapshot) lists everything waiting on Shane as {id, kind, since, summary, action},
    one kind at a time below, with ids that hold still while the condition holds and nothing secret in any field.
  * the board serves that list in /api/state as `needs`, shows it first on the page ('Nothing needs you.' when empty),
    and shows the owner a usage panel from GET /api/usage (the workers' usage/*.jsonl, per USAGE_FORMAT.md).
  * `crew needs` prints the same list: exit 0 when empty, 2 when not, --json prints the items.

needs.py is imported inside each test, so on the base commit every test fails on its own (RED) instead of the file
failing to import once.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # so evidence.py can run this file as a script in a clean copy

import copy, datetime, io, json, os, re, shutil, subprocess, unittest
from contextlib import redirect_stderr, redirect_stdout

from tests.helpers import ROOT, case_dir

NOW = 1791230000.0
KEY = 'Xq7_Lm2Rk9-PwT4zVb8NcY3sJf6HdG1aQe5UoIr0Kt'          # shaped like a room key (token_urlsafe(32))
CRED = r'D:\Work Area\Artifacts\tree\.runtime\handoffs\claude-code-credential.json'


def needs_module():
    from common_room import needs
    return needs


def task(id, status='waiting_decision', parent=None, sender='keeper', recipient='claude-code', fence=3, failed=0,
         decision='Which colour should the button be?', title='BUILD: a fixture job'):
    return {'id': id, 'status': status, 'parent_id': parent, 'sender': sender, 'recipient': recipient, 'fence': fence,
            'failed_attempts': failed, 'decision': decision, 'title': title, 'authority_id': 'orchestra', 'lease': None}


def note(seq, sender, body, minutes_ago=5):
    return {'seq': seq, 'sender': sender, 'authority_id': 'orchestra', 'body': body, 'timestamp': NOW - minutes_ago * 60}


def event(task_id, minutes_ago, kind='task_wait_decision'):
    return {'seq': 1, 'task_id': task_id, 'kind': kind, 'actor': 'x', 'timestamp': NOW - minutes_ago * 60}


def person(id, role='worker', key_active=True, **more):
    return dict({'id': id, 'role': role, 'contact': 'Credential contact active; assistant identity unverified',
                 'key_active': key_active, 'legacy': False}, **more)


def snapshot(tasks=(), notes=(), history=(), participants=(), stop=None):
    return {'server_time': NOW, 'actor': {'id': 'keeper', 'role': 'owner'}, 'stop': stop or {'active': False},
            'authorities': [{'id': 'orchestra', 'owner': 'keeper'}], 'tasks': list(tasks), 'notes': list(notes),
            'history': list(history), 'participants': [person('keeper', 'owner')] + list(participants)}


class NeedsShaneKinds(unittest.TestCase):
    def needs(self, snap):
        return needs_module().needs_shane(snap)

    def only(self, snap, kind):
        items = self.needs(snap)
        self.assertEqual([i['kind'] for i in items], [kind], items)
        item = items[0]
        self.assertEqual(set(item), {'id', 'kind', 'since', 'summary', 'action'})
        return item

    def test_an_empty_room_needs_nothing(self):
        self.assertEqual(self.needs(snapshot()), [])
        self.assertEqual(self.needs({}), [])

    def test_a_stopped_room_is_listed_with_the_resume_line(self):
        item = self.only(snapshot(stop={'active': True, 'by': 'keeper', 'at': NOW - 60, 'reason': 'Lunch.'}), 'stop')
        self.assertEqual((item['id'], item['since']), ('stop:%.3f' % (NOW - 60), NOW - 60))
        self.assertIn('STOPPED', item['summary']); self.assertIn('Lunch.', item['summary'])
        self.assertIn('crew-shane resume-all', item['action'])

    def test_a_root_task_waiting_on_the_keeper_is_a_decision(self):
        item = self.only(snapshot([task('t1')], history=[event('t1', 50), event('t1', 9)]), 'decision')
        self.assertEqual((item['id'], item['since']), ('decision:t1:f3', NOW - 540))
        self.assertIn('Which colour should the button be?', item['summary'])
        self.assertEqual(item['action'], 'crew-shane decide t1 --response "your answer"')

    def test_a_review_return_reads_as_one(self):
        t = task('t1', decision='Review requires an owner decision: SUBSTANCE: the test is missing.')
        item = self.only(snapshot([t]), 'decision')
        self.assertTrue(item['summary'].startswith('Review returned "BUILD: a fixture job"'), item['summary'])
        self.assertIn('SUBSTANCE: the test is missing.', item['summary'])

    def test_a_helper_the_keeper_sent_is_a_decision_and_one_another_seat_sent_is_not(self):
        self.only(snapshot([task('t1', parent='t0', sender='keeper')]), 'decision')
        self.assertEqual(self.needs(snapshot([task('t1', parent='t0', sender='ferrule')], history=[event('t1', 300)])), [])

    def test_a_task_stopped_at_two_strikes_is_listed_once_with_reinstate_and_refund(self):
        t = task('t1', failed=2, decision='Failure budget exhausted: SUBSTANCE: wrong file.', parent='t0', sender='claude-room')
        item = self.only(snapshot([t], history=[event('t1', 300)]), 'stopped')
        self.assertEqual(item['id'], 'stopped:t1:f3')
        self.assertIn('crew-shane reinstate t1', item['action']); self.assertIn('--refund', item['action'])
        for status in ('done', 'cancelled'):
            self.assertEqual(self.needs(snapshot([dict(t, status=status)])), [])

    def test_a_question_note_for_shane_is_listed_until_claude_room_or_keeper_cites_it(self):
        q = note(900, 'vesper', 'QUESTION vesper to claude-room, for Shane: may I take the second review?')
        item = self.only(snapshot(notes=[q]), 'question')
        self.assertEqual((item['id'], item['since']), ('question:900', q['timestamp']))
        self.assertIn('crew-shane note orchestra "answering #900: your answer"', item['action'])
        # another worker citing it does not answer it; #9001 is not #900
        still = [q, note(901, 'ferrule', 'I agree with #900.'), note(902, 'claude-room', 'Ruling on #9001.')]
        self.only(snapshot(notes=still), 'question')
        for answerer in ('claude-room', 'keeper'):
            self.assertEqual(self.needs(snapshot(notes=[q, note(903, answerer, 'RULING answering #900: yes.')])), [])

    def test_a_question_only_names_shane_in_its_first_line(self):
        q = note(900, 'vesper', 'QUESTION vesper to claude-room: which branch?\nShane said earlier it was main.', 10)
        self.assertEqual(self.needs(snapshot(notes=[q])), [])

    def test_a_seat_with_no_key_and_open_work_is_listed_and_one_without_work_is_not(self):
        seat = person('ferrule', key_active=False, label='Claude chat ferrule')
        work = task('t1', status='claimed', recipient='ferrule')
        item = self.only(snapshot([work], participants=[seat], history=[event('t1', 20, 'task_claim')]), 'seat')
        self.assertEqual((item['id'], item['since']), ('seat:ferrule:no-key', NOW - 1200))
        self.assertIn('Claude chat ferrule', item['summary']); self.assertIn('Start Common Room Crew.cmd', item['action'])
        self.assertEqual(self.needs(snapshot([dict(work, status='done')], participants=[seat])), [])
        self.assertEqual(self.needs(snapshot([work], participants=[dict(seat, key_active=True)])), [])
        self.assertEqual(self.needs(snapshot([work], participants=[dict(seat, legacy=True)])), [])
        # an older room has no key_active: only its 'No active key' wording counts, never stale contact
        old = {k: v for k, v in seat.items() if k != 'key_active'}
        self.assertEqual(self.needs(snapshot([work], participants=[dict(old, contact='Credential contact stale or key inactive')])), [])
        self.only(snapshot([work], participants=[dict(old, contact='No active key')]), 'seat')

    def test_claude_room_is_slow_only_after_30_minutes(self):
        helper = task('t1', parent='t0', sender='claude-room', recipient='claude-code-b')
        self.assertEqual(self.needs(snapshot([helper], history=[event('t1', 29)])), [])
        item = self.only(snapshot([helper], history=[event('t1', 31)]), 'slow')
        self.assertEqual(item['id'], 'slow:t1:f3')
        self.assertIn('claude-room', item['summary']); self.assertIn('crew-shane decide t1', item['action'])
        q = note(950, 'astra-room', 'QUESTION astra-room to claude-room: which file?', 29)
        self.assertEqual(self.needs(snapshot(notes=[q])), [])
        item = self.only(snapshot(notes=[dict(q, timestamp=NOW - 31 * 60)]), 'slow')
        self.assertEqual(item['id'], 'slow:note:950')
        answered = [dict(q, timestamp=NOW - 31 * 60), note(951, 'claude-room', 'RULING (#950): this one.')]
        self.assertEqual(self.needs(snapshot(notes=answered)), [])

    def test_kinds_come_in_order_and_oldest_first(self):
        snap = snapshot([task('t2'), task('t1'), task('t9', failed=2)],
                        notes=[note(900, 'vesper', 'QUESTION for Shane: one?')],
                        history=[event('t2', 5), event('t1', 50), event('t9', 1)],
                        stop={'active': True, 'by': 'keeper', 'at': NOW, 'reason': 'Now.'})
        self.assertEqual([i['id'] for i in self.needs(snap)],
                         ['stop:%.3f' % NOW, 'stopped:t9:f3', 'decision:t1:f3', 'decision:t2:f3', 'question:900'])


class NeedsResolutionCitations(unittest.TestCase):
    """Only visible, later resolutions by an accepted answerer close a QUESTION."""
    def question(self, seq=1377, shane=False):
        return note(seq, 'dot', 'QUESTION for ' + ('Shane' if shane else 'claude-room') + ': which file?', 40)

    def resolution(self, **changes):
        return dict({'seq': 1388, 'task_id': 't1', 'kind': 'task_resolve_decision',
                     'actor': 'claude-room', 'timestamp': NOW - 60, 'detail': 'RULING (#1377): use this file.'},
                    **changes)

    def items(self, question=None, **resolution):
        return needs_module().needs_shane(snapshot(notes=[question or self.question()],
                                                  history=[self.resolution(**resolution)]))

    def test_later_orchestrator_resolution_clears_slow_question(self):
        self.assertEqual(self.items(), [])

    def test_later_keeper_reinstatement_clears_shane_question(self):
        self.assertEqual(self.items(self.question(shane=True), kind='task_reinstate', actor='keeper'), [])

    def test_both_resolution_kinds_accept_both_answerers(self):
        for kind in ('task_resolve_decision', 'task_reinstate'):
            for actor in ('keeper', 'claude-room'):
                self.assertEqual(self.items(kind=kind, actor=actor), [], (kind, actor))

    def test_other_actor_does_not_answer(self):
        for actor in ('dot', 'ferrule', 'reviewer', None):
            self.assertEqual([i['id'] for i in self.items(actor=actor)], ['slow:note:1377'])

    def test_only_later_sequence_answers_even_when_history_is_unsorted(self):
        for seq in (1376, 1377):
            self.assertEqual([i['id'] for i in self.items(seq=seq)], ['slow:note:1377'])
        snap = snapshot(notes=[self.question()], history=[self.resolution(), self.resolution(seq=1376)])
        self.assertEqual(needs_module().needs_shane(snap), [])

    def test_citation_must_name_the_exact_number(self):
        for detail in ('Ruling #13.', 'Ruling #1378.', 'Ruling #13770.', 'Ruling #13771.'):
            self.assertEqual([i['id'] for i in self.items(detail=detail)], ['slow:note:1377'])
        self.assertEqual([i['id'] for i in self.items(self.question(seq=13))], ['slow:note:13'])

    def test_other_event_kinds_do_not_answer(self):
        for kind in ('task_cancel', 'task_finish', 'task_wait_decision', 'note', None):
            self.assertEqual([i['id'] for i in self.items(kind=kind)], ['slow:note:1377'])

    def test_missing_or_empty_detail_does_not_answer(self):
        for detail in ('', None):
            self.assertEqual([i['id'] for i in self.items(detail=detail)], ['slow:note:1377'])
        e = self.resolution()
        del e['detail']
        e['decision_response'] = 'Ruling #1377.'
        self.assertEqual([i['id'] for i in needs_module().needs_shane(
            snapshot(notes=[self.question()], history=[e]))], ['slow:note:1377'])

    def test_uses_snapshot_owner_and_does_not_mutate_snapshot(self):
        snap = snapshot(notes=[self.question(shane=True)], history=[self.resolution(actor='actual-owner')])
        snap['authorities'][0]['owner'] = 'actual-owner'
        before = copy.deepcopy(snap)
        self.assertEqual(needs_module().needs_shane(snap), [])
        self.assertEqual(snap, before)
        snap['history'][0]['actor'] = 'keeper'
        self.assertEqual([i['id'] for i in needs_module().needs_shane(snap)], ['question:1377'])

    def test_only_visible_notes_and_history_are_considered(self):
        snap = snapshot(notes=[self.question()])
        snap['tasks'] = [dict(task('t1', status='delivered'), decision_response='Ruling #1377.')]
        self.assertEqual([i['id'] for i in needs_module().needs_shane(snap)], ['slow:note:1377'])
        snap['notes'] = []
        snap['history'] = [self.resolution()]
        self.assertEqual(needs_module().needs_shane(snap), [])


class NeedsShaneIdsAndSecrets(unittest.TestCase):
    def busy(self):
        seat = person('ferrule', key_active=False)
        return snapshot([task('t1'), task('t2', parent='t0', sender='claude-room'), task('t3', failed=2),
                         task('t4', status='claimed', recipient='ferrule')],
                        notes=[note(900, 'vesper', 'QUESTION for Shane: one?'), note(901, 'hermes', 'QUESTION: two?', 40)],
                        history=[event('t1', 5), event('t2', 45), event('t3', 5), event('t4', 5)], participants=[seat],
                        stop={'active': True, 'by': 'keeper', 'at': NOW - 5, 'reason': 'Now.'})

    def test_every_kind_at_once_and_the_ids_hold_still(self):
        needs = needs_module()
        snap = self.busy()
        first = needs.needs_shane(snap)
        self.assertEqual(sorted({i['kind'] for i in first}), sorted(needs.KIND_ORDER))
        self.assertEqual(needs.needs_shane(copy.deepcopy(snap)), first)
        later = dict(copy.deepcopy(snap), server_time=NOW + 600)              # ten minutes on, nothing changed
        self.assertEqual([i['id'] for i in needs.needs_shane(later)], [i['id'] for i in first])
        self.assertEqual(len({i['id'] for i in first}), len(first))

    def test_a_new_episode_gets_a_new_id(self):
        needs = needs_module()
        before = needs.needs_shane(snapshot([task('t1', fence=3)]))[0]['id']
        after = needs.needs_shane(snapshot([task('t1', fence=7)]))[0]['id']
        self.assertNotEqual(before, after)

    def test_nothing_secret_reaches_any_field(self):
        needs = needs_module()
        leak = 'Use key ' + KEY + ' from ' + CRED + ' please'
        snap = self.busy()
        for t in snap['tasks']:
            t['decision'] = leak; t['title'] = leak
        for n in snap['notes']:
            n['body'] = n['body'] + ' ' + leak
        snap['stop']['reason'] = leak
        snap['participants'][1]['label'] = leak
        items = needs.needs_shane(snap)
        self.assertEqual(len(items), 7)
        text = json.dumps(items)
        for secret in (KEY, 'credential', '.runtime', 'claude-code-credential.json'):
            self.assertNotIn(secret, text)
        self.assertIn('[hidden]', text); self.assertIn('[path hidden]', text)

    def test_ordinary_names_and_hashes_are_not_hidden(self):
        needs = needs_module()
        line = 'core-cutover-fix at c4462c3ab18ff235aa7f1dc89747060bd3fb2978 in wt\\panels'
        self.assertEqual(needs.scrub(line, 500), line)

    def test_the_function_reads_no_clock_files_or_network(self):
        needs = needs_module()
        source = Path(needs.__file__).read_text(encoding='utf-8')
        for word in ('import time', 'import os', 'open(', 'socket', 'http', 'urllib', 'Path(', 'datetime'):
            self.assertNotIn(word, source.split('"""', 2)[2])


class BoardAndCrew(unittest.TestCase):
    """A real room from activation.start_room, as the crew CLI tests build one."""
    def start_room(self, selected=None):
        from tests.test_crew_cli import ALL_SEATS, CONFIG
        from common_room.activation import start_room
        room = start_room(case_dir('needs-panels'), enrollment=CONFIG, selected=selected or ALL_SEATS)
        self.addCleanup(room.close)
        return room

    def cli(self, room, actor, *args, json_mode=True):
        import common_room.crew as crew
        argv = ['--credential', str(room.root / (actor + '-credential.json'))] + (['--json'] if json_mode else []) + list(args)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = crew.main(argv)
        return code, out.getvalue(), err.getvalue()

    def client(self, room, actor):
        from common_room.crew_client import CrewClient
        return CrewClient(room.root / (actor + '-credential.json'))

    def asked(self, room, question='Which colour should the button be?'):
        code, out, err = self.cli(room, 'keeper', 'intake', '--scope', 'crew-scope-one', '--to', 'crew-worker-a',
                                  '--title', 'Fixture task', '--body', 'Do it.', '--acceptance', 'Right.')
        self.assertEqual(code, 0, err)
        tid = json.loads(out)['task_id']
        for args in (('ack', tid), ('claim', tid, '--seconds', '600'), ('ask', tid, '--question', question)):
            code, out, err = self.cli(room, 'crew-worker-a', *args)
            self.assertEqual(code, 0, err)
        return tid

    def test_crew_needs_exits_0_when_nothing_needs_shane(self):
        room = self.start_room()
        code, out, err = self.cli(room, 'keeper', 'needs', json_mode=False)
        self.assertEqual((code, out.strip()), (0, 'Nothing needs Shane.'), err)
        code, out, err = self.cli(room, 'keeper', 'needs')
        self.assertEqual((code, json.loads(out)), (0, []), err)

    def test_crew_needs_exits_2_and_prints_the_same_list_as_the_board(self):
        room = self.start_room()
        tid = self.asked(room)
        code, out, err = self.cli(room, 'keeper', 'needs')
        self.assertEqual(code, 2, err)
        items = json.loads(out)
        self.assertEqual([(i['kind'], i['action']) for i in items], [('decision', 'crew-shane decide ' + tid + ' --response "your answer"')])
        self.assertEqual(items, self.client(room, 'keeper').state()['needs'])
        code, out, err = self.cli(room, 'keeper', 'needs', json_mode=False)
        self.assertEqual(code, 2, err)
        self.assertIn('Needs Shane: 1 item(s)', out); self.assertIn('[decision] since ', out); self.assertIn(' CT  ', out)
        self.assertIn('do: crew-shane decide ' + tid, out)

    def test_the_board_state_carries_needs_and_key_active(self):
        room = self.start_room(selected=['crew-worker-a', 'crew-reviewer'])
        state = self.client(room, 'keeper').state()
        self.assertEqual(state['needs'], [])
        keys = {p['id']: p['key_active'] for p in state['participants']}
        self.assertEqual((keys['crew-worker-a'], keys['crew-worker-b']), (True, False))
        code, out, err = self.cli(room, 'keeper', 'intake', '--scope', 'crew-scope-one', '--to', 'crew-worker-b',
                                  '--title', 'Work for a seat without a key', '--body', 'Do it.', '--acceptance', 'Right.')
        self.assertEqual(code, 0, err)
        self.assertEqual([i['id'] for i in self.client(room, 'keeper').state()['needs']], ['seat:crew-worker-b:no-key'])
        code, out, err = self.cli(room, 'keeper', 'stop-all', '--reason', 'Testing the list.')
        self.assertEqual(code, 0, err)
        self.assertEqual([i['kind'] for i in self.client(room, 'keeper').state()['needs']], ['stop', 'seat'])

    def test_a_credential_path_in_a_question_never_reaches_the_board_list(self):
        room = self.start_room()
        path = str(room.root / 'crew-worker-a-credential.json')
        self.asked(room, 'Should I read ' + path + ' to finish?')
        state = self.client(room, 'keeper').state()
        self.assertNotIn('credential', json.dumps(state['needs']))
        self.assertIn('[path hidden]', state['needs'][0]['summary'])

    def put_usage(self, room, seat, rows, raw=()):
        folder = room.root / 'usage'; folder.mkdir(exist_ok=True)
        lines = [json.dumps(r, separators=(',', ':')) for r in rows] + list(raw)
        (folder / (seat + '.jsonl')).write_text('\n'.join(lines) + '\n', encoding='utf-8')

    def test_the_usage_panel_is_the_owners_and_counts_today_and_seven_days(self):
        room = self.start_room()
        now = datetime.datetime.now(datetime.timezone.utc)
        ts = lambda days: (now - datetime.timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')
        local = lambda days, s, pt, ot: {'ts': ts(days), 'seat': 'local-a', 'kind': 'local', 'task_id': 't0123abcd',
                                         'model': 'gemma4:31b-it-qat', 'seconds': s, 'prompt_tokens': pt, 'output_tokens': ot,
                                         'outcome': 'finished'}
        self.put_usage(room, 'local-a', [local(0, 10.0, 100, 50), local(3, 20.0, 200, None), local(9, 99.0, 999, 999)],
                       raw=['{"not": "a usage line", "secret": "' + KEY + '"}'])
        self.put_usage(room, 'claude-code', [{'ts': ts(2), 'seat': 'claude-code', 'kind': 'headless', 'task_id': 't0123abcd',
                                              'model': 'claude', 'seconds': 200.0, 'exit': 0, 'budget_cap_usd': 5.0,
                                              'outcome': 'finished'}])
        keeper = self.client(room, 'keeper')
        data = keeper._call(lambda p: p._request('/api/usage'))
        seats = {s['seat']: s for s in data['seats']}
        self.assertEqual(sorted(seats), ['claude-code', 'local-a'])
        self.assertEqual(seats['local-a']['week'], {'runs': 2, 'seconds': 30.0, 'prompt_tokens': 300, 'output_tokens': 50})
        self.assertEqual(seats['local-a']['today'], {'runs': 1, 'seconds': 10.0, 'prompt_tokens': 100, 'output_tokens': 50})
        self.assertEqual(seats['claude-code']['week'], {'runs': 1, 'seconds': 200.0, 'prompt_tokens': None, 'output_tokens': None})
        self.assertEqual(seats['claude-code']['today']['runs'], 0)
        self.assertEqual(data['skipped'], 1)
        self.assertNotIn(KEY, json.dumps(data)); self.assertNotIn(str(room.root), json.dumps(data))
        worker = self.client(room, 'crew-worker-a')
        from common_room.crew_client import CrewError
        with self.assertRaises(CrewError) as refused:
            worker._call(lambda p: p._request('/api/usage'))
        self.assertEqual(refused.exception.code, 'forbidden')

    def test_usage_totals_are_numbers_only(self):
        from common_room.board import usage_summary
        rows = [{'ts': '2026-10-05T12:00:00Z', 'seat': 'local-b', 'kind': 'local', 'seconds': 1.25, 'prompt_tokens': None,
                 'output_tokens': None, 'outcome': 'skipped_quiet', 'task_id': 't1', 'model': 'm'}]
        at = datetime.datetime(2026, 10, 5, 12, 0, tzinfo=datetime.timezone.utc).timestamp()
        self.assertEqual(usage_summary(rows, at + 60, at - 3600),
                         [{'seat': 'local-b', 'kind': 'local', 'today': {'runs': 1, 'seconds': 1.2, 'prompt_tokens': None, 'output_tokens': None},
                           'week': {'runs': 1, 'seconds': 1.2, 'prompt_tokens': None, 'output_tokens': None}}])
        self.assertEqual(usage_summary(rows, at + 8 * 86400, at + 8 * 86400 - 3600), [])

    # The real web/board.js against a fake page, as tests/test_stop_all.py runs it.
    def views(self, states, usage):
        node = os.environ.get('COMMON_ROOM_NODE') or shutil.which('node')
        if not node:
            self.skipTest('node not found: set COMMON_ROOM_NODE or put node on PATH')
        run = subprocess.run([node, '-e', HARNESS], input=json.dumps({'states': states, 'usage': usage}),
                             capture_output=True, text=True, encoding='utf-8', cwd=str(ROOT), timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def test_the_page_shows_needs_first_and_usage_to_the_owner_only(self):
        html = (ROOT / 'web' / 'index.html').read_text(encoding='utf-8')
        self.assertRegex(html, r'<div id="workspace" hidden>\s*<section id="needs-panel"')
        room = self.start_room()
        calm = {a: self.client(room, a).state() for a in ('keeper', 'crew-worker-a')}
        self.asked(room)
        busy = self.client(room, 'keeper').state()
        usage = {'seats': [{'seat': 'local-a', 'kind': 'local', 'today': {'runs': 1, 'seconds': 10.0, 'prompt_tokens': 100, 'output_tokens': 50},
                            'week': {'runs': 2, 'seconds': 4000.0, 'prompt_tokens': 300, 'output_tokens': 50}}], 'skipped': 1}
        seen = self.views({'calm-owner': calm['keeper'], 'calm-worker': calm['crew-worker-a'], 'busy-owner': busy}, usage)
        self.assertEqual((seen['calm-owner']['empty_hidden'], seen['calm-owner']['empty']), (False, 'Nothing needs you.'))
        self.assertEqual((seen['calm-worker']['empty_hidden'], seen['calm-worker']['empty']), (False, 'Nothing needs Shane.'))
        self.assertEqual(seen['calm-owner']['items'], [])
        owner = seen['busy-owner']
        self.assertTrue(owner['empty_hidden'])
        self.assertEqual(len(owner['items']), 1)
        self.assertEqual(owner['items'][0]['id'], busy['needs'][0]['id'])
        self.assertEqual(owner['items'][0]['text'][:3], ['Your decision', busy['needs'][0]['summary'], busy['needs'][0]['action']])
        self.assertTrue(owner['items'][0]['text'][3].startswith('Since ') and owner['items'][0]['text'][3].endswith(' CT'))
        self.assertEqual(owner['count'], '1 item')
        self.assertEqual((seen['calm-worker']['usage_hidden'], seen['calm-worker']['usage_fetched']), (True, False))
        self.assertFalse(seen['calm-owner']['usage_hidden'])
        self.assertEqual(seen['calm-owner']['usage_rows'], [['Seat', 'Today', 'Last 7 days'],
                                                            ['local-a (local)', '1 run · 10 s · 150 tokens', '2 runs · 67 min · 350 tokens']])
        self.assertIn('1 damaged usage line(s) skipped', seen['calm-owner']['usage_text'])


HARNESS = r'''
const fs=require('fs'),vm=require('vm'),path=require('path'),nodeCrypto=require('crypto');
const {PendingJournal}=require(path.resolve('web/recovery.js'));
const source=fs.readFileSync('web/board.js','utf8');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
function make(tag){return {tagName:String(tag).toUpperCase(),children:[],dataset:{},listeners:{},hidden:false,disabled:false,required:false,value:'',textContent:'',className:'',
  get options(){return this.children.filter(x=>x.tagName==='OPTION');},get firstChild(){return this.children[0];},
  append(...k){this.children.push(...k);},replaceChildren(...k){this.children=[...k];},after(){},focus(){},setAttribute(k,v){this[k]=v;},
  addEventListener(t,f){(this.listeners[t]=this.listeners[t]||[]).push(f);}};}
const settle=async()=>{for(let i=0;i<30;i++)await new Promise(r=>setImmediate(r));};
const texts=node=>node.children.map(c=>c.textContent);
async function view(state){
  const ids={},paths=[];
  const document={getElementById:id=>ids[id]||(ids[id]=make('div')),createElement:make,querySelector:()=>make('p'),hidden:false};
  for(const m of fs.readFileSync('web/index.html','utf8').matchAll(/<(\w+)\b([^>]*)>/g)){
   const id=/\bid="([^"]+)"/.exec(m[2]);if(!id)continue;const el=document.getElementById(id[1]);
   if(/(^|\s)hidden(\s|$|=)/.test(m[2]))el.hidden=true;if(/(^|\s)disabled(\s|$|=)/.test(m[2]))el.disabled=true;}
  const reply=p=>p==='/api/usage'?input.usage:p==='/api/plans'?{configured:false,plans:[],decisions:[]}:state;
  const context=vm.createContext({document,PendingJournal,console,crypto:nodeCrypto,setInterval:()=>0,Intl,
   sessionStorage:{getItem:()=>null,setItem(){},removeItem(){}},
   fetch:async(p,o)=>{paths.push(p);return {ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(reply(p)))};}});
  vm.runInContext(source,context);await settle();
  const g=id=>ids[id],table=g('usage').children.find(c=>c.tagName==='TABLE');
  return {empty_hidden:g('needs-empty').hidden,empty:g('needs-empty').textContent,count:g('needs-count').textContent,
   items:g('needs-list').children.map(li=>({id:li.dataset.needId,text:texts(li)})),
   usage_hidden:g('usage-panel').hidden,usage_fetched:paths.includes('/api/usage'),
   usage_rows:table?table.children.map(texts):[],usage_text:texts(g('usage')).join(' ')};
}
(async()=>{const out={};for(const [name,state] of Object.entries(input.states))out[name]=await view(state);
  process.stdout.write(JSON.stringify(out));})().catch(e=>{console.error(e.stack||String(e));process.exit(1);});
'''


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0], *[arg for arg in sys.argv[1:] if arg != '.']])
