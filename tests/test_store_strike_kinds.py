"""Strike kinds and strike refunds (cr-upgrade-2026-10-01, task core-strikes).

What the store now does, and what these tests hold it to:

  * `review` takes an optional `kind`: `substance` (the default when absent) or `paperwork`.
    A rejected paperwork review sends the task back exactly like today's rejection but adds no
    strike; `paperwork_returns` counts them per task, and the third one (and each later one)
    counts as a strike. Accepted reviews ignore the kind.
  * `resolve_decision` takes an optional `refund: true`, with the existing `body` as the written
    reason. It removes one strike (never below zero) and records the refund in `refunds`. Whoever
    may resolve today may refund (owner, or the sender of a helper); a task stopped at two strikes
    stays the owner's alone.
  * a client that never sends `kind` or `refund` gets exactly yesterday's behavior, and a log
    written by yesterday's code folds to the same task states (fold() is untouched).
  * the board history rows carry the review kind and any refund, only when the event had them.
"""
import copy, itertools, json, pathlib, subprocess, sys, types, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # so evidence.py can run this file as a script in a clean copy
from common_room.protocol import RoomError, canonical
from common_room.store import Store, fold
from tests.helpers import ROOT, case_dir
from tests.test_cancel_and_delegated_decisions import (
    DelegationFixture, LEGACY_EVENTS_JSON, LEGACY_STATE_SHA256, legacy_scenario, normalize, state_digest)

BASELINE_COMMIT = 'c166619'  # core-setup baseline: the live tree as running on 2026-10-01


class StrikeFixture(DelegationFixture):
    """A helper task whose worker has submitted work, ready for the reviewer."""
    def setUp(self):
        super().setUp()
        self.root_refs = itertools.count(1)  # distinct intake references for each root task

    def submitted_helper(self, parent=None, lease=None, recipient='second'):
        if parent is None: parent, lease = self.claimed_root('root-%d' % next(self.root_refs))
        child = self.helper(parent, lease, recipient=recipient)
        self.resubmit(child, recipient, acknowledge=True)
        return parent, lease, child

    def resubmit(self, child, worker='second', acknowledge=False):
        actor = self.people[worker]
        if acknowledge: self.call(actor, 'ack', {'task_id': child})
        held = self.claim(child, actor)
        self.call(actor, 'finish', {'task_id': child, 'fence': held['fence'], 'body': 'Draft', 'refs': [self.unique_ref()]})

    def reject(self, task, kind=None, note='Returned with a note.'):
        payload = {'task_id': task, 'accepted': False, 'note': note}
        if kind is not None: payload['kind'] = kind
        return self.call(self.reviewer, 'review', payload)['task']

    def refund(self, actor, task, body='A brief error, not the work.'):
        return self.call(actor, 'resolve_decision', {'task_id': task, 'body': body, 'refund': True})['task']

    def back_to_review(self, child):
        """The helper's sender answers the returned review, the worker submits again."""
        self.resolve(self.builder, child, 'Fixed the paperwork.')
        self.call(self.second, 'ack', {'task_id': child})
        self.resubmit(child)


