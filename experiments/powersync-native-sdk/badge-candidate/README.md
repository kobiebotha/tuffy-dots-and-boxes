# Installed Badgeware Native SDK candidate

**Current implementation:** ordinary SDK-managed mutation rows and SDK watch
notifications. Gameplay uses SQL `execute()`; the SDK owns upload retries,
completion and optimistic-row reconciliation. There is no `pending.json`,
accepted-receipt table, startup re-enqueue loop or replacement pending-state
store. Earlier sections below record the investigation and previous versions;
see the ordinary-mutation update at the end for current behavior.

Installed as **`dots_boxes_native`** under `/system/apps` on the connected Tufty
2350, retaining the original `dots_boxes` app. The new
app has separate `/state/dots_boxes_native` pairing/database/pending-move state.
It displays **Native SDK** at startup and uses the actual Rust native SDK with
Embassy for sync orchestration. The native SDK owns the CRUD upload queue,
upload retries and write checkpoints as well as download reconnection. Python
supplies the UI, verified TLS, pairing and backend RPC connector callbacks.
There is no custom-sync fallback.

Use a fresh room/pairing code for the first pairing of this separate app. The previous badge seat in
an existing room may already be bound to the original app's device identity.
The saved-move recovery check below passes. Broader multiplayer, poor-Wi-Fi
qualification and performance remain for manual testing.

## Installed identity and checks

- Native module: `native_powersync_sdk.mpy`, **1,105,161 bytes**.
- SHA256: `ca5de31cd4e9e593bf2f01296cf5bb48f7dd45ca141157f5799b04b1e566be97`.
- MicroPython 1.29.0, Tufty ABI **7942**, object representation C, softfp.
- SQLite 3.54.1 and core **0.5.4/cratesio**; one serialized database connection.
- All **23 installed files** read back with matching hashes.
- Original app (42 files) retained its hashes. Its active saved state was later
  cleared with operator authorization to make room for the new app (see below).
- `/system` restored read-only; no firmware, backend or hosting deployment.
- **93 SDK tests + 2 doc tests**, **36 Python adapter/outbox tests**, and **20 real
  SDK/SQLite C ABI host checks** passed.
- On-device import, GC, core identity, SDK/Embassy start, credential mailbox,
  SDK-authored request, SQL lease guard, stop/close and final GC passed.

See [manifest.json](manifest.json), [install-result.json](install-result.json),
[hardware-smoke.log](hardware-smoke.log), and the [ZIP](dots-boxes-native-sdk.zip).
The hardware check used only an in-memory database and fixture credentials;
it made no network requests or changes to real pairing/game state. Full match
latency or an improvement over the MicroPython client has not been measured.

## First-launch storage fix

The first real launch exposed a separate storage admission failure:
`/state` had 148 KiB free, below the 192 KiB required to initialize the game's
128 KiB bounded database with its 64 KiB reserve. The original generic
"saved game" message obscured this first-run failure. The app now reports
**Not enough storage** and prints the startup traceback over USB.

With operator authorization, the old `/state/dots_boxes` directory was cleared,
increasing free space to 236 KiB. Its four files had already been verified at
`/system/backups/dots_boxes-before-native-20261026` on the separate FAT volume.
The original app remains installed but will start fresh. The native app then
created its own database and unpaired session successfully. See
[startup-result.json](startup-result.json). Storage limits and reserve guards
were retained.

## RP2350 external RAM binding

