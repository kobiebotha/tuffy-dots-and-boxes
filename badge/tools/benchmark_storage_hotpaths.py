#!/usr/bin/env python3
"""Compare storage layouts with the pinned core and actual production Python VFS.

Only temporary host databases and the named report are written. Host timings
include os.sync and do not predict badge flash, MicroPython, or network latency.
"""
import argparse
import copy
import hashlib
import importlib.util
import json
from collections import Counter
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "badge/tools"), str(ROOT / "badge/tests")]
import benchmark_native as bench
from game import Game
from vfs_fixture import PythonVFSCore


def load_storage(path):
    spec = importlib.util.spec_from_file_location("storage_candidate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_case(source, fixture, inputs, scenario_name, layout, page, cache, parsed):
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    storage = load_storage(source)
    storage.PAGE_BYTES = page
    storage.MAX_SYNC_PAGES = 96 * 1024 // page
    storage.SQLITE_CACHE_PAGES = cache
    original_store, original_vfs = storage.Store, storage.BadgeVFS
    physical, calls = Counter(), Counter()
    timings = Counter()
    baseline_json = storage.json

    class CountJSON:
        def loads(self, value):
            calls["json.loads"] += 1
            return baseline_json.loads(value)

        def dumps(self, value, *args, **kwargs):
            calls["json.dumps"] += 1
            return baseline_json.dumps(value, *args, **kwargs)

    class Core(PythonVFSCore):
        def exec(self, sql):
            calls["native.exec"] += 1
            return super().exec(sql)

    class VFS(original_vfs):
        def _sync_db(self, stream):
            if self.db_dirty:
                physical["databaseImageWrites"] += 1
                physical["databaseImageBytes"] += self.db_size
            return super()._sync_db(stream)

    class Store(original_store):
        def start_sync(self, *args, **kwargs):
            result = super().start_sync(*args, **kwargs)
            calls.clear()
            physical.clear()
            self.native.reset_stats()
            return result

        def control_lines(self, payloads):
            started = time.process_time_ns()
            records = [json.loads(p) for p in payloads]
            if parsed:
                # Framing already decodes these records on the production path.
                result = super().control_lines(payloads, parsed=records)
            else:
                result = super().control_lines(payloads)
            timings["controlCpuNanoseconds"] += time.process_time_ns() - started
            if scenario_name.endswith("_ui") and any(event["full_checkpoint"] for event in result):
                self.room(inputs["roomId"])
                self.moves(inputs["roomId"])
            return result

        def close(self):
            if not self.opened:
                return super().close()
            result["calls"] = dict(calls)
            result["physical"] = dict(physical)
            result["vfs"] = dict(self.native.stats)
            result["controlCpuMilliseconds"] = round(timings["controlCpuNanoseconds"] / 1000000, 4)
            expected_room, expected_moves = self.room(inputs["roomId"]), self.moves(inputs["roomId"])
            try:
                game = Game()
                assert len(expected_moves) == 49, "Incomplete candidate game"
                for move in expected_moves:
                    assert game.claim(move["orientation"], move["row"], move["col"]) is not None
                assert game.finished and game.scores == [10, 10]
                assert not self.has_local_writes()
                assert self.native.exec("PRAGMA quick_check") == [["ok"]]
            finally:
                super().close()
            reopened = original_store(native=Core(), root=self.root).open()
            try:
                assert reopened.room(inputs["roomId"]) == expected_room
                assert reopened.moves(inputs["roomId"]) == expected_moves
                assert not reopened.has_local_writes()
                result["reopen49MovesPassed"] = True
            finally:
                reopened.close()

    candidate = copy.deepcopy(fixture)
    candidate["name"] = layout
    if layout.startswith("without_rowid"):
        candidate["rawSql"] = [sql + " WITHOUT ROWID" for sql in candidate["rawSql"]]
    if layout.endswith("_index"):
        candidate["rawSql"].append("CREATE INDEX moves_room_order ON moves(r,n)")
    storage.json, storage.Store, storage.BadgeVFS = CountJSON(), Store, VFS
    bench.storage, bench.HostCore = storage, Core
    result = {}
    result.update(bench.run(candidate, scenario_name,
        candidate["scenarios"][scenario_name.removesuffix("_ui")], inputs["userId"]))
    result.update({"source": str(source), "sourceSha256": source_sha256,
                   "pageBytes": page, "cachePages": cache, "parsedRecords": parsed})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixtures")
    parser.add_argument("--output", required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--layouts-only", action="store_true")
    parser.add_argument("--selected-only", action="store_true")
    parser.add_argument("--scenarios", nargs="+", choices=("fresh", "history_cold", "history_live_ui"),
                        default=("fresh", "history_live_ui"))
    args = parser.parse_args()
    inputs = json.loads(Path(args.fixtures).read_text())
    fixture = next(item for item in inputs["fixtures"] if item["name"] == "v2_raw_all")
    current = ROOT / "badge/app/dots_boxes/dots_storage.py"
    candidates = [(current, layout, 1024, 32, True) for layout in
                  ("rowid", "rowid_index", "without_rowid", "without_rowid_index")]
    if args.baseline:
        candidates.insert(0, (args.baseline, "rowid", 1024, 32, False))
    if args.selected_only:
        candidates = [candidate for candidate in candidates
                      if candidate[0] == args.baseline or candidate[1] == "without_rowid"]
    elif not args.layouts_only:
        candidates += [(current, "without_rowid", page, cache, True) for page, cache in
                       ((512, 64), (1024, 24), (1024, 48), (1024, 64), (2048, 16), (2048, 24), (4096, 8))]
    results = []
    for rep in range(args.repetitions):
        for source, layout, page, cache, parsed in candidates[::1 if rep % 2 == 0 else -1]:
            for scenario in args.scenarios:
                result = run_case(source, fixture, inputs, scenario, layout, page, cache, parsed)
                result["repetition"] = rep
                results.append(result)
                print(json.dumps(result), flush=True)
    report = {"methodology": {"core": "0.5.4/cratesio", "arenaBytes": 262144,
        "shadowBytes": 131072, "durability": "synchronous=FULL, journal_mode=DELETE; unchanged barriers",
        "fixture": str(Path(args.fixtures).resolve()), "library": str(bench.LIBRARY),
        "limits": "Actual production Python VFS through the pinned host core. Temporary POSIX files, not badge latency. Timing includes host os.sync. Raw tables receive identical service operations, checksums and grouping. All successful cases verify full game, quick_check, empty outbox and reopen.",
        "parsedRecords": "Framing decode is included in control CPU for both paths but excluded from storage JSON-call count; baseline also reparses inside storage."},
        "results": results}
    Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
    assert not any(("error" in r or "closeError" in r) and r["pageBytes"] == 1024
                   and r["cachePages"] == 32 for r in results), "Selected-layout failure; see report"


if __name__ == "__main__":
    main()