class PaperworkReviewTests(StrikeFixture):
    def test_a_paperwork_return_costs_no_strike(self):
        _, _, child = self.submitted_helper()
        task = self.reject(child, 'paperwork', 'The report names the wrong commit.')
        self.assertEqual((task['status'], task['failed_attempts']), ('waiting_decision', 0))
        self.assertEqual(task['paperwork_returns'], 1)
        self.assertEqual(task['review_kind'], 'paperwork')
        self.assertEqual(task['decision'], 'Review requires an owner decision: The report names the wrong commit.')
        self.assertEqual((task['reviewer'], task['review_note']), ('reviewer', 'The report names the wrong commit.'))
        self.assertEqual(task['result_body'], 'Draft')  # the submitted result is kept, as for any rejection
        # It goes back to work like today's rejection: the delegator answers, the worker resubmits.
        self.assertEqual(self.resolve(self.builder, child)['status'], 'delivered')

    def test_the_third_paperwork_return_is_a_strike_and_so_is_each_later_one(self):
        _, _, child = self.submitted_helper()
        seen = []
        for number in range(1, 5):
            task = self.reject(child, 'paperwork', 'Paperwork round %d.' % number)
            seen.append((task['paperwork_returns'], task['failed_attempts']))
            if number < 4: self.back_to_review(child)
        self.assertEqual(seen, [(1, 0), (2, 0), (3, 1), (4, 2)])
        task = self.store.task(self.owner, child)
        self.assertEqual((task['status'], task['failure_note'], task['failed_actor']), ('waiting_decision', 'Review rejected: Paperwork round 4.', 'second'))
        self.error('budget', lambda: self.resolve(self.builder, child))  # two strikes stops it, as before

    def test_a_substance_return_costs_a_strike_whether_or_not_the_kind_is_named(self):
        _, _, one = self.submitted_helper()
        _, _, two = self.submitted_helper()
        named, unnamed = self.reject(one, 'substance'), self.reject(two)
        for task in (named, unnamed):
            self.assertEqual((task['status'], task['failed_attempts']), ('waiting_decision', 1))
            self.assertNotIn('paperwork_returns', task)
            self.assertEqual(task['failure_note'], 'Review rejected: Returned with a note.')
        self.assertEqual(named['review_kind'], 'substance')
        self.assertNotIn('review_kind', unnamed)  # an old client leaves the task exactly as it did

    def test_paperwork_returns_and_strikes_add_up_independently(self):
        _, _, child = self.submitted_helper()
        self.reject(child, 'paperwork'); self.back_to_review(child)
        task = self.reject(child, 'substance')
        self.assertEqual((task['failed_attempts'], task['paperwork_returns']), (1, 1))
        self.back_to_review(child)
        task = self.reject(child, 'paperwork')  # the second paperwork return: still free
        self.assertEqual((task['failed_attempts'], task['paperwork_returns']), (1, 2))

    def test_an_accepted_review_ignores_the_kind(self):
        _, _, child = self.submitted_helper()
        done = self.call(self.reviewer, 'review', {'task_id': child, 'accepted': True, 'note': 'Read it.', 'kind': 'paperwork'})['task']
        self.assertEqual((done['status'], done['failed_attempts']), ('done', 0))
        for key in ('paperwork_returns', 'review_kind'): self.assertNotIn(key, done)

    def test_the_kind_is_checked(self):
        _, _, child = self.submitted_helper()
        for bad in ('Paperwork', 'cosmetic', '', None, True, 3, ['paperwork']):
            self.error('invalid', lambda b=bad: self.call(self.reviewer, 'review', {'task_id': child, 'accepted': False, 'note': 'x', 'kind': b}))
        self.assertEqual(self.store.task(self.owner, child)['status'], 'ready_for_review')
        self.assertEqual(self.store.task(self.owner, child)['failed_attempts'], 0)

    def test_only_the_two_optional_fields_are_new(self):
        _, _, child = self.submitted_helper()
        self.error('invalid', lambda: self.call(self.reviewer, 'review', {'task_id': child, 'accepted': False, 'note': 'x', 'kind': 'paperwork', 'extra': 1}))
        self.error('invalid', lambda: self.call(self.reviewer, 'review', {'task_id': child, 'accepted': False, 'note': 'x', 'refund': True}))
        self.error('invalid', lambda: self.call(self.reviewer, 'review', {'task_id': child, 'accepted': False, 'kind': 'paperwork'}))  # note still required
        self.error('invalid', lambda: self.call(self.builder, 'ack', {'task_id': child, 'kind': 'paperwork'}))
        self.error('invalid', lambda: self.call(self.owner, 'cancel', {'task_id': child, 'body': 'x', 'refund': True}))

    def test_a_retry_with_the_same_key_replays_and_a_changed_kind_is_refused(self):
        _, _, child = self.submitted_helper()
        payload = {'task_id': child, 'accepted': False, 'note': 'Paperwork.', 'kind': 'paperwork'}
        first = self.call(self.reviewer, 'review', payload, key='review-retry')
        again = self.call(self.reviewer, 'review', payload, key='review-retry')
        self.assertEqual(first, again)
        self.assertEqual(self.store.task(self.owner, child)['paperwork_returns'], 1)
        self.error('idempotency', lambda: self.call(self.reviewer, 'review', dict(payload, kind='substance'), key='review-retry'))


