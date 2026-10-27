#!/usr/bin/env python3
"""Generate a RAM-only, deterministic comparison for the installed badge apps.

The generated script must run after a fresh soft reset. It uses actual installed
Python/native code and either SQLite :memory: or the production LittleFS VFS.
No network is opened. Both variants receive identical checked raw room/move
records in identical 2,048-byte fragments. Flash mode overwrites only the two
SQLite files in the explicitly selected directory; session files are untouched.
"""
import argparse
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURE = HERE.parent / "host/fixture.json"
PAYLOAD = HERE / "performance_replay_payload.py"


def build_source(variant, storage="memory", root="/state/dots_boxes_benchmark",
                 pace_ms=0, repetitions=1, scenario="cold", keep_stream=False):
    if variant not in ("original", "native") or storage not in ("memory", "flash"):
        raise ValueError("Unsupported benchmark variant/storage")
    if scenario not in ("cold", "live", "warm_upload") or not 0 <= pace_ms <= 1000 or not 1 <= repetitions <= 20:
        raise ValueError("Unsupported benchmark scenario/pace/repetitions")
    if keep_stream and (variant != "native" or scenario != "warm_upload"):
        raise ValueError("keep_stream is an isolated native warm_upload control")
    fixture = json.loads(FIXTURE.read_text())
    config = {"variant": variant, "storage": storage, "root": root,
              "pace_ms": pace_ms, "repetitions": repetitions, "scenario": scenario,
              "keep_stream": bool(keep_stream),
              "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest()}
    return ("BENCH_CONFIG=" + repr(config) + "\nBENCH_FIXTURE=" + repr(fixture)
            + "\n" + PAYLOAD.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=("original", "native"), required=True)
    parser.add_argument("--storage", choices=("memory", "flash"), default="memory")
    parser.add_argument("--root", default="/state/dots_boxes_benchmark")
    parser.add_argument("--pace-ms", type=int, default=0)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--scenario", choices=("cold", "live", "warm_upload"), default="cold")
    parser.add_argument("--keep-stream", action="store_true", help="RAM-only causal control; native warm_upload only")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = build_source(args.variant, args.storage, args.root, args.pace_ms,
                          args.repetitions, args.scenario, args.keep_stream)
    args.output.write_text(source)
    print(args.output)


if __name__ == "__main__":
    main()
