"""Exercise scheduler/failure behavior with no serial device or live requests."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import benchmark_latency as runner


MOVE = "550e8400-e29b-41d4-a716-446655440001"
ROOM = "550e8400-e29b-41d4-a716-446655440000"


class Clock:
    def __init__(self):
        self.value = 10_000_000
    def __call__(self):
        return self.value
    def sleep(self, seconds):
        self.value += round(seconds * 1_000_000)


class Device:
    def __init__(self, clock, enabled=True, complete=True, busy=False, wait_sync=False):
        self.clock = clock
        self.enabled, self.complete, self.busy = enabled, complete, busy
        self.wait_sync = wait_sync
        self.finished = None
        self.closed = False
        self.calls = []
        self.armed = []
        self.moves = 0
        self.snapshot = {"format_version": 1, "local": [{"sample_id": 0, "queued_us": 100,
                         "panel_submitted_us": 300, "rpc_done_us": 500, "outcome": "accepted"}],
                         "downloads": []}
    def start(self, source):
        self.calls.append(("start", self.clock()))
        return {"command": "ready", "format_version": 1, "accepted": True, "enabled": self.enabled}
    def command(self, message):
        name = message["command"]
        self.calls.append((name, self.clock()))
        reply = {**message}
        if name in ("clock", "stop"):
            reply["device_us"] = self.clock() - 9_000_000
        if name == "status":
            reply.update(synced=not self.wait_sync, phase="stream", moves=self.moves)
            self.wait_sync = False
        if name == "arm_download":
            self.armed.append(message)
            reply["accepted"] = True
        if name == "export":
            if self.busy:
                raise runner.DeviceBusy()
            self.publish()
            reply["device"] = self.snapshot
        if name == "stop":
            reply["accepted"] = True
        return reply
    def publish(self):
        self.snapshot["downloads"] = [{"sample_id": sample["sample_id"], "armed_us": 1000,
                **({"sqlite_persisted_us": 2000, "framebuffer_us": 2100,
                    "panel_submitted_us": 2200} if self.complete else {})} for sample in self.armed]
        if self.complete and self.armed:
            self.moves = self.armed[-1]["move_index"]
    def wait_finished(self):
        self.calls.append(("finished", self.clock()))
        self.publish()
        self.finished = {"command": "finished", "device": self.snapshot}
        return self.finished
    def close(self):
        self.closed = True


class LatencyRunnerTests(unittest.TestCase):
    def plan(self, count=1):
        return [{"sample_id": index, "edge": ["h", 0, index], "move_index": index + 1,
                 "wait_seconds": 1, "move_id": str(MOVE[:-1]) + str(index + 1)} for index in range(count)]
    def capture(self, device, clock, **kwargs):
        return runner.run_capture(device, "RAM_SOURCE", seconds=10, clock=clock,
                                  sleep=clock.sleep, **kwargs)

    def test_local_capture_never_submits_rpc_and_final_stop_brackets_clock(self):
        clock = Clock()
        device = Device(clock)
        def forbidden(*_args, **_kwargs):
            self.fail("Local capture must not call the backend")
        capture = self.capture(device, clock, submit=forbidden)
        self.assertEqual(capture["capture"]["status"], "complete")
        self.assertEqual(capture["host"]["download_rpc"], [])
        self.assertEqual(len(capture["host"]["sync_probes"]), 2)
        self.assertEqual([name for name, _ in device.calls], ["start", "clock", "stop", "finished"])
        self.assertEqual(capture["device"]["local"][0]["rpc_done_us"], 500)
        self.assertTrue(device.closed)

    def test_download_is_armed_once_then_quiet_and_verified_before_next_rpc(self):
        clock = Clock()
        device = Device(clock, wait_sync=True)
        calls = []
        def submit(session, move, sample_id, clock):
            calls.append((sample_id, clock()))
            self.assertEqual(device.armed[-1]["move_id"], move["id"])
            return {"sample_id": sample_id, "host_send_us": clock(), "host_receive_us": clock(),
                    "accepted": True, "duplicate": False}
        capture = self.capture(device, clock, session={"private": "not exported"}, downloads=self.plan(2), submit=submit)
        self.assertEqual(capture["capture"]["status"], "complete")
        self.assertEqual(len(calls), 2)
        exports = [at for name, at in device.calls if name == "export"]
        self.assertEqual(exports[0] - calls[0][1], 1_000_000)
        self.assertGreaterEqual(calls[1][1], exports[0])
        # No host/device traffic is scheduled inside the download quiet window.
        for name, at in device.calls:
            for _sample, submitted in calls:
                self.assertFalse(submitted < at < submitted + 1_000_000, name)
        self.assertEqual(len(capture["host"]["sync_probes"]), 6)
        self.assertNotIn(MOVE, json.dumps(capture))
        self.assertNotIn("not exported", json.dumps(capture))

    def test_incomplete_download_stops_before_a_second_backend_write(self):
        clock = Clock()
        device = Device(clock, complete=False)
        calls = []
        def submit(session, move, sample_id, clock):
            calls.append(sample_id)
            return {"sample_id": sample_id, "accepted": True, "duplicate": False}
        capture = self.capture(device, clock, session={}, downloads=self.plan(2), submit=submit)
        self.assertEqual(calls, [0])
        self.assertEqual(capture["capture"]["status"], "failed")
        self.assertEqual(capture["capture"]["error"]["stage"], "verify_download")
        self.assertEqual(capture["device"]["downloads"], [{"sample_id": 0, "armed_us": 1000}])
        self.assertTrue(device.closed)

    def test_busy_export_stops_and_retains_final_partial_device_export(self):
        clock = Clock()
        device = Device(clock, complete=False, busy=True)
        capture = self.capture(device, clock, session={}, downloads=self.plan(),
                               submit=lambda *args, **kwargs: {"sample_id": 0, "accepted": True, "duplicate": False})
        self.assertEqual(capture["capture"]["error"]["type"], "DeviceBusy")
        self.assertEqual(capture["device"]["downloads"][0]["sample_id"], 0)
        self.assertEqual([name for name, _ in device.calls][-2:], ["stop", "finished"])

    def test_uncertain_rpc_outcome_is_never_retried(self):
        clock = Clock()
        device = Device(clock, complete=False)
        calls = []
        def submit(*args, **kwargs):
            calls.append(1)
            raise TimeoutError("may already have committed")
        capture = self.capture(device, clock, session={}, downloads=self.plan(2), submit=submit)
        self.assertEqual(calls, [1])
        self.assertEqual(capture["capture"]["error"], {"stage": "submit_download_rpc", "type": "TimeoutError"})
        self.assertTrue(device.closed)

    def test_keyboard_interrupt_still_stops_closes_and_returns_partial_artifact(self):
        clock = Clock()
        device = Device(clock)
        def interrupted(_seconds):
            raise KeyboardInterrupt()
        capture = runner.run_capture(device, "source", seconds=10, clock=clock, sleep=interrupted)
        self.assertEqual(capture["capture"]["error"]["type"], "KeyboardInterrupt")
        self.assertEqual(capture["device"]["local"][0]["outcome"], "accepted")
        self.assertTrue(device.closed)

    def test_wrong_board_rejects_plan_before_any_rpc(self):
        clock = Clock()
        device = Device(clock)
        device.moves = 4
        calls = []
        capture = self.capture(device, clock, session={}, downloads=self.plan(), submit=lambda *_a, **_k: calls.append(1))
        self.assertEqual(calls, [])
        self.assertEqual(capture["capture"]["error"]["stage"], "download_readiness")

    def test_disabled_baseline_is_supported_but_cannot_drive_downloads(self):
        clock = Clock()
        device = Device(clock, enabled=False)
        capture = self.capture(device, clock, enabled=False)
        self.assertEqual(capture["capture"]["status"], "complete")
        with self.assertRaisesRegex(ValueError, "enabled probe"):
            self.capture(device, clock, session={}, downloads=self.plan(), enabled=False)

    def test_validation_runs_before_device_or_backend(self):
        clock = Clock()
        device = Device(clock)
        with self.assertRaisesRegex(ValueError, "explicit private session"):
            self.capture(device, clock, downloads=self.plan())
        self.assertEqual(device.calls, [])
        with self.assertRaisesRegex(ValueError, "distinct"):
            runner.validate_plan(self.plan() * 2, 24)
        with self.assertRaisesRegex(ValueError, "board"):
            runner.validate_plan([{**self.plan()[0], "edge": ["h", 5, 0]}], 24)

    def test_private_credentials_and_exclusive_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private.json"
            session = {"url": "https://example.supabase.co", "key": "private", "access_token": "private", "room_id": ROOM}
            path.write_text(json.dumps(session))
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "0600"):
                runner.load_session(path)
            path.chmod(0o600)
            self.assertEqual(runner.load_session(path), session)
            output = Path(directory) / "capture.json"
            runner.write_capture(output, {"status": "test"})
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                runner.write_capture(output, {"status": "overwritten"})
            self.assertEqual(json.loads(output.read_text()), {"status": "test"})

    def test_cli_reserves_partial_output_before_serial_start(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            module = ModuleType("mpremote.transport_serial")
            def constructor(_port, timeout):
                self.assertEqual(timeout, 2)
                self.assertTrue(path.exists())
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                raise OSError("No device: private serial details should stay hidden")
            module.SerialTransport = constructor
            with patch.dict(sys.modules, {"mpremote": ModuleType("mpremote"), "mpremote.transport_serial": module}):
                result = runner.main(["--port", "FAKE", "--output", str(path), "--seconds", "1"])
            self.assertEqual(result, 1)
            capture = json.loads(path.read_text())
            self.assertEqual(capture["capture"]["error"], {"stage": "serial_start", "type": "OSError"})
            self.assertNotIn("private serial", path.read_text())


class Serial:
    def __init__(self, clock):
        self.clock = clock
        self.pending = bytearray()
        self.written = []
        self.timeout = .2
    @property
    def in_waiting(self):
        return len(self.pending)
    def read(self, count):
        self.clock.sleep(.01)
        count = min(count, 7)  # Deliberately split JSON across serial reads.
        result = bytes(self.pending[:count])
        del self.pending[:count]
        return result
    def write(self, value):
        self.written.append(value)
        request = json.loads(value)
        self.pending.extend(b"DOTS_LATENCY " + json.dumps({**request, "device_us": 1000}).encode() + b"\r\n")


class Transport:
    def __init__(self, clock):
        self.serial = Serial(clock)
        self.calls = []
    def enter_raw_repl(self, soft_reset):
        self.calls.append(("enter", soft_reset))
    def exec_raw_no_follow(self, source):
        self.calls.append(("execute", source))
        self.serial.pending.extend(b"boot text that must not become capture data\r\n\x04DOTS_LATENCY "
            + json.dumps({"command": "ready", "format_version": 1, "enabled": True, "accepted": True}).encode() + b"\r\n")
    def close(self):
        self.calls.append(("close",))


class RawProtocolTests(unittest.TestCase):
    def test_raw_entry_does_not_soft_reset_and_streaming_json_is_reassembled(self):
        clock = Clock()
        transport = Transport(clock)
        device = runner.RawDevice(transport, lambda: clock() / 1_000_000)
        self.assertTrue(device.start("RAM_SOURCE")["enabled"])
        self.assertEqual(transport.calls, [("enter", False), ("execute", b"RAM_SOURCE")])
        reply = device.command({"command": "clock", "request_id": 8})
        self.assertEqual(reply["device_us"], 1000)
        self.assertEqual(json.loads(transport.serial.written[0]), {"command": "clock", "request_id": 8})
        device.close()
        self.assertEqual(transport.calls[-1], ("close",))

    def test_protocol_busy_and_mismatched_responses_fail_closed(self):
        clock = Clock()
        transport = Transport(clock)
        device = runner.RawDevice(transport, lambda: clock() / 1_000_000)
        device.start("RAM_SOURCE")
        transport.serial.pending.extend(b'DOTS_LATENCY {"command":"export","request_id":8,"accepted":false,"error":"Busy"}\n')
        with self.assertRaises(runner.DeviceBusy):
            device.command({"command": "export", "request_id": 8})
        # The prior fake write also queued a response, so the next ID must not
        # accidentally consume it as the response to a different request.
        with self.assertRaises(runner.DeviceProtocolError):
            device.command({"command": "clock", "request_id": 9})

    def test_missing_reply_times_out_without_infinite_polling(self):
        clock = Clock()
        transport = Transport(clock)
        device = runner.RawDevice(transport, lambda: clock() / 1_000_000)
        with self.assertRaises(TimeoutError):
            device.read_message(timeout=.05)


if __name__ == "__main__":
    unittest.main()
