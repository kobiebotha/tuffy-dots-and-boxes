# Tufty end-to-end latency — 27 October 2026

Three complete 49-move games produced **69 local and 66 remote observations**
after excluding the first four moves of each game as warmup. Local feedback
reached display submission in a median **43 ms**, while Supabase upload
acknowledgement took about **1.24 seconds**. The remote display median was bounded
between **1.898 and 2.078 seconds**.

| Path | Retained samples | Observed p50 (ms) | Observed p95 (ms) | Observed max (ms) |
| --- | ---: | ---: | ---: | ---: |
| App move write → local display submission | 69 | 43 | 111 | 144 |
| App move write → Supabase persistence acknowledgement | 69 | 1,238 | 1,351 | 2,373 |
| Supabase commit → relevant SQLite row persisted | 66 | 1,830–2,010 | 2,187–2,372 | 3,490–3,678 |
| Supabase commit → display submission | 66 | 1,898–2,078 | 2,252–2,436 | 3,565–3,752 |

Remote ranges bound each statistic using the lower and upper per-sample
latencies. They are neither sample min/max ranges nor confidence intervals for
a wider population. Bounds are rounded outward to whole milliseconds; upload
acknowledgement upper bounds are rounded upward. Local display point values
are rounded to the nearest millisecond. The [machine-readable summary](latency-results/2026-10-27-final/summary.json)
retains microsecond-derived precision. Percentiles use the nearest-rank method.

## Method

The qualification used one USB-powered Pimoroni Tufty 2350 at 250 MHz, running
MicroPython 1.29.0 (`bw-1.29.0-gc`, 22 September 2026). All 23 installed app files
matched their source hashes before measurement. The normal app and pinned
native module were unchanged; the probe and scheduled test controls ran in RAM
through Badgeware's managed loop, with `HIRES | VSYNC` retained. SQLite used
schema 5, DELETE journaling, `synchronous=FULL`, a 32-page cache, and 1 KiB pages.
The database grew from 63,488 to 64,512 bytes. Wi-Fi RSSI was −53 dBm before
the games and −49, −55, and −51 dBm after the three runs respectively.
The raw-REPL controller restores `/system/lib` so the app can use the badge's
installed shared DNS helper, as it does from its normal launcher environment.

Each game used a dedicated room and accounts. Legal horizontal edges were played
in row/column order, followed by vertical edges, respecting the current seat
and extra turns for completed boxes. The first four moves of each game are
marked as warmup and excluded before pooling: six local and six remote moves
across the three games. Each game retained its own clock mapping; only derived
durations and intervals were pooled. Host controls stayed quiet during each
sample, and each export was saved after completion.

Local timing starts when a scheduled normal controller claim enters the app's
move-queue callback. This measures app logic, not physical
button-press latency. The app saves `pending.json` durably before drawing its
local edge. The accepted upload RPC response is a persistence acknowledgement
upper bound; it includes response transit and does not identify the precise
server commit instant.

Downloads use the existing authenticated Supabase RPC. The host brackets its
request and response, maps badge timestamps through serial clock probes with
an explicit **1,000 ppm drift allowance**, and retains the resulting latency
interval. This is an engineering assumption rather than qualified oscillator
accuracy. Each host RPC used a fresh curl HTTPS process without connection
reuse; its request/response window includes connection setup. The SQLite
endpoint is the committed publication of the exact move UUID and room in the
relevant table. The timestamp precedes the extra identity read.

Display submission means the relevant frame's `display.update()` returned.
Framebuffer drawing is recorded separately. The display driver can still have
its final DMA column outstanding at return, and no optical pixel measurement
was made. Local framebuffer drawing alone had p50 **32.811 ms** and p95
**102.874 ms**; the table uses the later display-submission endpoint.

## Instrumentation cost and scope

The post-game [observer calibration](latency-results/2026-10-27-final/observer-calibration.json)
measured `Probe.now()` at 14.014 µs per call over 2,000 iterations, including an
empty-loop cost of 1.438 µs per iteration. The extra exact-move correlation
query, measured 100 times after ten warmups, took p50 **15.126 ms**, p95
**15.292 ms**, and at most **16.094 ms**. It runs after the SQLite timestamp and
adds approximately 15 ms to the observed remote display path.

No correction was subtracted. Wrapper/check overhead was not fully calibrated,
and no external enabled/disabled A/B timing was performed. These are
instrumented, descriptive results for this badge, workload, and Wi-Fi
connection. Startup, reconnection, physical button detection, and optical
pixel visibility are outside the measured boundaries. Earlier confirmation
benchmarks end at a different milestone and do not form a matched comparison.

## Reproduction and evidence

The [benchmark guide](latency-benchmarks.md) defines every boundary and control.
The repeatable device qualification is [qualify_latency_hardware.py](qualify_latency_hardware.py);
pooled statistics come from [summarize_latency_runs.py](summarize_latency_runs.py),
with warmup annotations joined by sample identity. The retained captures and
before/after hardware metadata are in [the qualification folder](latency-results/2026-10-27-final/).
All three captures share instrumentation source SHA-256
`280cc592f943515548ab6a976668ef0ef1b8e7cc4658ef67de8246c80a8e459d`.

[Game verification](latency-results/2026-10-27-final/verification.json) confirmed
all three games finished with 49 contiguous moves and a 10–10 score. Each badge
database held all 49 published moves, with no pending move or SQL CRUD.
The final dataset had **zero dropped samples, rejected/unknown RPCs, duplicate
RPCs, or clock exclusions**. Earlier setup-only attempts stopped before any
measured move; their archived diagnostics are outside this final dataset.
No legitimate measured partial samples were discarded.

## Reusable prose

On one Tufty 2350, three complete games showed why local feedback and network
confirmation deserve separate measurements. From app move-callback entry, a
durably saved local edge reached display submission in a median 43 ms; the
accepted Supabase upload acknowledgement took about 1.24 seconds. For updates
originating in Supabase, the median latency was bounded at 1.830–2.010 seconds
to the published SQLite row and 1.898–2.078 seconds to display submission.
Those remote bounds include clock and commit-time uncertainty. The figures
include instrumentation, and display submission does not establish optical
pixel visibility.
