"""Compact wire values preserve canonical UUIDs and every fixed-board edge."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app/dots_boxes"))
from dots_codec import decode_edge, decode_status, decode_uuid, encode_edge, encode_uuid


class CodecTests(unittest.TestCase):
    def test_known_uuid_and_all_byte_values_round_trip(self):
        uuid = "550e8400-e29b-41d4-a716-446655440000"
        self.assertEqual(encode_uuid(uuid), "VQ6EAOKbQdSnFkRmVUQAAA==")
        self.assertEqual(decode_uuid("VQ6EAOKbQdSnFkRmVUQAAA=="), uuid)
        self.assertEqual(encode_uuid(uuid.upper()), encode_uuid(uuid))
        for byte in range(256):
            text = ("%02x" % byte) * 16
            canonical = text[:8] + "-" + text[8:12] + "-" + text[12:16] + "-" + text[16:20] + "-" + text[20:]
            self.assertEqual(decode_uuid(encode_uuid(canonical)), canonical)

    def test_rejects_invalid_or_noncanonical_compact_ids(self):
        for value in (None, 1, "", "bad", "VQ6EAOKbQdSnFkRmVUQAAA", "VQ6EAOKbQdSnFkRmVUQAAA==\n",
                      "VQ6EAOKbQdSnFkRmVUQAAB==", "_Q6EAOKbQdSnFkRmVUQAAA=="):
            with self.subTest(value=value), self.assertRaises(ValueError):
                decode_uuid(value)
        with self.assertRaises(ValueError):
            encode_uuid("not-a-uuid")

    def test_all_49_edges_round_trip_without_overlap(self):
        expected = ([('h', row, col) for row in range(5) for col in range(5)]
                    + [('v', row, col) for row in range(4) for col in range(6)])
        self.assertEqual([encode_edge(*edge) for edge in expected], list(range(49)))
        self.assertEqual([decode_edge(index) for index in range(49)], expected)
        for value in (-1, 49, True, None, "1", 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                decode_edge(value)
        for edge in (("h", 5, 0), ("h", 0, 5), ("v", 4, 0), ("v", 0, 6), ("x", 0, 0), ("h", True, 0)):
            with self.subTest(edge=edge), self.assertRaises(ValueError):
                encode_edge(*edge)

    def test_status_codes_include_cancelled_and_reject_unknown_values(self):
        self.assertEqual([decode_status(index) for index in range(4)], ["waiting", "playing", "finished", "cancelled"])
        for value in (-1, 4, None, True, "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                decode_status(value)


if __name__ == "__main__":
    unittest.main()
