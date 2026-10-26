"""Load the PowerSync native module qualified for Badgeware 1.29.0."""
import binascii
import hashlib
import os
import sys

NATIVE_BYTES = 737192
NATIVE_SHA256 = "359c1f5e619357c20d777414165540b55d290af63994ddb44d0d3a210cbcb9d5"


def module_directory():
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
        raise RuntimeError("Badgeware build is not qualified for this SDK")
    if (getattr(implementation, "_build", None) != "tufty"
            or uname.machine != "Pimoroni Tufty 2350 with RP2350"):
        raise RuntimeError("This SDK build requires the Tufty 2350")
    directory = module_directory()
    native_path = directory + "/native_sqlite.mpy"
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
    loaded = sys.modules.get("native_sqlite")
    if loaded is not None and getattr(loaded, "__file__", None) != native_path:
        raise RuntimeError("Another native module uses this SDK's module name")
    sys.path.insert(0, directory)
    try:
        import native_sqlite
    finally:
        sys.path.pop(0)
    if getattr(native_sqlite, "__file__", None) != native_path:
        raise RuntimeError("Native module did not load from this SDK")
    return native_sqlite
