# Badgeware runtime constraints

This bridge is specific to the existing Badgeware foreground execution model.
The full PowerSync SDK remains a normal concurrent-capable library by default;
the special code-generation choice below is restricted to this native module.

## PSRAM and atomic instructions

RP2350 exclusive atomic transactions are not supported in the external PSRAM
used for this module's GC allocation and SQLite-backed Rust heap. The core
extension uses `Rc`, but the full SDK and its async dependencies use `Arc`,
atomic reference counts, queues and locks. Merely compiling for Cortex-M33 does
not establish that those instructions work at the addresses returned by the
allocator.

The candidate native build uses the LLVM `lower-atomic` pass with fat LTO:

```text
-C target-cpu=cortex-m33 -C relocation-model=pic -C panic=abort -C passes=lower-atomic
```

This lowers atomic operations to ordinary accesses for a non-preemptible
environment. It is valid here only because **all accesses to the Rust SDK and
its heap occur serially on the Badgeware foreground thread**:

- No Embassy HAL, hardware timer driver, interrupt executor, second core or
  background thread may access this SDK state or invoke its wakers.
- The raw Embassy executor is polled only from `psb_poll`/cleanup. `__pender`
  only requests a later poll; it never polls recursively.
- Timer readiness and HTTP replies are delivered from the same foreground
  update, using the existing Python transport and firmware ticks.
- Python VFS callbacks must not recursively enter SDK methods. C bindings must
  reject reentry before calling Rust; callback exceptions are caught inside C
  and returned as SQLite errors rather than unwinding over Rust frames.
- `SQLITE_THREADSAFE=0` and the SDK's single pool serialize all SQLite accesses.
  Native queries hold an explicit pool lease for their complete C operation.

Changing any of those assumptions requires restoring real atomic semantics and
providing supported atomic storage or a suitable software-atomic implementation.
The SDK's generic `Send` bounds do not authorize concurrent use of this special
binding. Do not copy these flags into the general SDK build configuration.

The final build must inspect every linked Rust object and final code for
exclusive load/store instructions (`ldrex`, `strex`, `ldaex`, `stlex`, including
width variants); a successful archive build alone is insufficient. The native
build tooling records that audit separately. Hardware startup/publication remains
a separate qualification gate.

References: [LLVM LowerAtomic pass](https://llvm.org/docs/Passes.html#lower-atomic-lower-atomic-intrinsics-to-non-atomic-form),
[RP2350 PSRAM atomic issue](https://github.com/raspberrypi/pico-feedback/issues/398).

## SDK-owned upload queue and retries

The binding's `BackendConnector::upload_data()` reads an actual
`PowerSyncDatabase::next_crud_transaction()` from `_badge_move_outbox`, a
PowerSync insert-only table. Each explicit local transaction contains one
immutable move PUT; the insert-only table does not accumulate board rows.
Unexpected tables, operations, transaction sizes or malformed board edges fail
closed without acknowledging CRUD.

The foreground mailbox exported as `upload` contains
`{id, entry_id, move: {id, room_id, orientation, row, col}}`. Python supplies
only the stepped verified HTTP RPC and returns
`{op: "upload", id, result: "success" | "rejected" | "retry"}`. A transient
result returns `PowerSyncError` from the connector; the SDK upload actor owns
the five-second retry and repeats the same queued CRUD entry and immutable UUID.
Success or a permanently processed rejection invokes the actual
`CrudTransaction::complete()`. Cancellation drops the waiting connector future
without completing its transaction. `upload_close` reports obsolete mailbox
IDs so the foreground owner can release their transports.

The HTTP result does not confirm a canonical move. Python persists its local
accepted/rejected receipt before replying, preserving restart idempotency after
CRUD completion. Its durable pending session UUID remains until an exact
UUID/room is visible after committed full sync, or an explicit permanent
rejection is saved. Application writes must hold the SDK's writer lease so its
update hooks notify the upload actor. No Python upload retry timer is required.

Credential HTTP failures use `{op: "credentials_error"}`, which wakes all
pending connector credential futures with `IOERR`; native download/checkpoint
supervisors own their retries. A successful `{op: "credentials", token}` clears
that callback error. The connector holds a weak reference to the Context-owned
`Arc<PowerSyncDatabase>` to avoid a coordinator/options/connector/database
reference cycle.

The host C ABI proof exercises actual SQLite, insert-only CRUD, five-second SDK
retry timing, identity preservation, real completion, rejection, cancellation
without acknowledgment, and restart of the same uncompleted entry.

## Diagnostic breadcrumbs

Rust panic handling passes only static file/line information to a C reporting
callback, without allocating or raising a Python exception, then waits for the
application watchdog. Startup breadcrumbs identify the boundary reached before
a hardware hang:

| Stage | Boundary |
| --- | --- |
| 101 / 102 / 103 | Input validation / schema parsing / parsed inputs |
| 110 / 111 / 112 | Runtime lookup / state allocation / executor allocation |
| 113 / 114 / 115 | Task-pool allocation / engine allocation / runtime registered |
| 120 / 130 | Shared request state / SQLite pool and hooks |
| 140 / 141 | Environment and database construction / construction complete |
| 150 / 151 / 160 | Starting future / boxed future / context returned |

The C reporting implementation catches any MicroPython exceptions internally;
it must never longjmp out through the Rust panic or trace call. Diagnostics
must contain no tokens, session records or HTTP bodies.
