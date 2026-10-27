# Native SDK interface assessment

Assessed sibling checkout: `powersync-native` revision
`96a94f3e6e9442c4181d793f95241c2ee9cab4d8`, crate `powersync 0.1.1`,
edition 2024. The sibling was clean on branch `main`. All source references
below are relative to that checkout unless marked otherwise. This file is a
source assessment; executable proof results are recorded separately.

## Result

The full SDK has useful platform interfaces and is not fundamentally tied to
Tokio, reqwest, rusqlite, WAL, or multiple SQLite connections. It is nevertheless
currently a `std` crate. Disabling optional integrations does not make it
`no_std`. On Badgeware, a real port needs an SDK/dependency `no_std + alloc`
patch set, a deliberately owned SQLite interface, bounded cooperative work, and
platform bindings. This is more than changing the stream parser or providing an
HTTP callback. Do not label a core-only `.mpy` as the native SDK.

## Smallest useful configuration

Target dependency declaration starts with
`powersync = { path = ".../powersync", default-features = false }`.
No `tokio`, `smol`, `reqwest`, or `rusqlite` features are necessary for the
SDK design. `powersync/Cargo.toml` unconditionally links
`powersync_core = "=0.5.4"` and `powersync_sqlite_nostd = "=0.5.4"`, both
with `static`. These versions match this app's native SQLite/core pin.

The SDK requires allocator-backed `Box`, `String`, `Vec`, `Arc`, collections,
async task state, streams, and serialized JSON. It imports `std` throughout its
unconditional modules. `std::sync::Mutex` is used by `sync/status.rs`,
`sync/state.rs`, and `db/watch.rs`; `std::path::Path` by database opening;
`std::io::Error` by `error.rs`; `SystemTime` by subscription/status timestamp
APIs. Much of the rest is movable to `core`/`alloc`, but `HashSet`, mutexes,
path/IO types, dependency default features, and public timestamp types require
deliberate replacements or feature gates. There is no existing embedded feature.
The ARM reproducer, rather than this source inventory, determines the first
actual build failure.

## Platform seams and blockers

| Boundary | Existing SDK support | Smallest required work | Owner |
| --- | --- | --- | --- |
| Task execution | `env.rs:116` `AsyncRuntime::{spawn,delay_once}`; `PowerSyncTask` accepts `async_task::Task`; `SyncCoordinator::connect` spawns download, upload, and checkpoint retry tasks (`sync/coordinator.rs:36`) | Cooperative queue polled only from Badgeware foreground; timer futures use monotonic ticks; audit/disable dependency `std` features | Adapter plus SDK/dependency port |
| HTTP/TLS | `http.rs:18` `HttpClient::send` returns status, MIME, and asynchronous chunks; no reqwest required | Turn existing verified TLS transport into request/response handles and bounded byte chunks; drop/cancellation closes sockets | Adapter |
| SQLite | `ConnectionPool::single_connection` (`db/pool.rs:87`) serializes reader and writer leases on one async mutex | Public no-rusqlite connection factory or safe ownership-transfer constructor plus leased query API | Small SDK API patch plus binding |
| Database ownership | `SqliteConnection` and `RawSqliteConnection` exist, but their `db::connection` module is private (`db/mod.rs:28`); `LeasedConnection` raw access is `pub(crate)` | Expose a deliberate native API. Existing Python `exec(sql)` cannot implement the SDK's prepared/bound statements or BLOB operations | SDK API patch |
| Allocator ownership | Core transfers Rust-owned error strings to SQLite; current static wrapper globally uses `SQLite3Allocator` | Use one compatible shared allocator and requalify its total size, or patch every SQLite transfer site to allocate explicitly from SQLite before introducing a separate SDK heap. Independent per-crate global allocators cannot be linked into one Rust program | Native binding/core patch if separating heaps |
| Bounded work | No fixed operation or byte budget in `DownloadClient::run` (`sync/download/sync_iteration.rs:69`); ready futures can continue within one poll, and one service record can contain many operations | Stream/split data operations into bounded core records, then explicitly yield after bounded operations; preserve operation order and checkpoint transaction boundaries; adapter read budget alone is insufficient | SDK patch |
| Bounded input | `LineSplitter` (`util/line_split.rs:36`) grows `unfinished_line` without a maximum; `BsonObjects` accepts any length >= 5 (`util/bson_split.rs:70`); `ResponseBody::read_fully` (`http.rs:54`) trusts/grows response size | Incrementally stream/split NDJSON data operations preserving the pinned core contract, or negotiate a bounded protocol. Cap operation/control/checkpoint/BSON allocations separately; do not reject ordinary service data lines at 8192 bytes. Prefer NDJSON initially and avoid unexpectedly selecting BSON via the current Accept header | SDK patch plus adapter limits |
| Game uploads | `BackendConnector::upload_data` is invoked only when `ps_crud` has an item (`sync/upload.rs:119–154`) | Keep durable RPC uploader as app task, or add generic external-outbox pending/wake support. Do not invent `ps_crud` entries | App adapter or small SDK extension |
| Published confirmation | `request_checkpoint()` and reusable `CheckpointRequest` are public; `Instruction::DidCompleteSync {}` discards payload and full/partial distinction (`sync/instruction.rs:49`, `sync_iteration.rs:165`) | Expose a full-publication generation/receipt and applied request ID after commit, so game confirms exact UUID/room only on a full publication | Small SDK event API patch |

