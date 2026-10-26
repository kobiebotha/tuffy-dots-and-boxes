# Tuffy Dots and Boxes

Two Tufty 2350 badges, or one badge and a virtual badge in the browser, play one Dots and Boxes board. A web dashboard creates the room, shows a five-letter pairing code (A, B, and C only), and lets you join the other seat in the same tab. PowerSync delivers the board to each badge and to anyone watching in the browser. Moves are sent with a Supabase call, then synced back.

The linked projects are Supabase `rjjvtmqicwtikbveofyq`, PowerSync instance `6ad7f348423e048d1cdd5f03`, and the Vercel project `tuffy-dots-and-boxes`.

## Web

```bash
cp web/.env.example web/.env.local
# Fill VITE_SUPABASE_ANON_KEY with the Supabase anon / publishable key.
npm --prefix web ci
npm --prefix web test
npm --prefix web run dev
```

Open the site, create a room, and enter the spaced code on the badge. On that same room page, select **Join as virtual badge** to join and pair the second player automatically. You can then play both sides with one browser tab and one Tufty. Someone else can open the waiting room and choose a virtual badge or **Join with a physical badge** to get their own code. Anyone can watch. Each player account can have one waiting or playing room. Finishing it, archiving it as the host, or leaving it as the guest frees them to make another.

The host can **Archive room** from its lobby card or room page, including while
the game is playing. Archiving preserves the board history, closes the room,
invalidates its pairing codes, and returns connected badges to pairing when
the closed state syncs. It also frees both players to start another room.

Anonymous sign-in is required. There is no email form.

The browser opens one local database per anonymous user, restores sign-in once,
and displays connection errors in the lobby as well as room pages. `Live` means
the client is connected and has completed a sync; cached data remains usable
while it reconnects.

## Virtual badge

The room page (`/room/<id>`) includes **Join as virtual badge**. It uses an
independent persistent anonymous account, device ID, and local PowerSync
database, keeping the room creator's physical badge and archive controls intact.
Reloading or reopening the room restores the virtual badge. Failed pairing can
be retried with the same button without reserving another player seat.
The badge UI is React and uses the same pairing and move APIs as the physical
badge. Only PowerSync's SQLite database uses WebAssembly. The dev and build
commands copy the SDK's prebuilt workers and matching SQLite assets into
`web/public/assets/powersync/` so they are served together in development and
production. These generated files are ignored by Git.

1. Create a room and enter its code on the Tufty.
2. Select **Join as virtual badge** on the same page. Joining and pairing happen together.
3. Click a free edge, or use A / B / C (previous / claim / next). Focus the
   virtual badge for keyboard controls. **Auto-play my turns** makes it an
   automatic opponent; each move waits for PowerSync confirmation.

The legacy `/emulate` route remains available for testing. Its default profile
shares the room page's virtual badge. For testing without hardware, use
`/emulate` and `/emulate?badge=2` in two tabs.
Each profile is isolated. Both can create/join/pair, and the regular dashboard
can watch. Moves are saved before upload and retries reuse the exact UUID,
including after reload. Profiles accept 1–24 lowercase letters, digits,
underscores, or hyphens. The route is hidden from navigation; authentication
and server rules still enforce access and turns.

## Badge

The app is prepared under `badge/app/dots_boxes`. Package it with:

```bash
python3 badge/tools/package.py
```

That stages `web/public/dots-boxes.zip` and a manifest with content-versioned app files under `web/public/badge-app/`. It refreshes the original `web/public/apps/dots_boxes` copies and replaces only the generated `badge-app` subtree, keeping one release. Run `python3 badge/tools/package.py --check` to verify every served copy, manifest, and ZIP against the explicit app sources and reviewed native module; the web build runs this check too. Packaging never copies onto a USB drive.

