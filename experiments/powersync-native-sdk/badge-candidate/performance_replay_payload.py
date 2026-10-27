"""RAM payload; configuration and fixture are prepended by performance_replay.py.

Inclusive timings overlap: sdk_poll/native_exec contain VFS work; VFS callback
time contains image_sync time. Do not add these categories together. The
measured native call time minus its VFS callbacks estimates non-VFS SDK/SQLite
work. Pace waits represent only the explicitly requested foreground cadence.
Neither mode measures Wi-Fi, TLS, rendering or physical panel latency.
"""
import gc
import json
import os
import sys
import time


def micros():
    return time.ticks_us()


def elapsed(start):
    return time.ticks_diff(micros(), start)


def feed():
    BENCH_HEARTBEAT()


if "BENCH_HEARTBEAT" not in globals():
    import machine
    _benchmark_watchdog = machine.WDT(timeout=16000)
    BENCH_HEARTBEAT = _benchmark_watchdog.feed

_app = "/system/apps/" + ("dots_boxes_native" if BENCH_CONFIG["variant"] == "native" else "dots_boxes")
sys.path.insert(0, _app)
sys.path.insert(0, _app + "/powersync")
from dots_storage import Store, RAW_TABLE_SQL
from powersync import database as database_module
from powersync.http import PENDING, Gateway
from powersync.runtime import load_native
from powersync.sync import Connector, SyncClient
from sqlite_shadow_vfs import ShadowVFS

assert bool(getattr(SyncClient, "NATIVE_SDK", False)) == (BENCH_CONFIG["variant"] == "native")


class MeterVFS(ShadowVFS):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reset_meter()

    def reset_meter(self):
        self.calls = [0] * 11
        self.times = [0] * 11
        self.image_writes = self.image_bytes = self.image_sync_us = 0
        self.journal_bytes = 0

    def _sync_db(self, stream):
        start = micros()
        if self.db_dirty:
            self.image_writes += 1
            self.image_bytes += self.db_size
        try:
            return super()._sync_db(stream)
        finally:
            self.image_sync_us += elapsed(start)

    def __call__(self, op, item, arg1, arg2):
        start = micros()
        if op == 3 and item != self.db_handle:
            self.journal_bytes += len(arg2)
        try:
            return super().__call__(op, item, arg1, arg2)
        finally:
            self.calls[op] += 1
            self.times[op] += elapsed(start)

    def report(self):
        return {"callback_counts": list(self.calls), "callback_us": list(self.times),
                "callback_total_us": sum(self.times),
                "database_image_writes": self.image_writes,
                "database_image_bytes": self.image_bytes,
                "database_image_sync_us": self.image_sync_us,
                "journal_write_bytes": self.journal_bytes,
                "op_names": ["open", "close", "read", "write", "truncate", "sync",
                             "size", "delete", "access", "random", "time"]}


class TimedNative:
    def __init__(self, real):
        self.real = real
        self.io = None
        self.reset_meter()

    def reset_meter(self):
        self.exec_count = self.exec_us = self.sql_commits = 0
        self.poll_count = self.poll_us = self.poll_max_us = 0
        self.reply_count = self.reply_us = self.start_us = 0

    def __getattr__(self, name):
        return getattr(self.real, name)

    def configure(self, io):
        self.io = io
        return self.real.configure(io)

    def exec(self, sql):
        start = micros()
        try:
            result = self.real.exec(sql)
            if sql.strip().upper().startswith("COMMIT"):
                self.sql_commits += 1
            return result
        finally:
            self.exec_count += 1
            self.exec_us += elapsed(start)

    def sdk_start(self, *args):
        start = micros()
        try:
            return self.real.sdk_start(*args)
        finally:
            self.start_us += elapsed(start)

    def sdk_poll(self, *args):
        start = micros()
        try:
            return self.real.sdk_poll(*args)
        finally:
            duration = elapsed(start)
            self.poll_count += 1
            self.poll_us += duration
            self.poll_max_us = max(self.poll_max_us, duration)

    def sdk_reply(self, *args):
        start = micros()
        try:
            return self.real.sdk_reply(*args)
        finally:
            self.reply_count += 1
            self.reply_us += elapsed(start)

    def report(self):
        return {"exec_calls": self.exec_count, "exec_us_inclusive": self.exec_us,
                "python_sql_commits": self.sql_commits, "sdk_start_us": self.start_us,
                "sdk_poll_calls": self.poll_count, "sdk_poll_us_inclusive": self.poll_us,
                "sdk_poll_max_us": self.poll_max_us,
                "sdk_reply_calls": self.reply_count, "sdk_reply_us": self.reply_us}


