"""Supabase credentials and durable game moves for the PowerSync client."""
import time

from powersync.http import PENDING, Gateway, RetryableNetworkError
from powersync.sync import Connector, SyncClient

BADGE_STREAM = "badge_game_v2"


class GameConnector(Connector):
    def __init__(self, records, auth_url, move_url, anon_key, ca_files,
                 heartbeat=None, json_factory=Gateway, store=None):
        self.records = records
        self.store = store
        self.auth_url, self.move_url, self.anon_key = auth_url, move_url, anon_key
        self.ca_files, self.heartbeat, self.json_factory = ca_files, heartbeat, json_factory
        self.remote = None
        self.notice = ""
        self.status = "NEED PAIR"
        self.revoked = False

    def credentials_valid(self):
        record = self.records.record
        return bool(record["access_token"] and record["expires_at"]
                    and record["expires_at"] - int(time.time()) > 60)

    def fetch_credentials(self):
        record = self.records.record
        if not record["refresh_token"]:
            return None
        if self.credentials_valid():
            return record["access_token"]
        result = self._post(self.auth_url, {"refresh_token": record["refresh_token"]})
        if result is PENDING:
            return PENDING
        status = result.get("http_status", 200 if "access_token" in result else 503)
        if status == 401 or result.get("error") in ("invalid_grant", "invalid_request"):
            self.revoked, self.status = True, "SIGN IN AGAIN"
            return None
        if status >= 500 or status == 429:
            raise RetryableNetworkError("Session refresh unavailable")
        if "access_token" not in result or "refresh_token" not in result:
            raise ValueError("Invalid session refresh response")
        expires_in = int(result.get("expires_in", 3600))
        self.records.save(dict(record, access_token=result["access_token"],
                               refresh_token=result["refresh_token"],
                               expires_at=int(time.time()) + max(0, expires_in)))
        return self.records.record["access_token"]

    def invalidate_credentials(self):
        record = self.records.record
        if record["access_token"] is not None or record["expires_at"]:
            self.records.save(dict(record, access_token=None, expires_at=0))

    def upload_data(self, move):
        """Process the real SDK-selected mutation; the SDK owns its lifecycle."""
        record = self.records.record
        self.store.validate_move(move)
        if move["room_id"] != record["room_id"]:
            raise ValueError("SDK mutation belongs to another room")
        if self.store.has_move(move["id"], move["room_id"]):
            return True
        result = self._post(self.move_url, {
            "move_id": move["id"], "target": move["room_id"],
            "orientation": move["orientation"], "row_i": move["row"], "col_i": move["col"],
        }, token=record["access_token"])
        if result is PENDING:
            return PENDING
        status = result.get("http_status", 200)
        if status == 401:
            self.invalidate_credentials()
            raise RetryableNetworkError("Move token expired")
        if status >= 500 or status == 429:
            raise RetryableNetworkError("Move upload unavailable")
        if status >= 400:
            self.notice = str(result.get("message") or result.get("error") or "Move rejected")[:48]
            return False
        self.notice = ""
        return True

    def _post(self, url, body, token=None):
        # Only HTTP operations are translated into retryable failures. Session
        # saves below the caller remain ordinary OSError and must fail closed.
        try:
            if self.remote is None:
                self.remote = self.json_factory(url, self.ca_files, heartbeat=self.heartbeat,
                                                token=token, headers=self._headers())
                self.remote.start(body)
            result = self.remote.step()
        except OSError as exc:
            self.close()
            raise RetryableNetworkError(str(exc))
        if result is PENDING:
            return PENDING
        self.close()
        return result

    def _headers(self):
        if not isinstance(self.anon_key, str) or len(self.anon_key) < 20:
            raise ValueError("Supabase anon key is not configured")
        return {"apikey": self.anon_key}

    def close(self):
        remote, self.remote = self.remote, None
        if remote is not None:
            try:
                remote.close()
            except OSError:
                # Teardown of a broken socket cannot strand the next request.
                pass


class Sync(SyncClient):
    """Wire this game's connector and room subscription into the SDK."""
    def __init__(self, store, records, endpoint, auth_url, move_url, anon_key, ca_files,
                 heartbeat=None, **transport):
        connector = GameConnector(records, auth_url, move_url, anon_key, ca_files,
                                  heartbeat=heartbeat, json_factory=transport.get("json_factory", Gateway), store=store)
        super().__init__(store, connector, endpoint,
                         {"name": BADGE_STREAM, "params": {"room_id": records.record["room_id"]}},
                         ca_files, heartbeat=heartbeat, **transport)
