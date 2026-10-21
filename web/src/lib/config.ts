export interface PublicConfig {
  supabaseUrl: string;
  supabaseAnonKey: string;
  powersyncUrl: string;
}

function required(name: "VITE_SUPABASE_URL" | "VITE_SUPABASE_ANON_KEY" | "VITE_POWERSYNC_URL"): string {
  const value = import.meta.env[name];
  if (!value) throw new Error(`Missing ${name}`);
  return value;
}

export const config: PublicConfig = {
  supabaseUrl: required("VITE_SUPABASE_URL"),
  supabaseAnonKey: required("VITE_SUPABASE_ANON_KEY"),
  powersyncUrl: required("VITE_POWERSYNC_URL"),
};
