"""Host checks for pairing entry and the saved session. No badge and no network."""
import os
import sys
import tempfile
import unittest

APP = os.path.join(os.path.dirname(__file__), "..", "app", "dots_boxes")
sys.path.insert(0, APP)

from dots_pairing import CodeEntry
from dots_session import SessionFile, fresh_session, uuid4, validate_session


def entropy(size):
    return b"\x11" * size


class CodeEntryTests(unittest.TestCase):
    def test_fifth_letter_submits_and_up_deletes(self):
        entry = CodeEntry()
        entry.ready = True
        self.assertIsNone(entry.step(["A"]))
        self.assertEqual(entry.code, "A")
        entry.step([])
        self.assertIsNone(entry.step(["UP"]))
        self.assertEqual(entry.code, "")
        for letter in ("A", "B", "C", "A"):
            entry.step([])
            self.assertIsNone(entry.step([letter]))
        entry.step([])
        self.assertEqual(entry.step(["B"]), "ABCAB")

    def test_held_letter_does_not_repeat(self):
        entry = CodeEntry()
        entry.ready = True
        entry.step(["A"])
        self.assertEqual(entry.step(["A"]), None)
        self.assertEqual(entry.code, "A")


class SessionTests(unittest.TestCase):
    def test_round_trip(self):
        root = tempfile.mkdtemp()
        session = SessionFile(root)
        first = session.load(entropy)
        self.assertTrue(first["device_id"])
        self.assertIsNone(first["refresh_token"])
        again = session.load(entropy)
        self.assertEqual(again["device_id"], first["device_id"])
        paired = dict(first)
        paired.update(
            user_id="11111111-1111-4111-8111-111111111111",
            room_id="22222222-2222-4222-8222-222222222222",
            seat=1,
            refresh_token="refresh-token",
            access_token="access-token",
            expires_at=10,
            pending_move={"id": "33333333-3333-4333-8333-333333333333", "orientation": "h", "row": 0, "col": 1},
        )
        validate_session(paired)
        session.save(paired)
        self.assertEqual(session.load(entropy)["pending_move"]["col"], 1)

    def test_rejects_a_different_shape(self):
        with self.assertRaises(ValueError):
            validate_session({"version": 1})
        with self.assertRaises(ValueError):
            fresh_session("not-a-uuid")

    def test_uuid_version_bits(self):
        value = uuid4(entropy)
        self.assertEqual(value[14], "4")
        self.assertIn(value[19], "89ab")


if __name__ == "__main__":
    unittest.main()
