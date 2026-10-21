import { PowerSyncDatabase } from "@powersync/web";
import type { SupabaseClient } from "@supabase/supabase-js";
import { createConnector } from "./connector";
import { AppSchema } from "./schema";

const databases = new Map<string, Promise<PowerSyncDatabase>>();
const connections = new WeakMap<PowerSyncDatabase, Promise<void>>();

export function databaseFilename(userId: string): string {
  return `dots-and-boxes-${encodeURIComponent(userId)}.db`;
}

// Module-owned handles survive StrictMode's remount. A new anonymous identity
// cannot see the previous identity's cached room membership or pairing codes.
export function openUserDatabase(userId: string): Promise<PowerSyncDatabase> {
  const existing = databases.get(userId);
  if (existing) return existing;
  const opening = initializeDatabase(userId).catch((error: unknown) => {
    databases.delete(userId);
    throw error;
  });
  databases.set(userId, opening);
  return opening;
}

async function initializeDatabase(userId: string): Promise<PowerSyncDatabase> {
  // Serve the SDK's prebuilt worker and its matching SQLite binaries together.
  // This avoids Vite's dependency/worker transforms resolving WASM to the SPA.
  const worker = `${import.meta.env.BASE_URL}assets/powersync/@powersync/worker.js`;
  const database = new PowerSyncDatabase({
    schema: AppSchema,
    database: { dbFilename: databaseFilename(userId), worker },
    sync: { worker },
  });
  try {
    await database.init();
    return database;
  } catch (error) {
    await database.close();
    throw error;
  }
}

// Do not await this before showing local data: connect is not a first-sync
// barrier and can remain pending while offline. The SDK retries in background.
export function startSync(database: PowerSyncDatabase, userId: string, client?: SupabaseClient): Promise<void> {
  const existing = connections.get(database);
  if (existing) return existing;
  const connection = database.connect(client ? createConnector(userId, client) : createConnector(userId));
  connections.set(database, connection);
  return connection;
}