On `/install`, desktop Chrome or Edge can ask you to select the mounted Badgeware drive, copy the app, and read every file back to verify it. An existing `apps/dots_boxes` folder offers **Update game on badge**. Only packaged app paths are replaced; unknown files and `/state/dots_boxes` (pairing, database, pending moves) are retained. The browser snapshots replaced files in memory and attempts verified restoration on failure. If recovery cannot be verified, keep the drive connected and retry before resetting. Avoid editing the app from another program while copying; browser file operations cannot form a transaction against other OS writers. A successful readback does not replace safe OS ejection. Eject the drive in your operating system and press Reset once.

The page works without multiplayer sign-in or backend configuration. The ZIP and manual copy instructions remain available for other browsers: unzip the archive and copy `apps/dots_boxes` onto the Badgeware file drive, then eject and reset. The `RP2350` BOOTSEL firmware drive is rejected. Installer checks run with `npm --prefix web test`, `python3 -m unittest discover -s badge/tests -p test_package.py`, and `npm --prefix web run test:browser` (installed Google Chrome). Browser tests use a simulated drive, so physical picker, drive caching, ejection, and launcher behavior still need qualification on a real badge.

On the badge, A, B, and C append a letter, Up deletes, and the fifth letter sends the code. During a game only the seat whose turn it is can claim a line. Hold **Up for 2 seconds** during or after a game to disconnect this badge and return to pairing. A saved move resolves before leaving. This keeps the room and the other player in place; the same badge can re-enter its code while the room is open. Home closes the database and returns to the launcher while staying paired.

Claimed edges appear in the badge player's color immediately after the move is
saved locally, and stay visible during upload, retries, and reopening the app.
PowerSync confirmation replaces this pending edge with the canonical edge;
server rejection removes it. Scores, boxes, and the next turn wait for the
confirmed board, and a pending move blocks further claims.

The native module is the reviewed `badgegotchi_native.mpy` (PowerSync core 0.5.4). Do not rebuild it for this app.

The badge disables default subscriptions and requests only `badge_game_v2` for
its paired room. The browser lobby and previous games never enter its 128 KiB
database. Schema 5 stores six room values and three move values in native SQLite
raw tables without row IDs; `account` remains a local-only PowerSync table. UUIDs are encoded as
24-character base64 text. RPC requests and saved sessions retain canonical UUIDs.
Edge codes 0–48 encode the server-enforced 5×4 board. The browser schema is unchanged.

Upgrading schema 1, 2, or 3 atomically replaces only the synced cache, retaining
the account, client ID, and durable pending move. A complete checkpoint rebuilds
the board before play. Every database open registers the raw-table clear callbacks
before account reconciliation, and every sync start supplies the raw-table schema.
Incoming data is committed in bounded FULL transactions;
queries are paged below the native wrapper's 32-row result limit. The board is
reread only after a completed checkpoint and replayed only when its move count
changes. Increasing move counts read and apply only the new contiguous suffix;
inconsistent history falls back to a complete replay.

Schema 4 upgrades retain published rows, checkpoints and local state while
converting the two raw tables in a transaction.

The database uses a fixed 128 KiB PSRAM buffer. Random database reads and writes
stay in memory; each SQLite database sync writes the complete logical image
sequentially and flushes the LittleFS file. The rollback journal stays on flash,
with FULL ordering preserved. This avoids the expensive file-tail copies caused
by random LittleFS writes. The free-space guard reserves room for a replacement
image plus 64 KiB. This relies on the qualified Tufty LittleFS implementation;
it is not a general-purpose filesystem VFS.

An upgraded SQLite file retains its previous high-water size because the VFS
does not support shrinking files. Migration returns old cache pages to SQLite's
freelist for reuse within the existing 128 KiB cap. A newly created raw cache
finishes a full 49-move live game at 53 KiB in the current host benchmark; an upgraded
file may remain larger while using fewer pages.

