# PowerSync native SDK on Badgeware: investigation result

**Current status:** the SDK no_std port is implemented locally, and the real
SDK/Embassy candidate is installed on the Tufty as `dots_boxes_native`.
All 23 installed files were verified; native SDK startup, GC and cleanup passed
on the badge. The original app is retained; its active save was subsequently
cleared with operator authorization to free space for the native app. See the
[installed candidate and manual-test notes](badge-candidate/README.md) and
[SDK port results](port-results/README.md). Multiplayer, recovery and performance
remain for manual testing; a speed improvement has not been measured.

The investigation below records the original unpatched baseline and blockers.
Its compiler failure and pending module/install status are superseded above;
preservation statements below describe that earlier investigation stage.

Assessed 2026-10-26. **The unmodified full native SDK is blocked on the
Badgeware target. A defined embedded fork is required.** Its orchestration works
in the isolated host proof; no full-SDK ARM module or sideloadable app was
produced, and better badge performance has not been established. Retain the
working app until the port passes the gates below.

| Requested outcome | Evidence / status |
| --- | --- |
| Sideloadable Badgeware app | Existing app remains intact. Native-SDK candidate blocked before final ARM link; no candidate package or device installation. |
| Uses the PowerSync native SDK | **Host proof passes using actual upstream orchestration**, with a three-line raw-connection visibility patch; ARM async dependency subset also compiles. The full target SDK does not. |
| Better performance than the custom MicroPython SDK | Current baselines recorded. Host native proof is encouraging but has a different storage workload; no speed ratio or device speedup can be claimed. |

This follows the handoff's explicit scope: isolated proofs and a bounded
assessment, with no production-client switch or unbounded SDK rewrite.

## Pins and preservation

- App: `4867f40ebc777700478e84cd8ca6a8a3e481c4a5`; initial tree clean.
- SDK: sibling `powersync-native`, `96a94f3e6e9442c4181d793f95241c2ee9cab4d8`,
  `powersync 0.1.1`, edition 2024, Rust 1.96.0 (`ac68faa20`, LLVM 22.1.2).
- SQLite: 3.54.1; core: 0.5.4,
  `0ed9f4edba418a4f8ae7cb0594da683cd79a81b5`. SDK and badge core pins match.
- Target: RP2350B Cortex-M33, `thumbv8m.main-none-eabi`, PIC, softfp;
  MicroPython 1.29.0, ABI 7942, representation C.
- All new work is under this directory. Production sources, native prebuilts,
  served packages, sibling checkout, firmware, and real badge/session/database
  state remain unchanged. No push or deployment occurred.

The current native prebuilt is 737,192 bytes, SHA256
`359c1f5e619357c20d777414165540b55d290af63994ddb44d0d3a210cbcb9d5`.
Its extraction-era hardware smoke test remains outstanding. Older device
qualification does not qualify this identity. Further baseline/source hashes
and **181 passing badge tests** are in [the game assessment](assessment-game.md).

## What was executed

1. [Host proof](host/src/main.rs): no Tokio, smol, reqwest, or rusqlite;
   custom foreground `async-task` queue/timers and demand-driven fixture HTTP;
   one raw SQLite connection and the actual compact game schema. The copied
   SDK receives only [the raw API patch](patches/0001-expose-raw-connection.patch)
   and standalone Cargo metadata. Upstream orchestration is unchanged.
2. Across 21 lifecycle rounds it verifies the explicit `badge_game_v2` room
   subscription, fragmented input, unpublished rows remaining invisible,
   all 49 move IDs/fields plus room fields after full publication, and empty
   `ps_crud`. A transport error causes real SDK retry and credential refetch;
   the new request resumes bucket cursor 50 and publishes checkpoint 51.
   Disconnect drops each response and joins SDK tasks.
3. [Actual full-SDK ARM check](arm/sdk-check.log) exits **101**, with
   `E0463: can't find crate for std` in `parking 2.2.1`, despite disabled SDK
   optional features. Required dependency defaults, `async-broadcast`, SDK
   std APIs, and core-default `getrandom` need changes. This is a compiler
   failure with dependencies present, not a missing toolchain.
4. A [no_std async subset](arm/async-leaf/Cargo.toml) compiles for the target.
   ELF attributes, symbols, relocations and object sizes are retained. It
   contains no SDK orchestration and is not a final link or `.mpy` candidate.

There is no final ARM SDK binary to inspect: code/BSS size, loader relocations,
runtime symbols and WSD fit remain unqualified. The host binary contains one
SQLite engine/core and links no system SQLite; see
[libraries](host/results/libraries.log), [symbols](host/results/symbols.log),
and [feature graph](host/results/features.log). Its macOS size is not an ARM
size estimate.

## Resources and performance

| Measurement | Observed result | Scope |
| --- | --- | --- |
| Current custom client, checked memory sink | Median 1.308 ms / 21 trials | CPython host, 9,114 bytes / 50 ops; excludes core/SQLite/IO |
| Current custom client with real core | Median 5.856 ms, p95 6.807 ms / 31 trials | Fresh POSIX file, DELETE/FULL, 256 KiB SQLite/core arena; excludes Python heap, Wi-Fi/TLS and ShadowVFS/flash |
| Full native SDK host proof | Median 0.251 ms / 21 rounds | In-memory SQLite, 384 KiB shared arena; includes prepublication assertions; no filesystem durability cost |
| Native proof shared arena | Peak **298,816 bytes**, reservation 393,216 | SDK/core/SQLite and post-setup test-driver allocations; preloaded fixture/std allocations and stack excluded |
| Native proof with 256 KiB shared arena | SDK reports **NOMEM**, fixture cannot finish | Child process ultimately hits proof timeout; not a measured badge memory requirement |

