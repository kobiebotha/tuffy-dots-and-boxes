"""Pair once, then play the room that PowerSync downloads. Home closes SQLite."""

import badgeware
import machine
import sys
from time import ticks_ms, ticks_diff, sleep_ms


# Badgeware may retain imported app modules between launcher entries. The test
# app must never accidentally reuse the other game's custom Python supervisor.
_app_root = "/system/apps/dots_boxes_native/"
_app_modules = ("dots_client", "dots_config", "dots_storage", "dots_sync", "dots_session",
                "dots_codec", "dots_pairing", "game", "controls", "view", "logos", "sqlite_shadow_vfs")
for _name in tuple(sys.modules):
    if _name in _app_modules or _name == "powersync" or _name.startswith("powersync."):
        _loaded = getattr(sys.modules[_name], "__file__", "")
        if not isinstance(_loaded, str) or not _loaded.startswith(_app_root):
            del sys.modules[_name]


exit_requested = False
home_pin = machine.Pin.board.BUTTON_HOME


def request_exit(_pin=None):
    # Badgeware's normal Home IRQ resets immediately. A flag keeps SQL and
    # filesystem cleanup in the foreground, even if Home arrives during SQL.
    global exit_requested
    exit_requested = True


home_pin.irq(trigger=machine.Pin.IRQ_FALLING, handler=request_exit)

from dots_client import Client
from dots_session import SessionFile
from dots_storage import Store
from view import View
from powersync.sync import SyncClient

if not getattr(SyncClient, "NATIVE_SDK", False):
    raise RuntimeError("Native SDK candidate imported another app's sync client")


badge.mode(HIRES | VSYNC)
watchdog = machine.WDT(timeout=16000)
view = View(screen, color, font)
view.message("Native SDK", "Opening Native SDK test.")
badgeware.display.update()
watchdog.feed()
store = Store(heartbeat=watchdog.feed)
client = None
failure = None
failure_title = "Could not open"

try:
    store.open()
    client = Client(store, SessionFile(store.root), heartbeat=watchdog.feed)
    failure = client.failure
except Exception as exc:
    print("DOTS_OPEN_ERROR", type(exc).__name__)
    sys.print_exception(exc)
    if isinstance(exc, OSError) and str(exc) == "Not enough free badge storage":
        failure_title = "Not enough storage"
        failure = "Existing games kept. Press Home."
    else:
        failure = "Could not start game."

last_tick = ticks_ms()
buttons = (
    (BUTTON_A, "A"),
    (BUTTON_B, "B"),
    (BUTTON_C, "C"),
    (BUTTON_UP, "UP"),
    (BUTTON_DOWN, "DOWN"),
)


def update():
    global last_tick, failure
    watchdog.feed()
    if exit_requested:
        return True
    now = ticks_ms()
    elapsed = ticks_diff(now, last_tick)
    last_tick = now
    if failure or (client is not None and client.failure):
        view.message_if_changed(failure_title, failure or client.failure)
        return
    held = [name for button, name in buttons if badge.held(button)]
    try:
        client.step(held, elapsed)
    except Exception as exc:
        print("DOTS_LINK_ERROR", type(exc).__name__)
        failure = "Saved game kept. Press Home."
        view.message("Link unavailable", failure)
        return
    if exit_requested:
        return True
    if client.mode == "pair":
        view.pairing_if_changed(client.entry.code, client.entry.status)
    else:
        view.draw_if_changed(client.controller, local_seat=client.records.record["seat"])


def on_exit():
    # launch() calls this again after the module body returns. Cleanup belongs
    # solely to the foreground finally block; this hook never re-enters SQL.
    return None


def closed_home(_pin):
    badgeware.reset()


previous_clear = badge.default_clear
try:
    # Qualified Badgeware clear() skips only its framebuffer clear for None;
    # display.update() retains that framebuffer. Keep managed polling/VSYNC in
    # run(), but let View redraw only when the visible state changes.
    badge.default_clear = None
    # An optional RAM-only USB qualification driver observes the real managed
    # app loop. Normal launcher runs have no such module or extra callback.
    _qualification = sys.modules.get("_badge_native_recovery")
    _run_update = update if _qualification is None else _qualification.wrap_update(update, client)
    run(_run_update)
finally:
    badge.default_clear = previous_clear
    try:
        if client is not None:
            client.close()
    finally:
        store.close()

if exit_requested:
    view.message("Returning home", "Release Home.")
    badgeware.display.update()
    # HOME is also BOOT, so resetting while it is held enters the bootloader.
    while not home_pin.value():
        watchdog.feed()
        sleep_ms(10)
    badgeware.reset()
else:
    home_pin.irq(trigger=machine.Pin.IRQ_FALLING, handler=closed_home)
