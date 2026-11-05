"""One switch stops the whole room, one switch starts it again (cr-upgrade-2026-10-01, task core-stop).

What the store and board now do, and what these tests hold them to:

  * `stop_all` (keeper only, i.e. the owner role) pauses every active authority it owns with the room's
    own pause, remembers which ones it paused, writes `STOP` (JSON {by, at, reason}) beside room.sqlite
    (.runtime/handoffs/STOP in the live room) and posts a Lounge note starting `STOP:` in every authority.
  * claims and every other worker action are refused while stopped, because the authorities are paused.
  * `resume_all` resumes only the authorities `stop_all` paused, leaves any that were already paused (or
    revoked) as they were, deletes the STOP file and posts `RESUME:`.
  * everyone who is not the keeper is refused, and the relay refuses both actions like every owner action.
  * the board shows a banner on every view while stopped, a keeper-only "Pause everything" that needs
    STOP typed to confirm, and a "Resume everything" button.
  * a log written before this change replays to the same states (fold() gains only a marker that old
    events never carry).
"""
import copy, itertools, json, os, pathlib, re, shutil, subprocess, sys, threading, types, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # so evidence.py can run this file as a script in a clean copy
from common_room.board import make_server
from common_room.handoff_api import CredentialPolicy
from common_room.participant import Participant
from common_room.protocol import Actor, RoomError, canonical
from common_room.store import Store, fold
from tests.helpers import ROOT, case_dir
from tests.test_cancel_and_delegated_decisions import (
    DelegationFixture, LEGACY_EVENTS_JSON, LEGACY_STATE_SHA256, legacy_scenario, state_digest)
from tests.test_relay import RelayTestCase
from tests.test_store_strike_kinds import drive_old_style_commands, load_store_at

BASELINE_COMMIT = '6ead0d5'  # the accepted core-strikes commit: this task's RED base


class StopFixture(DelegationFixture):
    """Three authorities (work, orch, extra) so 'every authority' means more than one."""
    def setUp(self):
        super().setUp()
        self.call(self.owner, 'grant', {'id': 'extra', 'members': ['owner', 'builder', 'second', 'reviewer'],
            'capabilities': ['document.count'], 'root_id': 'fixture', 'scope': 'Fictional third authority'})
        self.flag = self.db.parent / 'STOP'
        self.refs_made = itertools.count(1)

    def authorities(self):
        return self.store._snapshot(self.owner)[1]

    def tasks(self):
        return self.store._snapshot(self.owner)[2]

    def events(self):
        return self.store._snapshot(self.owner)[0]

    def paused(self):
        return {k: a['paused'] for k, a in self.authorities().items()}

    def stop(self, reason='Shane pressed the red button.', actor=None, key=None):
        return self.call(actor or self.owner, 'stop_all', {'reason': reason}, key)

    def resume(self, payload=None, actor=None, key=None):
        return self.call(actor or self.owner, 'resume_all', {} if payload is None else payload, key)

    def notes(self, prefix):
        return [(e['payload']['authority_id'], e['payload']['body'], e['actor']) for e in self.events()
                if e['kind'] == 'note' and e['payload']['body'].startswith(prefix)]


