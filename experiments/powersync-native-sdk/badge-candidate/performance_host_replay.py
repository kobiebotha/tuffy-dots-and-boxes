#!/usr/bin/env python3
"""Check the badge replay against real cached host SQLite and native SDK code.

This is a fixture/harness compatibility check, never badge latency evidence.
Run each variant in a separate process. Libraries must already be built; this
tool never downloads, rebuilds, opens a network socket or touches badge state.
"""
import argparse
import ctypes as c
import json
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


class Buffer(c.Structure):
    _fields_ = [("ptr", c.c_void_p), ("len", c.c_size_t), ("capacity", c.c_size_t)]


class HostNative:
    def __init__(self):
        self.lib = c.CDLL(str(HERE.parent / "badge-module/rust/target/libbridge_host.dylib"))
        for name, result, args in (
            ("bridge_host_open", c.c_int, [c.POINTER(c.c_void_p)]),
            ("sqlite3_exec", c.c_int, [c.c_void_p, c.c_char_p, c.c_void_p, c.c_void_p, c.c_void_p]),
            ("sqlite3_errmsg", c.c_char_p, [c.c_void_p]),
            ("sqlite3_close", c.c_int, [c.c_void_p]),
            ("psb_start", c.c_void_p, [c.c_void_p, c.c_char_p, c.c_size_t, c.c_char_p, c.c_size_t, c.c_char_p, c.c_size_t]),
            ("psb_poll", Buffer, [c.c_void_p, c.c_uint, c.c_uint]),
            ("psb_command", c.c_int, [c.c_void_p, c.c_char_p, c.c_size_t]),
            ("psb_buffer_free", None, [Buffer]),
            ("psb_stop", c.c_int, [c.c_void_p]),
            ("psb_destroy", c.c_int, [c.c_void_p]),
            ("psb_sql_enter", c.c_void_p, [c.c_void_p]),
            ("psb_sql_leave", None, [c.c_void_p]),
        ):
            function = getattr(self.lib, name)
            function.restype, function.argtypes = result, args
        self.db, self.context = c.c_void_p(), None

    def configure(self, io):
        pass  # This check uses SQLite :memory:, never the badge VFS.

    def open(self, path):
        assert path == ":memory:"
        assert self.lib.bridge_host_open(c.byref(self.db)) == 0

    def exec(self, sql):
        lease = self.lib.psb_sql_enter(self.context) if self.context else None
        if self.context:
            assert lease, "SDK database busy"
        try:
            rows = []

            @c.CFUNCTYPE(c.c_int, c.c_void_p, c.c_int, c.POINTER(c.c_char_p), c.POINTER(c.c_char_p))
            def callback(context, count, values, columns):
                rows.append([None if values[i] is None else values[i].decode() for i in range(count)])
                return 0

            result = self.lib.sqlite3_exec(self.db, sql.encode(), callback, None, None)
            assert result == 0, (result, self.lib.sqlite3_errmsg(self.db).decode())
            assert len(rows) <= 32
            return rows
        finally:
            if lease:
                self.lib.psb_sql_leave(lease)

    def sdk_start(self, schema, endpoint, room):
        schema, endpoint, room = schema.encode(), endpoint.encode(), room.encode()
        self.context = self.lib.psb_start(self.db, schema, len(schema), endpoint, len(endpoint), room, len(room))
        assert self.context

    def sdk_poll(self, elapsed, budget):
        buffer = self.lib.psb_poll(self.context, elapsed, budget)
        try:
            return c.string_at(buffer.ptr, buffer.len).decode()
        finally:
            self.lib.psb_buffer_free(buffer)

    def sdk_reply(self, command):
        command = command.encode()
        assert self.lib.psb_command(self.context, command, len(command)) == 0

    def sdk_stop(self):
        if not self.context:
            return True
        if not self.lib.psb_stop(self.context):
            return False
        assert self.lib.psb_destroy(self.context)
        self.context = None
        return True

    def close(self):
        assert self.sdk_stop()
        assert self.lib.sqlite3_close(self.db) == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=("original", "native"), required=True)
    parser.add_argument("--scenario", choices=("cold", "live", "warm_upload"), default="cold")
    parser.add_argument("--pace-ms", type=int, default=0)
    parser.add_argument("--keep-stream", action="store_true")
    args = parser.parse_args()
    if args.keep_stream and (args.variant != "native" or args.scenario != "warm_upload"):
        parser.error("--keep-stream requires native warm_upload")
    if args.variant == "native":
        app = HERE / "apps/dots_boxes_native"
        sys.path[:0] = [str(app), str(app / "powersync")]
        native_class = HostNative
    else:
        sys.path[:0] = [str(ROOT / path) for path in (
            "badge/lib", "badge/app/dots_boxes", "sqlite-shadow-vfs", "native-sqlite/tests")]
        from native_sqlite_host import HostCore
        native_class = HostCore
    import powersync.runtime
    powersync.runtime.load_native = lambda heartbeat=None: native_class()
    time.ticks_us = lambda: time.perf_counter_ns() // 1000
    time.ticks_ms = lambda: time.perf_counter_ns() // 1000000
    time.ticks_diff = lambda a, b: a - b
    time.sleep_ms = lambda milliseconds: time.sleep(milliseconds / 1000)
    scope = {"BENCH_HEARTBEAT": lambda: None,
             "BENCH_CONFIG": {"variant": args.variant, "storage": "memory",
                              "root": "/state/dots_boxes_benchmark", "pace_ms": args.pace_ms,
                              "repetitions": 1, "scenario": args.scenario,
                              "keep_stream": args.keep_stream,
                              "runtime": "host compatibility check; not badge timings"},
             "BENCH_FIXTURE": json.loads((HERE.parent / "host/fixture.json").read_text())}
    exec(compile((HERE / "performance_replay_payload.py").read_text(), str(HERE / "performance_replay_payload.py"), "exec"), scope)


if __name__ == "__main__":
    main()
