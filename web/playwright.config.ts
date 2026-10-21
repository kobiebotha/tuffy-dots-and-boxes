import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/browser",
  fullyParallel: false,
  workers: 1,
  timeout: 45_000,
  expect: { timeout: 15_000 },
  reporter: "list",
  outputDir: "./test-results/browser",
  use: {
    baseURL: "http://127.0.0.1:5197",
    channel: process.env.PLAYWRIGHT_CHANNEL ?? "chrome",
    viewport: { width: 1440, height: 1000 },
    serviceWorkers: "block",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "npm run dev -- --port 5197 --strictPort",
    url: "http://127.0.0.1:5197",
    reuseExistingServer: false,
    timeout: 60_000,
    env: {
      // Installation must work even when the multiplayer backend is unconfigured.
      VITE_SUPABASE_URL: "",
      VITE_SUPABASE_ANON_KEY: "",
      VITE_POWERSYNC_URL: "",
    },
  },
});
