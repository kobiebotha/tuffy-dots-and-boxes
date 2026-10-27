"""Read-only validation of the normal native app state after benchmarking."""
import os, sys, machine, json, hashlib, binascii
watchdog = machine.WDT(timeout=16000)
sys.path.insert(0, '/system/apps/dots_boxes_native')
from dots_session import SessionFile
from dots_storage import Store
record = SessionFile('/state/dots_boxes_native').load(None)
assert record['pending_move'] is None
store = Store(heartbeat=watchdog.feed)
try:
    store.open()
    assert store.scalar('SELECT count(*) FROM ps_crud') == '0'
    assert store.scalar('PRAGMA quick_check') == 'ok'
    assert store.scalar('PRAGMA user_version') == '6'
    rows = []
    offset = 0
    while True:
        page = store.execute('SELECT id,r,e,n FROM moves ORDER BY id LIMIT 16 OFFSET %d' % offset)
        rows.extend(page)
        if len(page) < 16:
            break
        offset += len(page)
    raw_hash = binascii.hexlify(hashlib.sha256(json.dumps([
        store.execute('SELECT id,s,p,a,b,w,n FROM rooms ORDER BY id'), rows]).encode()).digest()).decode()
    try:
        os.stat('/state/dots_boxes_benchmark')
        raise AssertionError('Scratch directory remains')
    except OSError as error:
        assert error.args[0] == 2
    print('PERFORMANCE_FINAL_CHECK', json.dumps({
        'native_cache_restored': True, 'pending_move': None, 'crud_rows': 0,
        'quick_check': 'ok', 'schema_version': 6, 'paired_room': record['room_id'],
        'move_count': len(rows), 'raw_state_sha256': raw_hash,
        'scratch_removed': True, 'native_stats': store.native.stats()}))
finally:
    store.close()
    watchdog.feed()
