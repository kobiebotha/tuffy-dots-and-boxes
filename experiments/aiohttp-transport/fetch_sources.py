"""Fetch only the public, revision/hash-pinned comparison sources into this experiment."""
import hashlib
import json
from pathlib import Path
import urllib.request


ROOT = Path(__file__).resolve().parent


def main():
    for source in json.loads((ROOT / "sources.json").read_text()):
        url = "https://raw.githubusercontent.com/{repository}/{revision}/{path}".format(**source)
        with urllib.request.urlopen(url, timeout=30) as response:
            data = response.read(128 * 1024)
        if hashlib.sha256(data).hexdigest() != source["sha256"]:
            raise ValueError("Source digest mismatch: " + source["name"])
        target = ROOT / "sources" / source["name"] / "__init__.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        print(source["name"], source["revision"], "verified")


if __name__ == "__main__":
    main()
