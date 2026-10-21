import { createClient, type SupabaseClient } from "@supabase/supabase-js";
import { config } from "./config";
import type { Move } from "../game";
import { requireSession } from "./supabase";

// Separate storage and Auth broadcast channels let a real badge's dashboard
// and this virtual opponent run side by side in the same browser profile.
export const emulatorStorageKey = (profile = "default") => `dots-emulator-auth-v1:${new URL(config.supabaseUrl).host}:${profile}`;
const clients = new Map<string, SupabaseClient>();

export function emulatorProfile(search: string): string {
  const value = new URLSearchParams(search).get("badge") ?? "default";
  if (!/^[a-z0-9_-]{1,24}$/.test(value)) throw new Error("The badge profile must be 1–24 lowercase letters, numbers, underscores, or hyphens.");
  return value;
}

export function emulatorClient(profile = "default"): SupabaseClient {
  const existing = clients.get(profile);
  if (existing) return existing;
  const client = createClient(config.supabaseUrl, config.supabaseAnonKey, {
    auth: {
      storageKey: emulatorStorageKey(profile),
      persistSession: true,
      autoRefreshToken: true,
      detectSessionInUrl: false,
    },
  });
  clients.set(profile, client);
  return client;
}

export function emulatorDeviceId(profile = "default"): string {
  const key = `${emulatorStorageKey(profile)}:device`;
  const existing = localStorage.getItem(key);
  if (existing) return existing;
  const id = crypto.randomUUID();
  localStorage.setItem(key, id);
  return id;
}

const pairErrors: Record<string, string> = {
  unknown_code: "This code has expired. Create or join an open room again.",
  already_paired: "This seat is paired with another badge. Join a different room with the emulator.",
  invalid_code: "Pairing needs a five-letter A, B, C code.",
};

export async function pairEmulator(client: SupabaseClient, userId: string, roomId: string, code: string, deviceId: string): Promise<void> {
  await requireSession(userId, client);
  const { data, error } = await client.functions.invoke("badge-pair", {
    body: { code, device_id: deviceId },
  });
  if (error) {
    let reason: string | undefined;
    if (error.context instanceof Response) {
      try { reason = (await error.context.json()).error; } catch { /* Keep the transport error. */ }
    }
    throw new Error((reason && pairErrors[reason]) || error.message || "Pairing failed");
  }
  if (data?.user_id !== userId || data?.room_id !== roomId) {
    throw new Error("That code belongs to a different player. Reopen the emulator's own room.");
  }
  // Only ever install the response on this independent client, never on the
  // dashboard client. Both the existing session and paired session are ours.
  const session = await client.auth.setSession({ access_token: data.access_token, refresh_token: data.refresh_token });
  if (session.error) throw session.error;
}

export interface PendingMove {
  id: string;
  roomId: string;
  orientation: "h" | "v";
  row: number;
  col: number;
}

const pendingKey = (userId: string, roomId: string) => `dots-emulator-pending-v1:${userId}:${roomId}`;

export function readPendingMove(userId: string, roomId: string): PendingMove | null {
  const raw = localStorage.getItem(pendingKey(userId, roomId));
  if (!raw) return null;
  try {
    const value = JSON.parse(raw);
    if (value.roomId === roomId && typeof value.id === "string" &&
        (value.orientation === "h" || value.orientation === "v") &&
        Number.isInteger(value.row) && Number.isInteger(value.col)) return value;
  } catch { /* A stale or damaged draft must not stop the emulator opening. */ }
  return null;
}

export function savePendingMove(userId: string, roomId: string, move: PendingMove | null): void {
  const key = pendingKey(userId, roomId);
  if (move) localStorage.setItem(key, JSON.stringify(move));
  else localStorage.removeItem(key);
}

export function isPermanentMoveError(error: unknown): boolean {
  const code = error && typeof error === "object" && "code" in error ? String(error.code) : "";
  return code === "P0001" || code === "42501" || code.startsWith("22") || code.startsWith("23");
}

export async function sendEmulatorMove(client: SupabaseClient, userId: string, move: PendingMove): Promise<void> {
  await requireSession(userId, client);
  const { error } = await client.rpc("submit_move", {
    move_id: move.id,
    target: move.roomId,
    orientation: move.orientation,
    row_i: move.row,
    col_i: move.col,
  });
  if (error) throw error;
}

export function sameEdge(a: Move | undefined, b: Move): boolean {
  return a?.orientation === b.orientation && a.row === b.row && a.col === b.col;
}