class RefundTests(StrikeFixture):
    def returned(self, kind='substance'):
        parent, lease, child = self.submitted_helper()
        self.reject(child, kind)
        return parent, lease, child

    def test_the_owner_refunds_one_strike(self):
        _, _, child = self.returned()
        self.assertEqual(self.store.task(self.owner, child)['failed_attempts'], 1)
        task = self.refund(self.owner, child, 'The brief was wrong, not the work.')
        self.assertEqual((task['status'], task['failed_attempts']), ('delivered', 0))
        self.assertEqual(task['decision_response'], 'The brief was wrong, not the work.')
        self.assertEqual(task['refunds'], [{'by': 'owner', 'reason': 'The brief was wrong, not the work.', 'strikes_before': 1, 'strikes_after': 0}])
        self.assertEqual(task['failure_note'], 'Review rejected: Returned with a note.')  # what happened is not rewritten
        event = self.store.poll(self.owner)['events'][-1]
        self.assertEqual((event['kind'], event['actor']), ('task_resolve_decision', 'owner'))

    def test_the_helper_sender_refunds_its_own_helper(self):
        _, _, child = self.returned()
        task = self.refund(self.builder, child)
        self.assertEqual((task['status'], task['failed_attempts'], task['refunds'][0]['by']), ('delivered', 0, 'builder'))

    def test_a_refund_never_goes_below_zero(self):
        _, _, child = self.returned('paperwork')  # no strike on it
        task = self.refund(self.owner, child)
        self.assertEqual((task['status'], task['failed_attempts']), ('delivered', 0))
        self.assertEqual([(r['strikes_before'], r['strikes_after']) for r in task['refunds']], [(0, 0)])

    def test_each_refund_removes_one_strike_and_all_are_kept(self):
        _, _, child = self.returned()
        self.refund(self.builder, child, 'First refund.')
        self.call(self.second, 'ack', {'task_id': child}); self.resubmit(child)
        self.reject(child); self.back_to_review(child); self.reject(child)
        self.assertEqual(self.store.task(self.owner, child)['failed_attempts'], 2)  # stopped
        task = self.refund(self.owner, child, 'Second refund.')
        self.assertEqual(task['failed_attempts'], 1)
        self.assertEqual([(r['reason'], r['strikes_before'], r['strikes_after']) for r in task['refunds']],
                         [('First refund.', 1, 0), ('Second refund.', 2, 1)])

    def test_anyone_else_is_refused(self):
        parent, lease, child = self.returned()
        for actor in (self.third, self.second, self.reviewer, self.visitor):
            self.error('forbidden', lambda a=actor: self.refund(a, child))
        self.assertEqual(self.store.task(self.owner, child)['failed_attempts'], 1)
        # The sender's power stops at its own helper: it cannot refund the parent.
        self.call(self.builder, 'wait_decision', {'task_id': parent, 'fence': lease['fence'], 'question': 'Parent question'})
        self.error('forbidden', lambda: self.refund(self.builder, parent))
        self.assertEqual(self.store.task(self.owner, child)['status'], 'waiting_decision')

    def test_a_stopped_helper_is_refunded_by_the_owner_only(self):
        parent, lease, child = self.exhausted_helper()
        self.error('budget', lambda: self.refund(self.builder, child))
        for actor in (self.third, self.second, self.reviewer, self.visitor):
            self.error('forbidden', lambda a=actor: self.refund(a, child))
        self.assertEqual(self.store.task(self.owner, child)['failed_attempts'], 2)
        task = self.refund(self.owner, child, 'Both strikes were the brief.')
        self.assertEqual((task['status'], task['failed_attempts']), ('delivered', 1))
        self.assertEqual(task['refunds'][0]['strikes_before'], 2)

    def test_resolving_at_two_strikes_without_a_refund_is_still_refused(self):
        parent, lease, child = self.exhausted_helper()
        self.error('budget', lambda: self.resolve(self.builder, child))
        self.error('budget', lambda: self.resolve(self.owner, child))
        self.error('budget', lambda: self.call(self.owner, 'resolve_decision', {'task_id': child, 'body': 'x', 'refund': False}))
        self.assertEqual(self.store.task(self.owner, child)['failed_attempts'], 2)

    def test_a_refund_still_needs_a_waiting_decision(self):
        parent, lease = self.claimed_root(); child = self.helper(parent, lease)
        self.error('state', lambda: self.refund(self.builder, child))
        self.error('state', lambda: self.refund(self.owner, child))

    def test_the_refund_flag_is_a_boolean_and_false_is_an_ordinary_answer(self):
        _, _, child = self.returned()
        for bad in ('yes', 1, 0, None, 'true'):
            self.error('invalid', lambda b=bad: self.call(self.owner, 'resolve_decision', {'task_id': child, 'body': 'x', 'refund': b}))
        self.assertEqual(self.store.task(self.owner, child)['status'], 'waiting_decision')
        task = self.call(self.owner, 'resolve_decision', {'task_id': child, 'body': 'Just an answer.', 'refund': False})['task']
        self.assertEqual((task['status'], task['failed_attempts']), ('delivered', 1))
        self.assertNotIn('refunds', task)

    def test_a_refund_needs_its_written_reason(self):
        _, _, child = self.returned()
        self.error('invalid', lambda: self.call(self.owner, 'resolve_decision', {'task_id': child, 'body': '   ', 'refund': True}))
        self.error('invalid', lambda: self.call(self.owner, 'resolve_decision', {'task_id': child, 'refund': True}))

    def test_reinstate_is_untouched_and_keeps_the_paperwork_count(self):
        parent, lease, child = self.exhausted_helper()
        back = self.call(self.owner, 'reinstate', {'task_id': child, 'body': 'Idle worker.'})['task']
        self.assertEqual((back['status'], back['failed_attempts']), ('delivered', 0))
        _, _, other = self.submitted_helper()
        self.reject(other, 'paperwork')
        self.assertEqual(self.store.task(self.owner, other)['paperwork_returns'], 1)


