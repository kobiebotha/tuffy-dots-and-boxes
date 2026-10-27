"""Measure the extracted Python client with the existing checked memory sink.

This is a host baseline only. It deliberately excludes the native SDK, SQLite,
flash, TLS and Wi-Fi; its numbers are not a badge speedup comparison.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import sys


ROOT = Path(__file__).resolve().parents[3]
HARNESS = ROOT / "badge/tools/benchmark_download.py"
spec = importlib.util.spec_from_file_location("download_harness", HARNESS)
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)
paths = {"stream": ROOT / "badge/lib/powersync/stream.py",
         "sync": ROOT / "badge/lib/powersync/sync.py"}
sources = {name: path.read_text() for name, path in paths.items()}
room, expected, wire = harness.fixture()
result = {
    "scope": "Host checked memory sink; framing, JSON and scheduling only. No native SDK, SQLite, flash, TLS or Wi-Fi.",
    "python": sys.version,
    "host": platform.platform(),
    "source_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in paths.values()},
    "wire_sha256": hashlib.sha256(wire).hexdigest(),
    "wire_bytes": len(wire),
    "result": harness.run_case(sources, wire, expected, room, repetitions=21),
}
print(json.dumps(result, indent=2))
