import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import time
import tempfile
import unittest
from unittest.mock import patch

from src.mistakes import MistakesLedger


class TestMistakesTeamSync(unittest.TestCase):
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

    def test_hybrid_escalation_policy(self):
        project_root = self.tmp_dir.name
        cmd = "npm run test:e2e"

        # 0 failures -> no warning
        is_blocked0, reason0 = MistakesLedger.should_force_ask(cmd, action_type="command", project_root=project_root)
        self.assertFalse(is_blocked0)
        self.assertEqual(reason0, "")

        # 1st failure -> Advisory warning
        MistakesLedger.record_failure(summary="Jest timeout in e2e suite", source="command", details=cmd, project_root=project_root)
        is_blocked1, reason1 = MistakesLedger.should_force_ask(cmd, action_type="command", project_root=project_root)
        self.assertFalse(is_blocked1, "1st failure must NOT block (advisory only)")
        self.assertIn("Advisory", reason1)
        self.assertIn("Jest timeout", reason1)

        # 2nd failure -> Still advisory warning
        MistakesLedger.record_failure(summary="Jest timeout in e2e suite (attempt 2)", source="command", details=cmd, project_root=project_root)
        is_blocked2, reason2 = MistakesLedger.should_force_ask(cmd, action_type="command", project_root=project_root)
        self.assertFalse(is_blocked2, "2nd failure must NOT block (advisory only)")
        self.assertIn("Advisory", reason2)

        # 3rd failure -> Escalation to force_ask!
        MistakesLedger.record_failure(summary="Jest timeout in e2e suite (attempt 3)", source="command", details=cmd, project_root=project_root)
        is_blocked3, reason3 = MistakesLedger.should_force_ask(cmd, action_type="command", project_root=project_root)
        self.assertTrue(is_blocked3, "3rd failure must escalate to force_ask")
        self.assertIn("Dead-End Loop", reason3)

    def test_window_expiration(self):
        project_root = self.tmp_dir.name
        cmd = "pytest tests/integration"

        # Record 3 failures with timestamp 40 minutes ago
        old_time = time.time() - 2400
        with patch("time.time", return_value=old_time):
            for i in range(3):
                MistakesLedger.record_failure(summary=f"Old error {i}", source="test", details=cmd, project_root=project_root)

        # Check now (current time): older than 30-min window, should not block
        is_blocked, reason = MistakesLedger.should_force_ask(cmd, action_type="command", project_root=project_root)
        self.assertFalse(is_blocked)
        self.assertEqual(reason, "")

    @patch("src.team_cache.TeamCache.put")
    def test_team_cache_broadcast_with_secret_redaction(self, mock_team_put):
        project_root = self.tmp_dir.name
        secret_val = "sk-ant-" + "x" * 45
        sensitive_summary = f"Failed database connection with token {secret_val}"

        entry = MistakesLedger.record_failure(
            summary=sensitive_summary,
            file_path="src/db.py",
            source="exception",
            details="Traceback...",
            project_root=project_root
        )

        self.assertIsNotNone(entry)
        mock_team_put.assert_called_once()
        args, kwargs = mock_team_put.call_args
        cache_type, key, payload_json = args[0], args[1], args[2]

        self.assertEqual(cache_type, "TEAM_MISTAKE")
        self.assertNotIn(secret_val, payload_json)
        self.assertIn("MOCK_ANTHROPIC_KEY_ANCHOR", payload_json)

    def test_get_guard_prompt(self):
        project_root = self.tmp_dir.name
        MistakesLedger.record_failure(summary="Circular import in service.py", file_path="service.py", project_root=project_root)
        MistakesLedger.record_failure(summary="Missing redis host env var", file_path="config.py", project_root=project_root)

        prompt = MistakesLedger.get_guard_prompt(project_root=project_root)
        self.assertIsNotNone(prompt)
        self.assertIn("Anti-Stuck Guard", prompt)
        self.assertIn("Circular import", prompt)
        self.assertIn("Missing redis host", prompt)

    @patch("src.team_cache.TeamCache.list_entries")
    def test_team_mistakes_merge_and_ttl_cache(self, mock_list_entries):
        import json
        MistakesLedger._team_mistakes_cache = []
        MistakesLedger._team_mistakes_fetched_at = 0.0

        mock_list_entries.return_value = [
            {
                "key_hash": "abc1",
                "value": json.dumps({"file": "docker-compose.yml", "summary": "Postgres port conflict"}),
            },
            {
                "key_hash": "abc2",
                "value": json.dumps({"file": "service.py", "summary": "Circular import in service.py"}),  # duplicate with local
            },
            {
                "key_hash": "abc3",
                "value": json.dumps({"file": "auth.ts", "summary": "Clerk key expired"}),
            }
        ]

        project_root = self.tmp_dir.name
        # Record local mistake
        MistakesLedger.record_failure(summary="Circular import in service.py", file_path="service.py", project_root=project_root)

        # 1. Fetch team mistakes directly
        team_items = MistakesLedger.get_team_mistakes()
        self.assertEqual(len(team_items), 3)
        mock_list_entries.assert_called_once()

        # 2. Second call should hit in-memory TTL cache (not call API again)
        team_items_cached = MistakesLedger.get_team_mistakes()
        self.assertEqual(len(team_items_cached), 3)
        self.assertEqual(mock_list_entries.call_count, 1)

        # 3. Guard prompt should merge local + team (deduplicating circular import)
        guard = MistakesLedger.get_guard_prompt(project_root=project_root)
        self.assertIsNotNone(guard)
        self.assertIn("Circular import in service.py", guard)
        self.assertIn("team: docker-compose.yml: Postgres port conflict", guard)
        self.assertIn("team: auth.ts: Clerk key expired", guard)
        # Should not have duplicate "team: service.py: Circular import"
        self.assertNotIn("team: service.py: Circular import", guard)


if __name__ == "__main__":
    unittest.main()
