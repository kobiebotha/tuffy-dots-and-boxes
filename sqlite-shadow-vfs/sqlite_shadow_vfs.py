"""SQLite callback VFS with a bounded RAM shadow and physical rollback journal.

Uses only MicroPython os/time and file APIs. On Tufty the bytearray is allocated
from the PSRAM-backed heap. Supply this callable to native_sqlite.configure().
"""
import os
import time

DEFAULT_MAX_DATABASE_BYTES = 128 * 1024
_ZERO_BYTES = bytes(1024)


def _stat(path):
    try:
        return os.stat(path)
    except OSError as exc:
        if exc.args[0] != 2:
            raise
        return None


class ShadowVFS:
    """One connection with a bounded database shadow and a physical journal.

    SQLite's FULL journal barriers still precede every database sync. Between
    those barriers, random database I/O uses PSRAM; xSync writes its complete
    logical image sequentially and durably flushes it. This avoids LittleFS
    copying a file's tail at every random seek. The journal is never buffered.
    """

    def __init__(self, path, max_database_bytes=DEFAULT_MAX_DATABASE_BYTES,
                 heartbeat=None, buffer=None):
        if (not isinstance(path, str) or not path.startswith("/")
                or any(part in ("", ".", "..") for part in path.split("/")[1:])
                or "\0" in path):
            raise ValueError("VFS needs an absolute database file path")
        if type(max_database_bytes) is not int or max_database_bytes <= 0:
            raise ValueError("VFS needs a positive database capacity")
        if buffer is not None and (not isinstance(buffer, bytearray)
                                   or len(buffer) != max_database_bytes):
            raise ValueError("VFS buffer must be a bytearray matching the capacity")
        self.paths = (path, path + "-journal")
        self.max_database_bytes = max_database_bytes
        self.files = {}
        self.next_handle = 1
        self.heartbeat = heartbeat
        self.last_error = None
        self.db_buffer = bytearray(max_database_bytes) if buffer is None else buffer
        self.db_size = 0
        self.db_dirty = False
        self.db_handle = None

    def _path(self, path):
        if path not in self.paths:
            raise ValueError("Storage path is not owned by this database")
        return path

    def _zero_db(self, start, stop):
        while start < stop:
            end = min(stop, start + len(_ZERO_BYTES))
            self.db_buffer[start:end] = _ZERO_BYTES[:end - start]
            start = end

    def _load_db(self, stream, path, flags):
        size = os.stat(path)[6]
        if size > self.max_database_bytes:
            raise ValueError("Database exceeds storage limit")
        if size and stream.readinto(memoryview(self.db_buffer)[:size]) != size:
            raise OSError("Could not read the complete database")
        if not flags & 1:
            # LittleFS marks read-write opens dirty for their mtime attribute.
            # statvfs traverses that open CTZ and its directory entry separately,
            # counting this unchanged file twice until file_sync clears DIRTY.
            # Commit only that metadata now, before checking snapshot reserve.
            stream.flush()
        self.db_size, self.db_dirty = size, False

    def _sync_db(self, stream):
        if self.db_dirty:
            if self.db_size == 0:
                # A failed first commit may already have written a complete
                # header. An empty rollback must remove it or it could reappear
                # on reopen. Only an empty image is safe to recreate this way;
                # truncating a nonempty image risks pages absent from the journal.
                _, path, flags = self.files[self.db_handle]
                stream.close()
                stream = open(path, "w+b")
                self.files[self.db_handle] = (stream, path, flags)
            else:
                stream.seek(0)
                if stream.write(memoryview(self.db_buffer)[:self.db_size]) != self.db_size:
                    raise OSError("Incomplete database write; journal retained")
        # LittleFS flush is file_sync, including its data and directory commit.
        # Retain the qualified barrier and keep dirty data until both succeed.
        stream.flush()
        os.sync()
        self.db_dirty = False
        return stream

    def __call__(self, op, item, arg1, arg2):
        try:
            if self.heartbeat:
                self.heartbeat()
            if op == 9:
                if not 0 <= arg1 <= 512:
                    raise ValueError("Invalid entropy request")
                return (0, os.urandom(arg1))
            if op == 10:
                seconds = int(time.time())
                if time.gmtime(0)[0] == 2000:
                    seconds += 946684800
                elif time.gmtime(0)[0] != 1970:
                    raise ValueError("Unsupported clock epoch")
                return (0, (seconds // 86400, seconds % 86400))
            if op == 0:
                path = self._path(item)
                if any(entry[1] == path for entry in self.files.values()):
                    return (5, 0)
                flags = arg1
                exists = _stat(path) is not None
                if (not exists and not flags & 4) or (exists and flags & 16):
                    return (14, 0)
                mode = "rb" if flags & 1 else ("r+b" if exists else "x+b")
                stream = open(path, mode)
                if path == self.paths[0]:
                    try:
                        self._load_db(stream, path, flags)
                    except Exception:
                        stream.close()
                        raise
                handle = self.next_handle
                self.next_handle += 1
                self.files[handle] = (stream, path, flags)
                if path == self.paths[0]:
                    self.db_handle = handle
                return (0, handle)
            if op == 7:
                path = self._path(item)
                if path != self.paths[1]:
                    raise ValueError("Database deletion is not supported")
                if _stat(path) is not None:
                    os.remove(path)
                if arg1:
                    os.sync()
                return (0, 0)
            if op == 8:
                return (0, int(_stat(self._path(item)) is not None))
            stream, path, flags = self.files[item]
            is_db = item == self.db_handle
            if op == 1:
                if is_db and self.db_dirty:
                    stream = self._sync_db(stream)
                stream.close()
                del self.files[item]
                if is_db:
                    self.db_handle = None
                    self.db_size = 0
                if flags & 8:
                    if path != self.paths[1]:
                        raise ValueError("Database deletion is not supported")
                    os.remove(path)
                return (0, 0)
            if op == 2:
                if is_db:
                    if arg1 < 0 or arg2 < 0 or arg2 > self.max_database_bytes:
                        return (10, 0)
                    end = min(self.db_size, arg1 + arg2)
                    return (0, memoryview(self.db_buffer)[arg1:end])
                stream.seek(arg1)
                return (0, stream.read(arg2))
            if op == 3:
                if is_db:
                    end = arg1 + len(arg2)
                    if flags & 1:
                        return (10, 0)
                    if arg1 < 0 or end > self.max_database_bytes:
                        return (13, 0)
                    if arg2:
                        self._zero_db(self.db_size, arg1)
                        self.db_buffer[arg1:end] = arg2
                        self.db_size = max(self.db_size, end)
                        self.db_dirty = True
                    return (0, len(arg2))
                if arg1 < 0 or arg1 + len(arg2) > self.max_database_bytes + 4096:
                    return (13, 0)
                stream.seek(arg1)
                return (0, stream.write(arg2))
            if op == 4:
                if is_db:
                    if flags & 1:
                        return (10, 0)
                    if arg1 < 0:
                        return (1546, 0)
                    if arg1 > self.max_database_bytes:
                        return (13, 0)
                    if arg1 > self.db_size:
                        self._zero_db(self.db_size, arg1)
                    if arg1 != self.db_size:
                        # Rollback may discard pages spilled beyond the original
                        # database end. Keep the physical file's high-water tail:
                        # MicroPython cannot truncate it, and reopening with "w"
                        # could destroy unchanged pages absent from the journal.
                        # SQLite's restored header bounds its valid page count.
                        self.db_size, self.db_dirty = arg1, True
                    return (0, 0)
                stream.seek(0, 2)
                size = stream.tell()
                if arg1 < size:
                    self.last_error = "File truncation is unavailable"
                    return (1546, 0)
                if arg1 > self.max_database_bytes:
                    return (13, 0)
                if arg1 > size:
                    stream.seek(arg1 - 1)
                    if stream.write(b"\0") != 1:
                        return (10, 0)
                return (0, 0)
            if op == 5:
                if is_db:
                    self._sync_db(stream)
                    return (0, 0)
                stream.flush()
                os.sync()
                return (0, 0)
            if op == 6:
                if is_db:
                    return (0, self.db_size)
                position = stream.tell()
                stream.seek(0, 2)
                size = stream.tell()
                stream.seek(position)
                return (0, size)
            return (10, 0)
        except Exception as exc:
            self.last_error = type(exc).__name__ + ": " + str(exc)
            return (14 if op == 0 else 10, 0)

    def close_all(self):
        for handle in list(self.files):
            if self(1, handle, 0, 0)[0]:
                raise OSError("Could not close database storage")
