# Storage hot-path changes

The selected configuration retains 1 KiB SQLite pages, a 32-page cache, the
128 KiB PSRAM database shadow, the pinned native module, and FULL DELETE
journaling. It preserves every file flush and global sync barrier.

Schema 5 stores both raw tables WITHOUT ROWID. A schema 4 upgrade retains its
published rows, account, native client ID, oplog and checkpoints. The transaction
stages each raw table in TEMP memory, drops its former table and primary-key
index, and creates the smaller replacement using freed pages. A temporary
page-count limit prevents database growth, because the VFS cannot truncate a
grown file on rollback. Existing schema 1–3 upgrades retain their prior synced
cache replacement behavior. Native tests cover a completed 49-move upgrade,
failed-COMMIT rollback, continued sync and removals, and unchanged pending IDs.

An existing SQLite file retains its high-water length after migration. The
database-image byte reductions below apply to newly created caches. Relayout
alone does not make an existing larger file's sequential snapshot smaller.

Storage accepts the stream framer's already decoded records while retaining
envelope, table, operation, byte and page-budget validation. It records checkpoint
flags during validation, reads the two page PRAGMAs through one native call,
and avoids checking the identical page snapshot twice before the first record.
The VFS returns a buffer view for database reads; the existing native bridge
already consumes any readable buffer and copies it synchronously into SQLite.
Move reads support `moves(room_id, after=0)` with bounded keyset pages, and room
UUID encoding is cached for the current room.

The three-run selected-layout report is
`storage-hotpaths-comparison-results.json`. The fresh-cache live-game scenario
uses identical service operations, checksums, checkpoints, and full board reads:

| Measure | Previous rowid storage | Selected storage |
| --- | ---: | ---: |
| Completed live database | 58,368 B | 54,272 B |
| Complete database-image bytes written | 2,367,488 B | 2,229,248 B |
| SQLite sync barriers | 150 | 150 |
| Native exec calls | 962 | 663 |
| Storage JSON decodes | 597 | 199 |
| Database read callbacks | 353 | 329 |
| Worst native allocation | 154,176 B | 156,352 B |

The call counts include application board reads but exclude framing's initial
decode. The benchmark includes that initial decode in CPU timing for both paths.
Host timings include macOS `os.sync`; they establish no badge latency improvement.
The wrapper does not reproduce MicroPython allocation or actual flash programming.
The separate `storage-hotpaths-cold-results.json` repeats the 99-operation cold
history scenario three times per configuration; all cases finish and reopen the
49-move board with zero SQL CRUD.

## Session overlay

In-memory sessions keep their existing canonical version-1 shape. On disk, a
format-2 base envelope holds the session and its generation. Changes only to a
pending move replace a small `pending.json` overlay with that generation.
Pairing, credentials, account, room, or other base changes atomically replace the
base and advance its generation. Older overlays are ignored, including after a
reset immediately following the base rename. Existing plain version-1 files are
read as generation zero and can use the overlay immediately.

Both files retain write, flush, global sync, atomic rename, global sync ordering.
The in-memory saved state advances only after every step succeeds. Fault tests
cover short writes, interrupted syncs and renames, legacy loading, account/room/
credential binding, orphan overlays, and retry after failed durability barriers.
This is software fault qualification, not a physical power-cut test. Older app
versions cannot read the format-2 base or accept the new overlay files.

`session-overlay-byte-results.json` measures real temporary-file saves with
synthetic 2,048-byte access and 512-byte refresh tokens. Queueing and clearing one
move previously wrote 2,906 + 2,824 bytes; the overlay writes 123 + 48 bytes:
**97.02% fewer application file bytes**, with the token file unchanged. This does
not reduce the count of durability barriers or establish flash-sector savings.

## Rejected and deferred configurations

`storage-hotpaths-sweep-results.json` records the layout/page/cache sweep.

- An index on `(r,n)` increased database-image bytes and journal/read work for
  this bounded 49-move workload. It is not installed.
- A 24-page cache caused more reads. A 48/64-page cache saved only eight database
  read callbacks versus 32 pages while consuming about 20 KiB more native memory.
- 2 KiB pages grew the live database to 79,872 bytes. 4 KiB pages exceeded the
  96 KiB used-page budget even before downloading the fixture.
- A fresh WITHOUT ROWID cache at 512-byte pages / 64 cache pages completed at
  45,056 bytes and wrote 1,745,920 database-image bytes. This remains experimental:
  the no-truncate VFS cannot change an existing file's SQLite page size through
  its current migration path, and this option needs cold-history and device
  qualification. It is not selected.

Reproduce with the service-produced `fixtures.json` and a saved pre-change
`dots_storage.py` / `dots_session.py` snapshot. The report records source hashes.

```sh
python3 badge/tools/benchmark_storage_hotpaths.py /path/to/fixtures.json --baseline=/path/to/baseline/dots_storage.py --selected-only --repetitions=3 --output=/private/tmp/storage-comparison.json
python3 badge/tools/benchmark_storage_hotpaths.py /path/to/fixtures.json --baseline=/path/to/baseline/dots_storage.py --output=/private/tmp/storage-sweep.json
python3 badge/tools/benchmark_session_overlay.py --baseline=/path/to/baseline/dots_session.py --output=/private/tmp/session-bytes.json
```

The scripts never access a badge or network service and do not modify the pinned
native artifact. Prior benchmark reports remain unchanged.
