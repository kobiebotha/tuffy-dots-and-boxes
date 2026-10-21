import type { Page } from "@playwright/test";

export type DriveScenario = "normal" | "existing" | "cancel" | "denied" | "bootsel" | "wrong-folder" | "write-failure" | "update-failure" | "pause-write";

/** Browser-only drive: no test opens a real picker or touches mounted hardware. */
export async function mockDrive(page: Page, scenario: DriveScenario = "normal", failurePath = "view.py") {
  await page.addInitScript(({ scenario, failurePath }) => {
    interface Directory {
      kind: "directory";
      name: string;
      children: Map<string, Entry>;
      isSameEntry(other: unknown): Promise<boolean>;
      entries(): AsyncIterableIterator<[string, Entry]>;
      getDirectoryHandle(name: string, options?: { create?: boolean }): Promise<Directory>;
      getFileHandle(name: string, options?: { create?: boolean }): Promise<ReturnType<typeof file>>;
      removeEntry(name: string): Promise<void>;
    }
    type Entry = Directory | ReturnType<typeof file>;
    const log: string[] = [];
    let failed = false;
    let paused = false;
    const missing = () => new DOMException("Missing", "NotFoundError");

    function file(name: string, contents = "", path = name) {
      let data = new TextEncoder().encode(contents);
      const handle = {
        kind: "file" as const,
        name,
        async isSameEntry(other: unknown) { return other === handle; },
        async getFile() { log.push(`read:${path}`); return new File([data.slice()], name); },
        async createWritable() {
          let pending = data.slice();
          return {
            async write(bytes: Uint8Array) {
              log.push(`write:${path}`);
              if (!paused && scenario === "pause-write" && path === `/apps/dots_boxes/${failurePath}`) {
                paused = true;
                await new Promise<void>((resume) => { Object.assign(window, { __resumeWrite: resume }); });
              }
              if (!failed && (scenario === "write-failure" || scenario === "update-failure") && path === `/apps/dots_boxes/${failurePath}`) {
                failed = true;
                throw new DOMException("The badge was disconnected", "NetworkError");
              }
              pending = new Uint8Array(bytes);
            },
            async close() { data = pending; log.push(`close:${path}`); },
            async abort() { log.push(`abort:${path}`); },
          };
        },
        contents() { return Array.from(data); },
      };
      return handle;
    }

    function directory(name: string, path = ""): Directory {
      const children = new Map<string, Entry>();
      const handle: Directory = {
        kind: "directory", name, children,
        async isSameEntry(other: unknown) { return other === handle; },
        async *entries() { yield* children; },
        async getDirectoryHandle(name, options) {
          let entry = children.get(name);
          if (!entry && options?.create) {
            entry = directory(name, `${path}/${name}`);
            children.set(name, entry);
            log.push(`mkdir:${path}/${name}`);
          }
          if (!entry) throw missing();
          if (entry.kind !== "directory") throw new DOMException("Not a directory", "TypeMismatchError");
          return entry;
        },
        async getFileHandle(name, options) {
          let entry = children.get(name);
          if (!entry && options?.create) {
            entry = file(name, "", `${path}/${name}`);
            children.set(name, entry);
            log.push(`create:${path}/${name}`);
          }
          if (!entry) throw missing();
          if (entry.kind !== "file") throw new DOMException("Not a file", "TypeMismatchError");
          return entry;
        },
        async removeEntry(name) {
          const entry = children.get(name);
          if (!entry) throw missing();
          if (entry.kind === "directory" && entry.children.size) throw new DOMException("Not empty", "InvalidModificationError");
          children.delete(name);
          log.push(`remove:${path}/${name}`);
        },
      };
      return handle;
    }

    const root = directory(scenario === "bootsel" ? "RP2350" : "BADGEWARE");
    if (scenario !== "wrong-folder") {
      const apps = directory("apps", "/apps");
      apps.children.set("menu", directory("menu", "/apps/menu"));
      const other = directory("other_game", "/apps/other_game");
      other.children.set("__init__.py", file("__init__.py", "another installed game"));
      apps.children.set("other_game", other);
      if (scenario === "existing" || scenario === "update-failure") {
        const existing = directory("dots_boxes", "/apps/dots_boxes");
        existing.children.set("__init__.py", file("__init__.py", "old launcher entry", "/apps/dots_boxes/__init__.py"));
        existing.children.set("game.py", file("game.py", "old game code", "/apps/dots_boxes/game.py"));
        existing.children.set("personal.txt", file("personal.txt", "preserve my notes", "/apps/dots_boxes/personal.txt"));
        apps.children.set("dots_boxes", existing);
      }
      root.children.set("apps", apps);
    }
    root.children.set("main.py", file("main.py", "original Badgeware entry"));
    root.children.set("launcher.py", file("launcher.py", "original launcher"));
    const state = directory("state", "/state");
    state.children.set("saved.db", file("saved.db", "existing paired game state"));
    root.children.set("state", state);

    function snapshot(entry: Entry): unknown {
      return entry.kind === "file" ? entry.contents() : Object.fromEntries([...entry.children].map(([name, child]) => [name, snapshot(child)]));
    }
    Object.assign(window, {
      __driveLog: log,
      __driveSnapshot: () => snapshot(root),
      showDirectoryPicker: async () => {
        if (scenario === "cancel") throw new DOMException("Cancelled", "AbortError");
        if (scenario === "denied") throw new DOMException("Permission denied", "NotAllowedError");
        return root;
      },
    });
  }, { scenario, failurePath });
}

export async function driveSnapshot(page: Page): Promise<Record<string, unknown>> {
  return page.evaluate(() => (window as unknown as { __driveSnapshot: () => Record<string, unknown> }).__driveSnapshot());
}

export async function driveLog(page: Page): Promise<string[]> {
  return page.evaluate(() => (window as unknown as { __driveLog: string[] }).__driveLog);
}

export async function resumeWrite(page: Page): Promise<void> {
  await page.evaluate(() => (window as unknown as { __resumeWrite: () => void }).__resumeWrite());
}
