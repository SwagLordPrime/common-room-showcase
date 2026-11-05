"""A4 tier tag (task t9d61510b5, 2026-10-24; POST_SWITCH_PLAN_2026-10-22: "Tier tag on every task (Haiku, Sonnet or
Opus, plus effort), so bulk work stops landing on Opus").

A task may carry a tier (haiku, sonnet, opus) and an effort (low, medium, high, xhigh, max), set from PLAN.json and
from request_help (crew delegate), at creation only. Seats may carry a tier label from crew/seat-tiers.json (a config
file the switch sheet writes; never in git). The board, crew task views and relay-state show the tag; a seat's join
card names its own label; a task whose tier differs from its seat's label shows the difference and still runs. Nothing
is enforced. Untagged plans, helpers and unlabelled seats behave exactly as before. Fictional rooms in .checks, the
scratch room's own keeper file, no keeper vault.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # lets `python tests/test_a4_tier_tag.py` run from a tree root

import inspect, json, subprocess, types, unittest

from common_room import activation, join_cards
from common_room.board import state_body
from common_room.protocol import RoomError
from tests.helpers import case_dir
from tests.test_crew_plans import ALL_SEATS, CONFIG, PlanCLIBase, make_plan

TREE = Path(__file__).resolve().parents[1]
LABELS = {'plan-worker-a': 'sonnet', 'plan-reviewer': 'opus'}   # plan-worker-b stays unlabelled


def with_labels(labels):
    """Looked up, not assumed: on 356bbac start_room takes no seat tiers, and these tests must fail there on behaviour."""
    takes = 'seat_tiers' in inspect.signature(activation.start_room).parameters
    return {'seat_tiers': labels} if takes and labels is not None else {}


class Base(PlanCLIBase):
    labels = LABELS

    def start_room(self, root=None, selected=None, enrollment=None):
        room = activation.start_room(root or case_dir('a4-tier'), enrollment=enrollment or CONFIG,
                                     selected=selected or ALL_SEATS, **with_labels(self.labels))
        self.addCleanup(room.close)
        return room

    def approve(self, room, plan):
        path = self.write_plan(plan)
        code, out, err = self.run_cli(room, 'keeper', 'plan-approve', str(path), stdin=self.tty_answer('yes\n'))
        return code, out, err

    def tasks(self, room, actor='keeper'):
        return {t['title']: t for t in state_body(room.server, room.store.people[actor])['tasks']}

    def tagged_plan(self, **tags):
        return make_plan(tasks=[
            dict({'key': 't1', 'to': 'plan-worker-a', 'title': 'Task one', 'body': 'Do thing one.',
                  'acceptance': 'Thing one looks right.'}, **tags),
            {'key': 't2', 'to': 'plan-worker-b', 'title': 'Task two', 'body': 'Do thing two.',
             'acceptance': 'Thing two looks right.'}])


class UntaggedTests(Base):
    labels = None

    def test_an_untagged_plan_and_its_task_views_are_unchanged(self):
        room = self.start_room()
        code, out, err = self.approve(room, make_plan())
        self.assertEqual(code, 0, err)
        for task in self.tasks(room).values():
            for key in ('tier', 'effort', 'tier_mismatch'):
                self.assertNotIn(key, task)
        code, out, err = self.run_cli(room, 'plan-worker-a', 'tasks', json_mode=False)
        self.assertEqual(code, 0, err)
        self.assertNotIn('tier=', out)
        self.assertNotIn('tier', json.loads(self.run_cli(room, 'plan-worker-a', 'tasks')[1])['tasks'][0])
        tid = self.tasks(room)['Task one']['id']
        out = self.run_cli(room, 'plan-worker-a', 'show', tid, json_mode=False)[1]
        self.assertNotIn('tier=', out)

    def test_an_untagged_helper_is_unchanged_and_unlabelled_seats_show_no_label(self):
        room = self.start_room()
        tid = self.intake(room)
        self.assertEqual(self.run_cli(room, 'plan-worker-a', 'ack', tid)[0], 0)
        self.assertEqual(self.run_cli(room, 'plan-worker-a', 'claim', tid, '--seconds', '300')[0], 0)
        code, out, err = self.run_cli(room, 'plan-worker-a', 'delegate', tid, '--to', 'plan-worker-b', '--title', 'Help',
                                      '--body', 'Help me.', '--acceptance', 'Helped.')
        self.assertEqual(code, 0, err)
        child = self.tasks(room)['Help']
        self.assertNotIn('tier', child)
        self.assertNotIn('effort', child)
        for person in state_body(room.server, room.store.people['keeper'])['participants']:
            self.assertNotIn('tier', person)


class TaggedTests(Base):
    def test_tier_and_effort_round_trip_through_plan_approve_state_and_crew_views(self):
        room = self.start_room()
        code, out, err = self.approve(room, self.tagged_plan(tier='sonnet', effort='high'))
        self.assertEqual(code, 0, err)
        task = self.tasks(room)['Task one']
        self.assertEqual((task['tier'], task['effort']), ('sonnet', 'high'))
        self.assertNotIn('tier_mismatch', task)          # plan-worker-a is labelled sonnet
        self.assertNotIn('tier', self.tasks(room)['Task two'])
        line = self.run_cli(room, 'plan-worker-a', 'tasks', json_mode=False)[1]
        self.assertIn('tier=sonnet/high', line)
        show = self.run_cli(room, 'plan-worker-a', 'show', task['id'], json_mode=False)[1]
        self.assertIn('tier=sonnet/high', show)
        # re-approving the same plan is the same brief: nothing new, no conflict
        code, out, err = self.approve(room, self.tagged_plan(tier='sonnet', effort='high'))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.tasks(room)), 2)

    def test_a_changed_tag_on_an_approved_task_is_a_conflict(self):
        room = self.start_room()
        self.assertEqual(self.approve(room, self.tagged_plan(tier='sonnet'))[0], 0)
        code, out, err = self.approve(room, self.tagged_plan(tier='opus'))
        self.assertNotEqual(code, 0)
        self.assertEqual(len(self.tasks(room)), 2)

    def test_tier_and_effort_round_trip_through_request_help(self):
        room = self.start_room()
        tid = self.intake(room)
        self.assertEqual(self.run_cli(room, 'plan-worker-a', 'ack', tid)[0], 0)
        self.assertEqual(self.run_cli(room, 'plan-worker-a', 'claim', tid, '--seconds', '300')[0], 0)
        code, out, err = self.run_cli(room, 'plan-worker-a', 'delegate', tid, '--to', 'plan-worker-b', '--title', 'Help',
                                      '--body', 'Help me.', '--acceptance', 'Helped.', '--tier', 'haiku', '--effort', 'low')
        self.assertEqual(code, 0, err)
        child = self.tasks(room)['Help']
        self.assertEqual((child['tier'], child['effort']), ('haiku', 'low'))
        self.assertNotIn('tier_mismatch', child)          # plan-worker-b has no label: shown, no mismatch

    def test_relay_state_carries_the_tag(self):
        from common_room.relay import Relay
        room = self.start_room(selected=ALL_SEATS)
        self.assertEqual(self.approve(room, self.tagged_plan(tier='opus', effort='max'))[0], 0)
        relay = Relay(room.root, ['plan-worker-a'], clock=room.server.clock)
        (room.root / 'relay' / 'plan-worker-a' / 'requests' / 's1.json').write_text(
            json.dumps({'request_id': 's1', 'kind': 'state'}), encoding='utf-8')
        relay.process_once()
        state = json.loads((room.root / 'relay' / 'plan-worker-a' / 'relay-state.json').read_text(encoding='utf-8'))
        task = next(t for t in state['tasks'] if t['title'] == 'Task one')
        self.assertEqual((task['tier'], task['effort'], task['tier_mismatch']), ('opus', 'max', 'sonnet'))
        me = next(p for p in state['participants'] if p['id'] == 'plan-worker-a')
        self.assertEqual(me['tier'], 'sonnet')

    def test_a_mismatch_is_shown_and_the_task_still_runs(self):
        room = self.start_room()
        self.assertEqual(self.approve(room, self.tagged_plan(tier='opus'))[0], 0)
        task = self.tasks(room)['Task one']
        self.assertEqual(task['tier_mismatch'], 'sonnet')
        show = self.run_cli(room, 'plan-worker-a', 'show', task['id'], json_mode=False)[1]
        self.assertIn('seat label sonnet', show)
        self.assertEqual(self.run_cli(room, 'plan-worker-a', 'ack', task['id'])[0], 0)
        self.assertEqual(self.run_cli(room, 'plan-worker-a', 'claim', task['id'], '--seconds', '300')[0], 0)
        self.assertEqual(self.tasks(room)['Task one']['status'], 'claimed')


class RefusalTests(Base):
    def test_a_bad_tier_or_effort_in_a_plan_is_refused_clearly_and_nothing_is_created(self):
        room = self.start_room()
        for tags, words in (({'tier': 'gpt-5'}, 'tier must be one of haiku, sonnet, opus'),
                            ({'effort': 'huge'}, 'effort must be one of low, medium, high, xhigh, max'),
                            ({'tier': 1}, 'tier must be one of')):
            with self.subTest(tags=tags):
                code, out, err = self.approve(room, self.tagged_plan(**tags))
                self.assertNotEqual(code, 0)
                self.assertIn(words, err)
        self.assertEqual(self.tasks(room), {})

    def test_the_store_refuses_a_bad_value_from_any_client(self):
        room = self.start_room()
        keeper = room.store.people['keeper']
        msg = {'title': 'T', 'body': 'B', 'recipient': 'plan-worker-a', 'authority_id': 'plan-scope-one',
               'kind': 'handoff.record', 'acceptance': 'A'}
        for bad in ({'tier': 'Opus'}, {'effort': ''}, {'tier': None}):
            with self.subTest(bad=bad):
                with self.assertRaises(RoomError) as caught:
                    room.store.execute(keeper, 'intake', dict({'source_namespace': 'estate', 'external_reference': 'x',
                                                               'message': msg}, **bad), 'bad-' + str(len(str(bad))))
                self.assertEqual(caught.exception.code, 'invalid')
                self.assertIn('must be one of', str(caught.exception))

    def test_the_cli_refuses_a_bad_value_before_sending(self):
        room = self.start_room()
        code, out, err = self.run_cli(room, 'keeper', 'intake', '--scope', 'plan-scope-one', '--to', 'plan-worker-a',
                                      '--title', 'T', '--body', 'B', '--acceptance', 'A', '--tier', 'gpt')
        self.assertNotEqual(code, 0)
        self.assertEqual(self.tasks(room), {})

    def test_text_caps_still_hold_with_a_tag(self):
        room = self.start_room()
        for field, size in (('title', 121), ('body', 4001), ('acceptance', 2001)):
            with self.subTest(field=field):
                plan = self.tagged_plan(tier='haiku')
                plan['tasks'][0][field] = 'x' * size
                code, out, err = self.approve(room, plan)
                self.assertNotEqual(code, 0)
        self.assertEqual(self.tasks(room), {})


class SeatLabelTests(unittest.TestCase):
    def people(self):
        from common_room.protocol import Actor
        return dict({'keeper': Actor('keeper', 'owner')}, **{a['id']: Actor(a['id'], a['role']) for a in CONFIG['actors']})

    def test_bad_labels_are_refused_at_start(self):
        for labels in ({'plan-worker-a': 'gpt'}, {'nobody': 'opus'}, {'keeper': 'opus'}, ['plan-worker-a']):
            with self.subTest(labels=labels):
                with self.assertRaises(ValueError):
                    activation.start_room(case_dir('a4-bad'), enrollment=CONFIG, selected=ALL_SEATS, seat_tiers=labels).close()

    def test_a_missing_label_file_means_no_labels(self):
        self.assertIsNone(activation.load_seat_tiers(case_dir('a4-none') / 'seat-tiers.json'))
        example = activation.load_seat_tiers(TREE / 'crew' / 'seat-tiers.example.json')
        self.assertTrue(example and all(v in ('haiku', 'sonnet', 'opus') for v in example.values()))

    def test_the_join_card_names_the_seat_label_and_is_unchanged_without_one(self):
        enrollment = json.loads(json.dumps(CONFIG))
        plain = join_cards.render_card('plan-worker-a', enrollment)
        self.assertNotIn('Tier:', plain)
        self.assertEqual(join_cards.render_card('plan-worker-a', enrollment, tier=None), plain)
        labelled = join_cards.render_card('plan-worker-a', enrollment, tier='sonnet')
        self.assertIn('Tier: sonnet', labelled)
        self.assertEqual(labelled.replace([l for l in labelled.splitlines() if l.startswith('Tier: ')][0] + '\n', ''), plain)

    def test_a_bad_seat_tier_file_stops_the_room_start_with_exit_2(self):
        """The launcher path (activation.main): a bad label file is a configuration refusal, one line, exit 2, so
        keepalive stops instead of restarting the room forever, like a bad enrollment."""
        import contextlib, io
        root = case_dir('a4-main') / 'handoffs'
        activation.start_room(root, CONFIG, []).close()
        for name, body, words in (('broken.json', '{not json', 'not valid JSON'),
                                  ('shape.json', json.dumps({'seat_tiers': {}}), 'Invalid seat tier file'),
                                  ('label.json', json.dumps({'version': 1, 'seat_tiers': {'plan-worker-a': 'gpt'}}), 'unknown tier label')):
            (root / name).write_text(body, encoding='utf-8')
            err = io.StringIO()
            with self.subTest(name), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
                activation.main(['--root', str(root), '--seat-tiers', str(root / name)])
            self.assertEqual(ctx.exception.code, 2, err.getvalue())
            lines = err.getvalue().strip().splitlines()
            self.assertEqual(len(lines), 1, err.getvalue())
            self.assertTrue(lines[0].startswith('common_room.activation: '), lines)
            self.assertIn(words, lines[0])

    def test_the_join_card_script_reads_the_seat_tier_file(self):
        import contextlib, importlib.util, io
        spec = importlib.util.spec_from_file_location('join_card_a4', TREE / 'crew' / 'join_card.py')
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        tree = case_dir('a4-card-tree')     # a fictional tree: its enrollment and label file only
        (tree / '.runtime' / 'handoffs').mkdir(parents=True)
        (tree / 'crew').mkdir()
        (tree / '.runtime' / 'handoffs' / 'enrollment.json').write_text(json.dumps(CONFIG), encoding='utf-8')

        def card(seat):
            buf, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
                code = script.main([seat, '--tree', str(tree)])
            return code, buf.getvalue(), err.getvalue()
        code, plain, err = card('plan-worker-a')       # no label file: as before
        self.assertEqual(code, 0, err)
        self.assertNotIn('Tier:', plain)
        (tree / 'crew' / 'seat-tiers.json').write_text(json.dumps({'version': 1, 'seat_tiers': LABELS}), encoding='utf-8')
        code, labelled, err = card('plan-worker-a')
        self.assertEqual(code, 0, err)
        self.assertIn('Tier: sonnet', labelled)
        self.assertNotIn('Tier:', card('plan-worker-b')[1])   # an unlabelled seat's card shows no label
        (tree / 'crew' / 'seat-tiers.json').write_text('{not json', encoding='utf-8')
        code, out, err = card('plan-worker-a')
        self.assertEqual(code, 1)
        self.assertIn('seat-tiers.json', err)

    def test_the_board_card_shows_the_tag(self):
        js = (TREE / 'web' / 'board.js').read_text(encoding='utf-8')
        self.assertIn("'Tier: '+(t.tier||'-')", js)
        self.assertIn('t.tier_mismatch', js)


class RollbackStoreTests(Base):
    def old_store_module(self):
        shown = subprocess.run(['git', '-C', str(TREE), 'show', '356bbac:common_room/store.py'], capture_output=True,
                               text=True, encoding='utf-8')
        if shown.returncode != 0:
            self.skipTest('356bbac is not reachable through git here')
        old = types.ModuleType('common_room._store_356bbac_a4')
        old.__package__ = 'common_room'
        exec(compile(shown.stdout, '356bbac:common_room/store.py', 'exec'), old.__dict__)
        return old

    def test_356bbacs_store_opens_a_store_with_tagged_tasks(self):
        room = self.start_room()
        self.assertEqual(self.approve(room, self.tagged_plan(tier='opus', effort='xhigh'))[0], 0)
        tid = self.tasks(room)['Task one']['id']
        people, roots, path = dict(room.store.people), dict(room.store.roots), room.root / 'room.sqlite'
        room.close()
        old = self.old_store_module().Store(path, people, roots)
        task = old.task(people['keeper'], tid)
        self.assertEqual((task['tier'], task['effort']), ('opus', 'xhigh'))   # kept as plain fields, unused
        self.assertEqual(old.execute(people['plan-worker-a'], 'ack', {'task_id': tid}, 'old-ack')['task']['status'], 'acknowledged')

    def test_this_code_opens_a_store_356bbac_wrote(self):
        root = case_dir('a4-old-store')
        art = root / 'artifacts'
        art.mkdir()
        old = self.old_store_module()
        from common_room.protocol import Actor
        people = {'keeper': Actor('keeper', 'owner'), 'w': Actor('w', 'worker'), 'r': Actor('r', 'reviewer')}
        store = old.Store(root / 'room.sqlite', people, {'fixture': art})
        store.execute(people['keeper'], 'grant_handoff', {'id': 's', 'members': ['keeper', 'w', 'r'], 'capabilities': ['handoff.record'],
                      'root_id': 'fixture', 'scope': 'Old store', 'source_namespaces': ['estate']}, 'g1')
        tid = store.execute(people['keeper'], 'intake', {'source_namespace': 'estate', 'external_reference': 'e1', 'message': {
            'title': 'T', 'body': 'B', 'recipient': 'w', 'authority_id': 's', 'kind': 'handoff.record', 'acceptance': 'A'}}, 'i1')['task']['id']
        from common_room.store import Store
        new = Store(root / 'room.sqlite', people, {'fixture': art}, seat_tiers={'w': 'haiku'})
        task = new.task(people['keeper'], tid)
        self.assertNotIn('tier', task)
        self.assertNotIn('effort', task)


if __name__ == '__main__':
    unittest.main(argv=sys.argv[:1])    # evidence.py passes the tree root as an argument; it names no test
