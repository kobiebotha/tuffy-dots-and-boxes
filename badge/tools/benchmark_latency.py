"""Capture the prepared RAM-only device probe after reconnecting a badge.

Requires an explicitly selected serial port and an app cleanly closed to REPL.
Entering raw REPL interrupts running Python, so do not point this at a live game.
Without --downloads this captures button-driven local moves and makes no host RPC.
An explicit --downloads plan plus a private --session enables one RPC per sample.
"""
import argparse
import json
import math
import os
from pathlib import Path
import stat
import time
from uuid import UUID, uuid4

from latency_host import arm_download, clock_probe, clock_us, export_device, submit_move

PREFIX = b"DOTS_LATENCY "


class DeviceProtocolError(RuntimeError):
    pass


class DeviceBusy(DeviceProtocolError):
    pass


class RawDevice:
    """Small JSONL adapter; mpremote owns raw-REPL entry/source transfer only."""
    def __init__(self, transport, clock=time.monotonic):
        self.transport = transport
        self.serial = transport.serial
        self.clock = clock
        self.buffer = bytearray()
        self.finished = None
        self.notifications = []

    def read_message(self, timeout=15):
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self.buffer[:newline])
                del self.buffer[:newline + 1]
                start = line.find(PREFIX)
                if start < 0:
                    continue
                try:
                    reply = json.loads(line[start + len(PREFIX):])
                except (ValueError, UnicodeError):
                    raise DeviceProtocolError("Malformed device reply") from None
                if not isinstance(reply, dict):
                    raise DeviceProtocolError("Invalid device reply")
                if reply.get("command") == "finished":
                    self.finished = reply
                return reply
            self.serial.timeout = min(.2, max(.001, deadline - self.clock()))
            incoming = self.serial.read(max(1, min(4096, getattr(self.serial, "in_waiting", 0))))
            if incoming:
                self.buffer.extend(incoming)
                if len(self.buffer) > 262144:
                    raise DeviceProtocolError("Device reply exceeds bound")
        raise TimeoutError("Device reply deadline exceeded")

    def start(self, source):
        self.transport.enter_raw_repl(soft_reset=False)
        self.transport.exec_raw_no_follow(source.encode("utf-8") if isinstance(source, str) else source)
        reply = self.read_message(timeout=30)
        if (reply.get("command") != "ready" or reply.get("format_version") != 1
                or reply.get("accepted") is not True):
            raise DeviceProtocolError("Device did not become ready")
        return reply

    def command(self, message):
        if self.finished is not None:
            raise DeviceProtocolError("Device session already finished")
        self.serial.write(json.dumps(message, separators=(",", ":")).encode("ascii") + b"\n")
        reply = self.read_message()
        while reply.get("command") == "sample_done":
            if len(self.notifications) < 128:
                self.notifications.append(reply)
            reply = self.read_message()
        if (reply.get("command") != message["command"]
                or reply.get("request_id") != message["request_id"]):
            raise DeviceProtocolError("Mismatched command reply")
        if reply.get("error") or reply.get("accepted") is False:
            if reply.get("error") == "Busy":
                raise DeviceBusy("Device has an active measurement")
            raise DeviceProtocolError("Device rejected command")
        return reply

    def wait_finished(self):
        if self.finished is not None:
            return self.finished
        reply = self.read_message(timeout=15)
        if reply.get("command") != "finished":
            raise DeviceProtocolError("Device did not finish")
        return reply

    def close(self):
        # Do not send Ctrl-C on cleanup: a normal stop closes the app first.
        self.transport.close()


def _number(value, label, lower, upper):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not lower <= value <= upper):
        raise ValueError(label + " outside bounds")
    return value


def _uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value.lower():
        raise ValueError("Invalid canonical UUID")
    return value.lower()


def load_session(path):
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError("Session must be a regular private 0600 file")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise ValueError("Session must belong to the current user")
    session = json.loads(path.read_text())
    if not isinstance(session, dict):
        raise ValueError("Invalid session JSON")
    for key in ("url", "key", "access_token", "room_id"):
        if not isinstance(session.get(key), str) or not session[key]:
            raise ValueError("Missing session fields")
    from urllib.parse import urlsplit
    endpoint = urlsplit(session["url"].rstrip("/"))
    if (endpoint.scheme != "https" or not endpoint.netloc or endpoint.path or endpoint.query
            or endpoint.fragment or endpoint.username or endpoint.password):
        raise ValueError("Session URL must be an HTTPS origin")
    _uuid(session["room_id"])
    return session


