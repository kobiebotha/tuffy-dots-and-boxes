"""Copy-only imports and real SQLite-only integration without the game SDK."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

COMPONENT = Path(__file__).resolve().parents[1]
REPO = COMPONENT.parent
SUFFIX = ".dylib" if sys.platform == "darwin" else ".so"
LIBRARY = REPO / "native-sqlite/build/host/sqlite" / ("native_sqlite_host" + SUFFIX)


class StandaloneTests(unittest.TestCase):
    def test_single_copied_file_uses_custom_path_capacity_and_caller_buffer(self):
        with tempfile.TemporaryDirectory() as directory:
            shutil.copyfile(COMPONENT / "sqlite_shadow_vfs.py", Path(directory) / "sqlite_shadow_vfs.py")
            script = '''
import sys
sys.path.insert(0, sys.argv[1])
from sqlite_shadow_vfs import ShadowVFS
ram = bytearray(4096)
path = sys.argv[1] + '/custom.db'
io = ShadowVFS(path, max_database_bytes=len(ram), buffer=ram)
assert io.db_buffer is ram
assert io.paths == (path, path + '-journal')
code, handle = io(0, path, 6, 0)
assert code == 0, io.last_error
assert io(3, handle, 4095, b'x') == (0, 1)
assert io(3, handle, 4096, b'x')[0] == 13
assert io(4, handle, 4097, 0)[0] == 13
assert io(5, handle, 0, 0) == (0, 0)
io.close_all()
assert len(open(path, 'rb').read()) == 4096
assert not any(n == 'powersync' or n.startswith('powersync.') or n.startswith('dots_') for n in sys.modules)
'''
            result = subprocess.run([sys.executable, "-I", "-c", script, directory],
                                    cwd=directory, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_configuration_is_rejected_before_allocating_or_opening_files(self):
        sys.path.insert(0, str(COMPONENT))
        from sqlite_shadow_vfs import ShadowVFS
        for path in ("relative.db", "/", "/tmp/../escape.db", "/tmp//file.db", "/tmp/file.db/", "/tmp/\0.db"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                ShadowVFS(path)
        for capacity in (0, -1, True, 1.5):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                ShadowVFS("/tmp/file.db", max_database_bytes=capacity)
        with self.assertRaises(ValueError):
            ShadowVFS("/tmp/file.db", max_database_bytes=1024, buffer=bytearray(512))

    @unittest.skipUnless(LIBRARY.is_file(), "Build native-sqlite host --variant sqlite for integration")
    def test_plain_sqlite_commit_rollback_reopen_and_integrity_through_shadow(self):
        # Separate process: the host native adapter owns one global arena, and
        # this test explicitly chooses the SQLite-only shared library.
        import os
        script = '''
import sys, tempfile
sys.path[:0] = [sys.argv[1], sys.argv[1] + '/tests']
from sqlite_shadow_vfs import ShadowVFS
from sqlite_vfs_host import PythonVFSCore
assert not any(n == 'powersync' or n.startswith('dots_') for n in sys.modules)
with tempfile.TemporaryDirectory() as folder:
    path = folder + '/notes.db'
    native = PythonVFSCore()
    def open_db():
        io = ShadowVFS(path, max_database_bytes=64 * 1024)
        native.configure(io)
        native.open(path)
        native.exec('PRAGMA page_size=1024; PRAGMA max_page_count=64; PRAGMA synchronous=FULL; PRAGMA journal_mode=DELETE; PRAGMA temp_store=MEMORY')
        return io
    io = open_db()
    try:
        native.exec('CREATE TABLE notes(id INTEGER PRIMARY KEY, title TEXT)')
        native.exec("BEGIN; INSERT INTO notes VALUES(1,'committed'); COMMIT")
        native.exec("BEGIN; UPDATE notes SET title='rolled back'; ROLLBACK")
        assert native.exec('SELECT title FROM notes') == [['committed']]
        assert native.exec('PRAGMA integrity_check') == [['ok']]
        try:
            native.exec('SELECT powersync_rs_version()')
        except RuntimeError as exc:
            assert 'no such function' in str(exc)
        else:
            raise AssertionError('Integration test accidentally loaded PowerSync')
        assert native.stats['db.write'] > 0 and native.stats['journal.write'] > 0
        assert native.last_bridge_error is None
    finally:
        native.close()
        io.close_all()
    io = open_db()
    try:
        assert native.exec('SELECT title FROM notes') == [['committed']]
        assert native.exec('PRAGMA integrity_check') == [['ok']]
    finally:
        native.close()
        io.close_all()
'''
        environment = dict(os.environ, POWERSYNC_TEST_LIBRARY=str(LIBRARY))
        result = subprocess.run([sys.executable, "-I", "-c", script, str(COMPONENT)],
                                cwd=COMPONENT, env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
