#!/usr/bin/env python3
"""Benchmark service-produced fixtures with the pinned native core, no device.

Input comes from powersync-cloud/tests/benchmark-projection.mjs. Only temporary
POSIX databases are written. Never imports a serial or network library.
"""
import ctypes as c
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "badge/tests"), str(ROOT / "badge/app/dots_boxes")]
import dots_storage as storage
from native_fixture import HostCore, LIBRARY


def compact(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def quote(value):
    return "'" + value.replace("'", "''") + "'"


def run(fixture, scenario_name, scenario, user_id):
    native = HostCore()
    memory = native.library.sqlite3_memory_highwater
    memory.argtypes, memory.restype = [c.c_int], c.c_longlong
    used = native.library.sqlite3_memory_used
    used.restype = c.c_longlong
    memory(1)
    original_schema = storage.SCHEMA
    storage.SCHEMA = compact(fixture["schema"])
    store = None
    result = {"variant": fixture["name"], "scenario": scenario_name}
    with tempfile.TemporaryDirectory(prefix="dots-native-") as directory:
        try:
            store = storage.Store(native=native, root=directory + "/dots").open()
            for sql in fixture["rawSql"]:
                native.exec(sql)
            store.bind_account(user_id)
            stream = {"name": "benchmark", "params": {"room_id": "fixture"}}
            store.start_sync(stream, {"checkpoint_mode": "requests", "include_defaults": False,
                                     "parameters": {}, "active_streams": [stream],
                                     "schema": {"tables": [], "raw_tables": fixture["schema"]["raw_tables"]}}, clear_target=True)
            result["setupPeakBytes"] = memory(1)
            result["syncStartBytes"] = used()
            before_transactions = native.transactions
            started = time.perf_counter()
            operations = scenario["ops"]
            offset = checksum = count = completions = 0
            for group_size in scenario["groups"]:
                group = operations[offset:offset + group_size]
                offset += group_size
                count += group_size
                checksum = (checksum + sum(operation["checksum"] for operation in group)) & 0xffffffff
                checkpoint = {"checkpoint": {"last_op_id": str(offset), "buckets": [
                    {"bucket": "fixture", "checksum": checksum, "priority": 3, "count": count}]}}
                records = [compact(checkpoint)] + [compact({"data": {"bucket": "fixture", "data": [operation]}})
                    for operation in group] + [compact({"checkpoint_complete": {"last_op_id": str(offset)}})]
                batch, size = [], 0
                for record in records:
                    if batch and (len(batch) >= storage.MAX_SYNC_BATCH_RECORDS or size + len(record.encode()) > storage.MAX_SYNC_LINE_BYTES):
                        for event in store.control_lines(batch):
                            completions += sum("DidCompleteSync" in item for item in event["instructions"])
                            assert not any("CloseSyncStream" in item for item in event["instructions"]), event
                        batch, size = [], 0
                    batch.append(record)
                    size += len(record.encode())
                if batch:
                    for event in store.control_lines(batch):
                        completions += sum("DidCompleteSync" in item for item in event["instructions"])
                        assert not any("CloseSyncStream" in item for item in event["instructions"]), event
            assert offset == len(operations)
            assert completions == len(scenario["groups"]), (completions, scenario["groups"])
            actual_rows = sum(int(native.exec('SELECT count(*) FROM "' + table + '"')[0][0])
                for table in {operation["object_type"] for operation in operations})
            assert actual_rows == fixture["expectedRows"], (actual_rows, fixture["expectedRows"])
            for table, expected_json in (("rooms", fixture["expectedRoom"]), ("moves", fixture["expectedMove"])):
                if expected_json is None:
                    continue
                expected = json.loads(expected_json)
                names = [row[1] for row in native.exec('PRAGMA table_info("' + table + '")')]
                order = "" if table == "rooms" else ' ORDER BY "' + ("n" if "n" in names else "move_index") + '"'
                selected = native.exec('SELECT ' + ','.join('"' + name + '"' for name in names) +
                                       ' FROM "' + table + '"' + order + ' LIMIT 1')[0]
                assert selected == [None if expected[name] is None else str(expected[name]) for name in names], (table, selected, expected)
            assert native.exec("SELECT count(*) FROM ps_crud") == [["0"]]
            result.update({"hostMilliseconds": round((time.perf_counter() - started) * 1000, 2),
                "syncPeakBytes": memory(0), "syncEndBytes": used(),
                "transactions": native.transactions - before_transactions,
                "databasePages": int(native.exec("PRAGMA page_count")[0][0]),
                "freePages": int(native.exec("PRAGMA freelist_count")[0][0]),
                "databaseBytes": Path(directory + "/dots/state.sqlite").stat().st_size,
                "oplogRows": int(native.exec("SELECT count(*) FROM ps_oplog")[0][0]),
                "oplogJsonBytes": int(native.exec("SELECT coalesce(sum(length(data)),0) FROM ps_oplog")[0][0]),
                "maxReturnedRows": native.max_rows, "completedCheckpoints": completions})
        except Exception as error:
            result.update({"error": type(error).__name__ + ": " + str(error), "peakBytesAtFailure": memory(0)})
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception as error:
                    result["closeError"] = str(error)
            storage.SCHEMA = original_schema
    return result


def main():
    if not LIBRARY.exists():
        print(compact({"unavailable": "Set POWERSYNC_TEST_LIBRARY to the pinned fixture-host library.", "results": []}))
        return
    inputs = json.loads(Path(sys.argv[1]).read_text())
    results = [run(fixture, name, scenario, inputs["userId"]) for fixture in inputs["fixtures"]
               for name, scenario in fixture["scenarios"].items()]
    print(compact({"arenaBytes": 256 * 1024, "library": str(LIBRARY), "results": results}))


if __name__ == "__main__":
    main()
