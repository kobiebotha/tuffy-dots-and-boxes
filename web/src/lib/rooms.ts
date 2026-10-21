import { supabase } from "./supabase";
import type { SupabaseClient } from "@supabase/supabase-js";

export interface RoomTicket {
  room_id: string;
  player_id: string;
  seat: number;
  pairing_code: string;
}

async function call<T>(client: SupabaseClient, name: string, args?: Record<string, unknown>): Promise<T> {
  const { data, error } = await client.rpc(name, args);
  if (error) throw new Error(error.message);
  return data as T;
}

export function createRoom(client: SupabaseClient = supabase): Promise<RoomTicket> {
  return call(client, "create_room");
}

export function joinRoom(roomId: string, client: SupabaseClient = supabase): Promise<RoomTicket> {
  return call(client, "join_room", { target: roomId });
}

export function cancelRoom(roomId: string, client: SupabaseClient = supabase): Promise<void> {
  return call(client, "cancel_room", { target: roomId });
}

export function archiveRoom(roomId: string, client: SupabaseClient = supabase): Promise<void> {
  return call(client, "archive_room", { target: roomId });
}

export function leaveRoom(roomId: string, client: SupabaseClient = supabase): Promise<void> {
  return call(client, "leave_room", { target: roomId });
}

const codeKey = (roomId: string, userId: string) => `dots-code:${userId}:${roomId}`;

export function rememberCode(roomId: string, code: string, userId: string): void {
  sessionStorage.setItem(codeKey(roomId, userId), code);
}

export function rememberedCode(roomId: string, userId: string): string | null {
  return sessionStorage.getItem(codeKey(roomId, userId));
}
