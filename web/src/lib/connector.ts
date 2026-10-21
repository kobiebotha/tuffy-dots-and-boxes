import { UpdateType, type AbstractPowerSyncDatabase, type PowerSyncBackendConnector } from "@powersync/web";
import type { SupabaseClient } from "@supabase/supabase-js";
import { config } from "./config";
import { requireSession, supabase } from "./supabase";

const permanentCodes = new Set(["42501", "23505", "23514", "P0001"]);

export function createConnector(userId: string, client: SupabaseClient = supabase): PowerSyncBackendConnector {
  return {
    async fetchCredentials() {
      const session = await requireSession(userId, client);
      return {
        endpoint: config.powersyncUrl,
        token: session.access_token,
        expiresAt: session.expires_at ? new Date(session.expires_at * 1000) : undefined,
      };
    },

    async uploadData(database: AbstractPowerSyncDatabase) {
      await requireSession(userId, client);
      const transaction = await database.getNextCrudTransaction();
      if (!transaction) return;
      try {
        for (const op of transaction.crud) {
          if (op.op !== UpdateType.PUT || op.table !== "moves") {
            throw Object.assign(new Error(`Unexpected local write to ${op.table}`), { code: "P0001" });
          }
          const data = op.opData ?? {};
          const { error } = await client.rpc("submit_move", {
            move_id: op.id,
            target: data.room_id,
            orientation: data.orientation,
            row_i: data.row,
            col_i: data.col,
          });
          if (error) throw error;
        }
        await transaction.complete();
      } catch (error) {
        const code = typeof error === "object" && error && "code" in error ? String(error.code) : "";
        if (permanentCodes.has(code) || code.startsWith("22") || code.startsWith("23")) {
          console.error("Dropping a move the server rejected", error);
          await transaction.complete();
          return;
        }
        throw error;
      }
    },
  };
}
