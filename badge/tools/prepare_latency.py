"""Emit a self-contained RAM device script. Never opens a device or network."""
import argparse
import hashlib
import json
from pathlib import Path

TOOLS = Path(__file__).resolve().parent


def build_device_source(seconds=180, capacity=24, enabled=True, app_path=None):
    if type(seconds) is not int or not 10 <= seconds <= 240:
        raise ValueError("seconds must be 10..240")
    if type(capacity) is not int or not 1 <= capacity <= 64:
        raise ValueError("capacity must be 1..64 per direction")
    if type(enabled) is not bool:
        raise ValueError("enabled must be boolean")
    if app_path is not None and (not isinstance(app_path, str) or not app_path.startswith("/")
                                 or ".." in app_path.split("/")):
        raise ValueError("app_path must be an absolute device path")
    config = {"seconds": seconds, "capacity": capacity, "enabled": enabled, "app_path": app_path}
    probe_source = (TOOLS / "latency_probe.py").read_text()
    launcher = (TOOLS / "run_latency_device.py").read_text()
    digest = hashlib.sha256((probe_source + launcher).encode()).hexdigest()
    config["source_sha256"] = digest
    source = ("# Generated RAM-only latency harness; source SHA256 " + digest + "\n"
              + "_LATENCY_CONFIG = " + repr(config) + "\n"
              + "_latency_impl = {'__name__': 'latency_probe'}\n"
              + "exec(" + repr(probe_source) + ", _latency_impl)\n" + launcher)
    compile(source, "dots-latency-device.py", "exec")
    return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit-device", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=180)
    parser.add_argument("--capacity", type=int, default=24)
    parser.add_argument("--app-path", help="Installed app path; default checks /apps and /system/apps")
    parser.add_argument("--disabled", action="store_true", help="Emit uninstrumented comparison launcher")
    args = parser.parse_args()
    source = build_device_source(args.seconds, args.capacity, not args.disabled, args.app_path)
    args.emit_device.write_text(source)
    print(json.dumps({"prepared": str(args.emit_device.resolve()), "bytes": len(source.encode()),
                      "instrumented": not args.disabled, "hardware_tested": False}))


if __name__ == "__main__":
    main()
