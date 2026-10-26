import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PowerSyncDatabase } from "@powersync/web";
import type { SupabaseClient } from "@supabase/supabase-js";

const mocks = vi.hoisted(() => ({ createClient: vi.fn() }));
vi.mock("@supabase/supabase-js", () => ({ createClient: mocks.createClient }));
vi.mock("./config", () => ({ config: { supabaseUrl: "https://project.supabase.co", supabaseAnonKey: "public" } }));

function fakeClient(userId = "virtual-user") {
  const query = {
    select: vi.fn().mockReturnThis(),
    eq: vi.fn().mockReturnThis(),
    maybeSingle: vi.fn().mockResolvedValue({ data: null, error: null }),
  };
  return {
    auth: {
      getSession: vi.fn().mockResolvedValue({ data: { session: { user: { id: userId } } }, error: null }),
      setSession: vi.fn().mockResolvedValue({ error: null }),
    },
    from: vi.fn(() => query),
    query,
    rpc: vi.fn().mockResolvedValue({ data: { room_id: "room", player_id: "player", seat: 2, pairing_code: "ABCAB" }, error: null }),
    functions: {
      invoke: vi.fn().mockResolvedValue({ data: { user_id: userId, room_id: "room", access_token: "paired-access", refresh_token: "paired-refresh" }, error: null }),
    },
  };
}

function context() {
  const own = fakeClient();
  const database = { getOptional: vi.fn().mockResolvedValue(null) };
  return {
    own,
    database,
    client: own as unknown as SupabaseClient,
    db: database as unknown as Pick<PowerSyncDatabase, "getOptional">,
  };
}

beforeEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
  const storage = new Map<string, string>();
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
  });
  mocks.createClient.mockImplementation(() => fakeClient("dashboard-user"));
});

