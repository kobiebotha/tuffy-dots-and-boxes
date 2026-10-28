import { corsHeaders } from "npm:@supabase/supabase-js@2.118.0/cors";
import { pairBadge, type PairBody, type PairDeps, validatePairBody } from "./handler.ts";

const headers = { ...corsHeaders, "Access-Control-Allow-Methods": "POST, OPTIONS" };

export class ConfigurationError extends Error {}

function json(body: Record<string, unknown>, status: number): Response {
  return Response.json(body, { status, headers });
}

export function createHandler(makeDeps: () => PairDeps, powersyncUrl: string | (() => string)) {
  return async (request: Request): Promise<Response> => {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers });
    }
    if (request.method !== "POST") {
      return Response.json({ error: "method_not_allowed" }, {
        status: 405, headers: { ...headers, Allow: "POST, OPTIONS" },
      });
    }
    let payload: PairBody;
    try {
      payload = await request.json();
    } catch {
      return json({ error: "invalid_body" }, 400);
    }
    const validated = validatePairBody(payload);
    if ("error" in validated) return json({ error: validated.error }, 400);

    try {
      const endpoint = typeof powersyncUrl === "function" ? powersyncUrl() : powersyncUrl;
      const result = await pairBadge(payload, makeDeps());
      return json({ ...result.body, powersync_url: endpoint }, result.status);
    } catch (error) {
      if (error instanceof ConfigurationError) return json({ error: "misconfigured" }, 500);
      return json({ error: "unavailable" }, 503);
    }
  };
}
