import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { copyApp, MAX_SNAPSHOT_BYTES, supportsAppCopy, validateBadgeDrive, type AppDirectoryHandle, type AppFileHandle } from "./appCopy";
import { parseAppManifest, type AppRelease } from "./appManifest";
import { prepareApp, sha256, type PreparedApp } from "./appRelease";

const encode = (value: string) => new TextEncoder().encode(value);
type Entry = Directory | DataFile;
class Disk {
  connected = true;
  events: string[] = [];
  onRead?: (file: DataFile) => void | Promise<void>;
  onWrite?: (file: DataFile, bytes: Uint8Array) => void | Promise<void>;
  check() { if (!this.connected) throw new DOMException("Disconnected", "NotFoundError"); }
}
class Directory implements AppDirectoryHandle {
  kind = "directory" as const;
  children = new Map<string, Entry>();
  constructor(readonly name: string, readonly disk: Disk, readonly path = name) {}
  async *entries(): AsyncIterableIterator<[string, Entry]> {
    this.disk.check();
    yield* this.children.entries();
  }
  async getDirectoryHandle(name: string, options?: { create?: boolean }): Promise<Directory> {
    this.disk.check();
    let entry = this.children.get(name);
    if (!entry && options?.create) {
      this.disk.events.push(`mkdir:${this.path}/${name}`);
      entry = this.directory(name);
    }
    if (!(entry instanceof Directory)) throw new DOMException("Missing directory", "NotFoundError");
    return entry;
  }
  async getFileHandle(name: string, options?: { create?: boolean }): Promise<DataFile> {
    this.disk.check();
    let entry = this.children.get(name);
    if (!entry && options?.create) {
      this.disk.events.push(`create:${this.path}/${name}`);
      entry = this.file(name, new Uint8Array());
    }
    if (!(entry instanceof DataFile)) throw new DOMException("Missing file", "NotFoundError");
    return entry;
  }
  async removeEntry(name: string): Promise<void> {
    this.disk.check();
    const entry = this.children.get(name);
    if (entry instanceof Directory && entry.children.size) throw new DOMException("Directory not empty", "InvalidModificationError");
    this.disk.events.push(`remove:${this.path}/${name}`);
    this.children.delete(name);
  }
  async isSameEntry(other: AppDirectoryHandle | AppFileHandle) { this.disk.check(); return this === other; }
  directory(name: string): Directory {
    const child = new Directory(name, this.disk, `${this.path}/${name}`);
    this.children.set(name, child);
    return child;
  }
  file(name: string, bytes: Uint8Array | string): DataFile {
    const child = new DataFile(name, this.disk, `${this.path}/${name}`, typeof bytes === "string" ? encode(bytes) : bytes);
    this.children.set(name, child);
    return child;
  }
}
class DataFile implements AppFileHandle {
  kind = "file" as const;
  reportedSize?: number;
  constructor(readonly name: string, readonly disk: Disk, readonly path: string, public bytes: Uint8Array) {}
  async getFile(): Promise<File> {
    this.disk.check();
    this.disk.events.push(`read:${this.path}`);
    await this.disk.onRead?.(this);
    const file = new File([new Uint8Array(this.bytes)], this.name);
    if (this.reportedSize !== undefined) Object.defineProperty(file, "size", { value: this.reportedSize });
    return file;
  }
  async createWritable() {
    this.disk.check();
    this.disk.events.push(`writable:${this.path}`);
    return {
      write: async (bytes: Uint8Array) => {
        this.disk.check();
        this.disk.events.push(`write:${this.path}`);
        // Simulate even a partially committed write, so rollback cannot rely on
        // a particular browser's atomic staging-file implementation.
        this.bytes = new Uint8Array(bytes);
        await this.disk.onWrite?.(this, bytes);
      },
      close: async () => { this.disk.check(); },
      abort: async () => {},
    };
  }
  async isSameEntry(other: AppDirectoryHandle | AppFileHandle) { this.disk.check(); return this === other; }
}