class BoardHistoryTests(StrikeFixture):
    def rows(self, task):
        from common_room.board import history_marks  # imported here so a base copy without it fails these tests, not the whole file
        events = self.store._snapshot(self.owner)[0]
        return [(e['kind'], history_marks(e)) for e in events if e['task_id'] == task]

    def test_history_rows_carry_the_review_kind_and_the_refund_and_only_then(self):
        _, _, child = self.submitted_helper()
        self.reject(child, 'paperwork', 'Wrong commit in the report.')
        self.refund(self.owner, child, 'Brief error.')
        rows = self.rows(child)
        review = [m for k, m in rows if k == 'task_review'][0]
        refund = [m for k, m in rows if k == 'task_resolve_decision'][0]
        self.assertEqual(review, {'review_kind': 'paperwork', 'paperwork_returns': 1})
        self.assertEqual(refund, {'refund': {'by': 'owner', 'reason': 'Brief error.', 'strikes_before': 0, 'strikes_after': 0}})
        for kind, marks in rows:
            if kind not in {'task_review', 'task_resolve_decision'}: self.assertEqual(marks, {})

    def test_an_old_style_review_and_resolve_get_no_marks(self):
        _, _, child = self.submitted_helper()
        self.reject(child); self.resolve(self.builder, child)
        for kind, marks in self.rows(child): self.assertEqual(marks, {})


