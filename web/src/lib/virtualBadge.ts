import type { PowerSyncDatabase } from "@powersync/web";
import type { SupabaseClient } from "@supabase/supabase-js";
import { pairEmulator } from "./emulator";
import { joinRoom, rememberedCode, rememberCode } from "./rooms";
import { requireSession } from "./supabase";

interface OwnMembership {
  pairing_code: string | null;
  device_id: string | null;
  paired_at: string | null;
}

const joining = new WeakMap<SupabaseClient, Map<string, Promise<void>>>();

export function joinAndPairEmulator(
  client: SupabaseClient,
  userId: string,
  deviceId: string,
  roomId: string,
  db: Pick<PowerSyncDatabase, "getOptional">,
): Promise<void> {
  let requests = joining.get(client);
  if (!requests) {
    requests = new Map();
    joining.set(client, requests);
  }
  const key = JSON.stringify([userId, deviceId, roomId]);
  const existing = requests.get(key);
  if (existing) return existing;
  const request = joinAndPair(client, userId, deviceId, roomId, db).finally(() => {
    requests.delete(key);
  });
  requests.set(key, request);
  return request;
}

async function joinAndPair(
  client: SupabaseClient,
  userId: string,
  deviceId: string,
  roomId: string,
  db: Pick<PowerSyncDatabase, "getOptional">,
): Promise<void> {
  await requireSession(userId, client);
  // Check server membership before joining: a previous join can have succeeded
  // even if its response was lost, or its PowerSync row has not arrived yet.
  // Pairing codes and device IDs are available only through the own-user stream.
  const { data: membership, error } = await client.from("room_players")
    .select("paired_at")
    .eq("room_id", roomId)
    .eq("user_id", userId)
    .maybeSingle();
  if (error) throw new Error(error.message);

  let code: string | null;
  if (membership) {
    const own = await db.getOptional<OwnMembership>(
      "SELECT pairing_code, device_id, paired_at FROM room_players WHERE room_id = ? AND user_id = ?",
      [roomId, userId],
    );
    if (membership.paired_at && own?.paired_at && own.device_id) {
      if (own.device_id === deviceId) return;
      throw new Error("This seat is paired with another badge. Join a different room with the virtual badge.");
    }
    code = own?.pairing_code || rememberedCode(roomId, userId);
    if (!code) {
      throw new Error("Your virtual badge has joined. Waiting for its pairing details to sync; try joining again in a moment.");
    }
  } else {
    const ticket = await joinRoom(roomId, client);
    code = ticket.pairing_code;
  }

  rememberCode(roomId, code, userId);
  await pairEmulator(client, userId, roomId, code, deviceId);
}
