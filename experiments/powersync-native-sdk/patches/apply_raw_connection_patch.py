#!/usr/bin/env python3
"""Apply the experiment-only raw-connection API patch to a copied crate.

Usage: python3 patches/apply_raw_connection_patch.py host/vendor/powersync
The target must be inside this experiment. The sibling checkout is never edited.
"""
from pathlib import Path
import subprocess
import sys


def main():
    experiment = Path(__file__).resolve().parents[1]
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    target = Path(sys.argv[1]).resolve()
    if not target.is_relative_to(experiment) or target == experiment:
        raise SystemExit("Target must be a copied crate inside " + str(experiment))
    if not (target / "src/lib.rs").is_file() or not (target / "src/db/pool.rs").is_file():
        raise SystemExit("Target is not a copied powersync crate: " + str(target))
    patch = Path(__file__).with_name("0001-expose-raw-connection.patch").resolve()
    command = ["patch", "--batch", "--forward", "-p2", "-i", str(patch)]
    subprocess.run(command + ["--dry-run"], cwd=target, check=True)
    subprocess.run(command, cwd=target, check=True)


if __name__ == "__main__":
    main()