def validate_plan(plan, capacity):
    if not isinstance(plan, list) or len(plan) > min(49, capacity):
        raise ValueError("Download plan must fit the probe capacity")
    result, identities = [], set()
    for row in plan:
        if not isinstance(row, dict):
            raise ValueError("Invalid download sample")
        identity = row.get("sample_id")
        if type(identity) is not int or identity < 0 or identity in identities:
            raise ValueError("Download sample IDs must be distinct nonnegative integers")
        identities.add(identity)
        edge = row.get("edge")
        if (not isinstance(edge, list) or len(edge) != 3 or edge[0] not in ("h", "v")
                or type(edge[1]) is not int or type(edge[2]) is not int
                or not 0 <= edge[1] <= (4 if edge[0] == "h" else 3)
                or not 0 <= edge[2] <= (4 if edge[0] == "h" else 5)):
            raise ValueError("Download edge must be on the 5×4 board")
        index = row.get("move_index")
        if type(index) is not int or not 1 <= index <= 49:
            raise ValueError("Download move_index must be 1..49")
        wait = _number(row.get("wait_seconds", 5), "wait_seconds", .1, 60)
        result.append({"sample_id": identity, "edge": edge, "move_index": index,
                       "move_id": _uuid(row.get("move_id", str(uuid4()))), "wait_seconds": wait})
    return result


def _sleep_until(deadline_us, clock, sleep):
    while True:
        remaining = (deadline_us - clock()) / 1_000_000
        if remaining <= 0:
            return
        sleep(min(remaining, .2))


def run_capture(device, source, seconds=180, capacity=24, session=None, downloads=None,
                clock_drift_ppm=None, enabled=True, clock=clock_us, sleep=time.sleep,
                submit=submit_move, notify=lambda _message: None):
    """Return a serializable partial capture even on timeout/unknown RPC outcome.

    Device/session/source are injected to make scheduler and failure paths testable
    without hardware or network access. The caller writes the returned artifact.
    """
    _number(seconds, "seconds", 1, 220)
    if type(capacity) is not int or not 1 <= capacity <= 64:
        raise ValueError("capacity must be 1..64")
    if clock_drift_ppm is not None:
        _number(clock_drift_ppm, "clock_drift_ppm", 0, 1_000_000)
    if type(enabled) is not bool:
        raise ValueError("enabled must be boolean")
    plan = validate_plan(downloads or [], capacity)
    if plan and not enabled:
        raise ValueError("Download plans require an enabled probe")
    if plan and session is None:
        raise ValueError("A download plan requires an explicit private session")
    if sum(row["wait_seconds"] for row in plan) + len(plan) * 2 >= seconds:
        raise ValueError("Download plan quiet windows do not fit the capture duration")
    capture = {"format_version": 1,
               "device": {"format_version": 1, "local": [], "downloads": []},
               "host": {"format_version": 1, "clock": "monotonic_us", "sync_probes": [], "download_rpc": []},
               "capture": {"status": "incomplete", "requested_seconds": seconds, "capacity": capacity}}
    if clock_drift_ppm is not None:
        capture["clock_drift_ppm"] = clock_drift_ppm
    request_id = 0
    started = False
    stage = "device_start"
    def next_id():
        nonlocal request_id
        request_id += 1
        return request_id
    def probe_clock():
        capture["host"]["sync_probes"].append(clock_probe(device.command, next_id(), clock))
    try:
        started = True
        ready = device.start(source)
        if ready.get("enabled") is not enabled:
            raise DeviceProtocolError("The measurement probe mode differs from the request")
        deadline = clock() + int(seconds * 1_000_000)
        stage = "initial_clock_probe"
        probe_clock()
        notify("Capture started. Button-driven local moves are being measured.")
        for sample in plan:
            identity = sample["sample_id"]
            # Reserve time for normal stop/partial export rather than starting an
            # RPC whose completion cannot be observed within this capture.
            if clock() + int((sample["wait_seconds"] + 2) * 1_000_000) >= deadline:
                raise TimeoutError("Insufficient capture time for the next download")
            stage = "download_readiness"
            while True:
                status = device.command({"command": "status", "request_id": next_id()})
                if status.get("synced") is True and status.get("phase") == "stream":
                    if status.get("moves") != sample["move_index"] - 1:
                        raise DeviceProtocolError("Download plan does not match the current board")
                    break
                if clock() + int((sample["wait_seconds"] + 2.5) * 1_000_000) >= deadline:
                    raise TimeoutError("Device did not become ready for the download")
                sleep(.25)
            stage = "download_clock_probe"
            probe_clock()
            stage = "arm_download"
            arm_download(device.command, identity, sample["move_id"], sample["edge"], sample["move_index"], next_id())
            stage = "submit_download_rpc"
            move = {"id": sample["move_id"], "orientation": sample["edge"][0],
                    "row": sample["edge"][1], "col": sample["edge"][2]}
            # No retries: a transport failure can occur after the server commits.
            row = submit(session, move, identity, clock=clock)
            capture["host"]["download_rpc"].append(row)
            if row.get("accepted") is not True or row.get("duplicate") is not False:
                raise DeviceProtocolError("Download RPC was rejected or duplicate")
            stage = "download_quiet_window"
            _sleep_until(clock() + int(sample["wait_seconds"] * 1_000_000), clock, sleep)
            stage = "verify_download"
            snapshot = export_device(device.command, next_id())
            capture["device"] = snapshot
            matches = [item for item in snapshot.get("downloads", []) if item.get("sample_id") == identity]
            if (len(matches) != 1 or any(key not in matches[0]
                    for key in ("sqlite_persisted_us", "framebuffer_us", "panel_submitted_us"))):
                raise DeviceProtocolError("Download did not finish before the next sample")
            stage = "download_clock_probe"
            probe_clock()
        stage = "local_capture"
        _sleep_until(deadline, clock, sleep)
        capture["capture"]["status"] = "complete"
    except (Exception, KeyboardInterrupt) as error:
        capture["capture"].update(status="failed", error={"stage": stage, "type": type(error).__name__})
    finally:
        if started:
            try:
                if getattr(device, "finished", None) is None:
                    sent = clock()
                    stopped = device.command({"command": "stop", "request_id": next_id()})
                    received = clock()
                    if type(stopped.get("device_us")) is not int or stopped["device_us"] < 0 or received < sent:
                        raise DeviceProtocolError("Invalid final clock probe")
                    capture["host"]["sync_probes"].append({"host_send_us": sent,
                        "device_us": stopped["device_us"], "host_receive_us": received})
                finished = device.wait_finished()
                exported = finished.get("device")
                if not isinstance(exported, dict) or exported.get("format_version") != 1:
                    raise DeviceProtocolError("Missing final device export")
                capture["device"] = exported
            except (Exception, KeyboardInterrupt) as error:
                capture["capture"]["status"] = "failed"
                capture["capture"]["cleanup_error"] = type(error).__name__
        try:
            device.close()
        except Exception as error:
            capture["capture"]["status"] = "failed"
            capture["capture"]["close_error"] = type(error).__name__
    return capture


