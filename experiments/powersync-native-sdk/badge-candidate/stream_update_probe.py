"""Qualify live checkpoint TLS against an already published immutable move.

Run through performance_device.py. Uses the installed managed app and saved
pairing. A deliberate idempotent SDK queue entry exercises completion and a
covering checkpoint without posting a new move or changing the canonical board.
The normal connector recognizes the exact canonical UUID; SDK ownership and
durable pending/receipt ordering remain intact. No credentials are logged.
"""
import badgeware
import machine
import sys
import time
import json
import hashlib
import binascii

probe_watchdog = machine.WDT(timeout=16000)
boot = open('/system/main.py').read()
exec(boot[:boot.index('# Eat the wakeup button')])


class StreamUpdateProbe:
    def __init__(self):
        self.done = False
        self.stage = 'warmup'
        self.started = time.ticks_ms()
        self.next_report = 0
        self.requests = []
        self.errors = []

    def wrap_update(self, update, client):
        assert client is not None and not client.failure and client.sync is not None
        link = client.sync
        real_factory = link.http_factory
        real_reply = link._reply
        driver = self

        def factory(request, *args, **kwargs):
            driver.requests.append((request['id'], request['url'].split('?')[0].rsplit('/', 1)[-1]))
            return real_factory(request, *args, **kwargs)

        def reply(value):
            if value['op'] == 'error':
                driver.errors.append(value.get('id'))
            return real_reply(value)

        link.http_factory = factory
        link._reply = reply

        def frame():
            probe_watchdog.feed()
            result = update()
            now = time.ticks_ms()
            runtime = time.ticks_diff(now, driver.started)
            assert not client.failure and link.phase not in ('blocked', 'revoked'), (client.failure, link.phase, link.last_error)
            if runtime >= driver.next_report:
                print('UPDATE_LIVE_PROGRESS', runtime, driver.stage, link.status,
                      link.full_generation, link._single_tls)
                driver.next_report = runtime + 10000
            if driver.stage == 'warmup' and link.synced and client.records.record['pending_move'] is None:
                from dots_codec import encode_uuid, decode_uuid, decode_edge
                from powersync.database import _quote
                record = client.records.record
                rows = client.store.execute('SELECT id,e FROM moves WHERE r=%s ORDER BY n DESC LIMIT 1' %
                                            _quote(encode_uuid(record['room_id'])))
                assert len(rows) == 1, 'A published move is needed for this idempotent check'
                identity, edge = rows[0]
                orientation, row, col = decode_edge(int(edge))
                pending = {'id': decode_uuid(identity), 'orientation': orientation, 'row': row, 'col': col}
                assert client.store.has_move(pending['id'], record['room_id'])
                streams = [i for i in link.remote if link._is_stream(i) and i not in link._await_seed]
                assert len(streams) == 1
                driver.stream = streams[0]
                driver.stream_task = link.remote[driver.stream]
                driver.initial_full = link.full_generation
                driver.initial_count = client.store.room(record['room_id'])['move_count']
                driver.request_start = len(driver.requests)
                driver.error_start = len(driver.errors)
                driver.move_id, driver.room_id = pending['id'], record['room_id']
                driver.measure_started = time.ticks_ms()
                # Session-before-CRUD ordering matches Client._queue_move.
                # ensure_pending_move intentionally suppresses an already
                # canonical UUID, so this qualification explicitly inserts one
                # valid idempotent entry into the same real insert-only queue.
                client.records.save(dict(record, pending_move=pending))
                client.store.execute('INSERT INTO _badge_move_outbox(id,room_id,orientation,row_i,col_i) VALUES(%s,%s,%s,%d,%d)' %
                                     (_quote(pending['id']), _quote(record['room_id']), _quote(orientation), row, col))
                assert client.store.scalar('SELECT count(*) FROM ps_crud') == '1'
                driver.stage = 'checkpoint'
            elif driver.stage == 'checkpoint':
                pending = client.records.record['pending_move']
                assert pending is None or pending['id'] == driver.move_id
                if pending is None and link.synced:
                    assert link.full_generation > driver.initial_full
                    assert client.store.has_move(driver.move_id, driver.room_id)
                    assert client.store.scalar('SELECT count(*) FROM ps_crud') == '0'
                    assert client.store.scalar('PRAGMA quick_check') == 'ok'
                    assert client.store.room(driver.room_id)['move_count'] == driver.initial_count
                    requests = driver.requests[driver.request_start:]
                    same_stream = (link.remote.get(driver.stream) is driver.stream_task
                                   and driver.stream_task.phase != 'closed')
                    assert same_stream or link._single_tls, 'Stream lost without resource fallback'
                    assert any(kind == 'checkpoint-request' for _, kind in requests)
                    assert link._single_tls or not driver.errors[driver.error_start:]
                    with open('/system/apps/dots_boxes_native/powersync/sync.py', 'rb') as source:
                        sync_hash = binascii.hexlify(hashlib.sha256(source.read()).digest()).decode()
                    print('UPDATE_LIVE_RESULT', json.dumps({
                        'passed': True, 'workload': 'real TLS checkpoint for idempotent already-published SDK CRUD entry; no move RPC',
                        'elapsed_ms': time.ticks_diff(time.ticks_ms(), driver.measure_started),
                        'same_stream_retained': same_stream, 'single_tls': link._single_tls,
                        'sdk_http_requests': requests, 'sdk_http_errors': driver.errors[driver.error_start:],
                        'initial_full_generation': driver.initial_full, 'full_generation': link.full_generation,
                        'exact_uuid_room_confirmed': True, 'pending_cleared': True, 'crud_rows': 0,
                        'canonical_move_count': driver.initial_count, 'installed_sync_sha256': sync_hash}))
                    driver.done = True
                    return True
            assert runtime < 150000, ('Live qualification timeout', driver.stage, link.last_error)
            return result
        return frame


probe = StreamUpdateProbe()
sys.modules['_badge_native_recovery'] = probe
try:
    badgeware.launch('/system/apps/dots_boxes_native')
finally:
    del sys.modules['_badge_native_recovery']
    probe_watchdog.feed()
assert probe.done, 'Live qualification did not finish'