The initial native candidate imported and ran the core, then stalled on SDK
startup because RP2350 PSRAM does not support exclusive transactions used by
hardware atomic RMW loops. Raspberry Pi confirms this
[hardware limitation](https://github.com/raspberrypi/pico-feedback/issues/398).

This isolated binding owns all SDK state on one foreground executor, has no
Rust IRQ/peripheral driver or other-core caller, and rejects recursive native
entry from Python callbacks. Its **candidate-only** Rust build applies LLVM
`lower-atomic` under that non-concurrent contract. All SDK archive objects and
all **55 selected final-link objects** were disassembled and contain **zero
exclusive-access instructions**. The general SDK retains ordinary atomic
semantics. This configuration must never be reused with concurrent SDK-state
access. See the [execution contract](../badge-module/rust/RUNTIME.md) and
[final opcode audit](../badge-module/evidence/final-atomic-audit.json).

The C binding provides a 768 KiB compatible shared SQLite/Rust arena, rooted
VFS ownership, NLR exception barriers, raw SDK mailboxes and SQL lease guards.
Incoming chunks are capped at 2,048 bytes, a complete NDJSON record at 64 KiB,
and work yields between records. The prototype uses whole-record framing,
which differs from the original client's per-operation streaming. It remains
a candidate for manual qualification rather than a performance-qualified SDK.

Checkpoint receipts are emitted only after a committed full publication;
confirmation still requires the exact move UUID and room. Cached sync status,
partial progress and an HTTP acceptance cannot acknowledge pending work.
Rejection and reconnect require a fresh covering publication. The platform
adapter serializes startup TLS, handles allocation pressure without discarding
UUIDs, and retains database/free-space guards.

## SDK-owned uploads

Schema 6 retains raw `rooms` and `moves`, adding an internal insert-only
`_badge_move_outbox` and local-only upload receipts. Saving the pending UUID
precedes enqueue; startup fills a reset between those writes. The Rust
`BackendConnector::upload_data` selects a real SDK CRUD transaction, requests a
foreground backend RPC, returns failures to the SDK's retry loop, and calls
`CrudTransaction::complete()` only for accepted or permanently rejected work.
The SDK also owns write-checkpoint advancement; Python does not reset its
target or schedule a separate upload retry loop.

An accepted upload receipt is saved before acknowledging the transaction.
It prevents re-enqueue after a reset between transaction completion and full
canonical publication. A saved pending move still clears only on its exact
UUID and room in a committed full publication (or a durable permanent
rejection). Receipt replay handles an interrupted session save. Version 5
migration preserves the existing canonical tables and session files.

The native host proof checks five-second SDK retries with the same CRUD entry
and UUID, completion, cancellation, restart and credential failure recovery.
The adapter tests cover transport timeouts, 401 refresh, non-JSON/oversized
429/5xx responses, storage failures and receipt crash boundaries. These replace
the earlier external Python move-upload supervisor, whose transient network
errors could permanently block sync while the UI continued showing a pending
move.

On the badge, the saved move in room `6ad058a8-d19b-4d8c-b32d-e6f2b6945ccb`
was recovered without a state wipe. A forced RPC failure caused a native SDK
retry with the same UUID; the second attempt completed, its exact canonical
move was published, the pending session cleared and status became `SYNCED`
after 27.9 seconds including normal networking. This is a recovery result,
not a performance comparison. See [recovery-result.json](recovery-result.json)
and [recovery-hardware.log](recovery-hardware.log).

## Rebuild and install

From the application repo, with the existing private toolchain/cache:

```sh
python3 experiments/powersync-native-sdk/badge-module/build.py --no-cache
python3 experiments/powersync-native-sdk/badge-candidate/package.py
python3 -m unittest discover -s experiments/powersync-native-sdk/badge-candidate/tests -v
```

The USB helper uses the cached mpremote Python under
`../powerbadge/native/.tools/uv/archive-v0/d72dw6_U1MNvOQwC/bin/python`:

```sh
<device-python> experiments/powersync-native-sdk/badge-candidate/device.py install
<device-python> experiments/powersync-native-sdk/badge-candidate/device.py smoke
<device-python> experiments/powersync-native-sdk/badge-candidate/device.py diagnose
<device-python> experiments/powersync-native-sdk/badge-candidate/device.py reset
```

Installation validates the pinned board ABI, reconstructs the same FAT partition
from read-only metadata, verifies its geometry, and restores the read-only mount
after copying. It does not format flash or replace firmware. Each new file is
written to an owned temporary path, checked and renamed, with entrypoint last.
The package tool rejects a binary without the PSRAM execution/audit metadata.

## Performance investigation — 26 October 2026

The native candidate has a confirmed queued-move regression: when its SDK asks
for a later checkpoint while the download stream is active, the Python adapter
unconditionally reports a stream error and closes the stream. This happens
even without a TLS allocation failure. The SDK then applies its normal
**five-second download retry**, seeds the next iteration and reconnects. The
original client keeps its live stream during a successful upload when the
second TLS connection fits. See the exact paths and timer ownership in
[performance-source-notes.md](performance-source-notes.md).

The same-badge measurements below supersede the earlier statement that a
performance comparison had not been measured. They do not change the historical
installation or recovery results above. No production optimization, firmware
replacement, upload/retry rewrite, backend change or deployment was made during
this investigation.

Both clients ran on the same connected Tufty 2350 using their installed Python
and native modules, one variant per soft reset. The deterministic cold fixture
contains the same 49 raw moves and one room, delivered in identical 2,048-byte
fragments. Its 9,321 bytes include checkpoint metadata. The warm-upload fixture
first publishes 48 moves outside timing, then queues the same final UUID, accepts
one fixture RPC and publishes that exact UUID and room under a covering full
checkpoint. A 16 ms minimum foreground cadence exposes scheduling delays. The
native path uses the real insert-only CRUD queue and SDK upload/checkpoint
actors. Session saves in this isolated warm fixture are RAM-only; SQLite,
receipts and SDK transactions remain real.

Baseline three-run medians:

| Same-badge workload | Original | Native candidate |
| --- | ---: | ---: |
| Cold fixture, SQLite `:memory:` | 2.140 s | 0.573 s |
| Cold fixture, production LittleFS VFS | 12.869 s | 6.142 s |
| Cold fixture, VFS callback time within the flash run | 10.613 s | 5.418 s |
| Cold fixture, dirty database image writes | 10 / 421,888 bytes | 5 / 252,928 bytes |
| Cold fixture, longest foreground step with flash | 1.696 s | 2.880 s |
| Warm queued upload, `:memory:`, RPC acceptance to pending clear | 0.224 s | 5.429 s |
| Warm queued upload, flash, queue to pending clear | 1.770 s | 16.703 s |
| Warm queued upload, flash, VFS callback time | 1.469 s | 13.549 s |
| Warm queued upload, flash, dirty database image writes | 1 / 51,200 bytes | 11 / 664,576 bytes |

The native cold replay completes faster while its largest flash publication
blocks the foreground longer. SQLite/VFS work dominates both flash runs. The
queued replay separately reproduces the reconnect delay without network, TLS,
panel transfer or flash. This supports fixing the unnecessary stream teardown
before changing SDK retry timers. It does not justify bypassing the SDK's write
checkpoint or acknowledging a move from an HTTP response.

The warm flash workload adds a second cost: durable CRUD, upload receipts,
checkpoint accounting and the reconnect/reseed path trigger repeated complete
database image writes. Some commits protect required crash boundaries. The
11-image total does not imply that all 11 commits can be removed. Attribution
must distinguish the required queue/receipt/checkpoint work from the avoidable
stream teardown and ensuing reconnect.

An optional `keep_stream=true` causal control changes only that adapter policy
in RAM: when a later JSON checkpoint request needs service, it retains the active
stream unless the adapter has selected its single-TLS fallback. The exact
installed method block must match before this control is applied. It retains
the real SDK CRUD, retries, write checkpoints, receipts and fresh full
publication checks, and records the installed source hash. Control samples are
separate from the baseline measurements above. No installed source is edited
and this control is not a shipped optimization.

Those control runs describe the pre-update adapter. After the installed update
below, `--keep-stream` intentionally refuses its old source-block match. Use the
ordinary replay command to measure the updated installed app.

The same-badge control measurements are **one sample per storage mode**, compared
with the native baseline's three-run medians:

| Native warm upload | Baseline median | Keep-stream control, n=1 |
| --- | ---: | ---: |
| `:memory:`, queue to pending clear | 5.635 s | 0.495 s |
| Flash, queue to pending clear | 16.703 s | 10.117 s |
| Flash, dirty database image writes | 11 / 664,576 bytes | 9 / 543,744 bytes |
| Flash, longest foreground step | 6.100 s | 6.110 s |

The control eliminates the forced reconnect and its five-second wait while
preserving exact confirmation. Flash remains expensive, and its longest stall
does not improve. This separates an avoidable scheduling policy from the
remaining SDK/SQLite/VFS work; one control sample is not a performance
qualification for shipping a change.

The [results JSON](performance-results.json) retains raw runs, input-log hashes,
sample counts, minima, medians, maxima and attribution limits. It also records
live same-room downloads through Badgeware's managed network/VSYNC loop. Those
sequential runs include an auth refresh in a native run and stalled background
BadgeLink traffic in an original run. Auth requests and background networking
must be accounted for separately; their combined startup medians cannot support
a blanket claim that native networking or TLS is slower. Compare the room,
row count, response bytes and request counts as well as elapsed time. The fixture
results above are isolated replay measurements, not complete multiplayer match
latency.

The three live downloads reached `SYNCED` in 14.479/14.796/16.057 s for native
and 15.082/18.417/18.225 s for original: medians 14.796 s and 18.225 s under those
conditions. All live runs that recorded the raw state fingerprint agreed on
`6247094d3c65672dd1b9daecdf10c44883ae4b9436bb09d080d1ce0bd98ab747`.
The final report contains 26 replay runs, six live runs and two renderer runs.

Flash trials retain 1,024-byte pages, a 32-page cache, DELETE journaling,
`synchronous=FULL`, the production ShadowVFS and all durability barriers. The
meter counts callback operations, journal bytes and complete dirty database
images. Its timers are nested: SDK poll/native exec include VFS callbacks, and
VFS callback time includes database-image sync. Do not add those inclusive
timers. Subtracting measured VFS callback time leaves SDK/core/SQLite work and
binding overhead together; it does not isolate pure scheduler CPU. Early
per-operation arrays also include post-window validation because they were
returned by reference; the scalar total and image counters are the measured
snapshots. The harness now copies those arrays.

PSRAM is already used: the ShadowVFS database shadow is a bounded Python
`bytearray` on the Tufty's PSRAM-backed heap, and the native module's GC-owned BSS
contains its 768 KiB SQLite arena. The Rust allocator uses that SQLite allocator.
RAM-resident database I/O still becomes physical flash work at `xSync`, which
rewrites and durably flushes a complete dirty image. Moving the working database
into RAM therefore does not remove the measured commit cost. The `:memory:`
comparison isolates the path without that persistence cost.

The final native live run also checked actual allocation addresses: the SQLite
arena was at `0x1129e300`, and the 131,072-byte VFS shadow was at `0x113602e0`.
Both addresses are in the badge's PSRAM region, confirming the source-derived
memory placement on the measured device.

Live HTTP instrumentation separates DNS, TCP/SSL setup, TLS plus request send,
and response reads, recording active-call time and phase wall time. The
transport performs the TLS handshake lazily during `secure.write()`, so the
send phase combines handshake and HTTP writes. There is no pure-handshake
firmware hook. Renderer CPU and `display.update()` are measured separately;
panel transfer and VSYNC remain combined. These limits are carried into the
results instead of assigning their time to the SDK.

The renderer was measured for three rounds of 30 idle and 30 cursor frames per
client, separately from sync and storage. The tested gameplay framebuffers have
the identical SHA256
`8e1dc5ccb65586b44ff33057e1f94a9f662bab4621b9fc281ccd14dcd8777442`.
Medians of the three round medians are:

| Rendering path | Original | Native candidate |
| --- | ---: | ---: |
| Unchanged gameplay draw | 0.201 ms | 0.197 ms |
| Cursor-change draw | 11.869 ms | 11.923 ms |
| Idle `display.update()`, panel plus VSYNC | 10.984 ms | 10.981 ms |
| Cursor `display.update()`, panel plus VSYNC | 10.481 ms | 10.432 ms |

The tested gameplay rendering is effectively unchanged. These differences do
not explain the multi-second pauses during sync and commits.

Every successful replay verifies all raw room/move fields, exact UUID/room
membership, negative UUID/room cases, empty CRUD and `quick_check`. The warm
fixture additionally checks that RPC acceptance leaves pending work intact and
that clearing requires a newer committed full publication. The original badge
suite passes **181 tests**, and the candidate adapter/outbox suite passes
**36 tests**. The real SDK/SQLite C ABI proof passes **20 checks**, including
upload retries with the same UUID, cancellation, completion and restart. Host
replay checks additionally exercise both real core implementations and the warm
reconnect; their elapsed times are not badge measurements. SDK-owned uploads,
retries, raw tables, durable receipt ordering and confirmation gates remain
unchanged.

The fixture hash and input-log hashes identify replay inputs. Readback of 18
selected installed source files matched the checkout with zero mismatches.
Native loading also validates the pinned board ABI, module length and SHA256.
The source notes identify both the application adapter and sibling
`powersync-native` implementation being investigated; Git revisions alone are
insufficient to identify an SDK build from a dirty source checkout.

Reproduce from the application repository; the same USB helper Python described
above supplies `mpremote`:

```sh
python3 experiments/powersync-native-sdk/badge-candidate/performance_replay.py --variant original --storage memory --repetitions 3 --output /private/tmp/original-memory.py
<device-python> experiments/powersync-native-sdk/badge-candidate/performance_device.py /private/tmp/original-memory.py --output /private/tmp/original-memory.log

python3 experiments/powersync-native-sdk/badge-candidate/performance_replay.py --variant native --storage flash --repetitions 3 --output /private/tmp/native-flash.py
<device-python> experiments/powersync-native-sdk/badge-candidate/performance_device.py /private/tmp/native-flash.py --output /private/tmp/native-flash.log

python3 experiments/powersync-native-sdk/badge-candidate/performance_replay.py --variant native --storage memory --scenario warm_upload --pace-ms 16 --repetitions 3 --output /private/tmp/native-upload.py
<device-python> experiments/powersync-native-sdk/badge-candidate/performance_device.py /private/tmp/native-upload.py --output /private/tmp/native-upload.log

python3 experiments/powersync-native-sdk/badge-candidate/performance_live.py --variant original --output /private/tmp/original-live.py
<device-python> experiments/powersync-native-sdk/badge-candidate/performance_device.py /private/tmp/original-live.py --output /private/tmp/original-live.log

python3 experiments/powersync-native-sdk/badge-candidate/performance_host_replay.py --variant native --scenario warm_upload --pace-ms 16
python3 experiments/powersync-native-sdk/badge-candidate/performance_replay.py --variant native --storage flash --scenario warm_upload --pace-ms 16 --keep-stream --output /private/tmp/native-upload-control.py
<device-python> experiments/powersync-native-sdk/badge-candidate/performance_device.py /private/tmp/native-upload-control.py --output /private/tmp/native-upload-control.log
python3 experiments/powersync-native-sdk/badge-candidate/performance_results.py
```

Repeat with the other variant, using the same storage mode, scenario and pacing;
alternate live-network run order. The runner soft-resets between payloads. Flash
replay recreates only the selected directory's SQLite files and removes the
default `/state/dots_boxes_benchmark` scratch directory after closing. It needs
the normal database capacity plus reserve free on `/state`. The investigation
overwrites disposable cache SQLite files with operator authorization; pairing
and pending session files are retained. The tools do not install app changes.

After measurement, the native cache was restored to
`/state/dots_boxes_native/state.sqlite` and reopened through the normal Store.
The paired room still has the same nine canonical moves and raw-state hash;
`quick_check` is `ok`, schema is 6, pending is null and `ps_crud` is empty.
The scratch directory is gone and the badge was returned to its normal launcher.
See [the final hardware check](performance-final-check.log) and
[validation evidence](performance-validation.json).

The next implementation targets are the measured adapter stream-close policy
and transaction/image-write amplification. The RAM control establishes the
first cause, but still needs live concurrent-TLS and low-memory qualification
before becoming an installed change. Reducing the remaining flash cost needs
transaction-level profiling and coalescing only where SDK queue, receipt crash
boundaries and post-commit full-publication semantics permit it. Lowering retry
timers, dropping durability barriers or clearing pending on RPC acceptance would
change the correctness contract rather than address these measured causes.

## Installed stream-preservation update — 26 October 2026

With operator authorization, the measured stream policy is now installed in
`dots_boxes_native`. A checkpoint request retains the download stream unless
allocation pressure has selected the single-TLS fallback. A checkpoint
`MemoryError` releases both the failed request and any retained started streams;
unopened seed-gated streams remain available for the SDK's next attempt.
Uploads, retries, write checkpoints, receipts, raw tables and exact committed
full-publication confirmation remain SDK/application owned as before. Flash
durability and the PSRAM configuration are unchanged.

All **38 adapter/outbox tests** pass, including checkpoint/stream scheduling,
allocation failure recovery and pending confirmation. The real SDK host replay
also passes without applying the historical RAM control. Packaging retains the
same native binary hash and verifies all 23 installed files; only
`powersync/sync.py` changed on the badge, to SHA256
`be728697a5b0885afb999e4579d09f3c7457b628fbf4838ec238c4c987f9cfb4`.
The installer verified saved state unchanged and restored `/system` read-only.

The installed app passed a live managed-loop test using an idempotent SDK CRUD
entry for an already published UUID. The normal connector recognized that
canonical move, so no new move RPC or board mutation occurred. SDK transaction
completion then requested a real HTTPS write checkpoint while the existing TLS
download stream stayed open. The same stream survived, no transport errors or
single-TLS fallback occurred, full generation advanced from 1 to 2, pending
cleared and CRUD became empty. The board retained nine moves. That live check
took 9.087 seconds including durable storage and network work, and is a
checkpoint/TLS qualification rather than new-move latency. See
[the live check](update-live-check.log) and [the installed manifest](manifest.json).

Updated replay evidence is kept in `update-*.log`, separately from the historical
`performance-*.log` baseline inputs. The older source readback hashes and
performance-validation evidence identify the versions measured at that time.
Three ordinary on-badge warm-upload replays of the installed update (no RAM
patch, SQLite `:memory:`) cleared pending in a median **0.493 seconds**, versus
the earlier 5.635-second native baseline, with zero stream reconnects in every
run. All exact raw-row/UUID/room/full-publication and empty-CRUD checks passed.
See [the updated replay](update-upload-memory.log).

## Ordinary mutations and SDK watches — 26 October 2026

The insert-only command view is now an ordinary PowerSync table. A SQL INSERT
into `_badge_move_outbox` creates both its readable optimistic row and the real
SDK CRUD entry. The UI reads query snapshots driven by the SDK's committed
`watch_tables()` notifications, including changes from local writes and sync.
It renders the optimistic query row as an ordinary game edge. There is no
separate saved/display-pending record and no manual query invalidation after a
mutation. A covering checkpoint reconciles the SDK row automatically.

The connector processes only the SDK-selected transaction and returns the RPC
result. It writes no accepted receipts and performs no application cleanup.
SDK `CrudTransaction::complete()` and write checkpoints own that lifecycle.
Raw `rooms` and `moves` remain canonical server data; an optimistic row is not
mistaken for exact canonical membership. The query deduplicates an optimistic
UUID already present in the raw canonical table.

The native binding now exposes the existing Rust SDK table watches through
`sdk_reply({op:'watch',id,tables})`, coalesced `changed_watches` notifications in
`sdk_poll`, and `unwatch`. It introduces no custom SQLite update hooks or extra
upload supervisor. The table watches are bounded and cancelled on shutdown.
The raw game tables keep their logical schema but now use SQLite rowid storage:
SQLite's update hook does not report `WITHOUT ROWID` table changes, so retaining
that old physical layout would silently miss canonical query notifications.

Schema 7 removes `_badge_move_receipts`. The only legacy pending import happens
inside the database upgrade transaction; version 7 never re-enqueues from a
stale JSON file. `pending.json` and `pending.json.next` are removed after handoff,
and credentials saves never recreate them. No new pending-state table replaces
them. The operator authorized a fresh badge database for this installation.

Validation: **52 Python adapter/query/migration tests** and **31 real SDK/C ABI
checks** pass. The real SDK checks prove that a SQL mutation is immediately
readable, a failed transaction emits neither CRUD nor a watch update, local and
canonical commits trigger watches, HTTP acceptance retains the optimistic row,
and a covering full checkpoint removes it and emits another watch notification.
The new 1,107,030-byte native module has SHA256
`c5bd5fe3b43c482f3757e921b6a134d232d2882da026894e0e117171f0cccd06`;
the final 55-object audit contains zero exclusive-access instructions and retains
the PSRAM single-foreground-owner execution contract.

On the badge, the ordinary-mutation live check observed query row counts
`0 → 1 → 0`: SQL INSERT, real SDK watch notification, then SDK reconciliation
and another watch notification. The existing download stream stayed open,
canonical UUID/room membership was checked after full generation advanced, and
no pending files or receipt table remained. The live qualification used an
idempotent already-published UUID, so it issued no new move RPC.

A single offline flash replay of the same 684-byte final-move workload completed
in **9.469 s**, with **7 dirty database images / 430,080 bytes** and **8.838 s**
inside VFS callbacks. The previous stream-preserving control wrote 9 images /
543,744 bytes and took 11.228 s end-to-end. These are individual diagnostic
samples with different storage layouts, not a broad latency qualification.
The actual SDK watch exposed the optimistic row before fixture RPC acceptance,
and the covering full publication reconciled it with exact raw UUID/room and
empty CRUD checks. The operator-authorized flash replay left a fresh cache for
normal startup. See [current validation](ordinary-update-result.json),
[live watch evidence](ordinary-update-live.log), and
[flash replay evidence](ordinary-update-flash.log).
