import { createClient, type Session, type SupabaseClient } from "@supabase/supabase-js";
import { config } from "./config";

export const supabase = createClient(config.supabaseUrl, config.supabaseAnonKey, {
  auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: false },
});

const sessionRequests = new WeakMap<SupabaseClient, Promise<Session>>();

// React StrictMode replays startup effects. Share the entire restore/create
// operation so both mounts use the same anonymous account.
export function ensureSession(client: SupabaseClient = supabase): Promise<Session> {
  const existing = sessionRequests.get(client);
  if (existing) return existing;
  const request = restoreSession(client).finally(() => {
    sessionRequests.delete(client);
  });
  sessionRequests.set(client, request);
  return request;
}

async function restoreSession(client: SupabaseClient): Promise<Session> {
  const { data, error } = await client.auth.getSession();
  if (error) throw error;
  if (data.session) return data.session;
  const created = await client.auth.signInAnonymously();
  if (created.error || !created.data.session) {
    throw created.error ?? new Error("Anonymous sign-in failed");
  }
  return created.data.session;
}

export async function requireSession(userId: string, client: SupabaseClient = supabase): Promise<Session> {
  const { data, error } = await client.auth.getSession();
  if (error) throw error;
  if (!data.session || data.session.user.id !== userId) {
    throw new Error("Your sign-in changed. Reload to reconnect to your rooms.");
  }
  return data.session;
}
