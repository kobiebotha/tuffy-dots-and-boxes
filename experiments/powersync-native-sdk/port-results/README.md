# SDK no_std port: implemented and validated

**Subsequent badge integration:** the real SDK/Embassy candidate has now been
built, packaged and installed separately as `dots_boxes_native`. On-device
startup, GC and cleanup checks pass. See the [installed candidate](../badge-candidate/README.md).
The report below records the preceding SDK-port milestone; its statements
about pending badge integration describe that earlier stage. Performance
remains unmeasured.

The actual sibling `powersync-native` checkout now builds the full SDK for
RP2350 Cortex-M33 without `std`, both with a custom runtime and with the new
Embassy adapter. This supersedes the earlier investigation's compiler blocker.
It is an SDK port milestone; no badge `.mpy` or sideloadable candidate has been
produced yet, and improved badge performance is still unmeasured.

Base revision: `96a94f3e6e9442c4181d793f95241c2ee9cab4d8` (`powersync 0.1.1`).
The port is local working-tree changes, not a published release. Exact Rust
source/Cargo hashes and compiler identity are in [build-result.json](build-result.json).
No commit, push, deployment, firmware change or badge installation was made.

## Changes in the SDK

- `no_std + alloc` with defaults disabled; desktop defaults retain `std` and
  rusqlite. Tokio, smol, reqwest and rusqlite explicitly enable `std`.
- Runtime imports use `core`/`alloc`. Embedded collections use hashbrown and
  short synchronous locks use spin; filesystem/blocking pool helpers are
  `std` only. Public desktop collection/timestamp types remain unchanged.
- Removed the std-only broadcast dependency. Checkpoint readiness now uses
  event notifications with registration before state inspection; concurrent,
  reconnect, disconnect and coalesced-transition regressions pass.
- Core's OS `getrandom` dependency is disabled in no_std builds; UUID generation
  uses the existing SQLite randomness service. Core remains at 0.5.4.
- Optional `env::embassy` schedules cancellable SDK futures on a supplied Embassy
  executor with reusable static task slots. It installs no HAL, interrupt or
  hardware clock. The host supplies ordinary-waker timer futures and foreground
  polling; one database needs at least four slots. Pool exhaustion has an
  explicit panic policy. Generic Embassy-time queue requirements are documented.
- Exposed owned raw connections and deliberately unsafe `prepare_unchecked`
  typed statements. The unsafe contract covers leases, STATIC bindings and
  borrowed column buffers; a compile-fail doc test prevents accidental safe use.
  Existing trusted internal statement calls remain crate-private.
- Failed SQLite opens now release returned handles; an isolated repeated-open
  regression verifies allocator usage returns to baseline.
- Added signed no_std `UnixTimestamp`; desktop uses the original `SystemTime`
  with corrected conversion for dates before the Unix epoch.
- Added SDK documentation, unreleased changelog notes and Cortex-M33 CI builds
  for both no_std configurations, plus Embassy lifecycle tests.

Read the SDK's [embedded setup](../../../../powersync-native/powersync/README.md)
and [Embassy module](../../../../powersync-native/powersync/src/env/embassy.rs).

## Verification

| Check | Result |
| --- | --- |
| Full SDK release build, no defaults | Pass, Cortex-M33 / `thumbv8m.main-none-eabi` / PIC / panic abort |
| Same full target build with Embassy | Pass |
| Target dependency feature graph | No parking, async-broadcast, OS getrandom, Tokio, reqwest or rusqlite |
| SDK tests, defaults plus Embassy | **92 passed** (47 unit, 45 integration), plus **2 doc tests** |
| All SDK features compile | Pass, including Tokio, smol, reqwest, rusqlite, Embassy and FFI |
| Actual no_std SDK + Embassy + real SQLite/core game fixture | **12 check groups**, **21 lifecycle rounds**, pass |

Evidence: [bare target build](no-std.log), [Embassy target build](no-std-embassy.log),
[target features](arm-features.log), [SDK tests](desktop-tests.log),
[desktop runtime compile](all-features.log), [game proof](../port-proof/README.md).
The target builds and game proof identify the same current SDK Rust/Cargo
sources. The host proof feature audit confirms SDK `std/default` are disabled.

The game proof uses the actual modified checkout directly. It checks room
subscription, fragmented input, invisibility before full publication, every
published compact move/room field, empty SQL CRUD, transport-error retry,
credential refetch, cursor 50 to full checkpoint 51 publication, response
cancellation, timer cleanup and repeated slot reuse. It does not exercise live
pairing/move RPCs or a physical badge.

The shared host allocator peaked at **297,280 bytes** with 384 KiB configured
within a 512 KiB static backing buffer. This includes SDK/core/SQLite and some
host driver allocations, excludes preloaded System fixture allocations and
stack, and does not predict ARM usage. The 256 KiB configuration reports NOMEM
and the fixture cannot complete. In-memory host timings are recorded separately
in the proof JSON; no device speedup or comparison with file/flash workloads is
claimed.

## Reproduce

Using the existing Rust 1.96.0 tools and private Cargo cache, from the app repo:

```sh
python3 experiments/powersync-native-sdk/port-results/verify.py --desktop-tests --desktop-runtimes
python3 experiments/powersync-native-sdk/port-proof/run.py
```

These commands are offline and locked. The standard SDK command, from its repo,
is:

```sh
RUSTFLAGS="-C target-cpu=cortex-m33 -C relocation-model=pic -C panic=abort" \
  cargo build -p powersync --no-default-features --features embassy \
  --target thumbv8m.main-none-eabi --release --locked
cargo test -p powersync --features embassy
cargo check -p powersync --all-features
```

Build outputs are ignored; SDK source edits and evidence remain reviewable.
Earlier `arm/reproduce.py` intentionally expects the original unpatched SDK to
fail, and `host/prepare.py` requires the clean original pin. Use the new helpers
for this port; the old reports remain historical evidence.

## Remaining badge integration

The std dependency blocker is removed. A sideloadable Badgeware app still needs
the unified SDK/SQLite/core native module and final symbol/relocation/WSD audit,
C boundaries for Python exceptions and rooted callbacks, verified transport and
storage adapters, bounded operation framing/work, and committed full-publication
events for the game's exact-UUID confirmation rules. The external pending-move
queue and account-switch barriers must remain intact. Hardware qualification
and matched performance tests follow that integration.
