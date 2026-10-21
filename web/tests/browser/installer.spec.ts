import { createHash } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";
import { driveLog, driveSnapshot, mockDrive, resumeWrite } from "./installerDrive";

type FileTree = { [name: string]: FileTree | number[] };

async function realRelease(page: Page) {
  const response = await page.request.get("/badge-app/manifest.json");
  expect(response.ok()).toBe(true);
  const { release } = await response.json() as {
    release: { zipBytes: number; zipSha256: string; files: { path: string; url: string; bytes: number; sha256: string }[] };
  };
  expect(release.files.length).toBeGreaterThan(1);
  const tree: FileTree = {};
  for (const file of release.files) {
    const artifact = await page.request.get(file.url);
    expect(artifact.ok()).toBe(true);
    const bytes = await artifact.body();
    expect(bytes.length).toBe(file.bytes);
    expect(createHash("sha256").update(bytes).digest("hex")).toBe(file.sha256);
    const parts = file.path.split("/");
    const name = parts.pop()!;
    let directory = tree;
    for (const part of parts) directory = (directory[part] ??= {}) as FileTree;
    directory[name] = Array.from(bytes);
  }
  return { release, tree };
}

async function selectDrive(page: Page) {
  await page.getByRole("button", { name: "Choose badge drive", exact: true }).click();
}

async function screenshotInstaller(page: Page, state: "ready" | "success", width: number) {
  await page.setViewportSize({ width, height: width === 375 ? 812 : 1000 });
  await page.evaluate(async () => {
    await document.fonts.ready;
    window.scrollTo({ top: 0, behavior: "instant" });
    await new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
  });
  // Distinguish a compositing/capture artifact from duplicated React content.
  await expect(page.locator("header.top")).toHaveCount(1);
  await expect(page.locator("#install-heading")).toHaveCount(1);
  await expect(page.locator("h1")).toHaveCount(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: test.info().outputPath(`installer-${state}-${width}.png`), fullPage: true, animations: "disabled" });
}

