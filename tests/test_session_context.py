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
                "max_results": 5,
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


if __name__ == "__main__":
    unittest.main()
