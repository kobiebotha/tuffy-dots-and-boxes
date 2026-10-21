import { PowerSyncContext, useQuery, useStatus, useSyncStream } from "@powersync/react";
import type { PowerSyncDatabase } from "@powersync/web";
import type { SupabaseClient } from "@supabase/supabase-js";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { BoardView } from "./components/Board";
import { groupCode, replay, type Move } from "./game";
import { openUserDatabase, startSync } from "./lib/database";
import { emulatorClient, emulatorDeviceId, emulatorProfile, isPermanentMoveError, pairEmulator, readPendingMove, sameEdge, savePendingMove, sendEmulatorMove, type PendingMove } from "./lib/emulator";
import { cancelRoom, createRoom, joinRoom, leaveRoom, rememberedCode, rememberCode, type RoomTicket } from "./lib/rooms";
import { ensureSession } from "./lib/supabase";
import { connectionSummary, errorMessage } from "./lib/syncStatus";

interface Room {
  id: string;
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
interface Player { room_id: string; user_id: string; seat: number; pairing_code: string | null; device_id: string | null; paired_at: string | null }
interface StoredMove extends Move { id: string; move_index: number }
interface EmulatorSession { client: SupabaseClient; userId: string; deviceId: string; profile: string; db: PowerSyncDatabase }

export function EmulatorApp() {
  const [application, setApplication] = useState<EmulatorSession | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    async function open() {
      const profile = emulatorProfile(window.location.search);
      const client = emulatorClient(profile);
      const session = await ensureSession(client);
      const deviceId = emulatorDeviceId(profile);
      if (!alive) return;
      const db = await openUserDatabase(session.user.id);
      if (!alive) return;
      setApplication({ client, userId: session.user.id, db, deviceId, profile });
      void startSync(db, session.user.id, client).catch((reason: unknown) => {
        if (alive) setConnectionError(errorMessage(reason));
      });
    }
    void open().catch((reason: unknown) => { if (alive) setError(errorMessage(reason)); });
    return () => { alive = false; };
  }, []);
  if (error) return <main className="shell"><h1>Virtual Tufty</h1><p role="alert" className="error">{error}</p><a href="/emulate">Open default badge</a></main>;
  if (!application) return <main className="shell"><p role="status">Starting your virtual Tufty…</p></main>;
  return <PowerSyncContext.Provider value={application.db}><EmulatorShell application={application} connectionError={connectionError} /></PowerSyncContext.Provider>;
}

