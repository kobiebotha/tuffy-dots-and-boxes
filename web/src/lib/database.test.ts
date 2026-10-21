import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SupabaseClient } from "@supabase/supabase-js";

const mock = vi.hoisted(() => ({
  options: [] as unknown[],
  init: vi.fn(),
  close: vi.fn(),
  connect: vi.fn(),
  createConnector: vi.fn((userId: string) => ({ userId })),
}));
vi.mock("@powersync/web", () => ({
  PowerSyncDatabase: class {
    constructor(options: unknown) { mock.options.push(options); }
    init = mock.init;
    close = mock.close;
    connect = mock.connect;
  },
}));
vi.mock("./schema", () => ({ AppSchema: {} }));
vi.mock("./connector", () => ({ createConnector: mock.createConnector }));

beforeEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
  mock.options.length = 0;
  mock.init.mockResolvedValue(undefined);
  mock.close.mockResolvedValue(undefined);
  mock.connect.mockResolvedValue(undefined);
});

describe("database startup", () => {
  it("shares database initialization across StrictMode remounts", async () => {
    const { openUserDatabase, startSync } = await import("./database");
    const first = openUserDatabase("player-1");
    const remount = openUserDatabase("player-1");
    expect(first).toBe(remount);
    const database = await first;
    expect(mock.init).toHaveBeenCalledTimes(1);
    expect(mock.connect).not.toHaveBeenCalled();
    const connection = startSync(database, "player-1");
    expect(startSync(database, "player-1")).toBe(connection);
    await connection;
    expect(mock.connect).toHaveBeenCalledTimes(1);
    expect(mock.createConnector).toHaveBeenCalledWith("player-1");
  });

  it("keeps local startup available while the network connection is pending", async () => {
    mock.connect.mockReturnValue(new Promise(() => {}));
    const { openUserDatabase, startSync } = await import("./database");
    const database = await openUserDatabase("player-1");
    startSync(database, "player-1");
    expect(await openUserDatabase("player-1")).toBe(database);
    expect(mock.close).not.toHaveBeenCalled();
  });

  it("closes a failed initialization and retries with a new handle", async () => {
    mock.init.mockRejectedValueOnce(new Error("SQLite worker unavailable"));
    const { openUserDatabase } = await import("./database");
    await expect(openUserDatabase("player-1")).rejects.toThrow("SQLite worker unavailable");
    expect(mock.close).toHaveBeenCalledTimes(1);
    await openUserDatabase("player-1");
    expect(mock.init).toHaveBeenCalledTimes(2);
  });

  it("isolates cached membership and pairing data by anonymous identity", async () => {
    const { openUserDatabase } = await import("./database");
    const player1 = await openUserDatabase("player-1");
    const player2 = await openUserDatabase("player-2");
    expect(player1).not.toBe(player2);
    expect(mock.options).toEqual([
      {
        schema: {},
        database: { dbFilename: "dots-and-boxes-player-1.db", worker: "/assets/powersync/@powersync/worker.js" },
        sync: { worker: "/assets/powersync/@powersync/worker.js" },
      },
      {
        schema: {},
        database: { dbFilename: "dots-and-boxes-player-2.db", worker: "/assets/powersync/@powersync/worker.js" },
        sync: { worker: "/assets/powersync/@powersync/worker.js" },
      },
    ]);
  });

  it("exposes connection startup failure to its caller", async () => {
    mock.connect.mockRejectedValueOnce(new Error("Sync worker unavailable"));
    const { openUserDatabase, startSync } = await import("./database");
    const database = await openUserDatabase("player-1");
    await expect(startSync(database, "player-1")).rejects.toThrow("Sync worker unavailable");
  });

  it("connects the emulator database with its own auth client", async () => {
    const client = {} as SupabaseClient;
    const { openUserDatabase, startSync } = await import("./database");
    const database = await openUserDatabase("virtual-player");
    await startSync(database, "virtual-player", client);
    expect(mock.createConnector).toHaveBeenCalledWith("virtual-player", client);
  });
});