Raw results: [native proof](host/results/proof.json),
[256 KiB failure](host/results/arena-256.json),
[custom sink](game/baseline-download-host.json),
[custom real core](game/baseline-real-core-host.json).
The host variants differ in IO, allocation, warming and instrumentation:
**dividing their times would not establish a speedup**. The historical
716.36 ms badge result is framing/decode/construction/scheduling only and
predates source extraction; it is not full sync latency.

The host proof uses the constrained SQL feature profile from
`native-sqlite/tests/host_config.h`. It does not exercise target WSD storage,
loader adaptations, Python VFS callbacks or flash. The shared allocator keeps
core error buffers compatible with `sqlite3_free`; a separate SDK heap would
need explicit ownership-transfer fixes. Rust requested-byte counters include
test code, so the shared SQLite high-water reading is the authoritative total
for this fixture. Host 64-bit layouts do not predict ARM peaks.

Whole-line buffering is significant: the existing Python framer splits a large
service data array into bounded operations, while upstream accumulates the
whole line. Its 8,192-byte bound is **per generated operation/control record**,
not a service-line cap. Preserving valid large histories requires streaming
operations or a negotiated bounded protocol. Merely rejecting service lines
over 8,192 bytes would break the current fixture. Upstream per-line commits
also differ from current six-record batches, so flash cost needs measurement.

## Smallest port and responsibility split

| Work / owner | Defined change |
| --- | --- |
| SDK/dependencies | `no_std + alloc` feature; core/alloc imports, suitable collections/mutexes, clock and IO/path abstractions; no_std dependency features; port/replace `async-broadcast`; disable core getrandom default in favor of SQLite entropy. |
| SDK database API | Public owned single-connection factory and leased typed statements. The retained visibility patch proves the seam; design a deliberate stable API before adoption. Avoid desktop WAL/readers/cache defaults. |
| SDK bounded execution | Incremental operation framing, input limits, and yields by operations/bytes. Polling one task can still process an unbounded ready stream; synchronous checkpoint work also needs profiling. |
| SDK committed events | Expose full/partial publication scope, committed generation and covering checkpoint ID **after commit**. `has_synced` or a generic table watch cannot acknowledge a pending move. |
| Badgeware binding | One `.mpy` linking SDK/SQLite/core once; compatible allocator with explicit budget; foreground executor/timers; demand-driven verified TLS; one serialized database owner; C NLR barriers; rooted Python objects, cancellation, deterministic panic/OOM policy. |
| Existing Python app | Pairing, durable account/session files, immutable move RPC retries, exact UUID/room confirmation after full publication, rejection/stale barriers, migrations, game/UI. External move uploader stays outside `ps_crud`; native upload loop is only woken by SQL CRUD. |

Preserve DELETE/FULL, 128 KiB shadow database, LittleFS journal, storage reserve
and account-switch safeguards. No second supervisor may share the connection.
Minimum UI API: open, bounded query/execute, subscribe/connect, poll,
status/committed events, checkpoint request/completion, disconnect/close and
resource statistics. Detailed source/symbol references and lifecycle design:
[SDK assessment](assessment-sdk.md), [ARM/module assessment](assessment-arm.md),
[game contract](assessment-game.md).

## Adoption recommendation and next gates

Retain the current client for use. Pursue a separately maintained embedded
SDK branch if the sideloadable full-SDK outcome justifies an estimated
**3–6 engineer weeks plus 1–2 weeks contingency**. This is a planning estimate,
not evidence that the final module will fit or be faster.

1. Port the SDK/dependencies and add bounds/publication events; rerun host
   contracts. Keep changes attributable to adapters versus SDK/core patches.
2. Link the complete Cortex-M33/PIC/softfp `.mpy`; audit every unresolved symbol,
   section/GOT relocation, allocator requirement, WSD usage and code/data size.
3. Package a separate app directory and isolated state. Qualify import/GC,
   repeated enter/Home, socket/task cleanup, verified TLS, reconnect/reset,
   watchdog/UI latency, exact-move and full/partial checkpoint cases, rejection,
   account switching and storage faults. This investigation did not install it.
4. Alternate both clients on the same badge with identical fixtures/storage.
   Measure framing, core-inclusive publication and live end-to-end latency
   separately, including tails, maximum foreground call, memory and flash IO.
   Adopt only after correctness and performance gates pass.

Parser-only optimization and replacement firmware remain separate options;
neither satisfies the requested full-SDK Badgeware outcome.

## Reproduce

From the app repository, using the installed Rust 1.96.0 and ARM GCC toolchain
under `../powerbadge/native/.tools`:

```sh
python3 experiments/powersync-native-sdk/host/run.py
python3 experiments/powersync-native-sdk/arm/reproduce.py
python3 experiments/powersync-native-sdk/game/benchmark_baseline.py
python3 experiments/powersync-native-sdk/game/benchmark_real_core.py
python3 -m unittest discover -s badge/tests -v
```

Build outputs/private Cargo cache are ignored. The current cache was copied
from the existing tool cache and missing locked crates downloaded once.
Reproduction is offline by default; a fresh machine must supply the locked
crates, SQLite vendor sources, libclang and these toolchains. `host/prepare.py`
checks the sibling revision/cleanliness and verifies its generated SDK copy
contains only the assessed changes. `host/run.py` expects a successful 384 KiB
proof and a NOMEM failure at 256 KiB. `arm/reproduce.py` exits successfully when
it records the expected full-SDK compilation failure; inspect the underlying
exit code in `arm/build-result.json`.

Official sources: [pinned SDK](https://github.com/powersync-ja/powersync-native/tree/96a94f3e6e9442c4181d793f95241c2ee9cab4d8),
[published API](https://docs.rs/powersync/0.1.1/powersync/).
The API fetch was unavailable; assessment uses actual checked-out source.
