"""Host helpers for one explicitly driven badge latency session.

Import these from a host runner. Nothing runs on import: no serial dependency,
credential refresh, backend mutation, logging, or deployment. command(message)
should send one JSON command to the RAM launcher and return its matching reply.
Use a single host monotonic clock and a single device session for one capture.
"""
import json
import math
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import UUID


def clock_us():
    return time.monotonic_ns() // 1000


def _identity(value, label):
    if type(value) is not int or value < 0:
        raise ValueError(label + " must be a nonnegative integer")
    return value


def _canonical_uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value.lower():
        raise ValueError("Expected a canonical UUID")
    return value.lower()


def clock_probe(command, request_id, clock=clock_us):
    """Bracket one device clock read; the reply transport adds uncertainty."""
    _identity(request_id, "request_id")
    sent = clock()
    reply = command({"command": "clock", "request_id": request_id})
    received = clock()
    if (reply.get("command") != "clock" or reply.get("request_id") != request_id
            or type(reply.get("device_us")) is not int or reply["device_us"] < 0):
        raise ValueError("Mismatched device clock reply")
    if received < sent:
        raise ValueError("Host clock moved backwards")
    return {"host_send_us": sent, "device_us": reply["device_us"],
            "host_receive_us": received}


def arm_download(command, sample_id, move_id, edge, move_index, request_id):
    """Arm exact correlation before sending an opponent's move through the RPC."""
    _identity(sample_id, "sample_id")
    _identity(request_id, "request_id")
    _identity(move_index, "move_index")
    move_id = _canonical_uuid(move_id)
    if (not isinstance(edge, (list, tuple)) or len(edge) != 3 or edge[0] not in ("h", "v")
            or any(type(value) is not int or value < 0 for value in edge[1:])):
        raise ValueError("Invalid move edge")
    reply = command({"command": "arm_download", "request_id": request_id,
                     "sample_id": sample_id, "move_id": move_id,
                     "edge": list(edge), "move_index": move_index})
    if (reply.get("command") != "arm_download" or reply.get("request_id") != request_id
            or reply.get("accepted") is not True):
        raise ValueError("Device did not arm the download sample")
    return reply


def _http_json(request, timeout):
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(65537)
            if len(body) > 65536:
                raise ValueError("RPC response exceeds bound")
            return response.status, json.loads(body)
    except HTTPError as error:
        # Error text may contain private data. Never copy response bodies to logs.
        status = error.code
        error.close()
        return status, {}


def submit_move(session, move, sample_id, clock=clock_us, transport=_http_json, timeout=30):
    """Perform exactly one explicitly requested RPC; never retry an unknown outcome.

    session: {url,key,access_token,room_id}. move: {id,orientation,row,col}.
    The caller must arm the device first and use the opponent's authorized session.
    Returned records contain timing/acceptance only, never URLs, IDs or tokens.
    A successful response brackets an already-committed transaction. A duplicate
    does not identify its original commit window and is excluded by the reporter.
    """
    _identity(sample_id, "sample_id")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 120:
        raise ValueError("timeout must be between 0 and 120 seconds")
    endpoint = session.get("url", "").rstrip("/")
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query
            or parsed.fragment or parsed.username or parsed.password):
        raise ValueError("Supabase URL must be an HTTPS origin")
    for name in ("key", "access_token"):
        if not isinstance(session.get(name), str) or not session[name]:
            raise ValueError("Missing private session credentials")
    orientation, row, col = move.get("orientation"), move.get("row"), move.get("col")
    if (orientation not in ("h", "v") or type(row) is not int or type(col) is not int
            or not 0 <= row <= (4 if orientation == "h" else 3)
            or not 0 <= col <= (4 if orientation == "h" else 5)):
        raise ValueError("Move outside the 5×4 board")
    body = {"move_id": _canonical_uuid(move["id"]), "target": _canonical_uuid(session["room_id"]),
            "orientation": orientation, "row_i": row, "col_i": col}
    request = Request(endpoint + "/rest/v1/rpc/submit_move",
                      data=json.dumps(body).encode(), method="POST",
                      headers={"apikey": session["key"],
                               "Authorization": "Bearer " + session["access_token"],
                               "Content-Type": "application/json"})
    sent = clock()
    status, reply = transport(request, timeout)
    received = clock()
    if received < sent:
        raise ValueError("Host clock moved backwards")
    accepted = (200 <= status < 300 and isinstance(reply, dict) and reply.get("ok") is True)
    duplicate = reply.get("duplicate", False) is True if isinstance(reply, dict) else False
    record = {"sample_id": sample_id, "host_send_us": sent, "host_receive_us": received,
              "accepted": accepted, "duplicate": duplicate, "http_status": status}
    if accepted and type(reply.get("move_index")) is int:
        record["move_index"] = reply["move_index"]
    return record


def export_device(command, request_id):
    """Export only after timed work has completed, so serial I/O stays outside it."""
    _identity(request_id, "request_id")
    reply = command({"command": "export", "request_id": request_id})
    if reply.get("command") != "export" or reply.get("request_id") != request_id:
        raise ValueError("Mismatched device export reply")
    device = reply.get("device")
    if not isinstance(device, dict) or device.get("format_version") != 1:
        raise ValueError("Unsupported device export")
    return device
