const CODE = /^[ABC]{5}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export interface PairBody {
  code?: unknown;
  device_id?: unknown;
}

export interface PairDeps {
  findPlayer(code: string): Promise<PlayerRow | null>;
  markPaired(playerId: string, code: string, deviceId: string): Promise<boolean>;
  sessionFor(userId: string): Promise<SessionTokens>;
}

export interface PlayerRow {
  id: string;
  user_id: string;
  room_id: string;
  seat: number;
  device_id: string | null;
  paired_at: string | null;
  status: string;
}

export interface SessionTokens {
  access_token: string;
  refresh_token: string;
  expires_in: number;
}

export function validatePairBody(body: PairBody): { code: string; deviceId: string } | { error: string } {
  if (!body || typeof body !== "object") return { error: "invalid_body" };
  if (typeof body.code !== "string" || !CODE.test(body.code)) return { error: "invalid_code" };
  if (typeof body.device_id !== "string" || !UUID.test(body.device_id)) return { error: "invalid_device" };
  return { code: body.code, deviceId: body.device_id.toLowerCase() };
}

export async function pairBadge(body: PairBody, deps: PairDeps): Promise<{ status: number; body: Record<string, unknown> }> {
  const parsed = validatePairBody(body);
  if ("error" in parsed) return { status: 400, body: { error: parsed.error } };
  const player = await deps.findPlayer(parsed.code);
  if (!player || !["waiting", "playing"].includes(player.status)) {
    return { status: 401, body: { error: "unknown_code" } };
  }
  if (player.paired_at && player.device_id && player.device_id !== parsed.deviceId) {
    return { status: 409, body: { error: "already_paired" } };
  }
  // Obtain credentials before advancing the game to playing. A temporary Auth
  // failure must not count an unusable badge as paired.
  const session = await deps.sessionFor(player.user_id);
  if (!player.paired_at) {
    const claimed = await deps.markPaired(player.id, parsed.code, parsed.deviceId);
    if (!claimed) {
      // The claim is conditional in Postgres. An earlier read cannot authorize
      // a second device that lost the race, but the same device may retry.
      const current = await deps.findPlayer(parsed.code);
      if (!current || !["waiting", "playing"].includes(current.status)) {
        return { status: 401, body: { error: "unknown_code" } };
      }
      if (current.id !== player.id || !current.paired_at || current.device_id !== parsed.deviceId) {
        return { status: 409, body: { error: "already_paired" } };
      }
    }
  }
  return {
    status: 200,
    body: {
      user_id: player.user_id,
      room_id: player.room_id,
      seat: player.seat,
      access_token: session.access_token,
      refresh_token: session.refresh_token,
      expires_in: session.expires_in,
    },
  };
}
