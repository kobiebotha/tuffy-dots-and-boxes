"""The emitted RAM launcher uses Badgeware's loop and restores app globals."""
import builtins
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
from prepare_latency import build_device_source


class Input:
    def __init__(self):
        self.buffer = ""

    def feed(self, value):
        self.buffer += value

    def read(self, count):
        result, self.buffer = self.buffer[:count], self.buffer[count:]
        return result


class Poller:
    def __init__(self):
        self.source = None
        self.unregistered = False

    def register(self, source, mask):
        self.source = source

    def unregister(self, source):
        assert source is self.source
        self.unregistered = True

    def poll(self, _timeout):
        return [(self.source, 1)] if self.source.buffer else []


class LatencyLauncherTests(unittest.TestCase):
    def run_device(self, enabled=True, paired=True, commands=None, fail_update=False, pending=False,
                   schedule=None, synced=True, can_claim=True, occupied=False, loop_pending=False):
        """Execute the actual generated source; all device surfaces are fakes."""
        device_input = Input()
        poller = Poller()
        events = []
        clock = [0]
        wire_events = []

        class Output(io.StringIO):
            def write(self, text):
                if text.startswith("DOTS_LATENCY "):
                    message = json.loads(text[len("DOTS_LATENCY "):])
                    wire_events.append((message["command"], clock[0]))
                return super().write(text)

        output = Output()
        app_path = "/apps/test_dots_latency"
        app_module = ModuleType(app_path)
        previous_run = object()
        previous_builtin_display = object()
        loop_calls = []
        original_path, original_directory = sys.path[:], os.getcwd()

        class Records:
            def __init__(self):
                self.record = {"pending_move": None, "room_id": "private-room", "seat": 1}

            def save(self, value):
                events.append("save")
                clock[0] += 30
                self.record = value

        class Store:
            def control_lines(self, *args, **kwargs):
                return []

            def has_move(self, *_args):
                return False

        class Sync:
            synced, phase = True, "stream"

            def _after_upload(self, accepted=False):
                return accepted

        class View:
            def draw(self, *_args):
                events.append("draw")
                clock[0] += 20

        class Client:
            failure = None

            def __init__(self):
                self.records = Records()
                self.store = Store()
                self.sync = Sync() if paired else None
                if self.sync is not None:
                    self.sync.synced = synced
                self.controller = SimpleNamespace(on_claim=self._queue_move, pending_edge=None,
                                                  pending_player=0,
                                                  selecting=False, preview=None, can_claim=can_claim,
                                                  game=SimpleNamespace(moves=0, current_player=1))
                def edge_owner(orientation, row, col):
                    if (orientation not in ("h", "v") or type(row) is not int or type(col) is not int
                            or not 0 <= row <= (4 if orientation == "h" else 3)
                            or not 0 <= col <= (4 if orientation == "h" else 5)):
                        raise ValueError("Invalid edge")
                    return 1 if occupied else 0
                self.controller.game.edge_owner = edge_owner
                def step(held, elapsed):
                    events.append("step")
                    clock[0] += 5
                def tap():
                    events.append("tap")
                    if (self.controller.can_claim and self.controller.selecting
                            and self.controller.preview is not None):
                        accepted = self.controller.on_claim(self.controller.preview)
                        if accepted:
                            self.controller.selecting = False
                            self.controller.preview = None
                self.controller.step = step
                self.controller.tap = tap

            def _queue_move(self, edge):
                events.append("queue")
                self.records.save(dict(self.records.record, pending_move={
                    "id": "private-move", "orientation": edge[0], "row": edge[1], "col": edge[2]}))
                self.controller.pending_edge = edge
                self.controller.pending_player = 1
                return True

        client, view = Client(), View()
        if pending:
            client.records.record["pending_move"] = {
                "id": "preexisting-private-move", "orientation": "h", "row": 0, "col": 0}
        app_module.client, app_module.view = client, view
        badgeware = ModuleType("badgeware")
        display_count = [0]
        if schedule is not None:
            device_input.feed(json.dumps(schedule) + "\n")

        class Display:
            def update(self):
                events.append("display")
                clock[0] += 250
                display_count[0] += 1
                if schedule is not None:
                    if display_count[0] == 4:
                        device_input.feed('{"command":"stop","request_id":9}\n')
                elif commands is None:
                    device_input.feed('{"command":"stop","request_id":9}\n')
                else:
                    device_input.feed(commands)
                return "display-result"

            def backlight(self):
                return "original-backlight"

        original_display = Display()
        badgeware.display = original_display

        def app_update():
            if fail_update:
                raise RuntimeError("test update failed")
            if schedule is not None:
                client.controller.step([], 16)
                view.draw(client.controller)
            elif client.records.record["pending_move"] is None:
                client.controller.on_claim(("h", 0, 0))
                view.draw(client.controller)

        def normal_run(update, duration):
            loop_calls.append(duration)
            if loop_pending:
                client.records.record["pending_move"] = {"id": "recovery-pending"}
            for _ in range(8):
                if update() is not None:
                    return "normal-loop-result"
                # This is the unmodified Badgeware ordering: callback, display.
                badgeware.display.update()
            self.fail("stop protocol did not terminate the loop")

        badgeware._run = normal_run
        with tempfile.TemporaryDirectory(prefix="dots-latency-launcher-") as directory:
            def launch(path):
                self.assertEqual(path, app_path)
                os.chdir(directory)
                sys.path.insert(0, path)
                return builtins.run(app_update)

            badgeware.launch = launch
            fake_select = SimpleNamespace(POLLIN=1, poll=lambda: poller)
            fake_time = SimpleNamespace(perf_counter_ns=lambda: clock[0] * 1000)
            source = build_device_source(seconds=10, capacity=2, enabled=enabled, app_path=app_path)
            namespace = {}
            caught = None
            with patch.dict(sys.modules, {"badgeware": badgeware, "select": fake_select,
                                          "time": fake_time, app_path: app_module}), \
                    patch.object(sys, "stdin", device_input), \
                    patch.object(builtins, "run", previous_run, create=True), \
                    patch.object(builtins, "display", previous_builtin_display, create=True), \
                    redirect_stdout(output):
                try:
                    exec(compile(source, "generated-latency.py", "exec"), namespace)
                except Exception as error:
                    caught = error
                self.assertIs(builtins.run, previous_run)
                self.assertIs(builtins.display, previous_builtin_display)
                self.assertIs(badgeware.display, original_display)
                self.assertEqual(sys.path, original_path)
                self.assertEqual(os.getcwd(), original_directory)
            self.assertTrue(poller.unregistered)
        messages = [json.loads(line[len("DOTS_LATENCY "):]) for line in output.getvalue().splitlines()
                    if line.startswith("DOTS_LATENCY ")]
        return SimpleNamespace(messages=messages, events=events, namespace=namespace,
                               loop_calls=loop_calls, client=client, view=view, error=caught,
                               wire_events=wire_events)

    def test_generated_source_compiles_and_wraps_display_after_normal_transfer(self):
        result = self.run_device()
        self.assertIsNone(result.error)
        self.assertEqual(result.events, ["queue", "save", "draw", "display"])
        self.assertEqual(result.loop_calls, [10000])
        ready, stopped, finished = result.messages
        self.assertEqual(ready["command"], "ready")
        self.assertTrue(ready["enabled"])
        self.assertEqual(stopped, {"command": "stop", "request_id": 9,
                                   "device_us": 300, "accepted": True})
        self.assertEqual(finished["command"], "finished")
        row = finished["device"]["local"][0]
        self.assertEqual(row["framebuffer_us"], 50)
        self.assertEqual(row["panel_submitted_us"], 300)
        self.assertGreaterEqual(stopped["device_us"], row["panel_submitted_us"])
        self.assertEqual(row["outcome"], "incomplete")
        harness = finished["device"]["harness"]
        self.assertEqual(harness["seconds"], 10)
        self.assertEqual(harness["capacity"], 2)
        self.assertTrue(harness["enabled"])
        self.assertEqual(len(harness["source_sha256"]), 64)
        self.assertNotIn("private-move", repr(finished))
        self.assertNotIn("_queue_move", result.client.__dict__)
        self.assertNotIn("draw", result.view.__dict__)

    def test_disabled_baseline_uses_normal_loop_without_attaching(self):
        result = self.run_device(enabled=False)
        self.assertIsNone(result.error)
        self.assertFalse(result.messages[0]["enabled"])
        self.assertEqual(result.events, ["queue", "save", "draw", "display"])
        self.assertEqual(result.messages[-1]["device"]["local"], [])
        self.assertNotIn("_queue_move", result.client.__dict__)

    def test_unpaired_client_is_refused_before_loop_and_exports_empty_capture(self):
        result = self.run_device(paired=False)
        self.assertIsNone(result.error)
        self.assertEqual(result.loop_calls, [])
        self.assertEqual(result.events, [])
        self.assertEqual(result.messages[0], {
            "command": "ready", "accepted": False, "error": "PairedClientRequired"})
        self.assertEqual(result.messages[-1]["device"]["local"], [])

    def test_saved_pending_move_refuses_capture_without_entering_fatal_error_flow(self):
        result = self.run_device(pending=True)
        self.assertIsNone(result.error)
        self.assertEqual(result.loop_calls, [])
        self.assertEqual(result.events, [])
        self.assertEqual(result.messages[0], {
            "command": "ready", "accepted": False, "error": "PairedClientRequired"})
        finished = result.messages[-1]
        self.assertEqual(finished["command"], "finished")
        self.assertEqual(finished["device"]["local"], [])
        self.assertEqual(result.client.records.record["pending_move"]["id"], "preexisting-private-move")
        self.assertNotIn("preexisting-private-move", repr(finished))

    def test_partial_and_invalid_commands_do_not_block_stop(self):
        result = self.run_device(commands='not-json\n[1,2]\n{"command":"stop","request_id":17}\n')
        self.assertIsNone(result.error)
        invalid = [message for message in result.messages if message["command"] == "invalid"]
        self.assertEqual(len(invalid), 2)
        self.assertTrue(all(message["error"] == "InvalidJSON" for message in invalid))
        self.assertTrue(any(message.get("request_id") == 17 and message.get("accepted")
                            for message in result.messages))

    def test_exception_still_detaches_and_restores_globals(self):
        result = self.run_device(fail_update=True)
        self.assertIsInstance(result.error, RuntimeError)
        self.assertEqual(result.messages[-1]["command"], "finished")
        self.assertNotIn("_queue_move", result.client.__dict__)
        self.assertNotIn("draw", result.view.__dict__)

    def test_scheduled_claim_runs_after_controller_step_before_draw_and_ack_is_outside_timing(self):
        result = self.run_device(schedule={"command": "schedule_local", "request_id": 21,
                                           "edge": ["h", 0, 0], "delay_frames": 3})
        self.assertIsNone(result.error)
        scheduled = next(message for message in result.messages if message["command"] == "schedule_local")
        self.assertEqual(scheduled, {"command": "schedule_local", "request_id": 21,
                                     "accepted": True, "sample_id": 0})
        self.assertEqual(result.events[:13], ["step", "draw", "display", "step", "draw", "display",
                                             "step", "tap", "queue", "save", "draw", "display", "step"])
        row = result.messages[-1]["device"]["local"][0]
        self.assertEqual(row["queued_us"], 555)
        self.assertEqual(row["framebuffer_us"], 605)
        self.assertEqual(row["panel_submitted_us"], 855)
        ack_stamp = next(stamp for command, stamp in result.wire_events if command == "schedule_local")
        self.assertLess(ack_stamp, row["queued_us"])
        self.assertEqual([command for command, stamp in result.wire_events
                          if row["queued_us"] <= stamp < row["panel_submitted_us"]], [])
        self.assertEqual(result.events.count("tap"), 1)

    def test_scheduled_claim_refuses_unsynced_pending_unavailable_or_occupied_board(self):
        command = {"command": "schedule_local", "request_id": 22, "edge": ["h", 0, 0]}
        for settings in ({"synced": False}, {"can_claim": False}, {"occupied": True}, {"loop_pending": True}):
            with self.subTest(settings=settings):
                result = self.run_device(schedule=command, **settings)
                self.assertIsNone(result.error)
                scheduled = next(message for message in result.messages if message["command"] == "schedule_local")
                self.assertFalse(scheduled["accepted"])
                self.assertNotIn("tap", result.events)
                self.assertNotIn("queue", result.events)
                self.assertEqual(result.messages[-1]["device"]["local"], [])

    def test_scheduled_claim_validates_delay_and_edge_before_app_write(self):
        for extra in ({"delay_frames": 0}, {"delay_frames": 61}, {"delay_frames": True},
                      {"edge": ["v", 4, 0]}, {"edge": ["bad", 0, 0]}):
            with self.subTest(extra=extra):
                command = {"command": "schedule_local", "request_id": 23, "edge": ["h", 0, 0], **extra}
                result = self.run_device(schedule=command)
                self.assertIsNone(result.error)
                scheduled = next(message for message in result.messages if message["command"] == "schedule_local")
                self.assertFalse(scheduled["accepted"])
                self.assertNotIn("queue", result.events)
                self.assertEqual(result.messages[-1]["device"]["local"], [])

    def test_emitter_rejects_unbounded_or_relative_configuration(self):
        for args in ({"seconds": 1}, {"seconds": 241}, {"capacity": 65}, {"enabled": 1},
                     {"app_path": "relative/app"}, {"app_path": "/apps/../dots_boxes"}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                build_device_source(**args)
        source = build_device_source(app_path="/apps/dots_boxes")
        compile(source, "dots-latency-device.py", "exec")
        self.assertIn("source SHA256", source)


if __name__ == "__main__":
    unittest.main()
