"""Compact wire identities and edges for the server's fixed 5 by 4 board."""
import binascii

from dots_session import is_uuid

COLS = 5
ROWS = 4
EDGES = 49
_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_STATUSES = ("waiting", "playing", "finished", "cancelled")


def encode_uuid(value):
    if not is_uuid(value):
        raise ValueError("Invalid UUID")
    raw = binascii.unhexlify(value.replace("-", ""))
    return binascii.b2a_base64(raw).decode().strip()


def decode_uuid(value):
    if (not isinstance(value, str) or len(value) != 24 or value[-2:] != "=="
            or any(char not in _B64 for char in value[:-2])):
        raise ValueError("Invalid compact UUID")
    raw = binascii.a2b_base64(value.encode())
    if len(raw) != 16 or binascii.b2a_base64(raw).decode().strip() != value:
        raise ValueError("Invalid compact UUID")
    text = binascii.hexlify(raw).decode()
    return text[:8] + "-" + text[8:12] + "-" + text[12:16] + "-" + text[16:20] + "-" + text[20:]


def encode_edge(orientation, row, col):
    if type(row) is not int or type(col) is not int:
        raise ValueError("Invalid board edge")
    if orientation == "h" and 0 <= row <= ROWS and 0 <= col < COLS:
        return row * COLS + col
    if orientation == "v" and 0 <= row < ROWS and 0 <= col <= COLS:
        return 25 + row * (COLS + 1) + col
    raise ValueError("Invalid board edge")


def decode_edge(value):
    if type(value) is not int or not 0 <= value < EDGES:
        raise ValueError("Invalid compact board edge")
    if value < 25:
        return ("h", value // COLS, value % COLS)
    return ("v", (value - 25) // (COLS + 1), (value - 25) % (COLS + 1))


def decode_status(value):
    if type(value) is not int or not 0 <= value < len(_STATUSES):
        raise ValueError("Invalid compact room status")
    return _STATUSES[value]