class BenchmarkStore(Store):
    def _check_reserve(self):
        if BENCH_CONFIG["storage"] != "memory":
            return super()._check_reserve()


class Credentials(Connector):
    def __init__(self):
        self.publications = 0

    def fetch_credentials(self):
        return "deterministic-offline-fixture"

    def invalidate_credentials(self):
        raise AssertionError("Unexpected credential refresh")

    def checkpoint_applied(self, store, full_checkpoint):
        if full_checkpoint:
            self.publications += 1
        return True


class Response:
    def __init__(self, streaming):
        self.content_type = "application/x-ndjson" if streaming else "application/json"


class FixtureNetwork:
    def __init__(self, wire):
        self.wire = wire
        self.stream = None
        self.streams = []
        self.requests = self.stream_reads = self.json_reads = 0
        self.stream_bytes = 0
        self.started_us = self.first_chunk_us = self.last_chunk_us = 0
        self.reconnects = 0

    def factory(self, *args, **kwargs):
        if isinstance(args[0], dict):
            request = args[0]
            url, body = request["url"], json.loads(request.get("body") or "{}")
        else:
            url, body = args[:2]
        self.requests += 1
        task = FixtureHTTP(self, url, body)
        if task.streaming:
            if self.stream is not None:
                assert BENCH_CONFIG["scenario"] == "warm_upload", "Unexpected reconnect"
                self.reconnects += 1
            self.stream = task
            self.streams.append(task)
        return task

    def json_factory(self, *args, **kwargs):
        return Gateway(*args, factory=self.factory, **kwargs)


class FixtureHTTP:
    status = 200

    def __init__(self, network, url, body):
        self.network = network
        self.streaming = url.endswith("/sync/stream")
        assert self.streaming or url.endswith("/sync/checkpoint-request"), url
        self.response = Response(self.streaming)
        self.offset = 0
        self.closed = False
        if self.streaming:
            self.data = network.wire
        else:
            self.data = json.dumps({"data": {"checkpoint_request_id": body["checkpoint_request_id"]}}).encode()

    def step(self):
        if self.closed:
            return None
        if self.streaming:
            self.network.stream_reads += 1
        else:
            self.network.json_reads += 1
        if self.offset >= len(self.data):
            return PENDING if self.streaming else None
        chunk = self.data[self.offset:self.offset + 2048]
        self.offset += len(chunk)
        if self.streaming:
            self.network.stream_bytes += len(chunk)
            if not self.network.first_chunk_us:
                self.network.first_chunk_us = elapsed(self.network.started_us)
            self.network.last_chunk_us = elapsed(self.network.started_us)
        return chunk

    def close(self):
        self.closed = True


def fixture_wire():
    if BENCH_CONFIG["scenario"] == "cold":
        return (json.dumps(BENCH_FIXTURE["checkpoint"]) + "\n" + BENCH_FIXTURE["wire"]).encode(), 1
    lines = []
    room_op = BENCH_FIXTURE["operations"][-1]
    for index, move in enumerate(BENCH_FIXTURE["operations"][:-1]):
        count = index + 1
        room = dict(room_op)
        data = json.loads(room["data"])
        data["n"] = count
        if count < 49:
            data["s"], data["a"], data["b"] = 1, 0, 0
        room["data"] = json.dumps(data)
        changed = dict(move)
        changed["op_id"] = str(count * 2 - 1)
        room["op_id"] = str(count * 2)
        checkpoint = json.loads(json.dumps(BENCH_FIXTURE["checkpoint"]))
        checkpoint["checkpoint"]["last_op_id"] = str(count * 2)
        checkpoint["checkpoint"]["buckets"][0]["count"] = count + 1
        lines.append(json.dumps(checkpoint))
        # MicroPython dictionaries do not preserve insertion order. The
        # original streaming parser requires bucket before its data array.
        lines.append('{"data":{"bucket":"fixture","data":' + json.dumps([changed, room]) + '}}')
        lines.append(json.dumps({"checkpoint_complete": {"last_op_id": str(count * 2)}}))
    return ("\n".join(lines) + "\n").encode(), 49


