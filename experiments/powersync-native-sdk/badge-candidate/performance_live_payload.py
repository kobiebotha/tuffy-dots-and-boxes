# Reuses meter definitions from performance_replay_payload.py.
import badgeware
import network
from dots_session import SessionFile
from dots_client import Client
from powersync.http import HTTPS
from view import View

boot = open('/system/main.py').read()
exec(boot[:boot.index('# Eat the wakeup button')])

http_events = []
real_http_step = HTTPS.step
real_http_close = HTTPS.close
live_started = micros()


def http_step(task):
    event = getattr(task, '_performance_event', None)
    if event is None:
        event = {'path': task.path.split('?')[0], 'start_us': elapsed(live_started),
                 'calls': {}, 'cpu_us': {}, 'wall_us': {}, 'bytes': 0,
                 'phase': task.phase, 'phase_start': micros(), 'max_call_us': 0}
        task._performance_event = event
        http_events.append(event)
    phase = task.phase
    start = micros()
    try:
        result = real_http_step(task)
        if isinstance(result, bytes):
            event['bytes'] += len(result)
            if 'first_body_us' not in event:
                event['first_body_us'] = elapsed(live_started)
        event['status'] = task.status
        return result
    finally:
        duration = elapsed(start)
        event['calls'][phase] = event['calls'].get(phase, 0) + 1
        event['cpu_us'][phase] = event['cpu_us'].get(phase, 0) + duration
        event['max_call_us'] = max(event['max_call_us'], duration)
        if task.phase != event['phase']:
            previous = event['phase']
            event['wall_us'][previous] = event['wall_us'].get(previous, 0) + elapsed(event['phase_start'])
            event['phase'] = task.phase
            event['phase_start'] = micros()


def http_close(task):
    event = getattr(task, '_performance_event', None)
    if event is not None and 'closed_us' not in event:
        event['closed_us'] = elapsed(live_started)
        event['close_phase'] = task.phase
    return real_http_close(task)


HTTPS.step = http_step
HTTPS.close = http_close
database_module.ShadowVFS = MeterVFS
native = TimedNative(load_native(feed))
store = Store(native=native, root=BENCH_CONFIG['root'], heartbeat=feed)
store.open()
session = SessionFile('/state/dots_boxes_native')
record = session.load(None)
assert record['pending_move'] is None, 'Complete the saved move before live-download benchmarking'
assert record['refresh_token'], 'Existing badge pairing is required'
expected_room = record['room_id']
view = View(screen, color, font)
badge.mode(HIRES | VSYNC)
client = None
done = False
frame_count = frame_cpu_us = frame_max_us = render_cpu_us = render_max_us = 0
last_frame = time.ticks_ms()
started_ms = last_frame
live_started = micros()
initial_full = 0
result = None
idle_frames = 0
first_synced_us = None
first_sync_counters = None
render_calls = 0


