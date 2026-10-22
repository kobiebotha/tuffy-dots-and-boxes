# Packed board stream experiment

This is a local experiment. The installed badge still uses `badge_game_v2`, its
six room values, and all immutable move rows. No experimental stream, schema,
adapter, or backend migration has been deployed.

The proposed server materialization, `rooms.edge_history`, records the accepted
edge sequence as at most 49 ASCII symbols from
`0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklm`. Replaying it recovers
turns, scores, boxes, and winner. The experimental room projection is `{id,s,n,h}`;
the existing move/receipt shape remains `{id,r,e,n}` with compact UUIDs and an
explicit room binding. The standalone native qualifier supplies the replay
adapter; the production `Store.room` and `Client` contracts are unchanged.

## Reproduction

Use Node 25+ and the built sibling PowerSync service checkout, as with the
existing projection benchmark. Set `POWERSYNC_SERVICE_SOURCE` if it is elsewhere.
The SQL runner uses an existing PGlite runtime; set `PGLITE_MODULE` to its
`dist/index.js` when the default scratch runtime is unavailable.

```sh
node supabase/tests/packed-history.mjs --output=/private/tmp/dots-packed-history-fixture.json
node powersync-cloud/tests/packed-streams.mjs --sql-fixture=/private/tmp/dots-packed-history-fixture.json --output=badge/tools/packed-streams-results.json
```

For exact reproduction of the saved byte totals, use the retained synthetic SQL
output `badge/tools/packed-streams-sql-fixture.json` as `--sql-fixture` instead.
`packed-streams-results.json` records that SQL fixture's SHA-256. The
generated canonical UUIDs vary between SQL runs, so decimal checksum widths can
change operation byte totals slightly. The policy compiler, replay assertions,
row counts, and commit counts are deterministic. `--service-only` skips the
native qualifier; omitting `--sql-fixture` uses independently generated rows.

## Results

The saved report uses real PGlite migration/RPC output: all 50 room states and
49 immutable moves first match independent JavaScript replay, then their actual
SQL rows and UUIDs pass through the real PowerSync compiler and pinned native
core. Every adapter reconstructs the same complete board at each publication.

| Projection | Fresh PUT bytes | Fresh FULL commits | Live PUT bytes | Live FULL commits |
| --- | ---: | ---: | ---: | ---: |
| Current six room values + all moves | 10,360 | 9 | 20,489 | 50 |
| Room `{id,s,n}` + all moves | 10,326 | 9 | 18,731 | 50 |
| Packed history + all receipts | 10,386 | 9 | 20,508 | 50 |
| Packed history + own-seat receipts | 5,407 | 5 | 15,529 | 50 |
| Packed history + requested UUID receipt | 438 | 1 | 15,529 | 75–100 modeled |

These are complete PUT operation envelopes, including actual service checksums
and JSON escaping. They exclude HTTP/TLS, bucket/checkpoint framing and
subscription response traffic. Native measurements use temporary POSIX
databases and the fixed 256 KiB arena; they establish semantics and commit
counts, not badge Wi-Fi or LittleFS latency.

The live game has 50 canonical publications (initial board plus 49 moves) and
25 moves by the measured seat. A dynamic UUID receipt requires 25 subscription
changes. Persisting each unsubscribe/subscribe group adds one FULL commit:
`50 + 25 = 75`. If each subscription change also receives a separate checkpoint
before its move is accepted, the measured schedule adds another 25:
`50 + 25 + 25 = 100`. Transport reconnect costs are excluded; these two schedules
are not an upper bound on real client work.

Own-seat receipts retain 25 rows in this game without per-move subscription
changes. Their live database is 45,056 bytes, versus 54,272 for the current
projection and 35,840 for a dynamic receipt. Packed history with all receipts
adds redundant bytes. Reducing only room scalars produces modest savings while
keeping the existing move history and UUID proof.

## Receipt correctness and rollout decision

A packed history proves an edge sequence, not the UUID of a saved upload. Keep
an exact immutable receipt, verify its UUID and room, and require publication
with a matching history/index at a full checkpoint before clearing pending
state. A latest-move or latest-per-seat UUID is insufficient: after a reboot,
an older accepted UUID can still be pending while later moves already exist.

The compiler tests cover signed-user membership, malicious room/pending
parameters, own-seat isolation, terminal archived state, and membership removal.
The actual native core verifies full versus partial completion, exact UUID and
room, an older accepted UUID after database reopen, receipt bucket retirement,
row REMOVE publication, membership revocation, and account reset clearing both
the board and its receipt. The SQL runner separately qualifies incremental
updates, legacy replay equivalence, backfill and rollback, invalid moves,
idempotency, archive behavior, and function permissions.

Keep dynamic receipts experimental: their smallest fresh transfer introduces
additional durable work during play. Packed history with own-seat receipts is
the stronger next candidate for a device experiment, with its backend migration
and new badge adapter/stream still requiring an explicit rollout decision.
