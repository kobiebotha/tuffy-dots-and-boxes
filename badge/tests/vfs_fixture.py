"""Real pinned host core using the production Python BadgeVFS callbacks.

The adapter compiles only a tiny host VFS shim, linked to the same already-built
PowerSync/SQLite library as HostCore. No SQLite/core/native device code changes.
All storage behavior remains in the configured Python callback. One connection
may use this bridge at a time; close restores the previous default host VFS.
"""
from collections import Counter
import ctypes as c
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from native_fixture import HostCore, LIBRARY, ROOT

IO = c.CFUNCTYPE(c.c_int, c.c_int, c.c_char_p, c.c_int, c.c_longlong,
                 c.c_void_p, c.c_int, c.POINTER(c.c_longlong), c.POINTER(c.c_longlong))
OPERATIONS = ("open", "close", "read", "write", "truncate", "sync", "size",
              "delete", "access", "random", "time")


class PythonVFSCore(HostCore):
    _bridge = None
    _build = None
    _active = None

    def __init__(self):
        # Reuse HostCore's singleton arena/registration, including when an
        # ordinary HostCore test ran first in this same Python process.
        host = HostCore()
        self.library = host.library
        self.db = c.c_void_p()
        self.transactions = self.max_rows = 0
        self.io = None
        self.stats = Counter()
        self.events = []
        self.handles = {}
        self.last_bridge_error = None
        self._installed = False
        self._callback = IO(self._invoke)
        if PythonVFSCore._bridge is None:
            PythonVFSCore._build = tempfile.TemporaryDirectory(prefix="dots-python-vfs-")
            filename = "bridge.dylib" if sys.platform == "darwin" else "bridge.so"
            target = Path(PythonVFSCore._build.name) / filename
            include = Path(os.environ.get("POWERSYNC_SQLITE_INCLUDE", str(
                ROOT.parent / "powerbadge/native/sqlite-hello/vendor/sqlite-littlefs-vfs/vendor/sqlite")))
            flags = ["-dynamiclib"] if sys.platform == "darwin" else ["-shared", "-fPIC"]
            subprocess.run(["cc", "-std=c11", "-O1", *flags, "-I", str(include),
                            str(Path(__file__).with_name("vfs_bridge.c")), str(LIBRARY),
                            "-o", str(target)], check=True, capture_output=True)
            bridge = c.CDLL(str(target))
            bridge.python_vfs_install.argtypes = [IO]
            bridge.python_vfs_install.restype = c.c_int
            bridge.python_vfs_uninstall.restype = c.c_int
            bridge.python_vfs_default_name.restype = c.c_char_p
            bridge.python_vfs_probe_random.argtypes = [c.c_int, c.c_void_p]
            bridge.python_vfs_probe_random.restype = c.c_int
            PythonVFSCore._bridge = bridge
        self.bridge = PythonVFSCore._bridge

    @property
    def default_vfs(self):
        return self.bridge.python_vfs_default_name().decode()

    def reset_stats(self):
        self.stats.clear()
        self.events.clear()

    def probe_randomness(self, size):
        if not self._installed or not 0 <= size <= 512:
            raise ValueError("Install the VFS before requesting bounded entropy")
        buffer = c.create_string_buffer(size)
        if self.bridge.python_vfs_probe_random(size, buffer) != size:
            raise ValueError("Python VFS entropy callback failed")
        return buffer.raw

    def configure(self, callback):
        if self.db.value or self._installed:
            raise ValueError("Cannot replace an active Python VFS")
        if not callable(callback):
            raise ValueError("A Python VFS callback is required")
        self.io = callback

    def _invoke(self, op, path, handle, arg, buffer, length, out1, out2):
        # Never let exceptions cross SQLite/Rust/ctypes frames.
        try:
            if not 0 <= op <= 10:
                return 10
            item = path.decode() if path is not None else handle
            target = item if op in (0, 7, 8) else self.handles.get(handle, "runtime")
            kind = "journal" if isinstance(target, str) and target.endswith("-journal") else (
                "db" if isinstance(target, str) and target.endswith("state.sqlite") else "runtime")
            self.stats[OPERATIONS[op]] += 1
            self.stats[kind + "." + OPERATIONS[op]] += 1
            if op == 2:
                arg2 = length
            elif op == 3:
                arg2 = c.string_at(buffer, length)
            else:
                arg2 = None
            response = self.io(op, item, int(arg), arg2)
            if not isinstance(response, (tuple, list)) or len(response) != 2:
                raise ValueError("Invalid Python VFS response")
            code, value = response
            self.events.append({"op": op, "kind": kind, "arg": int(arg),
                                "length": length, "code": int(code)})
            if code:
                return int(code)
            if op in (2, 9):
                if not isinstance(value, (bytes, bytearray, memoryview)) or len(value) > length:
                    raise ValueError("Invalid Python VFS bytes")
                data = bytes(value)
                if data:
                    c.memmove(buffer, data, len(data))
                out1[0] = len(data)
                self.stats[kind + "." + OPERATIONS[op] + "Bytes"] += len(data)
            elif op == 10:
                if not isinstance(value, (tuple, list)) or len(value) != 2:
                    raise ValueError("Invalid Python VFS clock")
                out1[0], out2[0] = int(value[0]), int(value[1])
            elif op in (0, 3, 6, 8):
                out1[0] = int(value)
                if op == 0:
                    self.handles[int(value)] = item
                elif op == 3:
                    self.stats[kind + ".writeBytes"] += int(value)
            elif op == 1:
                self.handles.pop(handle, None)
            return 0
        except BaseException as error:
            self.last_bridge_error = type(error).__name__ + ": " + str(error)
            self.stats["bridgeErrors"] += 1
            return 14 if op == 0 else 10

    def open(self, path):
        if self.io is None or self.db.value:
            raise ValueError("Configure one Python VFS before opening")
        if PythonVFSCore._active is not None:
            raise ValueError("Only one Python VFS connection is supported")
        self.previous_default_vfs = self.default_vfs
        self._check(self.bridge.python_vfs_install(self._callback))
        self._installed = True
        PythonVFSCore._active = self
        code = self.library.sqlite3_open(path.encode(), c.byref(self.db))
        if code:
            message = self.library.sqlite3_errmsg(self.db).decode() if self.db.value else "SQLite open failed"
            self.close()
            raise RuntimeError(message)

    def close(self):
        if self.db.value:
            self._check(self.library.sqlite3_close(self.db))
            self.db = c.c_void_p()
        if self._installed:
            self._check(self.bridge.python_vfs_uninstall())
            self._installed = False
            PythonVFSCore._active = None
            if self.default_vfs != self.previous_default_vfs:
                raise AssertionError("Previous host VFS was not restored")
