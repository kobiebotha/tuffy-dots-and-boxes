# Game baseline and adoption contract

Assessed 2026-10-26 at app revision `4867f40ebc777700478e84cd8ca6a8a3e481c4a5`
(initial tree clean) and native SDK revision
`96a94f3e6e9442c4181d793f95241c2ee9cab4d8`. No app, package, backend,
session, installed badge or cached native artifacts were changed.

## Baseline identities and tests

| Artifact | Identity |
|---|---|
| SQLite / PowerSync core | 3.54.1 / 0.5.4, core commit `0ed9f4edba418a4f8ae7cb0594da683cd79a81b5` |
| `native-sqlite/prebuilt/powersync/native_sqlite.mpy` | 737,192 bytes; SHA256 `359c1f5e619357c20d777414165540b55d290af63994ddb44d0d3a210cbcb9d5` |
| Cached host library | 1,789,704 bytes; SHA256 `d938c20ac76c6532d63e9bd42e9ab0d531cfc36db346e84ce4fe46b955509030` |
| `badge/lib/powersync/sync.py` | SHA256 `462eb4b8ad2be702ae3f4b5f15bf512b7102b80c339a3926e2de4907196042a9` |
| `badge/lib/powersync/stream.py` | SHA256 `a33073941e5d7c6deb37466d033daaaed6572bf061b308805268d5ef8baeffd3` |
| `badge/lib/powersync/database.py` | SHA256 `a7cd5b7348dc3a79a0ee47c14f07a2d9102936e13272c266494b56746d6081ee` |
| `badge/app/dots_boxes/dots_sync.py` | SHA256 `4659261eb22090415caa3e32fd4c4e1847211822d32dd114fe336529969e5399` |
| Extracted `sqlite-shadow-vfs/sqlite_shadow_vfs.py` | SHA256 `0392ee099ff3c88610a24a3c18cdef9e63ff89405eec6a58e414bd9cf3bdd5be` |

Reproduction from repository root:

```sh
python3 -m unittest discover -s badge/tests -v
python3 experiments/powersync-native-sdk/game/benchmark_baseline.py
python3 experiments/powersync-native-sdk/game/benchmark_real_core.py --repetitions 31
```

All **181 tests passed in 28.372 seconds**, with no skipped tests. The complete
log is [`game/baseline-tests.log`](game/baseline-tests.log). Native tests used the
existing pinned host library; no build or packaging command ran. VFS tests build
their small callback bridge in a temporary directory, and each database/session
uses temporary files. Expected injected-failure diagnostics at the end are not
test failures. The host fixture keeps the constrained allocator/SQL profile,
substituting a POSIX VFS; actual ShadowVFS tests then install Python callbacks.
These tests do not execute ARM code. README's extraction-era native binary still
requires a Tufty smoke test; historical device results qualify older identities.

## Semantics that the native adapter must retain

| Contract | Current evidence | Native SDK consequence |
|---|---|---|
| Five letters using A/B/C; paired room and seat saved durably | `dots_pairing.CodeEntry`, `dots_client.Client._pair_step`, session tests | Pairing remains application Python; no auth/backend redesign. |
| Exactly `badge_game_v2` with `params.room_id`, defaults excluded, retired TTL subscriptions removed | `dots_sync.Sync`, `Database.start_sync`, `test_real_request_uses_one_room_and_removes_all_persisted_ttls` | Native stream subscription API plus explicit persisted-subscription reconciliation. |
| Immutable move UUID saved before HTTP; retry/reboot reuse it | `Client._queue_move`, `SessionFile.save`, native-VFS UUID/reopen test | External durable outbox remains application-owned; no `ps_crud` conversion. |
| HTTP acceptance keeps pending move | `GameConnector.upload_data` and `checkpoint_applied` | A successful upload future must not call application confirmation. |
| Confirmation requires exact published move UUID AND room AND a full checkpoint | `Store.has_move`, `GameConnector.checkpoint_applied`, `test_accepted_move_requires_exact_published_uuid_room_and_full_checkpoint` | Require a committed full-publication receipt/generation, then query public `moves`; internal oplog or arbitrary SQL watch is insufficient. |
| Reject clears preview but blocks claims until server checkpoint covers rejection | `SyncClient._after_upload`, seed/barrier phases, reject tests | SDK `request_checkpoint().wait_for_sync()` can supply freshness, together with the full-publication receipt. |
| Reconnect retains accepted UUID without duplicate in-process upload; reboot retries idempotently | `uploaded_move_id`, reconnect and reopen tests | Keep in-memory accepted marker separately from durable pending state. |
| New account cannot expose old cached rows or discard pending work | `Store.reconcile_session`, `Client._pair_step`, account-switch fault tests | Stop SDK, save validated new session, transactionally clear synced/internal state and change owner before reading board; block switch if pending exists. |
| Home and failures preserve state; cleanup runs in foreground | app `__init__.py` IRQ flag/finally, `Client.close` | Cancel tasks/socket first, finish or roll back SQL, close connection and roots; no Rust callback from IRQ. |
| Journal/full-sync durability and storage reserve remain | `ShadowVFS`, native-VFS short-write/flush/power-loss tests | Reuse tested VFS contract; preserve DELETE/FULL and no transaction across network waits. |

The current custom supervisor bounds each foreground update to **two transport
reads and six records**, allows at most one checkpoint publication in each SQL
transaction, and uses an 8,192-byte sync-record bound. These protect UI service
time and the fixed arena; preserving only eventual row equality is insufficient.
`http.HTTPS.step` retains TLS certificate validation (`CERT_REQUIRED`) and hostname
binding (`server_hostname=self.host`). It can remain a platform transport adapter
if its pending/would-block/cancel states map to Rust polling without unbounded
buffers or blocking calls.

