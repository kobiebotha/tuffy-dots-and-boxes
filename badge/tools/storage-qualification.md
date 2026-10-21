# Database shadow: safety and qualification

The badge keeps a bounded 128 KiB database image in PSRAM. SQLite database reads,
writes and size queries use that image. At database `xSync`, the VFS writes the
complete logical image sequentially, then calls `file.flush()` and `os.sync()`.
It clears the dirty flag only after both succeed. The rollback journal remains a
physical file, with SQLite `synchronous=FULL` and `journal_mode=DELETE` unchanged.
Sync results are exposed only after SQLite COMMIT succeeds.

## Source basis

The matched Tufty MicroPython revision is
`pimoroni/micropython@41d1e007537f9b15cf13a9436228dd59d6485eca`. The local source root is
`../badgegotchi/experiments/badgeware-native/vendor/micropython-41d1e007537f9b15cf13a9436228dd59d6485eca/`.
This is the qualified source/ABI match, not a claim that the complete installed
firmware image has a verified Git identity.

- `extmod/vfs_lfsx_file.c:105` installs the mtime attribute; line 170 maps
  `file.flush()` to LittleFS `file_sync`.
- `lib/littlefs/lfs2.c:3185` marks writable opens DIRTY for file attributes.
  `lfs2_file_sync_` at 3434 synchronizes data before committing the directory's
  CTZ reference and attributes, then clears DIRTY at 3482.
- `lfs2_file_flush` at 3353 explains the expensive copying of the remaining tail
  after random writes. A full sequential replacement avoids those repeated tail
  copies. LittleFS retains the previous CTZ image until the replacement's
  directory commit, so space for another complete image is necessary.
- `lfs2_fs_traverse_` at 4782 also visits open DIRTY/DUSTY CTZ structures;
  `lfs2_fs_size_` at 5181 counts visits without deduplication.
  `lib/littlefs/lfs2.h:721` explicitly describes the result as best effort and
  potentially too large when copy-on-write structures are shared.

## Space admission and metadata flush

After loading an opened read-write database, the VFS flushes its initial mtime
metadata before any reserve check. Without that flush, an unchanged file can be
counted through both its directory entry and its open DIRTY handle, falsely
reducing reported free space by approximately the database's allocated size.
Read-only opens do not require this metadata flush.

Before BEGIN, storage admission requires free bytes at least equal to the
**current logical database size + 64 KiB** for the replacement snapshot,
journal and metadata work. A new directory still requires the full 128 KiB cap
plus that reserve. The check uses logical growth in the shadow rather than the
older physical file size. It is not repeated inside `xSync` or rollback, where
old/new images and the journal legitimately coexist. This is an admission check,
not a guarantee against every later allocation failure: write/flush failures
remain SQLite I/O errors, preserving dirty data and recovery evidence.

## What was tested

[Direct VFS tests](../tests/test_vfs.py) cover bounded loading, metadata flush,
read-only access, growth/zero filling, unsupported shrink, journal ordering,
short writes, write/flush/global-sync failures, close failures and reserve checks.

[Native integration tests](../tests/test_vfs_native.py) use the real pinned
PowerSync `0.5.4/cratesio` core in its 256 KiB arena. The host
[C adapter](../tests/vfs_bridge.c) forwards SQLite callbacks into the actual
production Python VFS through [PythonVFSCore](../tests/vfs_fixture.py), rather than
substituting a C storage implementation. They verify full 49-move replay and
reopen, exact UUID acknowledgement, zero SQL CRUD, failed-COMMIT rollback, actual
short physical writes and flush failures. A captured durable-journal boundary
also verifies that reopening preserves the files and pending UUID and refuses
the interrupted write.

These host tests use temporary POSIX files. **A physical power cut during flash
programming, LittleFS metadata commit or SQLite journal deletion has not been
tested.** The captured interruption and fault injections establish software
behavior; they do not qualify those device power-loss boundaries or flash wear.
