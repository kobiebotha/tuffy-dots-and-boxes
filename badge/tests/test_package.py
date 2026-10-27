"""Release checks use temporary directories and never access mounted badge drives."""
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "badge_package", Path(__file__).resolve().parents[1] / "tools" / "package.py")
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)


class PackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = package.collect()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.public = Path(self.temporary.name).resolve() / "public"
        self.app = Path(self.temporary.name).resolve() / "source"
        self.native = Path(self.temporary.name).resolve() / "native"
        self.native.mkdir()
        (self.native / "native_sqlite.mpy").write_bytes(self.sources["powersync/native_sqlite.mpy"])
        notices = self.native / "THIRD_PARTY_NOTICES.txt"
        notices.write_bytes(self.sources["powersync/native_sqlite.LICENSE.txt"])
        for name, data in self.sources.items():
            destination = self.app / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        self.addCleanup(patch.stopall)
        patch.object(package, "PUBLIC", self.public).start()
        patch.object(package, "APP", self.app).start()
        patch.object(package, "SDK", self.app / "powersync").start()
        patch.object(package, "SHADOW_VFS", self.app / "sqlite_shadow_vfs.py").start()
        patch.object(package, "NATIVE", self.native).start()
        patch.object(package, "NATIVE_NOTICES", notices).start()
        self.release, self.outputs = package.release_outputs(self.sources)

    def stage(self):
        package.stage_outputs(self.outputs)

    def test_manifest_zip_and_every_served_file_match_explicit_sources(self):
        self.stage()
        package.check_outputs(self.outputs)
        manifest = json.loads((self.public / "badge-app/manifest.json").read_text())
        release = manifest["release"]
        self.assertEqual(manifest["schemaVersion"], 1)
        self.assertEqual(release["appId"], "dots_boxes")
        self.assertEqual(release["runtimeMpy"], 7942)
        self.assertEqual(len(release["files"]), len(package.SOURCE_FILES))
        self.assertEqual(self.outputs["dots-boxes.zip"],
                         self.outputs[release["zipUrl"].lstrip("/")])
        self.assertEqual(release["zipSha256"], package.sha(self.outputs["dots-boxes.zip"]))
        self.assertEqual(release["zipBytes"], len(self.outputs["dots-boxes.zip"]))
        with zipfile.ZipFile(io.BytesIO(self.outputs["dots-boxes.zip"])) as archive:
            self.assertEqual(set(archive.namelist()),
                             {"apps/dots_boxes/" + name for name in package.SOURCE_FILES})
            for file in release["files"]:
                data = self.sources[file["path"]]
                self.assertEqual(archive.read("apps/dots_boxes/" + file["path"]), data)
                self.assertEqual((self.public / file["url"].lstrip("/")).read_bytes(), data)
                self.assertEqual(file["sha256"], package.sha(data))
                self.assertEqual(file["bytes"], len(data))

    def test_release_is_deterministic_and_changes_with_any_source(self):
        self.assertEqual((self.release, self.outputs), package.release_outputs(self.sources))
        changed = {**self.sources, "game.py": self.sources["game.py"] + b"\n"}
        updated, _ = package.release_outputs(changed)
        self.assertNotEqual(updated["version"], self.release["version"])
        changed = {**self.sources, "sqlite_shadow_vfs.py": self.sources["sqlite_shadow_vfs.py"] + b"\n"}
        updated, _ = package.release_outputs(changed)
        self.assertNotEqual(updated["version"], self.release["version"])
        self.assertNotEqual(updated["zipUrl"], self.release["zipUrl"])
        changed = {**self.sources, "powersync/sync.py": self.sources["powersync/sync.py"] + b"\n"}
        updated, _ = package.release_outputs(changed)
        self.assertNotEqual(updated["version"], self.release["version"])

    def test_zip_imports_the_app_and_its_bundled_dependency_in_isolation(self):
        destination = Path(self.temporary.name) / "unpacked"
        with zipfile.ZipFile(io.BytesIO(self.outputs["dots-boxes.zip"])) as archive:
            archive.extractall(destination)
        app = destination / "apps/dots_boxes"
        script = '''
import sys
sys.path.insert(0, sys.argv[1])
from dots_client import Client
from dots_storage import Store
from dots_sync import GameConnector
from powersync.database import Database
from powersync.sync import Connector
import powersync
from powersync import bounded_dns
import sqlite_shadow_vfs
assert powersync.__file__ == sys.argv[1] + '/powersync/__init__.py'
assert bounded_dns.__file__ == sys.argv[1] + '/powersync/bounded_dns.py'
assert sqlite_shadow_vfs.__file__ == sys.argv[1] + '/sqlite_shadow_vfs.py'
assert issubclass(Store, Database)
assert issubclass(GameConnector, Connector)
'''
        result = subprocess.run([sys.executable, "-I", "-c", script, str(app)],
                                cwd=destination, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_generation_removes_only_explicit_retired_sdk_copies(self):
        legacy = self.public / "apps/dots_boxes"
        legacy.mkdir(parents=True)
        for name in package.RETIRED_FILES:
            path = legacy / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"previous generated SDK copy")
        self.stage()
        self.assertTrue(all(not (legacy / name).exists() for name in package.RETIRED_FILES))
        package.check_outputs(self.outputs)

    def test_check_rejects_tampered_manifest_file_zip_and_legacy_copy(self):
        for name in ("badge-app/manifest.json", self.release["files"][0]["url"].lstrip("/"),
                     self.release["zipUrl"].lstrip("/"), "dots-boxes.zip",
                     "apps/dots_boxes/game.py"):
            with self.subTest(name=name):
                self.stage()
                (self.public / name).write_bytes(b"tampered")
                with self.assertRaisesRegex(SystemExit, "differs from source"):
                    package.check_outputs(self.outputs)

    def test_check_rejects_missing_file_and_unlisted_generated_file(self):
        self.stage()
        staged = self.public / self.release["files"][0]["url"].lstrip("/")
        staged.unlink()
        with self.assertRaisesRegex(SystemExit, "differs from source"):
            package.check_outputs(self.outputs)
        self.stage()
        (self.public / "badge-app/unlisted.txt").write_text("extra")
        with self.assertRaisesRegex(SystemExit, "Unexpected staged release files"):
            package.check_outputs(self.outputs)

    def test_generation_removes_stale_release_only_within_generated_subtree(self):
        self.stage()
        unrelated = self.public / "unrelated.txt"
        unrelated.write_text("keep")
        old = self.public / "badge-app/old-release/files/old.py"
        old.parent.mkdir(parents=True)
        old.write_text("stale")
        self.stage()
        self.assertFalse(old.exists())
        self.assertEqual(unrelated.read_text(), "keep")
        package.check_outputs(self.outputs)

    def test_unlisted_source_is_never_packaged(self):
        (self.app / "credentials.json").write_text("not an app file")
        self.assertEqual(package.collect(), self.sources)

    def test_generation_rejects_unlisted_legacy_files_without_deleting_them(self):
        self.stage()
        unknown = self.public / "apps/dots_boxes/unknown.py"
        unknown.write_text("keep until explicitly removed")
        with self.assertRaisesRegex(SystemExit, "Unexpected public app files"):
            self.stage()
        self.assertTrue(unknown.exists())

    def test_source_check_rejects_native_and_runtime_pin_mismatches(self):
        native = self.native / "native_sqlite.mpy"
        native.write_bytes(self.sources["powersync/native_sqlite.mpy"][:-1] + b"!")
        with self.assertRaisesRegex(SystemExit, "Native module does not match"):
            package.collect()
        native.write_bytes(self.sources["powersync/native_sqlite.mpy"])
        (self.app / "powersync/runtime.py").write_text("wrong runtime pin")
        with self.assertRaisesRegex(SystemExit, "Runtime pin does not match"):
            package.collect()

    def test_native_dependency_is_collected_from_its_component_and_requires_powersync(self):
        (self.app / "powersync/native_sqlite.mpy").write_bytes(b"stale SDK copy")
        self.assertEqual(package.collect(), self.sources)
        with patch.object(package, "NATIVE_METADATA", {**package.NATIVE_METADATA, "variant": "sqlite"}), \
                self.assertRaisesRegex(SystemExit, "PowerSync Tufty build"):
            package.collect()

    def test_source_check_rejects_symlinks_and_missing_files(self):
        source = self.app / "game.py"
        source.unlink()
        with self.assertRaisesRegex(SystemExit, "Missing or symlinked"):
            package.collect()
        source.symlink_to(self.app / "controls.py")
        with self.assertRaisesRegex(SystemExit, "Missing or symlinked"):
            package.collect()

    def test_generation_rejects_symlinks_without_following_them(self):
        self.stage()
        target = self.public / "unrelated.txt"
        target.write_text("keep")
        (self.public / "badge-app/escape").symlink_to(target)
        with self.assertRaisesRegex(SystemExit, "Symlinked staged release"):
            self.stage()
        self.assertEqual(target.read_text(), "keep")

    def test_refuses_mounted_destinations(self):
        for root in ("/Volumes/BADGE", "/media/BADGE", "/mnt/BADGE", "/run/media/BADGE"):
            with self.subTest(root=root), self.assertRaisesRegex(SystemExit, "mounted drive"):
                package.refuse_mounted_destination(Path(root) / "apps/dots_boxes/game.py")


if __name__ == "__main__":
    unittest.main()
