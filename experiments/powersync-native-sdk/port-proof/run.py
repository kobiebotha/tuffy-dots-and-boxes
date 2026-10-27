#!/usr/bin/env python3
"""Build/run the real no_std SDK + Embassy proof against the working SDK checkout."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
EXPERIMENT = HERE.parent
ROOT = EXPERIMENT.parents[1]
SDK = ROOT.parent / "powersync-native"
RUST = ROOT.parent / "powerbadge/native/.tools/rustup/toolchains/1.96.0-aarch64-apple-darwin/bin"
RESULTS = HERE / "results"


def run_logged(name, command, env):
    result = subprocess.run(command, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (RESULTS / name).write_text("$ " + " ".join(map(str, command)) + "\n" + result.stdout)
    if result.returncode:
        raise SystemExit(result.stdout)
    return result.stdout


def main():
    subprocess.run([sys.executable, HERE / "prepare.py"], check=True)
    RESULTS.mkdir(exist_ok=True)
    env = dict(os.environ, CARGO_HOME=str(EXPERIMENT / ".cargo-home"), RUSTC=str(RUST / "rustc"))
    run_logged("build.log", [str(RUST / "cargo"), "build", "--manifest-path", str(HERE / "Cargo.toml"),
               "--release", "--locked", "--offline"], env)
    binary = HERE / "target/release/badge-native-sdk-embassy-proof"
    completed = subprocess.run([binary], env=dict(env, PROOF_ARENA_KIB="384"),
                               text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (RESULTS / "run.log").write_text(completed.stderr)
    if completed.returncode:
        raise SystemExit(completed.stderr)
    result = json.loads(completed.stdout)
    result["binary_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
    result["binary_bytes"] = binary.stat().st_size
    result["source_sha256"] = {str(p.relative_to(HERE)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in [HERE / "src/main.rs", HERE / "build.rs", HERE / "fixture.json", HERE / "Cargo.lock"]}
    (RESULTS / "proof.json").write_text(json.dumps(result, indent=2) + "\n")
    small = subprocess.run([binary], env=dict(env, PROOF_ARENA_KIB="256"),
                           text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (RESULTS / "arena-256.json").write_text(json.dumps({
        "configured_shared_arena_bytes": 262144, "exit_code": small.returncode,
        "output": small.stdout, "scope": "host SDK/core/test allocations; not a badge memory measurement",
    }, indent=2) + "\n")
    if small.returncode != 0 and "NOMEM" not in small.stdout:
        raise SystemExit("Unexpected 256 KiB failure; inspect arena-256.json")
    for name, command in (("symbols.log", ["nm", "-g", str(binary)]),
                          ("libraries.log", ["otool", "-L", str(binary)]),
                          ("sections.log", ["size", "-m", str(binary)]),
                          ("features.log", [str(RUST / "cargo"), "tree", "--manifest-path", str(HERE / "Cargo.toml"), "-e", "features", "--locked", "--offline"]),
                          ("sdk-features.log", [str(RUST / "cargo"), "tree", "--manifest-path", str(HERE / "Cargo.toml"), "-i", "powersync", "-e", "features", "--locked", "--offline"])):
        run_logged(name, command, env)
    features = (RESULTS / "sdk-features.log").read_text()
    if 'powersync feature "std"' in features or 'powersync feature "default"' in features:
        raise SystemExit("Host proof accidentally enabled SDK std/default feature")
    fixture = json.loads((HERE / "fixture.json").read_text())
    for filename, digest in fixture["sdk_source_sha256"].items():
        if hashlib.sha256((SDK / filename).read_bytes()).hexdigest() != digest:
            raise SystemExit("SDK changed during proof: " + filename + "; rerun for consistent evidence")
    print("no_std SDK + Embassy proof passed:", len(result["checks"]), "checks; median", result["median_microseconds"], "us")
    print("Shared arena observed peak:", result["sqlite_shared_peak_bytes"], "bytes; 256 KiB child exit:", small.returncode)
    print("SDK std/default features verified absent. Evidence:", RESULTS)


if __name__ == "__main__":
    main()
