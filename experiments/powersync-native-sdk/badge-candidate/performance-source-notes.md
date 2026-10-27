# Source attribution for badge performance investigation

These are source-derived hypotheses and instrumentation boundaries, **not badge
latency measurements**. Read these alongside the same-badge benchmark results.
Paths beginning `powersync-native/` refer to the sibling source checkout. The
line references identify the investigated sources before any optimization.

## Connection and TLS

Both clients use the same stepped DNS/TCP/verified-TLS design, and both ask for
`Connection: close`; a new HTTP task creates a fresh SSL context and socket.
The native wrapper substitutes SDK-authored HTTP request bytes without replacing
the TLS transport (`apps/dots_boxes_native/powersync/sync.py:37-65`).

In `apps/dots_boxes_native/powersync/http.py`, `HTTPS.step()` has these useful
boundaries:

| Phase before call | Work included | Lines |
| --- | --- | --- |
| `dns` | DNS query progress and initial nonblocking TCP connect | 216-241 |
| `tcp` | readiness check, SSL context/ciphers, CA file reads, wrap_socket | 242-262 |
| `send` | TLS handshake progress and HTTP request write, 512 bytes/call | 263-275 |
| `read` | TLS read and HTTP framing; 2,048 bytes/call for streams | 279-293 |

`do_handshake_on_connect=False` means timing `wrap_socket` alone does **not**
measure TLS handshake. The `send` phase includes handshake and request write.
Without an extra firmware TLS hook, label this honestly as **TLS + request
send**, with the SSL setup time listed separately. Record wall duration from
phase entry to exit as well as summed active step duration. Time waiting between
steps belongs to scheduling/pacing, not TLS CPU time.

The native adapter has a specific extra-reconnect hypothesis:

1. It delays the initial stream until its SDK seed checkpoint request finishes
   (`powersync/sync.py:158-161,261-265`).
2. When a later checkpoint JSON request appears while a stream is active, it
   sends `{op: error}` to Rust and closes that stream **unconditionally**
   (`powersync/sync.py:249-256`). This path is not gated by `_single_tls`.
3. An SDK download error requests the ordinary five-second retry
   (`powersync-native/powersync/src/sync/download/mod.rs:24-36`,
   `sync/options.rs:24-29`). Its following iteration seeds again and opens a new
   TLS stream.
4. An upload itself uses another foreground HTTP task. Memory pressure can
   explicitly release SDK HTTP and enable `_single_tls`
   (`powersync/sync.py:305-306,332-333,390-400`).

The original client keeps its live stream during a successful upload/checkpoint
when the second TLS connection fits (`badge/lib/powersync/sync.py:239-247`). Its
MemoryError fallback relinquishes that stream (`:218-225`). On a normal start,
it opens the stream directly; only `needs_write_barrier` routes establishment
through its seed/barrier phases (`:127-131,317-337`). Native Requests mode seeds
on every download iteration. Startup request counts therefore matter even
without a queued upload. The SDK must retain ownership of its seed/write target.

Capture task path/type, task creation and close times, header and first-byte
times, phase timing, reconnect count, deliberate stream errors, `_single_tls`
transitions, MemoryError, and checkpoint request count. Avoid logging auth
headers, tokens, session records or request/response bodies.

## Foreground SDK scheduling

Native `SyncClient.step()` polls Rust, performs credential/upload/HTTP steps,
then polls Rust again (`powersync/sync.py:366-385`). Each poll asks for a budget
of six (`:116-117`). The bridge advances SDK timers only using the elapsed
milliseconds passed by the app, then does at most six foreground executor polls
(`badge-module/rust/src/lib.rs:722-751`). `Engine.tick()` polls only if a wake is
pending (`:232-237`). Waking never re-enters the executor.

SDK download handling yields after every event
(`powersync-native/powersync/src/sync/download/sync_iteration.rs:74-94`). This
bounds records between executor polls; it does **not** bound time spent inside
one large record or its synchronous FULL commit. The native adapter reads at
most one transport chunk per update (`powersync/sync.py:241-244`). The original
download loop allows two immediate reads and six synthetic records per update
(`badge/lib/powersync/sync.py:7-8,342-385`). Consequently packet fragmentation
and the real managed-display cadence can affect native wall time even if the
same bytes take little Rust CPU time.

