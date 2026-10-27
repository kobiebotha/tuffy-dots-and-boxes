"""Ordinary SQL mutation -> real SDK upload -> committed watch query replay."""
from dots_sync import GameConnector
from dots_session import fresh_session, validate_session
from dots_codec import decode_edge
import hashlib
import binascii

FINAL_MOVE = '00000000-0000-0000-0000-000000000031'


def ordinary_wire(operations, last_op, count, write=None):
    checkpoint = json.loads(json.dumps(BENCH_FIXTURE['checkpoint']))
    checkpoint['checkpoint']['last_op_id'] = str(last_op)
    checkpoint['checkpoint']['buckets'][0]['count'] = count
    if write is not None:
        checkpoint['checkpoint']['write_checkpoint'] = write
    # MicroPython dictionaries do not retain insertion order; keep bucket
    # before data so the exact fixture works with both qualified framers.
    return (json.dumps(checkpoint) + '\n{"data":{"bucket":"fixture","data":' +
            json.dumps(operations) + '}}\n' +
            json.dumps({'checkpoint_complete': {'last_op_id': str(last_op)}}) + '\n').encode()


def ordinary_case(repetition, native):
    store = None
    sync = watch = None
    if BENCH_CONFIG['storage'] == 'flash':
        clear_flash_database(BENCH_CONFIG['root'])
    store = BenchmarkStore(native=native, root=BENCH_CONFIG['root'], heartbeat=feed)
    try:
        if BENCH_CONFIG['storage'] == 'memory':
            initialize_memory(store)
        else:
            store.open()
        room = dict(BENCH_FIXTURE['operations'][-1])
        old_room = json.loads(room['data'])
        old_room['n'], old_room['s'], old_room['a'], old_room['b'] = 48, 1, 0, 0
        room['data'], room['op_id'] = json.dumps(old_room), '49'
        initial_wire = ordinary_wire(BENCH_FIXTURE['operations'][:48] + [room], 49, 49)
        final_move = dict(BENCH_FIXTURE['operations'][-2])
        final_move['op_id'] = '50'
        final_room = dict(BENCH_FIXTURE['operations'][-1])
        final_room['op_id'] = '51'
        final_wire = ordinary_wire([final_move, final_room], 51, 50, '100')
        wire_sha = binascii.hexlify(hashlib.sha256(final_wire).digest()).decode()
        fixture = FixtureNetwork(initial_wire)
        marks = {'started': 0, 'optimistic_watch_us': None, 'rpc_accepted_us': None,
                 'reconciled_watch_us': None}
        attempts = []
        notifications = []

        class OrdinaryRecords:
            def __init__(self):
                self.record = dict(fresh_session('550e8400-e29b-41d4-a716-446655440002'),
                    user_id='550e8400-e29b-41d4-a716-446655440003',
                    room_id=BENCH_FIXTURE['room'], seat=1,
                    access_token='fixture-only', refresh_token='fixture-only',
                    expires_at=int(time.time()) + 3600)

            def save(self, value):
                # No mutation/session coordination belongs to this flow.
                validate_session(value)
                assert value['pending_move'] is None
                self.record = value

        class OrdinaryRPC:
            def __init__(self, url, *args, **kwargs):
                assert url == 'https://backend.fixture.invalid/move'

            def start(self, body):
                assert body['move_id'] == FINAL_MOVE and body['target'] == BENCH_FIXTURE['room']
                attempts.append(body['move_id'])

            def step(self):
                # Require proof that the real SDK watch exposed our row before
                # accepting the fixture RPC; no manual invalidation is used.
                if marks['optimistic_watch_us'] is None:
                    return PENDING
                assert store.scalar("SELECT count(*) FROM _badge_move_outbox WHERE id='%s'" % FINAL_MOVE) == '1'
                assert records.record['pending_move'] is None
                marks['rpc_accepted_us'] = elapsed(marks['started'])
                fixture.wire = final_wire
                for task in fixture.streams:
                    if not task.closed:
                        task.data += final_wire
                return {'http_status': 200}

            def close(self):
                pass

        records = OrdinaryRecords()
        connector = GameConnector(records, 'https://backend.fixture.invalid/auth',
            'https://backend.fixture.invalid/move', 'synthetic-anon-key-over-20-bytes', (),
            heartbeat=feed, json_factory=OrdinaryRPC, store=store)
        sync = SyncClient(store, connector, 'https://fixture.invalid',
            {'name': 'badge_game_v2', 'params': {'room_id': BENCH_FIXTURE['room']}}, (),
            heartbeat=feed, http_factory=fixture.factory, json_factory=fixture.json_factory)
        real_notify = store.notify_watches

        def notify(identities):
            notifications.extend(identities)
            real_notify(identities)

        store.notify_watches = notify
        watch = store.watch(BENCH_FIXTURE['room'])
        warm_started = micros()
        while not sync.synced and elapsed(warm_started) < 30000000:
            feed()
            sync.step(1)
            assert sync.phase not in ('blocked', 'revoked'), (sync.phase, sync.last_error)
        assert sync.synced and store.scalar('SELECT count(*) FROM moves') == '48'
        # Drain the initial watch notification so only mutation/reconciliation
        # notifications can establish the measured checks below.
        for _ in range(4):
            sync.step(0)
        watch.read()
        assert not watch.dirty
        notifications[:] = []
        initial_full = sync.full_generation
        initial_requests = fixture.requests
        initial_reads = fixture.stream_reads
        fixture.wire = b''
        fixture.first_chunk_us = fixture.last_chunk_us = 0
        native.reset_meter()
        if store.io:
            store.io.reset_meter()
        gc.collect()
        marks['started'] = micros()
        fixture.started_us = marks['started']
        orientation, row, col = decode_edge(json.loads(final_move['data'])['e'])
        store.execute("INSERT INTO _badge_move_outbox(id,room_id,orientation,row_i,col_i) VALUES('%s','%s','%s',%d,%d)" %
                      (FINAL_MOVE, BENCH_FIXTURE['room'], orientation, row, col))
        inserted_us = elapsed(marks['started'])
        assert store.scalar("SELECT count(*) FROM _badge_move_outbox WHERE id='%s'" % FINAL_MOVE) == '1'
        assert store.scalar('SELECT count(*) FROM ps_crud') == '1'
        assert not watch.dirty, 'The app manually invalidated the SDK watch'
        steps = step_us = max_step_us = wait_us = 0
        last_step = time.ticks_ms()
        while elapsed(marks['started']) < 60000000:
            feed()
            if BENCH_CONFIG['pace_ms']:
                wait_started = micros()
                while time.ticks_diff(time.ticks_ms(), last_step) < BENCH_CONFIG['pace_ms']:
                    time.sleep_ms(1)
                wait_us += elapsed(wait_started)
            now = time.ticks_ms()
            delta = time.ticks_diff(now, last_step)
            last_step = now
            step_started = micros()
            sync.step(delta)
            duration = elapsed(step_started)
            step_us += duration
            max_step_us = max(max_step_us, duration)
            steps += 1
            assert sync.phase not in ('blocked', 'revoked'), (sync.phase, sync.last_error)
            value = watch.read()
            if value is not None and value['local_moves']:
                assert watch.identity in notifications, 'Mutation did not trigger an SDK table watch'
                if marks['optimistic_watch_us'] is None:
                    marks['optimistic_watch_us'] = elapsed(marks['started'])
                    assert marks['rpc_accepted_us'] is None, 'Optimistic query was delayed until acceptance'
                    notifications[:] = []
            if (value is not None and value['local_moves'] == 0 and marks['rpc_accepted_us'] is not None
                    and sync.full_generation > initial_full and sync.synced):
                assert watch.identity in notifications, 'SDK reconciliation did not trigger a table watch'
                assert store.has_move(FINAL_MOVE, BENCH_FIXTURE['room'])
                assert store.scalar('SELECT count(*) FROM ps_crud') == '0'
                marks['reconciled_watch_us'] = elapsed(marks['started'])
                break
        wall_us = elapsed(marks['started'])
        assert marks['reconciled_watch_us'] is not None, ('No SDK reconciliation', sync.phase, sync.last_error)
        assert len(attempts) == 1 and fixture.reconnects == 0
        assert store.scalar('SELECT count(*) FROM _badge_move_outbox') == '0'
        report = {'variant': 'native', 'storage': BENCH_CONFIG['storage'], 'repetition': repetition,
                  'pace_ms': BENCH_CONFIG['pace_ms'], 'wall_us': wall_us,
                  'wire_bytes': len(final_wire), 'wire_sha256': wire_sha,
                  'fixture_sha256': BENCH_CONFIG['fixture_sha256'],
                  'inserted_us': inserted_us, 'optimistic_watch_us': marks['optimistic_watch_us'],
                  'rpc_accepted_us': marks['rpc_accepted_us'], 'reconciled_watch_us': marks['reconciled_watch_us'],
                  'initial_full_generation': initial_full, 'full_generation': sync.full_generation,
                  'foreground_steps': steps, 'step_cpu_us': step_us, 'max_step_us': max_step_us,
                  'explicit_pace_wait_us': wait_us, 'http_requests': fixture.requests - initial_requests,
                  'stream_reads': fixture.stream_reads - initial_reads, 'stream_reconnects': fixture.reconnects,
                  'rpc_attempts': len(attempts), 'native': native.report(),
                  'vfs': store.io.report() if store.io else None,
                  'host_compatibility_check': bool(globals().get('ORDINARY_HOST_CHECK', False)),
                  'scope': 'Ordinary SDK INSERT, optimistic query watch, SDK upload/completion and covering full reconciliation. Offline fixture RPC. No pending journal/receipts/manual watch invalidation.'}
        validate(store)
        report['validated_exact_raw_uuid_room_empty_crud_quick_check'] = True
        report['sdk_optimistic_and_reconciliation_watches_observed'] = True
        print('SDK_MUTATION_RESULT', json.dumps(report))
    finally:
        if watch is not None:
            watch.close()
        if sync is not None:
            sync.close()
        if store is not None:
            store.close()
            if BENCH_CONFIG['storage'] == 'flash' and database_module._stat(store.root) is not None:
                cleanup_flash(store)
        feed()


database_module.ShadowVFS = MeterVFS
ordinary_native = TimedNative(load_native(feed))
for ordinary_repetition in range(BENCH_CONFIG['repetitions']):
    ordinary_case(ordinary_repetition, ordinary_native)
