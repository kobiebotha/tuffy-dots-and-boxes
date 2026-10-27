#!/usr/bin/env python3
"""Reproduce the full SDK ARM compilation barrier without changing its checkout.

Pass --online only when downloading pinned crates is authorized. Build outputs,
cache writes and logs stay within experiments/powersync-native-sdk.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess

ARM = Path(__file__).resolve().parent
EXPERIMENT = ARM.parent
REPO = EXPERIMENT.parents[1]
SDK = REPO.parent / "powersync-native"
TOOLS = REPO.parent / "powerbadge/native/.tools"
RUST_BIN = TOOLS / "rustup/toolchains/1.96.0-aarch64-apple-darwin/bin"
SDK_REVISION = "96a94f3e6e9442c4181d793f95241c2ee9cab4d8"
COMMAND_EXIT_CODES = {}


def capture(name, command, environment):
    result = subprocess.run(command, env=environment, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (ARM / name).write_text("$ " + " ".join(map(str, command)) + "\n" + result.stdout)
    COMMAND_EXIT_CODES[name] = result.returncode
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--online", action="store_true")
    args = parser.parse_args()
    environment = dict(os.environ)
    environment["CARGO_HOME"] = str(EXPERIMENT / ".cargo-home")
    environment["RUSTC"] = str(RUST_BIN / "rustc")
    environment["CARGO_BUILD_JOBS"] = "1"
    environment["CARGO_TARGET_THUMBV8M_MAIN_NONE_EABI_RUSTFLAGS"] = (
        "-C target-cpu=cortex-m33 -C relocation-model=pic -C panic=abort")
    base = [str(RUST_BIN / "cargo")]
    select = ["--manifest-path", str(SDK / "Cargo.toml"), "--package", "powersync",
              "--no-default-features", "--target", "thumbv8m.main-none-eabi", "--locked"]
    offline = [] if args.online else ["--offline"]
    result = capture("sdk-check.log", base + ["check"] + select + [
        "--target-dir", str(ARM / "target")] + offline, environment)
    capture("features.log", base + ["tree"] + select + ["-e", "features,no-dev"] + offline, environment)
    capture("parking-chain.log", base + ["tree"] + select + ["-i", "parking", "-e", "features,no-dev"] + offline, environment)
    capture("getrandom-chain.log", base + ["tree"] + select + ["-i", "getrandom", "-e", "features,no-dev"] + offline, environment)
    probe = capture("std-probe.log", [str(RUST_BIN / "rustc"), "--crate-type", "lib",
        "--target", "thumbv8m.main-none-eabi", "-C", "target-cpu=cortex-m33",
        "-C", "relocation-model=pic", "-C", "panic=abort", str(ARM / "std-probe.rs"),
        "-o", str(ARM / "std-probe.rlib")], environment)
    capture("target-cfg.log", [str(RUST_BIN / "rustc"), "--print", "cfg", "--target",
        "thumbv8m.main-none-eabi"], environment)
    leaf = capture("async-leaf-build.log", base + ["build", "--manifest-path",
        str(ARM / "async-leaf/Cargo.toml"), "--target", "thumbv8m.main-none-eabi",
        "--target-dir", str(ARM / "async-leaf-target"), "--release", "--locked"] + offline, environment)
    validation_errors = []
    if leaf.returncode == 0:
        arm_bin = TOOLS / "arm-gnu-toolchain-13.2.Rel1-darwin-arm64-arm-none-eabi/bin"
        archive = ARM / "async-leaf-target/thumbv8m.main-none-eabi/release/libbadge_async_leaf_check.rlib"
        members = capture("async-leaf-members.log", [str(arm_bin / "arm-none-eabi-ar"),
            "t", str(archive)], environment)
        member = next((name for name in members.stdout.splitlines() if name.endswith(".o")), None)
        if members.returncode == 0 and member is not None:
            obj = ARM / "async-leaf.o"
            extract = subprocess.run([str(arm_bin / "arm-none-eabi-ar"), "p", str(archive), member],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
            COMMAND_EXIT_CODES["async-leaf-extract"] = extract.returncode
            (ARM / "async-leaf-extract.log").write_text(extract.stderr.decode(errors="replace"))
            if extract.returncode == 0:
                obj.write_bytes(extract.stdout)
                for name, flags in [("attributes", ["-A"]), ("sections", ["-WS"]),
                                    ("relocations", ["-Wr"]), ("symbols", ["-Ws"])]:
                    capture("async-leaf-" + name + ".log", [str(arm_bin / "arm-none-eabi-readelf"),
                        *flags, str(obj)], environment)
                capture("async-leaf-size.log", [str(arm_bin / "arm-none-eabi-size"), str(obj)], environment)
        else:
            validation_errors.append("Could not identify the async leaf object in its archive")
    pin = subprocess.check_output(["git", "-C", str(SDK), "rev-parse", "HEAD"], text=True).strip()
    version = subprocess.check_output([str(RUST_BIN / "rustc"), "-vV"], text=True)
    if pin != SDK_REVISION:
        validation_errors.append("SDK revision changed; reassess the new source before claiming reproduction")
    if result.returncode != 101 or not all(marker in result.stdout for marker in (
            "error[E0463]", "can't find crate for `std`", "parking")):
        validation_errors.append("SDK check did not reproduce the expected E0463/std/parking compiler failure")
    if probe.returncode == 0 or not all(marker in probe.stdout for marker in (
            "error[E0463]", "can't find crate for `std`")):
        validation_errors.append("Independent std probe did not reproduce the expected E0463/std failure")
    for name, code in COMMAND_EXIT_CODES.items():
        if name not in ("sdk-check.log", "std-probe.log") and code != 0:
            validation_errors.append("Required command failed: %s (exit %s)" % (name, code))
    (ARM / "build-result.json").write_text(json.dumps({
        "sdkRevision": pin, "sdkVersion": "0.1.1", "rustc": version,
        "target": "thumbv8m.main-none-eabi", "cpu": "cortex-m33",
        "relocationModel": "pic", "panic": "abort", "defaultFeatures": False,
        "checkExitCode": result.returncode, "loadableModuleProduced": False,
        "stdProbeExitCode": probe.returncode, "asyncLeafBuildExitCode": leaf.returncode,
        "asyncLeafScope": "Only selected no_std dependencies, not SDK orchestration or a final link",
        "commandExitCodes": COMMAND_EXIT_CODES,
        "validatedExpectedResults": not validation_errors,
        "validationErrors": validation_errors,
    }, indent=2) + "\n")
    print(result.stdout[:4000])
    print("Recorded ARM result and dependency trees in", ARM)
    if validation_errors:
        raise SystemExit("ARM reproduction validation failed:\n- " + "\n- ".join(validation_errors))
    print("Validated expected SDK/std failures and successful dependency/async-leaf inspection commands.")


if __name__ == "__main__":
    main()