The native SDK has two concrete game integration gaps at this revision:

1. `sync/upload.rs::CrudUpload::upload_step` invokes `BackendConnector.upload_data`
   only when `oldest_crud_item_id()` finds a `ps_crud` item. This game's queue is
   external and the tested invariant is zero `ps_crud`. Use a separate app upload
   task/future and SDK checkpoint API, or add an external-outbox trigger interface.
2. `sync/instruction.rs::Instruction::DidCompleteSync {}` and
   `download/sync_iteration.rs::DownloadClient::handle_event` provide no public
   full/partial committed-publication receipt; the handler only clears errors.
   `SyncStatusData::has_synced()` may reflect older syncs and cannot establish
   freshness. Add a receipt carrying full/partial scope and monotonically
   increasing committed generation; retain exact UUID/room verification after it.

The existing contract tests can be reused through a thin adapter, but several
supervisor tests deliberately inspect custom Python phases/internal controls.
Keep those as baseline tests; add adapter-level native tests for the same external
invariants rather than rewriting their expectations to make the candidate pass.

## Minimum UI-facing surface

An integrated native module needs `open(schema, vfs_callback)`, bounded typed
query/execute, `subscribe(name, params, include_defaults=False)`, `connect`,
`poll(elapsed_ms, work_budget)`, `status`, a committed-full-publication generation,
`request_checkpoint`/completion, `disconnect`/`close`, and resource statistics.
Python retains credentials/session files, pairing, RPC upload/idempotency, account
ownership/migration, game replay, controls and drawing. The SDK owns core
instruction dispatch, download/reconnect/checkpoint orchestration and stream
framing. Platform bindings own executor wakeups/timers, TLS sockets, SQLite
connection serialization, VFS callbacks, GC roots and exception boundaries.
Only one supervisor may own the connection. The existing `Database.control` and
native SDK must never run concurrently against it.

## Performance evidence and next device experiment

The historical device 716.36 ms result is **framing, decode/construction and
scheduling into a checked memory sink**, 9,114 bytes / 50 operations, excluding
Wi-Fi, TLS, SQLite and flash. It is not parser-only or full sync latency. Its
2026-10-21 source/native identities predate extraction. Current
`benchmark_download.py::load_sources` already resolves the extracted library
layout; no obsolete import fix was necessary.

The isolated helper reuses this exact synthetic fixture and checks every output
operation. Its [host result](game/baseline-download-host.json) is median **1,308 µs**
over 21 repetitions on CPython 3.15.1/macOS arm64: 9 foreground updates, 6
transport reads and 9 sink commits. Source and fixture digests are included.
This is a reproducible host baseline, **not a measured SDK speedup**. A native
host proof including SQLite/core is a different workload and cannot be divided
into this number to claim improvement. A firmware-native implementation may
reduce Python work while increasing transactions/flash work, so end-to-end
improvement is unproven until matched hardware testing.

A second [real-core host baseline](game/baseline-real-core-host.json) times the
current `SyncClient` against the cached native qualifier on a **fresh POSIX file
database each trial**. Schema/open/start and checkpoint metadata are outside
timing; timing starts before the same 9,114-byte wire and ends once full
publication is reported. The metadata explicitly assigns the fixture bucket to
subscription 0 of `badge_game_v2` at priority 3. Every trial then validates all 49
published IDs, room bindings, edges and indexes, the room's complete fields, zero
`ps_crud` and `quick_check=ok` outside timing.

Across 31 trials: **median 5,856.000 µs; p95 6,806.500 µs; min 5,578.417 µs;
max 7,160.292 µs**. Every trial used 9 foreground steps, 6 reads and 9 SQL
transactions; the resulting file was 51,200 bytes. The SQL profile is
`host_config.h`, 256 KiB arena, page size 1,024, cache 32 pages, DELETE/FULL.
SQLite's observed peak was 156,608 bytes, including the cached static core's
SQLite-allocator allocations; Python heap is excluded. This can be compared
with an SDK host proof using the same timed workload and SQL profile, with
explicit caveats for different core/Rust allocation, transport poll granularity,
transaction batching and host filesystem effects. It remains **host evidence**;
it does not establish sideloadability or better badge performance.

After a loadable candidate exists, use an isolated app/database/session and:

1. Import/reimport, force GC during pending callbacks, and repeatedly enter/Home
   exit; verify roots, cancellation, no leaked sockets/tasks and watchdog service.
2. Run the same 9,114-byte fixture through both implementations on the same badge,
   same fragment sizes and checked sink, alternating order across multiple rounds.
   Record medians, tails, max foreground step, allocations/peaks and module size.
3. Separately run real-core full-board and live single-move cases on identical
   fresh copied states and VFS profile. Record SQL transactions, flash bytes and
   durability barriers alongside timing; do not mask more flash work behind a
   faster Rust framer. Measure Wi-Fi/TLS/end-to-end behavior separately.
4. Repeat exact UUID/wrong UUID/wrong room, partial/full checkpoint, stale reject,
   reconnect, reboot, token revocation, account switch and 49-edge reopen tests.
5. Inject short write/flush failure; reset at journal/session rename boundaries;
   retain uncertain state. Physical power-cut qualification remains separate.

Sideloadability, use of upstream orchestration, and better performance are three
independent gates. The current baseline establishes test fixtures and comparison
conditions; it does not yet satisfy any native-SDK device gate.
