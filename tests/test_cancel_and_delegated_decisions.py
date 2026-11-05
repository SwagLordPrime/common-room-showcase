"""Cancel, delegated helper decisions and the helper caps (orchestra build, gate K, 2026-09-24).

What changed in the authority core, and what these tests hold it to:

  * new action `cancel` {task_id, body}: the authority owner on any task, or the SENDER of a
    helper task (one `request_help` created, intake namespace `room-helper`) on that helper.
    Everyone else is refused `forbidden`. Done or cancelled work is refused `state`. A live
    lease does not stop it: the fence moves, so the worker's later finish/fail is refused
    `lease`. `failed_attempts` is never touched.
  * `resolve_decision` may also be made by the sender of a helper task. The two-strike
    refusal and the state check are unchanged. `reinstate` stays owner-only.
  * a cancelled helper no longer blocks its parent's finish/assign.
  * helper caps: at most 8 helpers that are not cancelled, and at most 24 in total.
  * old event logs fold to identical state (replay tests at the bottom).
"""
import copy, hashlib, http.client, json, os, shutil, subprocess, threading, types, unittest
from pathlib import Path
from unittest import mock
from common_room.board import make_server
from common_room.handoff_api import CredentialPolicy
from common_room.participant import Participant
from common_room.protocol import Actor, canonical
from common_room.store import Store, fold, read_events
from tests.helpers import ROOT, case_dir
from tests.test_store import Clock, StoreFixture

BASELINE_COMMIT = 'c15d475'


class DelegationFixture(StoreFixture):
    """A handoff scope with three workers: builder delegates, second helps, third only watches."""
    def setUp(self):
        super().setUp()
        self.people['third'] = Actor('third', 'worker')
        self.third = self.people['third']; self.visitor = self.people['visitor']
        self.db = self.home / 'delegation.sqlite'
        self.store = Store(self.db, self.people, self.roots, clock=self.clock)
        self.grant('work')
        self.call(self.owner, 'grant_handoff', {'id': 'orch', 'members': ['owner', 'builder', 'second', 'third', 'reviewer', 'visitor'],
            'capabilities': ['handoff.record'], 'root_id': 'fixture', 'scope': 'Fictional orchestra work only', 'source_namespaces': ['estate']})
        self.refs = 0

    def root(self, ref='root', recipient='builder'):
        msg = self.message('orch', kind='handoff.record', recipient=recipient, title='Workstream ' + ref)
        task = self.call(self.owner, 'intake', {'source_namespace': 'estate', 'external_reference': ref, 'message': msg})['task']['id']
        self.call(self.owner, 'deliver', {'task_id': task})
        return task

    def claimed_root(self, ref='root'):
        task = self.root(ref); self.call(self.builder, 'ack', {'task_id': task})
        return task, self.claim(task)

    def helper(self, parent, lease, recipient='second', title='Helper piece'):
        return self.call(self.builder, 'request_help', {'task_id': parent, 'fence': lease['fence'], 'recipient': recipient,
            'title': title, 'body': 'Check one fictional piece only.', 'acceptance': 'A saved result and separate review.'})['task']['id']

    def unique_ref(self):
        self.refs += 1
        content = ('Fictional result ' + str(self.refs) + '\n').encode()
        name = 'result-' + str(self.refs) + '.txt'; (self.art / name).write_bytes(content)
        return {'path': name, 'sha256': hashlib.sha256(content).hexdigest()}

    def cancel(self, actor, task, body='No longer needed.'):
        return self.call(actor, 'cancel', {'task_id': task, 'body': body})['task']

    def resolve(self, actor, task, body='Use the approved wording.'):
        return self.call(actor, 'resolve_decision', {'task_id': task, 'body': body})['task']

    def asking_helper(self):
        """A helper whose worker has asked its delegator a question."""
        parent, lease = self.claimed_root()
        child = self.helper(parent, lease)
        self.call(self.second, 'ack', {'task_id': child}); held = self.claim(child, self.second)
        self.call(self.second, 'wait_decision', {'task_id': child, 'fence': held['fence'], 'question': 'Which of the two wordings?'})
        return parent, lease, child

    def exhausted_helper(self):
        parent, lease = self.claimed_root()
        child = self.helper(parent, lease)
        self.call(self.second, 'ack', {'task_id': child})
        for _ in (1, 2):
            held = self.claim(child, self.second)
            self.call(self.second, 'fail', {'task_id': child, 'fence': held['fence'], 'body': 'Fictional failed attempt'})
        stopped = self.store.task(self.owner, child)
        self.assertEqual((stopped['status'], stopped['failed_attempts']), ('waiting_decision', 2))
        return parent, lease, child


