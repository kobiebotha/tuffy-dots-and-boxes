"""Checked-in module variants must match their source and artifact manifests."""
import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ArtifactTests(unittest.TestCase):
    def test_prebuilt_variants_have_matching_hash_abi_and_source_inputs(self):
        payloads = {}
        for variant in ("sqlite", "powersync"):
            with self.subTest(variant=variant):
                folder = ROOT / "prebuilt" / variant
                metadata = json.loads((folder / "module.json").read_text())
                payload = (folder / metadata["file"]).read_bytes()
                self.assertEqual(metadata["variant"], variant)
                self.assertEqual(metadata["file"], "native_sqlite.mpy")
                self.assertEqual(metadata["mpy"], 7942)
                self.assertEqual(payload[:4], bytes((77, 6, 31, 31)))
                self.assertEqual(len(payload), metadata["bytes"])
                self.assertEqual(hashlib.sha256(payload).hexdigest(), metadata["sha256"])
                for name, expected in metadata["sourceFiles"].items():
                    self.assertEqual(hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), expected, name)
                self.assertEqual(hashlib.sha256(json.dumps(metadata["sourceFiles"], sort_keys=True).encode()).hexdigest(),
                                 metadata["sourceSha256"])
                self.assertNotIn(b"badgegotchi", payload)
                payloads[variant] = payload
        self.assertNotIn(b"powersync_rs_version", payloads["sqlite"])
        self.assertIn(b"powersync_rs_version", payloads["powersync"])


if __name__ == "__main__":
    unittest.main()
