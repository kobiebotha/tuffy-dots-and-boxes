export function errorMessage(error: unknown): string {
  // Worker and Supabase errors can be plain objects. Avoid serializing other
  // fields, which may contain request details or credentials.
  const message = typeof error === "string" ? error
    : error && typeof error === "object" && "message" in error ? error.message : undefined;
  return typeof message === "string" && message.trim()
    ? message
    : "Could not connect to live rooms. Try again.";
}

interface ConnectionStatus {
  connected: boolean;
  connecting: boolean;
  hasSynced?: boolean;
  downloadError?: unknown;
  uploadError?: unknown;
}

export function connectionSummary(status: ConnectionStatus) {
  const error = status.downloadError ?? status.uploadError;
  if (error) return { label: "Sync problem", live: false, error: errorMessage(error) };
  if (status.connected && status.hasSynced) return { label: "Live", live: true, error: null };
  if (status.connected) return { label: "Syncing", live: false, error: null };
  if (status.hasSynced) return { label: status.connecting ? "Reconnecting" : "Offline", live: false, error: null };
  return { label: "Connecting", live: false, error: null };
}
