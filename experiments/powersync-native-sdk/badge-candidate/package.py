#!/usr/bin/env python3
"""Pin and package the isolated full-native-SDK Badgeware candidate.

Writes only this experiment's staging tree, ZIP and manifest. Installation is
separate so final bytes can be verified before copying to the physical badge.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

HERE = Path(__file__).resolve().parent
APP_ID = "dots_boxes_native"
APP = HERE / "apps" / APP_ID
DIST = HERE.parent / "badge-module/dist"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path, default=DIST / "native_powersync_sdk.mpy")
    parser.add_argument("--metadata", type=Path, default=DIST / "module.json")
    args = parser.parse_args()
    native = args.native.read_bytes()
    metadata = json.loads(args.metadata.read_text())
    if (metadata.get("kind") != "native-powersync-sdk-candidate"
            or metadata.get("mpy") != 7942 or metadata.get("board") != "tufty2350"
            or metadata.get("firmwareReplaced") is not False):
        raise SystemExit("Native metadata does not identify this full-SDK Badgeware candidate")
    if (metadata.get("atomicLowering") != "LLVM lower-atomic"
            or metadata.get("exclusiveInstructionCount") != 0
            or metadata.get("requiresSingleForegroundOwner") is not True
            or metadata.get("concurrentSdkHeapAccessAllowed") is not False):
        raise SystemExit("Native candidate is not qualified for the foreground PSRAM binding")
    if (metadata.get("bytes") != len(native) or metadata.get("sha256") != digest(native)
            or native[:4] != bytes((77, 6, 31, 31))):
        raise SystemExit("Native candidate bytes do not match the reviewed link")
    destination = APP / "powersync/native_powersync_sdk.mpy"
    destination.write_bytes(native)
    runtime = APP / "powersync/runtime.py"
    text = runtime.read_text()
    text = re.sub(r"^NATIVE_BYTES = .*", "NATIVE_BYTES = " + str(len(native)), text, flags=re.M)
    text = re.sub(r'^NATIVE_SHA256 = .*', 'NATIVE_SHA256 = "' + digest(native) + '"', text, flags=re.M)
    runtime.write_text(text)
    files = []
    for path in sorted(APP.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.is_symlink():
            raise SystemExit("Refusing symlinked candidate content: " + str(path))
        relative = path.relative_to(APP).as_posix()
        if relative in ("powersync/stream.py", "powersync/native_sqlite.mpy"):
            raise SystemExit("Custom client artifact present in native SDK candidate: " + relative)
        content = path.read_bytes()
        files.append({"path": relative, "bytes": len(content), "sha256": digest(content)})
    manifest = {"schemaVersion": 1, "appId": APP_ID, "name": "Dots & Boxes Native SDK",
                "stateRoot": "/state/dots_boxes_native", "native": metadata,
                "manualHardwareQualificationPending": True, "files": files}
    (HERE / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    output = HERE / "dots-boxes-native-sdk.zip"
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for item in files:
            entry = zipfile.ZipInfo("apps/" + APP_ID + "/" + item["path"], (2026, 10, 26, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, (APP / item["path"]).read_bytes())
    print("Packaged", len(files), "files:", output)
    print("Native", len(native), digest(native))


if __name__ == "__main__":
    main()
