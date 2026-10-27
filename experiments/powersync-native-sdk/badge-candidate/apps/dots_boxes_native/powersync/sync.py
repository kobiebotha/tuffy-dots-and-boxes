"""Thin Badgeware platform adapter around the real Rust PowerSync SDK.

Rust owns stream framing, subscriptions, CRUD upload retries and checkpoints.
Python supplies verified HTTPS and backend connector callbacks through native
mailboxes. There is no custom-client fallback in this candidate.
"""
import binascii
import json

from .http import PENDING, HTTPS, Gateway, BODY_LIMIT, split_https, RetryableNetworkError


class Connector:
    status = "NEED AUTH"
    revoked = False
    notice = ""

    def fetch_credentials(self):
        raise NotImplementedError

    def invalidate_credentials(self):
        raise NotImplementedError

    def credentials_valid(self):
        return True

    def upload_data(self, move):
        return True

    def checkpoint_applied(self, store, full_checkpoint):
        return True

    def close(self):
        pass


class SDKHTTPS(HTTPS):
    """Use the qualified stepped TLS machinery for an SDK-authored request."""
    def __init__(self, request, ca_files, heartbeat=None):
        url = request["url"]
        method = request["method"]
        if method not in ("POST", "GET"):
            raise ValueError("Unsupported native HTTP method")
        body = request.get("body")
        content = body.encode() if body is not None else b""
        if len(content) > BODY_LIMIT:
            raise ValueError("Native HTTP request exceeds limit")
        streaming = url.split("?", 1)[0].endswith("/sync/stream")
        # Initialize the existing transport/socket/deadline state; replace only
        # the generated HTTP bytes so Rust owns method/body/auth semantics.
        super().__init__(url, {}, ca_files, streaming=streaming, heartbeat=heartbeat)
        header = (method + " " + self.path + " HTTP/1.1\r\nHost: " + self.host
                  + "\r\nConnection: close\r\nAccept-Encoding: identity\r\n"
                  + "Accept: " + ("application/x-ndjson" if streaming else "application/json")
                  + "\r\nContent-Length: " + str(len(content)) + "\r\n")
        for key, value in request.get("headers", ()):
            if (not isinstance(key, str) or not isinstance(value, str) or not key
                    or len(key) > 64 or len(value) > 8192
                    or any(c in key + value for c in "\r\n")):
                raise ValueError("Invalid native HTTP header")
            lowered = key.lower()
            if lowered in ("host", "connection", "content-length", "accept", "accept-encoding"):
                continue
            header += key + ": " + value + "\r\n"
        self.request = (header + "\r\n").encode() + content


