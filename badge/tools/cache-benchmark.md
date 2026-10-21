# Compact badge cache benchmark

Production now uses a 32-page cache after startup and migration complete. Initialization, cache clearing, and schema replacement still use an eight-page cache. FULL synchronous DELETE journaling remains enabled.

All 20 benchmark scenarios passed in the pinned 256 KiB SQLite/Rust arena, including reopening and verifying all 49 moves with zero SQL CRUD. Three existing real-core migration tests also passed, with cache clearing and schema replacement checked at eight pages and the cache raised to 32 pages afterward. The v1 fixture represents the old schema with selected legacy fields; creating the original full schema exceeds the host fixture's 256 KiB creation budget.

| Cache pages | Live reads | Live writes | Live syncs | Worst peak bytes |
| ---: | ---: | ---: | ---: | ---: |
| 8 | 2178 | 3354 | 414 | 195840 |
| 16 | 1426 | 2999 | 214 | 195840 |
| 24 | 1291 | 2931 | 150 | 195840 |
| 32 | 620 | 2923 | 150 | 208128 |
| 48 | 215 | 2955 | 150 | 240832 |

The 32-page setting reduces live reads by 71.5% and sync calls by 63.8% compared with eight pages. Its worst measured peak is 208128 bytes, leaving 54016 bytes of arena headroom. The 48-page candidate peaks at 240832 bytes, leaving only 21312 bytes, so it is not recommended. With UI queries included, reads fell from 2769 to 1006; sync counts remained at 414 versus 150.

At the representative checkpoint from move 4 to move 5, the eight-page cache performed 31 reads, 58 writes, and 7 syncs. The 32-page cache performed 4 reads, 50 writes, and 3 syncs in the first frozen run. Its four reads totalled 56 bytes of journal and header metadata, with no database-page reads. Across 50 live checkpoints, the minimum, median, and maximum sync counts were 5/9/11 at eight pages and 3/3/3 at 32 pages. Small write-count variations between runs arise from timestamp and journal state; the measured read and sync reductions were stable.

Cold history before oplog compaction, comprising 99 operations including 50 room versions, passed at 32 pages with 98 reads, 665 writes, and 51 syncs. At eight pages, those counts were 923 reads, 1072 writes, and 119 syncs. A fresh full board passed at 32 pages with 66 reads, 356 writes, and 27 syncs, compared with 528 reads, 561 writes, and 57 syncs at eight pages. Reopen queries at every cache size performed zero writes and zero syncs.

These measurements count actual VFS calls from the real pinned core through a POSIX wrapper configured with the badge's sector size of 512, device capabilities of 0, I/O methods version 1, and no file-control shortcuts. Creation and migration use eight cache pages, then the candidate setting is applied before streaming. More than three FULL sync calls per transaction at eight pages is consistent with rollback-journal cache spilling; the larger cache reduces these durable spill cycles.

The benchmark counts native file I/O. It does not measure Python VFS callback latency, LittleFS timing, total MicroPython heap, or Wi-Fi. The production database shadow is a separate optimization; its effect is not included in these POSIX cache measurements.

Reproduce without service or hardware mutations:

```sh
node powersync-cloud/tests/benchmark-projection.mjs --service-only --output=/tmp/dots-projection.json
python3 badge/tools/benchmark_cache.py /tmp/dots-projection.json --expanded --output=/tmp/dots-cache-v2.json
```

The script uses the prebuilt pinned fixture library from the sibling Badgegotchi project and compiles only a small host VFS counter. Results are versioned in cache-benchmark-results-v1.json and cache-benchmark-results-v2.json; the earlier projection baseline is unchanged.