class CancelTests(DelegationFixture):
    def test_owner_cancels_a_root_task(self):
        task = self.root(); before = self.store.task(self.owner, task)
        after = self.cancel(self.owner, task, 'Superseded by the new plan.')
        self.assertEqual(after['status'], 'cancelled')
        self.assertIsNone(after['lease']); self.assertIsNone(after['decision'])
        self.assertEqual(after['fence'], before['fence'] + 1)
        self.assertEqual(after['cancel_note'], 'Superseded by the new plan.')
        self.assertEqual(after['cancelled_by'], 'owner')
        self.assertEqual(after['failed_attempts'], before['failed_attempts'])
        event = self.store.poll(self.owner)['events'][-1]
        self.assertEqual((event['kind'], event['task_id'], event['actor']), ('task_cancel', task, 'owner'))
        self.assertEqual(self.store.task(self.owner, task), after)
        # An owner may also withdraw a queued draft.
        draft = self.call(self.owner, 'intake', {'source_namespace': 'estate', 'external_reference': 'draft',
            'message': self.message('orch', kind='handoff.record')})['task']['id']
        self.assertEqual(self.cancel(self.owner, draft)['status'], 'cancelled')

    def test_helper_sender_cancels_its_own_helper(self):
        parent, lease = self.claimed_root(); child = self.helper(parent, lease)
        after = self.cancel(self.builder, child, 'Delegated the wrong piece.')
        self.assertEqual((after['status'], after['cancelled_by'], after['cancel_note']), ('cancelled', 'builder', 'Delegated the wrong piece.'))
        self.assertEqual(self.store.task(self.owner, parent)['status'], 'claimed')

    def test_a_different_worker_in_the_scope_cannot_cancel_or_resolve_a_helper(self):
        parent, lease, child = self.asking_helper()
        for actor in (self.third, self.second, self.reviewer, self.visitor):
            self.error('forbidden', lambda a=actor: self.cancel(a, child))
            self.error('forbidden', lambda a=actor: self.resolve(a, child))
        task = self.store.task(self.owner, child)
        self.assertEqual(task['status'], 'waiting_decision'); self.assertNotIn('cancel_note', task)

    def test_workers_reviewer_and_observer_cannot_cancel_a_root_task(self):
        task, lease = self.claimed_root()
        for actor in (self.builder, self.second, self.third, self.reviewer, self.visitor):
            self.error('forbidden', lambda a=actor: self.cancel(a, task))
        self.assertEqual(self.store.task(self.owner, task)['status'], 'claimed')

    def test_recipient_worker_cannot_cancel_its_own_task(self):
        task, _ = self.claimed_root()
        self.error('forbidden', lambda: self.cancel(self.builder, task))
        parent, lease = self.claimed_root('second-root'); child = self.helper(parent, lease)
        self.error('forbidden', lambda: self.cancel(self.second, child))
        self.assertEqual(self.store.task(self.owner, child)['status'], 'delivered')

    def test_sender_of_a_task_that_is_not_a_helper_cannot_cancel_it(self):
        # A worker may create a count task; being its sender is not being its owner.
        task = self.call(self.builder, 'create', self.message(recipient='second'))['task']['id']
        self.assertEqual(self.store.task(self.owner, task)['sender'], 'builder')
        self.error('forbidden', lambda: self.cancel(self.builder, task))
        self.assertEqual(self.cancel(self.owner, task)['status'], 'cancelled')

    def test_cancel_of_done_or_cancelled_work_is_refused(self):
        parent, lease = self.claimed_root(); child = self.helper(parent, lease)
        self.call(self.second, 'ack', {'task_id': child}); held = self.claim(child, self.second)
        self.call(self.second, 'finish', {'task_id': child, 'fence': held['fence'], 'body': 'Done', 'refs': [self.unique_ref()]})
        self.call(self.reviewer, 'review', {'task_id': child, 'accepted': True, 'note': 'Read the saved piece'})
        for actor in (self.owner, self.builder):
            self.error('state', lambda a=actor: self.cancel(a, child))
        other = self.helper(parent, lease, title='Second piece'); self.cancel(self.builder, other, 'First reason')
        for actor in (self.owner, self.builder):
            self.error('state', lambda a=actor: self.cancel(a, other, 'Second reason'))
        self.assertEqual(self.store.task(self.owner, other)['cancel_note'], 'First reason')
        self.assertEqual(self.store.task(self.owner, child)['status'], 'done')

    def test_cancel_during_a_live_lease_fences_the_worker_out(self):
        task, lease = self.claimed_root()
        after = self.cancel(self.owner, task, 'Stop this workstream now.')
        self.assertGreater(after['fence'], lease['fence']); self.assertIsNone(after['lease'])
        self.error('lease', lambda: self.finish(task, lease))
        self.error('lease', lambda: self.call(self.builder, 'fail', {'task_id': task, 'fence': lease['fence'], 'body': 'late'}))
        self.error('lease', lambda: self.call(self.builder, 'renew', {'task_id': task, 'fence': lease['fence'], 'seconds': 300}))
        self.error('lease', lambda: self.call(self.builder, 'wait_decision', {'task_id': task, 'fence': lease['fence'], 'question': 'late?'}))
        self.error('state', lambda: self.claim(task))
        self.error('state', lambda: self.call(self.owner, 'assign', {'task_id': task, 'recipient': 'second'}))
        self.assertEqual(self.store.task(self.owner, task)['status'], 'cancelled')
        # The same holds for a helper cancelled by its sender while its worker holds it.
        parent, plea = self.claimed_root('parent'); child = self.helper(parent, plea)
        self.call(self.second, 'ack', {'task_id': child}); held = self.claim(child, self.second)
        self.cancel(self.builder, child)
        self.error('lease', lambda: self.finish(child, held, self.second, refs=[self.unique_ref()]))
        self.error('lease', lambda: self.call(self.second, 'fail', {'task_id': child, 'fence': held['fence'], 'body': 'late'}))

    def test_cancel_keeps_recorded_failures(self):
        # 2026-09-24 (root, after verification F3): a two-strike helper is stopped work; only the
        # owner withdraws it. The sender is refused so it cannot re-issue the same work with a
        # fresh budget without Shane.
        parent, lease, child = self.exhausted_helper()
        self.error('budget', lambda: self.cancel(self.builder, child, 'Two strikes; withdrawing it.'))
        after = self.cancel(self.owner, child, 'Two strikes; withdrawing it.')
        self.assertEqual((after['status'], after['failed_attempts']), ('cancelled', 2))
        self.assertIsNone(after['decision'])
        self.assertEqual(after['failure_note'], 'Fictional failed attempt')

    def test_cancel_needs_exact_fields_and_a_bounded_reason(self):
        task = self.root()
        for body in ('', '   ', 'x' * 2001, 'bad\x00reason', 7):
            self.error('invalid', lambda b=body: self.cancel(self.owner, task, b))
        self.error('invalid', lambda: self.call(self.owner, 'cancel', {'task_id': task}))
        self.error('invalid', lambda: self.call(self.owner, 'cancel', {'task_id': task, 'body': 'ok', 'fence': 1}))
        self.assertEqual(self.store.task(self.owner, task)['status'], 'delivered')
        self.assertEqual(self.cancel(self.owner, task, 'y' * 2000)['cancel_note'], 'y' * 2000)

    def test_cancel_retry_with_the_same_key_returns_the_same_receipt(self):
        task = self.root()
        first = self.call(self.owner, 'cancel', {'task_id': task, 'body': 'Withdrawn'}, 'cancel-once')
        again = self.call(self.owner, 'cancel', {'task_id': task, 'body': 'Withdrawn'}, 'cancel-once')
        self.assertEqual(first, again)
        self.assertEqual([e['kind'] for e in self.store.poll(self.owner)['events']].count('task_cancel'), 1)

    def test_cancel_follows_the_scope_pause_like_every_task_action(self):
        task = self.root(); self.call(self.owner, 'pause', {'authority_id': 'orch'})
        self.error('held', lambda: self.cancel(self.owner, task))
        self.call(self.owner, 'resume', {'authority_id': 'orch'})
        self.assertEqual(self.cancel(self.owner, task)['status'], 'cancelled')

    def test_a_cancelled_task_cannot_be_reinstated_back_to_life(self):
        parent, lease = self.claimed_root(); child = self.helper(parent, lease)
        self.call(self.second, 'ack', {'task_id': child}); held = self.claim(child, self.second)
        self.call(self.second, 'fail', {'task_id': child, 'fence': held['fence'], 'body': 'One fictional strike'})
        self.cancel(self.owner, child)
        self.error('state', lambda: self.call(self.owner, 'reinstate', {'task_id': child, 'body': 'Bring it back'}))
        self.error('state', lambda: self.resolve(self.owner, child))
        self.error('state', lambda: self.resolve(self.builder, child))
        after = self.store.task(self.owner, child)
        self.assertEqual((after['status'], after['failed_attempts']), ('cancelled', 1))


