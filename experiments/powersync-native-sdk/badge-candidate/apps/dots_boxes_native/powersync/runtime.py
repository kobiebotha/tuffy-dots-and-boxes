"""Load only the full-SDK candidate native module with a generated identity pin."""
import binascii
import hashlib
import os
import sys

# package.py replaces these placeholders after the isolated native link.
NATIVE_BYTES = 1107030
NATIVE_SHA256 = "c5bd5fe3b43c482f3757e921b6a134d232d2882da026894e0e117171f0cccd06"


def load_native(heartbeat=None):
    implementation = sys.implementation
    if (implementation.name != "micropython" or implementation.version[:3] != (1, 29, 0)
            or getattr(implementation, "_mpy", None) != 7942
            or getattr(implementation, "_build", None) != "tufty"
            or os.uname().machine != "Pimoroni Tufty 2350 with RP2350"):
        raise RuntimeError("Native SDK candidate requires Badgeware Tufty 1.29.0")
    directory = __file__.rsplit("/", 1)[0] if "/" in __file__ else "."
    if not directory.startswith("/"):
        directory = os.getcwd().rstrip("/") + "/" + directory
        if directory.endswith("/."):
            directory = directory[:-2]
    path = directory + "/native_powersync_sdk.mpy"
    digest, length = hashlib.sha256(), 0
    with open(path, "rb") as source:
        while True:
            chunk = source.read(4096)
            if not chunk:
                break
            digest.update(chunk)
            length += len(chunk)
            if heartbeat:
                heartbeat()
    if length != NATIVE_BYTES or binascii.hexlify(digest.digest()).decode() != NATIVE_SHA256:
        raise RuntimeError("Native SDK candidate integrity check failed")
    previous = sys.modules.get("native_powersync_sdk")
    if previous is not None and getattr(previous, "__file__", None) != path:
        raise RuntimeError("Another native SDK module is already loaded")
    sys.path.insert(0, directory)
    try:
        import native_powersync_sdk
    finally:
        sys.path.pop(0)
    if getattr(native_powersync_sdk, "__file__", None) != path:
        raise RuntimeError("Native SDK candidate loaded from another path")
    if not all(hasattr(native_powersync_sdk, name) for name in ("sdk_start", "sdk_poll", "sdk_reply", "sdk_stop")):
        raise RuntimeError("Candidate lacks the full native SDK bridge")
    return native_powersync_sdk
