import { createClient } from "npm:@supabase/supabase-js@2.118.0";
import { type PairDeps, type PlayerRow } from "./handler.ts";
import { ConfigurationError, createHandler } from "./http.ts";

function env(name: string): string {
  const value = Deno.env.get(name);
  if (!value) throw new ConfigurationError(`Missing ${name}`);
  return value;
}

function dependencies(): PairDeps {
  const url = env("SUPABASE_URL");
  const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? env("SUPABASE_SECRET_KEY");
  const anonKey = Deno.env.get("SUPABASE_ANON_KEY") ?? Deno.env.get("SUPABASE_PUBLISHABLE_KEY") ?? "";
  if (!anonKey) throw new ConfigurationError("Missing Supabase public key");

  const admin = createClient(url, serviceKey, { auth: { persistSession: false, autoRefreshToken: false } });
  const anon = createClient(url, anonKey, { auth: { persistSession: false, autoRefreshToken: false } });

  return {
    async findPlayer(code) {
      const { data, error } = await admin
        .from("room_players")
        .select("id, user_id, room_id, seat, device_id, paired_at, rooms!inner(status)")
        .eq("pairing_code", code)
        .limit(1)
        .maybeSingle();
      if (error || !data) return null;
      const room = data.rooms as { status: string } | { status: string }[];
      const status = Array.isArray(room) ? room[0]?.status : room?.status;
      return {
        id: data.id,
        user_id: data.user_id,
        room_id: data.room_id,
        seat: data.seat,
        device_id: data.device_id,
        paired_at: data.paired_at,
        status: status ?? "",
      } satisfies PlayerRow;
    },
    async markPaired(playerId, code, deviceId) {
      const { data, error } = await admin
        .from("room_players")
        .update({ device_id: deviceId, paired_at: new Date().toISOString() })
        .eq("id", playerId)
        .eq("pairing_code", code)
        .is("paired_at", null)
        .select("id")
        .maybeSingle();
      if (error) throw new Error("pair_failed");
      return data !== null;
    },
    async sessionFor(userId) {
      const { data: userData, error: userError } = await admin.auth.admin.getUserById(userId);
      if (userError || !userData.user) throw new Error("user_missing");
      let email = userData.user.email;
      if (!email) {
        email = `badge+${userId}@players.invalid`;
        const updated = await admin.auth.admin.updateUserById(userId, { email, email_confirm: true });
        if (updated.error) throw new Error("session_unavailable");
      }
      const link = await admin.auth.admin.generateLink({ type: "magiclink", email });
      const tokenHash = link.data?.properties?.hashed_token;
      if (link.error || !tokenHash) throw new Error("session_unavailable");
      const verified = await anon.auth.verifyOtp({ type: "magiclink", token_hash: tokenHash });
      const session = verified.data.session;
      if (verified.error || !session) throw new Error("session_unavailable");
      return {
        access_token: session.access_token,
        refresh_token: session.refresh_token,
        expires_in: session.expires_in ?? 3600,
      };
    },
  };
}

Deno.serve(createHandler(dependencies, () => env("POWERSYNC_URL")));
