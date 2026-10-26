"""Compare download scheduling/framing without Wi-Fi, flash or native SQLite.

Supply a previous app directory with --before. --emit-device writes a standalone
RAM-only MicroPython script containing both public source versions and a fixture;
run it with mpremote after safely ejecting any mounted USB filesystem if needed.
The minimal sink checks every operation but intentionally does no database work.
"""
import gc
import json
import time


class Sink:
    def __init__(self):
        self.operations = []
        self.commits = 0

    def control_lines(self, lines, parsed=None):
        self.commits += 1
        records = parsed if parsed is not None else [json.loads(line) for line in lines]
        result = []
        for record in records:
            self.operations.extend(record.get("data", {}).get("data", []))
            full = "checkpoint_complete" in record
            result.append({"instructions": [{"DidCompleteSync": {}}] if full else [],
                           "full_checkpoint": full})
        return result


class Records:
    def __init__(self, room):
        self.record = {"room_id": room, "pending_move": None, "expires_at": 0,
                       "refresh_token": "fixture", "access_token": "fixture"}


class Stream:
    status = 200
    def __init__(self, wire, fragment, pending):
        self.wire, self.fragment, self.pending = wire, fragment, pending
        self.offset = self.reads = 0

    def step(self):
        self.reads += 1
        if self.offset == len(self.wire):
            return self.pending
        end = self.offset + self.fragment
        data = self.wire[self.offset:end]
        self.offset += len(data)
        return data


def clock():
    if hasattr(time, "ticks_us"):
        return time.ticks_us()
    return time.perf_counter_ns() // 1000


def diff(a, b):
    return time.ticks_diff(a, b) if hasattr(time, "ticks_diff") else a - b


def run_case(sources, wire, expected, room, repetitions=3):
    framing = {"__name__": "download_benchmark_framing"}
    exec(sources["stream"], framing)
    pending = object()
    sync_scope = {"__name__": "download_benchmark_sync", "StreamLines": framing["StreamLines"],
                  "PENDING": pending, "Gateway": object, "HTTPS": object,
                  "split_https": lambda url: None, "MAX_SYNC_BATCH_RECORDS": 6,
                  "MAX_SYNC_LINE_BYTES": 8192}
    body = "\n".join(line for line in sources["sync"].splitlines()
                     if not line.startswith("from "))
    exec(body, sync_scope)
    timings = []
    result = None
    for _ in range(repetitions):
        gc.collect()
        sink = Sink()
        if "SyncClient" in sync_scope:
            sync = sync_scope["SyncClient"](sink, sync_scope["Connector"](), "https://fixture.invalid",
                                             {"name": "fixture", "params": {}}, ())
        else:
            sync = sync_scope["Sync"](sink, Records(room), "https://fixture.invalid", "", "", "", ())
        stream = Stream(wire, framing["MAX_FRAGMENT"], sync_scope["PENDING"])
        sync.stream = stream
        sync.phase, sync.connected, sync.core_started = "stream", True, True
        steps = 0
        started = clock()
        while not sync.synced and steps < 1000:
            sync.step(16)
            steps += 1
        elapsed = diff(clock(), started)
        assert sync.synced and sink.operations == expected, "Download output changed"
        timings.append(elapsed)
        result = {"fragment_bytes": framing["MAX_FRAGMENT"], "updates": steps,
                  "transport_calls": stream.reads, "commits": sink.commits,
                  "operations": len(sink.operations)}
    timings.sort()
    result["median_microseconds"] = timings[len(timings) // 2]
    result["microseconds"] = timings
    return result


def fixture():
    """Synthetic immutable 49-edge board, no credentials or cloud access."""
    import base64
    room = "550e8400-e29b-41d4-a716-446655440000"
    compact_room = base64.b64encode(bytes.fromhex(room.replace("-", ""))).decode()
    operations = []
    for index in range(1, 50):
        identity = base64.b64encode(index.to_bytes(16, "big")).decode()
        operations.append({"op_id": str(index), "op": "PUT", "object_type": "moves",
                           "object_id": identity, "checksum": 0,
                           "data": json.dumps({"r": compact_room, "e": index - 1, "n": index})})
    operations.append({"op_id": "50", "op": "PUT", "object_type": "rooms", "object_id": compact_room,
                       "checksum": 0, "data": json.dumps({"s": 2, "p": 1, "a": 10, "b": 10, "w": 0, "n": 49})})
    wire = (json.dumps({"data": {"bucket": "fixture", "data": operations}}) + "\n"
            + json.dumps({"checkpoint_complete": {"last_op_id": "50"}}) + "\n").encode()
    return room, operations, wire


def main():
    import argparse
    from pathlib import Path
    import sys
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True, type=Path)
    parser.add_argument("--after", type=Path, default=Path(__file__).resolve().parents[1] / "app/dots_boxes")
    parser.add_argument("--emit-device", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    def load_sources(root):
        if (root / "dots_stream.py").exists():
            return {"stream": (root / "dots_stream.py").read_text(),
                    "sync": (root / "dots_sync.py").read_text()}
        sdk = root.parents[1] / "lib/powersync"
        return {"stream": (sdk / "stream.py").read_text(), "sync": (sdk / "sync.py").read_text()}

    sources = {label: load_sources(root) for label, root in (("before", args.before), ("after", args.after))}
    room, expected, wire = fixture()
    if args.emit_device:
        body = Path(__file__).read_text().split("def fixture():")[0]
        script = ("import sys\nsys.path.insert(0, '/system/apps/dots_boxes')\n" + body
                  + "\nSOURCES=" + repr(sources) + "\nWIRE=" + repr(wire)
                  + "\nEXPECTED=" + repr(expected) + "\nROOM=" + repr(room)
                  + "\nfor label in ('before','after'):\n"
                  + "    print('DOTS_DOWNLOAD_BENCH',label,json.dumps(run_case(SOURCES[label],WIRE,EXPECTED,ROOM)))\n")
        args.emit_device.write_text(script)
        print("DEVICE_SCRIPT", args.emit_device)
        return
    sys.path.insert(0, str(args.after))
    result = {"scope": "Host scheduling/framing with a checked memory sink; excludes Wi-Fi, TLS, native core and flash.",
              "wire_bytes": len(wire),
              "results": {label: run_case(source, wire, expected, room) for label, source in sources.items()}}
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
