import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";
import wasm from "vite-plugin-wasm";

export default defineConfig({
  plugins: [react(), wasm()],
  // Vite 8 uses Rolldown. Current browsers run the SDK's top-level await.
  build: { target: "esnext" },
  optimizeDeps: {
    exclude: ["@journeyapps/wa-sqlite", "@powersync/web"],
  },
  worker: {
    format: "es",
    plugins: () => [wasm()],
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