function drive() {
  const disk = new Disk();
  const root = new Directory("BADGEWARE", disk);
  root.file("main.py", "original menu");
  const apps = root.directory("apps");
  apps.directory("menu").file("__init__.py", "original menu app");
  apps.directory("another_game").file("save.json", "other game state");
  return { root, disk, apps };
}

async function packageApp(): Promise<PreparedApp> {
  const contents = [
    ["__init__.py", encode("from .game import run\n")],
    ["game.mpy", new Uint8Array([77, 6, 10, 0, 255, 128, 13, 10])],
    ["assets/logo.bin", new Uint8Array([0, 255, 0, 128])],
  ] as const;
  const files = await Promise.all(contents.map(async ([path, bytes]) => ({
    file: { path, url: `/badge-app/test/${path}`, bytes: bytes.length, sha256: await sha256(bytes) }, bytes,
  })));
  const zip = encode("test zip bytes");
  const release: AppRelease = {
    version: "test-content-123", appId: "dots_boxes", name: "Dots & Boxes", board: "tufty2350", runtimeMpy: 7942,
    description: "Dots & Boxes", zipUrl: "/badge-app/test.zip", zipBytes: zip.length, zipSha256: await sha256(zip),
    files: files.map(({ file }) => file),
  };
  return { release, files, zip };
}

function installLock() {
  let locked = false;
  vi.stubGlobal("navigator", { locks: { request: vi.fn(async (_name: string, _options: unknown, callback: (lock: object | null) => Promise<void>) => {
    if (locked) return callback(null);
    locked = true;
    try { await callback({}); } finally { locked = false; }
  }) } });
}
const mutations = (disk: Disk) => disk.events.filter((event) => /^(mkdir|create|writable|write|remove):/.test(event));
beforeEach(installLock);
afterEach(() => vi.unstubAllGlobals());

describe("badge drive validation", () => {
  it("requires Badgeware markers and rejects BOOTSEL even if apps is present", async () => {
    const { root, apps } = drive();
    await expect(validateBadgeDrive(root)).resolves.toEqual({ apps, existing: false });
    const wrong = new Directory("Documents", new Disk());
    await expect(validateBadgeDrive(wrong)).rejects.toThrow("must contain an apps folder");
    wrong.directory("apps");
    await expect(validateBadgeDrive(wrong)).rejects.toThrow("main.py and apps/menu");
    root.file("INFO_UF2.TXT", "bootloader");
    await expect(validateBadgeDrive(root)).rejects.toThrow("BOOTSEL");
    await expect(validateBadgeDrive(new Directory("RP2350", new Disk()))).rejects.toThrow("BOOTSEL");
  });
  it("refuses case aliases and app file collisions", async () => {
    const { root, apps } = drive();
    apps.directory("Dots_Boxes");
    await expect(validateBadgeDrive(root)).rejects.toThrow("conflicting name");
    apps.children.delete("Dots_Boxes");
    apps.file("dots_boxes", "not a directory");
    await expect(validateBadgeDrive(root)).rejects.toThrow("is a file");
  });
  it("requires a secure supported browser", () => {
    expect(supportsAppCopy()).toBe(false);
    vi.stubGlobal("window", { isSecureContext: true, showDirectoryPicker: vi.fn() });
    expect(supportsAppCopy()).toBe(true);
    vi.stubGlobal("navigator", {});
    expect(supportsAppCopy()).toBe(false);
  });
});

