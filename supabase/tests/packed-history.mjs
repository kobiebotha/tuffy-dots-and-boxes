// Local PostgreSQL/WASM qualification. No Supabase service or network access.
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = resolve(fileURLToPath(new URL('../..', import.meta.url)));
const modulePath = process.env.PGLITE_MODULE || '/private/tmp/badgegotchi-sql-validation/node_modules/@electric-sql/pglite/dist/index.js';
const { PGlite } = await import(pathToFileURL(modulePath).href);
const migrationPath = 'supabase/migrations/20261021222547_experimental_packed_history.sql';
const migration = readFileSync(resolve(root, migrationPath), 'utf8');
const alphabet = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklm';
const report = { engine: 'PGlite 0.5.10 / PostgreSQL WASM', migration: migrationPath,
  limitations: ['Supabase Auth is simulated; no service, replication, device or real concurrent-session qualification.'], checks: [] };
const output = process.argv.find(arg => arg.startsWith('--output='))?.slice(9);
const reportOutput = process.argv.find(arg => arg.startsWith('--report='))?.slice(9);
const db = new PGlite();
const check = name => { report.checks.push(name); console.log('PASS', name); };

async function initialize(database) {
  await database.exec(`
    create role anon nologin; create role authenticated nologin;
    create role service_role nologin bypassrls;
    create role powersync_dbbb9f370607ca2fe2a8629b nologin;
    create schema auth;
    create function auth.uid() returns uuid language sql stable as
    $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
    grant usage on schema auth to authenticated, anon, service_role;
  `);
  for (const name of ['20261020190000_dots_and_boxes.sql', '20261021160240_archive_rooms.sql']) {
    await database.exec(readFileSync(resolve(root, 'supabase/migrations', name), 'utf8'));
  }
}
async function actor(user) {
  await db.query("select set_config('request.jwt.claim.sub', $1, false)", [user || '']);
}
async function roomState(room) {
  return (await db.query('select * from public.rooms where id=$1', [room])).rows[0];
}
async function seedRoom(host = randomUUID(), guest = randomUUID()) {
  const room = randomUUID();
  await db.query("insert into public.rooms(id,host_user_id,status) values($1,$2,'playing')", [room, host]);
  await db.query('insert into public.room_players(room_id,user_id,seat,device_id,paired_at) values($1,$2,1,$4,now()),($1,$3,2,$5,now())',
    [room, host, guest, randomUUID(), randomUUID()]);
  return { room, host, guest };
}
function edge(code) {
  return code < 25 ? { orientation: 'h', row: Math.floor(code / 5), col: code % 5 }
    : { orientation: 'v', row: Math.floor((code - 25) / 6), col: (code - 25) % 6 };
}
async function submit(game, code, id = randomUUID()) {
  const state = await roomState(game.room);
  await actor(state.current_seat === 1 ? game.host : game.guest);
  const e = edge(code);
  const result = await db.query('select public.submit_move($1,$2,$3,$4,$5) result', [id, game.room, e.orientation, e.row, e.col]);
  return { id, result: result.rows[0].result };
}
const boardFields = ['status', 'current_seat', 'score_p1', 'score_p2', 'winner', 'move_count', 'edge_history', 'finished_at'];
const board = state => Object.fromEntries(boardFields.map(key => [key, state[key]]));

class ReferenceGame {
  edges = new Set(); boxes = new Set(); scores = [0, 0]; seat = 1; history = '';
  play(code) {
    assert(!this.edges.has(code));
    this.edges.add(code); this.history += alphabet[code];
    let completed = 0;
    for (let row = 0; row < 4; row++) for (let col = 0; col < 5; col++) {
      const box = row * 5 + col;
      if (!this.boxes.has(box) && [row * 5 + col, (row + 1) * 5 + col,
        25 + row * 6 + col, 26 + row * 6 + col].every(value => this.edges.has(value))) {
        this.boxes.add(box); completed++;
      }
    }
    if (completed) this.scores[this.seat - 1] += completed;
    else this.seat = 3 - this.seat;
    return completed;
  }
}
function shuffled(seed) {
  const values = Array.from({ length: 49 }, (_, index) => index);
  for (let index = values.length - 1; index > 0; index--) {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    const other = seed % (index + 1);
    [values[index], values[other]] = [values[other], values[index]];
  }
  return values;
}

