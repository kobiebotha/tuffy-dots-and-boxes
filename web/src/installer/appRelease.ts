import { parseAppManifest, type AppFile, type AppRelease } from "./appManifest";
export type { AppRelease } from "./appManifest";

export interface PreparedApp {
  release: AppRelease;
  zip: Uint8Array;
  files: { file: AppFile; bytes: Uint8Array }[];
}

export async function sha256(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new Uint8Array(bytes));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function download(url: string, maximum: number, signal?: AbortSignal): Promise<Uint8Array> {
  const timeout = AbortSignal.timeout(60_000);
  const response = await fetch(url, {
    cache: "no-store", credentials: "omit", redirect: "error",
    signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
  });
  if (!response.ok) throw new Error("Couldn’t check the app download. Please try again.");
  if (!response.body || Number(response.headers.get("content-length")) > maximum) {
    throw new Error("The app download has an unexpected size.");
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > maximum) {
        await reader.cancel();
        throw new Error("The app download is larger than expected.");
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return bytes;
}

export async function loadAppRelease(signal?: AbortSignal): Promise<AppRelease | null> {
  const bytes = await download("/badge-app/manifest.json", 64 * 1024, signal);
  let manifest: unknown;
  try { manifest = JSON.parse(new TextDecoder().decode(bytes)); }
  catch { throw new Error("The app catalog could not be read. Please try again later."); }
  return parseAppManifest(manifest);
}

async function checkedDownload(url: string, expectedBytes: number, hash: string, signal?: AbortSignal) {
  const bytes = await download(url, expectedBytes, signal);
  if (bytes.length !== expectedBytes || await sha256(bytes) !== hash) {
    throw new Error("The app integrity check failed. Nothing has been written to your badge.");
  }
  return bytes;
}

/** Finish all downloads and integrity checks before a directory can be chosen. */
export async function prepareApp(release: AppRelease, signal?: AbortSignal): Promise<PreparedApp> {
  const checked = parseAppManifest({ schemaVersion: 1, release })!;
  const zip = await checkedDownload(checked.zipUrl, checked.zipBytes, checked.zipSha256, signal);
  const files: PreparedApp["files"] = [];
  // Small bounded batches avoid a large connection burst for asset-heavy apps.
  for (let index = 0; index < checked.files.length; index += 4) {
    files.push(...await Promise.all(checked.files.slice(index, index + 4).map(async (file) => ({
      file, bytes: await checkedDownload(file.url, file.bytes, file.sha256, signal),
    }))));
  }
  return { release: checked, zip, files };
}