describe("verified app copying", () => {
  it("copies native bytes, verifies each file, and writes the launcher last", async () => {
    const { root, apps, disk } = drive();
    const prepared = await packageApp();
    const progress = vi.fn();
    await copyApp(root, prepared, progress, { update: false });
    const app = await apps.getDirectoryHandle("dots_boxes");
    expect((await app.getFileHandle("game.mpy")).bytes).toEqual(prepared.files[1].bytes);
    const assets = await app.getDirectoryHandle("assets");
    expect((await assets.getFileHandle("logo.bin")).bytes).toEqual(prepared.files[2].bytes);
    expect(disk.events.filter((event) => event.startsWith("write:")).at(-1)).toBe("write:BADGEWARE/apps/dots_boxes/__init__.py");
    expect(progress.mock.calls.map(([value]) => value.completed)).toEqual([1, 2, 3]);
    expect(disk.events.filter((event) => event.startsWith("read:")).length).toBeGreaterThanOrEqual(6);
  });
  it("updates only package paths and preserves existing user files and directories", async () => {
    const { root, apps } = drive();
    const app = apps.directory("dots_boxes");
    app.file("__init__.py", "old launcher");
    app.file("game.mpy", "old game");
    const state = app.file("state.json", "saved user state");
    const unknown = app.directory("personal").file("notes.txt", "keep me");
    const prepared = await packageApp();
    await copyApp(root, prepared, undefined, { update: true });
    expect((await app.getFileHandle("__init__.py")).bytes).toEqual(prepared.files[0].bytes);
    expect(state.bytes).toEqual(encode("saved user state"));
    expect(unknown.bytes).toEqual(encode("keep me"));
    expect((await apps.getDirectoryHandle("another_game")).children.has("save.json")).toBe(true);
  });
  it("requires explicit fresh confirmation when the installation status changes", async () => {
    const { root, apps, disk } = drive();
    apps.directory("dots_boxes");
    await expect(copyApp(root, await packageApp(), undefined, { update: false })).rejects.toThrow("confirm Update");
    expect(mutations(disk)).toEqual([]);
    apps.children.delete("dots_boxes");
    await expect(copyApp(root, await packageApp(), undefined, { update: true })).rejects.toThrow("no longer present");
    expect(mutations(disk)).toEqual([]);
  });
  it("finishes every snapshot before any mutation, and refuses oversized snapshots", async () => {
    const { root, apps, disk } = drive();
    const app = apps.directory("dots_boxes");
    app.file("__init__.py", "old launcher");
    app.file("game.mpy", "old game");
    const last = app.directory("assets").file("logo.bin", "old image");
    disk.onRead = (file) => { if (file === last) throw new DOMException("Read denied", "NotAllowedError"); };
    await expect(copyApp(root, await packageApp(), undefined, { update: true })).rejects.toThrow("Permission");
    expect(mutations(disk)).toEqual([]);
    disk.onRead = undefined;
    last.reportedSize = MAX_SNAPSHOT_BYTES + 1;
    await expect(copyApp(root, await packageApp(), undefined, { update: true })).rejects.toThrow("too large");
    expect(mutations(disk)).toEqual([]);
  });
  it("refuses a file/folder collision or alias before writing any other target", async () => {
    const { root, apps, disk } = drive();
    const app = apps.directory("dots_boxes");
    app.file("__init__.py", "old launcher");
    app.directory("game.mpy");
    await expect(copyApp(root, await packageApp(), undefined, { update: true })).rejects.toThrow("conflicts with a folder");
    expect(mutations(disk)).toEqual([]);
    app.children.delete("game.mpy");
    app.directory("Assets");
    await expect(copyApp(root, await packageApp(), undefined, { update: true })).rejects.toThrow("conflicting name");
    expect(mutations(disk)).toEqual([]);
  });
  it("rejects corrupted prepared bytes at the mutation boundary", async () => {
    const { root, disk } = drive();
    const prepared = await packageApp();
    prepared.files[1].bytes[0] ^= 255;
    await expect(copyApp(root, prepared, undefined, { update: false })).rejects.toThrow("integrity check failed");
    expect(mutations(disk)).toEqual([]);
  });
  it.each(["corruption", "partial write"])("restores replacements after %s and preserves saved data", async (mode) => {
    const { root, apps, disk } = drive();
    const app = apps.directory("dots_boxes");
    const launcher = app.file("__init__.py", "old launcher");
    const game = app.file("game.mpy", "old game");
    const state = app.file("state.json", "my saved state");
    let failed = false;
    disk.onWrite = (file) => {
      if (file.name !== "logo.bin" || failed) return;
      failed = true;
      if (mode === "corruption") file.bytes = encode("corrupted readback");
      else throw new DOMException("Write failed midway", "UnknownError");
    };
    await expect(copyApp(root, await packageApp(), undefined, { update: true })).rejects.toThrow(mode === "corruption"
      ? "Recovery could not be verified" : "Previous app files were restored");
    expect(launcher.bytes).toEqual(encode("old launcher"));
    expect(game.bytes).toEqual(encode("old game"));
    expect(state.bytes).toEqual(encode("my saved state"));
    expect(app.children.has("assets")).toBe(mode === "corruption");
  });
  it("removes a failed new installation without removing unrelated apps", async () => {
    const { root, apps, disk } = drive();
    disk.onWrite = (file) => { if (file.name === "logo.bin") throw new Error("Disk full"); };
    await expect(copyApp(root, await packageApp(), undefined, { update: false })).rejects.toThrow("newly created entries were removed");
    expect(apps.children.has("dots_boxes")).toBe(false);
    expect(apps.children.has("another_game")).toBe(true);
  });
  it("restores a partially written existing file as well as earlier replacements", async () => {
    const { root, apps, disk } = drive();
    const app = apps.directory("dots_boxes");
    const game = app.file("game.mpy", "old game");
    const logo = app.directory("assets").file("logo.bin", "old logo");
    let failed = false;
    disk.onWrite = (file) => {
      if (file === logo && !failed) {
        failed = true;
        throw new Error("Partial write");
      }
    };
    await expect(copyApp(root, await packageApp(), undefined, { update: true })).rejects.toThrow("Previous app files were restored");
    expect(game.bytes).toEqual(encode("old game"));
    expect(logo.bytes).toEqual(encode("old logo"));
  });
  it("preserves an external edit to the same file handle when a later write fails", async () => {
    const { root, apps, disk } = drive();
    const app = apps.directory("dots_boxes");
    const launcher = app.file("__init__.py", "old launcher");
    const game = app.file("game.mpy", "old game");
    // Match both the original and installed byte count to exercise the hash
    // ownership check rather than just rejecting an unexpected size.
    const external = encode("OS edits");
    disk.onWrite = (file) => {
      if (file.name !== "logo.bin") return;
      // The OS editor changes the existing handle, without replacing its
      // filesystem entry, after the installer's earlier write passed readback.
      game.bytes = new Uint8Array(external);
      throw new Error("Later write failed");
    };
    await expect(copyApp(root, await packageApp(), undefined, { update: true })).rejects.toThrow("Recovery could not be verified");
    expect(game.bytes).toEqual(external);
    expect(launcher.bytes).toEqual(encode("old launcher"));
    expect(app.children.has("assets")).toBe(false);
  });
  it("retains unknown partially written existing content for manual recovery", async () => {
    const { root, apps, disk } = drive();
    const app = apps.directory("dots_boxes");
    const game = app.file("game.mpy", "old game");
    disk.onWrite = (file) => {
      if (file !== game) return;
      game.bytes = new Uint8Array([77, 6]);
      throw new Error("Partial write");
    };
    await expect(copyApp(root, await packageApp(), undefined, { update: true })).rejects.toThrow("Recovery could not be verified");
    expect(game.bytes).toEqual(new Uint8Array([77, 6]));
    expect(disk.events.filter((event) => event === `write:${game.path}`)).toHaveLength(1);
  });
  it("preserves an external edit to a newly created file during rollback", async () => {
    const { root, apps, disk } = drive();
    const external = encode("OS edits");
    disk.onWrite = async (file) => {
      if (file.name !== "logo.bin") return;
      const app = await apps.getDirectoryHandle("dots_boxes");
      const game = await app.getFileHandle("game.mpy");
      game.bytes = new Uint8Array(external);
      throw new Error("Later write failed");
    };
    await expect(copyApp(root, await packageApp(), undefined, { update: false })).rejects.toThrow("Recovery could not be verified");
    const app = await apps.getDirectoryHandle("dots_boxes");
    expect((await app.getFileHandle("game.mpy")).bytes).toEqual(external);
    expect(app.children.has("assets")).toBe(false);
    expect(app.children.has("__init__.py")).toBe(false);
  });
  it("reports incomplete recovery after disconnect", async () => {
    const { root, apps, disk } = drive();
    const app = apps.directory("dots_boxes");
    app.file("game.mpy", "old game");
    disk.onWrite = () => { disk.connected = false; };
    await expect(copyApp(root, await packageApp(), undefined, { update: true })).rejects.toThrow("Keep the badge drive connected. Recovery could not be verified");
  });
  it("reports incomplete recovery if restoring an old file fails readback verification", async () => {
    const { root, apps, disk } = drive();
    const app = apps.directory("dots_boxes");
    const game = app.file("game.mpy", "old game");
    disk.onWrite = (file, bytes) => {
      if (file.name === "logo.bin") throw new Error("Disk full");
      if (file === game && new TextDecoder().decode(bytes) === "old game") file.bytes = encode("bad restore");
    };
    await expect(copyApp(root, await packageApp(), undefined, { update: true })).rejects.toThrow("Recovery could not be verified");
  });
  it("retains files introduced by another writer during rollback", async () => {
    const { root, apps, disk } = drive();
    disk.onWrite = async (file) => {
      if (file.name !== "logo.bin") return;
      (await apps.getDirectoryHandle("dots_boxes")).file("personal.txt", "another writer's data");
      throw new Error("Disk full");
    };
    await expect(copyApp(root, await packageApp(), undefined, { update: false })).rejects.toThrow("Recovery could not be verified");
    expect((await (await apps.getDirectoryHandle("dots_boxes")).getFileHandle("personal.txt")).bytes).toEqual(encode("another writer's data"));
  });
  it("refuses a second tab while the first copy holds its Web Lock", async () => {
    const { root, disk } = drive();
    const prepared = await packageApp();
    let resume!: () => void;
    let entered!: () => void;
    const started = new Promise<void>((resolve) => { entered = resolve; });
    const held = new Promise<void>((resolve) => { resume = resolve; });
    disk.onWrite = async () => { entered(); await held; };
    const first = copyApp(root, prepared, undefined, { update: false });
    await started;
    await expect(copyApp(root, prepared, undefined, { update: false })).rejects.toThrow("Another tab");
    resume();
    await first;
  });
});

