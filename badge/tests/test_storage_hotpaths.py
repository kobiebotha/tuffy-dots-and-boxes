"""Guard equivalence and bounded incremental reads on the storage hot path."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / path)
                for path in ("lib", "app/dots_boxes")]
import dots_storage
from dots_storage import Store

ROOM = "11111111-1111-4111-8111-111111111111"


class StorageHotpathTests(unittest.TestCase):
    def test_decoded_records_keep_all_guards_and_checkpoint_flags_without_reparse(self):
        store = Store()
        parsed = [{"sync_stream_started": {}}, {"checkpoint_complete": {}},
                  {"partial_checkpoint_complete": {"priority": 3}}]
        payloads = [json.dumps(record) for record in parsed]
        with patch.object(store, "_sync_line", side_effect=AssertionError("record decoded already")), \
                patch.object(store, "_check_reserve"), patch.object(store, "_used_pages", return_value=0), \
                patch.object(store, "_apply_control", return_value=[]), patch.object(store, "_exec") as sql:
            events = store.control_lines(payloads, parsed=parsed)
        self.assertEqual([event["full_checkpoint"] for event in events], [False, True, False])
        self.assertEqual([call.args[0] for call in sql.call_args_list], ["BEGIN IMMEDIATE", "COMMIT"])

    def test_decoded_records_reject_invalid_shape_table_bucket_bytes_and_operation_budget(self):
        invalid = [([{}], []), ([{}], [None]), ([{}], [{"a": 0, "b": 0}]),
                   ([{}], [{"data": {"data": [{"op": "PUT", "object_type": "unexpected"}]}}]),
                   ([{}], [{"checkpoint": {"buckets": [{}] * 33}}]),
                   ([{}], [{"data": {"data": [{}] * 7}}]),
                   ([{"LogLine": "é" * 5000}], [{"LogLine": "é" * 5000}])]
        for raw, parsed in invalid:
            with self.subTest(parsed=str(parsed)[:80]), patch.object(Store, "_exec") as sql:
                payloads = [json.dumps(record, ensure_ascii=False) for record in raw]
                with self.assertRaises(ValueError):
                    Store().control_lines(payloads, parsed=parsed)
                sql.assert_not_called()

    def test_one_native_boundary_reads_both_page_guards(self):
        store = Store()
        with patch.object(store, "_exec", return_value=[["77"], ["11"]]) as sql:
            self.assertEqual(store._used_pages(), 66)
        sql.assert_called_once_with("PRAGMA page_count; PRAGMA freelist_count")
        with patch.object(store, "_exec", return_value=[["77"]]), self.assertRaises(ValueError):
            store._used_pages()

    def test_growth_guard_still_runs_between_records_and_rolls_back(self):
        store = Store()
        with patch.object(store, "_check_reserve"), \
                patch.object(store, "_used_pages", side_effect=[96, 97]) as pages, \
                patch.object(store, "_apply_control", return_value=[]) as apply, \
                patch.object(store, "_exec") as sql:
            with self.assertRaisesRegex(ValueError, "budget"):
                store.control_lines(['{"sync_stream_started":{}}', '{"checkpoint_complete":{}}'])
        self.assertEqual(pages.call_count, 2)
        self.assertEqual(apply.call_count, 1)
        self.assertEqual([call.args[0] for call in sql.call_args_list], ["BEGIN IMMEDIATE", "ROLLBACK"])

    def test_move_reads_use_bounded_keyset_pages_and_incremental_cursor(self):
        store = Store()
        rows = [[str(index - 1), str(index)] for index in range(1, 50)]
        with patch.object(store, "_exec", side_effect=[rows[:16], rows[16:32], rows[32:48], rows[48:]]) as sql:
            moves = store.moves(ROOM)
        self.assertEqual([move["move_index"] for move in moves], list(range(1, 50)))
        self.assertEqual(len(sql.call_args_list), 4)
        for call, cursor in zip(sql.call_args_list, (0, 16, 32, 48)):
            self.assertIn("AND n>%d ORDER BY n LIMIT 16" % cursor, call.args[0])
            self.assertNotIn("OFFSET", call.args[0])
        with patch.object(store, "_exec", return_value=rows[47:]) as sql:
            self.assertEqual(store.moves(ROOM, after=47), moves[47:])
        self.assertEqual(sql.call_count, 1)
        for cursor in (-1, 65, True, 1.0, None):
            with self.subTest(cursor=cursor), self.assertRaises(ValueError), patch.object(store, "_exec") as sql:
                store.moves(ROOM, after=cursor)
            sql.assert_not_called()

    def test_invalid_room_and_nonadvancing_move_order_are_rejected(self):
        store = Store()
        with self.assertRaises(ValueError):
            store.moves(None)
        with patch.object(store, "_exec", return_value=[["0", "1"], ["1", "1"]]), \
                self.assertRaisesRegex(ValueError, "order"):
            store.moves(ROOM)


if __name__ == "__main__":
    unittest.main()
