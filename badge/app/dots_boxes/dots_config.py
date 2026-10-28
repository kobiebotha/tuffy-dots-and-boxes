"""Client targets are filled by badge/tools/package.py from the web environment."""

SUPABASE_URL = ""
SUPABASE_ANON_KEY = ""
POWERSYNC_URL = ""
PAIR_URL = SUPABASE_URL + "/functions/v1/badge-pair"
AUTH_URL = SUPABASE_URL + "/auth/v1/token?grant_type=refresh_token"
MOVE_URL = SUPABASE_URL + "/rest/v1/rpc/submit_move"

_APP = __file__.rsplit("/", 1)[0]
CA_FILES = (
    "/system/lib/ca/gts-root-r1.der",
    "/system/lib/ca/gts-root-r4.der",
    _APP + "/ca/sectigo-r46.der",
)
