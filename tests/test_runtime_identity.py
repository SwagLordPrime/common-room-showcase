"""Public startup identity, using fictional activation fixtures only."""
import http.client, json, unittest
from pathlib import Path
from tests.helpers import case_dir
from tests.test_activation import CONFIG

class RuntimeIdentityTests(unittest.TestCase):
    def test_runtime_identity(self):
        from common_room.activation import start_room
        from common_room.build_identity import source_identity
        r=start_room(case_dir("identity"),enrollment=CONFIG)
        self.addCleanup(r.close)
        c=http.client.HTTPConnection(r.server.host,timeout=5)
        self.addCleanup(c.close)
        c.request("GET","/api/instance")
        response=c.getresponse();data=json.loads(response.read())
        self.assertEqual(response.status,200)
        self.assertEqual(set(data),{"protocol","instance_id","build"})
        self.assertEqual(data["build"],source_identity())
        self.assertEqual(r.descriptor["build"],data["build"])
        self.assertEqual(set(data["build"]),{"source_sha256","hash_scheme","lease_expiry_policy"})
        self.assertEqual(data["build"]["lease_expiry_policy"],"release-v1")
        self.assertRegex(data["build"]["source_sha256"],r"^[a-f0-9]{64}$")
        self.assertNotIn("token",json.dumps(data))
        self.assertNotIn("test-writer",json.dumps(data))
    def test_digest_covers_source_and_web_but_not_runtime(self):
        from common_room.build_identity import source_identity
        root=case_dir("identity-source")
        (root/"common_room").mkdir();(root/"web").mkdir();(root/".runtime").mkdir()
        (root/"common_room/a.py").write_text("x=1")
        (root/"web/a.js").write_text("x=1")
        first=source_identity(root)
        (root/".runtime/secret.json").write_text("fixture")
        self.assertEqual(first,source_identity(root))
        (root/"web/a.js").write_text("x=2")
        self.assertNotEqual(first["source_sha256"],source_identity(root)["source_sha256"])
        web_changed=source_identity(root)
        (root/"common_room/a.py").write_text("x=2")
        self.assertNotEqual(web_changed["source_sha256"],source_identity(root)["source_sha256"])
    def test_identity_is_captured_at_startup(self):
        from common_room.activation import start_room
        from unittest.mock import patch
        r=start_room(case_dir("identity-snapshot"),enrollment=CONFIG)
        self.addCleanup(r.close)
        with patch("common_room.board.source_identity",side_effect=AssertionError("request rehashed source")):
            c=http.client.HTTPConnection(r.server.host,timeout=5)
            self.addCleanup(c.close);c.request("GET","/api/instance")
            response=c.getresponse()
            self.assertEqual(json.loads(response.read())["build"],r.descriptor["build"])
            self.assertEqual(response.status,200)
