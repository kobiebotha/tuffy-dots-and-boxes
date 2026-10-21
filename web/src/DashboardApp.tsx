import { PowerSyncContext, useQuery, useStatus, useSyncStream } from "@powersync/react";
import type { PowerSyncDatabase } from "@powersync/web";
import { useEffect, useId, useMemo, useState } from "react";
import { BoardView } from "./components/Board";
import { PageFrame } from "./components/PageFrame";
import { groupCode, type Move } from "./game";
import { openUserDatabase, startSync } from "./lib/database";
import { archiveRoom, createRoom, joinRoom, leaveRoom, rememberedCode, rememberCode } from "./lib/rooms";
import { ensureSession } from "./lib/supabase";
import { connectionSummary, errorMessage } from "./lib/syncStatus";

interface Room {
  id: string;
  host_user_id: string;
  status: string;
  cols: number;
  rows: number;
  current_seat: number;
  score_p1: number;
  score_p2: number;
  winner: number | null;
  move_count: number;
  player_count: number;
  paired_count: number;
}

interface Player {
  room_id: string;
  user_id: string;
  seat: number;
  pairing_code: string | null;
  paired_at: string | null;
}

interface StoredMove {
  room_id: string;
  seat: number;
  orientation: string;
  row: number;
  col: number;
  move_index: number;
}

export default function DashboardApp({ path, go }: { path: string; go: (path: string) => void }) {
  const [application, setApplication] = useState<{ userId: string; db: PowerSyncDatabase } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [connectionError, setConnectionError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    ensureSession()
      .then(async (session) => {
        if (!alive) return;
        const database = await openUserDatabase(session.user.id);
        if (!alive) return;
        setApplication({ userId: session.user.id, db: database });
        void startSync(database, session.user.id).catch((reason: unknown) => {
          if (alive) setConnectionError(errorMessage(reason));
        });
      })
      .catch((reason: unknown) => {
        if (alive) setError(errorMessage(reason));
      });
    return () => {
      alive = false;
    };
  }, []);

  if (error) {
    return (
      <main className="shell boot-screen">
        <p className="kicker">Dots & Boxes</p>
        <h1>Connection interrupted.</h1>
        <p className="error" role="alert">{error}</p>
        <button onClick={() => window.location.reload()}>Try again</button>
      </main>
    );
  }
  if (!application) {
    return (
      <main className="shell boot-screen">
        <p className="kicker">Dots & Boxes</p>
        <h1>Powering up<span className="loading-dots">...</span></h1>
        <p role="status">Signing you in and getting the board ready.</p>
      </main>
    );
  }
  return (
    <PowerSyncContext.Provider value={application.db}>
      <Shell userId={application.userId} connectionError={connectionError} path={path} go={go} />
    </PowerSyncContext.Provider>
  );
}

function Shell({ userId, connectionError, path, go }: {
  userId: string; connectionError: string | null; path: string; go: (path: string) => void;
}) {
  const status = useStatus();
  const connection = connectionSummary(status);
  const syncError = connection.error ?? connectionError;
  const watch = path.startsWith("/watch/");
  const room = path.startsWith("/room/");
  const roomId = watch || room ? path.split("/")[2] : "";

  return (
    <PageFrame path={path} go={go} connection={connection} connectionError={connectionError}>
      {syncError ? (
        <aside className="note" role="alert">
          <p className="error">{syncError}</p>
          <button onClick={() => window.location.reload()}>Try again</button>
        </aside>
      ) : null}
      {roomId ? (
        <RoomPage key={roomId} userId={userId} roomId={roomId} viewOnly={watch} go={go} />
      ) : <Lobby userId={userId} go={go} />}
    </PageFrame>
  );
}

