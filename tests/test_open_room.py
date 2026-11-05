"""Canonical saved-room opener tests; fictional local rooms only."""
import json,unittest
from unittest.mock import patch
from tests.helpers import case_dir
from tests.test_activation import CONFIG

class SavedRoomTests(unittest.TestCase):
    def test_reuses_exact_instance_without_authentication_or_new_room(self):
        from common_room.activation import start_room
        from common_room.open_room import open_saved
        r=start_room(case_dir('openroom'),CONFIG,['test-writer']);self.addCleanup(r.close)
        before=(r.root/'keeper-credential.json').read_bytes()
        with patch('common_room.open_room.webbrowser.open') as opened:
            result=open_saved(r.root)
        self.assertIsNone(result);opened.assert_called_once_with(r.server.origin)
        self.assertEqual((r.root/'keeper-credential.json').read_bytes(),before);self.assertEqual(r.server.sessions,{})
    def test_restart_uses_saved_selection_and_revocations_same_port(self):
        from common_room.activation import start_room
        from common_room.open_room import open_saved
        r=start_room(case_dir('openroom'),CONFIG,['test-writer','test-reader']);origin=r.server.origin
        r.server.credential_policy.revoke('test-reader');old=r.credentials['test-writer']['token'];r.close()
        again=open_saved(r.root,launch_browser=False);self.addCleanup(again.close)
        self.assertEqual(again.server.origin,origin);self.assertIn('test-writer',again.credentials);self.assertNotIn('test-reader',again.credentials);self.assertNotEqual(again.credentials['test-writer']['token'],old)
    def test_missing_or_partial_runtime_refused(self):
        from common_room.open_room import open_saved
        root=case_dir('openroom')
        with self.assertRaises(ValueError):open_saved(root,launch_browser=False)
        self.assertFalse((root/'room.sqlite').exists())
    def test_mismatched_live_instance_refused_without_credentials(self):
        from common_room.activation import start_room
        from common_room.open_room import open_saved
        r=start_room(case_dir('openroom'),CONFIG,['test-writer']);self.addCleanup(r.close)
        p=r.root/'connection.json';d=json.loads(p.read_text());d['instance_id']='0'*32;p.write_text(json.dumps(d))
        with self.assertRaises(ValueError):open_saved(r.root,launch_browser=False)
        self.assertEqual(r.server.sessions,{})
    def test_saved_selection_unknown_actor_refused(self):
        from common_room.activation import start_room
        from common_room.open_room import open_saved
        r=start_room(case_dir('openroom'),CONFIG,['test-writer']);r.close()
        (r.root/'participant-selection.json').write_text(json.dumps({'version':1,'participants':['unknown']}))
        with self.assertRaises(ValueError):open_saved(r.root,launch_browser=False)

    def test_headless_room_survives_closed_stdin(self):
        import subprocess,sys,time
        from common_room.activation import start_room
        from common_room.open_room import descriptor,probe
        r=start_room(case_dir('headless'),CONFIG,['test-writer']);r.close()
        child=subprocess.Popen([sys.executable,'-X','utf8','-B','-m','common_room.open_room','--root',str(r.root),'--no-browser','--headless'],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        try:
            deadline=time.monotonic()+6;ready=False
            while time.monotonic()<deadline and child.poll() is None:
                try:ready=probe(descriptor(r.root))
                except ValueError:pass
                if ready:break
                time.sleep(.05)
            self.assertTrue(ready,'Headless process failed to keep the same saved room running with stdin closed')
            self.assertIsNone(child.poll())
        finally:
            if child.poll() is None:child.terminate()
            child.communicate(timeout=5)
