import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { Lobby, RoomPage } from "./App";

const state = vi.hoisted(() => ({ rooms: [] as Record<string, unknown>[], players: [] as Record<string, unknown>[] }));
vi.mock("@powersync/react", () => ({
  PowerSyncContext: {},
  useQuery: (query: string) => ({
    data: query.includes("FROM rooms") ? state.rooms : query.includes("FROM room_players") ? state.players : [],
    isLoading: false,
    error: null,
  }),
  useStatus: () => ({ hasSynced: true }),
  useSyncStream: () => ({ subscription: { hasSynced: true } }),
}));
vi.mock("./lib/database", () => ({ openUserDatabase: vi.fn(), startSync: vi.fn() }));
vi.mock("./lib/supabase", () => ({ ensureSession: vi.fn() }));
vi.mock("./lib/rooms", () => ({
  archiveRoom: vi.fn(), createRoom: vi.fn(), joinRoom: vi.fn(), leaveRoom: vi.fn(),
  rememberedCode: () => null, rememberCode: vi.fn(),
}));

function room(status = "waiting", host = "host") {
  return {
    id: "room-123", host_user_id: host, status, cols: 5, rows: 4,
    current_seat: 1, score_p1: 0, score_p2: 0, winner: null,
    move_count: 0, player_count: 2, paired_count: 2,
  };
}

function page(userId = "host", viewOnly = false) {
  return renderToStaticMarkup(<RoomPage userId={userId} roomId="room-123" viewOnly={viewOnly} go={() => {}} />);
}

beforeEach(() => {
  state.rooms = [room()];
  state.players = [];
  vi.stubGlobal("localStorage", { getItem: () => "1" });
});

describe("room archive access", () => {
  it.each(["waiting", "playing", "finished"])("lets the owner archive a %s room without relying on a player seat", (status) => {
    state.rooms = [room(status)];
    expect(page()).toContain(">Archive room</button>");
  });

  it("lets the owner archive while watching their room", () => {
    expect(page("host", true)).toContain(">Archive room</button>");
  });

  it("keeps archive off a guest's page even when they have seat one", () => {
    state.players = [{ room_id: "room-123", user_id: "guest", seat: 1 }];
    const html = page("guest");
    expect(html).not.toContain("Archive room");
    expect(html).toContain(">Leave room</button>");
    expect(page("guest", true)).not.toContain(">Leave room</button>");
  });

  it("only adds an archive control to owned lobby cards", () => {
    state.rooms = [room("playing"), { ...room("waiting", "other"), id: "another-room" }];
    const html = renderToStaticMarkup(<Lobby userId="host" go={() => {}} />);
    expect(html.match(/>Archive room<\/button>/g)).toHaveLength(1);
    expect(html).toContain("ANOTHE");
  });

  it("shows the archived state without a stale board or pairing code", () => {
    state.rooms = [room("cancelled")];
    state.players = [{ room_id: "room-123", user_id: "host", seat: 1, pairing_code: "ABCAB" }];
    const html = page();
    expect(html).toContain("Room archived");
    expect(html).toContain("badges have been disconnected");
    expect(html).not.toContain("ABCAB");
    expect(html).not.toContain("Archive room");
  });

  it("explains a closed room when sync removes it", () => {
    state.rooms = [];
    const html = page();
    expect(html).toContain("Room unavailable");
    expect(html).toContain("Archived rooms disconnect every badge and cannot be rejoined");
    expect(html).toContain("Back to rooms");
  });
});