function EmulatorShell({ application, connectionError }: { application: EmulatorSession; connectionError: string | null }) {
  const { userId, client, profile } = application;
  const status = useStatus();
  const connection = connectionSummary(status);
  const { data: roomRows, error: roomError } = useQuery<Room>("SELECT * FROM rooms WHERE status IN ('waiting', 'playing') ORDER BY created_at DESC");
  const { data: memberships, error: playerError } = useQuery<Player>("SELECT * FROM room_players WHERE user_id = ?", [userId]);
  const [selection, setSelection] = useState<string | null>(() => new URLSearchParams(window.location.search).get("room"));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const rooms = roomRows ?? [];
  const active = memberships?.find((player) => rooms.some((room) => room.id === player.room_id));
  const roomId = selection ?? active?.room_id ?? "";
  const syncError = connection.error ?? connectionError ?? (roomError || playerError ? errorMessage(roomError ?? playerError) : null);

  function openRoom(id: string) {
    setSelection(id);
    const url = new URL(window.location.href);
    if (id) url.searchParams.set("room", id);
    else url.searchParams.delete("room");
    window.history.replaceState(null, "", url);
  }

  async function enter(action: () => Promise<RoomTicket>) {
    setBusy(true);
    setError(null);
    try {
      const ticket = await action();
      rememberCode(ticket.room_id, ticket.pairing_code, userId);
      openRoom(ticket.room_id);
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  }

  return (
    <main className="shell emulator">
      <header className="top">
        <div><p className="kicker">Second badge{profile === "default" ? "" : ` · ${profile}`}</p><h1>Virtual Tufty</h1></div>
        <nav><a href="/" target="_blank" rel="noreferrer">Dashboard ↗</a><span className={connection.live && !connectionError ? "pill live" : "pill"} role="status">{connectionError ? "Sync problem" : connection.label}</span></nav>
      </header>
      {syncError ? <aside className="note"><p className="error" role="alert">{syncError}</p><button onClick={() => window.location.reload()}>Reconnect</button></aside> : null}
      {roomId ? <EmulatorRoom key={roomId} application={application} roomId={roomId} back={() => openRoom("")} /> : (
        <section>
          <div className="hero">
            <p>This virtual badge has its own player account. Create a room on the dashboard for your physical Tufty, then join it here.</p>
            <div className="actions"><button className="primary" disabled={busy || Boolean(active) || !connection.live} onClick={() => void enter(() => createRoom(client))}>Create a room</button>{active ? <button onClick={() => openRoom(active.room_id)}>Return to your game</button> : null}</div>
            <p className="muted">You can also host here and join from the dashboard. Both badges must pair before the first turn.</p>
          </div>
          <h2>Open rooms</h2>
          <ul className="room-list">
            {rooms.map((room) => <li key={room.id}><div><strong>{room.status === "waiting" ? "Waiting" : "Playing"}</strong><span>{room.id.slice(0, 8)} · {room.player_count}/2 players · {room.paired_count} paired</span></div><div className="actions">{active?.room_id === room.id ? <button onClick={() => openRoom(room.id)}>Open</button> : room.status === "waiting" && room.player_count < 2 ? <button className="primary" disabled={busy || Boolean(active) || !connection.live} onClick={() => void enter(() => joinRoom(room.id, client))}>Join as virtual badge</button> : null}<a className="button-link" href={`/watch/${room.id}`} target="_blank" rel="noreferrer">Watch ↗</a></div></li>)}
          </ul>
          {!rooms.length ? <p role="status">{status.hasSynced ? "No open rooms. Create one here or on the dashboard." : "Waiting for live rooms…"}</p> : null}
          {error ? <p className="error" role="alert">{error}</p> : null}
        </section>
      )}
    </main>
  );
}

function EmulatorRoom({ application, roomId, back }: { application: EmulatorSession; roomId: string; back: () => void }) {
  const { client, userId, deviceId } = application;
  const parameters = useMemo(() => ({ room_id: roomId }), [roomId]);
  const stream = useSyncStream({ name: "watch_room", parameters, ttl: 60 });
  const status = useStatus();
  const { data: rooms, error: roomError } = useQuery<Room>("SELECT * FROM rooms WHERE id = ?", [roomId]);
  const { data: players, error: playerError } = useQuery<Player>("SELECT * FROM room_players WHERE room_id = ? AND user_id = ?", [roomId, userId]);
  const { data: moveRows, error: moveError } = useQuery<StoredMove>("SELECT * FROM moves WHERE room_id = ? ORDER BY move_index", [roomId]);
  const room = rooms?.[0];
  const me = players?.[0];
  const moves = moveRows ?? [];
  const [error, setError] = useState<string | null>(null);
  const [pairing, setPairing] = useState(false);
  const [pairRequested, setPairRequested] = useState(false);
  const [pending, setPending] = useState<PendingMove | null>(() => readPendingMove(userId, roomId));
  const [sending, setSending] = useState(false);
  const [notice, setNotice] = useState<string | null>(pending ? "Recovered a saved move. Retry it to safely confirm the result." : null);
  const [autoPlay, setAutoPlay] = useState(false);
  const [selectedEdge, setSelectedEdge] = useState<Move | undefined>();
  const inFlight = useRef(false);
  const pendingRef = useRef(pending);
  const code = me?.pairing_code || rememberedCode(roomId, userId);
  const pairedHere = Boolean(me?.paired_at && me.device_id === deviceId);
  const live = connectionSummary(status).live;
  const game = useMemo(() => room ? replay(room.cols, room.rows, moveRows ?? []) : null, [room, moveRows]);
  const freeEdges: Move[] = [];
  if (game) {
    for (let row = 0; row <= game.rows; row++) for (let col = 0; col < game.cols; col++) if (!game.edgeOwner("h", row, col)) freeEdges.push({ orientation: "h", row, col });
    for (let row = 0; row < game.rows; row++) for (let col = 0; col <= game.cols; col++) if (!game.edgeOwner("v", row, col)) freeEdges.push({ orientation: "v", row, col });
  }
  const selected = freeEdges.find((edge) => sameEdge(selectedEdge, edge)) ?? freeEdges[0];
  const canPlay = Boolean(room?.status === "playing" && pairedHere && me?.seat === room.current_seat &&
    room.move_count === moves.length && stream?.subscription.hasSynced && live && !pending && !sending);
  const queryError = roomError ?? playerError ?? moveError;

  useEffect(() => {
    if (pending && moves.some((move) => move.id === pending.id)) {
      savePendingMove(userId, roomId, null);
      pendingRef.current = null;
      setPending(null);
      setNotice("Move confirmed by PowerSync.");
      setError(null);
    }
  }, [moveRows, pending, roomId, userId]);

  const send = useCallback(async (edge?: Move) => {
    if (inFlight.current || (!pendingRef.current && (!edge || !canPlay))) return;
    inFlight.current = true;
    setSending(true);
    setError(null);
    try {
      const move = pendingRef.current ?? { id: crypto.randomUUID(), roomId, orientation: edge!.orientation, row: edge!.row, col: edge!.col };
      // Save before the request. A lost response or reload must reuse this UUID.
      savePendingMove(userId, roomId, move);
      pendingRef.current = move;
      setPending(move);
      setNotice("Sending move…");
      await sendEmulatorMove(client, userId, move);
      if (pendingRef.current?.id === move.id) setNotice("Server accepted the move. Waiting for PowerSync confirmation…");
    } catch (reason) {
      setAutoPlay(false);
      if (isPermanentMoveError(reason)) {
        savePendingMove(userId, roomId, null);
        pendingRef.current = null;
        setPending(null);
        setNotice("The server rejected this move. The board will catch up through PowerSync.");
      } else setNotice("Move saved. Retry when connected; it will use the same move ID.");
      setError(errorMessage(reason));
    } finally { inFlight.current = false; setSending(false); }
  }, [canPlay, client, roomId, userId]);

  useEffect(() => {
    if (!autoPlay || !canPlay || !selected) return;
    const timeout = window.setTimeout(() => void send(selected), 500);
    return () => window.clearTimeout(timeout);
  }, [autoPlay, canPlay, selected?.orientation, selected?.row, selected?.col, send]);

  async function pair() {
    if (!code || pairing || !me) return;
    setPairing(true);
    setError(null);
    try {
      await pairEmulator(client, userId, roomId, code, deviceId);
      setPairRequested(true);
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setPairing(false); }
  }

  async function leave() {
    if (!me || pending || inFlight.current) return;
    setError(null);
    try {
      await (me.seat === 1 ? cancelRoom(roomId, client) : leaveRoom(roomId, client));
      back();
    } catch (reason) { setError(errorMessage(reason)); }
  }

  function cycle(delta: number) {
    if (!selected || !freeEdges.length) return;
    const index = freeEdges.findIndex((edge) => sameEdge(selected, edge));
    setSelectedEdge(freeEdges[(index + delta + freeEdges.length) % freeEdges.length]);
  }

  if (queryError) return <section><p className="error" role="alert">{errorMessage(queryError)}</p><button onClick={back}>All rooms</button></section>;
  if (!room) return <section><h2>{stream?.subscription.hasSynced ? "Room unavailable" : "Opening the board…"}</h2><button onClick={back}>All rooms</button></section>;
  const finished = room.status === "finished";
  const turn = finished ? (room.winner === 0 ? "A draw!" : `Player ${room.winner} wins!`) : room.status === "waiting" ? "Waiting for both badges" : canPlay ? "Your turn" : `Player ${room.current_seat}'s turn`;

  return (
    <section className="room emulator-room">
      <div className="emulator-room-top"><div><p className="kicker">{me ? `You are player ${me.seat}` : "Spectating"} · Room {room.id.slice(0, 8)}</p><h2>{turn}</h2></div><a className="button-link" href={`/watch/${room.id}`} target="_blank" rel="noreferrer">Watch on dashboard ↗</a></div>
      {me && !pairedHere && !finished ? <section className="code-panel"><p>{me.paired_at ? "This seat belongs to another device." : "Pair this virtual badge with its own code."}</p>{code ? <p className="code">{groupCode(code)}</p> : null}<button className="primary" disabled={!code || pairing || Boolean(me.paired_at) || !live} onClick={() => void pair()}>{pairing ? "Pairing…" : pairRequested ? "Pair again" : "Pair virtual badge"}</button>{pairRequested ? <p role="status">Pairing accepted. Waiting for the synced roster…</p> : null}</section> : null}
      <p role="status">{room.paired_count}/2 badges paired{pairedHere ? " · Virtual badge ready" : ""}. {room.status === "waiting" ? "Enter the other player's code on the physical Tufty." : `${room.move_count}/49 lines played.`}</p>
      <div className="tufty-screen" tabIndex={0} role="region" aria-label="Virtual badge controls" onKeyDown={(event) => {
        if (event.target !== event.currentTarget) return;
        const key = event.key.toLowerCase();
        if (["a", "c", "b", "arrowleft", "arrowright", "enter"].includes(key)) {
          event.preventDefault();
          if (key === "a" || key === "arrowleft") cycle(-1);
          else if (key === "c" || key === "arrowright") cycle(1);
          else if (canPlay && selected) void send(selected);
        }
      }}>
        <div className="emulator-scores"><span className={room.current_seat === 1 && !finished ? "active" : ""}>P1 <strong>{room.score_p1}</strong></span><span className={room.current_seat === 2 && !finished ? "active" : ""}>P2 <strong>{room.score_p2}</strong></span></div>
        <BoardView cols={room.cols} rows={room.rows} moves={moves} onEdge={(edge) => void send(edge)} disabled={!canPlay} selected={selected} />
        <div className="badge-buttons"><button disabled={!freeEdges.length || sending} onClick={() => cycle(-1)}>A · Previous</button><button className="primary" disabled={!canPlay || !selected} onClick={() => selected && void send(selected)}>B · Claim line</button><button disabled={!freeEdges.length || sending} onClick={() => cycle(1)}>C · Next</button></div>
      </div>
      <p className="muted">Click a free line, or focus the badge and use A / B / C (← / Enter / →). Yellow marks the selected line.</p>
      {pending ? <div className="note"><p role="status">{notice}</p><button disabled={sending || !live} onClick={() => void send()}>{sending ? "Sending…" : "Retry saved move"}</button></div> : notice ? <p role="status">{notice}</p> : null}
      {pairedHere && !finished ? <label className="auto-play"><input type="checkbox" checked={autoPlay} onChange={(event) => setAutoPlay(event.target.checked)} /> Auto-play my turns <span className="muted">(one move every ½ second, after sync confirmation)</span></label> : null}
      <div className="actions"><button onClick={back}>All rooms</button>{me && room.status === "waiting" ? <button disabled={pairing || Boolean(pending)} onClick={() => void leave()}>{me.seat === 1 ? "Cancel room" : "Leave room"}</button> : null}</div>
      {error ? <p className="error" role="alert">{error}</p> : null}
    </section>
  );
}
