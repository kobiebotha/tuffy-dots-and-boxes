# Investigate the PowerSync native SDK on Badgeware

Prepared 2026-10-26. Workspace: `/Users/kobie/projects/powersync/skunkworks/tuffy-dots-and-boxes`.

Investigate replacing the custom MicroPython PowerSync client with the actual
[PowerSync native SDK](https://github.com/powersync-ja/powersync-native), while
keeping the Tufty 2350's existing Badgeware firmware, launcher, UI and working
game. Establish which upstream SDK code can run, what platform adapters or fork
changes it needs, and whether the result is worth adopting. Produce an isolated
local proof where feasible and an evidence-based recommendation. Do not switch
the application to the experiment as part of this investigation.

## Intent and history

The user's original plan was to fork the native SDK for `no_std` and use Embassy.
The early `powerbadge` experiments instead integrated the already-`no_std`
PowerSync SQLite core first. The user subsequently asked to install an ordinary
Badgeware app without replacing the firmware. SQLite and the core were packaged
as a native `.mpy`, and a custom client was written around Badgeware's existing
networking and filesystem APIs.

The history establishes why Badgeware was preserved. It does not establish that
the full native SDK could not be adapted to run inside it. No comparative
full-SDK port assessment was found in the architecture audit. Investigate that
question directly rather than treating the current design as a necessity.

Use these names precisely:

| Component | Meaning |
| --- | --- |
| PowerSync native SDK | The upstream `powersync-native` repository and its `powersync` Rust crate, including SDK orchestration and platform interfaces |
| PowerSync SQLite core | The Rust SQLite extension from `powersync-sqlite-core`, currently pinned here to 0.5.4 |
| Native SQLite module | This repo's `native_sqlite.mpy`, which bundles SQLite and optionally the core, plus MicroPython bindings |
| Custom MicroPython SDK | This repo's `badge/lib/powersync` client, which drives the core and manages transport, framing, reconnects and storage integration |

Porting just the stream framer or continuing to call `powersync_control` through
new Rust glue would be a separate optimization. It would not demonstrate reuse
of the full native SDK. Retaining Python for the UI or a thin platform adapter
is compatible with this goal: investigate whether upstream SDK orchestration
can replace our custom orchestration.

## Scope and preservation

- Never `git push`; the user pushes. Never use blanket staging such as
  `git add .` or `git add -A`. A plan involving PRs requires distinct local
  branches. No PR or new chat is required for this investigation.
- Never deploy to the PowerSync Vercel organization without user approval.
  This is client-side work; do not deploy Vercel, PowerSync, Supabase, migrations,
  sync configuration or backend changes.
- Start with `git status --short` and applicable agent instructions. Preserve
  existing changes. At handoff time, a VFS extraction and associated test,
  documentation and packaging changes are in progress. Reinspect rather than
  assuming the tree is clean or this inventory remains current.
- Keep new source, patches, reports and build outputs isolated, for example in
  `experiments/powersync-native-sdk/`. Leave the working SDK, native prebuilts,
  runtime digest pins, served packages and installed badge application intact.
- Initial scope includes source investigation, local builds and isolated host
  and ARM proofs. It does not include installing a candidate on the badge,
  changing firmware, or modifying real pairing/session/database state. Describe
  the device experiment required after local feasibility is established.
- Treat sibling `powerbadge` and `badgegotchi` projects as reference evidence.
  Do not edit them incidentally or revive their historical firmware pipeline.
  Keep credentials, private configuration and raw flash captures out of reports.

## Read the current implementation first

The reusable components have recently moved into this repository. Historical
references to `dots_stream.py`, `dots_http.py` and `badgegotchi_native.mpy` do not
describe the current source layout.

| Read | Purpose |
| --- | --- |
| [README](../../README.md) | App behavior, packaging, source layout and hardware qualification limits |
| [SDK sync](../../badge/lib/powersync/sync.py) | `SyncClient`, `Connector`, instruction dispatch, uploads, checkpoints and foreground stepping |
| [SDK database](../../badge/lib/powersync/database.py) | `Database`, native lifecycle, transactions, validation and core controls |
| [SDK HTTP](../../badge/lib/powersync/http.py) and [stream framer](../../badge/lib/powersync/stream.py) | Platform transport, HTTP framing and bounded NDJSON processing |
| [SDK runtime](../../badge/lib/powersync/runtime.py) | Firmware checks and native artifact identity |
| [Game connector](../../badge/app/dots_boxes/dots_sync.py) | Supabase credentials, immutable move RPC and confirmation through published rows |
| [Game storage](../../badge/app/dots_boxes/dots_storage.py) and [session storage](../../badge/app/dots_boxes/dots_session.py) | Schema, migrations, account isolation and durable pending moves |
| [App entry point](../../badge/app/dots_boxes/__init__.py) | Badgeware loop, UI, watchdog and Home cleanup |
| [Native SQLite component](../../native-sqlite/README.md) | Supported API, ABI, builds, artifact ownership and qualification |
| [Source pins](../../native-sqlite/dependencies.json), [SQLite configuration](../../native-sqlite/src/config.h) and [C bindings](../../native-sqlite/src/module.c) | Actual dependency and runtime constraints |
| [Package tool](../../badge/tools/package.py) | Source/prebuilt selection and generated distribution boundaries |

At handoff time, `Database` imports `ShadowVFS` from
`sqlite-shadow-vfs/sqlite_shadow_vfs.py`; the previous
`badge/lib/powersync/vfs.py` is deleted in the working tree. The package tool
includes the extracted VFS independently. Inspect that refactor's actual state
before proposing a storage adapter.

Historical evidence:

- [Original networking handoff](/Users/kobie/projects/powersync/skunkworks/powerbadge/native/sqlite-hello/docs/next-agent-networking.md)
  and [Badgeware direction update](/Users/kobie/projects/powersync/skunkworks/powerbadge/native/sqlite-hello/docs/next-agent-network-measurement.md).
- [Core qualification](/Users/kobie/projects/powersync/skunkworks/powerbadge/native/sqlite-hello/docs/powersync-core-qualification.md)
  and [network profiling](/Users/kobie/projects/powersync/skunkworks/powerbadge/native/sqlite-hello/docs/network-profiling-qualification.md).
- [Badgeware native feasibility handoff](/Users/kobie/projects/powersync/skunkworks/badgegotchi/docs/handoffs/badgeware-native-feasibility.md)
  and [device feasibility results](/Users/kobie/projects/powersync/skunkworks/badgegotchi/experiments/badgeware-native/README.md).
- [Pairing and custom-client handoff](/Users/kobie/projects/powersync/skunkworks/badgegotchi/docs/handoffs/badge-pairing-powersync.md),
  especially its instructions to add a supervisor around the core.

If original chat context is needed, inspect the existing chats named
`2 - Compile SQLite for the badge`, `4 - Integrate PowerSync core with SQLite`,
`5 - Build Hello World Networking Flow`, `8 - Port dart app to react`, and
`9 - Test Badgeware native feasibility`. Do not message or resume them.

## Questions the investigation must answer

### Upstream SDK and embedded runtime

Inspect the actual upstream source and pin the assessed revision, crate version,
toolchain and feature set. Use its source and official API documentation rather
than assuming remembered APIs or release status. Start with
[the repository](https://github.com/powersync-ja/powersync-native),
[crate manifest](https://github.com/powersync-ja/powersync-native/blob/main/powersync/Cargo.toml)
and [Rust API documentation](https://docs.rs/powersync/).

Trace required `std`, allocator, task spawning, timers, synchronization, atomics,
filesystem and networking assumptions. Distinguish optional integrations such as
Tokio, smol, reqwest and rusqlite from facilities the SDK fundamentally needs.
Do not conclude that optional Tokio or reqwest dependencies make it unusable.
Determine whether existing interfaces support our platform, small upstream
changes suffice, or a substantial `no_std` fork is needed.

Assess cooperative polling from the existing Badgeware foreground loop. Define
who owns the executor, how work yields, how socket readiness and timers wake it,
and how cancellation, Home and watchdog servicing work. Do not assume a second
Embassy runtime can take over hardware already owned by Badgeware. Any broader
firmware alternative belongs in the report as a separate option.

### SQLite and core integration

The current native module exposes one connection and bounded `exec(sql)` results
as text/None. It has no public parameter-binding, typed-BLOB or raw connection
handle API, and caps results at 32 rows per statement. Determine what the native
SDK's database interface actually requires. Reusing this Python API unchanged
may be insufficient.

Compare linking SDK code with the same SQLite/core instance against exposing a
deliberate native database ABI. Account for the SDK's core dependencies and any
required version changes explicitly. Do not accidentally link duplicate SQLite
engines or core copies, pass connections between incompatible copies, or let
two sync supervisors own the same database.

Current constraints include `SQLITE_THREADSAFE=0`, omitted WAL and dynamic
extension loading, one serialized database owner, DELETE journaling with FULL
synchronization, a shared 256 KiB SQLite/core arena, 16 KiB WSD storage and a
128 KiB PSRAM database shadow. The journal remains on LittleFS. Check whether
upstream database pools, locking, update hooks and transaction behavior can work
with these constraints. Do not copy a desktop WAL/pool example unexamined.

### Native module and platform boundaries

The demonstrated target is Tufty 2350 / RP2350B, MicroPython 1.29.0, `.mpy` ABI
7942, object representation C, softfp calls and Cortex-M33 PIC code using
`thumbv8m.main-none-eabi`. The module header alone does not establish the float
ABI. MicroPython's native loader exposes a restricted runtime interface; it does
not export arbitrary firmware symbols.

Account for code/data placement, relocations, allocator ownership, GC roots,
Python callback lifetimes, error conversion and panic behavior. Catch
MicroPython NLR exceptions at the C boundary so they do not cross Rust frames.
Do not assume the existing 256 KiB SQLite arena also budgets the SDK's tasks,
Rust allocations or transport buffers. Measure them separately.

Identify which existing HTTP/TLS and filesystem adapters can remain Python
behind thin bindings, which must become native, and why. Preserve certificate
and hostname verification, stream backpressure and bounded per-frame work.

### Application semantics

Preserve the five-letter pairing flow, `badge_game_v2` room subscription, schema
and game RPC contracts. This game keeps its durable pending move outside
`ps_crud`; HTTP acceptance alone does not clear it. An accepted move is confirmed
only when its exact UUID and room appear after a published full checkpoint.
Rejected moves and stale boards have their own barrier behavior. Account
switching must not expose another user's cached rows or discard pending work.

Determine how to express these semantics through the native SDK's current
connector/download/upload interfaces. Do not silently convert the game to SQL
CRUD uploads, change the backend, or relax confirmation rules to simplify the
prototype. Separate general SDK feasibility from game-specific adaptation.

## Work sequence and evidence

1. Record checkout status, component boundaries and exact baseline identities.
   Read source pins and manifests; do not assume an older device result qualifies
   the current tree. The README currently notes that the extracted, renamed
   native binary still needs its hardware smoke test.
2. Produce a dependency and interface assessment with file/symbol references.
   List each blocker, the smallest proposed change, and whether it belongs in
   an adapter, upstream SDK patch or fork. Identify a minimal build configuration.
3. Where feasible, build an isolated host proof exercising the real native SDK's
   orchestration against deterministic transport/credential fixtures and real
   SQLite/core. Demonstrate subscription, fragmented download, publication on
   checkpoint, cancellation and reconnect. A normal desktop build alone does
   not establish compatibility with the intended embedded adapters.
4. Cross-compile and link the relevant SDK path for the actual ARM/PIC/softfp
   target. If blocked, retain the smallest reproducer, exact command/error and
   dependency chain. Successful archive creation alone is insufficient: inspect
   final symbols, relocations, code/BSS size and runtime services still required
   by a loadable module. Progress toward a minimal `.mpy` candidate where feasible.
5. Assess compatibility with existing tests, pending-move semantics and storage
   fault handling. Keep proofs isolated; do not alter the production client just
   to make the experiment pass. Specify a subsequent device test covering import,
   GC, lifecycle, networking, reset and UI responsiveness.
6. Report the result and a proposed adoption plan. If porting is substantial,
   finish with concrete evidence and an estimate rather than starting an
   unbounded SDK rewrite or replacing it with another custom client.

Relevant regression cases live in `badge/tests/test_sdk.py`, `test_sync.py`,
`test_session.py`, `test_client.py`, `test_vfs.py` and `test_vfs_native.py`.
The host component fixture is in `native-sqlite/tests/`. Read current tests and
their prerequisites before choosing commands; do not overwrite cached artifacts
or regenerate served packages during an isolated investigation.

Performance claims need a matched baseline. The historical on-device full-board
fixture took 716.36 ms for 9,114 bytes / 50 operations, including Python framing,
record decoding/construction and scheduling. It excluded Wi-Fi, TLS, SQLite and
flash. See [performance qualification](../../badge/tools/performance-qualification.md)
and [hardware results](../../badge/tools/performance-hardware-results.json).
It is neither parser-only nor end-to-end latency, and predates source extraction.
Check the current benchmark harness for obsolete import paths before reusing it.
Report host timings separately from badge timings, reservations separately from
observed peaks, and expected improvements separately from measured ones.

## Required outcome

Save a concise investigation report with reproducible commands, pinned source
revisions, any isolated patches/proofs and evidence for one of these outcomes:

- The full SDK works through platform adapters without upstream changes.
- The full SDK is viable with a defined patch set or fork, with remaining work
  and maintenance cost estimated.
- The full SDK is blocked under the Badgeware constraints, with a reproducible
  technical reason and the changes that would remove that blocker.

Include a responsibility table showing what moves from Python to upstream SDK
code, what remains in platform bindings and what stays application-specific.
Explain the minimum public interface needed by the existing Python UI and game.
Compare correctness, resource use, expected or measured performance, distribution
and maintenance costs against the current SDK. Recommend proceeding or retaining
the current client on that evidence. Keep any future parser-only optimization
or standalone firmware proposal distinct from the full-SDK result.
