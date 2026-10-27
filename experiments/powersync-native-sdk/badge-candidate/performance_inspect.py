import os, sys, machine, json
watchdog = machine.WDT(timeout=16000)
watchdog.feed()
sys.path.insert(0, '/system/apps/dots_boxes_native')
from dots_session import SessionFile
record = SessionFile('/state/dots_boxes_native').load(None)
print('BENCH_SESSION', json.dumps({k: record.get(k) for k in ('room_id', 'seat', 'expires_at')}))
print('BENCH_PENDING', open('/state/dots_boxes_native/pending.json').read())
print('ORIGINAL_FILES', os.listdir('/system/apps/dots_boxes'))
from dots_storage import Store
store = Store(heartbeat=watchdog.feed)
store.open()
print('BENCH_QUEUED', store.scalar('SELECT count(*) FROM ps_crud'))
print('BENCH_ROOM', json.dumps(store.room(record['room_id'])))
print('BENCH_MOVES', json.dumps(store.moves(record['room_id'])))
store.close()
if globals().get('BENCH_CLEAR_CACHE'):
    assert record['pending_move'] is None
    # Explicitly authorized cache overwrite; session/pending identity remains.
    os.remove('/state/dots_boxes_native/state.sqlite')
    os.sync()
    print('NATIVE_CACHE_REMOVED_FOR_BENCHMARK', os.statvfs('/state'))
watchdog.feed()
