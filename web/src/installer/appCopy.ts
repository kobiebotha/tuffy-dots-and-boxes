import { MAX_APP_BYTES, parseAppManifest, type AppFile } from "./appManifest";
import { sha256, type PreparedApp } from "./appRelease";

// The picker is still absent from some versions of lib.dom and browsers.
export interface AppFileHandle {
  kind: "file";
  name: string;
  getFile(): Promise<File>;
  createWritable(): Promise<{
    write(bytes: Uint8Array): Promise<void>;
    close(): Promise<void>;
    abort(): Promise<void>;
  }>;
  isSameEntry(other: AppEntry): Promise<boolean>;
}
export interface AppDirectoryHandle {
  kind: "directory";
  name: string;
  entries(): AsyncIterableIterator<[string, AppEntry]>;
  getDirectoryHandle(name: string, options?: { create?: boolean }): Promise<AppDirectoryHandle>;
  getFileHandle(name: string, options?: { create?: boolean }): Promise<AppFileHandle>;
  removeEntry(name: string): Promise<void>;
  isSameEntry(other: AppEntry): Promise<boolean>;
}
type AppEntry = AppDirectoryHandle | AppFileHandle;
type PickerWindow = Window & {
  showDirectoryPicker?: (options: { id: string; mode: "readwrite" }) => Promise<AppDirectoryHandle>;
};
export interface CopyProgress { completed: number; total: number; path: string }
export interface CopyOptions { update: boolean }
export interface BadgeDrive { apps: AppDirectoryHandle; existing: boolean }
export const MAX_SNAPSHOT_BYTES = MAX_APP_BYTES;
const APP_ID = "dots_boxes";

export function supportsAppCopy(): boolean {
  return typeof window !== "undefined" && window.isSecureContext &&
    typeof (window as PickerWindow).showDirectoryPicker === "function" &&
    typeof navigator !== "undefined" && typeof navigator.locks?.request === "function";
}

/** Call in the user's click handler, before awaiting anything else. */
export function chooseBadgeDrive(): Promise<AppDirectoryHandle> {
  const picker = (window as PickerWindow).showDirectoryPicker;
  if (!picker) return Promise.reject(new Error("Use the ZIP download in this browser."));
  return picker.call(window, { id: "dots-boxes-app", mode: "readwrite" });
}

/** FAT volumes ignore case. Never select the first of multiple aliases. */
async function findEntry(directory: AppDirectoryHandle, name: string): Promise<AppEntry | undefined> {
  let match: AppEntry | undefined;
  for await (const [candidate, entry] of directory.entries()) {
    if (candidate.toLowerCase() !== name.toLowerCase()) continue;
    if (match || candidate !== name || entry.name !== name) {
      throw new Error(`The badge contains a conflicting name for ${name}. No conflicting files were changed.`);
    }
    match = entry;
  }
  return match;
}

export async function validateBadgeDrive(root: AppDirectoryHandle): Promise<BadgeDrive> {
  if (/^(RP2350|RP2|RPI-RP2)$/i.test(root.name)) throw bootselError();
  for await (const [name] of root.entries()) {
    if (name.toUpperCase() === "INFO_UF2.TXT") throw bootselError();
  }
  const apps = await findEntry(root, "apps");
  if (!apps || apps.kind !== "directory") {
    throw new Error("Choose the Badgeware drive itself: it must contain an apps folder. No files were changed.");
  }
  const main = await findEntry(root, "main.py");
  const menu = await findEntry(apps, "menu");
  if (!main || main.kind !== "file" || !menu || menu.kind !== "directory") {
    throw new Error("This folder does not look like a Badgeware drive. Choose the drive containing main.py and apps/menu. No files were changed.");
  }
  const app = await findEntry(apps, APP_ID);
  if (app && app.kind !== "directory") throw new Error("apps/dots_boxes is a file. Move that file before installing. No files were changed.");
  return { apps, existing: !!app };
}

function bootselError(): Error {
  return new Error("This is the BOOTSEL drive. Press RESET once, then double-tap RESET to open the Badgeware file drive.");
}

function reason(error: unknown): string {
  if (error instanceof Error) {
    if (error.name === "NotAllowedError" || error.name === "SecurityError") {
      return "Permission to write the badge drive was denied. Choose the drive again and allow changes.";
    }
    if (error.name === "NotFoundError" || error.name === "InvalidStateError") {
      return "The badge drive disconnected or a required file became unavailable.";
    }
    return error.message;
  }
  return "The badge drive could not be written.";
}

interface Snapshot { handle: AppFileHandle; bytes: Uint8Array; hash: string }
interface Target { file: AppFile; bytes: Uint8Array; snapshot?: Snapshot }
interface Created {
  parent: AppDirectoryHandle;
  entry: AppEntry;
  installedBytes?: number;
  installedHash?: string;
}
interface Replaced {
  parent: AppDirectoryHandle;
  name: string;
  snapshot: Snapshot;
  installedBytes: number;
  installedHash: string;
}

