# Badge performance qualification — 21 October 2026

The selected client changes are installed on the Tufty 2350. The installed app
opened its existing game, migrated schema 4 to 5 and downloaded through the
normal Badgeware managed network loop. All 11 saved moves, score `[1,0]`, pairing
and empty pending state survived. No gameplay moves were submitted in this
qualification. The pinned native module and firmware are unchanged.

## Measured on the badge

| Isolated path | Previous | Selected | Scope |
| --- | ---: | ---: | --- |
| Unchanged board draw | 17.55 ms | 0.21 ms | Average over three rounds of 30 draws |
| Cursor change draw | 17.78 ms | 11.05 ms | Same framebuffer, alternating order |
| Queue then clear pending move | 133.76 ms | 31.45 ms | Median of six alternating LittleFS trials |

The renderer produced identical framebuffer hashes across 19 scenes: game,
cursor, preview, pending/confirmed, offline/error/pairing and reset transitions.
Both versions then used the same framebuffer for timing; separate allocations
can land in different RAM and confound comparisons. Default clearing is disabled
only while the game runs and restored on exit. The physical display transfer,
VSYNC and network loop remain in place. The timings exclude panel transfer.

Pending-move tests used isolated temporary LittleFS files with synthetic tokens
matching the saved session's token lengths. Six queue/clear pairs wrote 13,980 →
1,026 application bytes (92.7% fewer). Both versions retain write, flush, global
sync, rename, global sync ordering. Outlier trials occurred in both versions.
This is not a physical power-cut test or a measurement of flash-sector writes.

The checked download fixture contains 9,114 bytes and 50 operations: 49 moves and
one room. It runs real framing and scheduling into a memory sink, with every
operation checked, and excludes Wi-Fi, TLS, native SQLite and flash. Download
fragments grow from 512 to 2,048 bytes; updates fall from 18 to 9 and transport
calls from 18 to 6, with nine commits in either case. Across three alternating rounds of three
repetitions, median fixture CPU time fell from 1,330.83 to 716.36 ms (46.2%).
Detailed timings are in
[the hardware data](performance-hardware-results.json).

These measurements do not establish a new end-to-end move-confirmation time.
The earlier 3.60-second full-game result in the README belongs to the previous
raw-table qualification. Startup was outside this optimization's scope.

## Selected implementation

- Unchanged screens skip drawing. Text measurements have a bounded cache, and
  logo opaque spans are prepared once. Input handling avoids temporary lists.
- Download work has bounded transport/record budgets. Ordinary quoted JSON spans
  use native byte searches and bulk copies; control-byte, escape, depth, record
  size and envelope checks remain. Decoded records are reused by storage while
  the native core receives the original operation text.
- A live 49-move client now reads and applies 49 move rows in total, down from
  1,225. Missing, conflicting or regressed suffixes trigger a full replay.
- Raw rooms/moves use `WITHOUT ROWID`; page counters share one native crossing,
  and database reads return buffer views to the existing native bridge.
- Pending moves replace a small, generation-bound overlay instead of rewriting
  credentials. Legacy session files load directly. After a base session change,
  the new format-2 session is incompatible with older clients; rollbacks require
  deliberate session handling.

The [storage report](storage-hotpaths.md) includes reproducible pinned-native
fixtures and rejected page/cache/index options. For a newly created cache, the
49-move live workload uses 58,368 → 54,272 database bytes and writes 2,367,488 →
2,229,248 full-image bytes. Native exec calls fall from 962 to 663, and storage
JSON decodes from 597 to 199. All 150 durability barriers remain. An existing
database keeps its high-water file length; migration alone does not shrink its
full-image writes. Host timings are not badge latency measurements.

## Optional measurement harness

`profile_badge.py` is a development-only attachment to an existing client/view;
the normal app never imports or calls it. It stores eight milestone timestamps
for at most 32 moves in fixed arrays: 1,088 bytes of raw buffers, plus Python
objects. It records no IDs, tokens, logs or files during timing. On this badge,
eight timestamp marks averaged 238.7 microseconds; this excludes wrapper and
state-check overhead. Run attached/detached comparisons when attributing small
differences. The final render milestone means framebuffer drawing completed,
not that the panel finished displaying it.

## Validation and backend experiments

All 189 badge tests, 73 web tests, 13 sync-policy checks, the production web
build and package verification pass. Independent reviews checked incremental
replay, framing boundaries and scheduler budgets. The optional profiler now
retains at most one stream hook across reconnects and finishes traces without
a view after the accepted/rejected outcome is known.

The [packed-stream report](packed-streams.md) compares actual SQL output through
the service compiler and pinned native core. Packed history plus own-seat
receipts reduces full-game operation bytes from 20,489 to 15,529, with the same
50 live commits. Dynamic UUID receipts add 25–50 modeled commits. Eight native
proofs cover exact UUID/room acknowledgement, checkpoint publication and reset/
revocation. This is a candidate for future device qualification, not the
installed protocol.

The prepared SQL migration replaces full replay on each insert with checks of
at most two adjacent boxes and removes a redundant index. Its incremental
results match both the original SQL replay and independent JavaScript across
784 moves in 16 games. Backfill, rebuild, archive, invalid-move rollback,
idempotency and permissions also pass locally in PGlite. Real concurrent
PostgreSQL sessions and Supabase replication were not qualified. The migration
is in the standard migrations directory: applying it changes the default
server trigger and RPC immediately, even while the badge retains its current
stream. Nothing was applied to the cloud.

## Reproduction and limits

```sh
python3 -m unittest discover -s badge/tests -v
python3 badge/tools/benchmark_download.py --before=/path/to/previous/app --output=/private/tmp/download.json
python3 badge/tools/benchmark_download.py --before=/path/to/previous/app --emit-device=/private/tmp/download-device.py
python3 badge/tools/package.py --check
node powersync-cloud/tests/sync-streams.mjs
npm --prefix web test
npm --prefix web run build
```

The emitted download script contains public source and synthetic data and can
run in RAM with `mpremote`. Render comparison is exposed by `benchmark_render.py`;
it needs initialized Badgeware graphics and both public View implementations.
The session helper accepts a previous `dots_session.py`; device trials must use
isolated paths with adequate free space. USB storage must be safely unmounted
before changing app files through MicroPython. Installation verifies all 18
package hashes and restores the system filesystem to read-only.

Backend compact-history/SQL work is a local experiment. It does not alter the
default `badge_game_v2` subscription or deploy any service changes. This stream
already excludes browser lobby rows and unused room timestamps; adding a generic
`is_badge` flag would duplicate its existing separation. Upload connection setup,
startup tuning, browser performance and Snowflake IDs were excluded by request.
