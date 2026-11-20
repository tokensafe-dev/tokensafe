"""Stale-file edit guard: hook-level behavior for Claude Code + Antigravity/ZCode.

The guard must ask (never deny) only when the tracked file REALLY changed on
disk after the model last saw it, stay silent on fresh targets, and honor the
TOKENSAFE_STALE_GUARD=0 kill switch. Project files live OUTSIDE the data dir
(the state store skips everything under the TokenSafe data root).
"""
import os
import sys
import shutil
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src import metrics as metrics_mod
from src.metrics import MetricsTracker
from src.file_state import record_seen, check_stale


class TestClaudeCodeStaleGuard(unittest.TestCase):
    def setUp(self):
        self._data = tempfile.mkdtemp(prefix="tokensafe_stale_cc_data_")
        self._proj = tempfile.mkdtemp(prefix="tokensafe_stale_cc_proj_")
        os.environ["TOKENSAFE_DATA_DIR"] = self._data
        os.environ["TOKENSAFE_DB_PATH"] = os.path.join(self._data, "metrics.db")
        os.environ["TOKENSAFE_TELEMETRY_DISABLED"] = "1"
        metrics_mod._INITIALIZED_METRICS_DBS.clear()
        self.addCleanup(shutil.rmtree, self._data, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self._proj, ignore_errors=True)
        for key in ("TOKENSAFE_DATA_DIR", "TOKENSAFE_DB_PATH",
                    "TOKENSAFE_TELEMETRY_DISABLED", "TOKENSAFE_STALE_GUARD"):
            self.addCleanup(os.environ.pop, key, None)

    def _project_file(self, content: str) -> str:
        p = os.path.join(self._proj, "app.py")
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p

    def test_stale_edit_asks_with_reason(self):
        from hooks import claude_code_hook
        p = self._project_file("print(1)")
        record_seen(p)
        self._project_file("print(2)  # external linter reformat")
        payload = {"session_id": "stale_cc_1", "tool_name": "Edit", "tool_input": {"file_path": p}}
        res = claude_code_hook.handle("PreToolUse", payload)
        self.assertEqual(res["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertIn("changed on disk", res["hookSpecificOutput"]["permissionDecisionReason"])
        tracker = MetricsTracker(db_path=os.path.join(self._data, "metrics.db"))
        events = [e for e in tracker.get_recent_events(limit=20) if e.get("event_type") == "STALE_EDIT_ASK"]
        self.assertTrue(events, "STALE_EDIT_ASK event not recorded")

    def test_fresh_edit_passes_through(self):
        from hooks import claude_code_hook
        p = self._project_file("print(1)")
        record_seen(p)
        payload = {"session_id": "stale_cc_2", "tool_name": "Edit", "tool_input": {"file_path": p}}
        self.assertEqual(claude_code_hook.handle("PreToolUse", payload), {})

    def test_never_seen_file_passes_through(self):
        from hooks import claude_code_hook
        p = self._project_file("print(1)")
        payload = {"session_id": "stale_cc_3", "tool_name": "Edit", "tool_input": {"file_path": p}}
        self.assertEqual(claude_code_hook.handle("PreToolUse", payload), {})

    def test_kill_switch_disables_ask(self):
        from hooks import claude_code_hook
        p = self._project_file("print(1)")
        record_seen(p)
        self._project_file("print(2)")
        os.environ["TOKENSAFE_STALE_GUARD"] = "0"
        payload = {"session_id": "stale_cc_4", "tool_name": "MultiEdit", "tool_input": {"file_path": p}}
        self.assertEqual(claude_code_hook.handle("PreToolUse", payload), {})

    def test_post_tool_use_read_records_state(self):
        from hooks import claude_code_hook
        p = self._project_file("print(1)")
        payload = {
            "session_id": "trk_cc_1",
            "tool_name": "Read",
            "tool_input": {"file_path": p},
            "tool_response": "print(1)",
        }
        self.assertEqual(claude_code_hook.handle("PostToolUse", payload), {})
        self._project_file("print(2)")
        self.assertIsNotNone(check_stale(p), "PostToolUse Read did not snapshot the file state")

    def test_post_tool_use_edit_records_state(self):
        from hooks import claude_code_hook
        p = self._project_file("print(1)")
        payload = {
            "session_id": "trk_cc_2",
            "tool_name": "Edit",
            "tool_input": {"file_path": p},
            "tool_response": "ok",
        }
        claude_code_hook.handle("PostToolUse", payload)
        self.assertIsNone(check_stale(p), "post-edit state must match disk")


class TestAntigravityStaleGuard(unittest.TestCase):
    def setUp(self):
        self._data = tempfile.mkdtemp(prefix="tokensafe_stale_agy_data_")
        self._proj = tempfile.mkdtemp(prefix="tokensafe_stale_agy_proj_")
        os.environ["TOKENSAFE_DATA_DIR"] = self._data
        os.environ["TOKENSAFE_DB_PATH"] = os.path.join(self._data, "metrics.db")
        os.environ["TOKENSAFE_TELEMETRY_DISABLED"] = "1"
        metrics_mod._INITIALIZED_METRICS_DBS.clear()
        self.addCleanup(shutil.rmtree, self._data, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self._proj, ignore_errors=True)
        for key in ("TOKENSAFE_DATA_DIR", "TOKENSAFE_DB_PATH",
                    "TOKENSAFE_TELEMETRY_DISABLED", "TOKENSAFE_STALE_GUARD"):
            self.addCleanup(os.environ.pop, key, None)

    def _payload(self, path: str, conversation_id: str) -> dict:
        return {
            "toolCall": {
                "name": "edit_file",
                "args": {"Path": path, "TargetFile": path, "Replacement": "x = 1"},
            },
            "conversationId": conversation_id,
            "workspacePaths": [self._proj],
        }

    def test_stale_edit_force_ask(self):
        from hooks import pre_tool_use
        p = os.path.join(self._proj, "app.py")
        with open(p, "w", encoding="utf-8") as f:
            f.write("print(1)")
        record_seen(p)
        with open(p, "w", encoding="utf-8") as f:
            f.write("print(2)")
        with patch("src.license_manager.LicenseManager.check_quota",
                   return_value={"is_quota_exceeded": False}):
            res = pre_tool_use._handle(self._payload(p, "stale_agy_1"))
        self.assertEqual(res.get("decision"), "force_ask")
        self.assertIn("changed on disk", res.get("reason", ""))

    def test_fresh_edit_allow(self):
        from hooks import pre_tool_use
        p = os.path.join(self._proj, "app.py")
        with open(p, "w", encoding="utf-8") as f:
            f.write("print(1)")
        record_seen(p)
        with patch("src.license_manager.LicenseManager.check_quota",
                   return_value={"is_quota_exceeded": False}):
            res = pre_tool_use._handle(self._payload(p, "stale_agy_2"))
        self.assertEqual(res.get("decision"), "allow")

    def test_kill_switch(self):
        from hooks import pre_tool_use
        p = os.path.join(self._proj, "app.py")
        with open(p, "w", encoding="utf-8") as f:
            f.write("print(1)")
        record_seen(p)
        with open(p, "w", encoding="utf-8") as f:
            f.write("print(2)")
        os.environ["TOKENSAFE_STALE_GUARD"] = "0"
        with patch("src.license_manager.LicenseManager.check_quota",
                   return_value={"is_quota_exceeded": False}):
            res = pre_tool_use._handle(self._payload(p, "stale_agy_3"))
        self.assertNotEqual(res.get("decision"), "force_ask")


if __name__ == "__main__":
    unittest.main()
