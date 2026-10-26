"""Stepped PowerSync download, reconnect and checkpoint coordination."""
from .http import PENDING, Gateway, HTTPS, split_https
from .stream import StreamLines
from .database import MAX_SYNC_BATCH_RECORDS, MAX_SYNC_LINE_BYTES

MAX_BATCH_DELAY_MS = 1000
MAX_STREAM_READS = 2
MAX_STREAM_RECORDS = MAX_SYNC_BATCH_RECORDS


class Connector:
    """App-owned credentials and uploads, advanced once per foreground step.

    fetch_credentials returns a token, PENDING, or None (status/revoked).
    upload_pending reports unsent work; upload_data returns PENDING, True for
    acceptance, or False for rejection (requests a fresh checkpoint barrier).
    checkpoint_applied runs after commit and returns whether app writes are
    confirmed. close releases in-flight HTTP without discarding durable work.
    Override credentials_valid when tokens expire. A read-only client needs
    only fetch_credentials and invalidate_credentials.
    """
    status = "NEED AUTH"
    revoked = False
    notice = ""

    def fetch_credentials(self):
        raise NotImplementedError

    def invalidate_credentials(self):
        raise NotImplementedError

    def credentials_valid(self):
        return True

    def upload_pending(self):
        return False

    def upload_data(self):
        return True

    def checkpoint_applied(self, store, full_checkpoint):
        return True

    def close(self):
        pass


