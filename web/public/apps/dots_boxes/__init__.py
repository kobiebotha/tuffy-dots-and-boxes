"""Pair once, then play the room that PowerSync downloads. Home closes SQLite."""

import badgeware
import machine
from time import ticks_ms, ticks_diff, sleep_ms


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


badge.mode(HIRES | VSYNC)
watchdog = machine.WDT(timeout=16000)
view = View(screen, color, font)
view.message("Getting ready", "Opening the saved game.")
badgeware.display.update()
watchdog.feed()
store = Store(heartbeat=watchdog.feed)
client = None
failure = None

try:
    store.open()
    client = Client(store, SessionFile(store.root), heartbeat=watchdog.feed)
    failure = client.failure
except Exception as exc:
    print("DOTS_OPEN_ERROR", type(exc).__name__)
    failure = "Could not open the saved game."

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
        view.message("Could not open", failure or client.failure)
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
        view.pairing(client.entry.code, client.entry.status)
    else:
        view.draw(client.controller)


def on_exit():
    # launch() calls this again after the module body returns. Cleanup belongs
    # solely to the foreground finally block; this hook never re-enters SQL.
    return None


def closed_home(_pin):
    badgeware.reset()


try:
    run(update)
finally:
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