`Send + Sync` bounds on runtime, HTTP, connector, futures, and `Arc` are real
interface requirements, even on a single core. A binding must not put unrooted
MicroPython objects in those futures or use unsafe `Send` to conceal arbitrary
cross-thread Python access. A single-foreground adapter may keep opaque integer
handles in Rust and root all Python callback objects in its C/Python owner.
Wakers enqueue native work; they never reenter the Python VM.

The current Python `StreamLines.MAX_LINE = 8192` is an output-operation/control
record limit, not a service data-line limit. Its incremental framing retains
one operation and generates a core-compatible data envelope for that operation.
The matched 9,114-byte fixture has a single 9,067-byte data line containing all
50 operations and is accepted by that path. Upstream's `LineSplitter` buffers
that complete data line before core processing, which contributes to the host
memory peak. Simply adding an 8192-byte wire-line cap would reject this existing
valid fixture and would not reproduce the current bounded-memory contract.
The minimal framing work must split/stream data operations while preserving
bucket metadata, raw operation JSON, and order using core 0.5.4's verified
`bucket`/`data` consumption contract, with separate bounded handling of
control/checkpoint JSON and BSON. A negotiated bounded transport is an
alternative only if the existing service contract actually supports it.

## SQLite integration

`ConnectionPool::open` is unsuitable: it requests WAL, allocates a 50 MiB cache
budget per connection, and opens five readers (`db/pool.rs:32–57`). This is a
convenience implementation, not an SDK requirement. `single_connection` does
none of that, installs core update hooks, and uses the writer mutex for readers.
`SqliteConnection::prepare` and `ManagedStmt` are used directly for binding
text, NULL, integers, and BLOBs. `DownloadEvent::BinaryLine` binds bytes directly
(`sync/download/sync_iteration.rs:317`), and core results are read without the
Python module's 32-row result limit.

The preferred candidate is one linked `.mpy` containing SDK, SQLite, and core,
with one connection owned by the SDK pool and UI queries through that same
pool. Build core only once and use the app's existing static SQLite symbols.
Neither a second SQLite copy nor a borrowed connection crossing two independently
linked `.mpy` modules is acceptable. The current `RawSqliteConnection` closes
the connection on drop (`db/connection.rs:206`), so any future `from_raw_owned`
ABI must transfer ownership once and ensure Python cannot also close it.

The SDK's per-line `TransactionGuard` executes BEGIN/control/COMMIT synchronously
and rolls back on drop (`db/connection.rs:102`,
`sync/download/sync_iteration.rs:274`). It does not hold this transaction while
waiting on network IO. This can work with DELETE journaling, FULL sync,
`SQLITE_THREADSAFE=0`, and one cooperative database owner. It must retain the
existing storage reserve, fault propagation, and shadow VFS behavior. The
existing 256 KiB SQLite/core arena has not been qualified to include native SDK
task state, line buffers, or SDK JSON. Measure that additional budget whether
it is provided by an enlarged shared arena or a deliberately separated heap.

An additional allocator ABI requirement follows from the actual core source:
the badge's `powersync_static` wrapper installs `SQLite3Allocator` globally,
and core error paths transfer `CString::into_raw()` memory to SQLite to free
(`native-sqlite/vendor/powersync/crates/core/src/lib.rs:55` and
`crates/sqlite_nostd/src/nostd.rs:869,921`). A single linked Rust program has
one global allocator. Combining SDK and core therefore either puts all Rust
allocations through SQLite's allocator, requiring a newly measured combined
arena budget, or requires explicit SQLite allocation for every transferred
core allocation while the SDK uses a separate allocator. Assigning independent
global allocators by crate is not a solution. The final host proof routes all
SDK/core and post-setup driver allocations through one compatible SQLite
allocator; preloaded fixture/startup allocations retain System provenance. It
also preserves raw malloc pointers for host types requiring alignment greater
than the SQLite pool's 64-byte alignment. Its observed shared peak includes
host driver allocations and does not establish the target's minimum arena size.
Fault/error ownership transfers still require dedicated qualification.

Core update hooks must remain installed. `OwnedConnectionLease::drop` fetches
`powersync_update_hooks('get')` and notifies watchers (`db/pool.rs:216`). UI SQL
must release leases before scheduling work; synchronous pool methods can block
the foreground if called while another task owns the connection. Prefer
asynchronous operations represented as pollable request handles. The lease-drop
path currently discards update-hook read errors and deserves a fault-injection
test before adoption.

## Foreground lifecycle