def initialize_memory(store):
    def memory_io(op, item, arg1, arg2):
        if op == 9:
            return 0, os.urandom(arg1)
        if op == 10:
            now = int(time.time())
            if time.gmtime(0)[0] == 2000:
                now += 946684800
            return 0, (now // 86400, now % 86400)
        return 10, None
    store.native.configure(memory_io)
    store.native.open(":memory:")
    store.opened = True
    for statement in ("PRAGMA page_size=1024", "PRAGMA cache_size=8", "PRAGMA temp_store=MEMORY",
                      "PRAGMA synchronous=FULL", "BEGIN IMMEDIATE"):
        store.execute(statement)
    store._initialize(None)
    store.execute("COMMIT")
    store.execute("PRAGMA cache_size=32")


def clear_flash_database(root):
    assert root.startswith("/state/") and root.count("/") == 2, "Use one explicit /state directory"
    for name in ("state.sqlite", "state.sqlite-journal"):
        path = root + "/" + name
        try:
            os.remove(path)
        except OSError as error:
            if error.args[0] != 2:
                raise
    os.sync()


def cleanup_flash(store):
    clear_flash_database(store.root)
    if store.root == "/state/dots_boxes_benchmark":
        names = os.listdir(store.root)
        assert names == ["owner.txt"], "Unexpected benchmark scratch files; retained"
        with open(store.root + "/owner.txt") as source:
            assert source.read(128) == store.owner
        os.remove(store.root + "/owner.txt")
        os.rmdir(store.root)
        os.sync()


def validate(store):
    rows = []
    for offset in range(0, 49, 16):
        rows.extend(store.execute("SELECT id,r,e,n FROM moves ORDER BY n LIMIT 16 OFFSET %d" % offset))
    expected = []
    for operation in BENCH_FIXTURE["operations"][:-1]:
        data = json.loads(operation["data"])
        expected.append([operation["object_id"], data["r"], str(data["e"]), str(data["n"])])
    assert rows == expected, "Raw move UUID/room/edge/order changed"
    room = BENCH_FIXTURE["operations"][-1]
    data = json.loads(room["data"])
    assert store.execute("SELECT id,s,p,a,b,w,n FROM rooms") == [
        [room["object_id"]] + [str(data[key]) for key in ("s", "p", "a", "b", "w", "n")]], "Raw room changed"
    assert store.scalar("SELECT count(*) FROM ps_crud") == "0", "Unexpected CRUD"
    assert store.scalar("PRAGMA quick_check") == "ok"
    move = "00000000-0000-0000-0000-000000000031"
    assert store.has_move(move, BENCH_FIXTURE["room"]), "Exact UUID/room confirmation failed"
    assert not store.has_move(move, "550e8400-e29b-41d4-a716-446655440001"), "Wrong room confirmed"
    assert not store.has_move("00000000-0000-0000-0000-00000000ffff", BENCH_FIXTURE["room"]), "Wrong UUID confirmed"


def run_warm_upload(store, native, repetition):
    """Real game connector and SDK queue; fixture RPC and RAM session saves.

    Warm the first 48 moves before timing. Queue the 49th UUID through the
    normal SDK outbox, accept its RPC, then publish its exact room/move under a
    covering write checkpoint. Normal SDK checkpoint/reconnect scheduling runs.
    """
    from dots_sync import GameConnector
    from dots_session import fresh_session, validate_session
    from dots_codec import decode_edge

    def wire_for(operations, last_op, count, write=None):
        checkpoint = json.loads(json.dumps(BENCH_FIXTURE["checkpoint"]))
        checkpoint["checkpoint"]["last_op_id"] = str(last_op)
        checkpoint["checkpoint"]["buckets"][0]["count"] = count
        if write is not None:
            checkpoint["checkpoint"]["write_checkpoint"] = write
        return (json.dumps(checkpoint) + '\n{"data":{"bucket":"fixture","data":' + json.dumps(operations) + '}}'
                + "\n" + json.dumps({"checkpoint_complete": {"last_op_id": str(last_op)}}) + "\n").encode()

    room = dict(BENCH_FIXTURE["operations"][-1])
    before_room = json.loads(room["data"])
    before_room["n"], before_room["s"], before_room["a"], before_room["b"] = 48, 1, 0, 0
    room["data"] = json.dumps(before_room)
    room["op_id"] = "49"
    initial_wire = wire_for(BENCH_FIXTURE["operations"][:48] + [room], 49, 49)
    last = dict(BENCH_FIXTURE["operations"][-2])
    last["op_id"] = "50"
    final_room = dict(BENCH_FIXTURE["operations"][-1])
    final_room["op_id"] = "51"
    final_wire = wire_for([last, final_room], 51, 50, "100")
    fixture = FixtureNetwork(initial_wire)
    attempts = []
    marks = {"started": 0, "rpc_accepted_us": None, "pending_cleared_us": None,
             "publication_start": 0, "publication_cleared": None}

    class Records:
        def __init__(self):
            self.record = dict(fresh_session("550e8400-e29b-41d4-a716-446655440002"),
                               user_id="550e8400-e29b-41d4-a716-446655440003",
                               room_id=BENCH_FIXTURE["room"], seat=1,
                               access_token="fixture-only", refresh_token="fixture-only",
                               expires_at=int(time.time()) + 3600)
            validate_session(self.record)

        def save(self, value):
            validate_session(value)
            pending = self.record["pending_move"]
            if pending is not None and value["pending_move"] is None:
                publication = getattr(sync, "full_generation", sync.generation)
                assert publication > marks["publication_start"], "Cleared without fresh full publication"
                assert store.has_move(pending["id"], value["room_id"]), "Cleared before exact committed publication"
                marks["pending_cleared_us"] = elapsed(marks["started"])
                marks["publication_cleared"] = publication
            self.record = value

    records = Records()

    class RPC:
        def __init__(self, url, *args, **kwargs):
            assert url == "https://backend.fixture.invalid/move"
            self.calls = 0

        def start(self, body):
            assert body["move_id"] == "00000000-0000-0000-0000-000000000031"
            assert body["target"] == BENCH_FIXTURE["room"]
            attempts.append(body["move_id"])

        def step(self):
            self.calls += 1
            if self.calls == 1:
                return PENDING
            assert records.record["pending_move"] is not None, "RPC accepted pending incorrectly"
            marks["rpc_accepted_us"] = elapsed(marks["started"])
            fixture.wire = final_wire
            for task in fixture.streams:
                if not task.closed:
                    task.data += final_wire
            return {"http_status": 200}

        def close(self):
            pass

    kwargs = {"heartbeat": feed, "json_factory": RPC}
    if BENCH_CONFIG["variant"] == "native":
        kwargs["store"] = store
    connector = GameConnector(records, "https://backend.fixture.invalid/auth",
                              "https://backend.fixture.invalid/move", "synthetic-anon-key-over-20-bytes", (), **kwargs)
    sync = SyncClient(store, connector, "https://fixture.invalid",
                      {"name": "badge_game_v2", "params": {"room_id": BENCH_FIXTURE["room"]}},
                      (), heartbeat=feed, http_factory=fixture.factory, json_factory=fixture.json_factory)
    try:
        warm_start = micros()
        while not sync.synced and elapsed(warm_start) < 30000000:
            feed()
            sync.step(1)
            assert sync.phase not in ("blocked", "revoked"), ("Warmup stopped", sync.phase, sync.last_error)
        assert sync.synced, ("Warmup failed", sync.last_error)
        assert store.scalar("SELECT count(*) FROM moves") == "48"
        fixture.wire = b""  # A reconnect resumes after the warm checkpoint.
        initial_requests, initial_reads = fixture.requests, fixture.stream_reads
        initial_json_reads = fixture.json_reads
        native.reset_meter()
        if store.io is not None:
            store.io.reset_meter()
        gc.collect()
        marks["started"] = micros()
        marks["publication_start"] = getattr(sync, "full_generation", sync.generation)
        fixture.started_us = marks["started"]
        fixture.first_chunk_us = fixture.last_chunk_us = 0
        orientation, row, col = decode_edge(json.loads(last["data"])["e"])
        pending = {"id": "00000000-0000-0000-0000-000000000031", "orientation": orientation, "row": row, "col": col}
        # Mirror Client._queue_move's required session-before-CRUD ordering.
        records.save(dict(records.record, pending_move=pending))
        if BENCH_CONFIG["variant"] == "native":
            assert store.ensure_pending_move(records.record)
            assert store.scalar("SELECT count(*) FROM ps_crud") == "1"
            assert store.scalar("SELECT count(*) FROM _badge_move_outbox") == "0"
        queued_us = elapsed(marks["started"])
        steps = wait_us = step_us = max_step_us = 0
        last_step = time.ticks_ms()
        while elapsed(marks["started"]) < 60000000:
            feed()
            if BENCH_CONFIG["pace_ms"]:
                wait_start = micros()
                while time.ticks_diff(time.ticks_ms(), last_step) < BENCH_CONFIG["pace_ms"]:
                    time.sleep_ms(1)
                wait_us += elapsed(wait_start)
            now = time.ticks_ms()
            delta = time.ticks_diff(now, last_step)
            last_step = now
            step_start = micros()
            sync.step(delta)
            duration = elapsed(step_start)
            step_us += duration
            max_step_us = max(max_step_us, duration)
            steps += 1
            assert sync.phase not in ("blocked", "revoked"), (sync.phase, sync.last_error)
            if sync.synced and records.record["pending_move"] is None:
                break
        assert records.record["pending_move"] is None and sync.synced, ("Upload confirmation failed", sync.phase, sync.last_error)
        assert marks["rpc_accepted_us"] is not None
        assert len(attempts) == 1 and marks["pending_cleared_us"] >= marks["rpc_accepted_us"]
        report = dict(BENCH_CONFIG)
        import binascii
        import hashlib
        report.update({"repetition": repetition, "wall_us": elapsed(marks["started"]),
                       "enqueue_us": queued_us, "rpc_accepted_us": marks["rpc_accepted_us"],
                       "pending_cleared_us": marks["pending_cleared_us"],
                       "publication_start": marks["publication_start"],
                       "publication_cleared": marks["publication_cleared"],
                       "foreground_steps": steps, "step_cpu_us": step_us,
                       "explicit_pace_wait_us": wait_us, "max_step_us": max_step_us,
                       "http_requests": fixture.requests - initial_requests,
                       "stream_reads": fixture.stream_reads - initial_reads,
                       "checkpoint_reads": fixture.json_reads - initial_json_reads,
                       "stream_reconnects": fixture.reconnects, "rpc_attempts": len(attempts),
                       "wire_sha256": binascii.hexlify(hashlib.sha256(final_wire).digest()).decode(),
                       "wire_bytes": len(final_wire), "fragment_bytes": 2048,
                       "first_chunk_us": fixture.first_chunk_us, "last_chunk_us": fixture.last_chunk_us,
                       "native": native.report(), "vfs": store.io.report() if store.io else None,
                       "scope": "Warm 48-move state; actual GameConnector and SDK-owned CRUD/checkpoint/reconnect. Fixture RPC; session saves in RAM. No network/TLS/rendering."})
        validate(store)
        report["validated_raw_rows_exact_uuid_room_empty_crud_quick_check"] = True
        print("PERFORMANCE_REPLAY_RESULT", json.dumps(report))
    finally:
        sync.close()


def run_case(repetition, native):
    if BENCH_CONFIG["storage"] == "flash":
        clear_flash_database(BENCH_CONFIG["root"])
    store = BenchmarkStore(native=native, root=BENCH_CONFIG["root"], heartbeat=feed)
    sync = None
    try:
        if BENCH_CONFIG["storage"] == "memory":
            initialize_memory(store)
        else:
            store.open()
        if BENCH_CONFIG["scenario"] == "warm_upload":
            return run_warm_upload(store, native, repetition)
        wire, publications = fixture_wire()
        import binascii
        import hashlib
        wire_sha = binascii.hexlify(hashlib.sha256(wire).digest()).decode()
        fixture = FixtureNetwork(wire)
        connector = Credentials()
        native.reset_meter()
        if store.io is not None:
            store.io.reset_meter()
        gc.collect()
        started = micros()
        fixture.started_us = started
        sync = SyncClient(store, connector, "https://fixture.invalid",
                          {"name": "badge_game_v2", "params": {"room_id": BENCH_FIXTURE["room"]}},
                          (), heartbeat=feed, http_factory=fixture.factory, json_factory=fixture.json_factory)
        steps = wait_us = step_us = max_step_us = 0
        last_step = time.ticks_ms()
        while steps < 10000:
            feed()
            if BENCH_CONFIG["pace_ms"]:
                wait_start = micros()
                while time.ticks_diff(time.ticks_ms(), last_step) < BENCH_CONFIG["pace_ms"]:
                    time.sleep_ms(1)
                wait_us += elapsed(wait_start)
            now = time.ticks_ms()
            delta = time.ticks_diff(now, last_step)
            last_step = now
            step_start = micros()
            sync.step(delta)
            duration = elapsed(step_start)
            step_us += duration
            max_step_us = max(max_step_us, duration)
            steps += 1
            assert sync.phase not in ("blocked", "revoked"), (sync.phase, sync.last_error)
            observed_publications = getattr(sync, "full_generation", sync.generation)
            if sync.synced and observed_publications == publications:
                break
        wall_us = elapsed(started)
        assert sync.synced and observed_publications == publications, ("No publication", sync.last_error)
        assert fixture.stream.offset == len(wire), "Unconsumed wire"
        report = dict(BENCH_CONFIG)
        report.update({"repetition": repetition, "wire_sha256": wire_sha,
                       "wire_bytes": len(wire), "fragment_bytes": 2048,
                       "wall_us": wall_us, "step_cpu_us": step_us, "explicit_pace_wait_us": wait_us,
                       "foreground_steps": steps, "max_step_us": max_step_us,
                       "http_requests": fixture.requests, "stream_reads": fixture.stream_reads,
                       "checkpoint_reads": fixture.json_reads,
                       "first_chunk_us": fixture.first_chunk_us, "last_chunk_us": fixture.last_chunk_us,
                       "publications": observed_publications, "publication_callbacks": connector.publications,
                       "native": native.report(),
                       "vfs": store.io.report() if store.io is not None else None,
                       "scope": "Installed actual code; deterministic offline replay. Inclusive timers overlap. No network/TLS/rendering."})
        # Validation and shutdown stay outside the measured window and counters.
        validate(store)
        report["validated_raw_rows_exact_uuid_room_empty_crud_quick_check"] = True
        print("PERFORMANCE_REPLAY_RESULT", json.dumps(report))
    finally:
        if sync is not None:
            sync.close()
        store.close()
        if BENCH_CONFIG["storage"] == "flash":
            cleanup_flash(store)
        feed()


def apply_keep_stream_control():
    """Change one installed adapter policy in RAM after baseline measurement.

    Preserve the original method verbatim except its unconditional stream-close
    branch. The single-TLS fallback still closes the stream. No SDK, checkpoint,
    CRUD, receipt, confirmation or installed file is changed by this control.
    """
    assert BENCH_CONFIG["variant"] == "native" and BENCH_CONFIG["scenario"] == "warm_upload"
    module = sys.modules[SyncClient.__module__]
    with open(module.__file__) as source_file:
        source = source_file.read()
    start = source.index("    def _service_http(self):\n")
    end = source.index("    def _credentials(self):\n", start)
    method = "\n".join(line[4:] if line.startswith("    ") else line
                       for line in source[start:end].split("\n"))
    old = '''        if self._active_http is not None and self._is_stream(self._active_http):
            # A later checkpoint needs the only TLS budget. Report the
            # transport closure to Rust; its supervisor owns reconnects.
            identity = self._active_http
            self._reply({"op": "error", "id": identity})
            self._close_http(identity)
'''
    new = '''        if self._active_http is not None and self._is_stream(self._active_http):
            if self._single_tls:
                identity = self._active_http
                self._reply({"op": "error", "id": identity})
                self._close_http(identity)
            else:
                self._active_http = None
'''
    assert method.count(old) == 1, "Installed adapter changed; causal-control match refused"
    changed = method.replace(old, new, 1)
    scope = dict(module.__dict__)
    exec(changed, scope)
    SyncClient._service_http = scope["_service_http"]
    import binascii
    import hashlib
    BENCH_CONFIG["control_installed_sync_sha256"] = binascii.hexlify(hashlib.sha256(source.encode()).digest()).decode()
    BENCH_CONFIG["control_scope"] = "RAM-only _service_http policy; keep active stream for checkpoint HTTP unless single-TLS fallback"


if BENCH_CONFIG.get("keep_stream", False):
    apply_keep_stream_control()

database_module.ShadowVFS = MeterVFS
_native = TimedNative(load_native(feed))
for _repetition in range(BENCH_CONFIG["repetitions"]):
    run_case(_repetition, _native)
