import { pairBadge, type PairDeps, type PlayerRow, validatePairBody } from "./handler.ts";

function assert(condition: unknown, message = "assertion failed"): asserts condition {
  if (!condition) throw new Error(message);
}
const deviceA = "11111111-1111-4111-8111-111111111111";
const deviceB = "22222222-2222-4222-8222-222222222222";
const body = { code: "ABCAB", device_id: deviceA };
const tokens = { access_token: "access", refresh_token: "refresh", expires_in: 3600 };
function player(changes: Partial<PlayerRow> = {}): PlayerRow {
  return { id: "member", room_id: "room", user_id: "user", seat: 1,
    device_id: null, paired_at: null, status: "waiting", ...changes };
}
function dependencies(changes: Partial<PairDeps> = {}): PairDeps {
  return { findPlayer: () => Promise.resolve(player()), markPaired: () => Promise.resolve(true),
    sessionFor: () => Promise.resolve(tokens), ...changes };
}

Deno.test("pairing validates code and device before accessing the backend", async () => {
  assert("error" in validatePairBody({ code: "ABCD", device_id: deviceA }));
  assert("error" in validatePairBody({ code: "ABCAB", device_id: "invalid" }));
  const response = await pairBadge({ ...body, code: "invalid" }, dependencies({
    findPlayer: () => { throw new Error("backend should not be called"); },
  }));
  assert(response.status === 400);
});

Deno.test("unknown and closed room codes cannot issue sessions", async () => {
  for (const found of [null, player({ status: "cancelled" }), player({ status: "finished" })]) {
    const response = await pairBadge(body, dependencies({ findPlayer: () => Promise.resolve(found),
      sessionFor: () => { throw new Error("session should not be issued"); } }));
    assert(response.status === 401);
  }
});

Deno.test("a different paired device is rejected before session issuance", async () => {
  const response = await pairBadge(body, dependencies({
    findPlayer: () => Promise.resolve(player({ paired_at: "now", device_id: deviceB })),
    sessionFor: () => { throw new Error("session should not be issued"); },
  }));
  assert(response.status === 409);
  assert(!("access_token" in response.body));
});

Deno.test("credentials are ready before pairing advances the room", async () => {
  const calls: string[] = [];
  const response = await pairBadge(body, dependencies({
    sessionFor: () => { calls.push("session"); return Promise.resolve(tokens); },
    markPaired: (id, code, device) => {
      assert(id === "member" && code === body.code && device === deviceA);
      calls.push("claim"); return Promise.resolve(true);
    },
  }));
  assert(response.status === 200 && response.body.access_token === tokens.access_token);
  assert(calls.join(",") === "session,claim");
});

Deno.test("temporary Auth failure leaves the badge unpaired", async () => {
  let paired = false;
  let failed = false;
  try {
    await pairBadge(body, dependencies({ sessionFor: () => Promise.reject(new Error("auth unavailable")),
      markPaired: () => { paired = true; return Promise.resolve(true); } }));
  } catch { failed = true; }
  assert(failed && !paired);
});

Deno.test("a concurrent claim by another device never returns account credentials", async () => {
  let reads = 0;
  const response = await pairBadge(body, dependencies({
    findPlayer: () => Promise.resolve(reads++ === 0 ? player() : player({ paired_at: "now", device_id: deviceB })),
    markPaired: () => Promise.resolve(false),
  }));
  assert(response.status === 409);
  assert(!("access_token" in response.body) && !("refresh_token" in response.body));
});

Deno.test("simultaneous retries from the same device remain idempotent", async () => {
  let reads = 0;
  const response = await pairBadge(body, dependencies({
    findPlayer: () => Promise.resolve(reads++ === 0 ? player() : player({ paired_at: "now", device_id: deviceA })),
    markPaired: () => Promise.resolve(false),
  }));
  assert(response.status === 200 && response.body.refresh_token === tokens.refresh_token);
});

Deno.test("cancellation during pairing prevents credentials reaching the badge", async () => {
  let reads = 0;
  const response = await pairBadge(body, dependencies({
    findPlayer: () => Promise.resolve(reads++ === 0 ? player() : null),
    markPaired: () => Promise.resolve(false),
  }));
  assert(response.status === 401 && !("access_token" in response.body));
});