for (const existing of [false, true]) {
  test(`${existing ? "update" : "install"} copies the published game, verifies bytes, and preserves other data`, async ({ page }) => {
    await mockDrive(page, existing ? "existing" : "normal");
    const backendRequests: string[] = [];
    const pageErrors: string[] = [];
    page.on("request", (request) => {
      if (new URL(request.url()).hostname.endsWith(".test")) backendRequests.push(request.url());
    });
    page.on("pageerror", (error) => pageErrors.push(error.message));
    const { tree } = await realRelease(page);
    await page.goto("/install");
    const before = await driveSnapshot(page);
    await expect(page.getByRole("button", { name: "Choose badge drive", exact: true })).toBeEnabled();
    const instructions = page.locator('section[aria-labelledby="app-heading"] ol');
    await expect(instructions).toContainText("Choose badge drive");
    await expect(instructions).toContainText("Copy game to badge");
    if (!existing) {
      await screenshotInstaller(page, "ready", 375);
      await page.setViewportSize({ width: 1440, height: 1000 });
    }
    await selectDrive(page);
    await expect(page.getByText("BADGEWARE", { exact: true })).toBeVisible();
    expect((await driveLog(page)).filter((entry) => !entry.startsWith("read:"))).toEqual([]);
    await page.getByRole("button", { name: existing ? "Update game on badge" : "Copy game to badge", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Game copied and verified", exact: true })).toBeVisible();
    await expect(page.getByRole("status").filter({ has: page.getByRole("heading", { name: "Game copied and verified", exact: true }) })).toContainText(/Safely eject.*press Reset/i);
    const after = await driveSnapshot(page);
    const apps = after.apps as FileTree;
    expect(after.state).toEqual(before.state);
    expect(after["main.py"]).toEqual(before["main.py"]);
    expect(after["launcher.py"]).toEqual(before["launcher.py"]);
    expect(apps.menu).toEqual((before.apps as FileTree).menu);
    expect(apps.other_game).toEqual((before.apps as FileTree).other_game);
    expect(apps.dots_boxes).toEqual(existing ? {
      ...tree, "personal.txt": ((before.apps as FileTree).dots_boxes as FileTree)["personal.txt"],
    } : tree);
    const log = await driveLog(page);
    const writes = log.filter((entry) => entry.startsWith("write:"));
    expect(writes.at(-1)).toBe("write:/apps/dots_boxes/__init__.py");
    for (const write of writes) {
      const path = write.slice("write:".length);
      expect(log.lastIndexOf(`read:${path}`)).toBeGreaterThan(log.indexOf(`close:${path}`));
    }
    expect(backendRequests).toEqual([]);
    expect(pageErrors).toEqual([]);
    if (!existing) {
      for (const width of [1440, 375]) {
        await screenshotInstaller(page, "success", width);
      }
    }
  });
}

test("copying guards navigation and unloading until verification finishes", async ({ page }) => {
  await mockDrive(page, "pause-write", "badgegotchi_native.mpy");
  await page.goto("/install");
  // Give Back a real previous path without mounting the multiplayer dashboard.
  await page.evaluate(() => {
    history.replaceState(null, "", "/");
    history.pushState(null, "", "/install");
  });
  await selectDrive(page);
  await page.getByRole("button", { name: "Copy game to badge", exact: true }).click();
  await expect.poll(() => driveLog(page)).toContain("write:/apps/dots_boxes/badgegotchi_native.mpy");
  const progress = page.getByRole("progressbar", { name: "Game files copied and verified" });
  await expect(progress).toBeVisible();
  const lobby = page.getByRole("link", { name: "Lobby", exact: true });
  await expect(lobby).toHaveAttribute("aria-disabled", "true");
  // Exercise the handler as well as the disabled accessibility state.
  await lobby.click({ force: true });
  await expect(page).toHaveURL("/install");
  await expect(progress).toBeVisible();
  await page.evaluate(() => new Promise<void>((resolve) => {
    window.addEventListener("popstate", () => queueMicrotask(resolve), { once: true });
    history.back();
  }));
  await expect(page).toHaveURL("/install");
  await expect(page.locator("#install-heading")).toHaveCount(1);
  await expect(progress).toBeVisible();
  const unloadingPrevented = () => page.evaluate(() => {
    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event);
    return event.defaultPrevented;
  });
  expect(await unloadingPrevented()).toBe(true);
  await resumeWrite(page);
  await expect(page.getByRole("heading", { name: "Game copied and verified", exact: true })).toBeVisible();
  await expect(lobby).toBeEnabled();
  expect(await unloadingPrevented()).toBe(false);
});

for (const [scenario, message] of [
  ["cancel", /No drive (was )?selected/i],
  ["denied", /permission|access/i],
  ["bootsel", /BOOTSEL drive/],
  ["wrong-folder", /must contain an apps folder/],
] as const) {
  test(`${scenario} drive selection changes no files and can be retried`, async ({ page }) => {
    await mockDrive(page, scenario);
    await page.goto("/install");
    const before = await driveSnapshot(page);
    await selectDrive(page);
    await expect(page.getByRole("alert")).toContainText(message);
    expect(await driveSnapshot(page)).toEqual(before);
    expect((await driveLog(page)).filter((entry) => !entry.startsWith("read:"))).toEqual([]);
    await expect(page.getByRole("button", { name: "Choose badge drive", exact: true })).toBeEnabled();
  });
}

test("a corrupted published file prevents drive access and writes", async ({ page }) => {
  await mockDrive(page);
  const { release } = await realRelease(page);
  const artifact = release.files.find((file) => file.path.endsWith(".mpy"))!;
  await page.route(`**${artifact.url}`, (route) => route.fulfill({
    contentType: "application/octet-stream", body: Buffer.alloc(artifact.bytes),
  }));
  await page.goto("/install");
  await expect(page.getByRole("alert")).toContainText(/integrity check failed/i);
  const choose = page.getByRole("button", { name: "Choose badge drive", exact: true });
  if (await choose.count()) await expect(choose).toBeDisabled();
  expect(await driveLog(page)).toEqual([]);
});

for (const scenario of ["write-failure", "update-failure"] as const) {
  test(`${scenario} restores the drive and gives a recoverable error`, async ({ page }) => {
    const { release } = await realRelease(page);
    const lastDependency = release.files.filter((file) => file.path !== "__init__.py").at(-1)!;
    await mockDrive(page, scenario, lastDependency.path);
    await page.goto("/install");
    const before = await driveSnapshot(page);
    await selectDrive(page);
    await page.getByRole("button", { name: scenario === "update-failure" ? "Update game on badge" : "Copy game to badge", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText(/Installation did not finish/i);
    expect(await driveSnapshot(page)).toEqual(before);
    await expect(page.getByRole("heading", { name: "Game copied and verified", exact: true })).toHaveCount(0);
  });
}

for (const unavailable of ["folder picker", "Web Locks"] as const) {
  test(`unsupported ${unavailable} keeps the ZIP download and gives matching manual instructions`, async ({ page }) => {
    await page.addInitScript((unavailable) => {
      if (unavailable === "folder picker") {
        Object.assign(window, { showDirectoryPicker: undefined });
      } else {
        Object.defineProperty(navigator, "locks", { value: undefined, configurable: true });
      }
    }, unavailable);
    const { release } = await realRelease(page);
    await page.goto("/install");
    const install = page.locator('section[aria-labelledby="app-heading"]');
    const instructions = install.locator("ol");
    await expect(instructions).toContainText("Download game ZIP");
    await expect(instructions).toContainText(/unzip/i);
    await expect(instructions).toContainText(/copy.*dots_boxes.*apps/i);
    await expect(instructions).not.toContainText(/choose.*drive|button on this page|verification finishes/i);
    await expect(install.getByRole("heading", { name: "Copy the game manually", exact: true })).toBeVisible();
    const zip = install.getByRole("link", { name: "Download game ZIP" });
    await expect(zip).toBeVisible();
    await expect(page.getByRole("button", { name: "Choose badge drive", exact: true })).toHaveCount(0);
    await expect(install.getByText(/Direct copying.*unavailable/i)).toBeVisible();
    await expect(install.getByText(/desktop Chrome or Edge/i)).toBeVisible();
    await expect(page.getByRole("heading", { name: "Game copied and verified", exact: true })).toHaveCount(0);
    if (unavailable === "folder picker") {
      await screenshotInstaller(page, "ready", 1440);
      await screenshotInstaller(page, "ready", 375);
    }
    const downloading = page.waitForEvent("download");
    await zip.click();
    const download = await downloading;
    expect(download.suggestedFilename()).toBe("dots-boxes.zip");
    const stream = await download.createReadStream();
    const chunks: Buffer[] = [];
    for await (const chunk of stream!) chunks.push(Buffer.from(chunk));
    const bytes = Buffer.concat(chunks);
    expect(bytes.length).toBe(release.zipBytes);
    expect(createHash("sha256").update(bytes).digest("hex")).toBe(release.zipSha256);
  });
}
