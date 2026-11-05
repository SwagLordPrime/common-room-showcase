"""Browser report API acceptance with real loopback requests and fictional actors."""
import hashlib, unittest
from tests.test_handoff_client import HandoffHTTPTests
class BrowserArtifactTests(HandoffHTTPTests):
    def claimed(self):
        owner=self.client();worker=self.client('builder-key');review=self.client('review-key')
        t=owner.command('intake',self.intake(),'intake')['task']['id']
        owner.command('deliver',{'task_id':t},'deliver');worker.command('ack',{'task_id':t},'ack')
        fence=worker.command('claim',{'task_id':t,'seconds':300},'claim')['task']['lease']['fence']
        return t,fence,owner,worker,review
    def test_stage_exact_bytes_view_after_finish_separate_review(self):
        t,f,o,w,r=self.claimed();report='Saved report: café\n<script>inert</script>'
        payload={'task_id':t,'fence':f,'text':report}
        ref=w.handoff('stage_text',payload)
        self.assertEqual(ref,w.handoff('stage_text',payload))
        self.assertEqual(ref['sha256'],hashlib.sha256(report.encode()).hexdigest())
        with self.assertRaises(ValueError):r.handoff('view_text',{'task_id':t,**ref})
        finish={'task_id':t,'fence':f,'body':'Short completion summary only','refs':[ref]}
        first=w.command('finish',finish,'finish-lost')
        self.assertEqual(first,w.command('finish',finish,'finish-lost'))
        result=r.handoff('view_text',{'task_id':t,**ref})
        self.assertEqual(result,dict(ref,text=report))
        self.assertEqual(r.command('review',{'task_id':t,'accepted':True,'note':'Read exact saved report'},'review')['task']['status'],'done')
    def test_stage_empty_oversized_wrong_actor_and_stale_fence_refused(self):
        t,f,o,w,r=self.claimed()
        for text in ['', ' ', 'x'*13000, '\x00bad']:
            with self.assertRaises(ValueError):w.handoff('stage_text',{'task_id':t,'fence':f,'text':text})
        with self.assertRaises(ValueError):o.handoff('stage_text',{'task_id':t,'fence':f,'text':'Owner cannot submit'})
        with self.assertRaises(ValueError):w.handoff('stage_text',{'task_id':t,'fence':f+1,'text':'Wrong fence'})
        self.clock.now+=301
        with self.assertRaises(ValueError):w.handoff('stage_text',{'task_id':t,'fence':f,'text':'Expired'})
    def test_view_refuses_unreferenced_corrupt_and_revoked(self):
        t,f,o,w,r=self.claimed()
        ref=w.handoff('stage_text',{'task_id':t,'fence':f,'text':'Original report'})
        w.command('finish',{'task_id':t,'fence':f,'body':'Summary','refs':[ref]},'finish')
        with self.assertRaises(ValueError):r.handoff('view_text',{'task_id':t,'path':'other.txt','sha256':ref['sha256']})
        (self.art/ref['path']).write_text('corrupt')
        with self.assertRaises(ValueError):r.handoff('view_text',{'task_id':t,**ref})
        self.policy.revoke('reviewer')
        with self.assertRaises(ValueError):r.handoff('view_text',{'task_id':t,**ref})
    def test_state_has_restart_instance_and_no_owner_token(self):
        state=self.client('builder-key').state()
        self.assertTrue(state['instance_id'])
        self.assertNotIn('owner-key',str(state))

class BrowserRecoveryTests(unittest.TestCase):
    def _resolve_node_path(self):
        """Resolve node executable: COMMON_ROOM_NODE env var, hardcoded path, or which('node')."""
        import os, shutil
        from pathlib import Path
        # Try environment variable first
        env_node = os.environ.get('COMMON_ROOM_NODE')
        if env_node and Path(env_node).exists():
            return Path(env_node)
        # Try hardcoded Windows path (for Shane's machine)
        hardcoded = Path(r'C:/Users/you/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe')
        if hardcoded.exists():
            return hardcoded
        # Try PATH
        which_result = shutil.which('node')
        if which_result:
            return Path(which_result)
        # Not found
        return None

    def test_pending_journal_reloads_retries_and_refuses_identity_change(self):
        import subprocess
        from pathlib import Path
        node = self._resolve_node_path()
        if not node:
            self.skipTest('node not found: set COMMON_ROOM_NODE or put node on PATH')
        script=r"""
const assert=require('node:assert/strict'),{PendingJournal}=require('./web/recovery.js');
const data=new Map(),s={getItem:k=>data.get(k)||null,setItem:(k,v)=>data.set(k,v),removeItem:k=>data.delete(k)};
let j=new PendingJournal(s);j.bind('instance1','worker');
const r={action:'ack',payload:{task_id:'task'},request_id:'same-id'};
j.begin('command',r);
j=new PendingJournal(s);assert.equal(j.bind('instance1','worker'),'');
assert.equal(j.pending.request.request_id,'same-id');
assert.equal(j.begin('command',{...r,request_id:'new-id'}).request.request_id,'same-id');
assert.throws(()=>j.begin('command',{...r,action:'claim'}));
assert.match(j.bind('instance1','reviewer'),/discarded/i);assert.equal(j.pending,null);
j.begin('submit',{task_id:'task',fence:1,text:'Exact report',body:'Different summary',request_id:'finish-id'});
j.update({ref:{path:'results/file.txt',sha256:'a'.repeat(64)}});
j=new PendingJournal(s);j.bind('instance1','reviewer');assert.equal(j.pending.request.text,'Exact report');
assert.equal(j.pending.request.ref.sha256,'a'.repeat(64));assert.match(j.bind('instance2','reviewer'),/discarded/i);
assert.equal(data.size,0);
const broken=new PendingJournal({getItem:()=>null,setItem:()=>{throw Error('unavailable')},removeItem:()=>{}});
broken.bind('i','a');assert.throws(()=>broken.begin('command',r));
console.log('Pending recovery behavior passed');
"""
        p=subprocess.run([str(node),'-e',script],capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
