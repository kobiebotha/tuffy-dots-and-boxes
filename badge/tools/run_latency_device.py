"""RAM launcher template. Emit with prepare_latency.py; do not install this file.

Runs the installed app through Badgeware, with its normal loop and display.
DOTS_LATENCY JSON command responses are emitted only on explicit host requests
or outside the measured run. Home/Reset discards RAM data; use stop instead.
"""
import builtins
import json
import os
import select
import sys
import badgeware


def run_latency(config, implementation):
    probe = implementation["Probe"](config["capacity"])
    original_run = getattr(builtins, "run", None)
    original_display = badgeware.display
    original_path = sys.path[:]
    original_directory = os.getcwd()
    poller = select.poll()
    poller.register(sys.stdin, select.POLLIN)
    receiving = ""
    dropping = False
    client = None
    stopped = False
    scheduled = None
    schedule_error = None
    step_error = None

    def reply(message):
        print("DOTS_LATENCY " + json.dumps(message))

    def command(message):
        nonlocal stopped, scheduled, schedule_error
        name = message.get("command")
        result = {"command": name, "request_id": message.get("request_id")}
        try:
            if name == "stop":
                # Host brackets this read as the final clock probe, including
                # when a last move is incomplete at the capture deadline.
                result["device_us"] = probe.now()
                stopped = True
                scheduled = None
                result["accepted"] = True
            elif probe.active is not None:
                raise ValueError("Busy")
            elif name == "clock":
                result["device_us"] = probe.now()
            elif name == "arm_download" and config["enabled"]:
                if scheduled is not None:
                    raise ValueError("Local move already scheduled")
                probe.arm_download(client, message["sample_id"], message["move_id"],
                                   message["edge"], message["move_index"])
                result["accepted"] = True
            elif name == "schedule_local" and config["enabled"]:
                edge = message["edge"]
                delay = message.get("delay_frames", 3)
                controller = client.controller
                if (type(delay) is not int or not 1 <= delay <= 60
                        or not isinstance(edge, (list, tuple)) or len(edge) != 3
                        or scheduled is not None or not client.sync.synced
                        or client.sync.phase != "stream" or not controller.can_claim
                        or client.records.record["pending_move"] is not None
                        or probe.local_count == probe.capacity
                        or controller.game.edge_owner(*edge)):
                    raise ValueError("Claim unavailable")
                scheduled = {"edge": tuple(edge), "frames": delay}
                schedule_error = None
                result.update(accepted=True, sample_id=probe.local_count)
            elif name == "export":
                result["device"] = exported()
            elif name == "status":
                stream = client.sync.stream if client.sync else None
                result.update(synced=client.sync.synced if client.sync else False,
                              phase=client.sync.phase if client.sync else "closed",
                              moves=client.controller.game.moves,
                              current_seat=client.controller.game.current_player,
                              pending=client.records.record["pending_move"] is not None,
                              scheduled=scheduled is not None, schedule_error=schedule_error,
                              local_count=probe.local_count, download_count=probe.download_count,
                              sync_status=client.sync.status if client.sync else "closed",
                              sync_error=client.sync.last_error if client.sync else "",
                              connected=client.sync.connected if client.sync else False,
                              generation=client.sync.generation if client.sync else 0,
                              transport_phase=getattr(stream, "phase", None),
                              transport_status=getattr(stream, "status", None),
                              bytes_sent=getattr(stream, "sent", None),
                              mode=client.mode,
                              failure=client.failure,
                              app_failure=getattr(sys.modules.get(app_path), "failure", None),
                              step_error=step_error)
            else:
                raise ValueError("Unknown command")
        except (ValueError, KeyError, TypeError, OSError) as error:
            # No exception text, saved state, credentials or UUIDs go to USB.
            result.update(accepted=False, error="Busy" if probe.active is not None else type(error).__name__)
        reply(result)

    def poll_commands():
        nonlocal receiving, dropping
        # Bounded byte-at-a-time reads never wait for a partial command line.
        for _ in range(256):
            if not poller.poll(0):
                break
            char = sys.stdin.read(1)
            if not char:
                break
            if char == "\n":
                if receiving and not dropping:
                    try:
                        message = json.loads(receiving)
                        if not isinstance(message, dict):
                            raise ValueError("Object required")
                    except (ValueError, TypeError):
                        reply({"command": "invalid", "accepted": False, "error": "InvalidJSON"})
                    else:
                        command(message)
                receiving, dropping = "", False
            elif char != "\r" and not dropping:
                if len(receiving) >= 768:
                    receiving, dropping = "", True
                else:
                    receiving += char

    class DisplayProxy:
        def __getattr__(self, name):
            return getattr(original_display, name)

        def update(self, *args, **kwargs):
            result = original_display.update(*args, **kwargs)
            sample = probe.active
            probe.panel_submitted()
            # This notification is after every timestamp for the sample. It
            # lets the host wait silently instead of polling during timing.
            if sample is not None and probe.active is None:
                reply({"command": "sample_done", "kind": sample["_kind"],
                       "sample_id": sample["sample_id"]})
            return result

    def measured_run(update):
        nonlocal client
        # MicroPython need not expose function.__globals__. The module is
        # registered in sys.modules while Badgeware executes its entry point.
        module = sys.modules.get(app_path)
        client = getattr(module, "client", None)
        view = getattr(module, "view", None)
        if (client is None or view is None or client.sync is None or client.failure
                or client.records.record["pending_move"] is not None):
            reply({"command": "ready", "accepted": False, "error": "PairedClientRequired"})
            return
        detach = None
        original_step = client.controller.step
        owned_step = "step" in getattr(client.controller, "__dict__", {})
        try:
            if config["enabled"]:
                _, detach = implementation["attach"](client, view, probe)
                badgeware.display = DisplayProxy()
                def scheduled_step(*args, **kwargs):
                    nonlocal scheduled, schedule_error
                    result = original_step(*args, **kwargs)
                    if scheduled is not None:
                        scheduled["frames"] -= 1
                        if scheduled["frames"] <= 0:
                            edge = scheduled["edge"]
                            scheduled = None
                            controller = client.controller
                            if (probe.active is None and controller.can_claim
                                    and client.records.record["pending_move"] is None
                                    and not controller.game.edge_owner(*edge)):
                                controller.selecting = True
                                controller.preview = edge
                                controller.tap()
                                if client.records.record["pending_move"] is None:
                                    schedule_error = "ClaimUnavailable"
                            else:
                                schedule_error = "ClaimUnavailable"
                    return result
                client.controller.step = scheduled_step
            reply({"command": "ready", "format_version": 1, "accepted": True,
                   "enabled": config["enabled"]})

            def frame():
                probe.now()  # Extend wrapping ticks_us at least once per frame.
                poll_commands()
                if stopped:
                    return True
                return update()

            return badgeware._run(frame, duration=config["seconds"] * 1000)
        finally:
            badgeware.display = original_display
            if detach is not None:
                if owned_step:
                    client.controller.step = original_step
                else:
                    delattr(client.controller, "step")
                detach()

    app_path = config.get("app_path")
    if app_path is None:
        for candidate in ("/apps/dots_boxes", "/system/apps/dots_boxes"):
            try:
                os.stat(candidate + "/__init__.py")
            except OSError:
                continue
            app_path = candidate
            break
    if app_path is None:
        raise ValueError("Installed dots_boxes app not found")
    def exported():
        result = probe.export()
        result["harness"] = config
        return result

    try:
        builtins.run = measured_run
        badgeware.launch(app_path)
    finally:
        if original_run is None:
            delattr(builtins, "run")
        else:
            builtins.run = original_run
        badgeware.display = original_display
        sys.path[:] = original_path
        os.chdir(original_directory)
        poller.unregister(sys.stdin)
        reply({"command": "finished", "device": exported()})


run_latency(_LATENCY_CONFIG, _latency_impl)
