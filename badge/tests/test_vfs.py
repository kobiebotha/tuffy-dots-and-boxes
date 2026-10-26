"""Database shadow boundaries, durable barriers, and retained I/O failures."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / path)
                for path in ("lib", "app/dots_boxes")]
import powersync.database as database
import powersync.vfs as vfs
from dots_storage import Store
from powersync.vfs import BadgeVFS, DATABASE, MAX_DATABASE_BYTES, MIN_FREE_BYTES


class ProbeFile:
    def __init__(self, stream, name, events):
        self.stream, self.name, self.events = stream, name, events
        self.short_write = False
        self.write_error = False
        self.flush_error = False
        self.close_error = False
        self.short_read = False
        self.metadata_dirty = True

    def seek(self, *args):
        self.events.append((self.name, "seek", args))
        return self.stream.seek(*args)

    def tell(self):
        return self.stream.tell()

    def read(self, amount):
        self.events.append((self.name, "read", amount))
        return self.stream.read(amount)

    def readinto(self, target):
        self.events.append((self.name, "readinto", len(target)))
        if self.short_read:
            return self.stream.readinto(target[:-1])
        return self.stream.readinto(target)

    def write(self, data):
        self.events.append((self.name, "write", len(data)))
        if self.write_error:
            self.stream.write(data[:max(1, len(data) // 2)])
            self.stream.flush()
            raise OSError("uncertain physical write")
        if self.short_write:
            return self.stream.write(data[:len(data) // 2])
        return self.stream.write(data)

    def flush(self):
        self.events.append((self.name, "flush"))
        if self.flush_error:
            raise OSError("simulated flush failure")
        result = self.stream.flush()
        self.metadata_dirty = False
        return result

    def close(self):
        self.events.append((self.name, "close"))
        if self.close_error:
            raise OSError("simulated close failure")
        return self.stream.close()


class ShadowVFSTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / DATABASE
        self.journal = self.root / (DATABASE + "-journal")
        self.path.write_bytes(b"original")
        self.events = []
        self.probes = {}
        real_open = open
        self.next_short_read = False
        self.next_flush_error = False

        def open_probe(path, mode):
            name = "journal" if str(path).endswith("-journal") else "db"
            probe = ProbeFile(real_open(path, mode), name, self.events)
            probe.short_read = self.next_short_read
            probe.flush_error = self.next_flush_error
            probe.metadata_dirty = mode != "rb"
            self.probes[name] = probe
            return probe
        self.open_patch = patch.object(vfs, "open", side_effect=open_probe, create=True)
        self.sync_patch = patch.object(vfs.os, "sync", side_effect=lambda: self.events.append(("os", "sync")))
        self.open_patch.start()
        self.sync_mock = self.sync_patch.start()
        self.vfs = BadgeVFS(root=str(self.root))

    def tearDown(self):
        for probe in self.probes.values():
            probe.short_write = probe.write_error = probe.flush_error = probe.close_error = False
        self.sync_mock.side_effect = lambda: None
        self.vfs.close_all()
        self.sync_patch.stop()
        self.open_patch.stop()
        self.temp.cleanup()

    def db(self, flags=2):
        code, handle = self.vfs(0, str(self.path), flags, 0)
        self.assertEqual(code, 0, self.vfs.last_error)
        return handle

    def physical_journal(self):
        code, handle = self.vfs(0, str(self.journal), 6, 0)
        self.assertEqual(code, 0)
        self.assertEqual(self.vfs(3, handle, 0, b"durable rollback journal"), (0, 24))
        self.assertEqual(self.vfs(5, handle, 0, 0), (0, 0))
        return handle

    def test_random_database_io_is_memory_only_until_one_sequential_full_sync(self):
        handle = self.db()
        self.events.clear()
        self.assertEqual(self.vfs(3, handle, 2, b"ABC"), (0, 3))
        self.assertEqual(self.vfs(3, handle, 7, b"Z"), (0, 1))
        self.assertEqual(self.vfs(2, handle, 0, 32), (0, b"orABCnaZ"))
        self.assertEqual(self.vfs(6, handle, 0, 0), (0, 8))
        self.assertEqual(self.events, [])
        self.assertEqual(self.path.read_bytes(), b"original")
        self.assertTrue(self.vfs.db_dirty)
        self.assertEqual(len(self.vfs.db_buffer), MAX_DATABASE_BYTES)
        self.assertEqual(self.vfs(5, handle, 0, 0), (0, 0))
        self.assertEqual(self.path.read_bytes(), b"orABCnaZ")
        self.assertEqual(self.events, [("db", "seek", (0,)), ("db", "write", 8),
                                       ("db", "flush"), ("os", "sync")])
        self.assertFalse(self.vfs.db_dirty)
        self.events.clear()
        self.assertEqual(self.vfs(5, handle, 0, 0), (0, 0))
        self.assertEqual(self.events, [("db", "flush"), ("os", "sync")])

    def test_journal_is_physical_and_its_barrier_precedes_database_persistence(self):
        handle = self.db()
        journal = self.physical_journal()
        self.assertEqual(self.journal.read_bytes(), b"durable rollback journal")
        journal_flush = self.events.index(("journal", "flush"))
        self.assertEqual(self.vfs(3, handle, 0, b"changed!"), (0, 8))
        self.assertEqual(self.path.read_bytes(), b"original")
        self.assertEqual(self.vfs(5, handle, 0, 0), (0, 0))
        self.assertLess(journal_flush, self.events.index(("db", "write", 8)))
        self.assertTrue(self.journal.exists())
        self.assertEqual(self.vfs(1, journal, 0, 0), (0, 0))
        self.assertEqual(self.vfs(7, str(self.journal), 1, 0), (0, 0))
        self.assertFalse(self.journal.exists())

    def test_load_uses_one_bounded_readinto_and_reopen_discards_old_shadow(self):
        handle = self.db()
        self.assertEqual(self.events[0], ("db", "readinto", 8))
        self.assertEqual(self.vfs(1, handle, 0, 0), (0, 0))
        self.path.write_bytes(b"replacement")
        handle = self.db()
        self.assertEqual(self.vfs(2, handle, 0, 20), (0, b"replacement"))

    def test_database_read_returns_bounded_buffer_without_copying_page_bytes(self):
        handle = self.db()
        code, data = self.vfs(2, handle, 2, 4)
        self.assertEqual(code, 0)
        self.assertIsInstance(data, memoryview)
        self.assertEqual(bytes(data), b"igin")
        self.assertIs(data.obj, self.vfs.db_buffer)
        self.assertFalse(self.vfs.db_dirty)

    def test_readwrite_open_flushes_only_metadata_before_snapshot_space_check(self):
        handle = self.db()
        self.assertEqual(self.events, [("db", "readinto", 8), ("db", "flush")])
        self.assertFalse(self.probes["db"].metadata_dirty)
        self.assertEqual(self.path.read_bytes(), b"original")
        self.assertFalse(self.vfs.db_dirty)
        store = Store(root=str(self.root))
        store.io = self.vfs
        def lfs_free(_root):
            # Pinned LittleFS statvfs double-counts an open metadata-dirty CTZ.
            duplicate = 8 if self.probes["db"].metadata_dirty else 0
            return MIN_FREE_BYTES + 8 - duplicate
        with patch.object(database, "_free_bytes", side_effect=lfs_free):
            store._check_reserve()
            self.probes["db"].metadata_dirty = True
            with self.assertRaisesRegex(OSError, "snapshot"):
                store._check_reserve()
        self.assertEqual(self.vfs(2, handle, 0, 8), (0, b"original"))

    def test_empty_created_database_flushes_metadata_but_readonly_open_never_flushes(self):
        self.path.unlink()
        handle = self.db(flags=6)
        self.assertEqual(self.events, [("db", "flush")])
        self.assertEqual(self.vfs.db_size, 0)
        self.assertEqual(self.path.read_bytes(), b"")
        self.assertEqual(self.vfs(1, handle, 0, 0), (0, 0))
        self.path.write_bytes(b"original")
        self.events.clear()
        handle = self.db(flags=1)
        self.assertEqual(self.events, [("db", "readinto", 8)])
        self.assertEqual(self.vfs(2, handle, 0, 8), (0, b"original"))
        self.assertFalse(self.vfs.db_dirty)

    def test_open_metadata_flush_failure_closes_file_without_adopting_shadow(self):
        self.next_flush_error = True
        self.assertEqual(self.vfs(0, str(self.path), 2, 0)[0], 14)
        self.assertEqual(self.vfs.files, {})
        self.assertIsNone(self.vfs.db_handle)
        self.assertEqual(self.vfs.db_size, 0)
        self.assertFalse(self.vfs.db_dirty)
        self.assertTrue(self.probes["db"].stream.closed)
        self.assertEqual(self.path.read_bytes(), b"original")
        self.next_flush_error = False
        self.assertEqual(self.vfs(2, self.db(), 0, 8), (0, b"original"))

    def test_gap_and_growth_zero_stale_buffer_bytes_without_early_file_writes(self):
        handle = self.db()
        self.vfs.db_buffer[8:20] = b"stale bytes!"
        self.events.clear()
        self.assertEqual(self.vfs(3, handle, 12, b"end"), (0, 3))
        self.assertEqual(self.vfs(4, handle, 20, 0), (0, 0))
        expected = b"original" + bytes(4) + b"end" + bytes(5)
        self.assertEqual(self.vfs(2, handle, 0, 40), (0, expected))
        self.assertEqual(self.vfs(6, handle, 0, 0), (0, 20))
        self.assertEqual(self.path.read_bytes(), b"original")
        self.assertEqual(self.events, [])
        self.assertEqual(self.vfs(5, handle, 0, 0), (0, 0))
        self.assertEqual(self.path.read_bytes(), expected)

    def test_read_past_eof_is_short_for_the_native_wrapper_to_zero_pad(self):
        handle = self.db()
        self.assertEqual(self.vfs(2, handle, 5, 8), (0, b"nal"))
        self.assertEqual(self.vfs(2, handle, 1000000, 512), (0, b""))
        self.assertEqual(self.vfs(2, handle, -1, 1)[0], 10)
        self.assertEqual(self.vfs(2, handle, 0, -1)[0], 10)
        self.assertEqual(self.vfs(2, handle, 0, MAX_DATABASE_BYTES + 1)[0], 10)

    def test_database_limits_and_unsupported_shrink_do_not_change_shadow(self):
        handle = self.db()
        before = bytes(self.vfs.db_buffer)
        for offset, data in ((-1, b"x"), (MAX_DATABASE_BYTES, b"x")):
            self.assertEqual(self.vfs(3, handle, offset, data)[0], 13)
        self.assertEqual(self.vfs(4, handle, MAX_DATABASE_BYTES + 1, 0)[0], 13)
        self.assertEqual(self.vfs(4, handle, 7, 0)[0], 1546)
        self.assertEqual(bytes(self.vfs.db_buffer), before)
        self.assertEqual(self.vfs.db_size, 8)
        self.assertFalse(self.vfs.db_dirty)
        self.assertEqual(self.vfs(3, handle, MAX_DATABASE_BYTES - 1, b"x"), (0, 1))
        self.assertEqual(self.vfs.db_size, MAX_DATABASE_BYTES)
        self.assertEqual(len(self.vfs.db_buffer), MAX_DATABASE_BYTES)
        self.assertEqual(self.vfs(2, handle, 8, 3), (0, bytes(3)))
        self.assertEqual(self.vfs(5, handle, 0, 0), (0, 0))
        self.assertEqual(self.path.stat().st_size, MAX_DATABASE_BYTES)

    def test_readonly_database_cannot_write_or_grow(self):
        handle = self.db(flags=1)
        self.assertEqual(self.vfs(3, handle, 0, b"x")[0], 10)
        self.assertEqual(self.vfs(4, handle, 9, 0)[0], 10)
        self.assertEqual(self.vfs(2, handle, 0, 20), (0, b"original"))
        self.assertFalse(self.vfs.db_dirty)

    def test_oversized_or_short_initial_read_fails_closed_without_adopting_handle(self):
        for size in (MAX_DATABASE_BYTES + 1, 8):
            with self.subTest(size=size):
                self.path.write_bytes(b"x" * size)
                self.next_short_read = size == 8
                self.assertEqual(self.vfs(0, str(self.path), 2, 0)[0], 14)
                self.assertEqual(self.vfs.files, {})
                self.assertTrue(self.probes["db"].stream.closed)
                self.assertEqual(self.path.stat().st_size, size)

    def test_short_write_retains_dirty_image_and_journal_until_successful_retry(self):
        handle = self.db()
        self.physical_journal()
        expected = b"new data"
        self.assertEqual(self.vfs(3, handle, 0, expected), (0, 8))
        self.probes["db"].short_write = True
        self.assertEqual(self.vfs(5, handle, 0, 0)[0], 10)
        self.assertTrue(self.vfs.db_dirty)
        self.assertTrue(self.journal.exists())
        self.assertEqual(self.vfs(2, handle, 0, 8), (0, expected))
        self.probes["db"].short_write = False
        self.assertEqual(self.vfs(5, handle, 0, 0), (0, 0))
        self.assertFalse(self.vfs.db_dirty)
        self.assertEqual(self.path.read_bytes(), expected)
        self.assertTrue(self.journal.exists())

    def test_uncertain_write_flush_and_global_sync_errors_retain_dirty_state(self):
        handle = self.db()
        self.physical_journal()
        for fault in ("write", "flush", "sync"):
            with self.subTest(fault=fault):
                self.assertEqual(self.vfs(3, handle, 0, b"new data"), (0, 8))
                self.probes["db"].write_error = fault == "write"
                self.probes["db"].flush_error = fault == "flush"
                self.sync_mock.side_effect = OSError("sync failed") if fault == "sync" else lambda: None
                self.assertEqual(self.vfs(5, handle, 0, 0)[0], 10)
                self.assertTrue(self.vfs.db_dirty)
                self.assertIn(handle, self.vfs.files)
                self.assertTrue(self.journal.exists())
                self.probes["db"].write_error = self.probes["db"].flush_error = False
                self.sync_mock.side_effect = lambda: None
                self.assertEqual(self.vfs(5, handle, 0, 0), (0, 0))
                self.assertFalse(self.vfs.db_dirty)
                self.assertEqual(self.path.read_bytes(), b"new data")

    def test_close_flushes_dirty_data_and_retains_handle_if_sync_or_close_fails(self):
        handle = self.db()
        self.assertEqual(self.vfs(3, handle, 0, b"new data"), (0, 8))
        self.probes["db"].flush_error = True
        self.assertEqual(self.vfs(1, handle, 0, 0)[0], 10)
        self.assertTrue(self.vfs.db_dirty)
        self.assertIn(handle, self.vfs.files)
        self.assertFalse(self.probes["db"].stream.closed)
        with self.assertRaises(OSError):
            self.vfs.close_all()
        self.probes["db"].flush_error = False
        self.probes["db"].close_error = True
        self.assertEqual(self.vfs(1, handle, 0, 0)[0], 10)
        self.assertFalse(self.vfs.db_dirty)
        self.assertIn(handle, self.vfs.files)
        self.probes["db"].close_error = False
        self.vfs.close_all()
        self.assertEqual(self.vfs.files, {})
        self.assertEqual(self.path.read_bytes(), b"new data")

    def test_low_space_reserves_logical_snapshot_before_any_control_transaction(self):
        handle = self.db()
        self.assertEqual(self.vfs(4, handle, 20000, 0), (0, 0))
        self.assertEqual(self.path.stat().st_size, 8)
        store = Store(root=str(self.root))
        store.io = self.vfs
        # Use the growing shadow's logical size, not the older physical file.
        with patch.object(database, "_free_bytes", return_value=MIN_FREE_BYTES + 19999), \
                patch.object(store, "_exec") as sql, patch.object(store, "_used_pages", return_value=50):
            for action in (lambda: store.control("start", {}),
                           lambda: store.control_group([("start", {})]),
                           lambda: store.control_lines(['{"checkpoint_complete":{}}'])):
                with self.subTest(action=action), self.assertRaisesRegex(OSError, "snapshot"):
                    action()
            sql.assert_not_called()
        with patch.object(database, "_free_bytes", return_value=MIN_FREE_BYTES + 20000):
            store._check_reserve()

    def test_unopened_store_reserves_snapshot_from_existing_file_stat(self):
        store = Store(root=str(self.root))
        with patch.object(database, "_free_bytes", return_value=MIN_FREE_BYTES + 7):
            with self.assertRaisesRegex(OSError, "snapshot"):
                store._check_reserve()
        with patch.object(database, "_free_bytes", return_value=MIN_FREE_BYTES + 8):
            store._check_reserve()

    def test_new_directory_still_requires_full_database_cap_plus_reserve(self):
        destination = self.root / "new_game"
        store = Store(root=str(destination))
        with patch.object(database, "_free_bytes", return_value=MAX_DATABASE_BYTES + MIN_FREE_BYTES - 1):
            with self.assertRaisesRegex(OSError, "Not enough free"):
                store._owned_directory()
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
