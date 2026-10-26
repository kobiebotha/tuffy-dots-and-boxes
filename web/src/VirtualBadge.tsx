import { PowerSyncContext, useQuery, useStatus } from "@powersync/react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { EmulatorRoom, openEmulatorSession, type EmulatorSession } from "./Emulator";
import { startSync } from "./lib/database";
import { connectionSummary, errorMessage } from "./lib/syncStatus";
import { joinAndPairEmulator } from "./lib/virtualBadge";

interface Props {
  roomId: string;
  canJoin: boolean;
  go: (path: string) => void;
  children: ReactNode;
}

// Keep the dashboard's identity/provider intact: it owns the physical badge's
// code and the archive action. Only this board uses the virtual player's DB.
export function VirtualBadge({ roomId, canJoin, go, children }: Props) {
  const [application, setApplication] = useState<EmulatorSession | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let alive = true;
    setError(null);
    void openEmulatorSession().then((session) => {
      if (!alive) return;
      setApplication(session);
      void startSync(session.db, session.userId, session.client).catch((reason: unknown) => {
        if (alive) setError(errorMessage(reason));
      });
    }).catch((reason: unknown) => { if (alive) setError(errorMessage(reason)); });
    return () => { alive = false; };
  }, [attempt]);

  if (!application) return <>
    {canJoin ? <div className="virtual-badge-join">
      <p>Playing with one badge? Take the other seat in this browser.</p>
      <button className="primary" disabled={!error} onClick={() => setAttempt((value) => value + 1)}>Join as virtual badge</button>
      <p role="status">{error ? "Could not start your virtual badge. Try again." : "Getting your virtual badge ready…"}</p>
    </div> : null}
    {error ? <p className="error" role="alert">{error}</p> : null}
    {children}
  </>;

  return <PowerSyncContext.Provider value={application.db}>
    <VirtualBadgeRoom application={application} roomId={roomId} canJoin={canJoin} go={go}>
      {children}
    </VirtualBadgeRoom>
    {error ? <p className="error" role="alert">{error}</p> : null}
  </PowerSyncContext.Provider>;
}

function VirtualBadgeRoom({ application, roomId, canJoin, go, children }: Props & { application: EmulatorSession }) {
  const { userId, deviceId, client, db } = application;
  const status = useStatus();
  const connection = connectionSummary(status);
  const { data: players, error: playerError } = useQuery<{
    room_id: string; paired_at: string | null; device_id: string | null;
  }>("SELECT * FROM room_players WHERE user_id = ?", [userId]);
  const { data: activeRooms, error: roomError } = useQuery<{ id: string }>(
    "SELECT id FROM rooms WHERE status IN ('waiting', 'playing')",
  );
  const me = players?.find((player) => player.room_id === roomId);
  const otherRoom = players?.find((player) => player.room_id !== roomId && activeRooms?.some((room) => room.id === player.room_id));
  const pairedHere = Boolean(me?.paired_at && me.device_id === deviceId);
  const pairedElsewhere = Boolean(me?.paired_at && me.device_id !== deviceId);
  const [busy, setBusy] = useState(false);
  const [accepted, setAccepted] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef(false);
  const problem = error ?? connection.error ?? (playerError || roomError ? errorMessage(playerError ?? roomError) : null);

  async function join() {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      await joinAndPairEmulator(client, userId, deviceId, roomId, db);
      setAccepted(true);
    } catch (reason) { setError(errorMessage(reason)); }
    finally { inFlight.current = false; setBusy(false); }
  }

  return <>
    {pairedHere ? <EmulatorRoom application={application} roomId={roomId} back={() => { setAccepted(false); go(`/room/${roomId}`); }} embedded /> : <>
      {canJoin || me ? <div className="virtual-badge-join">
        <p>{pairedElsewhere ? "This virtual player's seat is paired with another badge." : otherRoom ? "Your virtual badge is already in another room." : "Playing with one badge? Take the other seat in this browser."}</p>
        {otherRoom ? <button className="secondary" onClick={() => go(`/room/${otherRoom.room_id}`)}>Open virtual badge’s room</button> : !pairedElsewhere ? <button className="primary" disabled={busy || !connection.live} onClick={() => void join()}>
          {busy ? "Joining and pairing…" : "Join as virtual badge"}
        </button> : null}
        {accepted ? <p role="status">Pairing accepted. Waiting for your virtual badge to sync…</p> : !connection.live ? <p role="status">{connection.label}. Your virtual badge will be ready when connected.</p> : null}
      </div> : null}
      {children}
    </>}
    {problem ? <p className="error" role="alert">{problem}</p> : null}
  </>;
}
