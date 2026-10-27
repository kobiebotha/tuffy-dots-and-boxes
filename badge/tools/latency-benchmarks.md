# Device latency benchmarks

The benchmark covers four end-to-end paths in the existing Tufty app:

| Requested path | Start | Recorded endpoint |
| --- | --- | --- |
| Local write → screen | Entry to the app's move-queue callback | First frame containing the saved local edge, then return from its `display.update()` |
| Local write → persisted in Supabase | The same move-queue entry | Accepted response from the existing `submit_move` RPC |
| Supabase update → relevant SQLite table | Commit of a host-submitted move, bounded by its request/response | Return from the FULL SQLite transaction that publishes that exact move UUID and room |
| Supabase update → screen | The same bounded Supabase commit | First frame containing that move's canonical edge, then return from its `display.update()` |

The retained hardware qualification is dated **27 October 2026**: three complete
49-move games, with 69 local and 66 remote observations after warmup. Captures
and hardware metadata were saved in the project after each sample. All 23
installed package files matched their source hashes. The badge ran at 250 MHz
on MicroPython 1.29.0 (2026-09-22), USB powered. Wi-Fi RSSI is retained in each
run's metadata. See the [results report](latency-results.md) and
[pooled data](latency-results/2026-10-27-final/summary.json) for values,
instrumentation costs, and statistical limits.

## What the boundaries mean

Normal local moves are durably written to the generation-bound `pending.json`
overlay, using write, flush, sync, rename, sync. They are displayed from that
pending state and uploaded through the existing RPC. They first appear in the
SQLite `moves` table after PowerSync downloads and publishes them. Consequently,
local write → SQLite is an upload/download roundtrip, not a direct local SQLite
write. The export can retain both the pending-file save and the optional SQLite
roundtrip as diagnostics; neither changes the four primary measurements.

The RPC response is a **persistence acknowledgement upper bound**. A successful
new move committed before that response was received; response transit and app
scheduling are included. The existing RPC does not return an exact database
commit timestamp. Reject errors, timeouts, and idempotent duplicate responses
must not be counted as fresh successful commits. Keep failed and incomplete
attempt counts alongside successful samples.

Two graphics endpoints are deliberately separate:

- `framebuffer_us`: the relevant edge has been drawn into RAM.
- `panel_submitted_us`: the associated `display.update()` has returned.

