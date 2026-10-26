#!/usr/bin/env python3
"""Fetch checksummed build sources; --check is strictly offline."""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import tarfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def digest(data, algorithm="sha256"):
    return hashlib.new(algorithm, data).hexdigest()


def load_pins():
    return json.loads((ROOT / "dependencies.json").read_text())


def source_directory(name):
    return ROOT / "vendor" / load_pins()[name]["directory"]


def verify(directory, files):
    return [name for name, expected in files.items()
            if (directory / name).is_symlink() or not (directory / name).is_file()
            or digest((directory / name).read_bytes()) != expected]


def member_name(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ValueError("Unsafe archive path: " + name)
    return PurePosixPath(*path.parts[1:]).as_posix()


def unpack(data, pin):
    """Extract only allowlisted regular files, validating all before writing."""
    contents = {}
    if pin["format"] == "zip":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for member in archive.infolist():
                name = member_name(member.filename)
                if name in pin["files"]:
                    if member.is_dir() or name in contents:
                        raise ValueError("Invalid or duplicate archive member: " + name)
                    contents[name] = archive.read(member)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive:
                name = member_name(member.name)
                if name in pin["files"]:
                    if not member.isfile() or name in contents:
                        raise ValueError("Invalid or duplicate archive member: " + name)
                    contents[name] = archive.extractfile(member).read()
    if contents.keys() != pin["files"].keys():
        raise ValueError("Archive is missing pinned source files")
    for name, expected in pin["files"].items():
        if digest(contents[name]) != expected:
            raise ValueError("Source checksum mismatch: " + name)
    return contents


def fetch(names, check=False, cache=None):
    pins = load_pins()
    for name in names:
        pin = pins[name]
        destination = ROOT / "vendor" / pin["directory"]
        missing = verify(destination, pin["files"])
        if missing and check:
            raise ValueError("Missing/modified %s sources: %s" % (name, ", ".join(missing)))
        if missing:
            archive = (Path(cache) if cache else ROOT / "cache") / pin["archive"]
            if archive.is_file():
                data = archive.read_bytes()
            else:
                with urllib.request.urlopen(pin["url"], timeout=60) as response:
                    data = response.read()
            if digest(data, pin["digest_algorithm"]) != pin["digest"]:
                raise ValueError("Archive checksum mismatch: " + name)
            contents = unpack(data, pin)
            for relative in missing:
                target = destination / relative
                if any(parent.is_symlink() for parent in (target, *target.parents)):
                    raise ValueError("Symlinked source destination: " + str(target))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(contents[relative])
            local_cache = ROOT / "cache" / pin["archive"]
            local_cache.parent.mkdir(parents=True, exist_ok=True)
            local_cache.write_bytes(data)
        print("Verified %s %s" % (name, pin["version"]), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dependencies", nargs="*", choices=tuple(load_pins()))
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--cache", type=Path, help="directory containing pinned source archives")
    args = parser.parse_args()
    try:
        fetch(args.dependencies or tuple(load_pins()), args.check, args.cache)
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
