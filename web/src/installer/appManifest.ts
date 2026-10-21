export interface AppFile {
  path: string;
  url: string;
  bytes: number;
  sha256: string;
}

export interface AppRelease {
  version: string;
  appId: "dots_boxes";
  name: "Dots & Boxes";
  board: "tufty2350";
  runtimeMpy: 7942;
  description: string;
  zipUrl: string;
  zipSha256: string;
  zipBytes: number;
  files: AppFile[];
}

export const MAX_APP_BYTES = 16 * 1024 * 1024;
const HASH = /^[0-9a-f]{64}$/;
const VERSION = /^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$/;
const SEGMENT = /^[A-Za-z0-9_][A-Za-z0-9_.+-]{0,99}$/;
const RESERVED = /^(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\.|$)/i;

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function safePath(value: unknown): value is string {
  return typeof value === "string" && value.length <= 240 && value.split("/").every(
    (part) => SEGMENT.test(part) && !part.endsWith(".") && !RESERVED.test(part),
  );
}

function assetUrl(value: unknown): value is string {
  return typeof value === "string" && value.startsWith("/badge-app/") && safePath(value.slice(11));
}

function byteCount(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0 && value <= MAX_APP_BYTES;
}

/** Reject paths that escape the app folder or alias another file on a FAT disk. */
export function parseAppManifest(value: unknown): AppRelease | null {
  const invalid = () => new Error("The app release information is invalid. Installation is unavailable.");
  if (!object(value) || value.schemaVersion !== 1) throw invalid();
  if (value.release === null) return null;
  const release = value.release;
  if (!object(release) || typeof release.version !== "string" || !VERSION.test(release.version) ||
      release.appId !== "dots_boxes" || release.name !== "Dots & Boxes" || release.board !== "tufty2350" ||
      release.runtimeMpy !== 7942 || typeof release.description !== "string" || release.description.length > 500 ||
      !assetUrl(release.zipUrl) || !release.zipUrl.endsWith(".zip") || !byteCount(release.zipBytes) ||
      typeof release.zipSha256 !== "string" || !HASH.test(release.zipSha256) ||
      !Array.isArray(release.files) || release.files.length < 1 || release.files.length > 128) throw invalid();
  const names = new Set<string>();
  const spellings = new Map<string, string>();
  let total = 0;
  const files: AppFile[] = release.files.map((file: unknown) => {
    if (!object(file) || !safePath(file.path) || !assetUrl(file.url) || !byteCount(file.bytes) ||
        typeof file.sha256 !== "string" || !HASH.test(file.sha256)) throw invalid();
    const name = file.path.toLowerCase();
    if (names.has(name)) throw invalid();
    const parts = file.path.split("/");
    for (let length = 1; length <= parts.length; length++) {
      const spelling = parts.slice(0, length).join("/");
      const key = spelling.toLowerCase();
      if (spellings.has(key) && spellings.get(key) !== spelling) throw invalid();
      spellings.set(key, spelling);
    }
    names.add(name);
    total += file.bytes;
    return { path: file.path, url: file.url, bytes: file.bytes, sha256: file.sha256 };
  });
  if (total > MAX_APP_BYTES || !files.some((file) => file.path === "__init__.py")) throw invalid();
  for (const name of names) {
    const parts = name.split("/");
    while (parts.length > 1) {
      parts.pop();
      if (names.has(parts.join("/"))) throw invalid();
    }
  }
  return {
    version: release.version, appId: release.appId, name: release.name, board: release.board,
    runtimeMpy: release.runtimeMpy, description: release.description, zipUrl: release.zipUrl,
    zipSha256: release.zipSha256, zipBytes: release.zipBytes, files,
  };
}