The [pinned Tufty driver](https://github.com/pimoroni/tufty2350/blob/ee84772fb7f7d5424e9593029caba815c6f89c80/modules/c/st7789/st7789.cpp)
waits for VSYNC when enabled and sends the frame by column. It deliberately
returns with the final column's DMA still outstanding. Therefore the second
endpoint is **display submission**, not complete transfer or proof of visible
pixels. No optical visibility measurement was made in this qualification.
Actual write-to-visible-pixel timing requires an external
optical measurement, such as a high-speed camera or photodiode with a correlated
trigger. Do not relabel framebuffer timing as screen paint.

SQLite timing must identify the requested UUID in the **published `moves`
table**, with the expected room. Incoming bytes, an oplog insert, an unrelated
checkpoint, or an increase in room move count alone are insufficient. Record
the commit timestamp before performing the identity read. The read validates
the sample; it must not silently redefine the timestamp as query completion.
`FULL` above describes SQLite's durability mode. A partial checkpoint can also
publish the relevant row through `DidCompleteSync`; the download metric uses
the earliest such committed publication. The app's separate local pending-move
confirmation still requires a full checkpoint.

## Running on the badge

The full-game qualification used for the README creates dedicated anonymous
test accounts, pairs both seats, and drives all 49 legal moves through the
normal app callback and authenticated opponent RPC. Select a new output folder:

```sh
python3 badge/tools/qualify_latency_hardware.py --port=/dev/cu.YOUR_BADGE \
  --output=badge/tools/latency-results/YOUR_NEW_RUN --games=3
python3 badge/tools/verify_latency_games.py badge/tools/latency-results/YOUR_NEW_RUN
python3 badge/tools/summarize_latency_runs.py \
  badge/tools/latency-results/YOUR_NEW_RUN/run1-capture.json \
  badge/tools/latency-results/YOUR_NEW_RUN/run2-capture.json \
  badge/tools/latency-results/YOUR_NEW_RUN/run3-capture.json \
  --clock-drift-ppm=1000 --output=badge/tools/latency-results/YOUR_NEW_RUN/summary.json
python3 badge/tools/qualify_latency_hardware.py --port=/dev/cu.YOUR_BADGE --return-home
```

The 1,000 ppm allowance is the published run's explicit assumption; it is not
a measured oscillator specification. The qualification starts with a fresh
MicroPython heap and restores the normal `/system/lib` search path. Public
captures are saved after each sample; the generated `.gitignore` excludes the
private session/recovery directory. Keep those private files out of published
artifacts. The final command returns the badge to its normal launcher after
database cleanup. It does not restore the previous pairing; the completed
test game remains saved, with the original state backed up privately.

For a manually driven capture with the general runner:

First close the running game cleanly and leave the badge at the REPL. The host
transport enters raw REPL with `soft_reset=False`, which still sends Ctrl-C;
starting it against a game with an open database could interrupt a write.
Safely eject any USB mass-storage mount first. Use a dedicated paired test
room, with no pending move, and an explicitly selected serial port.

The host runner needs the `mpremote` Python package. Its standard local-move
capture uploads a self-contained script into RAM, launches the installed app,
collects clock probes, and saves the final export after clean database close:

```sh
python3 badge/tools/benchmark_latency.py \
  --port=/dev/cu.YOUR_BADGE \
  --seconds=180 --capacity=24 \
  --output=/private/tmp/dots-latency-capture.json
```

Play test moves during that window, using the other seat to advance the game
when necessary. Use `--disabled` for an equivalent uninstrumented comparison.
The default app search checks `/apps/dots_boxes` and `/system/apps/dots_boxes`.
Use `--app-path` to select another installed location. `--disabled` cannot be
combined with download trials; it supplies an external-timing baseline rather
than internal milestone values.
Host capture duration is 1–220 seconds, with device cleanup time reserved after
the measurement window. The generated manual script accepts 10–240 seconds.
Capacity is 1–64 samples per direction. Reaching capacity drops further samples
and retains the drop count.

For source inspection or a manual launch, emit the same RAM script separately:

```sh
python3 badge/tools/prepare_latency.py --emit-device=/private/tmp/dots-latency-device.py
```

An alternative manual launch is:

```sh
mpremote connect /dev/cu.YOUR_BADGE resume run --no-follow /private/tmp/dots-latency-device.py repl
```

The manual `--no-follow … repl` transition remains unqualified. Plain
`mpremote run` waits for completion and does not forward stdin. The qualified
path handles raw REPL directly and does not use this manual transition or mount
a host filesystem.

Control requests are JSON lines containing `command` and `request_id`. Replies
start with `DOTS_LATENCY ` followed by JSON. Commands are `clock`,
`arm_download`, `schedule_local`, `export`, `status`, and `stop`. While a sample
is active, only `stop` is accepted; other commands return `Busy`. Arm the expected remote UUID,
edge, and move index **before** its RPC. The `finished` reply contains the final
device export after app cleanup. Use `stop` or the bounded run duration to finish;
Home/Reset loses the RAM capture. Exporting and clock probing belong between
samples. `schedule_local` selects a legal edge and delays the claim by a few
frames before invoking the normal controller callback; its measured start is
app callback entry, not a physical button press. A `sample_done` notification
is emitted only after all timestamps for that sample, allowing the host to wait
silently during measurement instead of polling.

The launcher preserves Badgeware's normal `HIRES | VSYNC` loop, watchdog,
polling, network scheduling, and persistence. It restores its wrappers on exit.
It does not install app files, change backend schema/configuration, or deploy.
Live trials perform real game moves through the existing backend. Do not use
`mpremote mount` during a trial: its binary file protocol also consumes stdin
and can collide with control JSON.

For download trials, provide `--downloads=/absolute/plan.json` and
`--session=/absolute/opponent-session.json`. The private session must be a
regular file owned by the current user with mode `0600`, containing `url`,
`key`, `access_token`, and `room_id`. Use an existing authorized session for the
seat whose turn it is; the runner does not pair accounts or refresh tokens.
It sends exactly one RPC per planned sample and does not retry unknown
outcomes. The plan is an array, for example on an empty board when that seat
can legally play the first edge:

```json
[{"sample_id": 0, "edge": ["h", 0, 0], "move_index": 1, "wait_seconds": 10}]
```

`move_id` is optional; the host generates a fresh UUID when omitted. The quiet
window must allow SQLite publication and display submission to complete before
the next trial. The runner stops with a partial capture on rejection, duplicate,
unfinished download, or unknown RPC outcome. Plan only legal moves for the
credentialed seat; it cannot automatically play the other seat's turns.
The runner brackets every explicit download with clock probes and records a
final clock read in the `stop` acknowledgement. Local-only capture keeps a quiet
measurement window because its durations come entirely from the badge clock.

## Supabase-origin measurements and the two clocks

The host and badge each use a monotonic microsecond clock. Do not subtract their
raw readings, use wall-clock time, or treat an RPC response timestamp as its
commit time.

For each clock probe, retain:

```text
host_send_us, device_us, host_receive_us
```

The host-minus-device clock offset at that instant lies in
`[host_send_us - device_us, host_receive_us - device_us]`. Expand the interval
by an explicit drift allowance for the distance from each probe, then
intersect the probe constraints. Empty intersections indicate an invalid
clock model or capture and must exclude the affected samples.

For each new, successful download move, retain:

```text
sample_id, host_send_us, host_receive_us, accepted=true, duplicate=false
```

The Supabase commit lies somewhere inside that request/response bracket. If a
device event maps to host interval `[event_lo, event_hi]`, its latency lies in:

```text
[max(0, event_lo - host_receive_us), event_hi - host_send_us]
```

Report that interval. A zero lower bound is valid when the update reached the
badge before the host received the RPC response. Do not report the midpoint
as an exact latency. Short serial probe RTTs and probes close to the event
improve the bound; they do not remove uncertainty in the HTTP commit bracket.
The drift allowance is an engineering assumption until the clocks have been
qualified; record the chosen value with every run.

## Repeatable workloads

1. Record app/source revision, native module digest, installed firmware,
   database size/high-water pages, display mode, Wi-Fi conditions, power source,
   probe capacity, clock drift assumption, and whether instrumentation is on.
2. Start in a dedicated paired room, with no pending move, initial full sync
   complete, and the normal live stream established. Keep cold startup and
   reconnection samples in separate runs from steady-state moves.
3. Warm the display, font cache, native code, and live connection with several
   unmeasured moves. Establish the expected row and edge before each sample;
   an already-present UUID is not a download measurement.
4. Use a predetermined legal edge sequence and one writer per seat. A simple
   sequence is horizontal edges in row/column order, followed by vertical
   edges in row/column order. Follow the canonical current seat after every
   move: completing a box grants another turn, so seats do not necessarily
   alternate by move number.
5. For local samples, drive a real app claim and wait for its exact UUID to
   resolve before the next claim. For download samples, arm the badge first,
   send one host move from the authorized seat, and wait for its SQLite and
   display milestones before sending another. Avoid overlapping trials.
6. Repeat across fresh rooms, including early and near-full boards. A room
   has only 49 legal moves. Retain warmups separately and aim for at least 30
   valid observations per reported workload across multiple rooms. Small
   sample counts produce descriptive percentiles, not reliable tail estimates.
7. Run equivalent attached and detached workloads in alternating order. Use
   external timing for that comparison; disabled instrumentation cannot
   supply internal milestones. Measure timer/buffer overhead separately and
   record any extra identity queries. Do not automatically subtract a
   calibration number from every observation.

Export after the window so JSON encoding, serial logging, file writes, and
report allocation do not become part of the measured path. Keep IDs only as
needed for in-memory correlation; exported timings need sample numbers rather
than tokens, credentials, row bodies, or saved sessions.

## Reporting

Device and host exports use `format_version: 1`. Device exports contain `local`
and `downloads` arrays; host exports contain `sync_probes` and `download_rpc`.
All milestone values use monotonic microseconds from the device session origin.
The same `sample_id` joins an armed download to its host RPC capture.

```sh
python3 badge/tools/report_latency.py /private/tmp/dots-device.json \
  --host=/private/tmp/dots-host.json \
  --clock-drift-ppm=YOUR_EXPLICIT_ASSUMPTION \
  --output=/private/tmp/dots-latency-report.json
```

The report includes sample counts, p50/p95/max, exclusions, and dropped samples.
Download summaries keep lower- and upper-bound statistics separate. Preserve
the raw exports and run metadata with the summary. Keep rejected, failed,
incomplete, duplicate, and out-of-capacity samples visible instead of silently
selecting successful fast moves.
