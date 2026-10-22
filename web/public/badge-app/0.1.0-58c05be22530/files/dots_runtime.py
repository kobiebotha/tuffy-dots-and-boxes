"""Load the PowerSync native module qualified for Badgeware 1.29.0."""
import binascii
import hashlib
import os
import sys

NATIVE_BYTES = 737049
NATIVE_SHA256 = "802223bc6d2f7daea4c04b975bf2c6208068ef968891557e726bd485d3efafa8"


def app_directory():
    directory = __file__.rsplit("/", 1)[0] if "/" in __file__ else "."
    if not directory.startswith("/"):
        directory = os.getcwd().rstrip("/") + "/" + directory
        if directory.endswith("/."):
            directory = directory[:-2]
    return directory


def load_native(heartbeat=None):
    implementation = sys.implementation
    uname = os.uname()
    if (implementation.name != "micropython"
            or implementation.version[:3] != (1, 29, 0)
            or getattr(implementation, "_mpy", None) != 7942):
        raise RuntimeError("Badgeware build is not qualified for this app")
    if (getattr(implementation, "_build", None) != "tufty"
            or uname.machine != "Pimoroni Tufty 2350 with RP2350"):
        raise RuntimeError("This app runs on the Tufty 2350")
    directory = app_directory()
    native_path = directory + "/badgegotchi_native.mpy"
    digest = hashlib.sha256()
    length = 0
    with open(native_path, "rb") as source:
        while True:
            block = source.read(4096)
            if not block:
                break
            digest.update(block)
            length += len(block)
            if heartbeat:
                heartbeat()
    actual = binascii.hexlify(digest.digest()).decode()
    if length != NATIVE_BYTES or actual != NATIVE_SHA256:
        raise RuntimeError("Native module integrity check failed")
    loaded = sys.modules.get("badgegotchi_native")
    if loaded is not None and getattr(loaded, "__file__", None) != native_path:
        raise RuntimeError("Another native module uses this app's module name")
    sys.path.insert(0, directory)
    try:
        import badgegotchi_native
    finally:
        sys.path.pop(0)
    if getattr(badgegotchi_native, "__file__", None) != native_path:
        raise RuntimeError("Native module did not load from this app")
    return badgegotchi_native
