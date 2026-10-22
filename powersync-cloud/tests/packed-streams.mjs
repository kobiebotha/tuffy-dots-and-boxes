#!/usr/bin/env node
// Local experiment only. No production stream or badge schema is changed.
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import * as sqlite from 'node:sqlite';
import { Game } from '../../web/src/game.ts';

const root = fileURLToPath(new URL('../../', import.meta.url));
const source = process.env.POWERSYNC_SERVICE_SOURCE ?? path.resolve(root, '../../powersync-service');
const { SqlSyncRules, DEFAULT_HYDRATION_STATE, DEFAULT_TAG, nodeSqlite, RequestParameters, RequestJwtPayload } =
  await import(pathToFileURL(path.join(source, 'packages/sync-rules/dist/index.js')));
const encode = value => JSON.stringify(value, (_key, item) => typeof item === 'bigint' ? Number(item) : item);
const uuid64 = id => Buffer.from(id.replaceAll('-', ''), 'hex').toString('base64');
const alphabet = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklm';
const sqlPath = process.argv.find(arg => arg.startsWith('--sql-fixture='))?.slice(14);
const sqlFixture = sqlPath ? JSON.parse(fs.readFileSync(sqlPath, 'utf8')) : null;
const userId = sqlFixture?.userId ?? 'aaaaaaaa-0000-4000-8000-000000000001';
const peerId = sqlFixture?.otherUserId ?? 'bbbbbbbb-0000-4000-8000-000000000002';
const outsiderId = 'eeeeeeee-0000-4000-8000-000000000003';
const roomId = sqlFixture?.roomId ?? '550e8400-e29b-41d4-a716-446655440000';
const otherRoom = '650e8400-e29b-41d4-a716-446655440000';
const roomFilter = "id = subscription.parameter('room_id') AND id IN (SELECT room_id FROM room_players WHERE user_id = auth.user_id())";
const moveFilter = "room_id = subscription.parameter('room_id') AND room_id IN (SELECT room_id FROM room_players WHERE user_id = auth.user_id())";
const roomFields = `base64(uuid_blob(id)) AS id,
  CASE WHEN status = 'waiting' THEN 0 WHEN status = 'playing' THEN 1 WHEN status = 'finished' THEN 2 ELSE 3 END AS s,
  current_seat AS p, score_p1 AS a, score_p2 AS b, winner AS w, move_count AS n`;
const minimalFields = `base64(uuid_blob(id)) AS id,
  CASE WHEN status = 'waiting' THEN 0 WHEN status = 'playing' THEN 1 WHEN status = 'finished' THEN 2 ELSE 3 END AS s,
  move_count AS n`;
const moveFields = `base64(uuid_blob(id)) AS id, base64(uuid_blob(room_id)) AS r,
  CASE WHEN orientation = 'h' THEN row * 5 + col ELSE 25 + row * 6 + col END AS e, move_index AS n`;
