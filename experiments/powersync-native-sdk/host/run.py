#!/usr/bin/env python3
"""Reproduce the isolated native SDK host proof; uses the existing local toolchain."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
EXPERIMENT = HERE.parent
ROOT = EXPERIMENT.parents[1]
RUST = ROOT.parent / "powerbadge/native/.tools/rustup/toolchains/1.96.0-aarch64-apple-darwin/bin"
RESULTS = HERE / "results"


def main():
    subprocess.run([sys.executable, HERE / "prepare.py"], check=True)
    RESULTS.mkdir(exist_ok=True)
    env = dict(os.environ, CARGO_HOME=str(EXPERIMENT / ".cargo-home"), RUSTC=str(RUST / "rustc"))
    command = [str(RUST / "cargo"), "build", "--manifest-path", str(HERE / "Cargo.toml"),
               "--release", "--locked", "--offline"]
    built = subprocess.run(command, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (RESULTS / "build.log").write_text("$ " + " ".join(command) + "\n" + built.stdout)
    if built.returncode:
        raise SystemExit(built.stdout)
    binary = HERE / "target/release/badge-native-sdk-proof"
    completed = subprocess.run([binary], env=dict(env, PROOF_ARENA_KIB="384"),
                               text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if completed.returncode:
        raise SystemExit(completed.stderr)
    (RESULTS / "run.log").write_text(completed.stderr)
    result = json.loads(completed.stdout)
    result["binary_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
    result["binary_bytes"] = binary.stat().st_size
    result["source_sha256"] = {str(p.relative_to(HERE)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in [HERE / "src/main.rs", HERE / "build.rs", HERE / "fixture.json", HERE / "Cargo.lock"]}
    (RESULTS / "proof.json").write_text(json.dumps(result, indent=2) + "\n")
    # A bounded-resource negative proof runs in a disposable child process.
    small = subprocess.run([binary], env=dict(env, PROOF_ARENA_KIB="256"),
                           text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (RESULTS / "arena-256.json").write_text(json.dumps({
        "configured_shared_arena_bytes": 262144, "exit_code": small.returncode,
        "output": small.stdout, "scope": "host SDK/core/test allocations; not a badge memory measurement",
    }, indent=2) + "\n")
    if small.returncode == 0 or "NOMEM" not in small.stdout:
        raise SystemExit("256 KiB case changed; reassess the recorded resource result")
    for name, command in (("symbols.log", ["nm", "-g", str(binary)]),
                          ("libraries.log", ["otool", "-L", str(binary)]),
                          ("sections.log", ["size", "-m", str(binary)]),
                          ("features.log", [str(RUST / "cargo"), "tree", "--manifest-path", str(HERE / "Cargo.toml"), "-e", "features", "--locked", "--offline"])):
        r = subprocess.run(command, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if r.returncode:
            raise SystemExit(r.stdout)
        (RESULTS / name).write_text("$ " + " ".join(command) + "\n" + r.stdout)
    print("Host proof passed:", len(result["checks"]), "checks; median", result["median_microseconds"], "us")
    print("Shared arena observed peak:", result["sqlite_shared_peak_bytes"], "bytes; 256 KiB child exit:", small.returncode)
    print("Evidence:", RESULTS)


if __name__ == "__main__":
    main()