/** Serialize this site's tabs, recheck consent, and only replace package paths. */
export async function copyApp(
  root: AppDirectoryHandle,
  prepared: PreparedApp,
  onProgress: ((progress: CopyProgress) => void) | undefined,
  options: CopyOptions,
): Promise<void> {
  if (typeof navigator === "undefined" || typeof navigator.locks?.request !== "function") {
    throw new Error("Use the ZIP download in this browser.");
  }
  if (!options || typeof options.update !== "boolean") throw new Error("Confirm whether to install or update Dots & Boxes first.");
  await navigator.locks.request("dots-boxes-app-install", { mode: "exclusive", ifAvailable: true }, async (lock) => {
    if (!lock) throw new Error("Another tab is copying Dots & Boxes. Wait for it to finish, then check your badge.");
    try { await copyExclusively(root, prepared, onProgress, options); }
    catch (error) { throw new Error(reason(error)); }
  });
}

async function copyExclusively(
  root: AppDirectoryHandle,
  prepared: PreparedApp,
  onProgress: ((progress: CopyProgress) => void) | undefined,
  options: CopyOptions,
): Promise<void> {
  const release = parseAppManifest({ schemaVersion: 1, release: prepared.release })!;
  if (prepared.files.length !== release.files.length) throw new Error("The app package is incomplete. No files were changed.");
  if (prepared.zip.length !== release.zipBytes || await sha256(prepared.zip) !== release.zipSha256) throw integrityError();
  const targets: Target[] = [];
  for (const file of release.files) {
    const matches = prepared.files.filter((candidate) => candidate.file.path === file.path);
    // Own the bytes throughout the operation, so callers cannot alter verified data.
    const bytes = matches.length === 1 ? new Uint8Array(matches[0].bytes) : undefined;
    if (!bytes || bytes.length !== file.bytes || await sha256(bytes) !== file.sha256) throw integrityError();
    targets.push({ file, bytes });
  }
  const { apps, existing } = await validateBadgeDrive(root);
  if (existing !== options.update) {
    throw new Error(existing
      ? "Dots & Boxes is already installed. Choose the badge drive again and confirm Update. No files were changed."
      : "The existing Dots & Boxes folder is no longer present. Choose the badge drive again. No files were changed.");
  }
  const appEntry = await findEntry(apps, APP_ID);
  const app = appEntry?.kind === "directory" ? appEntry : undefined;
  const directories = new Map<string, AppDirectoryHandle | undefined>([["", app]]);
  let snapshotBytes = 0;

  // Snapshot all targeted existing files before the first mkdir/create/write.
  // Unknown files (including user state and obsolete assets) are never removed.
  for (const target of targets) {
    const parts = target.file.path.split("/");
    const name = parts.pop()!;
    let parent = app;
    let prefix = "";
    for (const part of parts) {
      prefix = prefix ? `${prefix}/${part}` : part;
      if (!directories.has(prefix)) {
        const entry = parent ? await findEntry(parent, part) : undefined;
        if (entry && entry.kind !== "directory") throw new Error(`The package folder ${prefix} conflicts with a file. No files were changed.`);
        directories.set(prefix, entry as AppDirectoryHandle | undefined);
      }
      parent = directories.get(prefix);
    }
    const entry = parent ? await findEntry(parent, name) : undefined;
    if (!entry) continue;
    if (entry.kind !== "file") throw new Error(`The package file ${target.file.path} conflicts with a folder. No files were changed.`);
    const original = await entry.getFile();
    snapshotBytes += original.size;
    if (snapshotBytes > MAX_SNAPSHOT_BYTES) throw new Error("Existing app files are too large to back up safely in the browser. No files were changed.");
    const bytes = new Uint8Array(await original.arrayBuffer());
    if (bytes.length !== original.size) throw new Error("An existing app file changed while it was being read. No files were changed.");
    target.snapshot = { handle: entry, bytes, hash: await sha256(bytes) };
  }

  // Commit launcher last, after every dependency has passed readback.
  targets.sort((a, b) => Number(a.file.path === "__init__.py") - Number(b.file.path === "__init__.py"));
  const created: Created[] = [];
  const replaced: Replaced[] = [];
  try {
    const currentApp = await findEntry(apps, APP_ID);
    if (app ? !currentApp || !await currentApp.isSameEntry(app) : !!currentApp) throw changedError();
    if (!app) {
      const newApp = await apps.getDirectoryHandle(APP_ID, { create: true });
      created.push({ parent: apps, entry: newApp });
      await ensureEmpty(newApp);
      directories.set("", newApp);
    }
    for (const [index, target] of targets.entries()) {
      const parts = target.file.path.split("/");
      const name = parts.pop()!;
      let parent = directories.get("")!;
      let prefix = "";
      for (const part of parts) {
        prefix = prefix ? `${prefix}/${part}` : part;
        let directory = directories.get(prefix);
        const current = await findEntry(parent, part);
        if (directory) {
          if (!current || !await current.isSameEntry(directory)) throw changedError();
        } else {
          if (current) throw changedError();
          directory = await parent.getDirectoryHandle(part, { create: true });
          created.push({ parent, entry: directory });
          await ensureEmpty(directory);
          directories.set(prefix, directory);
        }
        parent = directory;
      }
      const current = await findEntry(parent, name);
      let handle: AppFileHandle;
      if (target.snapshot) {
        if (!current || !await current.isSameEntry(target.snapshot.handle)) throw changedError();
        const beforeWrite = await target.snapshot.handle.getFile();
        if (beforeWrite.size !== target.snapshot.bytes.length || await sha256(new Uint8Array(await beforeWrite.arrayBuffer())) !== target.snapshot.hash) throw changedError();
        handle = target.snapshot.handle;
      } else {
        if (current) throw changedError();
        handle = await parent.getFileHandle(name, { create: true });
        // create:true is not exclusive. Do not touch a nonempty racing entry.
        if ((await handle.getFile()).size !== 0) throw changedError();
        created.push({
          parent, entry: handle,
          installedBytes: target.file.bytes, installedHash: target.file.sha256,
        });
      }
      const writable = await handle.createWritable();
      if (target.snapshot) replaced.push({
        parent, name, snapshot: target.snapshot,
        installedBytes: target.file.bytes, installedHash: target.file.sha256,
      });
      try {
        await writable.write(new Uint8Array(target.bytes));
        await writable.close();
      } catch (error) {
        await writable.abort().catch(() => undefined);
        throw error;
      }
      await verify(handle, target.file.bytes, target.file.sha256, target.file.path);
      onProgress?.({ completed: index + 1, total: targets.length, path: target.file.path });
    }
  } catch (error) {
    const restored = await rollback(replaced, created);
    throw new Error(`${reason(error)} Installation did not finish.${restored
      ? " Previous app files were restored and newly created entries were removed. You can try again."
      : " Keep the badge drive connected. Recovery could not be verified; some files in apps/dots_boxes may be incomplete. Restore that folder from a backup or retry the installation before ejecting and resetting."}`);
  }
}

