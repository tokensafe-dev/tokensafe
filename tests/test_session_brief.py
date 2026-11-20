"""Cold-start brief (project-gated God Nodes banner):
- CC SessionStart gains the banner ONLY for real project cwds (parity with
  the Antigravity SessionStart hook).
- Antigravity plain chats (no workspacePaths) skip the topology scan entirely
  (previously they scanned the plugin dir); opened folders behave as before.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src import metrics as metrics_mod


def _fresh_env(tmp: str):
    os.environ["TOKENSAFE_DATA_DIR"] = tmp
    os.environ["TOKENSAFE_DB_PATH"] = os.path.join(tmp, "metrics.db")
    os.environ["TOKENSAFE_TELEMETRY_DISABLED"] = "1"
    metrics_mod._INITIALIZED_METRICS_DBS.clear()


class TestIsProjectRoot(unittest.TestCase):
    def test_git_dir_qualifies(self):
        from src.paths import is_project_root
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, ".git"))
            self.assertTrue(is_project_root(tmp))

    def test_manifest_file_qualifies(self):
        from src.paths import is_project_root
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "package.json"), "w") as f:
                f.write("{}")
            self.assertTrue(is_project_root(tmp))

    def test_plain_dir_does_not_qualify(self):
        from src.paths import is_project_root
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(is_project_root(tmp))

    def test_home_does_not_qualify(self):
        from src.paths import is_project_root
        self.assertFalse(is_project_root(os.path.expanduser("~")))

    def test_missing_dir_does_not_qualify(self):
        from src.paths import is_project_root
        self.assertFalse(is_project_root("/nonexistent/tokensafe-probe"))

    def test_data_dir_does_not_qualify(self):
        from src.paths import is_project_root, resolve_data_dir
        self.assertFalse(is_project_root(resolve_data_dir()))


class TestClaudeCodeSessionStartBrief(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="tokensafe_brief_cc_")
        _fresh_env(self._tmp)
        self.addCleanup(os.environ.pop, "TOKENSAFE_DATA_DIR", None)
        self.addCleanup(os.environ.pop, "TOKENSAFE_DB_PATH", None)
        self.addCleanup(os.environ.pop, "TOKENSAFE_TELEMETRY_DISABLED", None)

    def _run(self, cwd: str):
        from hooks import claude_code_hook
        with patch("src.graph_engine.GraphEngine.get_instance") as mock_get_inst, \
             patch("src.license_manager.LicenseManager.check_quota",
                   return_value={"is_quota_exceeded": False}), \
             patch("src.mistakes.MistakesLedger.get_guard_prompt", return_value=None), \
             patch("src.version_checker.VersionChecker.notify_os_update_background"):
            mock_get_inst.return_value.format_topology_banner.return_value = (
                "[TokenSafe Architecture Map | Core Hubs: src/a.py (10 deps)]"
            )
            res = claude_code_hook.handle("SessionStart", {"session_id": "brief_cc", "cwd": cwd})
            return res, mock_get_inst

    def test_banner_added_for_project_cwd(self):
        proj = os.path.join(self._tmp, "proj")
        os.makedirs(os.path.join(proj, ".git"))
        res, mock_get_inst = self._run(proj)
        ctx = res["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Mandatory Directives", ctx)
        self.assertIn("TokenSafe Architecture Map", ctx)
        self.assertIn("Core God Nodes and dependency hubs mapped above", ctx)
        mock_get_inst.assert_called_once_with(proj)

    def test_plain_chat_cwd_stays_banner_free(self):
        plain = os.path.join(self._tmp, "plain")
        os.makedirs(plain)
        res, mock_get_inst = self._run(plain)
        ctx = res["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Mandatory Directives", ctx)
        self.assertNotIn("TokenSafe Architecture Map", ctx)
        mock_get_inst.assert_not_called()


class TestAntigravityPlainChatGate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="tokensafe_brief_agy_")
        _fresh_env(self._tmp)
        self.addCleanup(os.environ.pop, "TOKENSAFE_DATA_DIR", None)
        self.addCleanup(os.environ.pop, "TOKENSAFE_DB_PATH", None)
        self.addCleanup(os.environ.pop, "TOKENSAFE_TELEMETRY_DISABLED", None)

    def _run(self, payload: dict):
        from hooks import session_start
        with patch("src.graph_engine.GraphEngine.get_instance") as mock_get_inst, \
             patch("src.license_manager.LicenseManager.check_quota",
                   return_value={"is_quota_exceeded": False}), \
             patch("src.license_manager.LicenseManager.ensure_community_license"), \
             patch("src.version_checker.VersionChecker.notify_os_update_background"), \
             patch("sys.stdin", io.StringIO(json.dumps(payload))), \
             patch("sys.stdout", new_callable=io.StringIO) as out:
            mock_get_inst.return_value.format_topology_banner.return_value = (
                "[TokenSafe Architecture Map | Core Hubs: src/a.py (10 deps)]"
            )
            session_start.main()
            return out.getvalue(), mock_get_inst

    def test_plain_chat_skips_topology_scan(self):
        payload = {"conversationId": "chat_plain"}  # no workspacePaths
        output, mock_get_inst = self._run(payload)
        self.assertIn("Mandatory Directives", output)
        self.assertNotIn("TokenSafe Architecture Map", output)
        mock_get_inst.assert_not_called()

    def test_opened_folder_keeps_banner(self):
        payload = {"conversationId": "chat_folder", "workspacePaths": [self._tmp]}
        output, mock_get_inst = self._run(payload)
        self.assertIn("TokenSafe Architecture Map", output)
        mock_get_inst.assert_called_once()


if __name__ == "__main__":
    unittest.main()
