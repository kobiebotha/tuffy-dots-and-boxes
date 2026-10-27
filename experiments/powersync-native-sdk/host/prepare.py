"""Generate public deterministic fixture and a patched SDK copy; no production writes."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SDK = ROOT.parent / "powersync-native"
REVISION = "96a94f3e6e9442c4181d793f95241c2ee9cab4d8"
assert subprocess.check_output(["git", "-C", SDK, "rev-parse", "HEAD"], text=True).strip() == REVISION
assert not subprocess.check_output(["git", "-C", SDK, "status", "--porcelain"], text=True)
snapshot = HERE / "sdk"
if not snapshot.exists():
    shutil.copytree(SDK / "powersync", snapshot)
    manifest = snapshot / "Cargo.toml"
    manifest.write_text(manifest.read_text().replace("repository.workspace = true",
        'repository = "https://github.com/powersync-ja/powersync-native"'))
    subprocess.run(["python3", str(HERE.parent / "patches/apply_raw_connection_patch.py"),
                    str(snapshot)], check=True)

# Reject accidental drift in the generated copy; the assessed SDK patch is only
# raw API visibility plus standalone Cargo repository metadata.
for original in (SDK / "powersync").rglob("*"):
    if not original.is_file():
        continue
    relative = original.relative_to(SDK / "powersync")
    expected_source = original.read_bytes()
    if relative.as_posix() == "Cargo.toml":
        expected_source = expected_source.replace(b"repository.workspace = true",
            b'repository = "https://github.com/powersync-ja/powersync-native"')
    elif relative.as_posix() == "src/lib.rs":
        expected_source = expected_source.replace(b"pub use db::pool::{ConnectionPool, LeasedConnection};",
            b"pub use db::connection::{RawSqliteConnection, SqliteConnection};\npub use db::pool::{ConnectionPool, LeasedConnection};")
    elif relative.as_posix() == "src/db/pool.rs":
        expected_source = expected_source.replace(b"pub(crate) fn sqlite_connection", b"pub fn sqlite_connection")
    assert (snapshot / relative).read_bytes() == expected_source, "Unexpected SDK copy change: " + str(relative)

source = ROOT / "badge/app/dots_boxes/dots_storage.py"
constants = {}
for node in ast.parse(source.read_text()).body:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in ("APP_SCHEMA", "RAW_TABLE_SQL"):
                constants[target.id] = ast.literal_eval(node.value)
spec = importlib.util.spec_from_file_location("download_benchmark", ROOT / "badge/tools/benchmark_download.py")
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)
room, operations, wire = benchmark.fixture()
checkpoint = {"checkpoint": {"last_op_id": "50", "buckets": [{"bucket": "fixture",
    "checksum": 0, "count": 50, "subscriptions": [{"sub": 0}]}],
    "streams": [{"name": "badge_game_v2", "is_default": False, "errors": []}]}}
fixture = {"schema": json.loads(constants["APP_SCHEMA"]), "raw_sql": constants["RAW_TABLE_SQL"],
    "room": room, "operations": operations, "wire": wire.decode(), "checkpoint": checkpoint,
    "benchmark_wire_bytes": len(wire), "sdk_revision": REVISION,
    "storage_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
(HERE / "fixture.json").write_text(json.dumps(fixture, indent=2) + "\n")
print("Prepared SDK copy and", len(wire), "byte /", len(operations), "operation fixture")