class DelegatedDecisionTests(DelegationFixture):
    def test_helper_sender_answers_its_helpers_question(self):
        parent, lease, child = self.asking_helper()
        before = self.store.task(self.owner, child)
        after = self.resolve(self.builder, child, 'Use the shorter wording.')
        self.assertEqual(after['status'], 'delivered'); self.assertIsNone(after['decision'])
        self.assertEqual(after['decision_response'], 'Use the shorter wording.')
        self.assertEqual(after['fence'], before['fence'] + 1); self.assertIsNone(after['lease'])
        self.call(self.second, 'ack', {'task_id': child})
        self.assertGreater(self.claim(child, self.second)['fence'], before['fence'])

    def test_helper_sender_answers_a_returned_review(self):
        parent, lease = self.claimed_root(); child = self.helper(parent, lease)
        self.call(self.second, 'ack', {'task_id': child}); held = self.claim(child, self.second)
        self.call(self.second, 'finish', {'task_id': child, 'fence': held['fence'], 'body': 'Draft', 'refs': [self.unique_ref()]})
        self.call(self.reviewer, 'review', {'task_id': child, 'accepted': False, 'note': 'One fix needed'})
        after = self.resolve(self.builder, child, 'Make the one fix.')
        self.assertEqual((after['status'], after['failed_attempts']), ('delivered', 1))

    def test_helper_sender_cannot_answer_at_two_strikes(self):
        parent, lease, child = self.exhausted_helper()
        self.error('budget', lambda: self.resolve(self.builder, child))
        self.error('budget', lambda: self.resolve(self.owner, child))
        self.assertEqual(self.store.task(self.owner, child)['failed_attempts'], 2)

    def test_resolve_still_needs_a_waiting_decision(self):
        parent, lease = self.claimed_root(); child = self.helper(parent, lease)
        self.error('state', lambda: self.resolve(self.builder, child))
        self.error('state', lambda: self.resolve(self.owner, child))

    def test_helper_sender_cannot_reinstate_but_the_owner_still_can(self):
        parent, lease, child = self.exhausted_helper()
        for actor in (self.builder, self.second, self.third, self.reviewer):
            self.error('forbidden', lambda a=actor: self.call(a, 'reinstate', {'task_id': child, 'body': 'Clear the strikes'}))
        self.assertEqual(self.store.task(self.owner, child)['failed_attempts'], 2)
        back = self.call(self.owner, 'reinstate', {'task_id': child, 'body': 'Idle worker, not a failure'})['task']
        self.assertEqual((back['status'], back['failed_attempts']), ('delivered', 0))

    def test_sender_power_does_not_reach_the_parent_or_other_tasks(self):
        parent, lease, child = self.asking_helper()
        self.call(self.builder, 'wait_decision', {'task_id': parent, 'fence': lease['fence'], 'question': 'Parent question'})
        self.error('forbidden', lambda: self.resolve(self.builder, parent))
        self.error('forbidden', lambda: self.cancel(self.builder, parent))
        self.assertEqual(self.resolve(self.owner, parent)['status'], 'delivered')


class ParentAndCapTests(DelegationFixture):
    def test_cancelled_helper_unblocks_the_parent_finish(self):
        parent, lease = self.claimed_root()
        first = self.helper(parent, lease, title='First'); second = self.helper(parent, lease, title='Second')
        self.error('helpers_pending', lambda: self.finish(parent, lease, refs=[self.unique_ref()]))
        self.cancel(self.builder, first)
        self.error('helpers_pending', lambda: self.finish(parent, lease, refs=[self.unique_ref()]))
        self.cancel(self.owner, second)
        self.assertEqual(self.finish(parent, lease, refs=[self.unique_ref()])['task']['status'], 'ready_for_review')

    def test_cancelled_helper_unblocks_the_parent_reassignment(self):
        parent, lease = self.claimed_root(); child = self.helper(parent, lease)
        self.clock.now += 301
        self.error('helpers_pending', lambda: self.call(self.owner, 'assign', {'task_id': parent, 'recipient': 'second'}))
        self.cancel(self.owner, child)
        self.assertEqual(self.call(self.owner, 'assign', {'task_id': parent, 'recipient': 'second'})['task']['recipient'], 'second')

    def test_live_helper_cap_is_eight_and_cancelling_frees_a_slot(self):
        parent, lease = self.claimed_root()
        children = [self.helper(parent, lease, title='Piece ' + str(i)) for i in range(8)]
        self.error('budget', lambda: self.helper(parent, lease, title='Ninth'))
        self.cancel(self.builder, children[0])
        replacement = self.helper(parent, lease, title='Replacement')
        self.assertEqual(self.store.task(self.owner, replacement)['status'], 'delivered')
        self.error('budget', lambda: self.helper(parent, lease, title='Ninth again'))

    def test_done_helpers_free_a_live_slot(self):
        # 2026-09-24 (root, after verification F7): "8 live" means 8 OPEN helpers (not done, not
        # cancelled); the 24 ceiling still bounds the total. A 9th open one is still refused.
        parent, lease = self.claimed_root()
        children = [self.helper(parent, lease, title='Piece ' + str(i)) for i in range(8)]
        self.error('budget', lambda: self.helper(parent, lease, title='Ninth while eight are open'))
        self.call(self.second, 'ack', {'task_id': children[0]}); held = self.claim(children[0], self.second)
        self.call(self.second, 'finish', {'task_id': children[0], 'fence': held['fence'], 'body': 'Done', 'refs': [self.unique_ref()]})
        self.call(self.reviewer, 'review', {'task_id': children[0], 'accepted': True, 'note': 'Fine'})
        self.helper(parent, lease, title='Ninth after one was accepted')

    def test_total_helper_cap_is_twenty_four_cancelled_included(self):
        parent, lease = self.claimed_root(); made = 0
        for round_ in range(3):
            batch = [self.helper(parent, lease, title='Round ' + str(round_) + ' piece ' + str(i)) for i in range(8)]
            made += len(batch)
            if round_ < 2:
                for child in batch: self.cancel(self.builder, child)
        self.assertEqual(made, 24)
        self.cancel(self.builder, batch[0])
        live = sum(t['parent_id'] == parent and t['status'] != 'cancelled' for t in self.store.list_tasks(self.owner))
        self.assertEqual(live, 7)
        self.error('budget', lambda: self.helper(parent, lease, title='Twenty-fifth'))
        self.assertEqual(sum(t['parent_id'] == parent for t in self.store.list_tasks(self.owner)), 24)