const variants = [
  { name: 'current', roomFields, receipt: 'all' },
  { name: 'minimal_room', roomFields: minimalFields, receipt: 'all' },
  { name: 'packed_all', roomFields: `${minimalFields}, edge_history AS h`, receipt: 'all' },
  { name: 'packed_own', roomFields: `${minimalFields}, edge_history AS h`, receipt: 'own' },
  { name: 'packed_dynamic', roomFields: `${minimalFields}, edge_history AS h`, receipt: 'dynamic' },
];
function compile(variant) {
  const queries = [`SELECT ${variant.roomFields} FROM rooms WHERE ${roomFilter}`];
  if (variant.receipt === 'own') {
    for (const seat of [1, 2]) queries.push(`SELECT ${moveFields} FROM moves
      WHERE room_id = subscription.parameter('room_id') AND seat = ${seat}
      AND room_id IN (SELECT room_id FROM room_players WHERE user_id = auth.user_id() AND seat = ${seat})`);
  } else queries.push(`SELECT ${moveFields} FROM moves WHERE ${moveFilter}` +
    (variant.receipt === 'dynamic' ? " AND id = subscription.parameter('pending_move_id')" : ''));
  const yaml = 'config:\n  edition: 3\nstreams:\n  experiment:\n    auto_subscribe: false\n    queries:\n' +
    queries.map(query => '      - |\n' + query.split('\n').map(line => `        ${line}`).join('\n')).join('\n') + '\n';
  const parsed = SqlSyncRules.fromYaml(yaml, { defaultSchema: 'public', throwOnError: false });
  assert.deepEqual(parsed.errors.map(error => error.message), [], variant.name);
  return { yaml, config: parsed.config.hydrate({ hydrationState: DEFAULT_HYDRATION_STATE, sqlite: nodeSqlite(sqlite) }) };
}
const players = [
  { id: 'alice', room_id: roomId, user_id: userId, seat: 1 },
  { id: 'bob', room_id: roomId, user_id: peerId, seat: 2 },
  { id: 'outsider', room_id: otherRoom, user_id: outsiderId, seat: 1 },
];
const game = new Game();
const moves = [];
const roomStates = [];
let history = '';
function snapshot() {
  return { id: roomId, status: game.finished ? 'finished' : 'playing', cols: 5, rows: 4,
    current_seat: game.currentPlayer, score_p1: game.scores[0], score_p2: game.scores[1],
    winner: game.winner, move_count: game.moves, edge_history: history };
}
roomStates.push(snapshot());
for (let edge = 0; edge < 49; edge++) {
  const orientation = edge < 25 ? 'h' : 'v';
  const local = edge < 25 ? edge : edge - 25;
  const width = edge < 25 ? 5 : 6;
  const move = { id: `${(edge + 1).toString(16).padStart(8, '0')}-1234-4123-8123-123456789abc`,
    room_id: roomId, seat: game.currentPlayer, orientation, row: Math.floor(local / width), col: local % width,
    move_index: edge + 1 };
  assert.notEqual(game.claim(orientation, move.row, move.col), null);
  moves.push(move);
  history += alphabet[edge];
  roomStates.push(snapshot());
}
if (sqlFixture) {
  const sql = sqlFixture;
  // The SQL runner exports the same deterministic edge ordering.
  // Compare the actual database materialization against independent JS replay.
  assert.equal(sql.roomStates.length, 50);
  for (let index = 0; index < 50; index++) {
    for (const key of ['edge_history', 'move_count', 'current_seat', 'score_p1', 'score_p2', 'winner'])
      assert.deepEqual(sql.roomStates[index][key], roomStates[index][key], `SQL state ${index}.${key}`);
  }
  assert.equal(sql.moves.length, 49);
  for (let index = 0; index < 49; index++) {
    for (const key of ['seat', 'orientation', 'row', 'col', 'move_index'])
      assert.equal(sql.moves[index][key], moves[index][key], `SQL move ${index}.${key}`);
  }
  // All downstream projections and native rows use the actual SQL results,
  // including random canonical UUIDs, after independent replay comparison.
  roomStates.splice(0, roomStates.length, ...sql.roomStates);
  moves.splice(0, moves.length, ...sql.moves);
}
const tableRef = name => ({ connectionTag: DEFAULT_TAG, schema: 'public', name });
async function resolve(config, count, pendingId = null, actor = userId, requestedRoom = roomId, roster = players) {
  const rows = [
    { table: 'rooms', record: roomStates[count] },
    { table: 'rooms', record: { ...roomStates[count], id: otherRoom } },
    ...moves.slice(0, count).map(record => ({ table: 'moves', record })),
    ...roster.map(record => ({ table: 'room_players', record })),
  ];
  const parameters = { room_id: requestedRoom, pending_move_id: pendingId };
  const { querier, errors } = config.getBucketParameterQuerier({
    globalParameters: new RequestParameters(new RequestJwtPayload({ sub: actor }), {}),
    hasDefaultStreams: false,
    streams: { experiment: [{ opaque_id: 0, priorityOverride: null, parameters }] },
  });
  assert.deepEqual(errors, []);
  const lookups = rows.flatMap(({ table, record }) => config.evaluateParameterRow(tableRef(table), record));
  const dynamic = await querier.queryDynamicBucketDescriptions({ async getParameterSets(requests) {
    return requests.map(lookup => ({ lookup, rows: lookups
      .filter(result => result.lookup.serializedRepresentation === lookup.serializedRepresentation)
      .flatMap(result => result.bucketParameters) }));
  } });
  const buckets = [...new Set([...querier.staticBuckets, ...dynamic].map(bucket => bucket.bucket))];
  const selected = rows.flatMap(({ table, record }) => config.evaluateRow({ sourceTable: tableRef(table), record })
    .filter(result => buckets.includes(result.bucket)).map(result => ({ bucket: result.bucket, table: result.table,
      id: result.id, data: result.data })));
  return { parameters, buckets, rows: selected };
}
function operation(row, index, remove = false) {
  const data = encode(row.data);
  const hash = crypto.createHash('sha256').update(remove ? `delete.${row.table}.${row.id}` : `put.${row.table}.${row.id}.${data}`);
  return { op_id: String(index), op: remove ? 'REMOVE' : 'PUT', object_type: row.table,
    object_id: row.id, checksum: hash.digest().readUInt32LE(0), ...(remove ? {} : { data }) };
}
function changes(previous, current, offset) {
  const key = row => `${row.bucket}\n${row.table}\n${row.id}`;
  const before = new Map(previous.rows.map(row => [key(row), row]));
  const after = new Map(current.rows.map(row => [key(row), row]));
  const ops = [];
  for (const [identity, row] of after) {
    if (!before.has(identity) || encode(before.get(identity).data) !== encode(row.data))
      ops.push({ bucket: row.bucket, operation: operation(row, offset + ops.length + 1) });
  }
  for (const [identity, row] of before) {
    // A vanished bucket is removed by the checkpoint, without per-row REMOVE.
    if (current.buckets.includes(row.bucket) && !after.has(identity))
      ops.push({ bucket: row.bucket, operation: operation(row, offset + ops.length + 1, true) });
  }
  return ops;
}
const fixtures = [];
for (const variant of variants) {
  const { yaml, config } = compile(variant);
  const own = moves.filter(move => move.seat === 1);
  const finalPending = own.at(-1).id;
  const final = await resolve(config, 49, finalPending);
  assert.equal(final.rows.filter(row => row.table === 'rooms').length, 1);
  assert.equal(final.rows.filter(row => row.table === 'moves').length,
    variant.receipt === 'dynamic' ? 1 : variant.receipt === 'own' ? own.length : 49);
  if (variant.receipt === 'own') {
    const actualIds = final.rows.filter(row => row.table === 'moves').map(row => row.id).sort();
    assert.deepEqual(actualIds, own.map(row => uuid64(row.id)).sort());
    const peerRows = (await resolve(config, 49, finalPending, peerId)).rows.filter(row => row.table === 'moves');
    assert.deepEqual(peerRows.map(row => row.id).sort(), moves.filter(row => row.seat === 2).map(row => uuid64(row.id)).sort());
  }
  assert.deepEqual((await resolve(config, 49, finalPending, outsiderId)).rows, []);
  assert.deepEqual((await resolve(config, 49, finalPending, userId, otherRoom)).rows, []);
  assert.deepEqual((await resolve(config, 49, finalPending, userId, roomId, players.filter(row => row.user_id !== userId))).rows, []);
  const previousStatus = roomStates[49].status;
  roomStates[49].status = 'cancelled';
  const archived = await resolve(config, 49, finalPending);
  assert.equal(Number(archived.rows.find(row => row.table === 'rooms').data.s), 3);
  assert.equal(archived.rows.filter(row => row.table === 'moves').length,
    final.rows.filter(row => row.table === 'moves').length);
  roomStates[49].status = previousStatus;
  if (variant.receipt === 'dynamic') {
    assert.equal((await resolve(config, 49, moves[0].id)).rows.filter(row => row.table === 'moves')[0].id, uuid64(moves[0].id));
    assert.equal((await resolve(config, 49, 'ffffffff-ffff-4fff-8fff-ffffffffffff')).rows.filter(row => row.table === 'moves').length, 0);
    assert.equal((await resolve(config, 49, finalPending, outsiderId, otherRoom)).rows.filter(row => row.table === 'moves').length, 0);
  }
  const makeScenario = async (live, separateSubscriptionCheckpoint = false) => {
    let previous = { rows: [], buckets: [] }, offset = 0, pending = null;
    const checkpoints = [];
    async function append(count, subscription = false) {
      const current = await resolve(config, count, pending);
      const operations = changes(previous, current, offset);
      offset += operations.length;
      checkpoints.push({ ...current, operations, last_op_id: String(offset), subscription,
        expectedMoveCount: count, expectedHistory: roomStates[count].edge_history });
      previous = current;
    }
    if (!live) { pending = finalPending; await append(49); }
    else {
      await append(0);
      for (let count = 1; count <= 49; count++) {
        const subscription = variant.receipt === 'dynamic' && moves[count - 1].seat === 1;
        if (subscription) {
          pending = moves[count - 1].id;
          if (separateSubscriptionCheckpoint) await append(count - 1, true);
        }
        await append(count, subscription && !separateSubscriptionCheckpoint);
      }
    }
    return { checkpoints, subscriptionChanges: checkpoints.filter(record => record.subscription).length,
      operationBytes: checkpoints.reduce((sum, record) => sum + record.operations.reduce((bytes, op) => bytes + Buffer.byteLength(encode(op.operation)), 0), 0),
      payloadBytes: checkpoints.reduce((sum, record) => sum + record.operations.reduce((bytes, op) => bytes + (op.operation.data ? Buffer.byteLength(op.operation.data) : 0), 0), 0) };
  };
  let recovery = null;
  if (variant.receipt === 'dynamic') {
    const state = await resolve(config, 49, moves[0].id);
    const operations = changes({ rows: [], buckets: [] }, state, 0);
    recovery = { ...state, operations, last_op_id: String(operations.length),
      expectedMoveCount: 49, expectedHistory: history };
  }
  fixtures.push({ ...variant, yaml, ownMoveCount: own.length, finalPending, recovery,
    roomFields: undefined, scenarios: { fresh: await makeScenario(false), live: await makeScenario(true),
      ...(variant.receipt === 'dynamic' ? { live_separate_subscription: await makeScenario(true, true) } : {}) } });
}
const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'dots-packed-'));
const fixturePath = path.join(directory, 'fixtures.json');
fs.writeFileSync(fixturePath, encode({ fixtures, roomId, userId, peerId, moves, roomStates, alphabet }));
const native = process.argv.includes('--service-only') ? null : JSON.parse(execFileSync(process.env.PYTHON ?? 'python3',
  [path.join(root, 'badge/tools/qualify_packed_projection.py'), fixturePath], { encoding: 'utf8', maxBuffer: 16 * 1024 * 1024 }));