describe("release integrity and path validation", () => {
  it("rejects corrupt downloads before preparing an install", async () => {
    const { release } = await packageApp();
    vi.stubGlobal("fetch", vi.fn(async () => new Response(encode("bad download"))));
    await expect(prepareApp(release)).rejects.toThrow("integrity check failed");
  });
  it.each(["../outside.py", "nested/../../outside.py", "/outside.py", "C:/file.py", "assets\\file.py", "assets/CON.txt", "assets/a.", "assets//a"])("rejects unsafe package path %s", async (path) => {
    const { release } = await packageApp();
    release.files[1].path = path;
    expect(() => parseAppManifest({ schemaVersion: 1, release })).toThrow("invalid");
  });
  it("rejects case aliases, directory aliases and file/folder collisions in a manifest", async () => {
    const { release } = await packageApp();
    const check = (paths: string[]) => parseAppManifest({ schemaVersion: 1, release: { ...release, files: paths.map((path) => ({ ...release.files[0], path })) } });
    expect(() => check(["__init__.py", "__INIT__.py"])).toThrow("invalid");
    expect(() => check(["__init__.py", "Assets/a.bin", "assets/b.bin"])).toThrow("invalid");
    expect(() => check(["__init__.py", "assets", "assets/a.bin"])).toThrow("invalid");
  });
});
