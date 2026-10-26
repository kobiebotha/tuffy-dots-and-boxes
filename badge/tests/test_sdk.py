"""Exercise the SDK with a different schema and without the game on sys.path."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

BADGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BADGE / "lib"))

from powersync.database import Database
from powersync.http import PENDING
from powersync.sync import Connector, SyncClient
from native_fixture import LIBRARY
from vfs_fixture import PythonVFSCore

SCHEMA = {"tables": [], "raw_tables": [
    {"name": "notes", "table_name": "notes", "clear": "DELETE FROM notes"}]}
DDL = ("CREATE TABLE notes(id TEXT PRIMARY KEY NOT NULL, title TEXT) WITHOUT ROWID",)


class NotesConnector(Connector):
    def __init__(self):
        self.refreshes = 0

    def fetch_credentials(self):
        return "notes-token-" + str(self.refreshes)

    def invalidate_credentials(self):
        self.refreshes += 1


class MemoryStream:
    def __init__(self, url, body, ca_files, **options):
        self.status = 200
        self.parts = []
        self.closed = False
        self.token = options["token"]
        self.body = body

    def step(self):
        return self.parts.pop(0) if self.parts else PENDING

    def close(self):
        self.closed = True


class StandaloneTests(unittest.TestCase):
    def test_copied_sdk_imports_with_no_game_or_badge_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            shutil.copytree(BADGE / "lib/powersync", Path(directory) / "powersync",
                            ignore=shutil.ignore_patterns("__pycache__"))
            script = '''
import sys
sys.path.insert(0, sys.argv[1])
from powersync.database import Database
from powersync.http import HTTPS
from powersync.stream import StreamLines
from powersync.sync import Connector, SyncClient
from powersync.runtime import module_directory
assert module_directory() == sys.argv[1] + '/powersync'
assert not any(name.startswith('dots_') or name in ('badgeware', 'machine') for name in sys.modules)
db = Database({'tables': [{'name': 'tasks', 'columns': []}]}, '/state/tasks', 'tasks-v1')
db._sync_line('{"data":{"data":[{"op":"PUT","object_type":"tasks"}]}}')
assert db.sync_schema['tables'][0]['name'] == 'tasks'
'''
            result = subprocess.run([sys.executable, "-I", "-c", script, directory],
                                    cwd=directory, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(LIBRARY.exists(), "Pinned host PowerSync qualifier unavailable")
class IndependentDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.native = PythonVFSCore()
        self.store = Database(SCHEMA, self.temporary.name + "/notes", "notes-app-v1\n",
                              native=self.native, create_statements=DDL).open()
        self.addCleanup(self.store.close)
        self.connector = NotesConnector()
        self.sync = SyncClient(self.store, self.connector, "https://sync.example",
                               {"name": "notes_stream", "params": {"folder": "inbox"}}, (),
                               http_factory=MemoryStream)
        self.addCleanup(self.sync.close)

    def start(self):
        for _ in range(10):
            self.sync.step(1)
            if self.sync.phase == "stream":
                return
        self.fail(self.sync.last_error)

    def test_custom_schema_sync_reopen_and_token_refresh(self):
        self.start()
        self.assertEqual(self.sync.stream.token, "notes-token-0")
        op = {"op_id": "1", "op": "PUT", "object_type": "notes", "object_id": "note-1",
              "checksum": 7, "data": json.dumps({"title": "A separate app"})}
        records = [
            {"checkpoint": {"last_op_id": "1", "buckets": [
                {"bucket": "notes", "checksum": 7, "priority": 3, "count": 1}]}},
            {"data": {"bucket": "notes", "data": [op]}}, {"checkpoint_complete": {}},
        ]
        self.sync.stream.parts = [b"".join(json.dumps(line).encode() + b"\n" for line in records)]
        self.sync.step(1)
        self.assertTrue(self.sync.synced, self.sync.last_error)
        self.assertEqual(self.store.execute("SELECT * FROM notes"), [["note-1", "A separate app"]])
        old_stream = self.sync.stream
        self.sync._instructions([{"FetchCredentials": {}}])
        self.start()
        self.assertTrue(old_stream.closed)
        self.assertEqual(self.sync.stream.token, "notes-token-1")
        self.sync.close()
        self.store.close()
        self.store.open()
        self.assertEqual(self.store.execute("SELECT * FROM notes"), [["note-1", "A separate app"]])
        self.assertEqual(self.store.scalar("SELECT title FROM notes"), "A separate app")
        self.assertFalse(self.store.has_local_writes())

    def test_unconfigured_table_is_rejected_before_database_work(self):
        with self.assertRaisesRegex(ValueError, "Unexpected synced table"):
            self.store.control_lines(['{"data":{"data":[{"op":"PUT","object_type":"moves"}]}}'])


if __name__ == "__main__":
    unittest.main()
