import { ConfigurationError, createHandler } from "./http.ts";
import { type PairDeps } from "./handler.ts";

function assert(condition: unknown, message = "assertion failed"): asserts condition {
  if (!condition) throw new Error(message);
}

const device = "11111111-1111-4111-8111-111111111111";
const powersyncUrl = "https://sync.example";
const body = { code: "ABCAB", device_id: device };
const tokens = { access_token: "access", refresh_token: "refresh", expires_in: 3600 };

function dependencies(changes: Partial<PairDeps> = {}): PairDeps {
  return {
    findPlayer: () => Promise.resolve({ id: "member", room_id: "room", user_id: "user", seat: 1,
      device_id: null, paired_at: null, status: "waiting" }),
    markPaired: () => Promise.resolve(true),
    sessionFor: () => Promise.resolve(tokens),
    ...changes,
  };
}

function request(payload: unknown = body): Request {
  return new Request("https://functions.example/badge-pair", {
    method: "POST", headers: { Origin: "http://localhost:5173", "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

function assertCors(response: Response): void {
  assert(response.headers.get("access-control-allow-origin") === "*");
  assert(response.headers.get("access-control-allow-methods") === "POST, OPTIONS");
  const allowed = response.headers.get("access-control-allow-headers")?.toLowerCase().split(/,\s*/);
  for (const name of ["authorization", "apikey", "content-type", "x-client-info"]) {
    assert(allowed?.includes(name), `CORS must allow ${name}`);
  }
}

Deno.test("browser pairing preflight succeeds before credentials or backend are accessed", async () => {
  const handler = createHandler(() => { throw new Error("must not access environment"); }, powersyncUrl);
  const response = await handler(new Request("https://functions.example/badge-pair", {
    method: "OPTIONS", headers: { Origin: "http://localhost:5173",
      "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "apikey,content-type" },
  }));
  assert(response.status === 204);
  assert(await response.text() === "");
  assertCors(response);
});

Deno.test("unsupported methods and malformed bodies retain browser CORS headers", async () => {
  const handler = createHandler(() => { throw new Error("must not access backend"); }, powersyncUrl);
  const get = await handler(new Request("https://functions.example/badge-pair"));
  assert(get.status === 405 && get.headers.get("allow") === "POST, OPTIONS");
  assertCors(get);
  const malformed = await handler(new Request("https://functions.example/badge-pair", { method: "POST", body: "{" }));
  assert(malformed.status === 400 && (await malformed.json()).error === "invalid_body");
  assertCors(malformed);
  const invalidCode = await handler(request({ ...body, code: "invalid" }));
  assert(invalidCode.status === 400 && (await invalidCode.json()).error === "invalid_code");
  assertCors(invalidCode);
});

Deno.test("successful browser pairing includes credentials and CORS headers", async () => {
  const response = await createHandler(() => dependencies(), powersyncUrl)(request());
  assert(response.status === 200);
  const result = await response.json();
  assert(result.access_token === tokens.access_token && result.powersync_url === powersyncUrl);
  assertCors(response);
});

Deno.test("unknown and already paired codes expose their errors to the browser", async () => {
  const variants: PairDeps[] = [
    dependencies({ findPlayer: () => Promise.resolve(null) }),
    dependencies({ findPlayer: () => Promise.resolve({ id: "member", room_id: "room", user_id: "user", seat: 1,
      device_id: "22222222-2222-4222-8222-222222222222", paired_at: "now", status: "playing" }) }),
  ];
  for (const [index, deps] of variants.entries()) {
    const response = await createHandler(() => deps, powersyncUrl)(request());
    assert(response.status === (index === 0 ? 401 : 409));
    assertCors(response);
    assert(!("access_token" in await response.json()));
  }
});

Deno.test("configuration and backend failures return safe errors with CORS headers", async () => {
  const cases = [
    { factory: () => { throw new ConfigurationError("private environment details"); }, status: 500, error: "misconfigured" },
    { factory: () => { throw new Error("private client initialization details"); }, status: 503, error: "unavailable" },
    { factory: () => dependencies({ sessionFor: () => Promise.reject(new Error("private Auth details")) }), status: 503, error: "unavailable" },
  ];
  for (const test of cases) {
    const response = await createHandler(test.factory, powersyncUrl)(request());
    assert(response.status === test.status);
    assertCors(response);
    assert((await response.json()).error === test.error);
  }
});