class CancelOverHTTPTests(DelegationFixture):
    def setUp(self):
        super().setUp()
        self.tokens = {'owner-key': self.owner, 'builder-key': self.builder, 'second-key': self.second,
                       'third-key': self.third, 'review-key': self.reviewer}
        self.policy = CredentialPolicy(self.tokens, self.clock.now + 28800, self.clock)
        self.server = make_server(self.store, self.tokens, clock=self.clock, credential_policy=self.policy)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)

    def client(self, token):
        c = Participant(self.server.origin, token); c.login(); return c

    def status(self):
        c = http.client.HTTPConnection(self.server.host, timeout=3)
        try:
            c.request('GET', '/api/status'); r = c.getresponse(); return json.loads(r.read())
        finally: c.close()

    def test_participant_client_and_board_carry_cancel_and_status_counts_it(self):
        parent, lease = self.claimed_root(); child = self.helper(parent, lease)
        with self.assertRaisesRegex(ValueError, 'forbidden'):
            self.client('third-key').command('cancel', {'task_id': child, 'body': 'Not mine'}, 'third-cancel')
        done = self.client('builder-key').command('cancel', {'task_id': child, 'body': 'Withdrawn by its delegator'}, 'builder-cancel')
        self.assertEqual(done['task']['status'], 'cancelled')
        counts = self.status()['counts']
        self.assertEqual(counts, {'claimed': 1, 'cancelled': 1})
        self.client('owner-key').command('cancel', {'task_id': parent, 'body': 'Whole workstream withdrawn'}, 'owner-cancel')
        self.assertEqual(self.status()['counts'], {'cancelled': 2})

    def board_views(self, tokens, submit=None):
        node = os.environ.get('COMMON_ROOM_NODE') or shutil.which('node')
        if not node: self.skipTest('node not found: set COMMON_ROOM_NODE or put node on PATH')
        states = {token: self.client(token).state() for token in tokens}
        run = subprocess.run([node, '-e', BOARD_HARNESS], input=json.dumps({'states': states, 'submit': submit}),
                             capture_output=True, text=True, cwd=str(ROOT), timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def test_board_offers_cancel_and_answer_only_to_permitted_actors(self):
        index = (ROOT / 'web' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('<option>cancelled</option>', index.split('id="filter-status"', 1)[1].split('</select>', 1)[0])
        parent, lease, asking = self.asking_helper()
        waiting = self.helper(parent, lease, title='Still waiting')
        withdrawn = self.helper(parent, lease, title='Withdrawn piece')
        self.call(self.second, 'ack', {'task_id': withdrawn}); held = self.claim(withdrawn, self.second)
        self.call(self.second, 'fail', {'task_id': withdrawn, 'fence': held['fence'], 'body': 'One strike'})
        self.cancel(self.builder, withdrawn, 'Superseded <b>by</b> a narrower piece')
        other_root = self.root('other', recipient='second')
        views = self.board_views(['owner-key', 'builder-key', 'second-key', 'third-key', 'review-key'])

        def buttons(token, task): return views[token]['cards'][task]['buttons']
        live = [parent, asking, waiting, other_root]
        for task in live: self.assertIn('Cancel task', buttons('owner-key', task), task)
        self.assertIn('Record decision', buttons('owner-key', asking))
        for task in (asking, waiting): self.assertIn('Cancel task', buttons('builder-key', task))
        self.assertIn('Answer question', buttons('builder-key', asking))
        for task in (parent, other_root): self.assertNotIn('Cancel task', buttons('builder-key', task))
        for token in ('second-key', 'third-key', 'review-key'):
            for task in live:
                for label in ('Cancel task', 'Answer question', 'Record decision'):
                    self.assertNotIn(label, buttons(token, task), (token, task))
        for token in ('owner-key', 'builder-key', 'second-key', 'third-key', 'review-key'):
            card = views[token]['cards'][withdrawn]
            self.assertNotIn('Cancel task', card['buttons']); self.assertNotIn('Reinstate', ' '.join(card['buttons']))
            self.assertEqual(card['column'], 'Done'); self.assertEqual(card['status'], 'cancelled')
            self.assertIn('Cancelled by builder: Superseded <b>by</b> a narrower piece', card['texts'])

    def test_board_cancel_form_needs_a_bounded_reason_and_posts_cancel(self):
        parent, lease = self.claimed_root(); child = self.helper(parent, lease)
        view = self.board_views(['builder-key'], submit={'task': child, 'reason': 'Wrong piece delegated'})['builder-key']
        form = view['cancel_form']
        self.assertIsNotNone(form, 'the helper sender sees no Cancel task control')
        self.assertEqual((form['maxLength'], form['required']), (2000, True))
        self.assertEqual(form['count_before'], '0 / 2000 characters')
        self.assertEqual(form['count_after'], str(len('Wrong piece delegated')) + ' / 2000 characters')
        posted = [r['body'] for r in view['requests'] if r['path'] == '/api/command']
        self.assertEqual(len(posted), 1)
        self.assertEqual((posted[0]['action'], posted[0]['payload']), ('cancel', {'task_id': child, 'body': 'Wrong piece delegated'}))


# Runs the real web/board.js render against a minimal fake DOM, once per signed-in actor.
BOARD_HARNESS = r'''
const fs=require('fs'),vm=require('vm'),path=require('path'),nodeCrypto=require('crypto');
const {PendingJournal}=require(path.resolve('web/recovery.js'));
const source=fs.readFileSync('web/board.js','utf8');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
function make(tag){return {tagName:String(tag).toUpperCase(),children:[],dataset:{},listeners:{},hidden:false,disabled:false,required:false,value:'',textContent:'',className:'',
 get options(){return this.children.filter(x=>x.tagName==='OPTION');},get firstChild(){return this.children[0];},
 append(...k){this.children.push(...k);},replaceChildren(...k){this.children=[...k];},after(){},focus(){},setAttribute(k,v){this[k]=v;},
 addEventListener(t,f){(this.listeners[t]=this.listeners[t]||[]).push(f);}};}
const settle=async()=>{for(let i=0;i<30;i++)await new Promise(r=>setImmediate(r));};
async function view(state,submit){
 const ids={},requests=[];
 const document={getElementById:id=>ids[id]||(ids[id]=make('div')),createElement:make,querySelector:()=>make('p'),hidden:false};
 const context=vm.createContext({document,PendingJournal,console,crypto:nodeCrypto,setInterval:()=>0,
  sessionStorage:{getItem:()=>null,setItem(){},removeItem(){}},
  fetch:async(p,o)=>{requests.push({path:p,body:o&&o.body?JSON.parse(o.body):null});return {ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(state))};}});
 vm.runInContext(source,context);await settle();
 const cards={};let cancelForm=null;
 for(const column of ids['board'].children)for(const card of column.children.filter(x=>x.tagName==='ARTICLE')){
  const buttons=card.children.filter(x=>x.tagName==='BUTTON');
  cards[card.dataset.taskId]={column:column.dataset.column,status:card.children[0].textContent,buttons:buttons.map(b=>b.textContent),
   texts:card.children.filter(x=>x.tagName==='P').map(x=>x.textContent)};
  if(submit&&card.dataset.taskId===submit.task){
   const open=buttons.find(b=>b.textContent==='Cancel task');if(!open)continue;
   const form=card.children[card.children.indexOf(open)+1];
   const area=form.children.find(x=>x.tagName==='TEXTAREA'),count=form.children.find(x=>x.tagName==='SMALL');
   open.onclick();cancelForm={maxLength:area.maxLength,required:area.required,count_before:count.textContent,hidden_after_open:form.hidden};
   area.value=submit.reason;for(const f of area.listeners.input||[])f();cancelForm.count_after=count.textContent;
   requests.length=0;await form.onsubmit({preventDefault(){}});await settle();
  }
 }
 return {cards,cancel_form:cancelForm,requests};
}
(async()=>{const out={};for(const [token,state] of Object.entries(input.states))out[token]=await view(state,input.submit);
 process.stdout.write(JSON.stringify(out));})().catch(e=>{console.error(e.stack||String(e));process.exit(1);});
'''


# ---------------------------------------------------------------- replay identity

def normalize(events):
    """Drop the machine-specific artifact root so a log recorded elsewhere compares exactly."""
    events = copy.deepcopy(events)
    for event in events:
        if event['kind'] == 'authority_granted':
            event['payload']['authority']['artifact_root'] = 'LEGACY-ROOT'
    return events


def state_digest(fold_fn, events):
    authorities, tasks = fold_fn(copy.deepcopy(events))
    return hashlib.sha256(canonical({'authorities': authorities, 'tasks': tasks}).encode('utf-8')).hexdigest()


def legacy_scenario(store_cls, home):
    """Drive a workflow with only the actions that existed at c15d475; return its normalized log.

    Task ids come from a patched uuid4 and time from a fixed clock, so the log is byte-stable.
    """
    art = home / 'artifacts'; art.mkdir()
    people = {a.id: a for a in [Actor('owner', 'owner'), Actor('builder', 'worker'), Actor('second', 'worker'), Actor('reviewer', 'reviewer')]}
    clock = Clock(); store = store_cls(home / 'legacy.sqlite', people, {'fixture': art}, clock=clock); n = [0]
    content = b'Legacy fictional result.\n'; (art / 'result.txt').write_bytes(content)
    ref = {'path': 'result.txt', 'sha256': hashlib.sha256(content).hexdigest()}
    def call(actor, action, payload):
        n[0] += 1; clock.now += 1
        return store.execute(people[actor], action, payload, 'legacy-' + str(n[0]))
    def msg(title, recipient='builder'):
        return {'title': title, 'body': 'Legacy fixture body.', 'recipient': recipient, 'authority_id': 'orch',
                'kind': 'handoff.record', 'acceptance': 'A saved result and separate review.'}
    counter = iter(range(1, 1000))
    import uuid as uuid_module
    with mock.patch.object(uuid_module, 'uuid4', side_effect=lambda: uuid_module.UUID(int=next(counter))):
        call('owner', 'grant_handoff', {'id': 'orch', 'members': ['owner', 'builder', 'second', 'reviewer'], 'capabilities': ['handoff.record'],
             'root_id': 'fixture', 'scope': 'Legacy replay fixture', 'source_namespaces': ['estate']})
        call('owner', 'grant', {'id': 'count', 'members': ['owner', 'builder', 'second', 'reviewer'], 'capabilities': ['document.count'],
             'root_id': 'fixture', 'scope': 'Legacy count fixture'})
        parent = call('owner', 'intake', {'source_namespace': 'estate', 'external_reference': 'legacy-parent', 'message': msg('Parent')})['task']['id']
        call('owner', 'deliver', {'task_id': parent}); call('builder', 'ack', {'task_id': parent})
        fence = call('builder', 'claim', {'task_id': parent, 'seconds': 900})['task']['lease']['fence']
        helpers = [call('builder', 'request_help', {'task_id': parent, 'fence': fence, 'recipient': 'second', 'title': title,
                   'body': 'Legacy helper body.', 'acceptance': 'A saved helper result.'})['task']['id'] for title in ('Helper one', 'Helper two')]
        one, two = helpers
        call('second', 'ack', {'task_id': one}); f = call('second', 'claim', {'task_id': one, 'seconds': 300})['task']['lease']['fence']
        call('second', 'wait_decision', {'task_id': one, 'fence': f, 'question': 'Which wording?'})
        call('owner', 'resolve_decision', {'task_id': one, 'body': 'Use the approved wording.'})
        call('second', 'ack', {'task_id': one}); f = call('second', 'claim', {'task_id': one, 'seconds': 300})['task']['lease']['fence']
        call('second', 'finish', {'task_id': one, 'fence': f, 'body': 'Helper one done.', 'refs': [ref]})
        call('reviewer', 'review', {'task_id': one, 'accepted': True, 'note': 'Read helper one.'})
        call('second', 'ack', {'task_id': two}); f = call('second', 'claim', {'task_id': two, 'seconds': 300})['task']['lease']['fence']
        call('second', 'fail', {'task_id': two, 'fence': f, 'body': 'First failure.'})
        f = call('second', 'claim', {'task_id': two, 'seconds': 300})['task']['lease']['fence']
        call('second', 'finish', {'task_id': two, 'fence': f, 'body': 'Helper two try.', 'refs': [ref]})
        call('reviewer', 'review', {'task_id': two, 'accepted': False, 'note': 'Second strike.'})
        call('owner', 'reinstate', {'task_id': two, 'body': 'Idle worker, not a failure.'})
        call('second', 'ack', {'task_id': two}); f = call('second', 'claim', {'task_id': two, 'seconds': 300})['task']['lease']['fence']
        call('second', 'finish', {'task_id': two, 'fence': f, 'body': 'Helper two done.', 'refs': [ref]})
        call('reviewer', 'review', {'task_id': two, 'accepted': True, 'note': 'Read helper two.'})
        call('builder', 'renew', {'task_id': parent, 'fence': fence, 'seconds': 900})
        call('builder', 'finish', {'task_id': parent, 'fence': fence, 'body': 'Parent done.', 'refs': [ref]})
        call('reviewer', 'review', {'task_id': parent, 'accepted': True, 'note': 'Read the parent.'})
        late = call('owner', 'intake', {'source_namespace': 'estate', 'external_reference': 'legacy-late', 'message': msg('Late')})['task']['id']
        call('owner', 'deliver', {'task_id': late}); call('owner', 'assign', {'task_id': late, 'recipient': 'second'})
        call('second', 'ack', {'task_id': late}); call('second', 'claim', {'task_id': late, 'seconds': 30})
        clock.now += 60
        call('owner', 'pause', {'authority_id': 'orch'}); call('owner', 'resume', {'authority_id': 'orch'})
        call('builder', 'note', {'authority_id': 'orch', 'body': 'Legacy lounge note.'})
        counted = call('builder', 'create', {'title': 'Count', 'body': 'Count words.', 'recipient': 'second', 'authority_id': 'count',
                       'kind': 'document.count', 'acceptance': 'A count.'})['task']['id']
        call('builder', 'deliver', {'task_id': counted})
    events, _, _ = store._snapshot(people['owner'])
    return normalize(events)


def load_baseline_store():
    """The store module exactly as committed at the baseline, imported under a private name."""
    try:
        shown = subprocess.run(['git', '-C', str(ROOT), 'show', BASELINE_COMMIT + ':common_room/store.py'],
                               capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if shown.returncode != 0: return None
    module = types.ModuleType('common_room._store_baseline_' + BASELINE_COMMIT)
    module.__package__ = 'common_room'; module.__file__ = BASELINE_COMMIT + ':common_room/store.py'
    exec(compile(shown.stdout, module.__file__, 'exec'), module.__dict__)
    return module


class ReplayIdentityTests(unittest.TestCase):
    def test_recorded_pre_change_log_folds_to_the_recorded_state(self):
        events = json.loads(LEGACY_EVENTS_JSON)
        self.assertEqual(hashlib.sha256(canonical(events).encode('utf-8')).hexdigest(), LEGACY_LOG_SHA256)
        self.assertEqual(state_digest(fold, events), LEGACY_STATE_SHA256)
        _, tasks = fold(events)
        self.assertEqual(sorted(t['status'] for t in tasks.values()), ['acknowledged', 'delivered', 'done', 'done', 'done'])
        for task in tasks.values():
            self.assertNotIn('cancel_note', task); self.assertNotIn('cancelled_by', task)

    def test_old_actions_still_write_the_recorded_log(self):
        self.assertEqual(legacy_scenario(Store, case_dir('legacy-now')), json.loads(LEGACY_EVENTS_JSON))

    def test_baseline_and_current_fold_agree_on_old_logs(self):
        baseline = load_baseline_store()
        if baseline is None: self.skipTest('baseline commit ' + BASELINE_COMMIT + ' is not reachable through git here')
        recorded = json.loads(LEGACY_EVENTS_JSON)
        self.assertEqual(legacy_scenario(baseline.Store, case_dir('legacy-then')), recorded)
        # Historical logs written before the release-v1 lease policy carry no policy field.
        historical = copy.deepcopy(recorded)
        for event in historical: event['payload'].pop('lease_expiry_policy', None)
        for events in (recorded, historical):
            self.assertEqual(canonical(baseline.fold(copy.deepcopy(events))), canonical(fold(copy.deepcopy(events))))
        self.assertNotEqual(state_digest(fold, recorded), state_digest(fold, historical))


# Recorded 2026-09-24 by driving legacy_scenario() through the store exactly as committed at
# c15d475 (before cancel existed), with the machine-specific artifact root normalized away.
# The state digest is canonical JSON of that baseline fold(). Do not regenerate with new code.
LEGACY_LOG_SHA256 = '0b959032d0fe6d06787f5643a3daa8207dc4178ba04867ab0560469d2f754886'
LEGACY_STATE_SHA256 = '31c1cc220ac37875aa2efd61883347169bb3ff25491876bbac4435eac6e95854'
LEGACY_EVENTS_JSON = r'''[
{"actor":"owner","kind":"authority_granted","payload":{"authority":{"artifact_root":"LEGACY-ROOT","capabilities":["handoff.record"],"id":"orch","members":["owner","builder","second","reviewer"],"owner":"owner","paused":false,"revoked":false,"root_id":"fixture","scope":"Legacy replay fixture","source_namespaces":["estate"]},"authority_id":"orch"},"seq":1,"task_id":null,"timestamp":1001.0},
{"actor":"owner","kind":"authority_granted","payload":{"authority":{"artifact_root":"LEGACY-ROOT","capabilities":["document.count"],"id":"count","members":["owner","builder","second","reviewer"],"owner":"owner","paused":false,"revoked":false,"root_id":"fixture","scope":"Legacy count fixture"},"authority_id":"count"},"seq":2,"task_id":null,"timestamp":1002.0},
{"actor":"owner","kind":"task_created","payload":{"authority_id":"orch","task":{"acceptance":"A saved result and separate review.","authority_id":"orch","body":"Legacy fixture body.","builder":null,"chain_depth":0,"decision":null,"failed_attempts":0,"fence":0,"id":"t00000000000000000000000000000001","intake":{"external_reference":"legacy-parent","fingerprint":"c4d08ad07337fb3ac74f807a6e6b32ae26546ef88672cf6125b2102597300743","source_namespace":"estate"},"kind":"handoff.record","lease":null,"owner":"owner","parent_id":null,"recipient":"builder","refs":[],"result_body":"","result_refs":[],"reviewer":null,"sender":"owner","status":"draft","title":"Parent"}},"seq":3,"task_id":"t00000000000000000000000000000001","timestamp":1003.0},
{"actor":"owner","kind":"task_deliver","payload":{"authority_id":"orch","changes":{"status":"delivered"}},"seq":4,"task_id":"t00000000000000000000000000000001","timestamp":1004.0},
{"actor":"builder","kind":"task_ack","payload":{"authority_id":"orch","changes":{"status":"acknowledged"}},"seq":5,"task_id":"t00000000000000000000000000000001","timestamp":1005.0},
{"actor":"builder","kind":"task_claim","payload":{"authority_id":"orch","changes":{"builder":"builder","fence":1,"lease":{"fence":1,"holder":"builder","until":1906.0},"status":"claimed"}},"seq":6,"task_id":"t00000000000000000000000000000001","timestamp":1006.0},
{"actor":"builder","kind":"task_created","payload":{"authority_id":"orch","parent_fence":1,"requested_from":"t00000000000000000000000000000001","task":{"acceptance":"A saved helper result.","authority_id":"orch","body":"Legacy helper body.","builder":null,"chain_depth":1,"decision":null,"failed_attempts":0,"fence":0,"id":"t00000000000000000000000000000002","intake":{"external_reference":"t00000000000000000000000000000002","fingerprint":"dc66b27442cec8f7ef022ba50a424d1f624de3c33b80c08748436b0390d032b8","source_namespace":"room-helper"},"kind":"handoff.record","lease":null,"owner":"owner","parent_id":"t00000000000000000000000000000001","recipient":"second","refs":[],"result_body":"","result_refs":[],"reviewer":null,"sender":"builder","status":"delivered","title":"Helper one"}},"seq":7,"task_id":"t00000000000000000000000000000002","timestamp":1007.0},
{"actor":"builder","kind":"task_created","payload":{"authority_id":"orch","parent_fence":1,"requested_from":"t00000000000000000000000000000001","task":{"acceptance":"A saved helper result.","authority_id":"orch","body":"Legacy helper body.","builder":null,"chain_depth":1,"decision":null,"failed_attempts":0,"fence":0,"id":"t00000000000000000000000000000003","intake":{"external_reference":"t00000000000000000000000000000003","fingerprint":"9bb695d0bea5d7c41c1a71438054b0329fbcfe346b1f7300ce78e10bc20d8fff","source_namespace":"room-helper"},"kind":"handoff.record","lease":null,"owner":"owner","parent_id":"t00000000000000000000000000000001","recipient":"second","refs":[],"result_body":"","result_refs":[],"reviewer":null,"sender":"builder","status":"delivered","title":"Helper two"}},"seq":8,"task_id":"t00000000000000000000000000000003","timestamp":1008.0},
{"actor":"second","kind":"task_ack","payload":{"authority_id":"orch","changes":{"status":"acknowledged"}},"seq":9,"task_id":"t00000000000000000000000000000002","timestamp":1009.0},
{"actor":"second","kind":"task_claim","payload":{"authority_id":"orch","changes":{"builder":"second","fence":1,"lease":{"fence":1,"holder":"second","until":1310.0},"status":"claimed"}},"seq":10,"task_id":"t00000000000000000000000000000002","timestamp":1010.0},
{"actor":"second","kind":"task_wait_decision","payload":{"authority_id":"orch","changes":{"decision":"Which wording?","fence":2,"lease":null,"status":"waiting_decision"}},"seq":11,"task_id":"t00000000000000000000000000000002","timestamp":1011.0},
{"actor":"owner","kind":"task_resolve_decision","payload":{"authority_id":"orch","changes":{"builder":null,"decision":null,"decision_response":"Use the approved wording.","fence":3,"lease":null,"reviewer":null,"status":"delivered"}},"seq":12,"task_id":"t00000000000000000000000000000002","timestamp":1012.0},
{"actor":"second","kind":"task_ack","payload":{"authority_id":"orch","changes":{"status":"acknowledged"}},"seq":13,"task_id":"t00000000000000000000000000000002","timestamp":1013.0},
{"actor":"second","kind":"task_claim","payload":{"authority_id":"orch","changes":{"builder":"second","fence":4,"lease":{"fence":4,"holder":"second","until":1314.0},"status":"claimed"}},"seq":14,"task_id":"t00000000000000000000000000000002","timestamp":1014.0},
{"actor":"second","kind":"task_finish","payload":{"authority_id":"orch","changes":{"fence":5,"lease":null,"result_body":"Helper one done.","result_refs":[{"path":"result.txt","sha256":"1414776b4ed949be38be604a17ad3d57d1e13aebb0dacfe1195e7b7d128b6fe8"}],"status":"ready_for_review"}},"seq":15,"task_id":"t00000000000000000000000000000002","timestamp":1015.0},
{"actor":"reviewer","kind":"task_review","payload":{"authority_id":"orch","changes":{"decision":null,"review_note":"Read helper one.","reviewer":"reviewer","status":"done"}},"seq":16,"task_id":"t00000000000000000000000000000002","timestamp":1016.0},
{"actor":"second","kind":"task_ack","payload":{"authority_id":"orch","changes":{"status":"acknowledged"}},"seq":17,"task_id":"t00000000000000000000000000000003","timestamp":1017.0},
{"actor":"second","kind":"task_claim","payload":{"authority_id":"orch","changes":{"builder":"second","fence":1,"lease":{"fence":1,"holder":"second","until":1318.0},"status":"claimed"}},"seq":18,"task_id":"t00000000000000000000000000000003","timestamp":1018.0},
{"actor":"second","kind":"task_fail","payload":{"authority_id":"orch","changes":{"decision":null,"failed_actor":"second","failed_attempts":1,"failed_fence":1,"failure_note":"First failure.","fence":2,"lease":null,"status":"acknowledged"}},"seq":19,"task_id":"t00000000000000000000000000000003","timestamp":1019.0},
{"actor":"second","kind":"task_claim","payload":{"authority_id":"orch","changes":{"builder":"second","fence":3,"lease":{"fence":3,"holder":"second","until":1320.0},"status":"claimed"}},"seq":20,"task_id":"t00000000000000000000000000000003","timestamp":1020.0},
{"actor":"second","kind":"task_finish","payload":{"authority_id":"orch","changes":{"fence":4,"lease":null,"result_body":"Helper two try.","result_refs":[{"path":"result.txt","sha256":"1414776b4ed949be38be604a17ad3d57d1e13aebb0dacfe1195e7b7d128b6fe8"}],"status":"ready_for_review"}},"seq":21,"task_id":"t00000000000000000000000000000003","timestamp":1021.0},
{"actor":"reviewer","kind":"task_review","payload":{"authority_id":"orch","changes":{"decision":"Review requires an owner decision: Second strike.","failed_actor":"second","failed_attempts":2,"failed_fence":4,"failure_note":"Review rejected: Second strike.","review_note":"Second strike.","reviewer":"reviewer","status":"waiting_decision"}},"seq":22,"task_id":"t00000000000000000000000000000003","timestamp":1022.0},
{"actor":"owner","kind":"task_reinstate","payload":{"authority_id":"orch","changes":{"builder":null,"decision":null,"decision_response":"Owner reinstated after 2 recorded failure(s): Idle worker, not a failure.","failed_attempts":0,"fence":5,"lease":null,"reviewer":null,"status":"delivered"}},"seq":23,"task_id":"t00000000000000000000000000000003","timestamp":1023.0},
{"actor":"second","kind":"task_ack","payload":{"authority_id":"orch","changes":{"status":"acknowledged"}},"seq":24,"task_id":"t00000000000000000000000000000003","timestamp":1024.0},
{"actor":"second","kind":"task_claim","payload":{"authority_id":"orch","changes":{"builder":"second","fence":6,"lease":{"fence":6,"holder":"second","until":1325.0},"status":"claimed"}},"seq":25,"task_id":"t00000000000000000000000000000003","timestamp":1025.0},
{"actor":"second","kind":"task_finish","payload":{"authority_id":"orch","changes":{"fence":7,"lease":null,"result_body":"Helper two done.","result_refs":[{"path":"result.txt","sha256":"1414776b4ed949be38be604a17ad3d57d1e13aebb0dacfe1195e7b7d128b6fe8"}],"status":"ready_for_review"}},"seq":26,"task_id":"t00000000000000000000000000000003","timestamp":1026.0},
{"actor":"reviewer","kind":"task_review","payload":{"authority_id":"orch","changes":{"decision":null,"review_note":"Read helper two.","reviewer":"reviewer","status":"done"}},"seq":27,"task_id":"t00000000000000000000000000000003","timestamp":1027.0},
{"actor":"builder","kind":"task_renew","payload":{"authority_id":"orch","changes":{"lease":{"fence":1,"holder":"builder","until":1928.0}}},"seq":28,"task_id":"t00000000000000000000000000000001","timestamp":1028.0},
{"actor":"builder","kind":"task_finish","payload":{"authority_id":"orch","changes":{"fence":2,"lease":null,"result_body":"Parent done.","result_refs":[{"path":"result.txt","sha256":"1414776b4ed949be38be604a17ad3d57d1e13aebb0dacfe1195e7b7d128b6fe8"}],"status":"ready_for_review"}},"seq":29,"task_id":"t00000000000000000000000000000001","timestamp":1029.0},
{"actor":"reviewer","kind":"task_review","payload":{"authority_id":"orch","changes":{"decision":null,"review_note":"Read the parent.","reviewer":"reviewer","status":"done"}},"seq":30,"task_id":"t00000000000000000000000000000001","timestamp":1030.0},
{"actor":"owner","kind":"task_created","payload":{"authority_id":"orch","task":{"acceptance":"A saved result and separate review.","authority_id":"orch","body":"Legacy fixture body.","builder":null,"chain_depth":0,"decision":null,"failed_attempts":0,"fence":0,"id":"t00000000000000000000000000000004","intake":{"external_reference":"legacy-late","fingerprint":"0a687482da07f7eb04e9e67c7b2629f0314c8f546a912ea8827830af93bbb3a2","source_namespace":"estate"},"kind":"handoff.record","lease":null,"owner":"owner","parent_id":null,"recipient":"builder","refs":[],"result_body":"","result_refs":[],"reviewer":null,"sender":"owner","status":"draft","title":"Late"}},"seq":31,"task_id":"t00000000000000000000000000000004","timestamp":1031.0},
{"actor":"owner","kind":"task_deliver","payload":{"authority_id":"orch","changes":{"status":"delivered"}},"seq":32,"task_id":"t00000000000000000000000000000004","timestamp":1032.0},
{"actor":"owner","kind":"task_assign","payload":{"authority_id":"orch","changes":{"builder":null,"fence":1,"lease":null,"recipient":"second","status":"delivered"}},"seq":33,"task_id":"t00000000000000000000000000000004","timestamp":1033.0},
{"actor":"second","kind":"task_ack","payload":{"authority_id":"orch","changes":{"status":"acknowledged"}},"seq":34,"task_id":"t00000000000000000000000000000004","timestamp":1034.0},
{"actor":"second","kind":"task_claim","payload":{"authority_id":"orch","changes":{"builder":"second","fence":2,"lease":{"fence":2,"holder":"second","until":1065.0},"status":"claimed"}},"seq":35,"task_id":"t00000000000000000000000000000004","timestamp":1035.0},
{"actor":"owner","kind":"authority_paused","payload":{"authority_id":"orch","lease_expiry_policy":"release-v1"},"seq":36,"task_id":null,"timestamp":1096.0},
{"actor":"owner","kind":"authority_resumed","payload":{"authority_id":"orch","lease_expiry_policy":"release-v1"},"seq":37,"task_id":null,"timestamp":1097.0},
{"actor":"builder","kind":"note","payload":{"authority_id":"orch","body":"Legacy lounge note."},"seq":38,"task_id":null,"timestamp":1098.0},
{"actor":"builder","kind":"task_created","payload":{"authority_id":"count","task":{"acceptance":"A count.","authority_id":"count","body":"Count words.","builder":null,"chain_depth":0,"decision":null,"failed_attempts":0,"fence":0,"id":"t00000000000000000000000000000005","kind":"document.count","lease":null,"owner":"owner","parent_id":null,"recipient":"second","refs":[],"result_body":"","result_refs":[],"reviewer":null,"sender":"builder","status":"draft","title":"Count"}},"seq":39,"task_id":"t00000000000000000000000000000005","timestamp":1099.0},
{"actor":"builder","kind":"task_deliver","payload":{"authority_id":"count","changes":{"status":"delivered"}},"seq":40,"task_id":"t00000000000000000000000000000005","timestamp":1100.0}
]'''

if __name__ == '__main__': unittest.main()