1. C entrypoint owns the Rust allocator and opaque SDK session. Python roots
   transport callbacks and the existing ShadowVFS for the entire session.
2. `step(elapsed_ms, budget)` services due timers and transport readiness, then
   polls the native runnable queue for a bounded amount of work. A native
   explicit record yield is necessary to make this bound meaningful.
3. TLS/socket readiness sets response-handle state and wakes tasks on the next
   foreground step. No Embassy hardware executor takes over Badgeware devices.
4. The Home path begins asynchronous disconnect, continues foreground polling
   until task cancellation is joined, closes every response handle, then releases
   subscriptions, connection, rooted callbacks, and allocator state. A timeout
   must report incomplete cleanup rather than freeing memory still reachable by
   wakers.
5. SDK failures become status/error values. MicroPython NLR is caught inside C
   callback wrappers before returning to Rust; panic/abort policy must prevent
   unwinding through MicroPython. Watchdog servicing surrounds bounded native
   calls; it cannot interrupt a long synchronous SQLite publication.

The SDK already supplies cancellation: `PowerSyncTask::cancel_and_join`
(`env.rs:142`), and `SyncTasks::cancel` joins all three tasks before explicit
core stop (`sync/coordinator.rs:85`, `:259`). Response body/transport Drop must
close its socket for this cancellation to release Badgeware networking resources.

## Game contract

Use the existing raw-table schema through `SchemaOrCustom::Custom`, preserving
compressed UUIDs, raw room/move table SQL, and externally durable session state.
`sync_stream("badge_game_v2", room parameters).subscribe(...)` expresses the
existing subscription; set `include_default_streams` false. Explicitly retire
old persisted subscriptions before room/account changes.

The RPC uploader stays responsible for immutable move UUID retries, credentials,
and accepted/rejected state. HTTP success keeps the durable pending move. Only
a post-commit full-publication receipt plus a query for exact move UUID and room
may confirm it. A generic table watch or `has_synced()` is insufficient:
`has_synced` records whether a priority has ever synced, including cached state
(`sync/status.rs:163`). On rejection/stale cache, request a fresh covering
checkpoint with `request_checkpoint()`, wait for it, then apply the existing
barrier rules. Request handles survive disconnect/reconnect, not clear
(`sync/checkpoint.rs:15`). Keep that barrier distinct from pending-move identity
confirmation.

Before account switching, preserve pending work and atomically reconcile the
session identity with the local account record as today. The SDK's hard clear
with `clear_local: false, soft: false` maps to the game's core `clear(0)` when
performed within the guarded reconciliation design. Do not use soft clear for
different accounts, expose cached game rows before reconciliation, or blindly
clear a session with an unresolved move.

## Responsibilities after a successful port

| Current responsibility | Target owner |
| --- | --- |
| Core instruction orchestration, reconnection, subscription lifecycle, status, stream framing, checkpoint request seeding/retry | Upstream native SDK plus defined bounded/no_std patches |
| Foreground executor, timers, Python exception barrier, rooted callbacks, verified TLS, file callbacks, allocator | Badgeware platform binding |
| SQLite/core and shadow VFS integration | One native component and serialized SDK pool; existing Python VFS may remain behind C exception barrier |
| Five-letter pairing, durable account/session record, move RPC and immutable UUID retries, full-checkpoint exact-move confirmation, game/UI | Existing Python application adapter |

Minimum Python-facing surface: `open(schema, owned storage callbacks)`,
`connect(room, credential handles)`, `step(elapsed_ms, budget)`,
`status()/take_events()` with full-publication generation and applied request ID,
bounded leased `query/execute` operations, `request_checkpoint()` request handles,
and `disconnect/close` completion. Existing `Sync.step`, `close`, `status`,
`notice`, and `generation` can be preserved by a thin Python wrapper. Pairing
and game helpers continue reading/writing through the shared SDK database owner.

## Cost and recommendation

Proceed as an explicit port milestone, with the working client retained until
device qualification. A working host SDK is evidence for the platform interface,
not a sideloadable native SDK app. Budget roughly 3–6 engineer weeks for dependency
port, owned database ABI, native module/executor/transport bridge, limits/events,
and device/game qualification, with 1–2 further weeks of contingency for PIC,
memory, or dependency work. This is an estimate, not a build result. The
maintenance cost is a pinned upstream branch plus audits whenever upstream
dependencies, task orchestration, or core ABI change.

Rust framing removes Python record construction/scheduling from the hot path,
so an improvement is plausible. Upstream currently commits each control event,
whereas the custom client batches up to six records: flash/SQLite cost may
offset parsing gains. No speedup is established until matched on-device tests
include both full-board processing and UI/p95 foreground latency with the same
storage configuration. Preserve the existing client if the candidate cannot
meet the app's bounded memory, frame-time, and durability constraints.

Official API documentation was requested at `https://docs.rs/powersync/0.1.1/powersync/`
and pinned GitHub source during this assessment; these URLs were unavailable
through the web tool. This assessment therefore uses the actual pinned checkout,
not unverified claims about the current published API.
