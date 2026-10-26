#!/usr/bin/env python3
"""Apply the pinned SQLite dynamic-runtime adaptations into build/generated."""
import hashlib
import json
from pathlib import Path

from fetch import ROOT, source_directory


def prepare():
    manifest = json.loads((ROOT / "sqlite-adaptations.json").read_text())
    source = (source_directory("sqlite") / "sqlite3.c").read_bytes()
    if hashlib.sha256(source).hexdigest() != manifest["source_sha256"]:
        raise ValueError("SQLite source checksum mismatch")
    text = source.decode()
    for change in manifest["changes"]:
        if text.count(change["before"]) != change["count"]:
            raise ValueError("SQLite adaptation no longer matches: " + change["before"])
        text = text.replace(change["before"], change["after"])
    encoded = text.encode()
    if hashlib.sha256(encoded).hexdigest() != manifest["adapted_sha256"]:
        raise ValueError("Adapted SQLite checksum mismatch")
    output = ROOT / "build/generated/sqlite3-adapted.inc"
    output.parent.mkdir(parents=True, exist_ok=True)
    if not output.exists() or output.read_bytes() != encoded:
        output.write_bytes(encoded)
    return output


if __name__ == "__main__":
    print(prepare())