Capture update start/end and gap/max, sdk_poll duration, sdk_reply duration,
input chunk sizes, PENDING read count, final input-feed timestamp, publication
timestamp, and rendering/VSYNC time. Subtract nested VFS callback duration from
sdk_poll time only once; the remainder still includes core parsing, SQLite CPU,
SDK work and mailbox serialization. It cannot be called pure scheduler CPU.

The bridge encodes incoming bytes as base64 inside JSON on the Python side and
decodes them in Rust (`powersync/sync.py:287-290`,
`badge-module/rust/src/lib.rs:892-924`). The SDK line splitter rescans its
unfinished whole record after each fragment and retains it until newline
(`powersync-native/powersync/src/util/line_split.rs:37-71`). Large historical
records can therefore increase both whole-record latency and peak memory.

## SQLite transactions and VFS

Both stores use 1,024-byte pages, a 32-page runtime cache, `synchronous=FULL`,
DELETE journaling and the bounded ShadowVFS. The candidate VFS source is
byte-identical to `sqlite-shadow-vfs/sqlite_shadow_vfs.py` at investigation time.
The database shadow is RAM-backed, but commit durability is physical:

- Database `xSync` rewrites the complete logical image when dirty, calls
  `flush()` and `os.sync()` (`sqlite_shadow_vfs.py:78-87`).
- Journal `xSync` calls `flush()` and `os.sync()` (`:202-208`).
- Journal deletion can also call `os.sync()` (`:126-134`).
- Random database writes merely dirty the shadow; their callback time alone
  under-reports physical commit cost (`:160-172`).

The C VFS allocates/converts arguments and returns to Python per callback
(`badge-module/native/src/vfs.c:9-16,53-66`). Measuring the wrapped Python VFS
is a useful lower bound on total VFS cost; its timer does not include all C
argument/result conversion overhead.

Every native `DownloadEvent` invokes `powersync_control` inside a new
TransactionGuard and commits before returning instructions
(`powersync-native/powersync/src/sync/download/sync_iteration.rs:97-105,282-305`).
The original splits a service data record into individual operation records
(`badge/lib/powersync/stream.py:94-108`) and batches up to six records into one
transaction, flushing on publication (`badge/lib/powersync/sync.py:388-415`,
`badge/lib/powersync/database.py:371-416`). Therefore transaction amplification
depends on the wire workload, not simply the number of game rows.

A host-only framing check using the actual original `StreamLines` and
`SyncClient._download`, 2,048-byte immediate source fragments and a counting
Store produced:

| Synthetic input | Original control calls | Original record transactions | Native record transactions from source |
| --- | ---: | ---: | ---: |
| checkpoint + one 50-op data record + full completion (9,321 bytes) | 52 | 9 (eight batches of 6, then 4) | 3 |
| checkpoint + one 1-op data record + full completion (473 bytes) | 3 | 1 | 3 |

These exclude initialization, connection events, subscriptions, seed state,
CRUD completion, receipt/session writes and stop. They count transaction
boundaries, **not physical dirty commits or elapsed time**. Count actual DB
`xSync`, journal `xSync`, journal delete, bytes written and database logical
size on the badge. A read-only transaction may cause no flash write.

## Upload and confirmation invariants

Native upload processing must remain `BackendConnector::upload_data()` reading
the SDK CRUD transaction and completing it only for success or durable permanent
rejection (`badge-module/rust/src/lib.rs:350-418`). Retry returns IOERR and the
SDK actor retains the entry/UUID and owns the five-second backoff
(`powersync-native/powersync/src/sync/upload.rs:99-155`). The SDK also owns write
checkpoint sequencing (`:128-137,205-265`) and ten-second checkpoint reposts
(`sync/checkpoint.rs:147-196`, `sync/options.rs:89-95`). Changing timer values
to hide a reconnect is not an attribution or correctness fix.

Full-generation status is advanced after `invoke_control()` has committed
(`sync/download/sync_iteration.rs:167-170`). The adapter confirms pending work
only when full generation advances and the exact UUID/room is found
(`apps/dots_boxes_native/powersync/sync.py:169-187`). HTTP acceptance, cached
`has_synced`, partial completion and receipt writes cannot replace this gate.

Any later optimization needs to retain raw `rooms`/`moves`, SDK upload ownership,
its retries and checkpoint targets, and crash ordering of pending/session and
upload receipts. Existing adapter/outbox and host bridge tests exercise these
contracts; include real same-badge checks after changing a measured bottleneck.
