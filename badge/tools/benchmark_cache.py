#!/usr/bin/env python3
"""Real 256KiB core/cache sweep with host file I/O counters. No app mutations.

First generate fixtures with benchmark-projection.mjs --service-only:
  node powersync-cloud/tests/benchmark-projection.mjs --service-only --output=/tmp/projection.json
  python3 badge/tools/benchmark_cache.py /tmp/projection.json --expanded --output=/tmp/cache-v2.json
The C wrapper links the existing pinned fixture library (never rebuilds it).
Its 512-byte sector, no mmap/device caps/file-control shortcuts match badge VFS
behavior. POSIX latency and Python callback cost are not Tufty timings.
"""
import argparse
import base64
import ctypes as c
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid

import benchmark_native as benchmark

METRICS = ("open", "read", "write", "sync", "truncate", "close", "size", "lock",
           "unlock", "access", "delete", "readBytes", "writeBytes", "deleteSync")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixtures")
    parser.add_argument("--output", required=True)
    parser.add_argument("--expanded", action="store_true", help="Also check cold history, UI reads, and reopen persistence")
    args = parser.parse_args()
    inputs = json.loads(Path(args.fixtures).read_text())
    if "fixtures" not in inputs:
        inputs = json.loads(Path(inputs["methodology"]["fixtures"]).read_text())
    fixture = next(item for item in inputs["fixtures"] if item["name"] == "v2_safe_r")
    original_store = benchmark.storage.Store
    original_core = benchmark.HostCore
    results = []
    migration_checks = None
    with tempfile.TemporaryDirectory(prefix="dots-cache-v1-") as folder:
        library = Path(folder) / "counting.dylib"
        header = benchmark.ROOT.parent / "powerbadge/native/sqlite-hello/vendor/sqlite-littlefs-vfs/vendor/sqlite"
        subprocess.run(["cc", "-std=c11", "-O1", "-dynamiclib", "-I", str(header),
                        str(Path(__file__).with_name("counting_vfs.c")), str(benchmark.LIBRARY),
                        "-o", str(library)], check=True, capture_output=True)
        counters = c.CDLL(str(library))
        counters.counts_get.argtypes = [c.c_int, c.c_int]
        counters.counts_get.restype = c.c_uint64

        def counts():
            return {name: counters.counts_get(0, index) for index, name in enumerate(METRICS)}

        class CountedCore(original_core):
            def __init__(self):
                super().__init__()
                self._check(counters.counts_install())

        benchmark.HostCore = CountedCore
        for pages in (8, 16, 24, 32, 48):
            checkpoints = []
            last_counts = {}
            reopen = {}
            ui_reads = False

            class CacheStore(original_store):
                def open(self):
                    super().open()  # Creation/migration retains the qualified eight-page cache.
                    self._exec("PRAGMA cache_size=%d" % pages)
                    return self

                def start_sync(self, *args, **kwargs):
                    result = super().start_sync(*args, **kwargs)
                    counters.counts_reset()
                    last_counts.clear()
                    last_counts.update(counts())
                    return result

                def control_lines(self, payloads):
                    result = super().control_lines(payloads)
                    if any("checkpoint_complete" in json.loads(line) for line in payloads):
                        if ui_reads:
                            self.room(inputs["roomId"])
                            self.moves(inputs["roomId"])
                            for line in payloads:
                                for op in json.loads(line).get("data", {}).get("data", []):
                                    if op.get("op") == "PUT" and op.get("object_type") == "moves":
                                        canonical = str(uuid.UUID(bytes=base64.b64decode(op["object_id"])))
                                        assert self.has_move(canonical, inputs["roomId"])
                        now = counts()
                        checkpoints.append({name: now[name] - last_counts[name] for name in METRICS})
                        last_counts.update(now)
                    return result

                def close(self):
                    qualify = args.expanded and self.opened
                    if qualify:
                        room = self.room(inputs["roomId"])
                        moves = self.moves(inputs["roomId"])
                    super().close()
                    if qualify:
                        counters.counts_reset()
                        # Reopen through the production eight-page checks, then
                        # apply the proposed warm cache and verify persistence.
                        reopened = CacheStore(native=CountedCore(), root=self.root).open()
                        try:
                            assert reopened.room(inputs["roomId"]) == room
                            assert reopened.moves(inputs["roomId"]) == moves
                            assert not reopened.has_local_writes()
                            assert not reopened.bind_account(inputs["userId"])
                            reopen.update({"passed": True, "moves": len(moves), "io": counts(),
                                           "peakBytes": int(reopened.native.library.sqlite3_memory_highwater(0))})
                        finally:
                            original_store.close(reopened)

            benchmark.storage.Store = CacheStore
            scenarios = ("fresh", "history_cold", "history_live", "history_live_ui") if args.expanded else ("fresh", "history_live")
            for scenario in scenarios:
                checkpoints.clear()
                reopen.clear()
                ui_reads = scenario == "history_live_ui"
                case = fixture["scenarios"]["history_live" if ui_reads else scenario]
                result = benchmark.run(fixture, scenario, case, inputs["userId"])
                # Last checkpoint snapshot excludes final validation reads and database close.
                result.update({"cachePages": pages, "io": dict(last_counts),
                               "checkpointIo": list(checkpoints)})
                if args.expanded:
                    result["reopen"] = dict(reopen)
                results.append(result)
                print(json.dumps({key: value for key, value in result.items() if key != "checkpointIo"}))
        benchmark.HostCore, benchmark.storage.Store = original_core, original_store
        if args.expanded:
            import test_sync
            original_test_core, original_test_store = test_sync.HostCore, test_sync.Store
            test_sync.HostCore = CountedCore

            class MigrationStore(original_store):
                def _exec(self, sql):
                    if sql == "SELECT powersync_clear(0)":
                        assert self.native.exec("PRAGMA cache_size") == [["8"]], "Migration must use cache8"
                    return super()._exec(sql)

                def open(self):
                    super().open()
                    self._exec("PRAGMA cache_size=32")
                    return self

            test_sync.Store = MigrationStore

            class MigrationChecks(test_sync.RealCoreTests):
                def install_legacy_cache(self, version):
                    # Fixtures model an installed legacy badge at cache8.
                    self.store._exec("PRAGMA cache_size=8")
                    return super().install_legacy_cache(version)

            names = (
                "test_version_one_and_two_migrations_preserve_identity_pending_and_replace_only_cache",
                "test_failed_compact_migration_rolls_back_old_cache_owner_and_version",
                "test_migrated_high_water_database_reuses_freelist_pages_for_sync",
            )
            try:
                outcome = unittest.TestResult()
                unittest.TestSuite(MigrationChecks(name) for name in names).run(outcome)
                migration_checks = {"run": outcome.testsRun, "passed": outcome.wasSuccessful(),
                                    "failures": [trace for _, trace in outcome.failures + outcome.errors]}
                print(json.dumps({"migrationChecks": migration_checks}))
            finally:
                test_sync.HostCore, test_sync.Store = original_test_core, original_test_store
    output = {"version": 2 if args.expanded else 1, "arenaBytes": 256 * 1024,
              "methodology": "Pinned real core; compact full49 fixtures; warm cache raised only after open/migration. POSIX VFS counted with badge sector512/caps0/io methods1/filecontrol NOTFOUND. No hardware timing claims.",
              "reproduce": "python3 badge/tools/benchmark_cache.py " + args.fixtures + (" --expanded" if args.expanded else "") + " --output=" + args.output,
              "results": results, "migrationChecks": migration_checks}
    Path(args.output).write_text(json.dumps(output, indent=2) + "\n")


if __name__ == "__main__":
    main()
