# SQLite shadow VFS for MicroPython

`sqlite_shadow_vfs.py` is a standalone SQLite storage callback implementation.
Copy this one file onto another app's import path. It has no dependency on
PowerSync, Dots & Boxes, or Badgeware imports; it uses `os`, `time` and ordinary
MicroPython file APIs. On Tufty, its RAM buffer lives in the PSRAM-backed heap.

SQLite performs random database reads and writes against a bounded RAM image.
At database sync, the VFS writes that image sequentially and flushes it to the
filesystem. The rollback journal remains a physical file. This avoids repeated
LittleFS file-tail copying while preserving SQLite's journal-before-database
sync ordering.

## Use

Use the separate [native-sqlite module](../native-sqlite/README.md), either its
SQLite-only or PowerSync variant, or another bridge implementing the same
`configure(callback)` operation contract. This file does not contain SQLite
itself and cannot register a VFS with CPython's standard `sqlite3` module.

```python
import native_sqlite
from sqlite_shadow_vfs import ShadowVFS

# The application creates and owns this directory.
path = "/state/my_app/notes.sqlite"
io = ShadowVFS(path, max_database_bytes=128 * 1024)
native_sqlite.configure(io)
native_sqlite.open(path)
try:
    native_sqlite.exec("PRAGMA page_size=1024")
    native_sqlite.exec("PRAGMA max_page_count=128")
    native_sqlite.exec("PRAGMA journal_mode=DELETE")
    native_sqlite.exec("PRAGMA synchronous=FULL")
    native_sqlite.exec("PRAGMA temp_store=MEMORY")
    print(native_sqlite.exec("SELECT sqlite_version()"))
finally:
    native_sqlite.close()
    io.close_all()
```

`ShadowVFS(path, max_database_bytes=131072, heartbeat=None, buffer=None)` accepts
an absolute database filename and a positive byte capacity. Only that file and
`path + "-journal"` are accessible through the callback. `heartbeat`, if provided,
is called for each operation. `buffer` may be a caller-owned `bytearray` whose
length equals the configured capacity, instead of allocating a new one.

The object is passed directly as `callback(op, item, arg1, arg2)`; it returns
`(sqlite_result_code, value)`. The native module's README documents operations
0–10. `last_error` describes the most recent callback failure. `db_size` exposes
the logical image size, including unsynced growth, for space-admission checks.

The [persistent example](examples/notes.py) shows directory setup, a capacity
limit, free-space admission and a transaction using plain SQLite.

## Storage contract

- One connection and one serialized owner; no other process, interrupt, thread,
  or VFS may access the same database or journal concurrently.
- Use DELETE rollback journaling, FULL synchronization and memory-only temporary
  storage. WAL and physical compaction (including VACUUM) are unsupported.
- Logical database truncation is supported for transaction rollback, including
  growth that spilled beyond SQLite's page cache. Nonempty images retain the
  physical file's high-water tail; SQLite's restored header page count bounds
  the valid database. Rollback to an empty image recreates and flushes an empty
  file so a failed first commit cannot reappear after reopening. This requires
  modern SQLite's valid in-header page count, as supplied by `native-sqlite`.
- Journal writes are bounded to the database capacity plus 4 KiB. Larger
  configurations and different page/cache sizes need their own qualification.
- Keep the VFS and its buffer alive until SQLite closes. Read callbacks return
  buffer views which the native bridge must consume before another operation.
- The application owns directory creation, account/schema policy and recovery.
  Retain an unexpected nonempty journal for explicit recovery; the PowerSync SDK
  implements that admission policy separately.
- Before writes, reserve space for another complete logical database image plus
  journal/metadata headroom. The game uses 64 KiB of headroom. LittleFS retains
  the old image until its replacement commits; RAM capacity alone is insufficient.
- Durability relies on file `flush()` and `os.sync()` having the qualified
  filesystem semantics. Another MicroPython port/filesystem needs validation.

This Python adapter is separate from the portable C `sqlite-littlefs-vfs`
library, which calls LittleFS directly.

## Tests

```sh
python3 -m unittest discover -s sqlite-shadow-vfs/tests -v
```

Unit tests cover the actual file callbacks, limits, logical shrink, journal
ordering and injected I/O failures. Isolated integrations use the native
component's SQLite-only host build to exercise this VFS without PowerSync or the
game, including spilled-growth rollback, automatic rollback after SQLITE_FULL,
and a failed first commit that returns to an empty database after reopening. Build that library
with `python3 native-sqlite/tools/build.py host --variant sqlite`; the integration
test skips with an explanation when it is unavailable. The PowerSync suite also
uses this component's C test bridge and Python adapter.

Host tests use temporary POSIX files. They do not execute the ARM `.mpy` or
establish physical flash power-loss guarantees. See the existing
[LittleFS qualification notes](../badge/tools/storage-qualification.md).
