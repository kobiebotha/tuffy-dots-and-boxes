#!/usr/bin/env python3
"""Execute a local, RAM-only benchmark payload over the badge USB REPL.

No app installation or deployment. The payload explicitly controls any scratch
SQLite writes. Output must contain timings only, never session credentials.
"""
import argparse
import json
import time
from pathlib import Path
from mpremote.transport_serial import SerialTransport


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("payload", type=Path)
    parser.add_argument("--port", default="/dev/cu.usbmodem1101")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()
    for attempt in range(5):
        try:
            device = SerialTransport(args.port)
            break
        except Exception:
            if attempt == 4:
                raise
            time.sleep(1)
    captured = bytearray()

    def consume(data):
        captured.extend(data)
        print(data.decode(errors="replace").replace("\r\n", "\n"), end="", flush=True)

    try:
        device.enter_raw_repl(soft_reset=True)
        _, error = device.exec_raw(args.payload.read_text(), timeout=args.timeout, data_consumer=consume)
        if error:
            raise RuntimeError(error.decode(errors="replace"))
    finally:
        if args.output:
            args.output.write_text(captured.decode(errors="replace").replace("\r\n", "\n"))
        try:
            device.exit_raw_repl()
        finally:
            device.close()


if __name__ == "__main__":
    main()
