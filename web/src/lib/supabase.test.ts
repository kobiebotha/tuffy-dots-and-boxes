import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Session, SupabaseClient } from "@supabase/supabase-js";

const auth = vi.hoisted(() => ({ getSession: vi.fn(), signInAnonymously: vi.fn() }));
vi.mock("@supabase/supabase-js", () => ({ createClient: () => ({ auth }) }));
vi.mock("./config", () => ({ config: { supabaseUrl: "https://example.test", supabaseAnonKey: "public" } }));

const session = { user: { id: "player-1" }, access_token: "session-token" } as Session;

beforeEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
});

describe("anonymous sign-in", () => {
  it("shares a delayed first sign-in across concurrent startup effects", async () => {
    let finishSignIn!: (value: { data: { session: Session }; error: null }) => void;
    auth.getSession.mockResolvedValue({ data: { session: null }, error: null });
    auth.signInAnonymously.mockImplementation(() => new Promise((resolve) => { finishSignIn = resolve; }));
    const { ensureSession } = await import("./supabase");
    const first = ensureSession();
    const remount = ensureSession();
    expect(first).toBe(remount);
    await vi.waitFor(() => expect(auth.signInAnonymously).toHaveBeenCalledTimes(1));
    finishSignIn({ data: { session }, error: null });
    expect(await first).toBe(session);
    expect(await remount).toBe(session);
    expect(auth.getSession).toHaveBeenCalledTimes(1);
  });

  it("uses a saved session without creating another account", async () => {
    auth.getSession.mockResolvedValue({ data: { session }, error: null });
    const { ensureSession } = await import("./supabase");
    expect(await ensureSession()).toBe(session);
    expect(auth.signInAnonymously).not.toHaveBeenCalled();
  });

  it("preserves restore failures and allows a later retry", async () => {
    const error = { message: "Could not refresh session" };
    auth.getSession.mockResolvedValueOnce({ data: { session: null }, error });
    const { ensureSession } = await import("./supabase");
    await expect(ensureSession()).rejects.toBe(error);
    expect(auth.signInAnonymously).not.toHaveBeenCalled();
    auth.getSession.mockResolvedValueOnce({ data: { session }, error: null });
    expect(await ensureSession()).toBe(session);
  });

  it("rejects a changed identity instead of syncing another player's cached database", async () => {
    auth.getSession.mockResolvedValue({ data: { session }, error: null });
    const { requireSession } = await import("./supabase");
    await expect(requireSession("player-2")).rejects.toThrow("Your sign-in changed");
    expect(auth.signInAnonymously).not.toHaveBeenCalled();
  });

  it("does not create another anonymous account when sync loses its session", async () => {
    auth.getSession.mockResolvedValue({ data: { session: null }, error: null });
    const { requireSession } = await import("./supabase");
    await expect(requireSession("player-1")).rejects.toThrow("Your sign-in changed");
    expect(auth.signInAnonymously).not.toHaveBeenCalled();
  });

  it("deduplicates each client's startup independently without borrowing the dashboard identity", async () => {
    const virtualSession = { user: { id: "virtual-player" }, access_token: "virtual-token" } as Session;
    auth.getSession.mockResolvedValue({ data: { session }, error: null });
    const virtualAuth = {
      getSession: vi.fn().mockResolvedValue({ data: { session: virtualSession }, error: null }),
      signInAnonymously: vi.fn(),
    };
    const virtualClient = { auth: virtualAuth } as unknown as SupabaseClient;
    const { ensureSession, requireSession } = await import("./supabase");
    const dashboard = ensureSession();
    const emulator = ensureSession(virtualClient);
    expect(ensureSession(virtualClient)).toBe(emulator);
    expect(emulator).not.toBe(dashboard);
    expect(await dashboard).toBe(session);
    expect(await emulator).toBe(virtualSession);
    expect(await requireSession("virtual-player", virtualClient)).toBe(virtualSession);
    await expect(requireSession("player-1", virtualClient)).rejects.toThrow("Your sign-in changed");
    expect(virtualAuth.signInAnonymously).not.toHaveBeenCalled();
  });
});