class SyncClient:
    def __init__(self, store, connector, endpoint, subscription, ca_files,
                 heartbeat=None, http_factory=HTTPS, json_factory=Gateway):
        self.store, self.connector = store, connector
        self.subscription = subscription
        self.endpoint = endpoint.rstrip("/")
        split_https(self.endpoint)
        self.ca_files = ca_files
        self.heartbeat = heartbeat
        self.http_factory, self.json_factory = http_factory, json_factory
        self.token = None
        self.phase = "token"
        self.remote = self.stream = None
        self.request = None
        self.checkpoint = None
        self.target = None
        self.lines = StreamLines()
        self.batch = []
        self.parsed_batch = []
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
        self.needs_write_barrier = False

    def _control(self, command, payload=None):
        return self.store.control(command, payload)

    def _disconnect(self):
        if self.stream is not None:
            self.stream.close()
        if self.remote is not None:
            self.remote.close()
        self.stream = self.remote = None
        self.connector.close()
        if self.core_started:
            try:
                self._control("stop")
            except Exception:
                pass
            self.core_started = False
        self.lines = StreamLines()
        self.batch = []
        self.parsed_batch = []
        self.batch_bytes = self.batch_ms = 0
        self.connected = self.synced = False
        self.checkpoint_seeded = False

    def close(self):
        if self.closed:
            return
        self._disconnect()
        self._control("subscriptions", {"unsubscribe": self.subscription})
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
                if line.startswith("Subscription to stream " + self.subscription["name"] + " ") and "could not be resolved" in line:
                    raise ValueError("PowerSync subscription unavailable; update sync configuration")
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
                    if self.connector.checkpoint_applied(self.store, full_checkpoint):
                        self.needs_write_barrier = False
                        self.synced = True
                        self.status = "SYNCED"
                        self.backoff_ms = 2000
            elif "CheckpointDelayedDueToLocalData" in instruction:
                # Core uses this for both SQL CRUD and a checkpoint older than
                # our accepted upload's target. The latter is normal: keep the
                # stream and pending upload until a newer checkpoint applies.
                if self.store.has_local_writes():
                    raise ValueError("Unexpected local SQL writes; retained")
                self.synced = False
                self.status = "SYNCING"

    def _invalidate_token(self):
        self.token = None
        self.connector.invalidate_credentials()

    def _after_upload(self, accepted=False):
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
        # Rejected uploads can also mean the cached data is stale. A new target
        # makes both outcomes wait for the server's current data.
        if not self.checkpoint_seeded:
            self.phase, self.status = "upload_seed", "SYNCING"
            return
        following = str(self._control("next_checkpoint_request_id"))
        self.checkpoint = {"client_id": self.checkpoint["client_id"], "checkpoint_request_id": following}
        self.phase, self.status = "upload_checkpoint", "SYNCING"

    def _checkpoint_request(self, request):
        if self.remote is None:
            self.remote = self.json_factory(self.endpoint + "/sync/checkpoint-request", self.ca_files,
                                            heartbeat=self.heartbeat, token=self.token)
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
            if self.live_upload and self.stream is not None and self.phase in ("upload", "upload_seed", "upload_checkpoint"):
                # A second TLS connection is optional. Release the download
                # socket and retry through the one-socket startup path. The
                # connector must retain an uncertain upload for idempotent retry.
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
            print("POWERSYNC_ERROR", type(exc).__name__, self.last_error)
            self._disconnect()
            self.phase = "blocked"
            self.status = "SYNC ERROR"

    def _step(self):
        pending = self.connector.upload_pending()
        if pending and self.phase not in ("token", "upload"):
            if (self.live_upload and self.phase == "stream" and self.connected and self.core_started
                    and self.connector.credentials_valid()):
                self._flush()
                self.synced = False
                self.phase = "upload"
                return
            self._disconnect()
            self.phase = "token"
            return
        if self.phase == "token":
            if self.core_started:
                self._disconnect()
            self.status = "CONNECTING"
            token = self.connector.fetch_credentials()
            if token is PENDING:
                return
            if token is None:
                self.status = self.connector.status
                if self.connector.revoked:
                    self._disconnect()
                    self.phase = "revoked"
                return
            self.token = token
            self.phase = "upload" if pending else "start"
            return
        if self.phase == "upload":
            if not self.connector.credentials_valid():
                self.phase = "token"
                return
            self.status = "SENDING"
            accepted = self.connector.upload_data()
            self.notice = self.connector.notice
            if accepted is PENDING:
                if not self.connector.credentials_valid():
                    self.phase = "token"
                return
            self._after_upload(accepted=accepted)
            return
        if self.phase == "upload_seed":
            accepted = self._checkpoint_request(self.checkpoint)
            if accepted is PENDING:
                return
            following = self.store.seed_checkpoint(accepted)
            self.checkpoint_seeded = True
            self.checkpoint = {"client_id": self.checkpoint["client_id"], "checkpoint_request_id": following}
            self.phase = "upload_checkpoint"
            return
        if self.phase == "upload_checkpoint":
            accepted = self._checkpoint_request(self.checkpoint)
            if accepted is PENDING:
                return
            self.target = accepted
            self._control("target_checkpoint_request_id", accepted)
            self.phase, self.status = "stream", "SYNCING"
            return
        if self.phase == "start":
            self.status = "SYNCING"
            # Retire persisted subscriptions before selecting the bounded stream.
            # Native 0.5.4 names the input key `params`; it emits `parameters`
            # in the HTTP request. Unknown input keys are silently ignored.
            stream = self.subscription
            before = self.phase
            self.core_started = True
            # The connector resolves durable uploads against published data.
            # Store supplies the registered raw-table schema on every start.
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
                                            token=self.token, streaming=True, heartbeat=self.heartbeat)
            self.phase, self.status = "stream", "SYNCING"
            return
        if self.phase == "stream":
            self._download()

    def _download(self):
        """Drain available input within fixed transport and core-record budgets.

        A fragment ending midway through a record can be completed by the next
        immediately available read in this update. No network wait holds a SQL
        transaction, and six records remain the per-update native-work bound.
        """
        reads = 0
        # A partial batch belongs to this update's eventual native work too.
        # Counting only newly framed records could commit a carried five plus
        # six new records before yielding, despite the six-record work budget.
        processed = len(self.batch)
        while self.phase == "stream":
            while processed < MAX_STREAM_RECORDS and self._line():
                processed += 1
                if self.phase != "stream":
                    return
            if processed >= MAX_STREAM_RECORDS or reads >= MAX_STREAM_READS:
                return
            data = self.stream.step()
            reads += 1
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

    def _line(self):
        record = self.lines.pop_record()
        if record is None:
            return False
        line, marker = record
        size = len(line.encode())
        if self.batch and self.batch_bytes + size > MAX_SYNC_LINE_BYTES:
            self._flush()
        self.batch.append(line)
        self.parsed_batch.append(marker)
        self.batch_bytes += size
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
        results = self.store.control_lines(self.batch, parsed=self.parsed_batch)
        self.batch = []
        self.parsed_batch = []
        self.batch_bytes = self.batch_ms = 0
        for result in results:
            self._instructions(result["instructions"], full_checkpoint=result["full_checkpoint"])
