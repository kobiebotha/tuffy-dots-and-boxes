# Actual no_std SDK with Embassy: host integration proof

This proof links the **modified sibling SDK directly**, using
`powersync = { default-features = false, features = ["embassy"] }`.
It does not copy SDK source or apply the earlier experiment's visibility patch.
The host test adapter uses std for its clock/fixture machinery; the SDK's
`std` and `default` features remain disabled, as verified in
[sdk-features.log](results/sdk-features.log).

Run from the application repository:

```sh
python3 experiments/powersync-native-sdk/port-proof/run.py
```

The helper uses the existing Rust 1.96.0 toolchain and private copied Cargo
registry from the previous investigation. Builds are offline and locked. It
regenerates a source identity for the current SDK working tree, builds the
proof, executes it, and records symbols, linked libraries, sizes and features.
If SDK source changes during execution, it rejects the result and requests a
rerun. The full SDK revision and source hashes are in
[fixture.json](fixture.json); the proof's own source hashes and measurements
are in [proof.json](results/proof.json).

The actual SDK `EmbassyRuntime` schedules its existing cancellable async-task
runnables through an Embassy raw executor and a reusable four-slot static task
pool. A host-defined `__pender` merely sets an atomic readiness flag. The single
foreground polling site services that flag; it never polls recursively. The
timer callback uses a logical monotonic clock and ordinary Rust wakers, removes
registrations when canceled, and requires no Embassy HAL or hardware driver.

The fixture uses real SQLite/core 0.5.4, the game's raw compact schema, one
serialized connection and the constrained SQL profile from the original host
proof. All SDK/core Rust allocations use the compatible SQLite allocator;
preloaded host fixtures retain System provenance. Host layouts and driver
allocations are included in these figures and do not predict badge memory.

The successful run verifies:

- Explicit room subscription with defaults excluded.
- Fragmented checkpoint metadata and the 9,114-byte/50-operation game stream.
- Public rows remain invisible before full checkpoint publication.
- Every published move ID/room/edge/index and room field, with zero SQL CRUD.
- Transport failure, credential refetch and resumption from cursor 50 through
  a complete publication at checkpoint 51.
- Disconnect closes response streams; all timer registrations are released.
- Twenty-one connect/publish/disconnect/clear rounds reuse the minimum task pool.

Recorded run: **12 check groups passed**. Latest timing samples are in
`results/proof.json`; they cover in-memory download/publication. The shared
allocator peaked at **297,280 bytes** with 384 KiB configured in its 512 KiB
host backing buffer. The 256 KiB child reports `NOMEM` and eventually hits the
fixture timeout. These are host results, not badge measurements. Each Embassy
executor poll can run multiple queued tasks, so the recorded maximum poll time
differs from the previous proof's individual task-poll metric.

No Wi-Fi, TLS, flash, ShadowVFS or MicroPython native loader is exercised here.
This is neither a sideloadable app nor a matched performance comparison with
the custom MicroPython client. ARM compilation and loader qualification are
separate gates. The earlier `host/` proof and its results remain unchanged.
