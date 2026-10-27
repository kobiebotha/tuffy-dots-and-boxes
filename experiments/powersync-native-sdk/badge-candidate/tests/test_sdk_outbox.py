"""Real SQLite/core checks for ordinary SDK mutations and legacy adoption."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[4]
APP = Path(__file__).resolve().parents[1] / 'apps/dots_boxes_native'
sys.path[:0] = [str(APP), str(ROOT / 'native-sqlite/tests')]
from native_sqlite_host import HostCore, LIBRARY
from dots_storage import Store
from dots_codec import encode_uuid, encode_edge

ROOM = '550e8400-e29b-41d4-a716-446655440000'
MOVE = '550e8400-e29b-41d4-a716-446655440001'
USER = '550e8400-e29b-41d4-a716-446655440002'
DEVICE = '550e8400-e29b-41d4-a716-446655440003'
LEGACY_SCHEMA = {
    'tables': [
        {'name': 'account', 'local_only': True, 'columns': [{'name': 'user_id', 'type': 'text'}]},
        {'name': '_badge_move_outbox', 'insert_only': True, 'columns': [
            {'name': 'room_id', 'type': 'text'}, {'name': 'orientation', 'type': 'text'},
            {'name': 'row_i', 'type': 'integer'}, {'name': 'col_i', 'type': 'integer'}]},
        {'name': '_badge_move_receipts', 'local_only': True, 'columns': [
            {'name': 'room_id', 'type': 'text'}, {'name': 'orientation', 'type': 'text'},
            {'name': 'row_i', 'type': 'integer'}, {'name': 'col_i', 'type': 'integer'},
            {'name': 'result', 'type': 'text'}, {'name': 'message', 'type': 'text'}]}],
    'raw_tables': [{'name': 'rooms', 'table_name': 'rooms', 'clear': 'DELETE FROM rooms'},
                   {'name': 'moves', 'table_name': 'moves', 'clear': 'DELETE FROM moves'}]}


def legacy_session(pending=True):
    return {'version': 1, 'device_id': DEVICE, 'user_id': USER, 'room_id': ROOM,
            'seat': 1, 'refresh_token': 'fixture-refresh', 'access_token': 'fixture-access',
            'expires_at': 3600, 'pending_move':
            {'id': MOVE, 'orientation': 'h', 'row': 0, 'col': 0} if pending else None}


@unittest.skipUnless(LIBRARY.exists(), 'Pinned host PowerSync core unavailable')
class SDKOutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name + '/native'
        self.native = HostCore()
        self.store = Store(native=self.native, root=self.root).open()
        self.addCleanup(self.close_store)

    def close_store(self):
        if self.store.opened:
            self.store.close()

    def count(self):
        return int(self.store.scalar('SELECT count(*) FROM ps_crud'))

    def local_rows(self):
        return self.store.execute('SELECT id,room_id,orientation,row_i,col_i FROM _badge_move_outbox ORDER BY id')

    def install_legacy(self, *, pending=True, queued=False, receipt=None, published=False):
        self.store.close()
        self.native.open(self.root + '/state.sqlite')
        try:
            self.native.exec('BEGIN IMMEDIATE')
            self.native.exec("SELECT powersync_replace_schema('%s')" % json.dumps(LEGACY_SCHEMA).replace("'", "''"))
            # Actual schema-6 raw tables used WITHOUT ROWID; a schema-only
            # downgrade would miss the SDK-watch storage migration entirely.
            self.native.exec('DROP TABLE moves')
            self.native.exec('DROP TABLE rooms')
            self.native.exec('CREATE TABLE rooms(id TEXT PRIMARY KEY NOT NULL,s INTEGER,p INTEGER,a INTEGER,b INTEGER,w INTEGER,n INTEGER) WITHOUT ROWID')
            self.native.exec('CREATE TABLE moves(id TEXT PRIMARY KEY NOT NULL,r TEXT,e INTEGER,n INTEGER) WITHOUT ROWID')
            self.native.exec("INSERT INTO rooms(id,s,p,a,b,w,n) VALUES('%s',1,1,0,0,NULL,%d)" %
                             (encode_uuid(ROOM), int(published)))
            if published:
                self.native.exec("INSERT INTO moves(id,r,e,n) VALUES('%s','%s',%d,1)" %
                                 (encode_uuid(MOVE), encode_uuid(ROOM), encode_edge('h', 0, 0)))
            if queued:
                self.native.exec("INSERT INTO _badge_move_outbox(id,room_id,orientation,row_i,col_i) VALUES('%s','%s','h',0,0)" %
                                 (MOVE, ROOM))
            if receipt:
                self.native.exec("INSERT INTO _badge_move_receipts(id,room_id,orientation,row_i,col_i,result,message) VALUES('%s','%s','h',0,0,'%s','fixture-result')" %
                                 (MOVE, ROOM, receipt))
            self.native.exec('PRAGMA user_version=6')
            self.native.exec('COMMIT')
        finally:
            self.native.close()
        session = Path(self.root) / 'session.json'
        session.write_text(json.dumps(legacy_session(pending)))
        return session.read_bytes()

    def test_one_ordinary_mutation_is_readable_and_has_one_sdk_put(self):
        identity = self.store.mutate_move(ROOM, 'h', 0, 0)
        self.assertTrue(isinstance(identity, str) and len(identity) == 36)
        self.assertEqual(self.local_rows(), [[identity, ROOM, 'h', '0', '0']])
        self.assertEqual(self.count(), 1)
        self.assertEqual(json.loads(self.store.execute('SELECT data FROM ps_crud')[0][0]),
                         {'op': 'PUT', 'type': '_badge_move_outbox', 'id': identity,
                          'data': {'room_id': ROOM, 'orientation': 'h', 'row_i': 0, 'col_i': 0}})
        self.assertEqual(self.store.scalar('SELECT count(*) FROM moves'), '0')
        self.assertEqual(self.store.scalar('SELECT count(*) FROM rooms'), '0')
        self.assertEqual(self.store.scalar('PRAGMA user_version'), '7')
        schema = json.loads(self.store.schema)
        outbox = next(table for table in schema['tables'] if table['name'] == '_badge_move_outbox')
        self.assertFalse(outbox.get('insert_only', False))
        self.assertFalse(any(table['name'] in ('_badge_move_receipts', '_badge_move_state') for table in schema['tables']))

    def test_mutation_rollback_drops_local_row_and_crud_together(self):
        self.store.execute('BEGIN IMMEDIATE')
        identity = self.store.mutate_move(ROOM, 'v', 1, 2)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.local_rows()[0][0], identity)
        self.store.execute('ROLLBACK')
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.local_rows(), [])
        self.store.close()
        self.store.open()
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.local_rows(), [])

    def test_reopen_does_not_duplicate_the_sdk_transaction(self):
        identity = self.store.mutate_move(ROOM, 'h', 0, 0)
        entry = self.store.execute('SELECT id,data FROM ps_crud')
        self.store.close()
        self.store.open()
        self.assertEqual(self.store.execute('SELECT id,data FROM ps_crud'), entry)
        self.assertEqual(self.local_rows()[0][0], identity)
        self.assertEqual(self.store._validate_local_writes()[0]['id'], identity)

    def test_sdk_completion_keeps_readable_row_and_never_reenqueues(self):
        identity = self.store.mutate_move(ROOM, 'h', 0, 0)
        # Isolate ordinary-row behavior at SDK queue removal. Actual SDK ABI
        # checks separately own complete() and checkpoint advancement.
        self.store.execute('DELETE FROM ps_crud')
        self.assertEqual(self.local_rows(), [[identity, ROOM, 'h', '0', '0']])
        self.store.close()
        self.store.open()
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.local_rows(), [[identity, ROOM, 'h', '0', '0']])
        for obsolete in ('ensure_pending_move', 'record_upload_result', 'prune_upload_receipts'):
            self.assertFalse(hasattr(self.store, obsolete))

    def test_snapshot_reads_local_mutation_without_writing_canonical_tables(self):
        identity = self.store.mutate_move(ROOM, 'h', 0, 0)
        result = self.store.snapshot(ROOM)
        self.assertIsNone(result['room'])
        self.assertEqual(result['local_moves'], 1)
        local = result['moves'][0]
        self.assertEqual((local['orientation'], local['row'], local['col']), ('h', 0, 0))
        self.assertEqual(self.store.scalar('SELECT count(*) FROM moves'), '0')
        self.assertEqual(self.count(), 1)

    def test_snapshot_deduplicates_the_exact_canonical_uuid(self):
        identity = self.store.mutate_move(ROOM, 'h', 0, 0)
        self.store.execute("INSERT INTO rooms(id,s,p,a,b,w,n) VALUES('%s',1,2,0,0,NULL,1)" % encode_uuid(ROOM))
        self.store.execute("INSERT INTO moves(id,r,e,n) VALUES('%s','%s',%d,1)" %
                           (encode_uuid(identity), encode_uuid(ROOM), encode_edge('h', 0, 0)))
        result = self.store.snapshot(ROOM)
        self.assertEqual(len(result['moves']), 1)
        self.assertEqual(result['moves'][0]['orientation'], 'h')
        # Reading canonical data cannot delete mutation rows or complete CRUD.
        self.assertEqual(self.count(), 1)
        self.assertEqual(len(self.local_rows()), 1)

    def test_invalid_mutation_does_not_leave_an_sdk_write(self):
        with self.assertRaises((ValueError, TypeError)):
            self.store.mutate_move(ROOM, 'h', 99, 0)
        self.assertEqual(self.local_rows(), [])
        self.assertEqual(self.count(), 0)

    def test_unknown_crud_fails_closed_on_reopen(self):
        self.store.mutate_move(ROOM, 'h', 0, 0)
        self.store.execute("UPDATE ps_crud SET data='%s'" % json.dumps({'op': 'DELETE', 'type': 'moves', 'id': MOVE}))
        self.store.close()
        with self.assertRaisesRegex(ValueError, 'Unexpected SDK CRUD'):
            self.store.open()

    def test_schema6_saved_before_enqueue_imports_same_uuid_once(self):
        before = self.install_legacy(pending=True)
        self.store.open()
        self.assertEqual(self.store.scalar('PRAGMA user_version'), '7')
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.local_rows(), [[MOVE, ROOM, 'h', '0', '0']])
        self.store.close()
        self.store.open()
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.local_rows(), [[MOVE, ROOM, 'h', '0', '0']])
        self.assertEqual((Path(self.root) / 'session.json').read_bytes(), before)

    def test_schema6_already_queued_is_not_inserted_again(self):
        self.install_legacy(pending=True, queued=True)
        self.store.open()
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.store._validate_local_writes()[0]['id'], MOVE)
        self.store.close()
        self.store.open()
        self.assertEqual(self.count(), 1)

    def test_schema6_accepted_receipt_without_queue_does_not_reenqueue(self):
        before = self.install_legacy(pending=True, receipt='success')
        self.store.open()
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.local_rows(), [])
        self.assertEqual((Path(self.root) / 'session.json').read_bytes(), before)
        self.store.close()
        self.store.open()
        self.assertEqual(self.count(), 0)

    def test_schema6_exact_canonical_pending_is_not_reenqueued(self):
        self.install_legacy(pending=True, published=True)
        self.store.open()
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.store.execute('SELECT id,r,e,n FROM moves'),
                         [[encode_uuid(MOVE), encode_uuid(ROOM), str(encode_edge('h', 0, 0)), '1']])
        self.assertEqual(self.store.execute('SELECT id,s,p,a,b,w,n FROM rooms'),
                         [[encode_uuid(ROOM), '1', '1', '0', '0', None, '1']])

    def test_schema6_queued_permanent_rejection_requires_drain(self):
        self.install_legacy(pending=False, queued=True, receipt='rejected')
        with self.assertRaisesRegex(ValueError, 'reject|drain'):
            self.store.open()
        self.native.open(self.root + '/state.sqlite')
        try:
            self.assertEqual(self.native.exec('PRAGMA user_version'), [['6']])
            self.assertEqual(self.native.exec('SELECT count(*) FROM ps_crud'), [['1']])
            self.assertEqual(self.native.exec('SELECT result FROM _badge_move_receipts'), [['rejected']])
        finally:
            self.native.close()

    def test_schema7_marker_never_reimports_stale_json_after_sdk_completion(self):
        self.install_legacy(pending=True)
        self.store.open()
        self.assertEqual(self.count(), 1)
        self.store.execute('DELETE FROM ps_crud')
        self.store.close()
        self.store.open()
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.local_rows()[0][0], MOVE)


if __name__ == '__main__':
    unittest.main()