Startup and migration use an eight-page SQLite cache, then raw schema 5
uses 32 pages within the fixed 256 KiB native arena. The stream reader drains
available data before flushing a batch that is at least one second old and has
no more bytes available, up to six records / 8 KiB. Full checkpoints remain publication boundaries;
network waits never hold an open SQL transaction.

Startup opens the download stream immediately. An accepted move keeps that
stream open and stays saved until its exact move UUID and room binding appear
in the published table after a full checkpoint. Accepted moves require no extra
checkpoint HTTP request. A reset reuses the saved UUID. Rejected moves retain a
conservative checkpoint barrier. Entering a code from a different browser account
safely replaces the synced cache after saving the new session; an unconfirmed
move must sync before changing accounts.

Deploy the matching sync config before installing this badge version. The linked
PowerSync instance is named `Production`; obtain approval before deploying:

```bash
powersync deploy sync-config --directory powersync-cloud
```

Changing sync streams does not require a service-config or Vercel deployment.

## Checks

```bash
python3 -m unittest discover -s badge/tests -v
npm --prefix web test
npm --prefix web run build
node powersync-cloud/tests/sync-streams.mjs
deno test --cached-only --lock=supabase/functions/deno.lock supabase/functions/badge-pair/handler_test.ts supabase/functions/badge-pair/http_test.ts
deno check --cached-only --lock=supabase/functions/deno.lock supabase/functions/badge-pair/index.ts
```

`supabase/tests/rules.sql` rolls back a rules check: one open room per person,
starting only after both badges pair, duplicate and turn validation, a full
49-edge game with 20 scored boxes, and creating a new room after finishing.

The stream policy tests compile and evaluate the real PowerSync config, including
membership checks, a full 49-move board, cancellation, and spectator privacy. They
use the sibling `../../powersync-service` checkout by default; set
`POWERSYNC_SERVICE_SOURCE` to use another built service checkout.

## Measured optimizations

Reproduce the local service-compiler and pinned-native benchmark with:

```bash
node powersync-cloud/tests/benchmark-projection.mjs
```

The saved report is `badge/tools/projection-benchmark-results.json`. This earlier benchmark
requires Node with `node:sqlite` and TypeScript stripping (verified on Node 25.9). The full
49-edge board measurement compares the original `badge_game` projection with
`badge_game_v2` using JSON table storage, before schema 4 enabled raw tables.
These are host measurements, not Tufty Wi-Fi or flash timings.

| Measure | Previous badge stream | Compact stream |
| --- | ---: | ---: |
| Selected row JSON | 7,677 B | 3,875 B |
| Complete PUT envelopes | 15,167 B | 10,354 B |
| Fresh native database | 82,944 B | 66,560 B |
| Peak SQLite allocation | 215,680 B | 195,712 B |
| Database after 49 live moves / 50 room revisions | 91,136 B | 67,584 B |

PUT measurements include actual service checksums and JSON escaping, excluding
HTTP/TLS, bucket/checkpoint framing, and other replication metadata. All 27
benchmark scenarios verify published fields, row totals, and an empty SQL outbox.
Both columns use the original eight-page cache to isolate projection changes.
The separate [cache benchmark](badge/tools/cache-benchmark.md) qualified the
32-page runtime cache: live database reads fell from 2,178 to 620 and sync calls
from 414 to 150, with a worst native allocation of 208,128 B. Its host timings
exclude LittleFS, MicroPython callbacks, and Wi-Fi.

The current raw-table comparison keeps the compact wire operations, 32-page
cache, and production Python VFS identical. Reproduce it with:

```bash
node powersync-cloud/tests/benchmark-projection.mjs --service-only --output=/private/tmp/dots-raw-service.json
python3 badge/tools/benchmark_raw_tables.py /private/tmp/dots-raw-service.json --repetitions=3 --output=badge/tools/raw-tables-benchmark-results.json
```

The [benchmark script](badge/tools/benchmark_raw_tables.py) and
[saved results](badge/tools/raw-tables-benchmark-results.json) compare JSON tables,
raw moves only, and raw rooms plus moves. Schema 4 uses the last option:

| Measure | Compact JSON tables | Raw rooms and moves |
| --- | ---: | ---: |
| Completed board downloaded to a new cache | 66,560 B | 57,344 B |
| Database after 49 live moves / 50 room revisions | 67,584 B | 58,368 B |
| Peak native allocation with live board reads | 207,936 B | 154,240 B |
| Database read callbacks with live board reads | 956 | 380 |
| Complete database-image bytes flushed during live play | 2,747,392 B | 2,367,488 B |
| SQLite sync barriers during live play | 150 | 150 |

Raw tables reduce the completed live cache and database-image bytes flushed by about
14%, and peak native allocation by about 26%. The reserved 256 KiB native arena
and 128 KiB database shadow remain fixed. Wire bytes, checkpoints, and FULL
durability barriers are unchanged. Host timings include macOS `os.sync` and do
not establish a Tufty latency improvement. Separate pinned-core and production
VFS tests cover migrations from schemas 1–3, rollback, sync and REMOVE after
reopen, account clearing, pending-move recovery, and all 49 moves.

- `base64(uuid_blob(id))` safely shortens UUID text from 36 to 24 characters.
  Bare BLOB results disappear from the current service's JSON projection; a
  BLOB ID becomes empty. The pinned native module also has no local
  `uuid_blob()` function, so conversion belongs in the service projection.
- Short room/move keys reduce both wire JSON and the JSON stored by the core.
  A single edge integer replaces orientation/row/column. Move seat is derived
  by replay; fixed dimensions and unused roster counts are omitted.
- Move `created_at` remains on the server for diagnostics, but is absent from
  both badge and browser move projections.
- Changing Postgres `int4` to `int2` saves source-table bytes, but does not
  change these JSON numbers or SQLite's value-sized integer representation.
  No source schema migration is needed for this optimization.
- A packed 49-character history could reduce fresh transfer further, but needs
  a backend-derived projection; streams cannot aggregate move rows. This
  prototype is not enabled in the app.

