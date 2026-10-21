"""Bounded NDJSON framing for the pinned PowerSync core's data-line contract.

Service data lines may contain a large oplog history for a tiny projection.
Keep one operation at a time and preserve its raw JSON and order. Core 0.5.4
consumes only bucket/data from data lines; checkpoint records remain unchanged.
The service encodes bucket before the data array. Reject another ordering rather
than buffer an unbounded array while waiting for its bucket.
"""
import json

MAX_LINE = 8192
MAX_FRAGMENT = 512
MAX_METADATA = 1024
MAX_DEPTH = 16
_SPACE = b" \t\r"


class StreamLines:
    def __init__(self):
        self.pending = b""
        self.offset = 0
        self._reset_line()

    def _reset_line(self):
        self.mode = "prefix"
        self.prefix = bytearray()
        self.token = bytearray()
        self.stack = []
        self.quoted = False
        self.escaped = False
        self.keys = set()
        self.key = None
        self.bucket = None
        self.data_prefix = None
        self.data_seen = False
        self.can_end = True
        self.metadata_bytes = 0
        self.started = False

    def feed(self, fragment):
        if self.offset != len(self.pending):
            raise ValueError("Drain PowerSync input before reading again")
        if not isinstance(fragment, bytes) or len(fragment) > MAX_FRAGMENT:
            raise ValueError("PowerSync fragment exceeds limit")
        self.pending = fragment
        self.offset = 0

    def finish(self):
        if self.offset != len(self.pending) or self.started:
            raise OSError("Truncated PowerSync line")

    def _lex(self, value):
        if self.quoted:
            if value < 32:
                raise ValueError("Control character in PowerSync string")
            if self.escaped:
                self.escaped = False
            elif value == 92:
                self.escaped = True
            elif value == 34:
                self.quoted = False
        elif value == 34:
            self.quoted = True
        elif value in (123, 91):
            if len(self.stack) >= MAX_DEPTH:
                raise ValueError("PowerSync nesting exceeds limit")
            self.stack.append(125 if value == 123 else 93)
        elif value in (125, 93):
            if not self.stack or self.stack.pop() != value:
                raise ValueError("Invalid PowerSync JSON boundary")

    def _bounded(self, target, value, limit):
        if len(target) >= limit:
            raise ValueError("PowerSync value exceeds limit")
        target.append(value)

    def _metadata(self, delimiter):
        value = json.loads(self.token.decode())
        self.token = bytearray()
        if self.key == "bucket":
            if not isinstance(value, str) or not value:
                raise ValueError("Invalid PowerSync bucket")
            self.bucket = value
            self.data_prefix = (b'{"data":{"bucket":' + json.dumps(value).encode() + b',"data":[')
        if delimiter == 44:
            self.mode, self.can_end = "member", False
        else:
            if not self.data_seen:
                raise ValueError("PowerSync data array missing")
            self.mode = "root_end"

    def _operation(self, delimiter):
        raw = bytes(self.token).strip()
        self.token = bytearray()
        if not isinstance(json.loads(raw.decode()), dict):
            raise ValueError("Invalid PowerSync operation")
        line = self.data_prefix + raw + b']}}'
        if len(line) > MAX_LINE:
            raise ValueError("PowerSync operation exceeds limit")
        if delimiter == 44:
            self.mode, self.can_end = "operation_start", False
        else:
            self.mode = "after_array"
        return line.decode()

    def _newline(self):
        if self.stack or self.quoted:
            raise ValueError("Truncated PowerSync line")
        if self.mode in ("prefix", "control"):
            raw = bytes(self.prefix).rstrip(b"\r")
            if not raw.strip():
                self._reset_line()
                return None
            if not isinstance(json.loads(raw.decode()), dict):
                raise ValueError("Invalid PowerSync line")
            result = raw.decode()
        elif self.mode == "line_end":
            result = None
        else:
            raise ValueError("Invalid PowerSync data envelope")
        self._reset_line()
        return result

    def pop(self):
        """Return at most one core record, retaining at most one socket fragment."""
        while self.offset < len(self.pending):
            value = self.pending[self.offset]
            self.offset += 1
            if value == 10:
                result = self._newline()
                if result is not None:
                    return result
                continue
            depth, quoted = len(self.stack), self.quoted
            self._lex(value)
            if value not in _SPACE:
                self.started = True

            if self.mode in ("prefix", "control"):
                self._bounded(self.prefix, value, MAX_LINE)
                if self.mode == "prefix" and value == 58 and depth == 1 and not quoted:
                    first = json.loads((bytes(self.prefix) + b'null}').decode())
                    if list(first) == ["data"]:
                        self.prefix = bytearray()
                        self.mode = "data_object"
                    else:
                        self.mode = "control"
                continue

            if self.mode not in ("operation_start", "operation"):
                self.metadata_bytes += 1
                if self.metadata_bytes > MAX_METADATA:
                    raise ValueError("PowerSync envelope exceeds limit")

            if self.mode == "data_object":
                if value in _SPACE:
                    continue
                if value != 123:
                    raise ValueError("Invalid PowerSync data object")
                self.mode, self.can_end = "member", True
            elif self.mode == "member":
                if value in _SPACE:
                    continue
                if value == 125 and self.can_end and self.data_seen:
                    self.mode = "root_end"
                elif value == 34:
                    self.token = bytearray(b'"')
                    self.mode = "key"
                else:
                    raise ValueError("Invalid PowerSync data member")
            elif self.mode == "key":
                self._bounded(self.token, value, MAX_METADATA)
                if value == 34 and not self.quoted:
                    self.key = json.loads(self.token.decode())
                    self.token = bytearray()
                    if self.key in self.keys:
                        raise ValueError("Duplicate PowerSync data member")
                    self.keys.add(self.key)
                    self.mode = "colon"
            elif self.mode == "colon":
                if value in _SPACE:
                    continue
                if value != 58:
                    raise ValueError("Invalid PowerSync member separator")
                self.mode = "value_start"
            elif self.mode == "value_start":
                if value in _SPACE:
                    continue
                if self.key == "data":
                    if value != 91 or self.bucket is None:
                        raise ValueError("PowerSync bucket must precede data array")
                    self.data_seen = True
                    self.mode, self.can_end = "operation_start", True
                else:
                    if value in (44, 125):
                        raise ValueError("PowerSync metadata value missing")
                    self.token = bytearray([value])
                    self.mode = "metadata"
            elif self.mode == "metadata":
                if depth == 2 and not quoted and value in (44, 125):
                    self._metadata(value)
                else:
                    self._bounded(self.token, value, MAX_METADATA)
            elif self.mode == "operation_start":
                if value in _SPACE:
                    continue
                if value == 93 and self.can_end:
                    self.mode = "after_array"
                elif value == 123:
                    self.token = bytearray(b'{')
                    self.mode = "operation"
                else:
                    raise ValueError("Invalid PowerSync operation boundary")
            elif self.mode == "operation":
                if depth == 3 and not quoted and value in (44, 93):
                    return self._operation(value)
                self._bounded(self.token, value, MAX_LINE - len(self.data_prefix) - 3)
            elif self.mode == "after_array":
                if value in _SPACE:
                    continue
                if value == 44:
                    self.mode, self.can_end = "member", False
                elif value == 125:
                    self.mode = "root_end"
                else:
                    raise ValueError("Invalid PowerSync array suffix")
            elif self.mode == "root_end":
                if value in _SPACE:
                    continue
                if value != 125:
                    raise ValueError("Invalid PowerSync root suffix")
                self.mode = "line_end"
            elif self.mode == "line_end" and value not in _SPACE:
                raise ValueError("Data after PowerSync record")
        self.pending = b""
        self.offset = 0
        return None
