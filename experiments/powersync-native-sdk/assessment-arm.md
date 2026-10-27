# ARM and Badgeware module assessment

Assessed SDK: `powersync` 0.1.1 at
`96a94f3e6e9442c4181d793f95241c2ee9cab4d8`. No SDK source, production native
module, served package, or device state was changed by this proof.

## Result

The unmodified full SDK does not compile for the existing Badgeware module
target, even with every SDK optional integration disabled. The retained
reproducer reaches an actual Rust compiler failure, not a missing toolchain or
unavailable network dependency:

```text
error[E0463]: can't find crate for `std`
  = note: the `thumbv8m.main-none-eabi` target may not support the standard library
  = note: `std` is required by `parking` because it does not declare `#![no_std]`
```

See [the complete compiler output](arm/sdk-check.log),
[feature graph](arm/features.log), and [reverse dependency chain](arm/parking-chain.log).
This was compiled with Rust 1.96.0 (`ac68faa20`, LLVM 22.1.2),
`thumbv8m.main-none-eabi`, Cortex-M33, PIC, and panic abort. The manifest uses
`--no-default-features`; Tokio, smol, reqwest and rusqlite are absent from this
selected build. Thus their optional desktop integrations do not explain the
failure. The installed target exposes 8/16/32-bit and pointer atomics; missing
64-bit atomics is not the first blocker. [Target configuration](arm/target-cfg.log).

The SDK's direct `async-lock`, `async-channel`, `event-listener` and
`futures-lite` dependencies activate their std defaults, which activate
`parking`. In addition, `async-broadcast` 0.7.2 has unconditional `std::sync`
`Arc`/`Mutex` imports and no no_std feature. Disabling dependency defaults alone
would still leave SDK source using `std::sync::Mutex`, `std::collections`,
`std::path`, `std::io` and `SystemTime`. The SDK also enables the core's default
`getrandom` feature: `powersync -> powersync_core/default -> uuid/v4 ->
getrandom` 0.4.3. A no_std profile needs to disable that default and let the core
use the existing SQLite entropy callback. [Exact chain](arm/getrandom-chain.log).

No full-SDK archive, ELF, or `.mpy` was produced, so final SDK code/BSS size,
relocation compatibility, unresolved runtime services and allocation peaks are
not measurable from this attempt. An empty wrapper would not make the SDK
loadable and is not supplied as a candidate app.

## Reproduction and positive lower-level proof

From this app repository:

```sh
python3 experiments/powersync-native-sdk/arm/reproduce.py
```

The script uses the existing tool binaries under
`../powerbadge/native/.tools` without modifying that historical project. It
writes build outputs to `arm/target/`, `arm/async-leaf-target/` and the experiment's
private `.cargo-home/`. The cache was seeded by copying the existing cargo
registry; missing **locked** SDK crates were downloaded after network approval.
`--online` is available for an authorized fresh cache. The default is offline.
The script exits successfully only after verifying the pinned SDK's expected
`E0463`/std/parking compiler failure, the independent std probe's `E0463`,
successful dependency trees and async-leaf build, and successful ELF inspection
commands. `build-result.json` retains every command status and the actual SDK
exit code **101**, plus explicit validation results. Missing offline dependencies
or another build failure fail validation. The independent [std probe](arm/std-probe.rs) fails for the same
target, proving the installed sysroot contains core/alloc rather than std.

[async-leaf](arm/async-leaf/Cargo.toml) is a separate **compile-only** positive
proof: the locked `async-task` 4.7.1, `async-channel` 2.5.0, `async-lock` 3.4.2,
`event-listener` 5.4.2 and `futures-lite` 2.6.1 build together on the real target
when their std defaults are disabled (futures-lite uses `alloc`). Its code
exercises task spawning signatures, boxed futures, channels, async locks and
events. This narrows the necessary fork work; the whole async ecosystem need
not be replaced. It does **not** contain upstream SDK orchestration, SQLite or
an executor implementation, and it is not a final link or loadable module.

The retained [ELF attributes](arm/async-leaf-attributes.log) identify Cortex-M33,
v8-M mainline Thumb, GOT-indirect PIC data references, and no hard-float
`Tag_ABI_VFP_args`. [Relocations](arm/async-leaf-relocations.log) include
`R_ARM_THM_CALL`, `R_ARM_THM_JUMP24`, `R_ARM_GOT_PREL`, `R_ARM_REL32`,
`R_ARM_ABS32` and unwind metadata `R_ARM_PREL31`. The leaf crate object has
2,495 text bytes, 40 data bytes (`.data.rel.ro`), and zero BSS. Those numbers
exclude its dependency objects, the allocator, core, executor and transport;
they are **not** an SDK size estimate. [Unresolved symbols](arm/async-leaf-symbols.log)
still require Rust allocation/deallocation, allocation failure/panic handlers,
AEABI memory support and dependency code. A future final `.mpy` link must
resolve these rather than treating this rlib as a deliverable.

## Module boundary and smallest changes

| Constraint / evidence | Required design or change |
| --- | --- |
| Existing `native-sqlite/Makefile` uses object representation C, `armv7emsp` header, explicit `-mfloat-abi=softfp`; Rust builds Cortex-M33 `thumbv8m.main-none-eabi` PIC. | Keep those exact conventions. The header does not establish float ABI. Do not link eabihf code or infer compatibility from archive creation. |
| `native-sqlite/src/module.c` owns one connection, 256 KiB arena and 16 KiB WSD; public `exec` returns text/None and caps rows at 32. | Integrate the SDK and SQLite/core into **one** module with one owner. A deliberate C ABI must expose raw connection/statement ownership plus typed bind/blob handling internally; the existing Python exec API cannot implement SDK internals. |
| SDK `ConnectionPool::single_connection` avoids the desktop `open` path's WAL, five readers and 50 MiB cache. | Use a single connection and preserve DELETE/FULL plus the shadow VFS. Expose a public raw constructor or single-open helper for the rusqlite-free build; raw connection types currently live in private `db::connection`. No desktop pool defaults. |
| SDK and existing module both use core **0.5.4**. | No core version upgrade is needed for this assessed revision. Link exactly one copy each of SQLite and the core; do not load another SQLite or pass a connection between copies. |
| Core's `crates/static/src/lib.rs` selects `SQLite3Allocator`; `powersync_core` alone does not. Core init's CString error transfer explicitly assumes SQLite allocation (`crates/core/src/lib.rs:54`). | The final wrapper must select a compatible allocator or fix every transferred-ownership site. Simply adding a separate Rust heap can make SQLite free memory from the wrong allocator. Budget SDK task/transport/schema allocations explicitly and record peaks; 256 KiB is a reservation, not spare SDK capacity. |
| `mpy_ld.py` rejects nonempty `.data` and fixed references to module BSS; only its function table and limited builtins resolve firmware services. | Audit the **final linked** sections and relocations after porting. Keep mutable module state behind compatible global GOT references or explicitly owned allocated context. Rust static mutex/once initialization and hidden platform calls need review. Do not import arbitrary firmware symbols. |
| Existing `src/vfs.c` catches MicroPython NLR inside each Python allocation/callback/conversion boundary. | Extend that C boundary for HTTP, credentials and timers; never permit a Python longjmp through a live Rust frame. Return explicit status codes, then raise at the outer Python call only after Rust returns. A Rust allocator cannot call a potentially raising MicroPython allocator directly. |
| Rust's heap is opaque to the MicroPython collector. | Keep Python callbacks, sockets, exception objects and buffers in module globals / an explicit Python root container. Pass stable handles into Rust. Hold buffers only with an explicit lifetime; never retain a borrowed Python buffer across a yield or GC. |
| Rust panic abort still requires a defined handler; the existing no_std core wrapper loops. | Supply a deterministic fatal-state/watchdog policy at the final module boundary. Do not assume `panic=abort` exports a usable firmware abort symbol or makes OOM recoverable. |
| `AsyncRuntime::custom` can schedule through async-task; no second hardware runtime is required. | Badgeware owns the executor drive: `poll(budget)` drains a bounded runnable queue, deadline/socket callbacks enqueue wakers, and Home cancels/joins tasks before socket and SQLite closure. No interrupt or background thread may call Python or the THREADSAFE=0 SQLite instance. |
| SDK line/BSON splitters grow buffers and loops may consume multiple ready events in one poll. | Add frame limits and cooperative yields to orchestration/framing, in addition to adapter byte limits. A bounded runnable count does not guarantee bounded time for one task poll; SQLite checkpoint application may still be a long synchronous call. |

Transport and filesystem may stay Python for the first port: existing verified
TLS/hostname checks and socket/file ownership remain with Badgeware. The C
bridge presents a demand-driven HTTP body stream, returns Pending when the
per-frame byte budget is exhausted, and wakes it on the next foreground frame.
It must close a canceled body, retain only bounded fragments, and never read
ahead without demand. Timers use Badgeware ticks and a stored deadline; no
Embassy driver takes hardware ownership from firmware. UI input and watchdog
service run between polls. A timer future must yield on its first poll as
required by `AsyncRuntime::delay_once`.

## Device gate after a real SDK build

Before calling a package sideloadable, complete final link inspection, then
test it in a **separate** app directory and separate test session/database:
import and ABI check; allocate/core init and peak tracking; repeated
create/connect/cancel/Home/close cycles; forced GC while callbacks and buffers
are pending; fragmented verified TLS download; disconnect/reconnect and reset
mid-checkpoint; pending-move confirmation only after full publication; VFS
fault injection and read-only storage errors; watchdog and worst-frame latency
while moving around the UI. Compare the same current board fixture with the
custom SDK on the same badge, including framing-only, sync-with-SQLite and
end-to-end timings separately. No badge performance improvement has been
established by these ARM compile probes.
