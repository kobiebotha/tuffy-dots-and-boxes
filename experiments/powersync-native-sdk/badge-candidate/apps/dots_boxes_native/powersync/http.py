"""Stepped verified HTTPS with bounded HTTP/1.1 framing and no redirects."""
import json

PENDING = object()


class RetryableNetworkError(OSError):
    """A platform HTTP failure that leaves durable application state intact."""


HEADER_LIMIT = 4096
BODY_LIMIT = 16384
READ_BYTES = 512
# Only downloads use larger reads. Keep request writes and JSON RPC responses
# on their qualified 512-byte path, including the second upload connection.
STREAM_READ_BYTES = 2048


def split_https(url):
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError("Verified HTTPS is required")
    rest = url[8:]
    host, sep, path = rest.partition("/")
    if (not host or len(host) > 253 or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in host)
            or host.startswith(".") or ".." in host or "#" in path or "\r" in path or "\n" in path):
        raise ValueError("Invalid HTTPS target")
    return host, "/" + path if sep else "/"


class Response:
    """Consume arbitrary network fragments without buffering an entire stream."""
    def __init__(self):
        self.status = None
        self.content_type = None
        self.buffer = b""
        self.mode = None
        self.remaining = None
        self.chunk_remaining = None
        self.need_crlf = False
        self.done = False

    def feed(self, data):
        if self.done:
            if data:
                raise ValueError("Data after HTTP response")
            return b""
        self.buffer += data
        if self.status is None:
            position = self.buffer.find(b"\r\n\r\n")
            if position < 0:
                if len(self.buffer) > HEADER_LIMIT:
                    raise ValueError("HTTP headers exceed limit")
                return b""
            if position > HEADER_LIMIT:
                raise ValueError("HTTP headers exceed limit")
            lines = self.buffer[:position].split(b"\r\n")
            self.buffer = self.buffer[position + 4:]
            status = lines.pop(0).split(b" ")
            if len(status) < 2 or status[0] not in (b"HTTP/1.0", b"HTTP/1.1"):
                raise ValueError("Invalid HTTP status")
            self.status = int(status[1])
            headers = {}
            for line in lines:
                if b":" not in line:
                    raise ValueError("Invalid HTTP header")
                key, value = line.split(b":", 1)
                key, value = key.lower().strip(), value.strip().lower()
                if key in headers and key in (b"content-length", b"transfer-encoding"):
                    raise ValueError("Ambiguous HTTP framing")
                headers[key] = value
            self.content_type = headers.get(b"content-type", b"application/json").decode()
            if headers.get(b"content-encoding", b"identity") != b"identity":
                raise ValueError("Compressed HTTP response unsupported")
            if b"transfer-encoding" in headers:
                if headers[b"transfer-encoding"] != b"chunked" or b"content-length" in headers:
                    raise ValueError("Unsupported HTTP framing")
                self.mode = "chunked"
            elif b"content-length" in headers:
                raw = headers[b"content-length"]
                if not raw or any(c < 48 or c > 57 for c in raw):
                    raise ValueError("Invalid HTTP length")
                self.remaining = int(raw)
                self.mode = "length"
            else:
                self.mode = "close"
        if self.mode == "close":
            output, self.buffer = self.buffer, b""
            return output
        if self.mode == "length":
            if len(self.buffer) > self.remaining:
                raise ValueError("HTTP content exceeds length")
            output, self.buffer = self.buffer, b""
            self.remaining -= len(output)
            self.done = self.remaining == 0
            return output
        output = []
        # Advance over the current fragment, retaining only an incomplete
        # framing suffix. Repeated tail slicing otherwise copies the same body
        # many times when several HTTP chunks arrive in one TLS read.
        buffer = self.buffer
        offset = 0
        while offset < len(buffer):
            if self.need_crlf:
                if len(buffer) - offset < 2:
                    break
                if buffer[offset:offset + 2] != b"\r\n":
                    raise ValueError("Invalid HTTP chunk boundary")
                offset += 2
                self.need_crlf = False
            if self.chunk_remaining is None:
                position = buffer.find(b"\r\n", offset)
                if position < 0:
                    if len(buffer) - offset > 32:
                        raise ValueError("HTTP chunk header exceeds limit")
                    break
                size = buffer[offset:position]
                if not size or len(size) > 8 or any(c not in b"0123456789abcdefABCDEF" for c in size):
                    raise ValueError("Unsupported HTTP chunk size")
                self.chunk_remaining = int(size, 16)
                offset = position + 2
                if self.chunk_remaining == 0:
                    self.mode = "trailers"
            if self.mode == "trailers":
                if len(buffer) - offset < 2:
                    break
                if buffer[offset:] != b"\r\n":
                    raise ValueError("HTTP trailers unsupported")
                offset = len(buffer)
                self.done = True
                break
            used = min(len(buffer) - offset, self.chunk_remaining)
            if used:
                output.append(buffer[offset:offset + used])
            offset += used
            self.chunk_remaining -= used
            if self.chunk_remaining == 0:
                self.chunk_remaining = None
                self.need_crlf = True
        self.buffer = buffer[offset:]
        return b"".join(output)

    def finish(self):
        if self.mode == "close" and self.status is not None:
            self.done = True
        if not self.done:
            raise OSError("Truncated HTTP response")


