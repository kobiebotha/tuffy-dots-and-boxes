#!/usr/bin/env python3
"""Apply a captured live stream through the real bounded badge parser/core.

Used by qualify_live_stream.mjs. Input contains public game rows and NDJSON,
never credentials. The temporary POSIX database is supplied by that caller.
"""
import argparse
import ctypes as c
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "badge/tests"), str(ROOT / "badge/app/dots_boxes")]
from native_fixture import HostCore
from dots_storage import Store, MAX_SYNC_BATCH_RECORDS, MAX_SYNC_LINE_BYTES
from dots_stream import StreamLines, MAX_FRAGMENT


def compact(value):
    return json.dumps(value, separators=(",", ":"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--database-root", required=True)
    args = parser.parse_args()
    fixture = json.load(sys.stdin)
    native = HostCore()
    memory = native.library.sqlite3_memory_highwater
    memory.argtypes, memory.restype = [c.c_int], c.c_longlong
    memory(1)
    store = Store(native=native, root=args.database_root).open()
    try:
        store.bind_account(fixture["user_id"])
        stream = {"name": "badge_game_v2", "params": {"room_id": fixture["room_id"]}}
        instructions = store.start_sync(stream, {
            "checkpoint_mode": "requests", "include_defaults": False,
            "parameters": {}, "active_streams": [stream],
        }, clear_target=True)
        request = next(item["EstablishSyncStream"]["request"] for item in instructions
                       if "EstablishSyncStream" in item)
        if args.prepare:
            print(compact(request))
            return
        assert request == fixture["request"], "Native live request changed"
        store.control("connection", "established")
        completed = full = records = commits = 0
        batch, batch_bytes = [], 0

        def flush():
            nonlocal completed, full, records, commits, batch, batch_bytes
            if not batch:
                return
            events = store.control_lines(batch)
            commits += 1
            records += len(batch)
            for event in events:
                for item in event["instructions"]:
                    assert "CloseSyncStream" not in item, "Core rejected stream"
                    assert "CheckpointDelayedDueToLocalData" not in item, "Unexpected target barrier"
                    if "LogLine" in item:
                        assert "could not be resolved" not in item["LogLine"].get("line", ""), "Unresolved stream"
                    if "DidCompleteSync" in item:
                        completed += 1
                        full += int(event["full_checkpoint"])
            batch, batch_bytes = [], 0

        def append(line):
            nonlocal batch_bytes
            size = len(line.encode())
            if batch and batch_bytes + size > MAX_SYNC_LINE_BYTES:
                flush()
            batch.append(line)
            batch_bytes += size
            marker = json.loads(line)
            if (len(batch) >= MAX_SYNC_BATCH_RECORDS or "checkpoint_complete" in marker
                    or "partial_checkpoint_complete" in marker):
                flush()

        framing = StreamLines()
        wire = fixture["wire"].encode()
        for offset in range(0, len(wire), MAX_FRAGMENT):
            framing.feed(wire[offset:offset + MAX_FRAGMENT])
            while True:
                line = framing.pop()
                if line is None:
                    break
                append(line)
        framing.finish()
        flush()
        assert completed and full == 1, "Full checkpoint did not publish"
        room = fixture["room"]
        expected_room = {name: room[name] for name in (
            "status", "cols", "rows", "current_seat", "score_p1", "score_p2", "winner", "move_count")}
        assert store.room(fixture["room_id"]) == expected_room, "Room mapping differs from Supabase"
        moves = fixture["moves"]
        expected_moves = [{name: move[name] for name in ("orientation", "row", "col", "move_index")}
                          for move in moves]
        assert store.moves(fixture["room_id"]) == expected_moves, "Move mapping differs from Supabase"
        assert all(store.has_move(move["id"], fixture["room_id"]) for move in moves), "Canonical UUID acknowledgement failed"
        assert not store.has_move(fixture["room_id"], fixture["room_id"]), "False move acknowledgement"
        assert not store.has_local_writes(), "Unexpected SQL upload queue"
        assert native.exec("SELECT count(*) FROM rooms") == [["1"]], "Unexpected rooms"
        assert native.exec("SELECT count(*) FROM moves") == [[str(len(moves))]], "Unexpected moves"
        assert native.exec("SELECT count(*) FROM ps_stream_subscriptions WHERE ttl IS NOT NULL") == [["1"]]
        print(compact({
            "qualified": True, "core": store._scalar("SELECT powersync_rs_version()"),
            "schema": store._scalar("PRAGMA user_version"), "arenaBytes": 256 * 1024,
            "wireBytes": len(wire), "records": records, "commits": commits,
            "fullCompletions": full, "rooms": 1, "moves": len(moves),
            "canonicalAcknowledgements": len(moves), "crudRows": 0,
            "databaseBytes": Path(args.database_root + "/state.sqlite").stat().st_size,
            "nativePeakBytes": memory(0),
        }))
    finally:
        store.close()


if __name__ == "__main__":
    main()
