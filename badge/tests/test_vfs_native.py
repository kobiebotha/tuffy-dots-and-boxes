"""Run real SQLite/PowerSync through the actual production Python BadgeVFS.

The C shim is only an ABI/SQLite adapter. Storage, buffering and failures use
the production BadgeVFS methods and real temporary files, never a C copy.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / path)
                for path in ("lib", "app/dots_boxes")]
import dots_storage
from dots_client import Client
from dots_storage import Store
from game import Game
from native_fixture import LIBRARY, HostCore
from vfs_fixture import PythonVFSCore
import test_sync as protocol


class FailOnceFile:
    """Inject an actual short physical write or flush failure, then recover."""
    def __init__(self, stream, failure):
        self.stream, self.failure, self.failed = stream, failure, False

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def write(self, value):
        if self.failure == "write" and not self.failed:
            self.failed = True
            return self.stream.write(value[:max(1, len(value) // 2)])
        return self.stream.write(value)

    def flush(self):
        if self.failure == "flush" and not self.failed:
            self.failed = True
            raise OSError("injected physical database flush failure")
        return self.stream.flush()


@unittest.skipUnless(LIBRARY.exists(), "Pinned host PowerSync qualifier unavailable")
class ActualVFSCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dots-vfs-native-")
        self.native = PythonVFSCore()
        self.previous_vfs = self.native.default_vfs
        self.store = Store(native=self.native, root=self.temp.name + "/dots").open()

    def tearDown(self):
        self.store.close()
        self.assertEqual(self.native.default_vfs, self.previous_vfs)
        self.assertIsNone(self.native.last_bridge_error)
        self.temp.cleanup()

    # Reuse protocol-level scenarios without changing their expectations. Their
    # only changed dependency is the actual Python VFS beneath the real core.
    deliver = protocol.RealCoreTests.deliver
    small_checkpoint = protocol.RealCoreTests.small_checkpoint
    test_checkpoint_commit_failure_rolls_back_rows_and_cursor = protocol.RealCoreTests.test_whole_checkpoint_commit_failure_rolls_back_rows_and_cursor
    test_exact_uuid_room_and_full_checkpoint_acknowledgement = protocol.RealCoreTests.test_accepted_move_requires_exact_published_uuid_room_and_full_checkpoint
    test_rejected_move_target_waits_without_false_sql_crud = protocol.RealCoreTests.test_older_checkpoint_waits_for_rejected_move_target_without_sql_crud
    test_reopen_sync_updates_and_removes_publish_at_checkpoint = protocol.RealCoreTests.test_raw_tables_reopen_sync_updates_and_removes_publish_only_at_checkpoint
    test_account_switch_after_reopen_clears_raw_rows_and_keeps_local_rows = protocol.RealCoreTests.test_raw_account_switch_after_reopen_clears_both_tables_and_preserves_local_rows
    test_pending_uuid_blocks_cross_account_clear_after_reopen = protocol.RealCoreTests.test_pending_move_blocks_cross_account_clear_and_survives_same_account_reopen
    cached_room = protocol.RealCoreTests.cached_room
    install_legacy_cache = protocol.RealCoreTests.install_legacy_cache
    assert_legacy_migration = protocol.RealCoreTests.assert_legacy_migration
    test_schema_three_migration_preserves_identity_and_pending_uuid = protocol.RealCoreTests.test_version_three_migration_preserves_identity_pending_and_rebuilds_raw_cache

    def test_native_uuids_create_device_and_durable_move_then_reuse_after_reopen(self):
        session = protocol.SessionFile(self.store.root)
        client = Client(self.store, session)
        device_id = client.records.record["device_id"]
        self.assertEqual(client.mode, "pair")
        paired = dict(protocol.Records().record, device_id=device_id)
        client.records.save(paired)
        self.store.reconcile_session(paired)
        client.mode = "play"
        client.sync = protocol.make_sync(self.store, records=client.records)
        protocol.start(client.sync)
        room = {"id": protocol.ROOM, "status": "playing", "cols": 5, "rows": 4,
                "current_seat": 1, "score_p1": 0, "score_p2": 0,
                "winner": None, "move_count": 0}
        self.deliver(client.sync, protocol.checkpoint_records("initial", [
            protocol.operation(1, "rooms", room)]), client)
        self.assertTrue(client.controller.can_claim)
        self.assertTrue(client._queue_move(("h", 0, 0)))
        pending = client.records.record["pending_move"]
        for value in (device_id, pending["id"]):
            self.assertRegex(value, "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
        self.assertNotEqual(device_id, pending["id"])
        self.assertEqual(session.load(self.store.new_uuid)["pending_move"], pending)
        client.close()
        self.store.close()
        self.store.open()
        with patch.object(self.store, "new_uuid", side_effect=AssertionError("saved IDs must be reused")):
            restored = Client(self.store, session)
            restored.sync = protocol.make_sync(self.store, records=restored.records,
                                               replies=[{"ok": True, "duplicate": True}])
            try:
                with patch.object(restored.sync.connector, "_post", wraps=restored.sync.connector._post) as upload:
                    protocol.start(restored.sync)
                self.assertEqual(upload.call_count, 1)
                self.assertEqual(upload.call_args.args[1]["move_id"], pending["id"])
                self.assertEqual(restored.records.record["device_id"], device_id)
                self.assertEqual(restored.records.record["pending_move"], pending)
                self.assertFalse(self.store.has_local_writes())
            finally:
                restored.close()

    def test_full49_live_reopen_and_no_sql_crud(self):
        game = Game()
        self.store.bind_account(protocol.USER)
        sync = protocol.make_sync(self.store)
        protocol.start(sync)
        checksum = 0
        expected = []
        for edge in range(49):
            orientation, row, col = protocol.decode_edge(edge)
            seat = game.current_player
            game.claim(orientation, row, col)
            move_id = "%08x-1234-4123-8123-123456789abc" % (edge + 1)
            move = protocol.operation(edge * 2 + 1, body={"id": move_id, "room_id": protocol.ROOM,
                "seat": seat, "orientation": orientation, "row": row, "col": col, "move_index": edge + 1})
            body = {"id": protocol.ROOM, "status": "finished" if game.finished else "playing",
                "cols": 5, "rows": 4, "current_seat": game.current_player,
                "score_p1": game.scores[0], "score_p2": game.scores[1], "winner": game.winner,
                "move_count": game.moves}
            room = protocol.operation(edge * 2 + 2, "rooms", body)
            checksum = (checksum + move["checksum"] + room["checksum"]) & 0xffffffff
            records = [{"checkpoint": {"last_op_id": room["op_id"], "buckets": [{
                "bucket": "shadow-game", "checksum": checksum, "priority": 3, "count": (edge + 1) * 2}]}},
                {"data": {"bucket": "shadow-game", "data": [move, room]}}, {"checkpoint_complete": {}}]
            self.deliver(sync, records)
            self.assertTrue(sync.synced, sync.last_error)
            self.assertTrue(self.store.has_move(move_id, protocol.ROOM))
            expected.append({"orientation": orientation, "row": row, "col": col, "move_index": edge + 1})
            self.assertEqual(self.store.moves(protocol.ROOM), expected)
        self.assertEqual(sum(game.scores), 20)
        before_room = self.store.room(protocol.ROOM)
        self.assertEqual(before_room["move_count"], 49)
        self.assertEqual(before_room["status"], "finished")
        self.assertFalse(self.store.has_local_writes())
        self.assertEqual(self.native.exec("PRAGMA quick_check"), [["ok"]])
        sync.close()
        self.store.close()
        self.assertEqual(self.native.default_vfs, self.previous_vfs)
        self.store.open()
        self.assertEqual(self.store.room(protocol.ROOM), before_room)
        self.assertEqual(self.store.moves(protocol.ROOM), expected)
        self.assertEqual(self.store.moves(protocol.ROOM, after=47), expected[47:])
        self.assertEqual(self.store.moves(protocol.ROOM, after=49), [])
        self.assertFalse(self.store.has_local_writes())
        self.assertFalse(self.store.bind_account(protocol.USER))

    def install_schema_four_tables(self):
        # Seed the previous on-disk layout before exercising the production
        # migrator. Both cached rows and internal native cursors must survive.
        self.store._exec("DROP TABLE rooms; DROP TABLE moves")
        for statement in dots_storage.RAW_TABLE_SQL:
            self.store._exec(statement.replace(" WITHOUT ROWID", ""))
        self.store._exec("PRAGMA user_version=4")

    def install_schema_four_cache(self):
        self.install_schema_four_tables()
        room = self.cached_room()
        session = protocol.SessionFile(self.store.root)
        pending = dict(protocol.Records().record, pending_move={"id": protocol.DEVICE,
                      "orientation": "h", "row": 0, "col": 0})
        session.save(protocol.Records().record)
        session.save(pending)
        self.store.close()
        return room, pending

    def test_full49_schema_four_relayout_preserves_all_moves_with_fixed_arena(self):
        self.install_schema_four_tables()
        self.test_full49_live_reopen_and_no_sql_crud()
        self.assertEqual(self.native.exec("PRAGMA user_version"), [["5"]])
        for sql, in self.native.exec("SELECT sql FROM sqlite_master WHERE name IN ('rooms','moves')"):
            self.assertIn("WITHOUT ROWID", sql)

    def test_schema_four_relayout_retains_published_cache_identity_checkpoint_and_pending_overlay(self):
        room, pending = self.install_schema_four_cache()
        original_size = Path(self.store.root, "state.sqlite").stat().st_size
        self.native.open(self.store.root + "/state.sqlite")
        native_state = {table: self.native.exec("SELECT * FROM " + table)
                        for table in ("ps_buckets", "ps_oplog", "ps_kv", "account")}
        self.native.close()
        self.store.open()
        self.assertEqual(self.native.exec("PRAGMA user_version"), [["5"]])
        for sql, in self.native.exec("SELECT sql FROM sqlite_master WHERE name IN ('rooms','moves')"):
            self.assertIn("WITHOUT ROWID", sql)
        self.assertEqual(self.store.room(protocol.ROOM), room)
        for table, expected in native_state.items():
            self.assertEqual(self.native.exec("SELECT * FROM " + table), expected)
        self.assertEqual(protocol.SessionFile(self.store.root).load(None), pending)
        self.assertGreaterEqual(self.store.io.db_size, original_size)
        self.assertFalse(self.store.has_local_writes())

    def test_schema_four_relayout_commit_failure_rolls_back_layout_and_cached_game(self):
        room, pending = self.install_schema_four_cache()
        original = self.store._exec

        def interrupted(sql):
            if sql == "COMMIT":
                raise OSError("interrupted relayout commit")
            return original(sql)

        with patch.object(self.store, "_exec", side_effect=interrupted), self.assertRaises(OSError):
            self.store.open()
        self.native.open(self.store.root + "/state.sqlite")
        try:
            self.assertEqual(self.native.exec("PRAGMA user_version"), [["4"]])
            self.assertEqual(self.native.exec("SELECT name FROM sqlite_master WHERE name LIKE '%_next'"), [])
            for sql, in self.native.exec("SELECT sql FROM sqlite_master WHERE name IN ('rooms','moves')"):
                self.assertNotIn("WITHOUT ROWID", sql)
        finally:
            self.native.close()
        self.store.open()
        self.assertEqual(self.store.room(protocol.ROOM), room)
        self.assertEqual(protocol.SessionFile(self.store.root).load(None), pending)

    def test_short_physical_database_write_rolls_back_checkpoint(self):
        self._physical_fault("write")

    def test_physical_database_flush_failure_rolls_back_checkpoint(self):
        self._physical_fault("flush")

    def _physical_fault(self, failure):
        sync = protocol.make_sync(self.store)
        protocol.start(sync)
        handle = self.store.io.db_handle
        stream, path, flags = self.store.io.files[handle]
        injected = FailOnceFile(stream, failure)
        self.store.io.files[handle] = (injected, path, flags)
        records = self.small_checkpoint() + [{"checkpoint_complete": {}}]
        with self.assertRaises(RuntimeError):
            self.store.control_lines([json.dumps(record) for record in records])
        self.assertTrue(injected.failed)
        self.assertFalse(sync.synced)
        self.assertIsNone(self.store.room(protocol.ROOM))
        self.assertEqual(self.native.exec("SELECT count(*) FROM ps_oplog"), [["0"]])
        self.assertEqual(self.native.exec("SELECT count(*) FROM ps_buckets"), [["0"]])
        self.assertFalse(self.store.has_local_writes())
        sync.close()
        self.store.close()
        self.store.open()
        self.assertIsNone(self.store.room(protocol.ROOM))
        self.assertEqual(self.native.exec("PRAGMA quick_check"), [["ok"]])

    def test_power_loss_snapshot_retains_journal_and_pending_uuid(self):
        sync = protocol.make_sync(self.store)
        protocol.start(sync)
        session = protocol.SessionFile(self.store.root)
        pending = dict(protocol.Records().record, pending_move={
            "id": protocol.DEVICE, "orientation": "h", "row": 0, "col": 0})
        session.save(pending)
        root = Path(self.temp.name) / "interrupted"
        root.mkdir()
        original = self.native.io
        captured = False

        def before_database_sync(op, item, arg1, arg2):
            nonlocal captured
            if op == 5 and item == self.store.io.db_handle and not captured:
                # The real SQLite journal is already FULL-synced. Capture the
                # durable files at this boundary, before the database xSync.
                captured = True
                for name in Path(self.store.root).iterdir():
                    if name.is_file():
                        (root / name.name).write_bytes(name.read_bytes())
                return (10, 0)
            return original(op, item, arg1, arg2)

        self.native.io = before_database_sync
        try:
            records = self.small_checkpoint() + [{"checkpoint_complete": {}}]
            with self.assertRaises(RuntimeError):
                self.store.control_lines([json.dumps(record) for record in records])
        finally:
            self.native.io = original
        self.assertTrue(captured)
        self.assertGreater((root / "state.sqlite-journal").stat().st_size, 0)
        before = {name.name: name.read_bytes() for name in root.iterdir()}
        sync.close()
        self.store.close()
        interrupted = Store(native=PythonVFSCore(), root=str(root))
        with self.assertRaisesRegex(ValueError, "Interrupted write"):
            interrupted.open()
        self.assertEqual({name.name: name.read_bytes() for name in root.iterdir()}, before)
        saved = protocol.SessionFile(str(root)).load(self.store.new_uuid)
        self.assertEqual(saved["pending_move"], pending["pending_move"])

    def test_time_entropy_and_default_vfs_restoration(self):
        self.assertEqual(self.native.exec("SELECT strftime('%Y','now')"), [[str(__import__("time").gmtime()[0])]])
        random_hex = self.native.exec("SELECT lower(hex(randomblob(16)))")[0][0]
        self.assertRegex(random_hex, "^[0-9a-f]{32}$")
        self.assertEqual(len(self.native.probe_randomness(32)), 32)
        self.assertGreater(self.native.stats["time"], 0)
        self.assertGreater(self.native.stats["random"], 0)
        self.assertGreater(self.native.stats["db.read"], 0)
        self.assertGreater(self.native.stats["journal.write"], 0)
        self.store.close()
        self.assertEqual(self.native.default_vfs, self.previous_vfs)
        # The original POSIX fixture remains usable after restoring the default.
        host = HostCore()
        host.configure(None)
        host.open(str(Path(self.temp.name) / "ordinary.sqlite"))
        try:
            self.assertEqual(host.exec("SELECT 1"), [["1"]])
        finally:
            host.close()

    def test_callback_exceptions_return_sqlite_error_without_crossing_frames(self):
        original = self.native.io

        def fail_clock(op, item, arg1, arg2):
            if op == 10:
                raise OSError("injected Python clock exception")
            return original(op, item, arg1, arg2)

        self.native.io = fail_clock
        try:
            # SQLite date functions return NULL when the VFS clock reports IOERR.
            self.assertEqual(self.native.exec("SELECT datetime('now')"), [[None]])
            self.assertIn("injected Python clock", self.native.last_bridge_error)
        finally:
            self.native.io = original
            self.native.last_bridge_error = None


if __name__ == "__main__":
    unittest.main()