try {
  await initialize(db);
  const legacy = await seedRoom();
  for (let code = 0; code < 7; code++) await submit(legacy, code);
  const legacyEmpty = await seedRoom();
  const legacyFinished = await seedRoom();
  for (let code = 0; code < 49; code++) await submit(legacyFinished, code);
  const legacyArchived = await seedRoom();
  for (let code = 0; code < 5; code++) await submit(legacyArchived, code);
  await actor(legacyArchived.host);
  await db.query('select public.archive_room($1)', [legacyArchived.room]);
  const legacyCases = [legacy, legacyEmpty, legacyFinished, legacyArchived];
  const legacyBefore = await Promise.all(legacyCases.map(game => roomState(game.room)));
  await db.exec('begin');
  try { await db.exec(migration); await db.exec('commit'); }
  catch (error) { await db.exec('rollback'); throw error; }
  const legacyAfter = await roomState(legacy.room);
  assert.equal(legacyAfter.edge_history, '0123456');
  for (let index = 0; index < legacyCases.length; index++) {
    const state = await roomState(legacyCases[index].room);
    assert.equal(state.edge_history, alphabet.slice(0, state.move_count));
    for (const key of Object.keys(legacyBefore[index])) assert.deepEqual(state[key], legacyBefore[index][key]);
  }
  check('Migration backfills empty/live/finished/archived rooms without changing existing rows or results');

  const indexes = (await db.query("select indexname,indexdef from pg_indexes where tablename='moves'")).rows;
  assert(!indexes.some(index => index.indexname === 'moves_room'));
  assert(indexes.some(index => index.indexdef.includes('UNIQUE') && index.indexdef.includes('(room_id, move_index)')));
  check('Only the duplicate non-unique moves_room index is removed');

  const fixture = { userId: '11111111-1111-4111-8111-111111111111', otherUserId: '22222222-2222-4222-8222-222222222222', roomStates: [] };
  let doubleBoxes = 0, retainedTurns = 0;
  for (let seed = 0; seed < 16; seed++) {
    const game = await seedRoom(seed ? undefined : fixture.userId, seed ? undefined : fixture.otherUserId);
    const reference = new ReferenceGame();
    if (!seed) { fixture.roomId = game.room; fixture.roomStates.push(await roomState(game.room)); }
    for (const code of seed ? shuffled(seed) : Array.from({ length: 49 }, (_, index) => index)) {
      const expectedActor = reference.seat;
      const accepted = await submit(game, code);
      assert.equal(accepted.result.ok, true);
      const completed = reference.play(code);
      doubleBoxes += completed === 2; retainedTurns += completed > 0;
      const incremental = await roomState(game.room);
      assert.equal(incremental.current_seat, reference.seat);
      assert.equal(incremental.score_p1, reference.scores[0]);
      assert.equal(incremental.score_p2, reference.scores[1]);
      assert.equal(incremental.edge_history, reference.history);
      assert.equal(incremental.move_count, reference.edges.size);
      assert.equal(incremental.status, reference.edges.size === 49 ? 'finished' : 'playing');
      assert.equal(incremental.winner, reference.edges.size !== 49 ? null
        : reference.scores[0] === reference.scores[1] ? 0 : reference.scores[0] > reference.scores[1] ? 1 : 2);
      await db.query('select public.recompute_room($1)', [game.room]);
      assert.deepEqual(board(await roomState(game.room)), board(incremental));
      if (!seed) fixture.roomStates.push(incremental);
      if (reference.edges.size === 49) {
        await actor(expectedActor === 1 ? game.host : game.guest);
        const retry = await db.query('select public.submit_move($1,$2,$3,$4,$5) result',
          [accepted.id, game.room, edge(code).orientation, edge(code).row, edge(code).col]);
        assert.equal(retry.rows[0].result.duplicate, true);
        assert.deepEqual(board(await roomState(game.room)), board(incremental));
      }
    }
    assert.equal(reference.scores[0] + reference.scores[1], 20);
  }
  assert(doubleBoxes > 0 && retainedTurns > 0);
  fixture.moves = (await db.query('select * from public.moves where room_id=$1 order by move_index', [fixture.roomId])).rows;
  assert.equal(fixture.moves.length, 49);
  check(`784 incremental moves match original SQL replay and independent JS replay, including ${doubleBoxes} double-box moves`);

  const invalid = await seedRoom();
  const beforeInvalid = board(await roomState(invalid.room));
  const rejects = async (user, orientation, row, col) => {
    await actor(user);
    await assert.rejects(db.query('select public.submit_move($1,$2,$3,$4,$5)', [randomUUID(), invalid.room, orientation, row, col]));
    assert.deepEqual(board(await roomState(invalid.room)), beforeInvalid);
    assert.equal((await db.query('select count(*)::int n from moves where room_id=$1', [invalid.room])).rows[0].n, 0);
  };
  await rejects(invalid.guest, 'h', 0, 0);
  await rejects(randomUUID(), 'h', 0, 0);
  for (const e of [['h', 5, 0], ['h', 0, 5], ['v', 4, 0], ['v', 0, 6], ['x', 0, 0]]) await rejects(invalid.host, ...e);
  await assert.rejects(db.query("insert into moves(id,room_id,seat,orientation,row,col,move_index) values($1,$2,1,'h',0,0,2)", [randomUUID(), invalid.room]), /consecutive/);
  await assert.rejects(db.query("insert into moves(id,room_id,seat,orientation,row,col,move_index) values($1,$2,2,'h',0,0,1)", [randomUUID(), invalid.room]), /out of turn/);
  assert.deepEqual(board(await roomState(invalid.room)), beforeInvalid);
  check('Wrong-turn, outsider, out-of-bounds, orientation and nonconsecutive inserts roll back rows and room history');

  const accepted = await submit(invalid, 0);
  const acceptedState = board(await roomState(invalid.room));
  await actor(invalid.host);
  const retry = await db.query("select public.submit_move($1,$2,'v',3,5) result", [accepted.id, invalid.room]);
  assert.equal(retry.rows[0].result.duplicate, true);
  assert.deepEqual(board(await roomState(invalid.room)), acceptedState);
  await actor(randomUUID());
  await assert.rejects(db.query("select public.submit_move($1,$2,'h',0,0)", [accepted.id, invalid.room]), /not your turn/);
  await actor(invalid.guest);
  await assert.rejects(db.query("select public.submit_move($1,$2,'h',0,0)", [randomUUID(), invalid.room]));
  assert.deepEqual(board(await roomState(invalid.room)), acceptedState);
  check('Exact UUID retries are idempotent; duplicate edges and outsider retries cannot change the board');

  await actor(invalid.host);
  await db.query('select public.archive_room($1)', [invalid.room]);
  const archived = await roomState(invalid.room);
  assert.equal(archived.status, 'cancelled');
  assert.equal(archived.edge_history, acceptedState.edge_history);
  assert.equal(archived.paired_count, 0);
  await assert.rejects(db.query("select public.submit_move($1,$2,'h',0,1)", [randomUUID(), invalid.room]), /not started/);
  await db.query('select public.recompute_room($1)', [invalid.room]);
  assert.deepEqual(board(await roomState(invalid.room)), board(archived));
  await actor(fixture.userId);
  await db.query('select public.archive_room($1)', [fixture.roomId]);
  fixture.terminalRoom = await roomState(fixture.roomId);
  assert.equal(fixture.terminalRoom.edge_history.length, 49);
  assert.equal(fixture.terminalRoom.status, 'cancelled');
  check('Active and completed archives retain packed history; terminal rooms reject new moves and rebuild correctly');

  await db.query("update public.rooms set edge_history='',score_p1=99 where id=$1", [legacy.room]);
  await db.query('select public.recompute_room($1)', [legacy.room]);
  assert.deepEqual(board(await roomState(legacy.room)), board(legacyAfter));
  check('Retained recompute_room rebuilds both derived scores and packed history from canonical moves');

  for (const value of ['n', alphabet + '0', 'é']) {
    await assert.rejects(db.query('update public.rooms set edge_history=$1 where id=$2', [value, legacy.room]), /rooms_edge_history_bounds/);
  }
  for (const name of ['recompute_room', 'recompute_room_replay', 'moves_recompute']) {
    const signature = name === 'moves_recompute' ? '()' : '(uuid)';
    for (const role of ['authenticated', 'anon']) {
      assert.equal((await db.query("select has_function_privilege($1,$2,'execute') permitted", [role, `public.${name}${signature}`])).rows[0].permitted, false);
    }
  }
  assert.equal((await db.query("select has_function_privilege('anon','public.submit_move(uuid,uuid,text,integer,integer)','execute') permitted")).rows[0].permitted, false);
  await db.exec('set role authenticated');
  await actor(fixture.userId);
  await assert.rejects(db.query("insert into public.moves(id,room_id,seat,orientation,row,col,move_index) values($1,$2,1,'h',0,0,1)", [randomUUID(), fixture.roomId]), /permission denied/);
  await db.exec('reset role');
  check('History alphabet/length constraints, internal function ACLs and client write restrictions remain enforced');

  for (const name of ['rules.sql', 'archive.sql']) await db.exec(readFileSync(resolve(root, 'supabase/tests', name), 'utf8'));
  check('Existing rules.sql and archive.sql regression suites pass against the new migration');

  const invalidLegacy = new PGlite();
  try {
    await initialize(invalidLegacy);
    const target = randomUUID();
    await invalidLegacy.query("insert into rooms(id,host_user_id,status) values($1,$2,'playing')", [target, randomUUID()]);
    // The previous replay accepts an indexed gap; the optimized counter must
    // not reinterpret that historical row as a different accepted sequence.
    await invalidLegacy.query("insert into moves(id,room_id,seat,orientation,row,col,move_index) values($1,$2,1,'h',0,0,2)", [randomUUID(), target]);
    await invalidLegacy.exec('begin');
    await assert.rejects(invalidLegacy.exec(migration), /invalid legacy move history/);
    await invalidLegacy.exec('rollback');
    assert.equal((await invalidLegacy.query("select count(*)::int n from information_schema.columns where table_schema='public' and table_name='rooms' and column_name='edge_history'")).rows[0].n, 0);
    assert.equal((await invalidLegacy.query("select to_regprocedure('public.recompute_room_replay(uuid)') name")).rows[0].name, null);
    assert.equal((await invalidLegacy.query("select count(*)::int n from pg_indexes where indexname='moves_room'")).rows[0].n, 1);
    assert.equal((await invalidLegacy.query('select move_count from rooms where id=$1', [target])).rows[0].move_count, 1);
    check('Invalid legacy histories reject migration atomically, retaining original columns/functions/indexes/data');
  } finally {
    await invalidLegacy.close();
  }
  if (output) writeFileSync(output, JSON.stringify(fixture, null, 2) + '\n');
  report.success = true; report.games = 16; report.movesCompared = 784; report.doubleBoxes = doubleBoxes;
  report.postgresVersion = (await db.query('select version() version')).rows[0].version;
  if (reportOutput) writeFileSync(reportOutput, JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report, null, 2));
} finally {
  await db.close();
}
