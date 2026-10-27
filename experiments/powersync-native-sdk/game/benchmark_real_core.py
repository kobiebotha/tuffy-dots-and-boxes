"""Host full-publication baseline through the current Python SyncClient.

Uses the cached pinned SQLite/core qualifier and a fresh temporary database for
each trial. No network, badge state, package output or native build is touched.
Database/schema/start/checkpoint metadata and output validation are outside timing.
"""
import argparse
import ctypes
import gc
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "badge/lib"), str(ROOT / "badge/app/dots_boxes"),
                str(ROOT / "sqlite-shadow-vfs"), str(ROOT / "native-sqlite/tests")]
from native_sqlite_host import HostCore, LIBRARY
from powersync.http import PENDING
from powersync.sync import Connector, SyncClient
from powersync.database import DATABASE, SQLITE_CACHE_PAGES
from dots_storage import Store

spec = importlib.util.spec_from_file_location("download_harness", ROOT / "badge/tools/benchmark_download.py")
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


class Credentials(Connector):
    def fetch_credentials(self):
        return "deterministic-offline-fixture"

    def invalidate_credentials(self):
        raise AssertionError("Unexpected credential refresh")


class Stream:
    status = 200

    def __init__(self, *args, **kwargs):
        self.parts = []
        self.reads = 0

    def step(self):
        self.reads += 1
        return self.parts.pop(0) if self.parts else PENDING

    def close(self):
        pass


def case(room, operations, wire, metadata):
    with tempfile.TemporaryDirectory(prefix="python-sdk-real-core-") as directory:
        native = HostCore()
        store = Store(native=native, root=directory + "/dots").open()
        sync = SyncClient(store, Credentials(), "https://fixture.invalid",
                          {"name": "badge_game_v2", "params": {"room_id": room}}, (),
                          http_factory=Stream)
        try:
            for _ in range(20):
                sync.step(16)
                if sync.phase == "stream":
                    break
            assert sync.phase == "stream", (sync.phase, sync.last_error)
            sync.stream.parts = [json.dumps(metadata).encode() + b"\n"]
            sync.step(16)
            sync._flush()
            assert not sync.synced
            assert store.scalar("SELECT count(*) FROM moves") == "0"
            assert store.scalar("SELECT count(*) FROM rooms") == "0"
            sync.stream.parts = [wire[index:index + 2048] for index in range(0, len(wire), 2048)]
            sync.stream.reads = 0
            transactions = native.transactions
            memory = native.library.sqlite3_memory_highwater
            memory.argtypes = [ctypes.c_int]
            memory.restype = ctypes.c_longlong
            memory(1)
            gc.collect()
            steps = 0
            started = time.perf_counter_ns()
            while not sync.synced and steps < 1000:
                sync.step(16)
                steps += 1
                assert sync.phase == "stream", (sync.phase, sync.last_error)
            elapsed = time.perf_counter_ns() - started
            peak_sqlite = memory(0)
            assert sync.synced, sync.last_error
            # Check every raw field, including the immutable IDs and room binding.
            rows = []
            for offset in range(0, 49, 16):
                rows.extend(store.execute("SELECT id,r,e,n FROM moves ORDER BY n LIMIT 16 OFFSET %d" % offset))
            expected_moves = []
            expected_rooms = []
            for op in operations:
                body = json.loads(op["data"])
                if op["object_type"] == "moves":
                    expected_moves.append([op["object_id"], body["r"], str(body["e"]), str(body["n"])])
                else:
                    expected_rooms.append([op["object_id"]] + [str(body[key]) for key in ("s", "p", "a", "b", "w", "n")])
            assert rows == expected_moves, "Published move fields changed"
            assert store.execute("SELECT id,s,p,a,b,w,n FROM rooms") == expected_rooms
            assert not store.has_local_writes()
            assert store.scalar("PRAGMA quick_check") == "ok"
            return {"elapsed_us": elapsed / 1000, "foreground_steps": steps,
                    "transport_calls": sync.stream.reads,
                    "transactions": native.transactions - transactions,
                    "sqlite_peak_bytes": peak_sqlite,
                    "database_bytes": Path(store.root, DATABASE).stat().st_size}
        finally:
            sync.close()
            store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=31)
    args = parser.parse_args()
    room, operations, wire = harness.fixture()
    metadata = {"checkpoint": {"last_op_id": "50", "buckets": [
        {"bucket": "fixture", "checksum": 0, "priority": 3, "count": 50,
         "subscriptions": [{"sub": 0}]}],
        "streams": [{"name": "badge_game_v2", "is_default": False, "errors": []}]}}
    samples = [case(room, operations, wire, metadata) for _ in range(args.repetitions)]
    ordered = sorted(item["elapsed_us"] for item in samples)
    result = {
        "scope": "Host current custom Python SyncClient to real SQLite/core full publication; no Wi-Fi, TLS or ShadowVFS/flash. Fresh POSIX file database each trial. Timing excludes database/schema/start/checkpoint metadata and output validation.",
        "python": sys.version,
        "host": platform.platform(),
        "library_sha256": hashlib.sha256(LIBRARY.read_bytes()).hexdigest(),
        "wire_sha256": hashlib.sha256(wire).hexdigest(),
        "wire_bytes": len(wire), "operations": len(operations), "fragment_bytes": 2048,
        "checkpoint_metadata": metadata,
        "arena_bytes": 262144, "sqlite_profile": "native-sqlite/tests/host_config.h",
        "journal_mode": "DELETE", "synchronous": "FULL", "cache_pages": SQLITE_CACHE_PAGES,
        "core_allocator": "PowerSync core uses SQLite allocator in cached native host qualifier; Python heap excluded.",
        "output_validation": "All 49 published move IDs/room/edge/index fields, one room's complete fields, zero ps_crud and quick_check=ok.",
        "median_us": statistics.median(ordered),
        "p95_us": ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)],
        "min_us": min(ordered), "max_us": max(ordered),
        "samples": samples,
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
