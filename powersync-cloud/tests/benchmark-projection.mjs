#!/usr/bin/env node
// Read-only, synthetic fixture benchmark. Compiles real service projections and
// optionally sends their exact JSON to the pinned 256 KiB native qualifier.
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
const { SqlSyncRules, DEFAULT_HYDRATION_STATE, DEFAULT_TAG, nodeSqlite } =
  await import(pathToFileURL(path.join(source, 'packages/sync-rules/dist/index.js')));
const encode = value => JSON.stringify(value, (_key, item) => typeof item === 'bigint' ? Number(item) : item);
const bytes = value => Buffer.byteLength(typeof value === 'string' ? value : encode(value));
const roomId = '550e8400-e29b-41d4-a716-446655440000';
const userId = 'aaaaaaaa-0000-4000-8000-000000000001';
const alphabet = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklm';
assert.equal(alphabet.length, 49);
const currentRoom = 'id, status, cols, rows, current_seat, score_p1, score_p2, winner, move_count, player_count, paired_count';
const compactRoom = `base64(uuid_blob(id)) AS id,
  CASE WHEN status = 'waiting' THEN 0 WHEN status = 'playing' THEN 1 WHEN status = 'finished' THEN 2 ELSE 3 END AS s,
  current_seat AS p, score_p1 AS a, score_p2 AS b, winner AS w, move_count AS n`;
