"""Client targets are filled by badge/tools/package.py from the web environment."""

SUPABASE_URL = 'https://rjjvtmqicwtikbveofyq.supabase.co'
SUPABASE_ANON_KEY = 'sb_publishable_SYDTKFnJ58aDT3kH0Kkr7Q_P4Lfhs6g'
POWERSYNC_URL = 'https://6ad7f348423e048d1cdd5f03.powersync.journeyapps.com'
PAIR_URL = SUPABASE_URL + "/functions/v1/badge-pair"
AUTH_URL = SUPABASE_URL + "/auth/v1/token?grant_type=refresh_token"
MOVE_URL = SUPABASE_URL + "/rest/v1/rpc/submit_move"

_APP = __file__.rsplit("/", 1)[0]
CA_FILES = (
    "/system/lib/ca/gts-root-r1.der",
    "/system/lib/ca/gts-root-r4.der",
    _APP + "/ca/sectigo-r46.der",
)