def drive_old_style_commands(store_cls, home):
    """Only actions and fields that exist at the baseline: a rejection to a stop, an answer at one
    strike, a refused answer at two, fail and a reinstate. Returns the normalized event log."""
    import hashlib, uuid as uuid_module
    from unittest import mock
    from common_room.protocol import Actor
    from tests.test_store import Clock
    art = home / 'artifacts'; art.mkdir()
    people = {a.id: a for a in [Actor('owner', 'owner'), Actor('builder', 'worker'), Actor('second', 'worker'), Actor('reviewer', 'reviewer')]}
    clock = Clock(); store = store_cls(home / 'old.sqlite', people, {'fixture': art}, clock=clock); n = [0]
    content = b'Old-style fictional result.\n'; (art / 'result.txt').write_bytes(content)
    ref = {'path': 'result.txt', 'sha256': hashlib.sha256(content).hexdigest()}
    def call(actor, action, payload):
        n[0] += 1; clock.now += 1
        return store.execute(people[actor], action, payload, 'old-' + str(n[0]))
    def refused(actor, action, payload):
        try: call(actor, action, payload)
        except RoomError as exc: return exc.code
        raise AssertionError('expected a refusal')
    counter = iter(range(1, 1000))
    with mock.patch.object(uuid_module, 'uuid4', side_effect=lambda: uuid_module.UUID(int=next(counter))):
        call('owner', 'grant_handoff', {'id': 'orch', 'members': ['owner', 'builder', 'second', 'reviewer'], 'capabilities': ['handoff.record'],
             'root_id': 'fixture', 'scope': 'Old-style fixture', 'source_namespaces': ['estate']})
        msg = {'title': 'Parent', 'body': 'Old fixture body.', 'recipient': 'builder', 'authority_id': 'orch',
               'kind': 'handoff.record', 'acceptance': 'A saved result and separate review.'}
        parent = call('owner', 'intake', {'source_namespace': 'estate', 'external_reference': 'old-parent', 'message': msg})['task']['id']
        call('owner', 'deliver', {'task_id': parent}); call('builder', 'ack', {'task_id': parent})
        fence = call('builder', 'claim', {'task_id': parent, 'seconds': 1800})['task']['lease']['fence']
        child = call('builder', 'request_help', {'task_id': parent, 'fence': fence, 'recipient': 'second', 'title': 'Helper',
                     'body': 'Old helper body.', 'acceptance': 'A saved helper result.'})['task']['id']
        call('second', 'ack', {'task_id': child})
        f = call('second', 'claim', {'task_id': child, 'seconds': 300})['task']['lease']['fence']
        call('second', 'finish', {'task_id': child, 'fence': f, 'body': 'First try.', 'refs': [ref]})
        call('reviewer', 'review', {'task_id': child, 'accepted': False, 'note': 'First rejection.'})
        call('builder', 'resolve_decision', {'task_id': child, 'body': 'Fix it.'})           # sender answers at one strike
        call('second', 'ack', {'task_id': child})
        f = call('second', 'claim', {'task_id': child, 'seconds': 300})['task']['lease']['fence']
        call('second', 'finish', {'task_id': child, 'fence': f, 'body': 'Second try.', 'refs': [ref]})
        call('reviewer', 'review', {'task_id': child, 'accepted': False, 'note': 'Second rejection.'})
        assert refused('builder', 'resolve_decision', {'task_id': child, 'body': 'Again.'}) == 'budget'
        assert refused('owner', 'resolve_decision', {'task_id': child, 'body': 'Again.'}) == 'budget'
        call('owner', 'reinstate', {'task_id': child, 'body': 'Reset by the owner.'})
        call('second', 'ack', {'task_id': child})
        f = call('second', 'claim', {'task_id': child, 'seconds': 300})['task']['lease']['fence']
        call('second', 'fail', {'task_id': child, 'fence': f, 'body': 'Could not finish.'})
        f = call('second', 'claim', {'task_id': child, 'seconds': 300})['task']['lease']['fence']
        call('second', 'finish', {'task_id': child, 'fence': f, 'body': 'Third try.', 'refs': [ref]})
        call('reviewer', 'review', {'task_id': child, 'accepted': True, 'note': 'Read it.'})
    events, _, _ = store._snapshot(people['owner'])
    return normalize(events)


def load_store_at(commit):
    """The store module exactly as committed at `commit`, imported under a private name, or None."""
    try:
        shown = subprocess.run(['git', '-C', str(ROOT), 'show', commit + ':common_room/store.py'], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if shown.returncode != 0: return None
    module = types.ModuleType('common_room._store_at_' + commit)
    module.__package__ = 'common_room'; module.__file__ = commit + ':common_room/store.py'
    exec(compile(shown.stdout, module.__file__, 'exec'), module.__dict__)
    return module


class ReplayIdentityTests(unittest.TestCase):
    """GUARDS, green on the baseline by design: the rules above must not change what old logs mean."""
    def test_a_recorded_log_folds_to_the_recorded_state(self):
        events = json.loads(LEGACY_EVENTS_JSON)
        self.assertEqual(state_digest(fold, events), LEGACY_STATE_SHA256)

    def test_old_style_commands_write_the_same_events_as_the_baseline(self):
        baseline = load_store_at(BASELINE_COMMIT)
        if baseline is None: self.skipTest('baseline commit ' + BASELINE_COMMIT + ' is not reachable through git here')
        then = drive_old_style_commands(baseline.Store, case_dir('strikes-then'))
        now = drive_old_style_commands(Store, case_dir('strikes-now'))
        self.assertEqual(now, then)
        self.assertGreater(len(now), 20)

    def test_a_log_written_by_the_baseline_folds_the_same_under_both(self):
        baseline = load_store_at(BASELINE_COMMIT)
        if baseline is None: self.skipTest('baseline commit ' + BASELINE_COMMIT + ' is not reachable through git here')
        events = drive_old_style_commands(baseline.Store, case_dir('strikes-fold'))
        self.assertEqual(canonical(baseline.fold(copy.deepcopy(events))), canonical(fold(copy.deepcopy(events))))
        _, tasks = fold(copy.deepcopy(events))
        for task in tasks.values():
            for key in ('paperwork_returns', 'review_kind', 'refunds'): self.assertNotIn(key, task)

    def test_the_recorded_scenario_still_writes_the_recorded_log(self):
        self.assertEqual(legacy_scenario(Store, case_dir('strikes-legacy')), json.loads(LEGACY_EVENTS_JSON))


if __name__ == '__main__':
    unittest.main()
