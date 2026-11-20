import os
import shutil
import tempfile
import unittest

from src.mcp_server import _dispatch_tool_call


class TestSmartBundle(unittest.TestCase):
    """Multi-symbol context bundle: one call replaces 3+ smart_read_file calls,
    deduplicating shared imports and merging 1-hop dependency signatures."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        # Fresh metrics DB + data dir: the developer's real local DB may already
        # be over the Community quota, which would pause smart_bundle in tests.
        # The monthly quota ALSO reads the server-floor mirror from
        # resolve_data_dir(), so the DATA dir must be isolated too.
        self._old_db_env = os.environ.get("TOKENSAFE_DB_PATH")
        os.environ["TOKENSAFE_DB_PATH"] = os.path.join(self.dir, "metrics.db")
        self._old_data_env = os.environ.get("TOKENSAFE_DATA_DIR")
        os.environ["TOKENSAFE_DATA_DIR"] = os.path.join(self.dir, "data")
        os.environ["TOKENSAFE_TELEMETRY_DISABLED"] = "1"
        self.a_py = os.path.join(self.dir, "repo.py")
        with open(self.a_py, "w", encoding="utf-8") as f:
            f.write(
                "import os\n"
                "import json\n"
                "\n"
                "class Repo:\n"
                "    def fetch(self):\n"
                "        return self._load()\n"
                "\n"
                "    def _load(self):\n"
                "        return json.dumps({})\n"
            )
        self.b_py = os.path.join(self.dir, "service.py")
        with open(self.b_py, "w", encoding="utf-8") as f:
            f.write(
                "import os\n"
                "import json\n"
                "\n"
                "class Service:\n"
                "    def run(self):\n"
                "        return os.path.join('x', 'y')\n"
            )

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        if self._old_db_env is None:
            os.environ.pop("TOKENSAFE_DB_PATH", None)
        else:
            os.environ["TOKENSAFE_DB_PATH"] = self._old_db_env
        if self._old_data_env is None:
            os.environ.pop("TOKENSAFE_DATA_DIR", None)
        else:
            os.environ["TOKENSAFE_DATA_DIR"] = self._old_data_env
        os.environ.pop("TOKENSAFE_TELEMETRY_DISABLED", None)

    def _bundle(self, targets, **extra):
        return _dispatch_tool_call("smart_bundle", {"targets": targets, **extra})

    def test_bundle_merges_files_and_dedupes_shared_imports(self):
        res = self._bundle([
            {"path": self.a_py, "symbol": "Repo"},
            {"path": self.b_py, "symbol": "Service"},
        ])
        self.assertFalse(res.get("isError"))
        text = res["content"][0]["text"]
        self.assertIn("Shared imports", text)
        self.assertIn("class Repo", text)
        self.assertIn("class Service", text)
        self.assertEqual(text.count("import os"), 1, "shared import emitted once")
        self.assertEqual(text.count("import json"), 1, "shared import emitted once")
        # Non-target code folds away
        self.assertIn("...", text)

    def test_bundle_merges_dep_signatures(self):
        res = self._bundle([{"path": self.a_py, "symbol": "Repo"}])
        text = res["content"][0]["text"]
        self.assertIn("Merged 1-hop dependency signatures", text)
        self.assertIn("_load", text)

    def test_bundle_without_deps_section_when_disabled(self):
        res = self._bundle([{"path": self.a_py, "symbol": "Repo"}], include_deps=False)
        text = res["content"][0]["text"]
        self.assertNotIn("Merged 1-hop dependency signatures", text)

    def test_validation_errors(self):
        res = self._bundle([{"path": self.a_py}, {"path": self.b_py}] * 5)  # 10 > 8
        self.assertTrue(res.get("isError"))
        res = self._bundle([{"path": os.path.join(self.dir, "nope.py")}])
        self.assertTrue(res.get("isError"))
        res = self._bundle([{"symbol": "X"}])  # missing path
        self.assertTrue(res.get("isError"))
        res = _dispatch_tool_call("smart_bundle", {})
        self.assertTrue(res.get("isError"))

    def test_bundle_tools_listed(self):
        from src.mcp_server import TOOLS
        names = {t["name"] for t in TOOLS}
        self.assertIn("smart_bundle", names)


if __name__ == "__main__":
    unittest.main()
