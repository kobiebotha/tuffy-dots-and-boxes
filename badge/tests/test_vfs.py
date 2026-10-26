"""PowerSync storage admission policy with the standalone shadow VFS."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / path) for path in
                ("sqlite-shadow-vfs", "badge/lib", "badge/app/dots_boxes")]
import powersync.database as database
from powersync.database import DATABASE, MAX_DATABASE_BYTES, MIN_FREE_BYTES
from sqlite_shadow_vfs import ShadowVFS
from dots_storage import Store


class StorageAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / DATABASE
        self.path.write_bytes(b"original")
        self.vfs = ShadowVFS(str(self.path))
        # An open VFS exposes its logical snapshot size to the SDK guard.
        self.vfs.db_handle = 1
        self.vfs.db_size = 8

    def test_low_space_reserves_logical_snapshot_before_any_control_transaction(self):
        self.vfs.db_size = 20000
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
