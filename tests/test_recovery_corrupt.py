"""Regression for unreadable browser storage during a participant switch."""
import subprocess,unittest
from pathlib import Path
class CorruptRecoveryTests(unittest.TestCase):
    def _resolve_node_path(self):
        """Resolve node executable: COMMON_ROOM_NODE env var, hardcoded path, or which('node')."""
        import os, shutil
        # Try environment variable first
        env_node = os.environ.get('COMMON_ROOM_NODE')
        if env_node and Path(env_node).exists():
            return Path(env_node)
        # Try hardcoded path (the original fallback)
        hardcoded = Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
        if hardcoded.exists():
            return hardcoded
        # Try PATH
        which_result = shutil.which('node')
        if which_result:
            return Path(which_result)
        # Not found
        return None

    def test_unreadable_storage_cannot_retain_previous_actor_action(self):
        node = self._resolve_node_path()
        if not node:
            self.skipTest('node not found: set COMMON_ROOM_NODE or put node on PATH')
        script=r"""
const assert=require('node:assert/strict'),{PendingJournal}=require('./web/recovery.js');
for(const failure of ['json','read','remove']){
 let raw=null,broken=false;
 const storage={getItem:()=>{if(broken&&failure==='read')throw Error('read failed');return broken?'{bad':raw;},
 setItem:(k,v)=>raw=v,removeItem:()=>{if(broken&&failure==='remove')throw Error('remove failed');raw=null;}};
 const j=new PendingJournal(storage);j.bind('room-one','worker-one');
 j.begin('command',{action:'ack',payload:{task_id:'old-task'},request_id:'old-id'});broken=true;
 try{j.bind('room-two','reviewer-two');}catch(e){}
 assert.equal(j.pending,null,'Old pending retained after '+failure);
}
console.log('All corruption and storage-failure switches clear prior actions.');
"""
        p=subprocess.run([str(node),'-e',script],capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
