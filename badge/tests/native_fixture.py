"""Pinned real PowerSync host qualifier; POSIX VFS, never a badge VFS test.

Reuse the locally built qualifier from badgegotchi, or set
POWERSYNC_TEST_LIBRARY to its location. The allocator and 32-row result cap
match the qualified native module. Tests skip if that library is unavailable.
"""
import ctypes as c
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIBRARY = Path(os.environ.get("POWERSYNC_TEST_LIBRARY", str(
    ROOT.parent / "badgegotchi/experiments/badgeware-native/powersync-audit/build/fixture-host.dylib")))


class HostCore:
    library = None
    arena = None

    def __init__(self):
        if self.library is None:
            lib = c.CDLL(str(LIBRARY))
            lib.sqlite3_config.argtypes = [c.c_int]
            self.__class__.arena = c.create_string_buffer(256 * 1024)
            self._check(lib.sqlite3_config(
                c.c_int(8), c.cast(self.arena, c.c_void_p), c.c_int(len(self.arena)), c.c_int(64)))
            for name, result, args in (
                ("sqlite3_open", c.c_int, [c.c_char_p, c.POINTER(c.c_void_p)]),
                ("sqlite3_close", c.c_int, [c.c_void_p]),
                ("sqlite3_exec", c.c_int, [c.c_void_p, c.c_char_p, c.c_void_p, c.c_void_p, c.c_void_p]),
                ("sqlite3_errmsg", c.c_char_p, [c.c_void_p]),
            ):
                function = getattr(lib, name)
                function.restype, function.argtypes = result, args
            self._check(lib.fixture_register())
            self.__class__.library = lib
        self.db = c.c_void_p()
        self.transactions = 0
        self.max_rows = 0

    @staticmethod
    def _check(code):
        if code:
            raise RuntimeError("SQLite code %d" % code)

    def configure(self, _callback):
        pass

    def open(self, path):
        self._check(self.library.sqlite3_open(path.encode(), c.byref(self.db)))

    def exec(self, sql):
        rows = []
        callback_type = c.CFUNCTYPE(c.c_int, c.c_void_p, c.c_int,
                                   c.POINTER(c.c_char_p), c.POINTER(c.c_char_p))

        @callback_type
        def callback(_context, count, values, _columns):
            rows.append([None if values[i] is None else values[i].decode() for i in range(count)])
            return 0

        code = self.library.sqlite3_exec(self.db, sql.encode(), callback, None, None)
        if code:
            raise RuntimeError(self.library.sqlite3_errmsg(self.db).decode())
        self.max_rows = max(self.max_rows, len(rows))
        if len(rows) > 32:
            raise ValueError("query row limit")
        if sql == "BEGIN IMMEDIATE":
            self.transactions += 1
        return rows

    def close(self):
        self._check(self.library.sqlite3_close(self.db))
        self.db = c.c_void_p()