class StopAllTests(StopFixture):
    def test_stop_pauses_every_authority_writes_the_flag_and_posts_the_note(self):
        self.assertEqual(self.paused(), {'work': False, 'orch': False, 'extra': False})
        done = self.stop('Shane pressed the red button.')
        self.assertEqual(self.paused(), {'work': True, 'orch': True, 'extra': True})
        self.assertEqual(done['flag'], 'written')
        flag = json.loads(self.flag.read_text(encoding='utf-8'))
        self.assertEqual(set(flag), {'by', 'at', 'reason'})
        self.assertEqual((flag['by'], flag['reason']), ('owner', 'Shane pressed the red button.'))
        self.assertRegex(flag['at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        self.assertEqual(sorted(a for a, _, _ in self.notes('STOP:')), ['extra', 'orch', 'work'])
        for _, body, actor in self.notes('STOP:'): self.assertEqual((body, actor), ('STOP: Shane pressed the red button.', 'owner'))
        # one command, several events, one receipt: the receipt points at the last event
        self.assertEqual(len(done['seqs']), 6)
        self.assertEqual(done['seq'], done['seqs'][-1])
        self.assertEqual(self.events()[-1]['seq'], done['seq'])
        self.assertEqual([a['stopped_all']['by'] for a in self.authorities().values()], ['owner'] * 3)

    def test_claims_and_other_worker_actions_are_refused_while_stopped(self):
        task = self.root(); self.call(self.builder, 'ack', {'task_id': task})
        live, lease = self.claimed_root('live')
        self.stop()
        self.error('held', lambda: self.claim(task))
        self.error('held', lambda: self.call(self.builder, 'renew', {'task_id': live, 'fence': lease['fence'], 'seconds': 300}))
        self.error('held', lambda: self.call(self.builder, 'finish', {'task_id': live, 'fence': lease['fence'], 'body': 'Late', 'refs': [self.unique_ref()]}))
        # a current lease is cancelled without being called a failure, exactly as a deliberate pause does
        after = self.tasks()[live]
        self.assertEqual((after['status'], after['lease'], after['failed_attempts']), ('acknowledged', None, 0))
        self.assertEqual(self.tasks()[task]['status'], 'acknowledged')

    def test_resume_restores_only_what_stop_paused_and_removes_the_flag(self):
        self.call(self.owner, 'pause', {'authority_id': 'work'})   # paused by hand before the stop
        self.stop()
        self.assertEqual(self.paused(), {'work': True, 'orch': True, 'extra': True})
        self.assertNotIn('stopped_all', self.authorities()['work'])  # not paused by the stop
        done = self.resume()
        self.assertEqual(done['flag'], 'removed')
        self.assertFalse(self.flag.exists())
        self.assertEqual(self.paused(), {'work': True, 'orch': False, 'extra': False})
        self.assertFalse(any('stopped_all' in a or 'room_stopped' in a for a in self.authorities().values()))
        self.assertEqual(sorted(a for a, _, _ in self.notes('RESUME:')), ['extra', 'orch', 'work'])
        task = self.root(); self.call(self.builder, 'ack', {'task_id': task})
        self.assertEqual(self.claim(task)['holder'], 'builder')   # work goes on again where it was resumed

    def test_only_the_keeper_may_stop_or_resume(self):
        before = len(self.events())
        for actor in (self.builder, self.second, self.reviewer, self.third, self.visitor):
            self.error('forbidden', lambda a=actor: self.stop(actor=a))
        self.assertEqual((len(self.events()), self.flag.exists()), (before, False))
        self.stop()
        for actor in (self.builder, self.second, self.reviewer, self.third, self.visitor):
            self.error('forbidden', lambda a=actor: self.resume(actor=a))
        self.assertEqual(self.paused(), {'work': True, 'orch': True, 'extra': True})
        self.assertTrue(self.flag.exists())

    def test_stop_and_resume_refuse_the_wrong_state_and_bad_fields(self):
        self.error('state', lambda: self.resume())                 # not stopped
        self.stop()
        self.error('state', lambda: self.stop())                   # already stopped
        self.resume()
        for bad in ('', '   ', 'x' * 501):
            self.error('invalid', lambda b=bad: self.call(self.owner, 'stop_all', {'reason': b}))
        self.error('invalid', lambda: self.call(self.owner, 'stop_all', {}))
        self.error('invalid', lambda: self.call(self.owner, 'stop_all', {'reason': 'x', 'extra': 1}))
        self.error('invalid', lambda: self.call(self.owner, 'resume_all', {'authority_id': 'work'}))
        self.assertEqual(self.paused(), {'work': False, 'orch': False, 'extra': False})
        self.assertFalse(self.flag.exists())

    def test_the_resume_reason_is_optional(self):
        self.stop(); self.resume()
        self.stop(); self.resume({'reason': 'Back to work after the fix.'})
        bodies = [b for _, b, _ in self.notes('RESUME:')]
        self.assertEqual(sorted(set(bodies)), ['RESUME: Back to work after the fix.', 'RESUME: Resumed by the keeper.'])

    def test_a_revoked_authority_is_left_alone(self):
        self.call(self.owner, 'revoke', {'authority_id': 'extra'})
        self.stop()
        self.assertEqual(self.paused(), {'work': True, 'orch': True, 'extra': False})
        self.assertEqual(sorted(a for a, _, _ in self.notes('STOP:')), ['orch', 'work'])
        self.resume()
        self.assertEqual(self.paused(), {'work': False, 'orch': False, 'extra': False})
        self.assertTrue(self.authorities()['extra']['revoked'])

    def test_a_single_authority_resumed_by_hand_leaves_the_rest_for_resume_all(self):
        self.stop()
        self.call(self.owner, 'resume', {'authority_id': 'orch'})
        self.assertEqual(self.paused(), {'work': True, 'orch': False, 'extra': True})
        self.assertNotIn('stopped_all', self.authorities()['orch'])
        self.resume()
        self.assertEqual(self.paused(), {'work': False, 'orch': False, 'extra': False})
        self.assertFalse(self.flag.exists())

    def test_the_owner_with_nothing_to_stop_is_refused(self):
        bare = Store(self.home / 'bare.sqlite', self.people, self.roots, clock=self.clock)
        self.error('state', lambda: bare.execute(self.owner, 'stop_all', {'reason': 'Nothing here'}, 'bare-1'))
        self.error('state', lambda: bare.execute(self.owner, 'resume_all', {}, 'bare-2'))

    def test_a_retry_with_the_same_key_replays_one_receipt(self):
        first = self.stop(key='stop-key')
        count = len(self.events())
        self.flag.unlink()                      # a replay must not touch the file again
        again = self.stop(key='stop-key')
        self.assertEqual(len(self.events()), count)
        self.assertEqual({k: v for k, v in first.items() if k != 'flag'}, again)
        self.assertFalse(self.flag.exists())
        self.error('idempotency', lambda: self.stop('A different reason', key='stop-key'))
        self.resume(key='resume-key')
        count = len(self.events())
        self.flag.write_text('{}', encoding='utf-8')      # a replayed resume must not delete it again
        self.resume(key='resume-key')
        self.assertEqual(len(self.events()), count)
        self.assertTrue(self.flag.exists())

    def test_a_failed_flag_write_never_undoes_the_committed_stop(self):
        (self.db.parent / 'STOP.tmp').mkdir()   # the temp file path is a folder, so the write fails
        done = self.stop()
        self.assertTrue(done['flag'].startswith('failed: '))
        self.assertEqual(self.paused(), {'work': True, 'orch': True, 'extra': True})
        self.assertFalse(self.flag.exists())

    def test_stop_changes_task_state_exactly_as_pausing_each_authority_by_hand(self):
        live = self.root('live'); self.call(self.builder, 'ack', {'task_id': live}); self.claim(live, seconds=900)
        old, old_lease = self.claimed_root('old')              # a 300 second lease
        self.clock.now += 400                                  # 'old' is now expired; 'live' still holds its lease
        copy_db = self.home / 'by-hand.sqlite'
        shutil.copy(self.db, copy_db)
        by_hand = Store(copy_db, self.people, self.roots, clock=self.clock)
        for authority in ('work', 'orch', 'extra'): by_hand.execute(self.owner, 'pause', {'authority_id': authority}, 'hand-' + authority)
        self.stop()
        stopped = fold(self.events()); manual = fold(by_hand._snapshot(self.owner)[0])
        for authority in stopped[0].values(): authority.pop('stopped_all', None); authority.pop('room_stopped')
        self.assertEqual(canonical(stopped[0]), canonical(manual[0]))
        self.assertEqual(canonical(stopped[1]), canonical(manual[1]))
        self.assertEqual(self.tasks()[old]['lease_expiry']['policy'], 'release-v1')

    def test_the_stop_record_says_which_authorities_it_paused_and_which_were_already_paused(self):
        self.call(self.owner, 'pause', {'authority_id': 'work'})
        self.stop('Check the record.')
        for authority in self.authorities().values():
            record = authority['room_stopped']
            self.assertEqual((record['by'], record['reason']), ('owner', 'Check the record.'))
            self.assertEqual((sorted(record['paused']), record['already_paused']), (['extra', 'orch'], ['work']))

    def test_a_stop_over_authorities_that_were_all_paused_by_hand_can_still_be_undone(self):
        for authority in ('work', 'orch', 'extra'): self.call(self.owner, 'pause', {'authority_id': authority})
        self.stop()
        self.assertFalse(any('stopped_all' in a for a in self.authorities().values()))   # it paused nothing itself
        self.assertTrue(all('room_stopped' in a for a in self.authorities().values()))   # but the room is stopped
        self.assertTrue(self.flag.exists())
        self.error('state', lambda: self.stop('Again'))
        done = self.resume()
        self.assertEqual(done['flag'], 'removed'); self.assertFalse(self.flag.exists())
        self.assertEqual(self.paused(), {'work': True, 'orch': True, 'extra': True})        # still paused by hand
        self.assertFalse(any('room_stopped' in a for a in self.authorities().values()))
        self.error('state', lambda: self.resume())

    def test_resuming_every_authority_by_hand_does_not_strand_the_stop(self):
        self.stop()
        for authority in ('work', 'orch', 'extra'): self.call(self.owner, 'resume', {'authority_id': authority})
        self.assertEqual(self.paused(), {'work': False, 'orch': False, 'extra': False})
        self.assertTrue(all('room_stopped' in a for a in self.authorities().values()))      # the room is still marked stopped
        self.assertTrue(self.flag.exists())
        self.error('state', lambda: self.stop('Again'))
        before = len(self.events())
        done = self.resume()
        self.assertEqual(done['flag'], 'removed'); self.assertFalse(self.flag.exists())
        self.assertEqual(len(done['seqs']), 3)                                              # only the three RESUME notes
        self.assertEqual(len(self.events()), before + 3)
        self.assertFalse(any('room_stopped' in a for a in self.authorities().values()))
        self.assertEqual(self.paused(), {'work': False, 'orch': False, 'extra': False})


class RelayStopRefusalTests(RelayTestCase):
    def test_the_relay_refuses_stop_all_and_resume_all_before_sending(self):
        room = self.start(); relay = self.relay(room, ['relay-writer'])
        calls = []; real = relay.clients['relay-writer'].command
        def spy(action, payload, request_id):
            calls.append(action); return real(action, payload, request_id)
        relay.clients['relay-writer'].command = spy
        before = len(self.events(room))
        for action, payload in (('stop_all', {'reason': 'Sneaky'}), ('resume_all', {})):
            self.write_request(room, 'relay-writer', action + '.json', {'request_id': action + '-req', 'kind': 'command', 'action': action, 'payload': payload})
        relay.process_once()
        for action in ('stop_all', 'resume_all'):
            resp = self.response(room, 'relay-writer', action + '-req')
            self.assertFalse(resp['ok']); self.assertEqual(resp['error'], 'forbidden')
        self.assertEqual((len(self.events(room)), calls), (before, []))


class BoardStopTests(StopFixture):
    def setUp(self):
        super().setUp()
        self.tokens = {'owner-key': self.owner, 'builder-key': self.builder, 'review-key': self.reviewer}
        self.policy = CredentialPolicy(self.tokens, self.clock.now + 28800, self.clock)
        self.server = make_server(self.store, self.tokens, clock=self.clock, credential_policy=self.policy)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)

    def client(self, token):
        c = Participant(self.server.origin, token); c.login(); return c

    def test_the_board_state_says_when_the_room_is_stopped(self):
        for token in ('owner-key', 'builder-key'): self.assertEqual(self.client(token).state()['stop'], {'active': False})
        self.client('owner-key').command('stop_all', {'reason': 'Paused from the test.'}, 'board-stop')
        for token in ('owner-key', 'builder-key', 'review-key'):
            stop = self.client(token).state()['stop']
            self.assertEqual((stop['active'], stop['by'], stop['reason']), (True, 'owner', 'Paused from the test.'))
            self.assertIsInstance(stop['at'], float)
        with self.assertRaisesRegex(ValueError, 'forbidden'): self.client('builder-key').command('resume_all', {}, 'sneaky-resume')
        self.client('owner-key').command('resume_all', {}, 'board-resume')
        self.assertEqual(self.client('builder-key').state()['stop'], {'active': False})

    def views(self, states, script):
        node = os.environ.get('COMMON_ROOM_NODE') or shutil.which('node')
        if not node: self.skipTest('node not found: set COMMON_ROOM_NODE or put node on PATH')
        run = subprocess.run([node, '-e', STOP_HARNESS], input=json.dumps({'states': states, 'script': script}),
                             capture_output=True, text=True, cwd=str(ROOT), timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def test_the_real_board_script_shows_the_banner_and_the_keeper_only_buttons(self):
        calm = {t: self.client(t).state() for t in ('owner-key', 'builder-key')}
        self.client('owner-key').command('stop_all', {'reason': 'Paused from the test.'}, 'ui-stop')
        stopped = {t: self.client(t).state() for t in ('owner-key', 'builder-key')}
        seen = self.views({'calm-owner': calm['owner-key'], 'calm-builder': calm['builder-key'],
                           'stopped-owner': stopped['owner-key'], 'stopped-builder': stopped['builder-key']}, None)
        calm_owner, calm_builder = seen['calm-owner'], seen['calm-builder']
        self.assertEqual((calm_owner['banner_hidden'], calm_owner['stop_hidden'], calm_owner['resume_hidden']), (True, False, True))
        self.assertEqual((calm_builder['banner_hidden'], calm_builder['stop_hidden'], calm_builder['resume_hidden']), (True, True, True))
        stopped_owner, stopped_builder = seen['stopped-owner'], seen['stopped-builder']
        self.assertEqual((stopped_owner['banner_hidden'], stopped_owner['stop_hidden'], stopped_owner['resume_hidden']), (False, True, False))
        self.assertEqual((stopped_builder['banner_hidden'], stopped_builder['stop_hidden'], stopped_builder['resume_hidden']), (False, True, True))
        for view in (stopped_owner, stopped_builder):
            self.assertIn('THE ROOM IS STOPPED', view['banner']); self.assertIn('Stopped by owner', view['banner'])
            self.assertIn('Reason: Paused from the test.', view['banner'])

    def test_the_stop_button_needs_STOP_typed_before_anything_is_sent(self):
        calm = self.client('owner-key').state()
        seen = self.views({'owner': calm}, 'typed-confirm')['owner']
        self.assertEqual(seen['form_hidden_before'], True)
        self.assertEqual(seen['form_hidden_after_click'], False)
        self.assertEqual(seen['go_disabled'], {'': True, 'stop': True, 'STO': True, 'STOP ': True, 'STOP': False})
        self.assertEqual(seen['sent_with_wrong_text'], [])
        self.assertEqual(seen['sent_with_STOP'], [{'path': '/api/command', 'action': 'stop_all', 'payload': {'reason': 'Paused from the board by the keeper.'}}])
        self.assertEqual(seen['resume_sent'], [{'path': '/api/command', 'action': 'resume_all', 'payload': {}}])


# A node harness that runs the real web/board.js against a fake page, like the cancel tests' one.
STOP_HARNESS = r'''
const fs=require('fs'),vm=require('vm'),path=require('path'),nodeCrypto=require('crypto');
const {PendingJournal}=require(path.resolve('web/recovery.js'));
const source=fs.readFileSync('web/board.js','utf8');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
function make(tag){return {tagName:String(tag).toUpperCase(),children:[],dataset:{},listeners:{},hidden:false,disabled:false,required:false,value:'',textContent:'',className:'',
  get options(){return this.children.filter(x=>x.tagName==='OPTION');},get firstChild(){return this.children[0];},
  append(...k){this.children.push(...k);},replaceChildren(...k){this.children=[...k];},after(){},focus(){},setAttribute(k,v){this[k]=v;},
  addEventListener(t,f){(this.listeners[t]=this.listeners[t]||[]).push(f);}};}
const settle=async()=>{for(let i=0;i<30;i++)await new Promise(r=>setImmediate(r));};
async function view(state,script){
  const ids={},requests=[];
  const document={getElementById:id=>ids[id]||(ids[id]=make('div')),createElement:make,querySelector:()=>make('p'),hidden:false};
  // start each element as index.html ships it: hidden / disabled attributes in the markup
  for(const m of fs.readFileSync('web/index.html','utf8').matchAll(/<(\w+)\b([^>]*)>/g)){
   const id=/\bid="([^"]+)"/.exec(m[2]);if(!id)continue;const el=document.getElementById(id[1]);
   if(/(^|\s)hidden(\s|$|=)/.test(m[2]))el.hidden=true;if(/(^|\s)disabled(\s|$|=)/.test(m[2]))el.disabled=true;}
  const context=vm.createContext({document,PendingJournal,console,crypto:nodeCrypto,setInterval:()=>0,Intl,
   sessionStorage:{getItem:()=>null,setItem(){},removeItem(){}},
   fetch:async(p,o)=>{requests.push({path:p,body:o&&o.body?JSON.parse(o.body):null});return {ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(state))};}});
  vm.runInContext(source,context);await settle();
  const g=id=>ids[id];
  const out={banner_hidden:g('stop-banner').hidden,banner:g('stop-banner').textContent,stop_hidden:g('stop-all').hidden,resume_hidden:g('resume-all').hidden};
  const commands=()=>requests.filter(r=>r.path==='/api/command'&&r.body).map(r=>({path:r.path,action:r.body.action,payload:r.body.payload}));
  if(script==='typed-confirm'){
   out.form_hidden_before=g('stop-form').hidden;
   g('stop-all').onclick();out.form_hidden_after_click=g('stop-form').hidden;
   out.go_disabled={};
   for(const text of ['','stop','STO','STOP ','STOP']){g('stop-confirm').value=text;g('stop-confirm').oninput();out.go_disabled[text]=g('stop-go').disabled;}
   requests.length=0;g('stop-confirm').value='stop';await g('stop-form').onsubmit({preventDefault(){}});await settle();out.sent_with_wrong_text=commands();
   requests.length=0;g('stop-confirm').value='STOP';await g('stop-form').onsubmit({preventDefault(){}});await settle();out.sent_with_STOP=commands();
   requests.length=0;g('resume-all').onclick();await settle();out.resume_sent=commands();
  }
  return out;
}
(async()=>{const out={};for(const [name,state] of Object.entries(input.states))out[name]=await view(state,input.script);
  process.stdout.write(JSON.stringify(out));})().catch(e=>{console.error(e.stack||String(e));process.exit(1);});
'''


class StopFixtureLog(StopFixture):
    """Records the event log after a stop with work in flight, and again after the resume."""
    @classmethod
    def record(cls):
        fixture = cls(); fixture.setUp()
        try:
            live = fixture.root('live'); fixture.call(fixture.builder, 'ack', {'task_id': live}); fixture.claim(live, seconds=900)
            fixture.stop(); stopped = fixture.events()
            fixture.resume(); resumed = fixture.events()
            return [stopped, resumed]
        finally:
            fixture.doCleanups()


class ReplayIdentityTests(unittest.TestCase):
    """GUARDS, green on the base by design: the new switch must not change what old logs mean."""
    def test_a_recorded_log_folds_to_the_recorded_state_and_gains_no_marker(self):
        events = json.loads(LEGACY_EVENTS_JSON)
        self.assertEqual(state_digest(fold, events), LEGACY_STATE_SHA256)
        authorities, _ = fold(copy.deepcopy(events))
        self.assertFalse(any('stopped_all' in a for a in authorities.values()))

    def test_the_recorded_scenario_still_writes_the_recorded_log(self):
        self.assertEqual(legacy_scenario(Store, case_dir('stop-legacy')), json.loads(LEGACY_EVENTS_JSON))

    def test_old_style_commands_write_the_same_events_as_the_base_store(self):
        base = load_store_at(BASELINE_COMMIT)
        if base is None: self.skipTest('base commit ' + BASELINE_COMMIT + ' is not reachable through git here')
        self.assertEqual(drive_old_style_commands(Store, case_dir('stop-now')), drive_old_style_commands(base.Store, case_dir('stop-then')))

    def test_a_log_with_stop_and_resume_events_folds_under_the_core_setup_baseline_to_the_same_work(self):
        """Rollback safety for the cutover: the stop uses only event kinds the baseline knows, and the baseline
        ignores the extra fields, so a database written with these events replays under c166619."""
        baseline = load_store_at('c166619')
        if baseline is None: self.skipTest('baseline commit c166619 is not reachable through git here')
        events = StopFixtureLog.record()
        for events_so_far in events:
            then = baseline.fold(copy.deepcopy(events_so_far)); now = fold(copy.deepcopy(events_so_far))
            for authority in now[0].values(): authority.pop('stopped_all', None); authority.pop('room_stopped', None)
            self.assertEqual(canonical(then[0]), canonical(now[0]))
            self.assertEqual(canonical(then[1]), canonical(now[1]))
        self.assertTrue(any('stopped_all' in a for a in fold(events[0])[0].values()))  # the first log really held a live stop

    def test_a_log_written_by_the_base_store_folds_the_same_under_both(self):
        base = load_store_at(BASELINE_COMMIT)
        if base is None: self.skipTest('base commit ' + BASELINE_COMMIT + ' is not reachable through git here')
        events = drive_old_style_commands(base.Store, case_dir('stop-fold'))
        self.assertEqual(canonical(base.fold(copy.deepcopy(events))), canonical(fold(copy.deepcopy(events))))


if __name__ == '__main__':
    unittest.main()
