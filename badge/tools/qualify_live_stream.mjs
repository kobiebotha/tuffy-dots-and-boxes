#!/usr/bin/env node
/** Opt-in live read-only v2 qualification; refreshes an existing private session.
 * Usage: node badge/tools/qualify_live_stream.mjs --credentials=/absolute/file.json
 * No pairing, SQL/RPC mutations, device access, or deployment. A rotated refresh
 * token is saved atomically to the same 0600 file. Tokens are never logged.
 */
import { readFile, stat, writeFile, rename, mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
let stage = 'arguments';
let directory;
let rotatedPath;
try {
  const args = Object.fromEntries(process.argv.slice(2).map(arg => {
    const split = arg.indexOf('=');
    if (!arg.startsWith('--') || split < 0) throw new Error('Use --credentials=/absolute/file.json');
    return [arg.slice(2, split), arg.slice(split + 1)];
  }));
  if (!args.credentials || !isAbsolute(args.credentials) || Object.keys(args).some(key => key !== 'credentials')) {
    throw new Error('Use --credentials=/absolute/file.json');
  }
  stage = 'private credential validation';
  const metadata = await stat(args.credentials);
  if (!metadata.isFile() || (metadata.mode & 0o777) !== 0o600) throw new Error('Credential file must be a regular 0600 file');
  const session = JSON.parse(await readFile(args.credentials, 'utf8'));
  for (const key of ['user_id', 'room_id', 'device_id']) if (!uuid.test(session[key])) throw new Error('Invalid credential identity');
  for (const key of ['access_token', 'refresh_token', 'powersync_url', 'url', 'key']) {
    if (typeof session[key] !== 'string' || !session[key]) throw new Error('Invalid credential shape');
  }
  if (![1, 2].includes(session.seat)) throw new Error('Invalid credential seat');
  const env = Object.fromEntries((await readFile(join(root, 'web/.env.local'), 'utf8'))
    .split(/\r?\n/).filter(line => /^[A-Z_]+=/.test(line)).map(line => {
      const split = line.indexOf('=');
      return [line.slice(0, split), line.slice(split + 1).replace(/^['"]|['"]$/g, '')];
    }));
  const supabase = env.VITE_SUPABASE_URL?.replace(/\/$/, '');
  const powersync = env.VITE_POWERSYNC_URL?.replace(/\/$/, '');
  const key = env.VITE_SUPABASE_ANON_KEY;
  if (!supabase?.startsWith('https://') || !powersync?.startsWith('https://') || !key
      || session.url.replace(/\/$/, '') !== supabase || session.powersync_url.replace(/\/$/, '') !== powersync) {
    throw new Error('Credential endpoints do not match this project public environment');
  }
  const getJSON = async (url, options) => {
    const response = await fetch(url, { ...options, signal: AbortSignal.timeout(30_000) });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  };
  stage = 'authorized Auth refresh';
  const refreshed = await getJSON(`${supabase}/auth/v1/token?grant_type=refresh_token`, {
    method: 'POST', headers: { apikey: key, 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh_token: session.refresh_token }),
  });
  if (refreshed.user?.id !== session.user_id || typeof refreshed.access_token !== 'string'
      || typeof refreshed.refresh_token !== 'string') throw new Error('Refresh identity mismatch');
  Object.assign(session, { access_token: refreshed.access_token, refresh_token: refreshed.refresh_token,
    expires_in: refreshed.expires_in });
  if ('expires_at' in session) session.expires_at = refreshed.expires_at;
  rotatedPath = `${args.credentials}.refresh-${process.pid}`;
  await writeFile(rotatedPath, JSON.stringify(session), { mode: 0o600, flag: 'wx' });
  await rename(rotatedPath, args.credentials);
  rotatedPath = undefined;

  stage = 'actual native request preparation';
  directory = await mkdtemp(join(tmpdir(), 'dots-live-native-'));
  const databaseRoot = join(directory, 'dots');
  const context = { user_id: session.user_id, room_id: session.room_id };
  const native = (prepare, value) => JSON.parse(execFileSync('python3', [
    join(root, 'badge/tools/qualify_stream_native.py'), ...(prepare ? ['--prepare'] : []),
    `--database-root=${databaseRoot}`,
  ], { input: JSON.stringify(value), encoding: 'utf8', timeout: 30_000, stdio: ['pipe', 'pipe', 'pipe'] }));
  const request = native(true, context);
  if (JSON.stringify(request.streams) !== JSON.stringify({ include_defaults: false, subscriptions: [
    { stream: 'badge_game_v2', parameters: { room_id: session.room_id }, override_priority: null },
  ] })) throw new Error('Unexpected native stream subscription');

  stage = 'bounded live PowerSync stream';
  const started = performance.now();
  const abort = new AbortController();
  const deadline = setTimeout(() => abort.abort(), 30_000);
  let wire = '';
  let bytes = 0;
  let full = false;
  try {
    const response = await fetch(`${powersync}/sync/stream`, { method: 'POST', signal: abort.signal,
      headers: { Authorization: `Bearer ${session.access_token}`, 'Content-Type': 'application/json',
        Accept: 'application/x-ndjson' }, body: JSON.stringify(request) });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8', { fatal: true });
    let buffered = '';
    try {
      while (!full) {
        const chunk = await reader.read();
        if (chunk.done) throw new Error('Stream ended before a full checkpoint');
        bytes += chunk.value.byteLength;
        if (bytes > 512 * 1024) throw new Error('Stream exceeded 512KiB bound');
        buffered += decoder.decode(chunk.value, { stream: true });
        for (;;) {
          const end = buffered.indexOf('\n');
          if (end < 0) break;
          const line = buffered.slice(0, end);
          buffered = buffered.slice(end + 1);
          wire += line + '\n';
          if (line.trim() && Object.hasOwn(JSON.parse(line), 'checkpoint_complete')) {
            full = true;
            break;
          }
        }
      }
    } finally { await reader.cancel(); }
  } finally { clearTimeout(deadline); }
  const checkpointMilliseconds = Math.round(performance.now() - started);

  stage = 'read-only canonical Supabase comparison';
  const headers = { apikey: key, Authorization: `Bearer ${session.access_token}` };
  const [rooms, moves] = await Promise.all([
    getJSON(`${supabase}/rest/v1/rooms?id=eq.${session.room_id}&select=status,cols,rows,current_seat,score_p1,score_p2,winner,move_count`, { headers }),
    getJSON(`${supabase}/rest/v1/moves?room_id=eq.${session.room_id}&select=id,orientation,row,col,move_index&order=move_index.asc`, { headers }),
  ]);
  if (rooms.length !== 1 || moves.length > 49) throw new Error('Unexpected canonical board bounds');
  stage = 'actual native apply and canonical UUID acknowledgement';
  const result = native(false, { ...context, request, wire, room: rooms[0], moves });
  console.log(JSON.stringify({ ...result, checkpointMilliseconds,
    transport: 'live PowerSync HTTPS + real Supabase Auth/RLS',
    vfs: 'temporary host POSIX; no badge hardware', authRefreshed: true }));
} catch (error) {
  // Child stderr and HTTP response bodies are deliberately not printed.
  console.error(JSON.stringify({ qualified: false, stage,
    error: error?.code || error?.name || 'Error' }));
  process.exitCode = 1;
} finally {
  if (rotatedPath) await rm(rotatedPath, { force: true });
  if (directory) await rm(directory, { recursive: true, force: true });
}
