"""Source integrity, archive extraction and reproducible adaptation checks."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import fetch
import prepare


class ArchiveTests(unittest.TestCase):
    def archive(self, members):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            for name, data, kind in members:
                entry = tarfile.TarInfo(name)
                entry.type = kind
                entry.size = len(data) if kind == tarfile.REGTYPE else 0
                archive.addfile(entry, io.BytesIO(data) if entry.size else None)
        return output.getvalue()

    def pin(self):
        return {"format": "tar", "files": {"src/module.c": hashlib.sha256(b"source").hexdigest()}}

    def test_only_pinned_files_are_returned(self):
        data = self.archive([("root/src/module.c", b"source", tarfile.REGTYPE),
                             ("root/unused.c", b"not needed", tarfile.REGTYPE)])
        self.assertEqual(fetch.unpack(data, self.pin()), {"src/module.c": b"source"})

    def test_unsafe_paths_links_duplicates_missing_and_tampered_sources_fail(self):
        cases = [
            [("root/../escape", b"", tarfile.REGTYPE)],
            [("/absolute", b"", tarfile.REGTYPE)],
            [("root/src/module.c", b"", tarfile.SYMTYPE)],
            [("root/src/module.c", b"source", tarfile.REGTYPE)] * 2,
            [], [("root/src/module.c", b"modified", tarfile.REGTYPE)],
        ]
        for members in cases:
            with self.subTest(members=members), self.assertRaises(ValueError):
                fetch.unpack(self.archive(members), self.pin())

    def test_check_never_downloads_or_repairs_a_modified_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "vendor/sample/file.c"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"modified")
            pins = {"sample": {"directory": "sample", "version": "1",
                               "files": {"file.c": hashlib.sha256(b"source").hexdigest()}}}
            with patch.object(fetch, "ROOT", root), patch.object(fetch, "load_pins", return_value=pins), \
                    patch.object(fetch.urllib.request, "urlopen", side_effect=AssertionError("offline check")):
                with self.assertRaisesRegex(ValueError, "Missing/modified"):
                    fetch.fetch(["sample"], check=True)
            self.assertEqual(source.read_bytes(), b"modified")


class AdaptationTests(unittest.TestCase):
    @unittest.skipUnless((fetch.source_directory("sqlite") / "sqlite3.c").is_file(),
                         "Fetch SQLite sources to check the real adaptation")
    def test_actual_adaptation_matches_the_pinned_result(self):
        output = prepare.prepare()
        manifest = json.loads((fetch.ROOT / "sqlite-adaptations.json").read_text())
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), manifest["adapted_sha256"])

    def test_modified_input_fails_before_generating_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "sqlite3.c").write_bytes(b"modified")
            with patch.object(prepare, "source_directory", return_value=root), \
                    self.assertRaisesRegex(ValueError, "checksum"):
                prepare.prepare()


if __name__ == "__main__":
    unittest.main()
