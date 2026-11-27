"""Manifest invariants for the managed CPython runtime (bin/python-runtime.json).

The npm launcher downloads a prebuilt CPython when no 3.10+ interpreter exists.
Nothing at runtime double-checks the pin, so CI must: every target needs a real
64-hex SHA-256, a plausible size, and an asset name that matches the pinned
version and release, or a silent typo turns into a broken install path.
"""

import json
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = PROJECT_ROOT / "bin" / "python-runtime.json"
LAUNCHER_PATH = PROJECT_ROOT / "bin" / "tokensafe.js"
EXPECTED_BASE_URL = (
    "https://github.com/astral-sh/python-build-standalone/releases/download"
)
REQUIRED_TARGETS = {
    "darwin-arm64",
    "darwin-x64",
    "linux-x64",
    "linux-arm64",
    "win32-x64",
}


class ManagedRuntimeManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def test_schema_and_https_source(self):
        self.assertEqual(self.manifest["schema"], 1)
        self.assertTrue(self.manifest["base_url"].startswith("https://"))
        self.assertEqual(self.manifest["base_url"].rstrip("/"), EXPECTED_BASE_URL)
        self.assertIn("python-build-standalone", self.manifest["source"])

    def test_every_target_is_pinned(self):
        targets = self.manifest["targets"]
        self.assertLessEqual(REQUIRED_TARGETS, set(targets))
        python, release = self.manifest["python"], self.manifest["release"]
        self.assertRegex(python, r"^3\.1[0-9]\.[0-9]+$")
        self.assertRegex(release, r"^[0-9]{8}$")

        for key, target in targets.items():
            with self.subTest(target=key):
                self.assertRegex(target["sha256"], r"^[0-9a-f]{64}$")
                self.assertGreater(target["size"], 10 * 1024 * 1024)
                self.assertIn(python, target["asset"])
                self.assertIn(release, target["asset"])
                self.assertTrue(target["asset"].endswith("-install_only.tar.gz"))

    def test_targets_are_unique(self):
        assets = [t["asset"] for t in self.manifest["targets"].values()]
        digests = [t["sha256"] for t in self.manifest["targets"].values()]
        self.assertEqual(len(assets), len(set(assets)))
        self.assertEqual(len(digests), len(set(digests)))

    def test_launcher_verifies_and_exposes_escape_hatches(self):
        source = LAUNCHER_PATH.read_text(encoding="utf-8")
        for needle in (
            "python-runtime.json",
            "sha256File",
            "TOKENSAFE_NO_DOWNLOAD",
            "TOKENSAFE_PYTHON",
            "TOKENSAFE_HOME",
            "RUNTIME_PYTHON_LAYOUT",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, source)
        # The download must never be extracted before the digest is compared.
        self.assertLess(
            source.index("sha256File(archive)"), source.index("extractArchive(archive, dir)")
        )

    def test_manifest_is_published_in_the_npm_package(self):
        package = json.loads(
            (PROJECT_ROOT / "package.json").read_text(encoding="utf-8")
        )
        self.assertIn("bin/python-runtime.json", package["files"])


if __name__ == "__main__":
    unittest.main()