describe("joining and pairing a virtual badge", () => {
  it("joins and pairs in one action using only the virtual badge's identity", async () => {
    const { own, client, database, db } = context();
    const { joinAndPairEmulator } = await import("./virtualBadge");
    const { rememberedCode } = await import("./rooms");
    const { supabase } = await import("./supabase");

    await joinAndPairEmulator(client, "virtual-user", "device", "room", db);

    expect(own.from).toHaveBeenCalledWith("room_players");
    expect(own.query.select).toHaveBeenCalledWith("paired_at");
    expect(own.query.eq.mock.calls).toEqual([["room_id", "room"], ["user_id", "virtual-user"]]);
    expect(own.rpc).toHaveBeenCalledWith("join_room", { target: "room" });
    expect(own.functions.invoke).toHaveBeenCalledWith("badge-pair", { body: { code: "ABCAB", device_id: "device" } });
    expect(own.auth.setSession).toHaveBeenCalledWith({ access_token: "paired-access", refresh_token: "paired-refresh" });
    expect(rememberedCode("room", "virtual-user")).toBe("ABCAB");
    expect(rememberedCode("room", "dashboard-user")).toBeNull();
    expect(database.getOptional).not.toHaveBeenCalled();
    expect(supabase.from).not.toHaveBeenCalled();
    expect(supabase.rpc).not.toHaveBeenCalled();
    expect(supabase.auth.setSession).not.toHaveBeenCalled();
  });

  it("pairs an existing unpaired membership using its own synced code without joining again", async () => {
    const { own, client, database, db } = context();
    own.query.maybeSingle.mockResolvedValue({ data: { paired_at: null }, error: null });
    database.getOptional.mockResolvedValue({ pairing_code: "CCABA", device_id: null, paired_at: null });
    const { joinAndPairEmulator } = await import("./virtualBadge");

    await joinAndPairEmulator(client, "virtual-user", "device", "room", db);

    expect(database.getOptional).toHaveBeenCalledWith(
      "SELECT pairing_code, device_id, paired_at FROM room_players WHERE room_id = ? AND user_id = ?",
      ["room", "virtual-user"],
    );
    expect(own.rpc).not.toHaveBeenCalled();
    expect(own.functions.invoke).toHaveBeenCalledWith("badge-pair", { body: { code: "CCABA", device_id: "device" } });
  });

  it("retries failed pairing using the saved ticket before the membership has synced", async () => {
    const { own, client, db } = context();
    own.functions.invoke.mockResolvedValueOnce({ data: null, error: { message: "Connection lost" } });
    const { joinAndPairEmulator } = await import("./virtualBadge");

    await expect(joinAndPairEmulator(client, "virtual-user", "device", "room", db)).rejects.toThrow("Connection lost");
    own.query.maybeSingle.mockResolvedValue({ data: { paired_at: null }, error: null });
    await joinAndPairEmulator(client, "virtual-user", "device", "room", db);

    expect(own.rpc).toHaveBeenCalledTimes(1);
    expect(own.functions.invoke).toHaveBeenCalledTimes(2);
    expect(own.auth.setSession).toHaveBeenCalledTimes(1);
  });

  it("recovers a lost join response without attempting another join", async () => {
    const { own, client, database, db } = context();
    own.rpc.mockResolvedValueOnce({ data: null, error: { message: "Connection lost" } });
    const { joinAndPairEmulator } = await import("./virtualBadge");

    await expect(joinAndPairEmulator(client, "virtual-user", "device", "room", db)).rejects.toThrow("Connection lost");
    expect(own.functions.invoke).not.toHaveBeenCalled();
    own.query.maybeSingle.mockResolvedValue({ data: { paired_at: null }, error: null });
    await expect(joinAndPairEmulator(client, "virtual-user", "device", "room", db)).rejects.toThrow("pairing details to sync");
    database.getOptional.mockResolvedValue({ pairing_code: "ABCAB", device_id: null, paired_at: null });
    await joinAndPairEmulator(client, "virtual-user", "device", "room", db);

    expect(own.rpc).toHaveBeenCalledTimes(1);
    expect(own.functions.invoke).toHaveBeenCalledTimes(1);
  });

  it("reuses a seat already paired with this device", async () => {
    const { own, client, database, db } = context();
    own.query.maybeSingle.mockResolvedValue({ data: { paired_at: "now" }, error: null });
    database.getOptional.mockResolvedValue({ pairing_code: null, device_id: "device", paired_at: "now" });
    const { joinAndPairEmulator } = await import("./virtualBadge");

    await joinAndPairEmulator(client, "virtual-user", "device", "room", db);

    expect(own.rpc).not.toHaveBeenCalled();
    expect(own.functions.invoke).not.toHaveBeenCalled();
    expect(own.auth.setSession).not.toHaveBeenCalled();
  });

  it("rejects a seat paired with another device", async () => {
    const { own, client, database, db } = context();
    own.query.maybeSingle.mockResolvedValue({ data: { paired_at: "now" }, error: null });
    database.getOptional.mockResolvedValue({ pairing_code: "ABCAB", device_id: "other-device", paired_at: "now" });
    const { joinAndPairEmulator } = await import("./virtualBadge");

    await expect(joinAndPairEmulator(client, "virtual-user", "device", "room", db)).rejects.toThrow("another badge");

    expect(own.rpc).not.toHaveBeenCalled();
    expect(own.functions.invoke).not.toHaveBeenCalled();
  });

  it("can confirm a lost pairing response using the saved code while device details catch up", async () => {
    const { own, client, db } = context();
    own.functions.invoke.mockResolvedValueOnce({ data: null, error: { message: "Connection lost" } });
    const { joinAndPairEmulator } = await import("./virtualBadge");

    await expect(joinAndPairEmulator(client, "virtual-user", "device", "room", db)).rejects.toThrow("Connection lost");
    own.query.maybeSingle.mockResolvedValue({ data: { paired_at: "now" }, error: null });
    await joinAndPairEmulator(client, "virtual-user", "device", "room", db);

    expect(own.rpc).toHaveBeenCalledTimes(1);
    expect(own.functions.invoke).toHaveBeenCalledTimes(2);
  });

  it("stops before any room requests if the virtual badge's identity changed", async () => {
    const { own, client, db } = context();
    own.auth.getSession.mockResolvedValue({ data: { session: { user: { id: "other-user" } } }, error: null });
    const { joinAndPairEmulator } = await import("./virtualBadge");

    await expect(joinAndPairEmulator(client, "virtual-user", "device", "room", db)).rejects.toThrow("sign-in changed");

    expect(own.from).not.toHaveBeenCalled();
    expect(own.rpc).not.toHaveBeenCalled();
    expect(own.functions.invoke).not.toHaveBeenCalled();
  });

  it("does not join or pair if authoritative membership lookup fails", async () => {
    const { own, client, db } = context();
    own.query.maybeSingle.mockResolvedValue({ data: null, error: { message: "Offline" } });
    const { joinAndPairEmulator } = await import("./virtualBadge");

    await expect(joinAndPairEmulator(client, "virtual-user", "device", "room", db)).rejects.toThrow("Offline");

    expect(own.rpc).not.toHaveBeenCalled();
    expect(own.functions.invoke).not.toHaveBeenCalled();
  });

  it("shares simultaneous requests so a repeated click cannot join twice", async () => {
    const { own, client, db } = context();
    const { joinAndPairEmulator } = await import("./virtualBadge");

    const first = joinAndPairEmulator(client, "virtual-user", "device", "room", db);
    const second = joinAndPairEmulator(client, "virtual-user", "device", "room", db);
    expect(first).toBe(second);
    await Promise.all([first, second]);

    expect(own.rpc).toHaveBeenCalledTimes(1);
    expect(own.functions.invoke).toHaveBeenCalledTimes(1);
  });
});