class HTTPS:
    """One bounded DNS/TCP/TLS/read/write operation per foreground update."""
    def __init__(self, url, body, ca_files, token=None, streaming=False, heartbeat=None, headers=None):
        import time
        self.time = time
        self.host, self.path = split_https(url)
        self.ca_files, self.heartbeat = ca_files, heartbeat
        content = json.dumps(body).encode()
        if len(content) > BODY_LIMIT:
            raise ValueError("HTTP request exceeds limit")
        header = ("POST " + self.path + " HTTP/1.1\r\nHost: " + self.host +
                  "\r\nConnection: close\r\nContent-Type: application/json\r\n" +
                  "Accept: " + ("application/x-ndjson" if streaming else "application/json") +
                  "\r\nAccept-Encoding: identity\r\nContent-Length: " + str(len(content)) + "\r\n")
        if token is not None:
            if not isinstance(token, str) or len(token) > 8192 or any(c in token for c in "\r\n "):
                raise ValueError("Invalid bearer token")
            header += "Authorization: Bearer " + token + "\r\n"
        for key, value in (headers or {}).items():
            if (not isinstance(key, str) or not isinstance(value, str) or not key
                    or any(c in key + value for c in "\r\n") or key.lower() in ("host", "content-length", "authorization")):
                raise ValueError("Invalid HTTP header")
            header += key + ": " + value + "\r\n"
        self.request = (header + "\r\n").encode() + content
        self.sent = 0
        self.query = self.raw = self.secure = self.poller = None
        self.response = Response()
        self.phase = "dns"
        self.started = self.last_data = time.ticks_ms()
        self.streaming = streaming
        self.connected_reported = False

    @property
    def status(self):
        return self.response.status

    def _close_socket(self):
        if self.query is not None:
            self.query.close()
            self.query = None
        if self.poller is not None and self.raw is not None:
            try:
                self.poller.unregister(self.raw)
            except (OSError, KeyError):
                pass
            self.poller = None
        if self.secure is not None:
            self.secure.close()
        elif self.raw is not None:
            self.raw.close()
        self.raw = self.secure = None

    def close(self):
        self._close_socket()
        self.phase = "closed"
        self.request = b""

    def step(self):
        if self.heartbeat:
            self.heartbeat()
        if self.phase == "closed":
            return None
        now = self.time.ticks_ms()
        if ((self.status is None and self.time.ticks_diff(now, self.started) > 15000)
                or self.time.ticks_diff(now, self.last_data) > (60000 if self.streaming else 15000)):
            self.close()
            raise OSError("HTTPS deadline")
        if self.phase == "dns":
            import bounded_dns
            import network
            wlan = network.WLAN(network.STA_IF)
            if not wlan.isconnected():
                raise OSError("Wi-Fi unavailable")
            if self.query is None:
                self.query = bounded_dns.SteppedQuery(self.host, bounded_dns.resolver_address(wlan), timeout_ms=3000, attempts=2)
            address = self.query.step()
            if address is bounded_dns.PENDING:
                return PENDING
            self.query.close()
            self.query = None
            import socket
            import select
            self.raw = socket.socket()
            self.raw.setblocking(False)
            try:
                self.raw.connect((address, 443))
            except OSError as exc:
                if exc.args[0] not in (115, 119):
                    raise
            self.poller = select.poll()
            self.poller.register(self.raw, select.POLLOUT)
            self.phase = "tcp"
            return PENDING
        if self.phase == "tcp":
            if not self.poller.poll(0):
                return PENDING
            self.poller.unregister(self.raw)
            self.poller = None
            import tls
            context = tls.SSLContext(tls.PROTOCOL_TLS_CLIENT)
            context.verify_mode = tls.CERT_REQUIRED
            ciphers = [name for name in context.get_ciphers() if "GCM" in name or "CHACHA20" in name]
            if not ciphers or not self.ca_files:
                raise ValueError("Verified TLS trust unavailable")
            context.set_ciphers(ciphers)
            for path in self.ca_files:
                with open(path, "rb") as source:
                    certificate = source.read(4097)
                if len(certificate) > 4096:
                    raise ValueError("TLS root exceeds limit")
                context.load_verify_locations(certificate)
            self.secure = context.wrap_socket(self.raw, server_hostname=self.host, do_handshake_on_connect=False)
            self.phase = "send"
            return PENDING
        if self.phase == "send":
            try:
                count = self.secure.write(memoryview(self.request)[self.sent:self.sent + READ_BYTES])
            except OSError as exc:
                if exc.args[0] in (11, 35):
                    return PENDING
                raise
            if count:
                self.sent += count
            if self.sent == len(self.request):
                self.request = b""
                self.phase = "read"
            return PENDING
        if self.response.done:
            self.close()
            return None
        try:
            chunk = self.secure.read(STREAM_READ_BYTES if self.streaming else READ_BYTES)
        except OSError as exc:
            if exc.args[0] in (11, 35):
                return PENDING
            raise
        if chunk is None:
            return PENDING
        if chunk == b"":
            self.response.finish()
            self.close()
            return None
        self.last_data = now
        result = self.response.feed(chunk)
        return result if result else PENDING


