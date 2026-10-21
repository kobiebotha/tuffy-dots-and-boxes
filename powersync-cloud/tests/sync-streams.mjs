#!/usr/bin/env node
// Compile the real policy and resolve its buckets, including membership lookups.
// Uses the PowerSync service checkout shared with Badgegotchi qualification.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import * as sqlite from 'node:sqlite';

const source = process.env.POWERSYNC_SERVICE_SOURCE ?? fileURLToPath(new URL('../../../../powersync-service', import.meta.url));
const { SqlSyncRules, DEFAULT_HYDRATION_STATE, DEFAULT_TAG, nodeSqlite, RequestParameters, RequestJwtPayload } =
  await import(pathToFileURL(path.join(source, 'packages/sync-rules/dist/index.js')));
const parsed = SqlSyncRules.fromYaml(fs.readFileSync(new URL('../sync-config.yaml', import.meta.url), 'utf8'), {
  defaultSchema: 'public', throwOnError: false,
});
assert.deepEqual(parsed.errors.map(error => error.message), []);
const config = parsed.config.hydrate({ hydrationState: DEFAULT_HYDRATION_STATE, sqlite: nodeSqlite(sqlite) });
const alice = 'aaaaaaaa-0000-4000-8000-000000000001';
const bob = 'bbbbbbbb-0000-4000-8000-000000000002';
const eve = 'eeeeeeee-0000-4000-8000-000000000003';
const live = '11111111-0000-4000-8000-000000000001';
const old = '22222222-0000-4000-8000-000000000002';
const other = '33333333-0000-4000-8000-000000000003';
const rows = [];
function add(table, record) { rows.push({ table, record }); }
function tableRef(name) { return { connectionTag: DEFAULT_TAG, schema: 'public', name }; }
function room(id, status = 'playing') {
  return { id, status, host_user_id: alice, cols: 5, rows: 4, current_seat: 1,
    score_p1: 0, score_p2: 0, winner: null, move_count: 0, player_count: 2, paired_count: 2,
    created_at: '2026-10-20T00:00:00Z', started_at: null, finished_at: null };
}
function player(id, room_id, user_id, seat, pairing_code) {
  return { id, room_id, user_id, seat, pairing_code, device_id: null, paired_at: null, created_at: '2026-10-20T00:00:00Z' };
}
add('rooms', room(live));
add('rooms', room(old, 'finished'));
add('rooms', room(other));
add('room_players', player('alice-live', live, alice, 1, 'AAAAA'));
add('room_players', player('bob-live', live, bob, 2, 'BBBBB'));
add('room_players', player('alice-old', old, alice, 1, null));
add('room_players', player('eve-other', other, eve, 1, 'CCCCC'));
for (const room_id of [live, old, other]) {
  let index = 0;
  for (const orientation of ['h', 'v']) {
    for (let row = 0; row < (orientation === 'h' ? 5 : 4); row++) {
      for (let col = 0; col < (orientation === 'h' ? 5 : 6); col++) {
        index++;
        add('moves', { id: `${room_id.slice(0, 24)}${index.toString(16).padStart(12, '0')}`, room_id, seat: index % 2 + 1,
          orientation, row, col, move_index: index, created_at: '2026-10-20T00:00:00Z' });
      }
    }
  }
  assert.equal(index, 49);
}

async function visible(user, { defaults = false, stream, roomId } = {}) {
  const streams = stream ? { [stream]: [{ opaque_id: 0, priorityOverride: null, parameters: { room_id: roomId } }] } : {};
  const { querier, errors } = config.getBucketParameterQuerier({
    globalParameters: new RequestParameters(new RequestJwtPayload({ sub: user }), {}),
    hasDefaultStreams: defaults, streams,
  });
  assert.deepEqual(errors, []);
  const parameters = rows.flatMap(({ table, record }) => config.evaluateParameterRow(tableRef(table), record));
  const dynamic = await querier.queryDynamicBucketDescriptions({
    async getParameterSets(lookups) {
      return lookups.map(lookup => ({ lookup, rows: parameters
        .filter(result => result.lookup.serializedRepresentation === lookup.serializedRepresentation)
        .flatMap(result => result.bucketParameters) }));
    },
  });
  const buckets = new Set([...querier.staticBuckets, ...dynamic].map(bucket => bucket.bucket));
  return rows.flatMap(({ table, record }) => config.evaluateRow({ sourceTable: tableRef(table), record })
    .filter(result => buckets.has(result.bucket)).map(result => ({ table: result.table, ...result.data })));
}
let passed = 0;
async function check(name, test) { await test(); passed++; console.log(`PASS ${name}`); }
const badge = roomId => ({ stream: 'badge_game', roomId });
const compact = roomId => ({ stream: 'badge_game_v2', roomId });
const uuid64 = id => Buffer.from(id.replaceAll('-', ''), 'hex').toString('base64');
const watch = roomId => ({ stream: 'watch_room', roomId });

