"""Stage a deterministic, verified browser release. Never write to a USB drive."""
import argparse
import hashlib
import io
import json
import shutil
import sys
import zipfile
from pathlib import Path

BADGE = Path(__file__).resolve().parents[1]
APP = BADGE / "app" / "dots_boxes"
REPO = BADGE.parent
PUBLIC = REPO / "web" / "public"
APP_ID = "dots_boxes"
VERSION = "0.1.0"
NATIVE_BYTES = 737049
NATIVE_SHA256 = "802223bc6d2f7daea4c04b975bf2c6208068ef968891557e726bd485d3efafa8"
SOURCE_FILES = (
    "__init__.py",
    "game.py",
    "controls.py",
    "view.py",
    "logos.py",
    "icon.png",
    "dots_config.py",
    "dots_http.py",
    "dots_runtime.py",
    "dots_stream.py",
    "dots_storage.py",
    "dots_codec.py",
    "dots_session.py",
    "dots_sync.py",
    "dots_client.py",
    "dots_pairing.py",
    "ca/sectigo-r46.der",
    "badgegotchi_native.mpy",
)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def refuse_mounted_destination(path):
    for candidate in (path, path.resolve()):
        text = str(candidate)
        for marker in ("/Volumes/", "/media/", "/mnt/", "/run/media/"):
            if marker in text:
                raise SystemExit("Refusing to write onto a mounted drive: " + text)
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise SystemExit("Refusing a symlinked release destination: " + str(path))


def collect():
    files = {}
    for name in SOURCE_FILES:
        path = APP / name
        ancestors = [APP]
        for part in Path(name).parts:
            ancestors.append(ancestors[-1] / part)
        if any(part.is_symlink() for part in ancestors) or not path.is_file():
            raise SystemExit("Missing or symlinked app file: " + name)
        files[name] = path.read_bytes()
    native = files["badgegotchi_native.mpy"]
    if len(native) != NATIVE_BYTES or sha(native) != NATIVE_SHA256 or native[:4] != bytes((77, 6, 31, 31)):
        raise SystemExit("Native module does not match the reviewed build")
    identity = files["dots_runtime.py"].decode("utf-8")
    if NATIVE_SHA256 not in identity or str(NATIVE_BYTES) not in identity:
        raise SystemExit("Runtime pin does not match the native module")
    return files


def make_zip(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(files.items()):
            entry = zipfile.ZipInfo("apps/" + APP_ID + "/" + name, (2026, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data)
    return output.getvalue()


def release_outputs(files):
    """Return every served byte, using an immutable URL derived from app contents."""
    identity = [(name, len(data), sha(data)) for name, data in sorted(files.items())]
    fingerprint = sha(json.dumps(identity, separators=(",", ":")).encode())[:12]
    version = VERSION + "-" + fingerprint
    base = "badge-app/" + version
    payload = make_zip(files)
    release = {
        "version": version,
        "appId": APP_ID,
        "name": "Dots & Boxes",
        "board": "tufty2350",
        "runtimeMpy": 7942,
        "description": "Dots & Boxes for Tufty 2350 running Badgeware.",
        "zipUrl": "/" + base + "/app.zip",
        "zipSha256": sha(payload),
        "zipBytes": len(payload),
        "files": [
            {"path": name, "url": "/" + base + "/files/" + name,
             "bytes": len(data), "sha256": sha(data)}
            for name, data in sorted(files.items())
        ],
    }
    manifest = json.dumps({"schemaVersion": 1, "release": release}, indent=2) + "\n"
    outputs = {
        "dots-boxes.zip": payload,
        "badge-app/manifest.json": manifest.encode("utf-8"),
        base + "/app.zip": payload,
    }
    for name, data in files.items():
        outputs[base + "/files/" + name] = data
        # Keep the original public app URLs consistent with the release and ZIP.
        outputs["apps/" + APP_ID + "/" + name] = data
    return release, outputs


def staged_files(root):
    if root.is_symlink():
        raise SystemExit("Symlinked staged release: " + str(root))
    if not root.exists():
        return set()
    if not root.is_dir():
        raise SystemExit("Staged release is not a directory: " + str(root))
    found = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise SystemExit("Symlinked staged release: " + str(path))
        if path.is_file():
            found.add(path.relative_to(PUBLIC).as_posix())
        elif not path.is_dir():
            raise SystemExit("Unexpected staged release entry: " + str(path))
    return found


def check_outputs(outputs):
    for name, expected in outputs.items():
        path = PUBLIC / name
        refuse_mounted_destination(path)
        if not path.is_file() or path.read_bytes() != expected:
            raise SystemExit("Staged release differs from source: " + name +
                             ". Run python3 badge/tools/package.py.")
    staged = staged_files(PUBLIC / "badge-app") | staged_files(PUBLIC / "apps" / APP_ID)
    extra = staged - outputs.keys()
    if extra:
        raise SystemExit("Unexpected staged release files: " + ", ".join(sorted(extra)))


def stage_outputs(outputs):
    for name in outputs:
        refuse_mounted_destination(PUBLIC / name)
    # Only this generated subtree is replaced. Do not accumulate obsolete releases
    # or touch unrelated public assets (including files outside this app folder).
    generated = PUBLIC / "badge-app"
    staged_files(generated)  # Reject links before recursively removing generated data.
    legacy = PUBLIC / "apps" / APP_ID
    unexpected = staged_files(legacy) - outputs.keys()
    if unexpected:
        raise SystemExit("Unexpected public app files; remove explicitly: " +
                         ", ".join(sorted(unexpected)))
    if generated.exists():
        shutil.rmtree(generated)
    for name, data in outputs.items():
        destination = PUBLIC / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    check_outputs(outputs)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="verify served files, manifest and both ZIPs without writing")
    args = parser.parse_args(argv)
    refuse_mounted_destination(PUBLIC)
    if any(marker in str(APP.resolve()) for marker in ("/Volumes/", "/media/", "/mnt/")):
        raise SystemExit("Refusing to package from a mounted drive")
    files = collect()
    release, outputs = release_outputs(files)
    if args.check:
        check_outputs(outputs)
    else:
        stage_outputs(outputs)
    print("%s %s (%d ZIP bytes, %d app files)" %
          ("verified" if args.check else "staged", release["version"], release["zipBytes"], len(files)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