class Gateway:
    def __init__(self, url, ca_files, heartbeat=None, factory=HTTPS, token=None, headers=None):
        self.url, self.ca_files, self.heartbeat, self.factory = url, ca_files, heartbeat, factory
        self.token = token
        self.headers = headers
        self.task = None
        self.buffer = b""

    def start(self, body):
        if self.task is not None:
            raise ValueError("Gateway already has a request")
        self.buffer = b""
        self.task = self.factory(self.url, body, self.ca_files, heartbeat=self.heartbeat, token=self.token, headers=self.headers)

    def step(self):
        if self.task is None:
            raise ValueError("Gateway request missing")
        data = self.task.step()
        status = self.task.status
        if status is not None and (status == 429 or status >= 500):
            # Proxies may send oversized HTML or no JSON for transient errors.
            # Headers suffice: close before buffering any untrusted error body.
            self.close()
            return {"http_status": status, "error": "request_unavailable"}
        if data is PENDING:
            return PENDING
        if data is not None:
            if len(self.buffer) + len(data) > BODY_LIMIT:
                self.close()
                raise ValueError("Gateway response exceeds limit")
            self.buffer += data
            return PENDING
        status = self.task.status
        self.task = None
        try:
            value = json.loads(self.buffer.decode())
        finally:
            self.buffer = b""
        if not isinstance(value, dict):
            raise ValueError("Invalid gateway response")
        if not 200 <= status < 300:
            if "error" not in value:
                value = {"error": value.get("message") or "request_failed"}
            value = dict(value)
            value["http_status"] = status
        return value

    def close(self):
        if self.task is not None:
            self.task.close()
        self.task = None
        self.buffer = b""
