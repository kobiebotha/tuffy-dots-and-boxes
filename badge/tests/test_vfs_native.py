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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app/dots_boxes"))
import dots_storage
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
        self.assertFalse(self.store.has_local_writes())
        self.assertFalse(self.store.bind_account(protocol.USER))

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
        saved = protocol.SessionFile(str(root)).load(lambda count: b"x" * count)
        self.assertEqual(saved["pending_move"], pending["pending_move"])

    def test_time_entropy_and_default_vfs_restoration(self):
        self.assertEqual(self.native.exec("SELECT strftime('%Y','now')"), [[str(dots_storage.time.gmtime()[0])]])
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