const currentMove = 'id, room_id, seat, orientation, row, col, move_index';
const encodedEdge = "CASE WHEN orientation = 'h' THEN row * 5 + col ELSE 25 + row * 6 + col END";
const compactMove = `base64(uuid_blob(id)) AS id, ${encodedEdge} AS e, move_index AS n`;
const variants = [
  { name: 'audit_move_created_at', room: currentRoom, move: `${currentMove}, created_at` },
  { name: 'current_badge', room: currentRoom, move: currentMove },
  { name: 'base64_move_id_only', room: currentRoom, move: `base64(uuid_blob(id)) AS id, room_id, seat, orientation, row, col, move_index` },
  { name: 'v2_safe_r', room: compactRoom, move: `${compactMove}, base64(uuid_blob(room_id)) AS r` },
  { name: 'v2_minimal', room: compactRoom, move: compactMove },
  { name: 'v2_raw_moves', room: compactRoom, move: `${compactMove}, base64(uuid_blob(room_id)) AS r`, raw: ['moves'] },
  { name: 'v2_raw_all', room: compactRoom, move: `${compactMove}, base64(uuid_blob(room_id)) AS r`, raw: ['rooms', 'moves'] },
  { name: 'packed_history', room: `${compactRoom}, edge_history AS h`, hypotheticalBackend: true },
  { name: 'packed_bitsets', room: `${compactRoom}, edges_p1 AS u, edges_p2 AS v, box_owners AS x`, hypotheticalBackend: true },
];
function compile(room, move) {
  const queries = [
    `SELECT ${room} FROM rooms WHERE id = subscription.parameter('room_id') AND id IN (SELECT room_id FROM room_players WHERE user_id = auth.user_id())`,
    ...(move ? [`SELECT ${move} FROM moves WHERE room_id = subscription.parameter('room_id') AND room_id IN (SELECT room_id FROM room_players WHERE user_id = auth.user_id())`] : []),
  ];
  const yaml = 'config:\n  edition: 3\nstreams:\n  benchmark:\n    queries:\n' +
    queries.map(query => '      - |\n' + query.split('\n').map(line => `        ${line}`).join('\n')).join('\n') + '\n';
  const parsed = SqlSyncRules.fromYaml(yaml, { defaultSchema: 'public', throwOnError: false });
  assert.deepEqual(parsed.errors.map(error => error.message), []);
  const config = parsed.config.hydrate({ hydrationState: DEFAULT_HYDRATION_STATE, sqlite: nodeSqlite(sqlite) });
  return { yaml, project(table, record) {
    const got = config.evaluateRow({ sourceTable: { connectionTag: DEFAULT_TAG, schema: 'public', name: table }, record });
    assert.equal(got.length, 1);
    return { table: got[0].table, id: got[0].id, data: got[0].data };
  } };
}
const game = new Game();
const moves = [];
const roomStates = [];
let history = '';
function snapshot() {
  let p1 = 0n, p2 = 0n, boxes = 0n;
  [...game.horizontal, ...game.vertical].forEach((owner, index) => {
    if (owner === 1) p1 |= 1n << BigInt(index);
    if (owner === 2) p2 |= 1n << BigInt(index);
  });
  game.boxes.forEach((owner, index) => { boxes |= BigInt(owner) << BigInt(index * 2); });
  return { id: roomId, status: game.finished ? 'finished' : game.moves ? 'playing' : 'waiting',
    cols: 5n, rows: 4n, current_seat: BigInt(game.currentPlayer), score_p1: BigInt(game.scores[0]),
    score_p2: BigInt(game.scores[1]), winner: game.winner === null ? null : BigInt(game.winner),
    move_count: BigInt(game.moves), player_count: 2n, paired_count: 2n,
    edge_history: history, edges_p1: p1, edges_p2: p2, box_owners: boxes };
}
roomStates.push(snapshot());
for (let edge = 0; edge < 49; edge++) {
  const orientation = edge < 25 ? 'h' : 'v';
  const index = edge < 25 ? edge : edge - 25;
  const width = orientation === 'h' ? 5 : 6;
  const row = Math.floor(index / width), col = index % width;
  const move = { id: `${(edge + 1).toString(16).padStart(8, '0')}-1234-4123-8123-123456789abc`,
    room_id: roomId, seat: BigInt(game.currentPlayer), orientation, row: BigInt(row), col: BigInt(col),
    move_index: BigInt(edge + 1), created_at: `2026-10-20T12:00:${String(edge).padStart(2, '0')}.000Z` };
  assert.notEqual(game.claim(orientation, row, col), null);
  history += alphabet[edge];
  moves.push(move);
  roomStates.push(snapshot());
}
// Lossless history and the edge scalar both replay exactly, including boxes.
const replay = new Game();
for (const char of history) {
  const edge = alphabet.indexOf(char), index = edge < 25 ? edge : edge - 25;
  replay.claim(edge < 25 ? 'h' : 'v', Math.floor(index / (edge < 25 ? 5 : 6)), index % (edge < 25 ? 5 : 6));
}
assert.deepEqual(replay, game);
for (const move of moves) {
  const roundTrip = Buffer.from(Buffer.from(move.id.replaceAll('-', ''), 'hex').toString('base64'), 'base64').toString('hex');
  assert.equal(roundTrip, move.id.replaceAll('-', ''));
}
function operation(row, index) {
  const data = encode(row.data);
  // Exact hashData() algorithm from service-core/src/util/utils.ts.
  const checksum = crypto.createHash('sha256').update(`put.${row.table}.${row.id}.${data}`).digest().readUInt32LE(0);
  return { op_id: String(index), op: 'PUT', object_type: row.table, object_id: row.id, checksum, data };
}
function scenario(rows, groups) {
  const ops = rows.map((row, index) => operation(row, index + 1));
  return { ops, groups, payloadBytes: rows.reduce((sum, row) => sum + bytes(row.data), 0),
    operationBytes: ops.reduce((sum, op) => sum + bytes(op), 0), rowCount: rows.length };
}
const fixtures = [];
const summaries = [];
for (const variant of variants) {
  const projection = compile(variant.room, variant.move);
  const roomRows = roomStates.map(row => projection.project('rooms', row));
  const moveRows = variant.move ? moves.map(row => projection.project('moves', row)) : [];
  if (variant.name.startsWith('v2_')) {
    assert.equal(roomRows.at(-1).id.length, 24);
    const compactReplay = new Game();
    for (let index = 0; index < moveRows.length; index++) {
      const row = moveRows[index];
      assert.equal(row.id.length, 24);
      assert.equal(Buffer.from(row.id, 'base64').toString('hex'), moves[index].id.replaceAll('-', ''));
      assert.equal(row.data.n, BigInt(index + 1));
      if ('r' in row.data) {
        assert.equal(row.data.r, roomRows.at(-1).id);
        assert.equal(Buffer.from(row.data.r, 'base64').toString('hex'), roomId.replaceAll('-', ''));
      }
      const edge = Number(row.data.e), local = edge < 25 ? edge : edge - 25;
      compactReplay.claim(edge < 25 ? 'h' : 'v', Math.floor(local / (edge < 25 ? 5 : 6)), local % (edge < 25 ? 5 : 6));
    }
    assert.deepEqual(compactReplay, game);
    // A second room uses distinct canonical move UUIDs. Encoded IDs remain
    // globally distinct, independently of the single-room subscription rule.
    const secondRoom = '650e8400-e29b-41d4-a716-446655440000';
    const oldIds = new Set(moveRows.map(row => row.id));
    for (const move of moves) {
      const replacement = { ...move, room_id: secondRoom, id: '1' + move.id.slice(1) };
      const row = projection.project('moves', replacement);
      assert.equal(oldIds.has(row.id), false);
      if ('r' in row.data) assert.equal(Buffer.from(row.data.r, 'base64').toString('hex'), secondRoom.replaceAll('-', ''));
    }
  }
  const freshRows = [...moveRows, roomRows.at(-1)];
  const historyRows = [roomRows[0]];
  const liveGroups = [1];
  for (let index = 0; index < 49; index++) {
    if (variant.move) historyRows.push(moveRows[index]);
    historyRows.push(roomRows[index + 1]);
    liveGroups.push(variant.move ? 2 : 1);
  }
  const sampleRows = [roomRows.at(-1), ...moveRows.slice(0, 1)];
  const tables = [...new Set(sampleRows.map(row => row.table))].map(name => {
    const row = sampleRows.find(row => row.table === name);
    return { name, columns: Object.entries(row.data).filter(([key]) => key !== 'id').map(([name, value]) => ({
      name, type: typeof value === 'string' ? 'text' : 'integer',
    })) };
  });
  // The installed schema omits the roster counts even though the current
  // service projection includes them in the raw JSON. Preserve that baseline.
  if (['audit_move_created_at', 'current_badge', 'base64_move_id_only'].includes(variant.name)) {
    tables.find(table => table.name === 'rooms').columns = tables.find(table => table.name === 'rooms').columns
      .filter(column => !['player_count', 'paired_count'].includes(column.name));
  }
  const schema = { tables: [{ name: 'account', local_only: true, columns: [{ name: 'user_id', type: 'text' }] },
    ...tables.filter(table => !variant.raw?.includes(table.name))],
    raw_tables: tables.filter(table => variant.raw?.includes(table.name)).map(table => ({ name: table.name, table_name: table.name })) };
  const rawSql = tables.filter(table => variant.raw?.includes(table.name)).map(table =>
    `CREATE TABLE "${table.name}" (id TEXT PRIMARY KEY NOT NULL, ${table.columns.map(column => `"${column.name}" ${column.type.toUpperCase()}`).join(', ')})`);
  const scenarios = { fresh: scenario(freshRows, [freshRows.length]),
    history_cold: scenario(historyRows, [historyRows.length]), history_live: scenario(historyRows, liveGroups) };
  for (const row of sampleRows) assert.equal(typeof row.id, 'string');
  fixtures.push({ ...variant, schema, rawSql, scenarios, expectedRows: moveRows.length + 1,
    expectedRoom: encode(roomRows.at(-1).data), expectedMove: moveRows[0] ? encode(moveRows[0].data) : null });
  summaries.push({ name: variant.name, hypotheticalBackend: !!variant.hypotheticalBackend,
    raw: variant.raw ?? [], fresh: { ...scenarios.fresh, ops: undefined, groups: undefined },
    history: { ...scenarios.history_cold, ops: undefined, groups: undefined },
    roomSample: roomRows.at(-1).data, moveSample: moveRows[0]?.data ?? null });
}
// Show why direct BLOB ids are unsafe and why int2 cannot shrink numeric JSON.
const probe = query => {
  const parsed = SqlSyncRules.fromYaml(`config:\n  edition: 3\nstreams:\n  probe:\n    query: SELECT ${query} FROM moves\n`, { defaultSchema: 'public', throwOnError: false });
  assert.deepEqual(parsed.errors.map(error => error.message), []);
  const config = parsed.config.hydrate({ hydrationState: DEFAULT_HYDRATION_STATE, sqlite: nodeSqlite(sqlite) });
  return config.evaluateRow({ sourceTable: { connectionTag: DEFAULT_TAG, schema: 'public', name: 'moves' }, record: moves[0] });
};
const semantics = {
  rawBlobId: probe('uuid_blob(id) AS id')[0],
  rawBlobColumn: probe('id, uuid_blob(room_id) AS r')[0],
  base64Id: probe('base64(uuid_blob(id)) AS id')[0],
  numericPayloadsIdentical: encode({ e: 7n, n: 49n }) === encode({ e: 7, n: 49 }),
};
assert.equal(semantics.rawBlobId.id, '');
assert.deepEqual(semantics.rawBlobId.data, {});
assert.equal('r' in semantics.rawBlobColumn.data, false);
const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'dots-projection-'));
const fixturePath = path.join(directory, 'fixtures.json');
fs.writeFileSync(fixturePath, encode({ fixtures, roomId, userId }));
let native = null;
if (!process.argv.includes('--service-only')) {
  native = JSON.parse(execFileSync(process.env.PYTHON ?? 'python3', [path.join(root, 'badge/tools/benchmark_native.py'), fixturePath],
    { encoding: 'utf8', maxBuffer: 16 * 1024 * 1024 }));
}
const result = { methodology: { scope: 'Local actual service compiler and fixed 256 KiB real native core. Synthetic game; no cloud writes or Tufty timing claims.',
  reproduce: 'node powersync-cloud/tests/benchmark-projection.mjs --output=/private/tmp/dots-projection-benchmark.json',
  guarantees: ['Actual compiler checks v2 CASE and base64 output.',
    'All 49 transformed moves replay exactly, including turn, scores, edge owners and boxes.',
    'Base64 UUID outputs are padded 24-character TEXT and round-trip to canonical UUID bytes.',
    'Distinct canonical UUIDs across two rooms produce disjoint move IDs; r decodes to its correct room.',
    'Native scenarios assert expected published rows, empty SQL CRUD and completed checkpoints.'],
  operationBytes: 'Exact compact JSON PUT envelope bytes with actual service SHA256 checksum algorithm; excludes HTTP/TLS, bucket envelopes, checkpoint lines and replication metadata.',
  history: 'One initial room row plus each accepted move and resulting room update, before service oplog compaction. Native cold and live checkpoints reported separately.',
  packed: 'Packed variants require a backend materialized column/projection; values are derived from actual Game replay, not supported cross-row aggregation inside streams.',
  native: 'POSIX VFS with the qualified 256 KiB SQLite arena and 32-row wrapper limit; not device LittleFS latency, firmware RSSI or total MicroPython heap.',
  fixtures: fixturePath }, semantics, projections: summaries, native,
  sources: { powersyncFunctions: 'https://docs.powersync.com/sync/rules/supported-sql',
    sqliteUuidExtension: 'https://github.com/sqlite/sqlite/blob/master/ext/misc/uuid.c',
    postgresNumeric: 'https://www.postgresql.org/docs/current/datatype-numeric.html',
    sqliteRecordFormat: 'https://sqlite.org/fileformat2.html#record_format',
    powersyncRawTables: 'https://docs.powersync.com/client-sdks/advanced/raw-tables' } };
const output = process.argv.find(arg => arg.startsWith('--output='))?.slice(9) ?? path.join(directory, 'results.json');
fs.writeFileSync(output, encode(result) + '\n');
for (const row of summaries) console.log(`${row.name.padEnd(23)} fresh payload ${String(row.fresh.payloadBytes).padStart(5)}B / PUT ${String(row.fresh.operationBytes).padStart(5)}B; history payload ${String(row.history.payloadBytes).padStart(5)}B / PUT ${String(row.history.operationBytes).padStart(5)}B`);
if (native) for (const row of native.results) console.log(`${row.variant.padEnd(23)} ${row.scenario.padEnd(12)} ${row.error ?? `${row.databaseBytes}B DB, peak ${row.syncPeakBytes}B, ${row.transactions} commits`}`);
console.log(`RESULT ${output}`);
