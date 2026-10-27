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


    def _check_spilled_growth_rollback(self, automatic):
        import os
        script = '''
import sys, tempfile
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[1] + '/tests']
from sqlite_shadow_vfs import ShadowVFS
from sqlite_vfs_host import PythonVFSCore
automatic = sys.argv[2] == 'automatic'
with tempfile.TemporaryDirectory() as folder:
    path = folder + '/growth.db'
    native = PythonVFSCore()
    def open_db():
        io = ShadowVFS(path)
        native.configure(io)
        native.open(path)
        native.exec('PRAGMA page_size=1024; PRAGMA max_page_count=64; PRAGMA synchronous=FULL; PRAGMA journal_mode=DELETE; PRAGMA temp_store=MEMORY; PRAGMA cache_size=32')
        return io
    def check_original():
        assert native.exec('SELECT count(*), length(payload) FROM entries') == [['1', '32']]
        assert native.exec('PRAGMA integrity_check') == [['ok']]
    io = open_db()
    try:
        native.exec('CREATE TABLE entries(id INTEGER PRIMARY KEY, payload BLOB)')
        native.exec('INSERT INTO entries VALUES(-1, zeroblob(32))')
        original_size = io.db_size
        native.reset_stats()
        native.exec('BEGIN; UPDATE entries SET payload=zeroblob(40) WHERE id=-1')
        for identity in range(48):
            native.exec('INSERT INTO entries VALUES(%d, zeroblob(900))' % identity)
        # These writes must reach the VFS before rollback, rather than remaining
        # entirely in SQLite's page cache as in the small rollback fixture.
        assert native.stats['db.write'] > 0, native.stats
        assert io.db_size > original_size, (io.db_size, original_size)
        if automatic:
            try:
                for identity in range(48, 128):
                    native.exec('INSERT INTO entries VALUES(%d, zeroblob(900))' % identity)
            except RuntimeError as error:
                assert 'database or disk is full' in str(error), error
            else:
                raise AssertionError('Expected SQLITE_FULL to roll back the transaction')
        else:
            native.exec('ROLLBACK')
        check_original()
        shrinks = [event for event in native.events if event['op'] == 4
                   and event['kind'] == 'db' and event['arg'] == original_size]
        assert shrinks and all(event['code'] == 0 for event in shrinks), shrinks
        assert native.last_bridge_error is None, native.last_bridge_error
    finally:
        native.close()
        io.close_all()
    assert not Path(path + '-journal').exists(), 'Rollback left a hot journal'
    io = open_db()
    try:
        check_original()
        native.exec('BEGIN; INSERT INTO entries VALUES(1000, zeroblob(900)); COMMIT')
        assert native.exec('SELECT count(*) FROM entries') == [['2']]
        assert native.exec('PRAGMA integrity_check') == [['ok']]
    finally:
        native.close()
        io.close_all()
    io = open_db()
    try:
        assert native.exec('SELECT count(*) FROM entries') == [['2']]
        assert native.exec('PRAGMA integrity_check') == [['ok']]
    finally:
        native.close()
        io.close_all()
'''
        environment = dict(os.environ, POWERSYNC_TEST_LIBRARY=str(LIBRARY))
        result = subprocess.run([sys.executable, "-I", "-c", script, str(COMPONENT),
                                 "automatic" if automatic else "explicit"],
                                cwd=COMPONENT, env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(LIBRARY.is_file(), "Build native-sqlite host --variant sqlite for integration")
    def test_spilled_growth_rollback_preserves_rows_and_can_commit_after_reopen(self):
        self._check_spilled_growth_rollback(automatic=False)

    @unittest.skipUnless(LIBRARY.is_file(), "Build native-sqlite host --variant sqlite for integration")
    def test_sqlite_full_automatically_rolls_back_spilled_growth(self):
        self._check_spilled_growth_rollback(automatic=True)



    @unittest.skipUnless(LIBRARY.is_file(), "Build native-sqlite host --variant sqlite for integration")
    def test_failed_first_commit_cannot_resurrect_rolled_back_schema_after_reopen(self):
        import os
        script = '''
import sys, tempfile
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[1] + '/tests']
from sqlite_shadow_vfs import ShadowVFS
from sqlite_vfs_host import PythonVFSCore
with tempfile.TemporaryDirectory() as folder:
    path = folder + '/empty.db'
    native = PythonVFSCore()
    def open_db():
        io = ShadowVFS(path)
        native.configure(io)
        native.open(path)
        native.exec('PRAGMA page_size=1024; PRAGMA max_page_count=128; PRAGMA synchronous=FULL; PRAGMA journal_mode=DELETE; PRAGMA temp_store=MEMORY; PRAGMA cache_size=32')
        return io
    io = open_db()
    persisted_sizes = []
    try:
        assert io.db_size == Path(path).stat().st_size == 0
        native.exec('BEGIN; CREATE TABLE entries(id INTEGER PRIMARY KEY, payload BLOB)')
        for identity in range(48):
            native.exec('INSERT INTO entries VALUES(%d, zeroblob(900))' % identity)
        assert native.stats['db.write'] > 0, native.stats
        sync_db = io._sync_db
        def fail_after_first_database_sync(stream):
            sync_db(stream)
            if not persisted_sizes:
                persisted_sizes.append(Path(path).stat().st_size)
                raise OSError('Database sync failed after persistence')
        io._sync_db = fail_after_first_database_sync
        try:
            native.exec('COMMIT')
        except RuntimeError as error:
            assert 'disk I/O error' in str(error), error
        else:
            raise AssertionError('Expected the injected COMMIT failure')
        assert persisted_sizes and persisted_sizes[0] > 0, persisted_sizes
        assert native.exec("SELECT name FROM sqlite_master WHERE type='table'") == []
        shrinks = [event for event in native.events if event['op'] == 4
                   and event['kind'] == 'db' and event['arg'] == 0]
        assert shrinks and all(event['code'] == 0 for event in shrinks), shrinks
        assert native.last_bridge_error is None, native.last_bridge_error
    finally:
        native.close()
        io.close_all()
    assert not Path(path + '-journal').exists(), 'Rollback left a hot journal'
    io = open_db()
    try:
        assert native.exec("SELECT name FROM sqlite_master WHERE type='table'") == []
        assert native.exec('PRAGMA integrity_check') == [['ok']]
        native.exec('CREATE TABLE recovered(id INTEGER PRIMARY KEY)')
    finally:
        native.close()
        io.close_all()
    io = open_db()
    try:
        assert native.exec("SELECT name FROM sqlite_master WHERE type='table'") == [['recovered']]
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
