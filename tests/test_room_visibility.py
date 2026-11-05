"""Aggregate public status and authenticated note receipts with fictional data."""
import http.client,json,unittest
from tests.test_handoff_client import HandoffHTTPTests

class VisibilityTests(HandoffHTTPTests):
    def public(self,path):
        c=http.client.HTTPConnection(self.host,timeout=3)
        try:
            c.request("GET",path);r=c.getresponse();return r.status,r.read()
        finally:c.close()
    def test_status_has_counts_without_scoped_content(self):
        owner=self.client();task=owner.command("intake",self.intake(body="private fixture body"),"intake")["task"]["id"]
        status,raw=self.public("/api/status");self.assertEqual(status,200)
        data=json.loads(raw)
        self.assertEqual(set(data),{"protocol","instance_id","server_time","last_event_seq","counts","active_claims","expired_claims","build","state_bytes","live_sessions"})
        self.assertEqual(data["counts"],{"draft":1})
        self.assertEqual(data["active_claims"],0)
        for forbidden in [task,"private fixture body","owner-key","builder-key","authorities","participants","notes"]:
            self.assertNotIn(forbidden,raw.decode())
        owner.command("deliver",{"task_id":task},"deliver")
        self.assertEqual(json.loads(self.public("/api/status")[1])["counts"],{"delivered":1})
    def test_note_receipt_metadata_matches_event(self):
        owner=self.client();owner.command("note",{"authority_id":"handoffs","body":"Scoped note"},"note")
        note=owner.state()["notes"][-1]
        # 2026-09-23 (root, crew build): notes also carry their own scope id so a seat can filter
        # its Lounge by scope. The viewer is already a member of that scope; nothing else is added.
        self.assertEqual(set(note),{"sender","authority_id","body","seq","timestamp","sha256"})
        self.assertEqual(note["authority_id"],"handoffs")
        events=self.store._snapshot(self.owner)[0];event=events[-1]
        from common_room.protocol import canonical
        import hashlib
        self.assertEqual(note["seq"],event["seq"])
        self.assertEqual(note["timestamp"],event["timestamp"])
        self.assertEqual(note["sha256"],hashlib.sha256(canonical(event).encode()).hexdigest())
    def test_status_page_and_exact_origin_guard(self):
        status,raw=self.public("/status");self.assertEqual(status,200)
        self.assertIn(b"Room status",raw)
        c=http.client.HTTPConnection(self.host,timeout=3)
        try:
            c.request("GET","/api/status",headers={"Origin":"https://example.com"})
            r=c.getresponse();r.read();self.assertEqual(r.status,403)
        finally:c.close()