await check('badge defaults are disabled until an explicit board subscription', async () => {
  assert.deepEqual(await visible(alice), []);
});
await check('badge receives one compact board and at most 49 moves', async () => {
  const got = await visible(alice, badge(live));
  assert.equal(got.length, 50);
  assert.equal(got.filter(row => row.table === 'rooms').length, 1);
  assert.equal(got.filter(row => row.table === 'moves').length, 49);
  assert.ok(got.every(row => row.table === 'rooms' ? row.id === live : row.room_id === live));
  assert.ok(got.every(row => !('pairing_code' in row) && !('device_id' in row) && !('created_at' in row)));
});
await check('both members receive the same board', async () => {
  assert.deepEqual(await visible(bob, badge(live)), await visible(alice, badge(live)));
});
await check('client room parameters cannot grant a nonmember board access', async () => {
  assert.deepEqual(await visible(eve, badge(live)), []);
  assert.deepEqual(await visible(alice, badge(other)), []);
});
await check('archival releases badge bindings while preserving terminal state and canonical moves for both badges', async () => {
  const record = rows.find(row => row.table === 'rooms' && row.record.id === live).record;
  const members = rows.filter(row => row.table === 'room_players' && row.record.room_id === live).map(row => row.record);
  const originalRoom = { ...record };
  const originalMembers = members.map(member => ({ ...member }));
  const canonical = rows.filter(row => row.table === 'moves' && row.record.room_id === live).map(row => row.record);
  const canonicalIds = canonical.map(move => move.id).sort();
  const compactIds = canonical.map(move => uuid64(move.id)).sort();
  try {
    Object.assign(record, { status: 'cancelled', paired_count: 0, move_count: 49,
      finished_at: '2026-10-21T00:00:00Z' });
    for (const member of members) {
      Object.assign(member, { pairing_code: null, device_id: null, paired_at: null });
    }
    assert.deepEqual(members.map(member => member.user_id).sort(), [alice, bob].sort());
    for (const user of [alice, bob]) {
      const legacy = await visible(user, badge(live));
      const compacted = await visible(user, compact(live));
      assert.equal(legacy.length, 50);
      assert.equal(compacted.length, 50);
      assert.equal(legacy.find(row => row.table === 'rooms').status, 'cancelled');
      assert.equal(compacted.find(row => row.table === 'rooms').s, 3n);
      assert.equal(legacy.find(row => row.table === 'rooms').move_count, 49);
      assert.equal(compacted.find(row => row.table === 'rooms').n, 49);
      const moves = legacy.filter(row => row.table === 'moves');
      const compactMoves = compacted.filter(row => row.table === 'moves');
      // An accepted move can arrive with the archive checkpoint. Preserve every
      // canonical UUID so a badge can resolve its pending move before leaving.
      assert.deepEqual(moves.map(move => move.id).sort(), canonicalIds);
      assert.deepEqual(compactMoves.map(move => move.id).sort(), compactIds);
      for (const original of canonical) {
        assert.equal(moves.find(move => move.id === original.id).move_index, original.move_index);
        assert.equal(compactMoves.find(move => move.id === uuid64(original.id)).n, original.move_index);
      }
      assert.ok(legacy.every(row => row.table !== 'room_players'));
      assert.ok(compacted.every(row => row.table !== 'room_players'));
    }
    assert.deepEqual(await visible(eve, badge(live)), []);
    assert.deepEqual(await visible(eve, compact(live)), []);
    for (const user of [alice, bob, eve]) {
      assert.ok((await visible(user, { defaults: true })).every(row => row.table !== 'rooms' || row.id !== live));
      assert.ok((await visible(user, watch(live))).every(row => row.table !== 'rooms' || row.id !== live));
    }
    // The retained private roster has no usable code or binding in browser
    // projections either; membership alone authorizes the terminal badge data.
    for (const user of [alice, bob]) {
      const membership = (await visible(user, { defaults: true }))
        .find(row => row.table === 'room_players' && row.room_id === live);
      assert.ok(membership);
      assert.equal(membership.pairing_code, null);
      assert.equal(membership.device_id, null);
      assert.equal(membership.paired_at, null);
    }
  } finally {
    Object.assign(record, originalRoom);
    members.forEach((member, index) => Object.assign(member, originalMembers[index]));
  }
});
await check('unrelated lobby and membership history cannot grow the badge cache', async () => {
  const before = await visible(alice, badge(live));
  for (let i = 0; i < 1000; i++) {
    const id = `44444444-0000-4000-8000-${i.toString(16).padStart(12, '0')}`;
    add('rooms', room(id, 'finished'));
    add('room_players', player(`history-player-${i}`, id, alice, 1, null));
  }
  assert.deepEqual(await visible(alice, badge(live)), before);
});
await check('v2 has lossless text UUIDs, six room values and three move values', async () => {
  const got = await visible(alice, compact(live));
  assert.equal(got.length, 50);
  const room = got.find(row => row.table === 'rooms');
  assert.deepEqual(room, { table: 'rooms', id: uuid64(live), s: 1n, p: 1, a: 0, b: 0, w: null, n: 0 });
  const moves = got.filter(row => row.table === 'moves');
  assert.equal(new Set(moves.map(row => row.id)).size, 49);
  assert.deepEqual(moves.map(row => Number(row.e)).sort((a, b) => a - b), Array.from({ length: 49 }, (_, i) => i));
  for (const move of moves) {
    assert.equal(move.id.length, 24);
    assert.equal(move.r, uuid64(live));
    assert.equal(Number(move.n), Number(move.e) + 1);
    assert.deepEqual(Object.keys(move).sort(), ['e', 'id', 'n', 'r', 'table']);
    const original = rows.find(row => row.table === 'moves' && uuid64(row.record.id) === move.id).record.id;
    assert.equal(Buffer.from(move.id, 'base64').toString('hex'), original.replaceAll('-', ''));
  }
});
await check('v2 membership isolation and cancellation match the installed badge stream', async () => {
  assert.deepEqual(await visible(eve, compact(live)), []);
  assert.deepEqual(await visible(alice, compact(other)), []);
  assert.deepEqual(await visible(bob, compact(live)), await visible(alice, compact(live)));
  const record = rows.find(row => row.table === 'rooms' && row.record.id === live).record;
  for (const [status, code] of [['waiting', 0n], ['playing', 1n], ['finished', 2n], ['cancelled', 3n]]) {
    record.status = status;
    assert.equal((await visible(alice, compact(live))).find(row => row.table === 'rooms').s, code);
  }
  record.status = 'playing';
});
await check('browser move projections omit audit timestamps', async () => {
  const got = await visible(alice, { ...watch(live), defaults: true });
  assert.ok(got.filter(row => row.table === 'moves').every(row => !('created_at' in row)));
});
await check('spectators receive a board without pairing codes or device rows', async () => {
  const got = await visible(eve, watch(live));
  assert.equal(got.length, 50);
  assert.ok(got.every(row => row.table !== 'room_players'));
});
await check('room owner receives exactly their own pairing details', async () => {
  const got = (await visible(alice, watch(live))).filter(row => row.table === 'room_players');
  assert.equal(got.length, 1);
  assert.equal(got[0].pairing_code, 'AAAAA');
  assert.equal(got[0].user_id, alice);
});
await check('overlapping browser streams always project identical row contents', async () => {
  const unique = new Map();
  for (const row of await visible(alice, { ...watch(live), defaults: true })) {
    const key = `${row.table}:${row.id}`;
    if (unique.has(key)) assert.deepEqual(row, unique.get(key));
    unique.set(key, row);
  }
});
await check('removed membership revokes badge access at the next checkpoint', async () => {
  const index = rows.findIndex(row => row.table === 'room_players' && row.record.id === 'bob-live');
  const removed = rows.splice(index, 1)[0];
  assert.deepEqual(await visible(bob, badge(live)), []);
  assert.deepEqual(await visible(bob, compact(live)), []);
  rows.splice(index, 0, removed);
});
console.log(JSON.stringify({ passed, scope: 'Local PowerSync policy compiler and bucket evaluator; no live deployment.' }));
