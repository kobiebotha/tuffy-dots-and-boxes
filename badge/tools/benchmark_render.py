"""RAM-only renderer comparison, usable on CPython or the qualified badge.

Load this source with exec(source, namespace), then call namespace['run'] with
the before/after View classes, a zero-argument screen factory, color, font and
Controller. Badge factory: lambda: picovector.image(320, 240). Load each View
with its matching Logos class when comparing an older version. This never
opens a file, touches flash, polls buttons or sends the framebuffer to a panel.
Host timings are not device timings; use the real font and screen.raw on badge.
"""
import gc
import hashlib
import time


def _micros():
    if hasattr(time, "ticks_us"):
        return time.ticks_us()
    return time.perf_counter_ns() // 1000


def _elapsed(start):
    if hasattr(time, "ticks_diff"):
        return time.ticks_diff(_micros(), start)
    return _micros() - start


def _timed(draw, controller, iterations, cursor=False):
    gc.collect()
    started = _micros()
    for _ in range(iterations):
        if cursor:
            controller.col = (controller.col + 1) % (controller.game.cols + 1)
        draw(controller, 1)
    return _elapsed(started) / iterations


def _change(controller, scene):
    game = controller.game
    if scene == "cursor":
        controller.col += 1
    elif scene == "selection":
        controller.selecting = True
    elif scene == "preview":
        controller.preview = ("h", 0, 0)
    elif scene == "taken_preview":
        game.claim("h", 0, 0)
    elif scene == "pending":
        controller.selecting = False
        controller.preview = None
        controller.pending_edge = ("v", 0, 0)
        controller.pending_player = 2
        controller.status_line = "SENDING MOVE"
    elif scene == "confirmed":
        game.claim("v", 0, 0)
        controller.pending_edge = None
        controller.pending_player = 0
        controller.status_line = ""
    elif scene == "offline":
        controller.status_line = "OFFLINE"
    elif scene == "message":
        controller.message_ms = 500
        controller.message = "Selection cancelled."
    elif scene == "message_expired":
        controller.message_ms = 0
    elif scene == "flash":
        game.boxes[0] = 1
        controller.flash_boxes = ((0, 0),)
        controller.flash_ms = 899
    elif scene == "flash_phase":
        controller.flash_ms = 749
    elif scene == "flash_expired":
        controller.flash_ms = 0
    elif scene == "reset":
        game.reset()
    elif scene == "finished":
        for row in range(game.rows + 1):
            for col in range(game.cols):
                game.claim("h", row, col)
        for row in range(game.rows):
            for col in range(game.cols + 1):
                game.claim("v", row, col)


def run(before, after, screen_factory, color, font, controller_factory, iterations=30, rounds=3):
    if iterations < 1 or rounds < 1:
        raise ValueError("Benchmark needs at least one iteration and round")
    old_screen, new_screen = screen_factory(), screen_factory()
    old, new = before(old_screen, color, font), after(new_screen, color, font)
    old_controller, new_controller = controller_factory(), controller_factory()
    old_draw = old.draw
    new_draw = new.draw_if_changed
    checked = []
    for scene in ("idle", "cursor", "selection", "preview", "taken_preview",
                  "pending", "confirmed", "offline", "message", "message_expired",
                  "flash", "flash_phase", "flash_expired", "reset", "finished",
                  "pair", "pair_changed", "error", "return_to_game"):
        _change(old_controller, scene)
        _change(new_controller, scene)
        if scene in ("pair", "pair_changed"):
            code = "ABC" if scene == "pair" else "ABCAA"
            old.pairing(code, "CHECKING CODE")
            new.pairing_if_changed(code, "CHECKING CODE")
        elif scene == "error":
            old.message("Link unavailable", "Saved game kept. Press Home.")
            new.message_if_changed("Link unavailable", "Saved game kept. Press Home.")
        else:
            old_draw(old_controller, 1)
            new_draw(new_controller, 1)
        if hashlib.sha256(old_screen.raw).digest() != hashlib.sha256(new_screen.raw).digest():
            raise AssertionError("Framebuffer differs for " + scene)
        checked.append(scene)
    # A firmware screen may live in SRAM while a new image lives in PSRAM.
    # Distinct buffers are necessary for pixel equality, but would confound
    # timing. Rebind *both* the view and its logo renderer to the original
    # screen, then alternate measurement order on that same physical buffer.
    new.screen = old_screen
    new.logos.screen = old_screen
    new_screen = None
    samples = []
    for round_index in range(rounds):
        sample = {}
        for cursor in (False, True):
            order = (("before", old_draw), ("after", new_draw))
            if round_index % 2:
                order = (order[1], order[0])
            for label, draw in order:
                controller = controller_factory()
                draw(controller, 1)
                sample[label + ("Cursor" if cursor else "Idle") + "Microseconds"] = (
                    _timed(draw, controller, iterations, cursor))
        samples.append(sample)
    return {
        "framebufferScenesMatched": checked,
        "iterations": iterations,
        "rounds": rounds,
        "timingBuffer": "both renderers use the first screen; order alternates by round",
        "samples": samples,
        "beforeIdleMicroseconds": sum(s["beforeIdleMicroseconds"] for s in samples) / rounds,
        "afterIdleMicroseconds": sum(s["afterIdleMicroseconds"] for s in samples) / rounds,
        "beforeCursorMicroseconds": sum(s["beforeCursorMicroseconds"] for s in samples) / rounds,
        "afterCursorMicroseconds": sum(s["afterCursorMicroseconds"] for s in samples) / rounds,
        "scope": "RAM rendering only; excludes panel transfer, VSYNC, network and flash",
    }
