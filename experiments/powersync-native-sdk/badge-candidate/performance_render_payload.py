"""RAM-only on-badge renderer/panel timing; prepend BENCH_VARIANT.

Run after a fresh soft reset through performance_device.py. The qualified
Badgeware boot supplies badge, screen, color, font, HIRES and VSYNC builtins.
Only selected installed game/controls/view modules are imported; no Store,
session, sync or network task is constructed. Both variants use the existing
physical screen buffer and identical partially played board/cursor scenes.
"""
import gc
import hashlib
import binascii
import json
import sys
import time
import machine
import badgeware

if "BENCH_VARIANT" not in globals():
    BENCH_VARIANT = "original"
if "BENCH_RENDER_ITERATIONS" not in globals():
    BENCH_RENDER_ITERATIONS = 30
if "BENCH_RENDER_ROUNDS" not in globals():
    BENCH_RENDER_ROUNDS = 3
assert BENCH_VARIANT in ("original", "native")
assert 1 <= BENCH_RENDER_ITERATIONS <= 120
assert 1 <= BENCH_RENDER_ROUNDS <= 10

_render_watchdog = machine.WDT(timeout=16000)
_render_watchdog.feed()
_app_path = "/system/apps/" + ("dots_boxes_native" if BENCH_VARIANT == "native" else "dots_boxes")
sys.path.insert(0, _app_path)
from game import Game
from controls import Controller
from view import View


def _micros():
    return time.ticks_us()


def _duration(start):
    return time.ticks_diff(_micros(), start)


def _summary(values):
    ordered = sorted(values)
    count = len(ordered)
    return {"count": count, "total_us": sum(ordered), "min_us": ordered[0],
            "median_us": ordered[count // 2],
            "p95_us": ordered[min(count - 1, (count * 95 + 99) // 100 - 1)],
            "max_us": ordered[-1], "mean_us": sum(ordered) / count}


def _fingerprint():
    return binascii.hexlify(hashlib.sha256(screen.raw).digest()).decode()


def _controller():
    game = Game(5, 4)
    # An unfinished, repeatable board with edges and completed boxes.
    for col in range(5):
        game.claim("h", 0, col)
        game.claim("h", 1, col)
    for col in range(6):
        game.claim("v", 0, col)
    controller = Controller(game)
    controller.message_ms = 0
    controller.status_line = "SYNCED"
    return controller


badge.mode(HIRES | VSYNC)
_previous_clear = badge.default_clear
badge.default_clear = None
_results = []
try:
    _view = View(screen, color, font)
    for _round in range(BENCH_RENDER_ROUNDS):
        # Alternating order reduces clock/thermal/order bias.
        _order = ("idle", "cursor") if _round % 2 == 0 else ("cursor", "idle")
        for _scene in _order:
            _control = _controller()
            _view._state = None
            for _warmup in range(3):
                _view.draw_if_changed(_control, local_seat=1)
                badgeware.display.update()
                _render_watchdog.feed()
            _initial_sha = _fingerprint()
            _draws, _panels, _redrawn = [], [], 0
            gc.collect()
            for _iteration in range(BENCH_RENDER_ITERATIONS):
                _render_watchdog.feed()
                if _scene == "cursor":
                    _control.col = (_control.col + 1) % (_control.game.cols + 1)
                _start = _micros()
                _changed = _view.draw_if_changed(_control, local_seat=1)
                _draw_us = _duration(_start)
                _start = _micros()
                badgeware.display.update()
                _panel_us = _duration(_start)
                _draws.append(_draw_us)
                _panels.append(_panel_us)
                _redrawn += bool(_changed)
            assert _redrawn == (BENCH_RENDER_ITERATIONS if _scene == "cursor" else 0)
            _final_sha = _fingerprint()
            if _scene == "idle":
                assert _initial_sha == _final_sha, "Idle framebuffer changed"
            _results.append({"round": _round, "scene": _scene,
                             "redraw_count": _redrawn,
                             "draw_if_changed": _summary(_draws),
                             "display_update_panel_and_vsync": _summary(_panels),
                             "initial_framebuffer_sha256": _initial_sha,
                             "final_framebuffer_sha256": _final_sha})
    print("PERFORMANCE_RENDER_RESULT", json.dumps({
        "variant": BENCH_VARIANT, "iterations": BENCH_RENDER_ITERATIONS,
        "rounds": BENCH_RENDER_ROUNDS, "mode": "HIRES | VSYNC",
        "screen": "same physical Badgeware screen buffer",
        "board": {"cols": 5, "rows": 4, "moves": 16, "local_seat": 1,
                  "horizontal_rows": [0, 1], "vertical_rows": [0], "status": "SYNCED"},
        "samples": _results,
        "scope": "Renderer CPU and separate display.update panel+VSYNC. No SQLite, TLS or sync. Panel transfer and VSYNC are inclusive, not separately attributable without firmware hooks."}))
finally:
    badge.default_clear = _previous_clear
    _render_watchdog.feed()