export function Lobby({ userId, go }: { userId: string; go: (path: string) => void }) {
  const status = useStatus();
  const { data: roomRows, error: roomError, isLoading } = useQuery<Room>("SELECT * FROM rooms WHERE status IN ('waiting', 'playing') ORDER BY created_at DESC");
  const { data: playerRows, error: playerError } = useQuery<Player>("SELECT * FROM room_players WHERE user_id = ?", [userId]);
  const rooms = roomRows ?? [];
  const mine = playerRows ?? [];
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const active = mine.find((player) => rooms.some((room) => room.id === player.room_id));

  async function create() {
    setBusy(true);
    setError(null);
    try {
      const ticket = await createRoom();
      rememberCode(ticket.room_id, ticket.pairing_code, userId);
      go(`/room/${ticket.room_id}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not create a room");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="lobby" aria-label="Game lobby">
      <div className="hero">
        <div className="hero-copy">
          <h1>Little badges.<br /><span>Big moves.</span></h1>
          <div className="actions hero-actions">
            {active ? (
              <button className="primary" onClick={() => go(`/room/${active.room_id}`)}>
                Return to your room <span aria-hidden="true">→</span>
              </button>
            ) : (
              <button className="primary" disabled={busy} onClick={() => void create()}>
                {busy ? "Creating room…" : "Create a room"}<span aria-hidden="true">+</span>
              </button>
            )}
            <a className="text-link" href="#open-rooms">Find a game <span aria-hidden="true">↓</span></a>
          </div>
          <p className="hero-footnote">{active ? "Your board is waiting. Pick up where you left off." : "Two players · 20 boxes · Bragging rights included"}</p>
          {error ? <p className="error" role="alert">{error}</p> : null}
        </div>
        <GamePreview />
      </div>
      <ol className="quick-start" aria-label="How to play">
        <li>
          <span className="step-number">01</span>
          <div>
            <strong>Make a room</strong>
            <p>Invite a worthy opponent.</p>
          </div>
        </li>
        <li>
          <span className="step-number">02</span>
          <div>
            <strong>Pair your badges</strong>
            <p>Enter your A / B / C codes.</p>
          </div>
        </li>
        <li>
          <span className="step-number">03</span>
          <div>
            <strong>Own the board</strong>
            <p>Close the most boxes to win.</p>
          </div>
        </li>
      </ol>
      <InstallNote />
      <div className="section-heading" id="open-rooms">
        <div>
          <p className="kicker">PICK YOUR NEXT MATCH</p>
          <h2>Open rooms <span className="count">{rooms.length.toString().padStart(2, "0")}</span></h2>
        </div>
        <span className="section-caption">
          {rooms.filter((item) => item.status === "playing").length} in play <span aria-hidden="true">/</span> {rooms.filter((item) => item.status === "waiting").length} waiting
        </span>
      </div>
      {roomError || playerError ? <p className="error" role="alert">{errorMessage(roomError ?? playerError)}</p> : null}
      <ul className="room-list">
        {rooms.map((room) => (
          <li key={room.id} className="room-card">
            <div className="room-card-main">
              <span className={room.status === "waiting" ? "room-state waiting" : "room-state playing"}>
                <span className="status-dot" />
                {room.status === "waiting" ? (room.player_count < 2 ? "WAITING FOR PLAYER" : "PAIRING BADGES") : "IN PLAY"}
              </span>
              <h3>Room <span>#{room.id.slice(0, 6).toUpperCase()}</span></h3>
              <div className="room-details">
                <span>{room.player_count}/2 players</span>
                <span>{room.paired_count}/2 badges paired</span>
              </div>
            </div>
            <div className="room-card-bottom">
              <div className="mini-score" aria-label={`Player 1: ${room.score_p1}, Player 2: ${room.score_p2}`}>
                <span className="player-one">P1 <b>{String(room.score_p1).padStart(2, "0")}</b></span>
                <span className="score-divider">:</span>
                <span className="player-two"><b>{String(room.score_p2).padStart(2, "0")}</b> P2</span>
              </div>
              <div className="actions">
                {active?.room_id === room.id ? (
                  <button className="primary" onClick={() => go(`/room/${room.id}`)}>
                    Your room <span aria-hidden="true">→</span>
                  </button>
                ) : room.status === "waiting" && room.player_count < 2 && room.host_user_id !== userId && !active ? (
                  <JoinButton userId={userId} roomId={room.id} go={go} />
                ) : null}
                <button className="secondary" onClick={() => go(`/watch/${room.id}`)}>
                  Watch <span aria-hidden="true">↗</span>
                </button>
              </div>
            </div>
            {room.host_user_id === userId ? <ArchiveRoomControl roomId={room.id} onArchived={() => go("/")} /> : null}
          </li>
        ))}
      </ul>
      {rooms.length === 0 && !roomError ? (
        <div className="empty-state" role="status">
          <span className="kicker">{isLoading || !status.hasSynced ? "SCANNING THE ARCADE" : "THE BOARD IS YOURS"}</span>
          <h3>{isLoading || !status.hasSynced ? "Finding live rooms…" : "Be the first to make a move."}</h3>
          <p>{isLoading || !status.hasSynced ? "Rooms will appear as soon as sync completes." : "Create a room above, grab a friend, and get those badges ready."}</p>
        </div>
      ) : null}
    </section>
  );
}

function JoinButton({ userId, roomId, go }: { userId: string; roomId: string; go: (path: string) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <>
      <button
        className="primary"
        disabled={busy}
        onClick={() => {
          setBusy(true);
          setError(null);
          joinRoom(roomId)
            .then((ticket) => {
              rememberCode(ticket.room_id, ticket.pairing_code, userId);
              go(`/room/${ticket.room_id}`);
            })
            .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "Could not join"))
            .finally(() => setBusy(false));
        }}
      >
        {busy ? "Joining…" : "Join room"}
      </button>
      {error ? <span className="error">{error}</span> : null}
    </>
  );
}

export function RoomPage({ userId, roomId, viewOnly, go }: { userId: string; roomId: string; viewOnly: boolean; go: (path: string) => void }) {
  const parameters = useMemo(() => ({ room_id: roomId }), [roomId]);
  const stream = useSyncStream({ name: "watch_room", parameters, ttl: 60 });
  const status = useStatus();
  const { data: roomRows, isLoading, error: roomError } = useQuery<Room>("SELECT * FROM rooms WHERE id = ?", [roomId]);
  const { data: playerRows, error: playerError } = useQuery<Player>("SELECT * FROM room_players WHERE room_id = ? ORDER BY seat", [roomId]);
  const { data: moveRows, error: moveError } = useQuery<StoredMove>("SELECT * FROM moves WHERE room_id = ? ORDER BY move_index", [roomId]);
  const rooms = roomRows ?? [];
  const players = playerRows ?? [];
  const moves = moveRows ?? [];
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const room = rooms[0];
  const me = players.find((player) => player.user_id === userId);
  const code = viewOnly ? null : me?.pairing_code || rememberedCode(roomId, userId);
  const queryError = roomError ?? playerError ?? moveError;

  async function run(action: () => Promise<void>, next = "/") {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await action();
      go(next);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "That did not work");
    } finally {
      setBusy(false);
    }
  }

  if (queryError) {
    return (
      <section>
        <h1>Could not open the board</h1>
        <p className="error" role="alert">{errorMessage(queryError)}</p>
        <button onClick={() => window.location.reload()}>Try again</button>
      </section>
    );
  }

  if (!room) {
    if (status.downloadError) {
      return (
        <section>
          <h1>Sync problem</h1>
          <p>{errorMessage(status.downloadError)}</p>
          <button onClick={() => window.location.reload()}>Try again</button>
        </section>
      );
    }
    if (isLoading || !stream?.subscription?.hasSynced) {
      return (
        <section>
          <h1>Opening your room</h1>
          <p>The board is syncing.</p>
          {code ? <PairingCode code={code} paired={false} /> : null}
          <button onClick={() => go("/")}>Back to rooms</button>
        </section>
      );
    }
    return (
      <section>
        <h1>Room unavailable</h1>
        <p>It may have been archived. Archived rooms disconnect every badge and cannot be rejoined.</p>
        <button onClick={() => go("/")}>Back to rooms</button>
      </section>
    );
  }

  if (room.status === "cancelled") {
    return (
      <section>
        <h1>Room archived</h1>
        <p>This room is closed and its badges have been disconnected. Head back to the lobby for another game.</p>
        <button onClick={() => go("/")}>Back to rooms</button>
      </section>
    );
  }

  const boardMoves: Move[] = moves.flatMap((move) =>
    move.orientation === "h" || move.orientation === "v"
      ? [{ orientation: move.orientation, row: move.row, col: move.col, seat: move.seat }]
      : [],
  );

  return (
    <section className={`room ${room.status} ${viewOnly ? "spectator" : "member"}`}>
      <div className="room-heading">
        <div>
          <p className="kicker">{viewOnly ? "SPECTATOR MODE" : "YOUR MATCH"} / #{room.id.slice(0, 6).toUpperCase()}</p>
          <h1>{headline(room)}</h1>
        </div>
        <button className="secondary" onClick={() => go("/")}>
          All rooms <span aria-hidden="true">↗</span>
        </button>
      </div>
      <div className="room-layout">
        <div className="game-stage">
          <div className="stage-heading">
            <span className="kicker">THE BOARD</span>
            <span className="stage-meta">{room.cols} × {room.rows} BOXES</span>
          </div>
          <div className="scorebar">
            <Score seat={1} score={room.score_p1} active={room.status === "playing" && room.current_seat === 1} />
            <div className="match-status" role="status">
              <p className="kicker">{label(room)}</p>
              <span>{room.move_count} / {room.cols * (room.rows + 1) + (room.cols + 1) * room.rows} lines claimed</span>
            </div>
            <Score seat={2} score={room.score_p2} active={room.status === "playing" && room.current_seat === 2} />
          </div>
          <BoardView cols={room.cols} rows={room.rows} moves={boardMoves} />
          <div className="board-caption">
            <span className="player-one">P1 / GREEN</span>
            <span>{viewOnly ? "Watching the badges play" : "Make your moves on the badge"}</span>
            <span className="player-two">P2 / BLUE</span>
          </div>
        </div>
        <aside className="room-sidebar">
          {code && room.status !== "finished" ? (
            <PairingCode code={code} paired={Boolean(me?.paired_at)} />
          ) : (
            <div className="side-panel">
              <p className="kicker">{room.status === "finished" ? "THAT’S A WRAP" : "FRONT ROW SEAT"}</p>
              <h2>{room.status === "finished" ? "Good game." : "Every line counts."}</h2>
              <p>{room.status === "finished" ? "The final board is in. Head back to the lobby for your next match." : "Claim a line. Close a box for a point and another turn. Most boxes wins."}</p>
            </div>
          )}
          <div className="side-panel">
            <p className="kicker">BADGE CHECK</p>
            <div className="pairing-status">
              <strong>{room.paired_count}<span> / 2</span></strong>
              <span>badges paired</span>
            </div>
            <p>{room.status === "waiting" ? "Your match starts automatically when both badges are paired." : "Moves sync here as the badges play."}</p>
          </div>
          <div className="actions room-actions">
            {room.host_user_id === userId ? <ArchiveRoomControl roomId={room.id} onArchived={() => go("/")} /> : null}
            {!viewOnly && me && room.host_user_id !== userId && room.status === "waiting" ? (
              <button className="danger" disabled={busy} onClick={() => void run(() => leaveRoom(room.id))}>
                {busy ? "Leaving…" : "Leave room"}
              </button>
            ) : null}
          </div>
          {error ? <p className="error" role="alert">{error}</p> : null}
        </aside>
      </div>
    </section>
  );
}

function ArchiveRoomControl({ roomId, onArchived }: { roomId: string; onArchived: () => void }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const descriptionId = useId();

  async function archive() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await archiveRoom(roomId);
      onArchived();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not archive this room");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="archive-room">
      {confirming ? (
        <div className="archive-confirmation" role="group" aria-label="Archive this room" aria-describedby={descriptionId}>
          <p id={descriptionId}>Archive this room? This ends the match and disconnects every badge. The room cannot be rejoined.</p>
          <div className="actions">
            <button className="danger" disabled={busy} onClick={() => void archive()}>
              {busy ? "Archiving…" : "Confirm archive"}
            </button>
            <button className="secondary" disabled={busy} onClick={() => { setConfirming(false); setError(null); }}>
              Keep room
            </button>
          </div>
        </div>
      ) : (
        <button className="danger" onClick={() => setConfirming(true)}>Archive room</button>
      )}
      {error ? <p className="error" role="alert">{error}</p> : null}
    </div>
  );
}

function Score({ seat, score, active }: { seat: number; score: number; active: boolean }) {
  return (
    <div className={active ? `score seat-${seat} active` : `score seat-${seat}`}>
      <span>PLAYER {seat}{active ? " / TURN" : ""}</span>
      <strong>{String(score).padStart(2, "0")}</strong>
    </div>
  );
}

function PairingCode({ code, paired }: { code: string; paired: boolean }) {
  return (
    <section className="code-panel" aria-label="Pairing code">
      <p className="kicker">{paired ? "YOU’RE CONNECTED" : "YOUR PAIRING CODE"}</p>
      <h2>{paired ? "Badge ready." : "Meet your badge."}</h2>
      <p className="code">{groupCode(code)}</p>
      <p>{paired ? "Your badge is paired. Use this code again if it needs to reconnect." : "Enter this sequence with the badge’s A, B, and C buttons. Up deletes a letter."}</p>
      <span className="pairing-hint">{paired ? "READY FOR THE NEXT MOVE" : "5 LETTERS. THEN YOU’RE IN."}</span>
    </section>
  );
}

function label(room: Room): string {
  if (room.status === "finished") return room.winner === 0 ? "Draw" : `Player ${room.winner} wins`;
  if (room.status === "playing") return `Player ${room.current_seat}'s turn`;
  return "Waiting for badges";
}

function headline(room: Room): string {
  if (room.status === "finished") return "Game over";
  if (room.status === "playing") return "Live game";
  return "Pair your badge";
}

function InstallNote() {
  const [hidden, setHidden] = useState(() => localStorage.getItem("dots-saw-install") === "1");
  if (hidden) return null;
  return (
    <aside className="note install-note">
      <div>
        <span className="note-label">NEW HERE?</span>
        <p>Your badge needs the game before its first match.</p>
      </div>
      <div className="actions">
        <a className="text-link" href="/install" onClick={() => localStorage.setItem("dots-saw-install", "1")}>
          Set up your Tufty <span aria-hidden="true">→</span>
        </a>
        <button
          className="dismiss"
          aria-label="Dismiss installation tip"
          onClick={() => { localStorage.setItem("dots-saw-install", "1"); setHidden(true); }}
        >
          ×
        </button>
      </div>
    </aside>
  );
}

const PREVIEW_MOVES: Move[] = [
  { orientation: "h", row: 0, col: 0 }, { orientation: "v", row: 0, col: 0 },
  { orientation: "h", row: 1, col: 0 }, { orientation: "v", row: 0, col: 1 },
  { orientation: "h", row: 1, col: 1 }, { orientation: "v", row: 1, col: 2 },
  { orientation: "h", row: 2, col: 1 }, { orientation: "v", row: 1, col: 1 },
  { orientation: "h", row: 0, col: 2 }, { orientation: "v", row: 0, col: 3 },
  { orientation: "h", row: 2, col: 3 }, { orientation: "v", row: 2, col: 4 },
  { orientation: "h", row: 3, col: 0 }, { orientation: "h", row: 3, col: 2 },
];

function GamePreview() {
  return (
    <div className="game-preview" aria-label="Example game board">
      <div className="preview-header">
        <span className="kicker">A LITTLE FRIENDLY COMPETITION</span>
        <span className="preview-label">DEMO</span>
      </div>
      <BoardView cols={5} rows={3} moves={PREVIEW_MOVES} />
      <div className="preview-footer">
        <span className="player-one"><i />PLAYER 1</span>
        <span className="preview-versus">VS</span>
        <span className="player-two"><i />PLAYER 2</span>
      </div>
    </div>
  );
}