def frame():
    global client, live_started, last_frame, started_ms, result, done
    global frame_count, frame_cpu_us, frame_max_us, render_cpu_us, render_max_us, render_calls
    global idle_frames, first_synced_us, first_sync_counters
    feed()
    if not network.WLAN(network.STA_IF).isconnected():
        assert time.ticks_diff(time.ticks_ms(), started_ms) < 60000, 'Wi-Fi did not connect'
        return
    if client is None:
        native.reset_meter()
        store.io.reset_meter()
        live_started = micros()
        started_ms = time.ticks_ms()
        client = Client(store, session, heartbeat=feed)
        last_frame = time.ticks_ms()
    now = time.ticks_ms()
    delta = time.ticks_diff(now, last_frame)
    last_frame = now
    start = micros()
    client.step([], delta)
    duration = elapsed(start)
    frame_cpu_us += duration
    frame_max_us = max(frame_max_us, duration)
    frame_count += 1
    start = micros()
    view.draw_if_changed(client.controller, record['seat'])
    duration = elapsed(start)
    render_cpu_us += duration
    render_max_us = max(render_max_us, duration)
    render_calls += 1
    link = client.sync
    assert not client.failure and link.phase not in ('blocked', 'revoked'), (client.failure, link.phase, link.last_error)
    if link.synced:
        if first_synced_us is None:
            first_synced_us = elapsed(live_started)
            first_sync_counters = {'frame_count': frame_count, 'frame_cpu_us': frame_cpu_us,
                                   'frame_max_us': frame_max_us, 'render_cpu_us': render_cpu_us,
                                   'render_max_us': render_max_us, 'native': native.report(),
                                   'vfs': store.io.report()}
        idle_frames += 1
    if idle_frames >= 30 or time.ticks_diff(now, started_ms) > 90000:
        result = {'variant': BENCH_CONFIG['variant'], 'first_synced_us': first_synced_us,
                  'room': expected_room, 'move_count': store.room(expected_room)['move_count'] if store.room(expected_room) else None,
                  'frame_count': frame_count, 'frame_cpu_us': frame_cpu_us, 'frame_max_us': frame_max_us,
                  'render_cpu_us': render_cpu_us, 'render_max_us': render_max_us, 'render_calls': render_calls,
                  'native': native.report(), 'vfs': store.io.report(), 'http': http_events,
                  'synced': link.synced, 'last_error': link.last_error, 'full_generation': getattr(link, 'full_generation', link.generation),
                  'first_sync_counters': first_sync_counters, 'single_tls': getattr(link, '_single_tls', None),
                  'scope': 'Actual same paired room cold download on managed VSYNC loop. send phase includes TLS handshake and HTTP write.'}
        for event in http_events:
            event.pop('phase_start', None)
        assert client.records.record['pending_move'] is None
        assert store.scalar('SELECT count(*) FROM ps_crud') == '0'
        assert store.scalar('PRAGMA quick_check') == 'ok'
        import hashlib, binascii
        import uctypes
        native_stats = native.stats()
        result['memory'] = {'native_stats': native_stats,
                            'sqlite_rust_arena_address': hex(native_stats[5]),
                            'vfs_shadow_address': hex(uctypes.addressof(store.io.db_buffer)),
                            'vfs_shadow_bytes': len(store.io.db_buffer)}
        rows = []
        offset = 0
        while True:
            page = store.execute('SELECT id,r,e,n FROM moves ORDER BY id LIMIT 16 OFFSET %d' % offset)
            rows.extend(page)
            if len(page) < 16:
                break
            offset += len(page)
        result['raw_state_sha256'] = binascii.hexlify(hashlib.sha256(json.dumps([
            store.execute('SELECT id,s,p,a,b,w,n FROM rooms ORDER BY id'), rows]).encode()).digest()).decode()
        done = True
        return True


previous_clear = badge.default_clear
try:
    badge.default_clear = None
    run(frame)
finally:
    badge.default_clear = previous_clear
    if client is not None:
        client.close()
    store.close()
    if globals().get('BENCH_RESTORE_NATIVE_CACHE') and done and result['synced']:
        assert BENCH_CONFIG['variant'] == 'native'
        os.rename(BENCH_CONFIG['root'] + '/state.sqlite', '/state/dots_boxes_native/state.sqlite')
        os.sync()
        result['native_cache_restored'] = True
    else:
        clear_flash_database(BENCH_CONFIG['root'])
    os.remove(BENCH_CONFIG['root'] + '/owner.txt')
    os.rmdir(BENCH_CONFIG['root'])
    feed()
assert done and result is not None
import machine, hashlib, binascii
result['identity'] = {'implementation': str(sys.implementation), 'clock_hz': machine.freq(), 'files': {}}
result['identity']['badge_id'] = binascii.hexlify(machine.unique_id()).decode()
from powersync import runtime as runtime_module
result['identity']['verified_native_sha256'] = runtime_module.NATIVE_SHA256
result['identity']['verified_native_bytes'] = runtime_module.NATIVE_BYTES
for name in ('dots_client.py', 'dots_storage.py', 'dots_sync.py', 'view.py', 'sqlite_shadow_vfs.py',
             'powersync/sync.py', 'powersync/database.py', 'powersync/http.py', 'powersync/runtime.py'):
    digest = hashlib.sha256()
    with open(_app + '/' + name, 'rb') as source:
        while True:
            chunk = source.read(4096)
            if not chunk:
                break
            digest.update(chunk)
            feed()
    result['identity']['files'][name] = binascii.hexlify(digest.digest()).decode()
print('PERFORMANCE_LIVE_RESULT', json.dumps(result))
