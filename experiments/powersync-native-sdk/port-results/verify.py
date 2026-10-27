#!/usr/bin/env python3
"""Reproduce the current SDK no_std Cortex-M33 build matrix, offline and locked."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent
EXPERIMENT = HERE.parent
APP = EXPERIMENT.parents[1]
SDK = APP.parent / "powersync-native"
RUST = APP.parent / "powerbadge/native/.tools/rustup/toolchains/1.96.0-aarch64-apple-darwin/bin"


def capture(name, command, env):
    result = subprocess.run(command, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (HERE / name).write_text("$ " + " ".join(map(str, command)) + "\n" + result.stdout)
    if result.returncode:
        raise SystemExit(result.stdout)
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--desktop-tests", action="store_true", help="also run the SDK tests with default features and Embassy")
    parser.add_argument("--desktop-runtimes", action="store_true", help="also compile all optional desktop runtime features")
    args = parser.parse_args()
    env = dict(os.environ, CARGO_HOME=str(EXPERIMENT / ".cargo-home"),
        RUSTC=str(RUST / "rustc"), RUSTDOC=str(RUST / "rustdoc"),
        CARGO_TARGET_THUMBV8M_MAIN_NONE_EABI_RUSTFLAGS="-C target-cpu=cortex-m33 -C relocation-model=pic -C panic=abort")
    base = [str(RUST / "cargo"), "build", "--manifest-path", str(SDK / "Cargo.toml"),
        "-p", "powersync", "--no-default-features", "--target", "thumbv8m.main-none-eabi",
        "--target-dir", str(EXPERIMENT / "port-build/arm"), "--offline", "--locked", "--release"]
    builds = {}
    for label, extra in [("no-std", []), ("no-std-embassy", ["--features", "embassy"])]:
        capture(label + ".log", base + extra, env)
        builds[label] = {"exit_code": 0}
    graph = capture("arm-features.log", [str(RUST / "cargo"), "tree", "--manifest-path", str(SDK / "Cargo.toml"),
        "-p", "powersync", "--no-default-features", "--features", "embassy", "--target", "thumbv8m.main-none-eabi",
        "--offline", "--locked", "-e", "features,no-dev"], env)
    for forbidden in ["parking v", "async-broadcast v", "getrandom v", "tokio v", "reqwest v", "rusqlite v"]:
        if forbidden in graph:
            raise SystemExit("Desktop dependency leaked into target graph: " + forbidden)
    if args.desktop_tests:
        capture("desktop-tests.log", [str(RUST / "cargo"), "test", "--manifest-path", str(SDK / "Cargo.toml"),
            "-p", "powersync", "--features", "embassy", "--target-dir", str(EXPERIMENT / "port-build/host"),
            "--offline", "--locked"], env)
    if args.desktop_runtimes:
        capture("all-features.log", [str(RUST / "cargo"), "check", "--manifest-path", str(SDK / "Cargo.toml"),
            "-p", "powersync", "--all-features", "--target-dir", str(EXPERIMENT / "port-build/host"),
            "--offline", "--locked"], env)
    files = [SDK / "powersync/Cargo.toml", SDK / "Cargo.lock", *sorted((SDK / "powersync/src").rglob("*.rs"))]
    source = {str(p.relative_to(SDK)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    (HERE / "build-result.json").write_text(json.dumps({
        "sdk_base_revision": subprocess.check_output(["git", "-C", str(SDK), "rev-parse", "HEAD"], text=True).strip(),
        "source_state": "modified SDK working tree", "source_sha256": source,
        "rustc": subprocess.check_output([str(RUST / "rustc"), "-vV"], text=True),
        "target": "thumbv8m.main-none-eabi", "cpu": "cortex-m33", "relocation": "pic", "panic": "abort",
        "release_builds": builds, "loadable_badge_module_produced": False,
    }, indent=2) + "\n")
    print("Full SDK no_std release builds passed, with and without Embassy; target dependency graph verified.")


if __name__ == "__main__":
    main()
