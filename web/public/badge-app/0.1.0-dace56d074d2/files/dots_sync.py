"""Refresh the Supabase session, send one pending move, then download PowerSync.

Moves go to PostgREST. The SQL outbox stays empty. A badge subscribes to only
its paired room; the web lobby and the user's old games never fill LittleFS.
"""
import json
import time

from dots_http import PENDING, Gateway, HTTPS, split_https
from dots_stream import StreamLines
from dots_storage import MAX_SYNC_BATCH_RECORDS, MAX_SYNC_LINE_BYTES

# TCP/TLS can split one checkpoint over multiple nonblocking reads. Keep the
# bounded wire records in RAM briefly, without opening a SQL transaction, so a
# network pause does not turn each fragment into a durable flash rewrite.
MAX_BATCH_DELAY_MS = 1000
BADGE_STREAM = "badge_game_v2"


class Sync:
    def __init__(self, store, records, endpoint, auth_url, move_url, anon_key, ca_files,
                 heartbeat=None, http_factory=HTTPS, json_factory=Gateway):
        self.store = store
        self.records = records
        self.room_id = records.record["room_id"]
        self.endpoint = endpoint.rstrip("/")
        split_https(self.endpoint)
        self.auth_url = auth_url
        self.move_url = move_url
        self.anon_key = anon_key
        self.ca_files = ca_files
        self.heartbeat = heartbeat
        self.http_factory = http_factory
        self.json_factory = json_factory
        self.phase = "token"
        self.remote = self.stream = None
        self.request = None
        self.checkpoint = None
        self.target = None
        self.lines = StreamLines()
        self.batch = []
        self.batch_bytes = self.batch_ms = 0
        self.connected = False
        self.synced = False
        self.status = "CONNECTING"
        self.notice = ""
        self.retry_ms = 0
        self.backoff_ms = 2000
        self.closed = False
        self.core_started = False
        self.generation = 0
        self.last_error = ""
        self.live_upload = True
        self.checkpoint_seeded = False
        self.uploaded_move_id = None
        self.needs_write_barrier = False

    def _control(self, command, payload=None):
        return self.store.control(command, payload)

    def _headers(self):
        if not isinstance(self.anon_key, str) or len(self.anon_key) < 20:
            raise ValueError("Supabase anon key is not configured")
        return {"apikey": self.anon_key}

    def _disconnect(self):
        if self.stream is not None:
            self.stream.close()
        if self.remote is not None:
            self.remote.close()
        self.stream = self.remote = None
        if self.core_started:
            try:
                self._control("stop")
            except Exception:
                pass
            self.core_started = False
        self.lines = StreamLines()
        self.batch = []
        self.batch_bytes = self.batch_ms = 0
        self.connected = self.synced = False
        self.checkpoint_seeded = False

    def close(self):
        self._disconnect()
        self._control("subscriptions", {"unsubscribe": {
            "name": BADGE_STREAM, "params": {"room_id": self.room_id},
        }})
        self.closed = True

    def _retry(self):
        self._disconnect()
        self.phase = "token"
        self.retry_ms = self.backoff_ms
        self.backoff_ms = min(60000, self.backoff_ms * 2)
        self.status = "OFFLINE"

    def _instructions(self, instructions, full_checkpoint=False):
        if not isinstance(instructions, list):
            raise ValueError("Invalid core response")
        for instruction in instructions:
            if "LogLine" in instruction:
                line = instruction["LogLine"].get("line", "")
                if line.startswith("Subscription to stream " + BADGE_STREAM + " ") and "could not be resolved" in line:
                    raise ValueError("PowerSync badge subscription unavailable; update sync configuration")
            elif "EstablishSyncStream" in instruction:
                establish = instruction["EstablishSyncStream"]
                self.request = establish["request"]
                self.checkpoint = establish["checkpoint_request"]
                self.phase = "seed" if self.needs_write_barrier else "open_stream"
            elif "FetchCredentials" in instruction:
                self._invalidate_token()
                self.phase = "token"
            elif "CloseSyncStream" in instruction:
                if self.stream is not None:
                    self.stream.close()
                    self.stream = None
                self.connected = self.synced = False
                self.phase = "token"
            elif "DidCompleteSync" in instruction:
                self.generation += 1
                applied = instruction["DidCompleteSync"].get("applied_checkpoint_request_id")
                covered = (self.target is None and full_checkpoint) or (
                    self.target is not None and applied is not None and int(applied) >= int(self.target))
                if covered:
                    record = self.records.record
                    move = record["pending_move"]
                    # Moves are immutable canonical rows with the client's UUID.
                    # Seeing this exact room/move in a fully applied checkpoint
                    # confirms the upload and its derived board atomically.
                    if (full_checkpoint and move is not None
                            and self.store.has_move(move["id"], record["room_id"])):
                        self.records.save(dict(record, pending_move=None))
                        self.uploaded_move_id = None
                        self.needs_write_barrier = False
                    if self.records.record["pending_move"] is None:
                        self.synced = True
                        self.status = "SYNCED"
                        self.backoff_ms = 2000
            elif "CheckpointDelayedDueToLocalData" in instruction:
                # Core uses this for both SQL CRUD and a checkpoint older than
                # our accepted upload's target. The latter is normal: keep the
                # stream and saved UUID until a newer checkpoint applies.
                if self.store.has_local_writes():
                    raise ValueError("Unexpected local SQL writes; retained")
                self.synced = False
                self.status = "SYNCING"

    def _invalidate_token(self):
        record = self.records.record
        if record["access_token"] is not None or record["expires_at"]:
            self.records.save(dict(record, access_token=None, expires_at=0))

    def _after_move(self, accepted=False):
        if accepted:
            if self.target is not None:
                self._control("target_checkpoint_request_id", "0")
            self.target = None
            self.needs_write_barrier = False
            self.phase = "stream" if self.stream is not None else "start"
            self.status = "SYNCING"
            return
        self.needs_write_barrier = True
        if self.stream is None:
            self.phase = "start"
            return
        # Rejected moves can also mean the cached turn is stale. A new target
        # makes both outcomes wait for the server's current board.
        if not self.checkpoint_seeded:
            self.phase, self.status = "move_seed", "SYNCING"
            return
        following = str(self._control("next_checkpoint_request_id"))
        self.checkpoint = {"client_id": self.checkpoint["client_id"], "checkpoint_request_id": following}
        self.phase, self.status = "move_checkpoint", "SYNCING"

    def _post(self, url, body, token=None):
        if self.remote is None:
            self.remote = self.json_factory(url, self.ca_files, heartbeat=self.heartbeat,
                                            token=token, headers=self._headers())
            self.remote.start(body)
        result = self.remote.step()
        if result is PENDING:
            return PENDING
        self.remote.close()
        self.remote = None
        return result

    def _checkpoint_request(self, request):
        if self.remote is None:
            self.remote = self.json_factory(self.endpoint + "/sync/checkpoint-request", self.ca_files,
                                            heartbeat=self.heartbeat, token=self.records.record["access_token"])
            self.remote.start(request)
        result = self.remote.step()
        if result is PENDING:
            return PENDING
        self.remote.close()
        self.remote = None
        if result.get("http_status") == 401:
            self._invalidate_token()
            raise OSError("PowerSync token expired")
        if "error" in result:
            raise OSError("PowerSync checkpoint unavailable")
        value = result.get("data", {}).get("checkpoint_request_id")
        if (not isinstance(value, str) or not value.isdigit() or len(value) > 19
                or not 0 < int(value) < 9223372036854775807):
            raise ValueError("Invalid PowerSync checkpoint acknowledgment")
        return value

    def step(self, elapsed_ms=0):
        if self.closed or self.phase in ("blocked", "revoked"):
            return
        self.retry_ms = max(0, self.retry_ms - max(0, elapsed_ms))
        if self.retry_ms:
            return
        if self.batch:
            self.batch_ms += max(0, elapsed_ms)
        try:
            self._step()
        except MemoryError:
            self.last_error = "Not enough memory for simultaneous HTTPS requests"
            if self.live_upload and self.stream is not None and self.phase in ("move", "move_seed", "move_checkpoint"):
                # A second TLS connection is optional. Release the download
                # socket and retry through the one-socket startup path. The
                # saved move UUID still makes an uncertain upload idempotent.
                self.live_upload = False
                self._retry()
            else:
                self._disconnect()
                self.phase, self.status = "blocked", "LOW MEMORY"
        except OSError as exc:
            self.last_error = str(exc)[:96]
            self._retry()
        except (ValueError, RuntimeError) as exc:
            self.last_error = str(exc)[:96]
            print("DOTS_SYNC_ERROR", type(exc).__name__, self.last_error)
            self._disconnect()
            self.phase = "blocked"
            self.status = "SYNC ERROR"

    def _step(self):
        record = self.records.record
        pending = record["pending_move"]
        uploaded = pending is not None and pending["id"] == self.uploaded_move_id
        if pending and not uploaded and self.phase not in ("token", "move"):
            if (self.live_upload and self.phase == "stream" and self.connected and self.core_started
                    and record["expires_at"] and record["expires_at"] - int(time.time()) > 60):
                self._flush()
                self.synced = False
                self.phase = "move"
                return
            self._disconnect()
            self.phase = "token"
            return
        if self.phase == "token":
            if self.core_started:
                self._disconnect()
            if not record["refresh_token"]:
                self.status = "NEED PAIR"
                return
            now = int(time.time())
            if record["access_token"] and record["expires_at"] and record["expires_at"] - now > 60:
                self.phase = "move" if pending and not uploaded else "start"
                return
            self.status = "CONNECTING"
            result = self._post(self.auth_url, {"refresh_token": record["refresh_token"]})
            if result is PENDING:
                return
            status = result.get("http_status", 200 if "access_token" in result else 503)
            if status == 401 or result.get("error") in ("invalid_grant", "invalid_request"):
                self._disconnect()
                self.phase, self.status = "revoked", "SIGN IN AGAIN"
                return
            if "access_token" not in result or "refresh_token" not in result:
                raise OSError("Session refresh unavailable")
            updated = dict(record)
            updated["access_token"] = result["access_token"]
            updated["refresh_token"] = result["refresh_token"]
            expires_in = result.get("expires_in", 3600)
            if type(expires_in) is not int:
                expires_in = int(expires_in)
            updated["expires_at"] = int(time.time()) + max(0, expires_in)
            self.records.save(updated)
            self.phase = "move" if pending and not uploaded else "start"
            return
        if self.phase == "move":
            move = record["pending_move"]
            if move is None:
                self.phase = "start"
                return
            self.status = "SENDING"
            result = self._post(self.move_url, {
                "move_id": move["id"],
                "target": record["room_id"],
                "orientation": move["orientation"],
                "row_i": move["row"],
                "col_i": move["col"],
            }, token=record["access_token"])
            if result is PENDING:
                return
            status = result.get("http_status", 200)
            if status == 401:
                self._invalidate_token()
                self.phase = "token"
                return
            if status >= 500 or status == 429:
                raise OSError("Move upload unavailable")
            if status >= 400:
                self.notice = str(result.get("message") or result.get("error") or "Move rejected")[:48]
                updated = dict(record)
                updated["pending_move"] = None
                self.records.save(updated)
                self._after_move()
                return
            self.uploaded_move_id = move["id"]
            self.notice = ""
            self._after_move(accepted=True)
            return
        if self.phase == "move_seed":
            accepted = self._checkpoint_request(self.checkpoint)
            if accepted is PENDING:
                return
            following = self.store.seed_checkpoint(accepted)
            self.checkpoint_seeded = True
            self.checkpoint = {"client_id": self.checkpoint["client_id"], "checkpoint_request_id": following}
            self.phase = "move_checkpoint"
            return
        if self.phase == "move_checkpoint":
            accepted = self._checkpoint_request(self.checkpoint)
            if accepted is PENDING:
                return
            self.target = accepted
            self._control("target_checkpoint_request_id", accepted)
            self.phase, self.status = "stream", "SYNCING"
            return
        if self.phase == "start":
            self.status = "SYNCING"
            # Old versions persisted all default streams. Explicitly remove
            # those TTLs and any previous room before opening the bounded one.
            # Native 0.5.4 names the input key `params`; it emits `parameters`
            # in the HTTP request. Unknown input keys are silently ignored.
            stream = {"name": BADGE_STREAM, "params": {"room_id": self.room_id}}
            before = self.phase
            self.core_started = True
            # A resumed UUID is idempotently uploaded first, then confirmed by
            # its published move row. Old checkpoint targets must not block it.
            # Store supplies the registered raw-table schema on every start;
            # publication and UUID confirmation still wait for a full checkpoint.
            self.needs_write_barrier = False
            self.target = None
            self._instructions(self.store.start_sync(stream, {
                "checkpoint_mode": "requests",
                "include_defaults": False,
                "parameters": {},
                "active_streams": [stream],
            }, clear_target=not self.needs_write_barrier))
            if self.phase == before:
                raise ValueError("PowerSync did not open a stream")
            return
        if self.phase == "seed":
            accepted = self._checkpoint_request(self.checkpoint)
            if accepted is PENDING:
                return
            following = self.store.seed_checkpoint(accepted)
            self.checkpoint_seeded = True
            self.checkpoint = {"client_id": self.checkpoint["client_id"], "checkpoint_request_id": following}
            self.phase = "barrier"
            return
        if self.phase == "barrier":
            accepted = self._checkpoint_request(self.checkpoint)
            if accepted is PENDING:
                return
            self.target = accepted
            self._control("target_checkpoint_request_id", accepted)
            self.phase = "open_stream"
            return
        if self.phase == "open_stream":
            self.stream = self.http_factory(self.endpoint + "/sync/stream", self.request, self.ca_files,
                                            token=record["access_token"], streaming=True, heartbeat=self.heartbeat)
            self.phase, self.status = "stream", "SYNCING"
            return
        if self.phase == "stream":
            buffered = self._drain()
            if self.phase != "stream":
                return
            if buffered:
                return
            data = self.stream.step()
            if self.stream.status is not None and not self.connected:
                if self.stream.status == 401:
                    self._invalidate_token()
                    self._retry()
                    return
                if self.stream.status != 200:
                    if 400 <= self.stream.status < 500 and self.stream.status != 429:
                        raise ValueError("PowerSync stream HTTP %d; check sync configuration" % self.stream.status)
                    raise OSError("PowerSync stream unavailable")
                self.connected = True
                self._instructions(self._control("connection", "established"))
            if data is PENDING:
                # Give bytes already waiting in TLS a chance to complete the
                # checkpoint before flushing an old partial batch. The limit
                # still commits an idle metadata tail promptly.
                if self.batch and self.batch_ms >= MAX_BATCH_DELAY_MS:
                    self._flush()
                return
            if data is None:
                self.lines.finish()
                self._flush()
                self._retry()
                return
            self.lines.feed(data)
            self._drain()

    def _drain(self):
        """Drain a bounded number of already-buffered records before flushing."""
        processed = False
        for _ in range(MAX_SYNC_BATCH_RECORDS):
            if not self._line():
                break
            processed = True
            if self.phase != "stream":
                break
        return processed

    def _line(self):
        line = self.lines.pop()
        if line is None:
            return False
        size = len(line.encode())
        if self.batch and self.batch_bytes + size > MAX_SYNC_LINE_BYTES:
            self._flush()
        self.batch.append(line)
        self.batch_bytes += size
        marker = {} if line.startswith('{"data":') else json.loads(line)
        complete = "checkpoint_complete" in marker or "partial_checkpoint_complete" in marker
        # Each completion may publish views. Keep at most one publication in a
        # transaction to bound the qualified native core's fixed 256KB arena.
        if (complete or len(self.batch) >= MAX_SYNC_BATCH_RECORDS
                or self.batch_bytes >= MAX_SYNC_LINE_BYTES):
            self._flush()
        return True

    def _flush(self):
        if not self.batch:
            return
        results = self.store.control_lines(self.batch)
        self.batch = []
        self.batch_bytes = self.batch_ms = 0
        for result in results:
            self._instructions(result["instructions"], full_checkpoint=result["full_checkpoint"])
