import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json
import tempfile
import unittest
from unittest.mock import patch

from src.format_parsers.lockfiles import LockfileReducer


class TestLockfileQuery(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.orig_data_dir = os.environ.get("TOKENSAFE_DATA_DIR")
        os.environ["TOKENSAFE_DATA_DIR"] = self.tmp_dir.name

    def tearDown(self):
        if self.orig_data_dir is not None:
            os.environ["TOKENSAFE_DATA_DIR"] = self.orig_data_dir
        else:
            os.environ.pop("TOKENSAFE_DATA_DIR", None)
        self.tmp_dir.cleanup()

    def test_query_npm_lock_v2_v3(self):
        npm_lock = {
            "name": "test-project",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "packages": {
                "": {"name": "test-project", "version": "1.0.0"},
                "node_modules/express": {
                    "version": "4.19.2",
                    "resolved": "https://registry.npmjs.org/express/-/express-4.19.2.tgz",
                    "integrity": "sha512-..."
                },
                "node_modules/@prisma/client": {
                    "version": "5.10.2",
                    "resolved": "https://registry.npmjs.org/@prisma/client/-/client-5.10.2.tgz"
                }
            }
        }
        lock_path = os.path.join(self.tmp_dir.name, "package-lock.json")
        with open(lock_path, "w", encoding="utf-8") as f:
            json.dump(npm_lock, f)

        # Query existing package
        res = LockfileReducer.query_dependency(lock_path, "express")
        self.assertTrue(res["found"])
        self.assertEqual(res["version"], "4.19.2")
        self.assertIn("express-4.19.2.tgz", res.get("resolved", ""))

        # Query scoped package
        res_prisma = LockfileReducer.query_dependency(lock_path, "@prisma/client")
        self.assertTrue(res_prisma["found"])
        self.assertEqual(res_prisma["version"], "5.10.2")

        # Query non-existing package
        res_missing = LockfileReducer.query_dependency(lock_path, "non-existent-pkg")
        self.assertFalse(res_missing["found"])
        self.assertIn("not found in package-lock.json", res_missing.get("message", ""))

    def test_query_cargo_lock(self):
        cargo_content = """
[[package]]
name = "tokio"
version = "1.38.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
dependencies = [
 "bytes",
 "pin-project-lite",
]

[[package]]
name = "serde"
version = "1.0.203"
source = "registry+https://github.com/rust-lang/crates.io-index"
"""
        lock_path = os.path.join(self.tmp_dir.name, "Cargo.lock")
        with open(lock_path, "w", encoding="utf-8") as f:
            f.write(cargo_content)

        res = LockfileReducer.query_dependency(lock_path, "tokio")
        self.assertTrue(res["found"])
        self.assertEqual(res["version"], "1.38.0")
        self.assertIn("crates.io-index", res.get("resolved", ""))

    def test_query_poetry_lock(self):
        poetry_content = """
[[package]]
name = "requests"
version = "2.31.0"
description = "Python HTTP for Humans."
optional = false
python-versions = ">=3.7"

[[package]]
name = "pydantic"
version = "2.6.4"
description = "Data validation using Python type hints"
"""
        lock_path = os.path.join(self.tmp_dir.name, "poetry.lock")
        with open(lock_path, "w", encoding="utf-8") as f:
            f.write(poetry_content)

        res = LockfileReducer.query_dependency(lock_path, "pydantic")
        self.assertTrue(res["found"])
        self.assertEqual(res["version"], "2.6.4")

    def test_generate_lockfile_summary(self):
        npm_lock = {
            "name": "my-app",
            "version": "2.0.0",
            "lockfileVersion": 3,
            "packages": {
                f"node_modules/pkg-{i}": {"version": f"1.{i}.0"}
                for i in range(50)
            }
        }
        lock_path = os.path.join(self.tmp_dir.name, "package-lock.json")
        with open(lock_path, "w", encoding="utf-8") as f:
            json.dump(npm_lock, f)

        summary_path = LockfileReducer.generate_lockfile_summary(lock_path)
        self.assertIsNotNone(summary_path)
        self.assertTrue(os.path.isfile(summary_path))

        with open(summary_path, "r", encoding="utf-8") as f:
            summary_content = f.read()

        self.assertIn("TokenSafe Lockfile Guard", summary_content)
        self.assertIn("query_dependency", summary_content)

    def test_find_lockfile(self):
        sub_dir = os.path.join(self.tmp_dir.name, "src", "nested")
        os.makedirs(sub_dir, exist_ok=True)
        root_lock = os.path.join(self.tmp_dir.name, "package-lock.json")
        with open(root_lock, "w") as f:
            f.write("{}")

        found = LockfileReducer.find_lockfile(start_dir=sub_dir)
        self.assertEqual(os.path.realpath(found), os.path.realpath(root_lock))

    def test_query_bun_lock(self):
        bun_lock_jsonc = """{
  "lockfileVersion": 1,
  "configVersion": 1,
  "workspaces": {
    "": {
      "name": "my-app",
      "dependencies": {
        "marked": "^18.2.0",
        "@tanstack/react-query": "^5.107.1",
        "missing-pkg": "^1.0.0",
      },
      "devDependencies": {
        "typescript": "^5.7.0",
      },
    },
  },
  "packages": {
    "marked": ["marked@18.2.0", "", {"bin": {"marked": "bin/marked.js"}}, "sha512-fWegr+WsEsiMVW4KUNdIIzzMTZSzNiqhy0//6+2MRi2d5NFNsL/AYwBQW8eBnRbTBVRnDVzjaJEUqKaCWjMMdQ=="],
    "@tanstack/react-query": ["@tanstack/react-query@5.107.1", "", {"dependencies": {"@tanstack/query-core": "5.107.1"}}, "sha512-udCOOLp3ZAheUrIHLCe+ZZge6nr2ZUrGMSWcCuXsjY1Bf2JeeE09oxst97PILiCY6N8xo/7bIo3Hvz13zh06FA=="],
  },
}
"""
        lock_path = os.path.join(self.tmp_dir.name, "bun.lock")
        with open(lock_path, "w", encoding="utf-8") as f:
            f.write(bun_lock_jsonc)

        # 1. Direct package with // inside sha512 hash
        res = LockfileReducer.query_dependency(lock_path, "marked")
        self.assertTrue(res["found"])
        self.assertEqual(res["version"], "18.2.0")
        self.assertIn("fWegr+WsEsiMVW4KUNdIIzzMTZSzNiqhy0//6+2MRi2d5NFNsL", res.get("resolved", ""))
        self.assertEqual(res["_lockfile_bytes"], len(bun_lock_jsonc.encode("utf-8")))

        # 2. Scoped package
        res_scoped = LockfileReducer.query_dependency(lock_path, "@tanstack/react-query")
        self.assertTrue(res_scoped["found"])
        self.assertEqual(res_scoped["version"], "5.107.1")
        self.assertEqual(res_scoped["dependencies"].get("@tanstack/query-core"), "5.107.1")

        # 3. Workspace fallback dependency (in workspaces[""] but not in packages)
        res_ws = LockfileReducer.query_dependency(lock_path, "missing-pkg")
        self.assertTrue(res_ws["found"])
        self.assertEqual(res_ws["version"], "^1.0.0")
        self.assertIsNone(res_ws["resolved"])

        # 4. Completely missing package
        res_missing = LockfileReducer.query_dependency(lock_path, "not-installed-anywhere")
        self.assertFalse(res_missing["found"])
        self.assertIn("not found in bun.lock", res_missing.get("message", ""))

    def test_reduce_bun_lock(self):
        bun_lock_jsonc = """{
  "lockfileVersion": 1,
  "configVersion": 1,
  "workspaces": {
    "": {
      "dependencies": {
        "marked": "^18.2.0",
      },
    },
  },
  "packages": {
    "marked": ["marked@18.2.0", "", {}, "sha512-test"],
  },
}
"""
        # Test with over 30 lines to trigger reduction
        padded = bun_lock_jsonc + "\n" * 40
        reduced, was_red = LockfileReducer.reduce(padded, filename="bun.lock")
        self.assertTrue(was_red)
        self.assertIn("TokenSafe Lockfile Summary: bun.lock", reduced)
        self.assertIn("marked: 18.2.0", reduced)

    def test_find_lockfile_downward_monorepo(self):
        root = os.path.join(self.tmp_dir.name, "monorepo")
        frontend = os.path.join(root, "frontend")
        os.makedirs(frontend, exist_ok=True)
        bun_lock = os.path.join(frontend, "bun.lock")
        with open(bun_lock, "w") as f:
            f.write("{}")

        # Finding from root without lockfile at root should find frontend/bun.lock
        found = LockfileReducer.find_lockfile(start_dir=root)
        self.assertIsNotNone(found)
        self.assertEqual(os.path.realpath(found), os.path.realpath(bun_lock))

    @patch("src.team_cache.TeamCache.put")
    def test_l3_payload_strips_paths_and_private_bytes(self, mock_team_put):
        lock_path = os.path.join(self.tmp_dir.name, "Cargo.lock")
        with open(lock_path, "w", encoding="utf-8") as f:
            f.write("[[package]]\nname = \"tokio\"\nversion = \"1.38.0\"\n")

        res = LockfileReducer.query_dependency(lock_path, "tokio")
        self.assertTrue(res["found"])
        self.assertTrue(mock_team_put.called)

        # Inspect payload sent to TeamCache.put
        args, kwargs = mock_team_put.call_args
        cache_type, entry_key, payload_json = args
        self.assertEqual(cache_type, "LOCKFILE_INDEX")
        payload = json.loads(payload_json)
        self.assertNotIn("lockfile_path", payload)
        self.assertNotIn("_lockfile_bytes", payload)
        self.assertEqual(payload.get("package"), "tokio")

    @patch("src.team_cache.TeamCache.get")
    def test_l3_team_cache_integration(self, mock_team_get):
        mock_team_get.return_value = json.dumps({
            "found": True,
            "package": "fastapi",
            "version": "0.110.0",
            "resolved": "pypi"
        })
        lock_path = os.path.join(self.tmp_dir.name, "poetry.lock")
        with open(lock_path, "w") as f:
            f.write("[[package]]\nname = \"dummy\"\nversion = \"0.1\"")

        res = LockfileReducer.query_dependency(lock_path, "fastapi")
        self.assertTrue(res["found"])
        self.assertEqual(res["source"], "L3_team_cache")
        self.assertEqual(res["version"], "0.110.0")


if __name__ == "__main__":
    unittest.main()
