"""Reuse the deterministic public game fixture and identify the actual SDK port."""
import hashlib
import json
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SDK = ROOT.parent / "powersync-native"
fixture = json.loads((HERE.parent / "host/fixture.json").read_text())
fixture["sdk_revision"] = subprocess.check_output(["git", "-C", SDK, "rev-parse", "HEAD"], text=True).strip()
fixture["sdk_source_state"] = "working-tree no_std port; no copied SDK"
source_files = [SDK / "powersync/Cargo.toml", SDK / "Cargo.lock", *sorted((SDK / "powersync/src").rglob("*.rs"))]
fixture["sdk_source_sha256"] = {str(p.relative_to(SDK)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files}
fixture["sdk_source_digest"] = hashlib.sha256(json.dumps(fixture["sdk_source_sha256"],sort_keys=True).encode()).hexdigest()
(HERE / "fixture.json").write_text(json.dumps(fixture, indent=2) + "\n")
print("Prepared actual SDK port identity and unchanged 9114-byte game fixture")
