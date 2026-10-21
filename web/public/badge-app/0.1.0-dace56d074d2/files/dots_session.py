"""Pairing session stored next to the database. Tokens never go in SQLite."""
import binascii
import json
import os

MAX_BYTES = 16384
FILES = ("session.json", "session.json.next")


def is_uuid(value):
    if not isinstance(value, str) or len(value) != 36:
        return False
    parts = value.split("-")
    if [len(part) for part in parts] != [8, 4, 4, 4, 12]:
        return False
    hexits = value.replace("-", "").lower()
    return len(hexits) == 32 and all(char in "0123456789abcdef" for char in hexits)


def uuid4(entropy):
    value = bytearray(entropy(16))
    if len(value) != 16:
        raise ValueError("UUID randomness unavailable")
    value[6] = (value[6] & 15) | 64
    value[8] = (value[8] & 63) | 128
    text = binascii.hexlify(value).decode()
    return text[:8] + "-" + text[8:12] + "-" + text[12:16] + "-" + text[16:20] + "-" + text[20:]


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

    def load(self, entropy):
        info = _stat(self.path)
        if info is None:
            created = fresh_session(uuid4(entropy))
            self.save(created)
            return created
        if not 0 < info[6] <= MAX_BYTES:
            raise ValueError("Invalid session size; retained")
        with open(self.path) as source:
            return validate_session(json.loads(source.read(MAX_BYTES + 1)))

    def save(self, value):
        validate_session(value)
        encoded = json.dumps(value)
        if len(encoded.encode()) > MAX_BYTES:
            raise ValueError("Session is too large")
        pending = self.path + ".next"
        with open(pending, "w") as target:
            if target.write(encoded) != len(encoded):
                raise OSError("Incomplete session write")
            target.flush()
        os.sync()
        os.rename(pending, self.path)
        os.sync()
