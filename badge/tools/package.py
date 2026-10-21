"""Zip the badge app for the web installer. This never writes to a USB drive."""
import hashlib
import io
import sys
import zipfile
from pathlib import Path

BADGE = Path(__file__).resolve().parents[1]
APP = BADGE / "app" / "dots_boxes"
REPO = BADGE.parent
OUTPUT = REPO / "web" / "public" / "dots-boxes.zip"
APP_ID = "dots_boxes"
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
    text = str(path)
    for marker in ("/Volumes/", "/media/", "/mnt/", "/run/media/"):
        if marker in text:
            raise SystemExit("Refusing to write onto a mounted drive: " + text)


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
    identity = (APP / "dots_runtime.py").read_text()
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


def main():
    refuse_mounted_destination(OUTPUT)
    if any(marker in str(APP) for marker in ("/Volumes/", "/media/", "/mnt/")):
        raise SystemExit("Refusing to package from a mounted drive")
    files = collect()
    payload = make_zip(files)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(payload)
    print("wrote %s (%d bytes, %d files)" % (OUTPUT.name, len(payload), len(files)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
