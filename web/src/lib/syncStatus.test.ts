import { describe, expect, it } from "vitest";
import { connectionSummary, errorMessage } from "./syncStatus";

describe("connection status", () => {
  it("reports live only after the connected client has synced", () => {
    expect(connectionSummary({ connected: true, connecting: false, hasSynced: true }).live).toBe(true);
    expect(connectionSummary({ connected: true, connecting: false, hasSynced: false }).label).toBe("Syncing");
  });

  it("does not mistake a saved checkpoint for a live connection", () => {
    expect(connectionSummary({ connected: false, connecting: false, hasSynced: true })).toMatchObject({ label: "Offline", live: false });
    expect(connectionSummary({ connected: false, connecting: true, hasSynced: true })).toMatchObject({ label: "Reconnecting", live: false });
  });

  it("surfaces worker errors before and after the first sync", () => {
    const downloadError = { message: "PSYNC_S2103: Invalid token" };
    expect(connectionSummary({ connected: false, connecting: true, downloadError })).toMatchObject({ label: "Sync problem", error: downloadError.message });
    expect(connectionSummary({ connected: true, connecting: false, hasSynced: true, uploadError: new Error("Upload unavailable") })).toMatchObject({ live: false, error: "Upload unavailable" });
  });

  it("reads only the error message from relayed errors", () => {
    expect(errorMessage({ message: "Connection refused", token: "private" })).toBe("Connection refused");
    expect(errorMessage({ token: "private" })).toBe("Could not connect to live rooms. Try again.");
  });
});
