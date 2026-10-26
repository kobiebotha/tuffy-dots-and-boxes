import type { ReactNode } from "react";

export function PageFrame({ path, go, connection, connectionError, busy = false, children }: {
  path: string;
  go: (path: string) => void;
  connection?: { live: boolean; label: string };
  connectionError?: string | null;
  busy?: boolean;
  children: ReactNode;
}) {
  const install = path === "/install";
  const inRoom = path.startsWith("/room/") || path.startsWith("/watch/");
  return (
    <main className="shell">
      <a className="skip-link" href="#portal-content">Skip to content</a>
      <header className="top">
        <a className="brand" href="/" aria-disabled={busy || undefined} onClick={(event) => { event.preventDefault(); if (!busy) go("/"); }}>
          <span className="brand-name">
            dots<span className="brand-amp">&</span>boxes<span className="brand-period">.</span>
          </span>
          <span className="brand-subtitle">THE TUFTY ARCADE</span>
        </a>
        <nav aria-label="Main navigation">
          <a
            className={!install && !inRoom ? "nav-link selected" : "nav-link"}
            aria-current={!install && !inRoom ? "page" : undefined}
            aria-disabled={busy || undefined}
            href="/"
            onClick={(event) => { event.preventDefault(); if (!busy) go("/"); }}
          >
            Lobby
          </a>
          <a
            className={install ? "nav-link selected" : "nav-link"}
            aria-current={install ? "page" : undefined}
            href="/install"
            onClick={(event) => { event.preventDefault(); go("/install"); }}
          >
            Install on badge
          </a>
          {connection ? <span className={connection.live && !connectionError ? "pill live" : "pill"} role="status">
            <span className="status-dot" />{connectionError ? "Sync problem" : connection.label}
          </span> : <span className="pill">USB INSTALLER</span>}
        </nav>
      </header>
      <div id="portal-content" tabIndex={-1}>
        {children}
      </div>
      <footer className="footer">
        <div className="powered-by">
          <span>POWERED BY</span>
          <a href="https://supabase.com" target="_blank" rel="noreferrer">
            <img src="/brand/supabase.svg" alt="" />supabase
          </a>
          <span className="footer-plus">+</span>
          <a href="https://powersync.com" target="_blank" rel="noreferrer">
            <img src="/brand/powersync.svg" alt="" />PowerSync
          </a>
        </div>
      </footer>
    </main>
  );
}