Primary references: [PowerSync SQL functions](https://docs.powersync.com/sync/rules/supported-sql),
[SQLite UUID extension](https://github.com/sqlite/sqlite/blob/master/ext/misc/uuid.c),
[SQLite record format](https://sqlite.org/fileformat2.html#record_format), and
[Postgres integer types](https://www.postgresql.org/docs/current/datatype-numeric.html).

## Deployment and hardware checkpoint

PowerSync streams version **4** is deployed to the linked instance, with initial
replication complete, zero replication lag, and no reported errors. The deployed
browser pairing function includes CORS preflight support and checked atomic
pairing claims. Existing badges can continue using `badge_game`. No Vercel
deployment or Git push was performed.

Verification: 94 badge tests (including the actual pinned native core and
production Python VFS), 32 web
tests, 13 stream policy tests, and 13 pairing/HTTP tests pass; TypeScript and the
production web build pass. Two independent browser emulators completed all
49 edges as a 10–10 draw; the regular dashboard displayed the same result.
Reloading during a move recovered its saved UUID and confirmed it after retry.
`web/public/dots-boxes.zip` is rebuilt from the final 17-file app, including the
unchanged pinned native artifact.

The physical Tufty recovered its previously accepted pending UUID, migrated to
schema 3, and completed its original game. A second full game paired through the
real five-letter endpoint and played the physical badge against `/emulate`:
49 edges, 10–10, no pending move, zero SQL CRUD, and zero extra checkpoint HTTP
requests. Every physical move was checked by its exact UUID after publication.

On the same badge, the direct LittleFS VFS with a 32-page cache took a median
15.67 seconds to confirm a move. The database shadow and stream batching reduced
that to **4.08 seconds** across 25 physical moves (3.85–5.43 seconds). Database
open took 1.15 seconds; the first full sync including real pairing took 14.66
seconds. The match finished in about three minutes. These are single-device
measurements through normal Badgeware managed networking, close to the access
point, not a latency guarantee. See the [hardware report](badge/tools/hardware-qualification-results.json)
and [storage qualification](badge/tools/storage-qualification.md) for methodology
and power-loss testing limits.

That schema 3 app was installed on the Tufty. All 17 packaged files matched their
source bytes, the pinned native artifact was unchanged, and all 220 unrelated
badge files retained their hashes. A fresh launch of the installed files, with
no source overlay or autoplay, reopened the complete 49-move game in 1.20 seconds
and finished syncing in 7.83 seconds from harness start. The score remained
10–10, with no pending move and an empty SQL outbox. The previous app is backed up at
`/var/folders/g4/djq7_lkd2v3272ykq8__p4zm0000gn/T/dots-badge-before-update-77elphz7`.

## Raw-table hardware qualification

Schema 4 is now installed on the Tufty. Only `dots_storage.py` and `dots_sync.py`
were overwritten, without a backup; all 17 installed package files matched
their source hashes and the pinned native module was unchanged. Installation
temporarily remounted the game partition for writing and restored its read-only
mount afterward. The installer ZIP is rebuilt. No service or Vercel deployment
is required for this client-only change.

The existing schema 3 cache migrated and resynced its complete 49-move, 10–10
game with no pending move or SQL CRUD. It retained its 76 KiB high-water file.
After that check, only this completed synced cache was recreated, retaining the
pairing session, to qualify a new raw-table cache.

A fresh full match through real five-letter pairing and normal Badgeware managed
networking finished all 49 moves as a 10–10 draw. All 25 physical badge moves
were confirmed by their exact UUID after publication, with zero extra checkpoint
HTTP requests and an empty SQL outbox. Median confirmation was **3.60 seconds**
(3.27–5.10 seconds), compared with 4.08 seconds in the previous run: an 11.8%
reduction in this measurement. The database finished at **66 KiB**, compared
with 76 KiB previously, and native peak usage was 133,760 B. This is one badge
and Wi-Fi connection; the previous opponent was the browser emulator and this
run used an authenticated RPC opponent, so it is not a randomized A/B result.

A subsequent launch of the installed files, without source overlays or autoplay,
verified native raw tables, schema 4, the 32-page cache, FULL durability, and all
49 persisted moves. Opening took 1.04 seconds and sync completed at 7.14 seconds
from harness start; the score remained 10–10 with no pending move or SQL CRUD.
All **103 badge tests** pass, including migration, rollback, account clearing,
sync and REMOVE after reopen, pending recovery, and production VFS failure tests.
The [raw-table hardware report](badge/tools/raw-tables-hardware-results.json)
records the timings, migration check, package verification, and limitations.

Temporary install/test helpers remain in `/private/tmp/dots-*`; review them
before reuse, and never print their private session credentials. Do not deploy
to Vercel without approval or push Git.


## Badge performance qualification, 21 October 2026

The current client skips unchanged rendering, caches text measurements and logo
spans, replays only new moves, reads download fragments in 2 KiB chunks with
bounded draining, and avoids repeated JSON decoding/native calls. Pending moves
use a small generation-bound file overlay with the same durability barriers.
The native module, UUID protocol and default sync stream remain unchanged.

On the Tufty, alternating tests measured idle rendering at 17.55 → 0.21 ms,
cursor rendering at 17.78 → 11.05 ms, and queueing plus clearing a pending move
at a median 133.76 → 31.45 ms. These measure separate CPU/file paths, not total
move latency or physical display refresh. A download-only check of the installed
client preserved the saved 11-move game through its schema 4 → 5 migration.

See the [current qualification report](badge/tools/performance-qualification.md)
and [storage experiments](badge/tools/storage-hotpaths.md) for fixtures, device
measurements, reproduction commands, compatibility limits and local backend
experiments. The optional RAM profiler is never imported by the packaged app.