def write_capture(path, capture):
    """Create a new private artifact; never overwrite a previous measurement."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(capture, output, indent=2, allow_nan=False)
        output.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="Explicit serial device; badge app must already be cleanly closed")
    parser.add_argument("--output", type=Path, required=True, help="New capture JSON; never overwritten")
    parser.add_argument("--seconds", type=float, default=180, help="Host capture duration after ready (1..220)")
    parser.add_argument("--capacity", type=int, default=24)
    parser.add_argument("--app-path", help="Optional absolute installed badge app directory")
    parser.add_argument("--disabled", action="store_true", help="Run the launcher without instrumentation; incompatible with --downloads")
    parser.add_argument("--clock-drift-ppm", type=float)
    parser.add_argument("--session", type=Path, help="Private 0600 opponent session JSON; used only with an explicit download plan")
    parser.add_argument("--downloads", type=Path, help="Explicit download sample array; enables one backend RPC per sample")
    args = parser.parse_args(argv)
    try:
        session = load_session(args.session) if args.session else None
        downloads = json.loads(args.downloads.read_text()) if args.downloads else []
        plan = validate_plan(downloads, args.capacity)
        if args.downloads and session is None:
            raise ValueError("--downloads requires --session")
        if args.downloads and args.disabled:
            raise ValueError("--disabled is incompatible with --downloads")
        _number(args.seconds, "seconds", 1, 220)
        if type(args.capacity) is not int or not 1 <= args.capacity <= 64:
            raise ValueError("capacity must be 1..64")
        if args.clock_drift_ppm is not None:
            _number(args.clock_drift_ppm, "clock_drift_ppm", 0, 1_000_000)
        if sum(row["wait_seconds"] for row in plan) + len(plan) * 2 >= args.seconds:
            raise ValueError("Download plan does not fit capture duration")
        from prepare_latency import build_device_source
        source = build_device_source(seconds=min(240, math.ceil(args.seconds) + 20), capacity=args.capacity,
                                     enabled=not args.disabled, app_path=args.app_path)
        # Reserve a private output before opening serial or making any request.
        # O_EXCL prevents a race from overwriting an earlier measurement.
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            capture = {"format_version": 1, "device": {"format_version": 1, "local": [], "downloads": []},
                       "host": {"format_version": 1, "sync_probes": [], "download_rpc": []},
                       "capture": {"status": "failed", "error": {"stage": "serial_start", "type": "Unknown"}}}
            try:
                from mpremote.transport_serial import SerialTransport
                device = RawDevice(SerialTransport(args.port, timeout=2))
                capture = run_capture(device, source, seconds=args.seconds, capacity=args.capacity,
                                      session=session, downloads=plan, clock_drift_ppm=args.clock_drift_ppm,
                                      enabled=not args.disabled,
                                      notify=lambda message: print(message, flush=True))
            except (Exception, KeyboardInterrupt) as error:
                capture["capture"]["error"] = {"stage": "serial_start", "type": type(error).__name__}
            finally:
                json.dump(capture, output, indent=2, allow_nan=False)
                output.write("\n")
                output.flush()
        print("Capture " + capture["capture"]["status"] + ". Saved " + str(args.output))
        return 0 if capture["capture"]["status"] == "complete" else 1
    except (Exception, KeyboardInterrupt) as error:
        # Private session contents/HTTP bodies/serial output are never printed.
        print("Capture could not start or save (" + type(error).__name__ + ").")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