const report = { scope: 'Local synthetic service compiler/bucket evaluator and pinned native core. No production protocol change or cloud write.',
  fixturePath, sqlFixtureCompared: sqlPath ?? null,
  sqlFixtureSha256: sqlPath ? crypto.createHash('sha256').update(fs.readFileSync(sqlPath)).digest('hex') : null,
  independentReferenceComparisons: sqlPath ? { roomStates: 50, canonicalMoves: 49 } : null,
  caveats: ['Operation bytes exclude HTTP/TLS/checkpoint framing and native subscription control responses.',
    'Dynamic receipts require a new subscription per own move; both coalesced and separately published subscription checkpoints are measured.',
    'Persisted native subscription commands are counted; transport reconnect/request costs are excluded, so these schedules are not a latency upper bound.',
    'Room scalar reduction requires an explicit replay adapter; the installed Store.room/Client contract is unchanged.',
    'Native POSIX results establish semantics and commit counts, not Tufty radio/flash latency.'],
  variants: fixtures.map(fixture => ({ name: fixture.name, receipt: fixture.receipt, ownMoveCount: fixture.ownMoveCount,
    scenarios: Object.fromEntries(Object.entries(fixture.scenarios).map(([name, value]) => [name,
      { checkpoints: value.checkpoints.length, subscriptionChanges: value.subscriptionChanges,
        payloadBytes: value.payloadBytes, operationBytes: value.operationBytes }])) })), native };
const output = process.argv.find(arg => arg.startsWith('--output='))?.slice(9) ?? path.join(directory, 'report.json');
fs.writeFileSync(output, encode(report) + '\n');
console.log(encode(report));
console.log(`RESULT ${output}`);
