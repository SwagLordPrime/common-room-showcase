"""Regression witnesses from the first independent protocol HOLD."""
import unittest
from common_room.protocol import Actor, RoomError, decode_request, make_event

class ProtocolRevisionTests(unittest.TestCase):
    def test_exponent_overflow_is_refused_at_every_json_depth(self):
        for raw in (b'{"n":1e999}', b'{"n":-1e999}', b'{"nested":[{"n":1e999}]}'):
            with self.subTest(raw=raw), self.assertRaises(RoomError):
                decode_request(raw)

    def test_surrogate_json_cannot_enter_utf8_protocol(self):
        with self.assertRaises(RoomError):
            decode_request(b'{"body":"\\ud800"}')

    def test_task_identity_cannot_be_a_mutable_value(self):
        for value in ([], {}, 7, True):
            with self.subTest(value=value), self.assertRaises(RoomError):
                make_event(1, "drafted", Actor("keeper","owner"), value, {}, 100.0)

    def test_actor_identity_and_role_are_checked_when_created(self):
        for identity,role in ((["keeper"],"owner"),("keeper",["owner"]),("keeper","superuser"),("","owner")):
            with self.subTest(identity=identity,role=role), self.assertRaises(RoomError):
                Actor(identity,role)

    def test_event_kind_and_time_fail_with_a_protocol_error(self):
        for kind, timestamp in (([],100.0),("drafted",10**1000),("drafted",float("inf"))):
            with self.subTest(kind=kind),self.assertRaises(RoomError):
                make_event(1,kind,Actor("keeper","owner"),"task-a",{},timestamp)

    def test_valid_finite_deep_json_and_scalar_identity_remain_accepted(self):
        self.assertEqual(decode_request(b'{"nested":[{"n":1e3}]}'),{"nested":[{"n":1000.0}]})
        event=make_event(1,"drafted",Actor("keeper","owner"),"task-a",{},100.0)
        self.assertEqual(event.task_id,"task-a")

if __name__=="__main__":unittest.main()
