"""Credentials stored next to SQLite, with read-only legacy move import.

New mutations live only in the SDK database. Legacy pending files are read for
the one-time database migration and are deleted after that transaction commits.
"""
import json
import os

MAX_BYTES = 16384
FILES = ("session.json", "session.json.next", "pending.json", "pending.json.next")


def is_uuid(value):
    if not isinstance(value, str) or len(value) != 36:
        return False
    parts = value.split("-")
    if [len(part) for part in parts] != [8, 4, 4, 4, 12]:
        return False
    hexits = value.replace("-", "").lower()
    return len(hexits) == 32 and all(char in "0123456789abcdef" for char in hexits)


def _token(value):
    return value is None or (isinstance(value, str) and 0 < len(value) <= 8192 and "\n" not in value and "\r" not in value)


def validate_session(value):
    allowed = {"version", "device_id", "user_id", "room_id", "seat",
               "refresh_token", "access_token", "expires_at", "pending_move"}
    if not isinstance(value, dict) or set(value) != allowed or value.get("version") != 1:
        raise ValueError("Unsupported session; retained")
    if not is_uuid(value["device_id"]):
        raise ValueError("Invalid device id; retained")
    for key in ("user_id", "room_id"):
        if value[key] is not None and not is_uuid(value[key]):
            raise ValueError("Invalid session identity; retained")
    seat = value["seat"]
    if seat is not None and seat not in (1, 2):
        raise ValueError("Invalid seat; retained")
    if not _token(value["refresh_token"]) or not _token(value["access_token"]):
        raise ValueError("Invalid saved token; retained")
    expires = value["expires_at"]
    if expires is not None and (type(expires) is not int or expires < 0):
        raise ValueError("Invalid token expiry; retained")
    paired = value["refresh_token"] is not None
    if paired and (value["user_id"] is None or value["room_id"] is None or seat is None):
        raise ValueError("Incomplete paired session; retained")
    if not paired and (value["access_token"] is not None or value["pending_move"] is not None):
        raise ValueError("Unpaired session has play state; retained")
    move = value["pending_move"]
    if move is not None:
        if (not isinstance(move, dict) or set(move) != {"id", "orientation", "row", "col"}
                or not is_uuid(move["id"]) or move["orientation"] not in ("h", "v")
                or type(move["row"]) is not int or type(move["col"]) is not int
                or not 0 <= move["row"] <= 5 or not 0 <= move["col"] <= 5):
            raise ValueError("Invalid pending move; retained")
    return value


def fresh_session(device_id):
    return validate_session({
        "version": 1,
        "device_id": device_id,
        "user_id": None,
        "room_id": None,
        "seat": None,
        "refresh_token": None,
        "access_token": None,
        "expires_at": None,
        "pending_move": None,
    })


def _stat(path):
    try:
        return os.stat(path)
    except OSError as exc:
        if exc.args[0] != 2:
            raise
        return None


class SessionFile:
    def __init__(self, root):
        if not root.startswith("/") or root.endswith("/") or "/../" in root:
            raise ValueError("Session needs an absolute directory")
        self.root = root
        self.path = root + "/session.json"
        self.pending_path = root + "/pending.json"
        self.base = None
        self.generation = 0
        self._embedded_pending = False

    def _read(self, path, info):
        if not 0 < info[6] <= MAX_BYTES:
            raise ValueError("Invalid session size; retained")
        with open(path) as source:
            return json.loads(source.read(MAX_BYTES + 1))

    def _remember(self, value):
        self.base = dict(value, pending_move=None)

    def load(self, new_uuid):
        info = _stat(self.path)
        if info is None:
            if _stat(self.pending_path) is not None:
                raise ValueError("Pending move has no session; retained")
            created = fresh_session(new_uuid())
            self.save(created)
            return created
        saved = self._read(self.path, info)
        if isinstance(saved, dict) and "format" in saved:
            if (set(saved) != {"format", "generation", "session"} or saved["format"] != 2
                    or type(saved["generation"]) is not int or not 0 < saved["generation"] < 9223372036854775807):
                raise ValueError("Unsupported session envelope; retained")
            self.generation = saved["generation"]
            value = validate_session(saved["session"])
        else:
            self.generation = 0
            value = validate_session(saved)
        self._embedded_pending = value["pending_move"] is not None
        pending_info = _stat(self.pending_path)
        if pending_info is not None:
            overlay = self._read(self.pending_path, pending_info)
            if (not isinstance(overlay, dict) or set(overlay) != {"version", "generation", "pending_move"}
                    or overlay["version"] != 1 or type(overlay["generation"]) is not int
                    or not 0 <= overlay["generation"] < 9223372036854775807):
                raise ValueError("Unsupported pending move envelope; retained")
            if overlay["generation"] > self.generation:
                raise ValueError("Pending move has a newer session; retained")
            if overlay["generation"] == self.generation:
                value = validate_session(dict(value, pending_move=overlay["pending_move"]))
        self._remember(value)
        return value

    def save(self, value):
        validate_session(value)
        if self.base is None and _stat(self.path) is not None:
            self.load(None)
        # Keep the credential record's compatible shape, but never persist a
        # mutation or recreate the old pending.json overlay.
        base = dict(value, pending_move=None)
        if self.base == base and not self._embedded_pending:
            return
        if self.generation >= 9223372036854775806:
            raise ValueError("Session generation exhausted; retained")
        generation = self.generation + 1
        self._replace(self.path, {"format": 2, "generation": generation, "session": base})
        self.generation = generation
        self._embedded_pending = False
        self._remember(base)

    def discard_legacy_pending(self):
        """Call only after the SDK database has durably imported legacy work."""
        if self.base is None:
            raise ValueError("Load credentials before finishing migration")
        if self._embedded_pending:
            self.save(self.base)
        removed = False
        for path in (self.pending_path, self.pending_path + ".next"):
            try:
                os.remove(path)
                removed = True
            except OSError as exc:
                if exc.args[0] != 2:
                    raise
        if removed:
            os.sync()

    def _replace(self, path, value):
        encoded = json.dumps(value, separators=(",", ":"))
        if len(encoded.encode()) > MAX_BYTES:
            raise ValueError("Session is too large")
        pending = path + ".next"
        with open(pending, "w") as target:
            if target.write(encoded) != len(encoded):
                raise OSError("Incomplete session write")
            target.flush()
        os.sync()
        os.rename(pending, path)
        os.sync()