function integrityError(): Error {
  return new Error("The app integrity check failed. Nothing has been written to your badge.");
}
function changedError(): Error {
  return new Error("The app folder changed during installation. Stop other copying to the badge and try again.");
}
async function ensureEmpty(directory: AppDirectoryHandle): Promise<void> {
  for await (const _entry of directory.entries()) throw changedError();
}
async function verify(handle: AppFileHandle, size: number, hash: string, path: string): Promise<void> {
  const file = await handle.getFile();
  if (file.size !== size || await sha256(new Uint8Array(await file.arrayBuffer())) !== hash) {
    throw new Error(`The copied file ${path} did not pass verification.`);
  }
}
async function rollback(replaced: Replaced[], created: Created[]): Promise<boolean> {
  let complete = true;
  for (const { parent, name, snapshot, installedBytes, installedHash } of [...replaced].reverse()) {
    try {
      const current = await findEntry(parent, name);
      if (!current || !await current.isSameEntry(snapshot.handle)) { complete = false; continue; }
      // Entry identity alone does not establish ownership: an OS editor may
      // change this very handle after our successful write. Restore only an
      // exact installer result. Unknown partial writes and external edits are
      // retained for manual recovery instead of overwriting their contents.
      const currentFile = await snapshot.handle.getFile();
      if (currentFile.size !== snapshot.bytes.length && currentFile.size !== installedBytes) { complete = false; continue; }
      const currentHash = await sha256(new Uint8Array(await currentFile.arrayBuffer()));
      if (currentFile.size === snapshot.bytes.length && currentHash === snapshot.hash) continue;
      if (currentFile.size !== installedBytes || currentHash !== installedHash) { complete = false; continue; }
      const writable = await snapshot.handle.createWritable();
      try {
        await writable.write(new Uint8Array(snapshot.bytes));
        await writable.close();
      } catch (error) { await writable.abort().catch(() => undefined); throw error; }
      await verify(snapshot.handle, snapshot.bytes.length, snapshot.hash, name);
    } catch { complete = false; }
  }
  // Identity checks and nonrecursive deletion preserve entries from other writers.
  for (const { parent, entry, installedBytes, installedHash } of [...created].reverse()) {
    try {
      const current = await findEntry(parent, entry.name);
      if (!current) continue;
      if (!await current.isSameEntry(entry)) { complete = false; continue; }
      if (current.kind === "file") {
        // A file we created may since have been edited through the same handle.
        // Only remove its known empty initial state or exact installer bytes.
        const currentFile = await current.getFile();
        if (currentFile.size !== 0 && (currentFile.size !== installedBytes ||
            await sha256(new Uint8Array(await currentFile.arrayBuffer())) !== installedHash)) {
          complete = false;
          continue;
        }
      }
      await parent.removeEntry(entry.name);
    } catch { complete = false; }
  }
  return complete;
}