class SyncClient:
    """Keep the existing UI interface while advancing native SDK mailboxes."""
    NATIVE_SDK = True

    def __init__(self, store, connector, endpoint, subscription, ca_files,
                 heartbeat=None, http_factory=SDKHTTPS, json_factory=Gateway):
        self.store, self.connector = store, connector
        self.endpoint = endpoint.rstrip("/")
        split_https(self.endpoint)
        self.subscription, self.ca_files = subscription, ca_files
        self.heartbeat, self.http_factory = heartbeat, http_factory
        self.native = store.native
        required = ("sdk_start", "sdk_poll", "sdk_reply", "sdk_stop")
        if not all(hasattr(self.native, name) for name in required):
            raise RuntimeError("This candidate requires the full native PowerSync SDK")
        self.remote = {}
        self.request_urls = {}
        self._await_seed = set()
        self._active_http = None
        self._single_tls = False
        self._network_operation = False
        self._resource_retry_ms = 0
        self.upload = None
        self._upload_replied = set()
        self._platform_offline = False
        self._native_upload_error = False
        self.headers_sent = set()
        self.can_feed = ()
        self._http_cursor = 0
        self.connected = self.synced = False
        self.closed = False
        self.phase, self.status = "token", "CONNECTING"
        self.notice = self.last_error = ""
        self.generation = self.full_generation = 0
        self._full_seen = False
        self._credentials_needed = False
        self._authenticating = self._uploading = False
        self.needs_write_barrier = False
        self._barrier_full_start = 0
        # Retire prior room TTLs before ownership passes to the native SDK.
        self.store.retire_subscriptions()
        self.native.sdk_start(self.store.schema, self.endpoint,
                              self.subscription["params"]["room_id"])
        self.phase = "stream"

    def _reply(self, value):
        self.native.sdk_reply(json.dumps(value))

    def _poll(self, elapsed_ms):
        result = self.native.sdk_poll(max(0, elapsed_ms), 6)
        state = json.loads(result) if isinstance(result, str) else result
        if not isinstance(state, dict):
            raise ValueError("Invalid native SDK status")
        upload_closed = set(state.get("upload_close", ()))
        for identity in upload_closed:
            self._upload_replied.discard(identity)
            if self.upload is not None and self.upload["id"] == identity:
                self.connector.close()
                self.upload = None
                self._uploading = self._authenticating = False
        upload = state.get("upload")
        if upload is not None:
            identity = upload["id"]
            if identity not in upload_closed and identity not in self._upload_replied:
                self.store.validate_move(upload["move"])
                if self.upload is not None and self.upload != upload:
                    raise ValueError("Overlapping native upload requests")
                if self.upload is None:
                    self.upload = upload
                    self._platform_offline = False
        self._native_upload_error = bool(state.get("upload_error"))
        notify = getattr(self.store, "notify_watches", None)
        if notify is not None:
            notify(state.get("changed_watches", ()))
        closed = set(state.get("close", ()))
        for identity in closed:
            self._close_http(identity)
        if state.get("invalidate_credentials"):
            self.connector.close()
            self._authenticating = self._uploading = False
            self.connector.invalidate_credentials()
        for request in state.get("requests", ()):
            identity = request["id"]
            if identity in closed:
                # A request can be created and canceled in the same Rust poll.
                # Its mailbox is already gone; do not open an orphan transport.
                continue
            if identity in self.remote:
                raise ValueError("Duplicate native HTTP request")
            if len(self.remote) >= 3 or not request["url"].startswith(self.endpoint + "/"):
                raise ValueError("Unexpected native HTTP target")
            self.remote[identity] = self.http_factory(request, self.ca_files, heartbeat=self.heartbeat)
            self.request_urls[identity] = request["url"]
            if request["url"].split("?", 1)[0].endswith("/sync/stream"):
                # Core 0.5.4 Requests mode always seeds each download iteration.
                # Its seed future runs independently of stream response headers.
                self._await_seed.add(identity)
        self.can_feed = state.get("can_feed", ())
        self._credentials_needed = bool(state.get("credentials_needed"))
        self.connected = bool(state.get("connected"))
        self.generation = int(state.get("generation", self.generation))
        full = int(state.get("full_generation", self.full_generation))
        if full < self.full_generation:
            raise ValueError("Native publication generation went backwards")
        publication_advanced = full > self.full_generation
        if publication_advanced:
            self.full_generation = full
            self._full_seen = self.connected
        # SDK has_synced includes cached progress. A reconnect must publish a
        # fresh full checkpoint before this UI can claim from its cached board.
        if not self.connected:
            self._full_seen = False
        if (self.needs_write_barrier
                and self.full_generation > self._barrier_full_start):
            # CRUD marks the core's target before upload. The SDK advances it
            # with its write checkpoint, so an uncovered full cannot publish.
            self.needs_write_barrier = False
        if state.get("error"):
            self.last_error = str(state["error"])[:96]
        self._update_status(bool(state.get("synced")))

    def _update_status(self, native_synced):
        self.synced = bool(native_synced and self.connected and self._full_seen
                           and not self.needs_write_barrier)
        if self.connector.revoked:
            self.phase, self.status = "revoked", self.connector.status
        elif self._platform_offline or self._native_upload_error:
            self.phase, self.status = "retry", "OFFLINE"
        elif self._uploading:
            self.phase, self.status = "upload", "SENDING"
        elif self._authenticating:
            self.phase, self.status = "token", "CONNECTING"
        else:
            self.phase = "stream"
            self.status = "SYNCED" if self.synced else ("SYNCING" if self.connected else "CONNECTING")
        self.notice = self.connector.notice

    def _close_http(self, identity):
        task = self.remote.pop(identity, None)
        self.headers_sent.discard(identity)
        self.request_urls.pop(identity, None)
        self._await_seed.discard(identity)
        if self._active_http == identity:
            self._active_http = None
        if task is not None:
            try:
                task.close()
            except OSError:
                # An already broken socket must not block SDK reconnection.
                pass

    def _is_stream(self, identity):
        return self.request_urls.get(identity, "").split("?", 1)[0].endswith("/sync/stream")

    def _release_sdk_http(self):
        for identity in tuple(self.remote):
            self._reply({"op": "error", "id": identity})
            self._close_http(identity)

    def _service_http(self):
        # One transport operation/chunk per UI update. Rust requests reads only
        # when its bounded single-fragment mailbox has capacity.
        if self._single_tls and getattr(self.connector, "remote", None) is not None:
            return
        available = [identity for identity in self.can_feed if identity in self.remote]
        if not available:
            return
        json_requests = [identity for identity in available if not self._is_stream(identity)]
        if json_requests:
            if self._active_http is not None and self._is_stream(self._active_http):
                if self._single_tls:
                    # Release the stream only after allocation pressure has
                    # selected the one-socket fallback. Rust owns reconnects.
                    identity = self._active_http
                    self._reply({"op": "error", "id": identity})
                    self._close_http(identity)
                else:
                    # Service the checkpoint while retaining the live stream.
                    # Closing it here would turn a successful upload into a
                    # download error and incur the SDK's retry/reseed delay.
                    self._active_http = None
            if self._active_http is not None and self._active_http not in json_requests:
                return
            identity = self._active_http if self._active_http is not None else json_requests[0]
        else:
            available = [identity for identity in available if identity not in self._await_seed]
            if not available or (self._active_http is not None and self._active_http not in available):
                return
            identity = self._active_http if self._active_http is not None else available[self._http_cursor % len(available)]
            self._http_cursor += 1
        self._active_http = identity
        task = self.remote.get(identity)
        if task is None:
            return
        try:
            data = task.step()
            if task.status is not None and identity not in self.headers_sent:
                content_type = getattr(task.response, "content_type", None)
                self._reply({"op": "headers", "id": identity, "status": task.status,
                             "content_type": content_type})
                self.headers_sent.add(identity)
            if data is PENDING:
                return
            if data is None:
                self._reply({"op": "end", "id": identity})
                if (task.status == 200 and self.request_urls.get(identity, "").endswith("/sync/checkpoint-request")):
                    for stream in tuple(self._await_seed):
                        if stream < identity:
                            self._await_seed.discard(stream)
                self._close_http(identity)
                return
            if not isinstance(data, bytes) or len(data) > 2048:
                raise ValueError("Native transport fragment exceeds limit")
            encoded = binascii.b2a_base64(data).strip().decode()
            self._reply({"op": "chunk", "id": identity, "data_b64": encoded})
        except (OSError, ValueError, RuntimeError, MemoryError) as exc:
            self.last_error = str(exc)[:96]
            if isinstance(exc, MemoryError):
                self._single_tls = True
            self._reply({"op": "error", "id": identity})
            self._close_http(identity)
            if isinstance(exc, MemoryError):
                # A retained live stream can hold the TLS allocation that this
                # checkpoint needs. Free it now; seed-gated streams have not
                # opened a socket and must remain available after their seed.
                for stream in tuple(self.remote):
                    if self._is_stream(stream) and stream not in self._await_seed:
                        self._reply({"op": "error", "id": stream})
                        self._close_http(stream)
        return

    def _credentials(self):
        if self._uploading or self._resource_retry_ms:
            return
        wants_upload_credentials = self.upload is not None and not self.connector.credentials_valid()
        if not (self._credentials_needed or self._authenticating or wants_upload_credentials):
            return
        if self._single_tls and not self.connector.credentials_valid() and getattr(self.connector, "remote", None) is None:
            self._release_sdk_http()
        try:
            token = self.connector.fetch_credentials()
        except RetryableNetworkError as exc:
            self._network_failed(exc)
            return
        self._authenticating = token is PENDING
        if token is PENDING:
            return
        if token is not None:
            self._platform_offline = False
        if self._credentials_needed:
            self._reply({"op": "credentials", "token": token})
            self._credentials_needed = False
        if token is None and self.connector.revoked:
            self.phase, self.status = "revoked", self.connector.status

    def _upload(self):
        if self._resource_retry_ms or self._authenticating or self.upload is None:
            return
        if not self.connector.credentials_valid():
            self.connector.close()
            self._uploading = False
            return
        self.synced = False
        self._uploading = True
        if self._single_tls and getattr(self.connector, "remote", None) is None:
            self._release_sdk_http()
        try:
            result = self.connector.upload_data(self.upload["move"])
        except RetryableNetworkError as exc:
            self._network_failed(exc)
            return
        self.notice = self.connector.notice
        if result is PENDING:
            return
        self._finish_upload("success" if result else "rejected")
        self._platform_offline = False
        if result is False:
            self.needs_write_barrier = True
            self._barrier_full_start = self.full_generation

    def _finish_upload(self, result):
        upload, self.upload = self.upload, None
        self._uploading = False
        if upload is not None:
            self.connector.close()
            self._upload_replied.add(upload["id"])
            self._reply({"op": "upload", "id": upload["id"], "result": result})

    def _network_failed(self, error):
        self.last_error = str(error)[:96]
        self.connector.close()
        self._authenticating = self._uploading = False
        self._platform_offline = True
        if self._credentials_needed:
            self._reply({"op": "credentials_error"})
            self._credentials_needed = False
        self._finish_upload("retry")

    def step(self, elapsed_ms=0):
        if self.closed or self.phase in ("blocked", "revoked"):
            return
        try:
            self._resource_retry_ms = max(0, self._resource_retry_ms - max(0, elapsed_ms))
            if self.heartbeat:
                self.heartbeat()
            check_budget = getattr(self.store, "_check_sync_budget", None)
            if check_budget is not None:
                check_budget()
            check_reserve = getattr(self.store, "_check_reserve", None)
            if check_reserve is not None:
                check_reserve()
            self._poll(elapsed_ms)
            self._network_operation = True
            self._credentials()
            self._upload()
            self._service_http()
            self._network_operation = False
            self._poll(0)
            if self.connector.revoked:
                self._stop_native()
        except (OSError, ValueError, RuntimeError, MemoryError) as exc:
            self.last_error = str(exc)[:96]
            if isinstance(exc, MemoryError) and self._network_operation:
                # A failed second TLS allocation must not strand a durable move.
                # Release sockets, then retry the app RPC with one TLS connection.
                self.connector.close()
                self._authenticating = self._uploading = False
                self._single_tls = True
                self._resource_retry_ms = 250
                self._network_failed(exc)
                self._release_sdk_http()
                self.synced = self.connected = False
                self.phase, self.status = "stream", "CONNECTING"
                self._network_operation = False
                return
            self._network_operation = False
            print("NATIVE_SDK_ERROR", type(exc).__name__, self.last_error)
            self.synced = self.connected = False
            self.phase = "blocked"
            self.status = "LOW MEMORY" if isinstance(exc, MemoryError) else "SYNC ERROR"
            self._stop_native()

    def _stop_native(self):
        self.connector.close()
        for identity in tuple(self.remote):
            self._close_http(identity)
        # Cancellation runs in the foreground, never through the Home IRQ.
        for _ in range(64):
            if self.heartbeat:
                self.heartbeat()
            if self.native.sdk_stop():
                return
            self.native.sdk_poll(0, 6)
        raise RuntimeError("Native SDK cancellation did not finish")

    def close(self):
        if self.closed:
            return
        self._stop_native()
        self.closed = True
        self.connected = self.synced = False

