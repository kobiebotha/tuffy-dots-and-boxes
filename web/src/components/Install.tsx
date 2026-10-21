import { useEffect, useRef, useState } from "react";
import {
  chooseBadgeDrive, copyApp, supportsAppCopy, validateBadgeDrive,
  type AppDirectoryHandle, type CopyProgress,
} from "../installer/appCopy";
import { loadAppRelease, prepareApp, type PreparedApp } from "../installer/appRelease";

type ReleaseState = "loading" | "ready" | "error";
type SelectedDrive = { root: AppDirectoryHandle; update: boolean };

function describeError(error: unknown): string {
  if (error instanceof Error) {
    if (error.name === "AbortError") return "No drive selected. Choose your badge drive when you’re ready.";
    if (error.name === "NotAllowedError" || error.name === "SecurityError") {
      return "Folder access wasn’t allowed. Choose the badge drive and allow access, or use the ZIP download.";
    }
    return error.message;
  }
  return "The badge drive couldn’t be reached. Reconnect your badge and try again.";
}

export function Install({ onBusyChange }: { onBusyChange: (busy: boolean) => void }) {
  const [supported] = useState(supportsAppCopy);
  const [releaseState, setReleaseState] = useState<ReleaseState>("loading");
  const [prepared, setPrepared] = useState<PreparedApp | null>(null);
  const [releaseError, setReleaseError] = useState("");
  const [retry, setRetry] = useState(0);
  const [drive, setDrive] = useState<SelectedDrive | null>(null);
  const [operation, setOperation] = useState<"choosing" | "copying" | null>(null);
  const [progress, setProgress] = useState<CopyProgress | null>(null);
  const [installError, setInstallError] = useState("");
  const [complete, setComplete] = useState(false);
  const operationRef = useRef<typeof operation>(null);
  const mounted = useRef(false);
  const copying = operation === "copying";

  useEffect(() => {
    mounted.current = true;
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (operationRef.current !== "copying") return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", beforeUnload);
    return () => {
      mounted.current = false;
      window.removeEventListener("beforeunload", beforeUnload);
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let current = true;
    setReleaseState("loading");
    setPrepared(null);
    setReleaseError("");
    void (async () => {
      try {
        const release = await loadAppRelease(controller.signal);
        if (!release) throw new Error("The game package isn’t available yet. Please try again later.");
        const next = await prepareApp(release, controller.signal);
        if (current) { setPrepared(next); setReleaseState("ready"); }
      } catch (error) {
        if (current) { setReleaseError(describeError(error)); setReleaseState("error"); }
      }
    })();
    return () => { current = false; controller.abort(); };
  }, [retry]);

  async function chooseDrive() {
    if (operationRef.current || !prepared || !supported) return;
    operationRef.current = "choosing";
    setOperation("choosing");
    setDrive(null);
    setComplete(false);
    setInstallError("");
    try {
      // Keep the native picker in the click's user activation, before any awaits.
      const root = await chooseBadgeDrive();
      const { existing } = await validateBadgeDrive(root);
      if (mounted.current) setDrive({ root, update: existing });
    } catch (error) {
      if (mounted.current) setInstallError(describeError(error));
    } finally {
      operationRef.current = null;
      if (mounted.current) setOperation(null);
    }
  }

  async function install() {
    if (operationRef.current || !drive || !prepared) return;
    operationRef.current = "copying";
    setOperation("copying");
    onBusyChange(true);
    setInstallError("");
    setComplete(false);
    setProgress({ completed: 0, total: prepared.files.length, path: "" });
    try {
      await copyApp(drive.root, prepared, (next) => {
        if (mounted.current) setProgress(next);
      }, { update: drive.update });
      if (mounted.current) setComplete(true);
    } catch (error) {
      if (mounted.current) setInstallError(describeError(error));
    } finally {
      operationRef.current = null;
      onBusyChange(false);
      if (mounted.current) { setOperation(null); setDrive(null); }
    }
  }

  return (
    <section className="install" aria-labelledby="install-heading">
      <p className="kicker">FIRST, A LITTLE BADGE MAGIC</p>
      <h1 id="install-heading">New badge.<br /><span>Meet your next game.</span></h1>
      <p className="install-intro">First set up Wi-Fi, then copy the game onto your badge.</p>
      <section className="install-wifi install-steps" aria-labelledby="wifi-heading">
        <h2 id="wifi-heading">1. Connect your badge to Wi-Fi</h2>
        <div className="install-wifi-layout">
          <ol>
            <li>Plug your Tufty 2350 into your computer with a USB data cable. Double-tap the rear <strong>Reset</strong> button to enter disk mode.</li>
            <li>Open the badge’s USB drive on your computer. Open <code>secrets.py</code> in a text editor; it’s at the top level of the drive, alongside the <code>apps</code> folder.</li>
            <li>Enter your network name (SSID) in <code>WIFI_SSID</code> and your password in <code>WIFI_PASSWORD</code>. Replace the values between the quotes and keep the rest of the file as it is.</li>
            <li>Save <code>secrets.py</code> and leave the badge in disk mode. You can install the game below before ejecting.</li>
          </ol>
          <div className="install-wifi-example">
            <p className="kicker">IN SECRETS.PY</p>
            <pre><code>{'WIFI_SSID = "Your network name"\nWIFI_PASSWORD = "Your Wi-Fi password"'}</code></pre>
            <p>Already set up Wi-Fi? Put your badge in disk mode, then continue to step 2.</p>
            <a className="text-link" href="https://github.com/pimoroni/tufty2350#configuring-wifi" target="_blank" rel="noreferrer">Badgeware Wi-Fi guide <span aria-hidden="true">↗</span></a>
          </div>
        </div>
        <p className="install-tip">Seeing a drive called <code>RP2350</code>? Press Reset once, then double-tap Reset again to open the Badgeware file drive.</p>
      </section>
      <section className="install-layout install-app" aria-labelledby="app-heading">
        <div className="install-steps">
          <h2 id="app-heading">2. Install Dots &amp; Boxes</h2>
          <ol>
            {supported ? (
              <li>With your badge still in disk mode, click <strong>Choose badge drive</strong> in the install panel and select the badge’s drive. Then click <strong>Copy game to badge</strong> (or <strong>Update game on badge</strong>). Keep the badge connected and this page open until verification finishes.</li>
            ) : (
              <li>Click <strong>Download game ZIP</strong> in the install panel and unzip the download. Copy the extracted <code>dots_boxes</code> folder from <code>apps/dots_boxes</code> into the badge drive’s existing <code>apps</code> folder.</li>
            )}
            <li>Safely eject the drive from your computer, press <strong>Reset</strong> once, then open <strong>Dots &amp; Boxes</strong> from the launcher.</li>
            <li>Create or join a room on this site, then enter its five-letter pairing code with the badge’s A, B, and C buttons.</li>
          </ol>
          <div className="control-notes">
            <p className="kicker">KNOW YOUR BUTTONS</p>
            <p><kbd>A</kbd> <kbd>B</kbd> <kbd>C</kbd> Enter your pairing code</p>
            <p><kbd>UP</kbd> Move up in a game, or delete the last code letter</p>
            <p><kbd>HOLD UP 2s</kbd> Leave the room, disconnect this badge, and return to pairing</p>
            <p><kbd>HOME</kbd> Exit the app and stay paired</p>
            <p className="muted">The fifth letter sends your code automatically.</p>
          </div>
        </div>
        <aside className="download-panel" aria-labelledby="copy-heading">
          <p className="kicker">{supported ? "STRAIGHT TO YOUR BADGE" : "INSTALL WITH A ZIP"}</p>
          <h2 id="copy-heading">{supported ? <>One small copy.<br />A whole new game.</> : "Copy the game manually"}</h2>
          <p>{supported ? "Install directly from your browser. Your other apps and saved game data stay in place." : "Download the ZIP, then copy the game files onto your badge."}</p>
          <div className="install-action">
            {!supported ? (
              <p className="install-status">Direct copying is unavailable in this browser, so the drive picker isn’t shown. Use the ZIP below, or open this page in desktop Chrome or Edge for automatic copying.</p>
            ) : releaseState === "loading" ? (
              <p className="install-status" role="status">Downloading and checking the game files…</p>
            ) : releaseState === "error" ? (
              <>
                <p className="error" role="alert">{releaseError}</p>
                <button onClick={() => setRetry((value) => value + 1)}>Check game files again</button>
              </>
            ) : complete ? (
              <div className="install-success" role="status">
                <h3>Game copied and verified</h3>
                <p>Safely eject the Badgeware drive from your computer, press Reset once, then open Dots &amp; Boxes in the launcher.</p>
                <button className="secondary" onClick={() => void chooseDrive()}>Install on another badge</button>
              </div>
            ) : copying ? (
              <div className="install-progress" role="status">
                <p>Copying and verifying…</p>
                <progress aria-label="Game files copied and verified" value={progress?.completed ?? 0} max={progress?.total ?? 1} />
                <p>{progress?.completed ?? 0} of {progress?.total ?? 0} files verified</p>
                <p className="install-file">{progress?.path || "Preparing the badge drive…"}</p>
                <p>Keep your badge connected and this page open.</p>
              </div>
            ) : drive ? (
              <>
                <p className="install-selected">Selected drive: <strong>{drive.root.name}</strong></p>
                {drive.update ? <p className="install-status">Dots &amp; Boxes is already here. Update its app files and keep your saved game and pairing.</p> : null}
                <button className="primary" onClick={() => void install()}>{drive.update ? "Update game on badge" : "Copy game to badge"}</button>
                <button className="secondary" onClick={() => void chooseDrive()}>Choose another drive</button>
              </>
            ) : (
              <button className="primary" disabled={operation !== null} onClick={() => void chooseDrive()}>
                {operation === "choosing" ? "Checking the drive…" : "Choose badge drive"}
              </button>
            )}
          </div>
          {installError ? <p className="error install-error" role="alert">{installError}</p> : null}
          <div className={supported ? "install-manual" : "install-manual install-manual-primary"}>
            {supported ? <p className="kicker">PREFER TO COPY IT YOURSELF?</p> : null}
            <a className={supported ? "button secondary" : "button primary"} href="/dots-boxes.zip" download aria-disabled={copying || undefined} onClick={(event) => { if (copying) event.preventDefault(); }}>
              Download game ZIP <span aria-hidden="true">↓</span>
            </a>
            <p>Unzip the download and copy <code>dots_boxes</code> into the badge’s <code>apps</code> folder. Then safely eject the drive and press Reset once.</p>
          </div>
        </aside>
      </section>
    </section>
  );
}
