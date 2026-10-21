import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SupabaseClient } from "@supabase/supabase-js";
import { archiveRoom } from "./rooms";

const mock = vi.hoisted(() => ({ rpc: vi.fn() }));
vi.mock("./supabase", () => ({ supabase: { rpc: mock.rpc } }));

beforeEach(() => {
  mock.rpc.mockReset();
  mock.rpc.mockResolvedValue({ data: null, error: null });
});

describe("archiving rooms", () => {
  it("sends the selected room to the authenticated archive endpoint", async () => {
    await archiveRoom("room-123");
    expect(mock.rpc).toHaveBeenCalledWith("archive_room", { target: "room-123" });
  });

  it("preserves server rejections so the control can show an error and retry", async () => {
    mock.rpc.mockResolvedValueOnce({ data: null, error: { message: "Only the host can archive this room" } });
    await expect(archiveRoom("room-123")).rejects.toThrow("Only the host can archive this room");
    await archiveRoom("room-123");
    expect(mock.rpc).toHaveBeenCalledTimes(2);
  });

  it("uses an explicitly supplied client instead of borrowing the portal identity", async () => {
    const rpc = vi.fn().mockResolvedValue({ data: null, error: null });
    await archiveRoom("room-123", { rpc } as unknown as SupabaseClient);
    expect(rpc).toHaveBeenCalledWith("archive_room", { target: "room-123" });
    expect(mock.rpc).not.toHaveBeenCalled();
  });
});
