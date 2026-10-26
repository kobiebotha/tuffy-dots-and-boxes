#!/usr/bin/env python3
"""Build native_sqlite .mpy modules or a host test library from pinned sources."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from fetch import ROOT, fetch, load_pins, source_directory
from prepare import prepare

RUST_VERSION = "1.96.0"
ARM_TARGET = "thumbv8m.main-none-eabi"


def run(command, **kwargs):
    print("+ " + " ".join(map(str, command)), flush=True)
    subprocess.run(list(map(str, command)), check=True, **kwargs)


def rust_info():
    try:
        output = subprocess.check_output(["rustc", "+" + RUST_VERSION, "-vV"], text=True)
    except FileNotFoundError:
        raise SystemExit("Install rustup and Rust " + RUST_VERSION + " for the PowerSync variant.")
    return next(line[6:] for line in output.splitlines() if line.startswith("host: "))


def build_core(target, offline=False):
    environment = dict(os.environ)
    # Upstream's build.rs probes git. A source archive must never inherit the
    # consuming project's HEAD as its own PowerSync core version identifier.
    environment["GIT_CEILING_DIRECTORIES"] = str(ROOT / "vendor")
    key = "CARGO_TARGET_" + target.upper().replace("-", "_").replace(".", "_") + "_RUSTFLAGS"
    flags = "-C relocation-model=pic -C panic=abort"
    if target == ARM_TARGET:
        flags = "-C target-cpu=cortex-m33 " + flags
    environment[key] = flags
    command = ["cargo", "+" + RUST_VERSION, "build", "--manifest-path",
               source_directory("powersync") / "Cargo.toml", "--target", target,
               "--target-dir", ROOT / "build/core", "--package", "powersync_static",
               "--features", "static", "--no-default-features", "--release", "--locked"]
    if offline:
        command.append("--offline")
    run(command, cwd=source_directory("powersync"), env=environment)
    return ROOT / "build/core" / target / "release/libpowersync.a"


def metadata(artifact, variant, kind, core=None):
    pins = load_pins()
    inputs = sorted([ROOT / "Makefile", ROOT / "dependencies.json", ROOT / "sqlite-adaptations.json", ROOT / "requirements-build.txt",
                     *ROOT.glob("src/*"), *ROOT.glob("tools/*.py"),
                     ROOT / "tests/host_config.h", ROOT / "tests/host_shim.c"])
    source_hashes = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in inputs if path.is_file()}
    result = {
        "file": artifact.name, "variant": variant, "kind": kind,
        "bytes": artifact.stat().st_size,
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "sqlite": pins["sqlite"]["version"],
        "sourceSha256": hashlib.sha256(json.dumps(source_hashes, sort_keys=True).encode()).hexdigest(),
        "sourceFiles": source_hashes,
    }
    if kind == "mpy":
        if artifact.read_bytes()[:4] != bytes((77, 6, 31, 31)):
            raise ValueError("Native module does not match the Tufty .mpy ABI")
        result.update(mpy=7942, micropython=pins["micropython"]["version"],
                      micropythonCommit=pins["micropython"]["commit"],
                      board="tufty2350", floatAbi="softfp", arenaBytes=256 * 1024,
                      wsdBytes=16384, queryRowLimit=32, armGcc="13.2.1")
    if core:
        result.update(powersync=pins["powersync"]["version"], rust=RUST_VERSION,
                      powersyncCommit=pins["powersync"]["commit"],
                      powersyncArchiveSha256=hashlib.sha256(core.read_bytes()).hexdigest())
    (artifact.parent / "module.json").write_text(json.dumps(result, indent=2) + "\n")
    print("Built %s %s: %s (%d bytes, sha256 %s)" %
          (variant, kind, artifact.relative_to(ROOT), result["bytes"], result["sha256"]), flush=True)
    return result


def build_host(variant, offline):
    core = build_core(rust_info(), offline) if variant == "powersync" else None
    folder = ROOT / "build/host" / variant
    folder.mkdir(parents=True, exist_ok=True)
    filename = "native_sqlite_host.dylib" if sys.platform == "darwin" else "native_sqlite_host.so"
    output = folder / filename
    flags = ["-dynamiclib", "-D_DARWIN_C_SOURCE"] if sys.platform == "darwin" else [
        "-shared", "-fPIC", "-D_POSIX_C_SOURCE=200809L"]
    command = [os.environ.get("CC", "cc"), "-std=c11", "-O1", *flags,
               "-include", ROOT / "tests/host_config.h", "-I", ROOT / "src",
               "-I", source_directory("sqlite"), source_directory("sqlite") / "sqlite3.c",
               ROOT / "tests/host_shim.c"]
    if core:
        command.extend(["-DNATIVE_SQLITE_POWERSYNC", core])
    command.extend(["-lm", "-o", output])
    run(command)
    metadata(output, variant, "host", core)


def build_mpy(variant, offline):
    for module in ("elftools", "ar"):
        if importlib.util.find_spec(module) is None:
            raise SystemExit("Install requirements-build.txt into the active Python environment.")
    if shutil.which("arm-none-eabi-gcc") is None:
        raise SystemExit("Put Arm GNU Toolchain 13.2.Rel1 (arm-none-eabi-gcc) on PATH.")
    compiler = subprocess.check_output(["arm-none-eabi-gcc", "-dumpfullversion"], text=True).strip()
    if compiler != "13.2.1":
        raise SystemExit("The qualified build requires Arm GCC 13.2.1; found " + compiler)
    core = build_core(ARM_TARGET, offline) if variant == "powersync" else None
    prepare()
    # dynruntime.mk uses this interpreter for the linker and .mpy merger.
    command = ["make", "-C", ROOT, "VARIANT=" + variant, "BUILD=build/" + variant,
               "PYTHON=" + sys.executable, "all"]
    if core:
        command.append("CORE_ARCHIVE=" + str(core))
    # Archive indices preserve insertion order from the Python process that
    # created them. Do not inherit a cache made under a different hash seed.
    linker_cache = ROOT / ".mpy_ld_cache-armv7emsp"
    if linker_cache.is_symlink():
        raise ValueError("Refusing a symlinked native linker cache")
    if linker_cache.exists():
        shutil.rmtree(linker_cache)
    # The upstream Python linker iterates sets while choosing archive symbols.
    # Fix its hash seed so repeated links produce the same module bytes.
    run(command, env=dict(os.environ, PYTHONHASHSEED="0"))
    output = ROOT / "dist" / variant / "native_sqlite.mpy"
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "native_sqlite.mpy", output)
    metadata(output, variant, "mpy", core)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=("mpy", "host"))
    parser.add_argument("--variant", choices=("sqlite", "powersync", "all"), default="sqlite")
    parser.add_argument("--offline", action="store_true", help="also forbid Cargo dependency downloads")
    args = parser.parse_args()
    variants = ("sqlite", "powersync") if args.variant == "all" else (args.variant,)
    names = ["sqlite"]
    if args.target == "mpy":
        names.append("micropython")
    if "powersync" in variants:
        names.append("powersync")
    try:
        fetch(names, check=True)
        for variant in variants:
            (build_mpy if args.target == "mpy" else build_host)(variant, args.offline)
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
