"""Explicit session context, active-workspace hygiene, and search-root regression.

The bug these tests pin down: `scoped_search(path=<absolute repo dir>)` without
an explicit `project_path` used to validate the path against the machine-wide
`.active_workspace` marker. When that marker pointed at scratch space (or at a
different repo), a path that was demonstrably inside the project was rejected
as "outside project root".
"""

import os
import shutil
import tempfile
import unittest

from src.mcp_server import handle_tool_call
from src.paths import get_active_workspace, is_transient_dir, save_active_workspace
from src.session_context import (
    current_root,
    current_session_id,
    resolve_path,
    session_scope,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestSessionContext(unittest.TestCase):
    def test_scope_binds_root_and_session(self):
        with session_scope(root=REPO_ROOT, session_id="acp:s1", agent="acp"):
            self.assertEqual(current_root(), os.path.realpath(REPO_ROOT))
            self.assertEqual(current_session_id("fallback"), "acp:s1")
        self.assertIsNone(current_root())
        self.assertEqual(current_session_id("fallback"), "fallback")

    def test_nested_scopes_restore_parent(self):
        with session_scope(root=REPO_ROOT, session_id="outer"):
            with session_scope(root=os.path.join(REPO_ROOT, "src"), session_id="inner"):
                self.assertEqual(current_session_id(), "inner")
            self.assertEqual(current_session_id(), "outer")

    def test_scope_is_thread_local(self):
        import threading
        seen = {}

        def worker():
            seen["before"] = current_root()
            with session_scope(root=REPO_ROOT, session_id="thread:1"):
                seen["inside"] = current_root()

        with session_scope(root=REPO_ROOT, session_id="main"):
            t = threading.Thread(target=worker)
            t.start()
            t.join()
        self.assertIsNone(seen["before"])
        self.assertEqual(seen["inside"], os.path.realpath(REPO_ROOT))

    def test_resolve_path_prefers_scope_root(self):
        with session_scope(root=REPO_ROOT, session_id="scope"):
            self.assertEqual(
                resolve_path("src/mcp_server.py"),
                os.path.join(os.path.realpath(REPO_ROOT), "src", "mcp_server.py"),
            )
            self.assertEqual(resolve_path("/etc/hosts"), "/etc/hosts")


class TestActiveWorkspaceHygiene(unittest.TestCase):
    def setUp(self):
        self._tmp_data = tempfile.mkdtemp(prefix="tokensafe_ctx_")
        self.addCleanup(shutil.rmtree, self._tmp_data, ignore_errors=True)
        self._orig_data = os.environ.get("TOKENSAFE_DATA_DIR")
        os.environ["TOKENSAFE_DATA_DIR"] = self._tmp_data

    def tearDown(self):
        if self._orig_data is None:
            os.environ.pop("TOKENSAFE_DATA_DIR", None)
        else:
            os.environ["TOKENSAFE_DATA_DIR"] = self._orig_data

    def test_transient_detection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertTrue(is_transient_dir(tmpdir))
        self.assertTrue(is_transient_dir(tempfile.gettempdir()))
        self.assertFalse(is_transient_dir(REPO_ROOT))
        self.assertFalse(is_transient_dir(os.path.join(REPO_ROOT, "src")))

    def test_temp_dir_never_becomes_the_marker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            save_active_workspace(tmpdir)
        self.assertIsNone(get_active_workspace())

    def test_real_workspace_is_recorded(self):
        save_active_workspace(REPO_ROOT)
        self.assertEqual(get_active_workspace(), REPO_ROOT)

    def test_poisoned_marker_is_ignored(self):
        marker = os.path.join(self._tmp_data, ".active_workspace")
        with tempfile.TemporaryDirectory() as stale:
            with open(marker, "w", encoding="utf-8") as f:
                f.write(stale)
            self.assertIsNone(get_active_workspace())


class TestSearchRootResolution(unittest.TestCase):
    def setUp(self):
        self._tmp_data = tempfile.mkdtemp(prefix="tokensafe_root_")
        self.addCleanup(shutil.rmtree, self._tmp_data, ignore_errors=True)
        self._orig_data = os.environ.get("TOKENSAFE_DATA_DIR")
        os.environ["TOKENSAFE_DATA_DIR"] = self._tmp_data

    def tearDown(self):
        if self._orig_data is None:
            os.environ.pop("TOKENSAFE_DATA_DIR", None)
        else:
            os.environ["TOKENSAFE_DATA_DIR"] = self._orig_data

    def test_absolute_path_is_the_root_in_glob_mode(self):
        res = handle_tool_call(
            "scoped_search",
            {
                "query": "*.py",
                "mode": "glob",
                "path": os.path.join(REPO_ROOT, "src", "bench"),
                "max_results": 10,
            },
        )
        self.assertNotIn("isError", res)
        self.assertIn("[glob]", res["content"][0]["text"])
        self.assertIn("driver.py", res["content"][0]["text"])

    def test_absolute_path_is_the_root_in_text_mode(self):
        res = handle_tool_call(
            "scoped_search",
            {"query": "run_agent", "path": os.path.join(REPO_ROOT, "src", "bench"), "max_results": 3},
        )
        self.assertNotIn("isError", res)
        self.assertIn("driver.py", res["content"][0]["text"])

    def test_poisoned_marker_cannot_hijack_absolute_path(self):
        marker = os.path.join(self._tmp_data, ".active_workspace")
        with tempfile.TemporaryDirectory() as stale:
            with open(marker, "w", encoding="utf-8") as f:
                f.write(stale)
            res = handle_tool_call(
                "scoped_search",
                {"query": "*.py", "mode": "glob", "path": os.path.join(REPO_ROOT, "src"), "max_results": 3},
            )
        self.assertNotIn("isError", res)

    def test_outside_root_still_rejected_with_real_paths(self):
        res = handle_tool_call(
            "scoped_search",
            {
                "queries": [{"pattern": "x", "type": "text", "path": "../outside"}],
                "project_path": REPO_ROOT,
            },
        )
        self.assertTrue(res.get("isError"))
        text = res["content"][0]["text"]
        self.assertIn("outside project root", text)
        self.assertIn(REPO_ROOT, text)


class TestBatchRootRebase(unittest.TestCase):
    """A batch whose absolute paths all live in one other repo is that repo's batch.

    The reported failure: an agent searched a second checkout with
    queries=[{path: '/other/repo'}, {path: '/other/repo/src'}] and the handler
    rejected the whole call ("outside project root"), spending a turn on an error
    that named the one thing the caller had already told it.
    """

    def setUp(self):
        self._tmp_data = tempfile.mkdtemp(prefix="tokensafe_rebase_data_")
        self.addCleanup(shutil.rmtree, self._tmp_data, ignore_errors=True)
        self._orig_data = os.environ.get("TOKENSAFE_DATA_DIR")
        os.environ["TOKENSAFE_DATA_DIR"] = self._tmp_data

        self.base = tempfile.mkdtemp(prefix="tokensafe_rebase_")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.session_repo = os.path.join(self.base, "session-repo")
        self.other_repo = os.path.join(self.base, "other-repo")
        os.makedirs(os.path.join(self.session_repo, "src"))
        os.makedirs(os.path.join(self.other_repo, "src"))
        with open(os.path.join(self.session_repo, "src", "app.py"), "w", encoding="utf-8") as f:
            f.write("spin_session = 1\n")
        for rel in ("a.py", os.path.join("src", "b.py")):
            with open(os.path.join(self.other_repo, rel), "w", encoding="utf-8") as f:
                f.write("spinWheel()\n")
        # Project marker so the rebase lands on the repo root, not src/.
        open(os.path.join(self.other_repo, "pyproject.toml"), "w").close()

    def tearDown(self):
        if self._orig_data is None:
            os.environ.pop("TOKENSAFE_DATA_DIR", None)
        else:
            os.environ["TOKENSAFE_DATA_DIR"] = self._orig_data

    def test_all_absolute_paths_in_one_other_repo_rebases(self):
        res = handle_tool_call(
            "scoped_search",
            {
                "queries": [
                    {"pattern": "spinWheel", "type": "text", "path": self.other_repo},
                    {"pattern": "spinWheel", "type": "text", "path": os.path.join(self.other_repo, "src")},
                ],
                "project_path": self.session_repo,
            },
        )
        self.assertNotIn("isError", res, res["content"][0]["text"])
        text = res["content"][0]["text"]
        self.assertIn("auto-rebased", text)
        self.assertIn("b.py", text)

    def test_mixed_roots_still_rejected(self):
        res = handle_tool_call(
            "scoped_search",
            {
                "queries": [
                    {"pattern": "x", "type": "text", "path": os.path.join(self.session_repo, "src")},
                    {"pattern": "x", "type": "text", "path": self.other_repo},
                ],
                "project_path": self.session_repo,
            },
        )
        self.assertTrue(res.get("isError"))
        self.assertIn("outside project root", res["content"][0]["text"])


class TestOumvWorkspaceRouting(unittest.TestCase):
    """`oumv` must answer for the repo the caller named, never for the bound cwd."""

    def setUp(self):
        self._tmp_data = tempfile.mkdtemp(prefix="tokensafe_oumv_route_")
        self.addCleanup(shutil.rmtree, self._tmp_data, ignore_errors=True)
        self._orig_data = os.environ.get("TOKENSAFE_DATA_DIR")
        os.environ["TOKENSAFE_DATA_DIR"] = self._tmp_data
        self.repo = tempfile.mkdtemp(prefix="tokensafe_oumv_repo_")
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        with open(os.path.join(self.repo, "package.json"), "w", encoding="utf-8") as f:
            f.write('{"dependencies": {"react": "^18.0.0"}, "devDependencies": {"vite": "^5.0.0"}}')

    def tearDown(self):
        if self._orig_data is None:
            os.environ.pop("TOKENSAFE_DATA_DIR", None)
        else:
            os.environ["TOKENSAFE_DATA_DIR"] = self._orig_data

    def test_path_is_the_workspace_for_status(self):
        import json
        res = handle_tool_call("oumv", {"action": "status", "path": self.repo})
        self.assertNotIn("isError", res, res["content"][0]["text"])
        payload = json.loads(res["content"][0]["text"])
        self.assertEqual(payload["workspace"], self.repo)
        self.assertEqual(payload["detectedStack"]["workspace"], self.repo)
        self.assertEqual(payload["detectedStack"]["frontend"]["framework"], "react")

    def test_workspace_dir_is_honored(self):
        import json
        res = handle_tool_call("oumv", {"action": "status", "workspace_dir": os.path.join(self.repo, "package.json")})
        self.assertNotIn("isError", res, res["content"][0]["text"])
        self.assertEqual(json.loads(res["content"][0]["text"])["workspace"], self.repo)

    def test_missing_workspace_fails_closed(self):
        res = handle_tool_call("oumv", {"action": "status", "path": os.path.join(self.repo, "nope")})
        self.assertTrue(res.get("isError"))
        self.assertIn("not found or not a directory", res["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
