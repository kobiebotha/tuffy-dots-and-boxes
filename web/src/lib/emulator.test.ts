import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SupabaseClient } from "@supabase/supabase-js";

const mocks = vi.hoisted(() => ({ createClient: vi.fn() }));
vi.mock("@supabase/supabase-js", () => ({ createClient: mocks.createClient }));
vi.mock("./config", () => ({ config: { supabaseUrl: "https://project.supabase.co", supabaseAnonKey: "public" } }));

function fakeClient(userId = "emulator-user") {
  return {
    auth: {
      getSession: vi.fn().mockResolvedValue({ data: { session: { user: { id: userId }, access_token: "own-token" } }, error: null }),
      signInAnonymously: vi.fn(),
      setSession: vi.fn().mockResolvedValue({ error: null }),
    },
    functions: { invoke: vi.fn() },
    rpc: vi.fn().mockResolvedValue({ error: null }),
  };
}

beforeEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
  const storage = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
    removeItem: (key: string) => storage.delete(key),
  });
  mocks.createClient.mockImplementation(() => fakeClient());
});

describe("virtual badge identity", () => {
  it("uses a separate persisted Auth channel for each badge without replacing the dashboard client", async () => {
    const { emulatorClient } = await import("./emulator");
    const { supabase } = await import("./supabase");
    const first = emulatorClient();
    const second = emulatorClient("2");
    expect(emulatorClient()).toBe(first);
    expect(emulatorClient("2")).toBe(second);
    expect(first).not.toBe(second);
    expect(first).not.toBe(supabase);
    const options = mocks.createClient.mock.calls.map((call) => call[2].auth);
    expect(options[0].storageKey).toBeUndefined();
    expect(options[1].storageKey).toBe("dots-emulator-auth-v1:project.supabase.co:default");
    expect(options[2].storageKey).toBe("dots-emulator-auth-v1:project.supabase.co:2");
    expect(options[1].persistSession).toBe(true);
    expect(mocks.createClient).toHaveBeenCalledTimes(3);
  });

  it("accepts short profile names and rejects accidental sharing through malformed names", async () => {
    const { emulatorProfile } = await import("./emulator");
    expect(emulatorProfile("")).toBe("default");
    expect(emulatorProfile("?badge=2&room=abc")).toBe("2");
    expect(emulatorProfile("?badge=tufty-test_3")).toBe("tufty-test_3");
    for (const value of ["", "../2", "X", "x".repeat(25)]) expect(() => emulatorProfile(`?badge=${value}`)).toThrow("badge profile");
  });

  it("restores a persistent device UUID and gives the second profile its own UUID", async () => {
    const { emulatorDeviceId } = await import("./emulator");
    const first = emulatorDeviceId();
    const second = emulatorDeviceId("2");
    expect(first).toMatch(/^[0-9a-f-]{36}$/);
    expect(second).not.toBe(first);
    expect(emulatorDeviceId()).toBe(first);
    expect(emulatorDeviceId("2")).toBe(second);
  });

  it("uses the actual badge pairing endpoint and installs credentials only on the emulator", async () => {
    const own = fakeClient();
    own.functions.invoke.mockResolvedValue({ data: { user_id: "emulator-user", room_id: "room", access_token: "paired-access", refresh_token: "paired-refresh" }, error: null });
    const { pairEmulator } = await import("./emulator");
    const { supabase } = await import("./supabase");
    await pairEmulator(own as unknown as SupabaseClient, "emulator-user", "room", "ABCAB", "device");
    expect(own.functions.invoke).toHaveBeenCalledWith("badge-pair", { body: { code: "ABCAB", device_id: "device" } });
    expect(own.auth.setSession).toHaveBeenCalledWith({ access_token: "paired-access", refresh_token: "paired-refresh" });
    expect(supabase.auth.setSession).not.toHaveBeenCalled();
  });

  it("refuses a session for a different player or room before installing it", async () => {
    const own = fakeClient();
    own.functions.invoke.mockResolvedValue({ data: { user_id: "dashboard-user", room_id: "room", access_token: "foreign" }, error: null });
    const { pairEmulator } = await import("./emulator");
    await expect(pairEmulator(own as unknown as SupabaseClient, "emulator-user", "room", "ABCAB", "device")).rejects.toThrow("different player");
    expect(own.auth.setSession).not.toHaveBeenCalled();
  });

  it("explains pairing conflicts without replacing a session", async () => {
    const own = fakeClient();
    own.functions.invoke.mockResolvedValue({ data: null, error: { context: new Response(JSON.stringify({ error: "already_paired" })), message: "HTTP 409" } });
    const { pairEmulator } = await import("./emulator");
    await expect(pairEmulator(own as unknown as SupabaseClient, "emulator-user", "room", "ABCAB", "device")).rejects.toThrow("another badge");
    expect(own.auth.setSession).not.toHaveBeenCalled();
  });
});

describe("emulated move recovery", () => {
  const move = { id: "saved-move-uuid", roomId: "room", orientation: "h" as const, row: 0, col: 2 };

  it("retains the same move UUID across failed responses and reloads for idempotent RPC retries", async () => {
    const own = fakeClient();
    own.rpc.mockResolvedValueOnce({ error: { message: "Connection lost" } }).mockResolvedValueOnce({ error: null });
    const { readPendingMove, savePendingMove, sendEmulatorMove } = await import("./emulator");
    const client = own as unknown as SupabaseClient;
    savePendingMove("emulator-user", "room", move);
    await expect(sendEmulatorMove(client, "emulator-user", move)).rejects.toMatchObject({ message: "Connection lost" });
    const restored = readPendingMove("emulator-user", "room");
    expect(restored).toEqual(move);
    await sendEmulatorMove(client, "emulator-user", restored!);
    expect(own.rpc.mock.calls).toEqual([
      ["submit_move", { move_id: "saved-move-uuid", target: "room", orientation: "h", row_i: 0, col_i: 2 }],
      ["submit_move", { move_id: "saved-move-uuid", target: "room", orientation: "h", row_i: 0, col_i: 2 }],
    ]);
    // An RPC response alone must not discard the saved move before sync.
    expect(readPendingMove("emulator-user", "room")).toEqual(move);
    expect(readPendingMove("dashboard-user", "room")).toBeNull();
    savePendingMove("emulator-user", "room", null);
    expect(readPendingMove("emulator-user", "room")).toBeNull();
  });

  it("blocks submission when an injected client's identity changes", async () => {
    const own = fakeClient("other-user");
    const { sendEmulatorMove } = await import("./emulator");
    await expect(sendEmulatorMove(own as unknown as SupabaseClient, "emulator-user", move)).rejects.toThrow("sign-in changed");
    expect(own.rpc).not.toHaveBeenCalled();
  });

  it("distinguishes permanent validation errors from retryable network errors", async () => {
    const { isPermanentMoveError } = await import("./emulator");
    for (const code of ["P0001", "42501", "23505", "22023"]) expect(isPermanentMoveError({ code })).toBe(true);
    for (const error of [{ code: "503" }, new TypeError("Failed to fetch"), null]) expect(isPermanentMoveError(error)).toBe(false);
  });
});
