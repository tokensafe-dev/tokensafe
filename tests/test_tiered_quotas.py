"""
Unit tests for the Tiered Freemium Quota model (Guest 50k vs Community 500k),
Resilient Soft Degradation, and Installer Activation Funnel.
"""

import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

from src.license_manager import LicenseManager
from src.mcp_server import _dispatch_tool_call
from src.guard import TokenSafeGuard
from src.installer import AgentInstaller


class TestTieredQuotas(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = self._tmp.name
        self._patch_data_dir = mock.patch("src.license_manager.resolve_data_dir", return_value=self.data_dir)
        self._patch_data_dir.start()
        self.addCleanup(self._patch_data_dir.stop)
        self.addCleanup(self._tmp.cleanup)

    def tearDown(self):
        os.environ.pop("TOKENSAFE_LICENSE_KEY", None)

    def test_unauthenticated_fallback_to_guest(self):
        """When no license file exists, get_active_license returns guest tier with 50k limit."""
        with mock.patch.object(LicenseManager, "_resolve_active_license_record", return_value=(None, "")):
            tier, payload = LicenseManager.get_active_license()
        self.assertEqual(tier, "guest")
        self.assertEqual(payload["tier"], "guest")
        self.assertEqual(payload["max_tokens_monthly"], 50000)

    def test_legacy_anonymous_community_key_mapped_to_guest(self):
        """Legacy auto-issued key with community@tokensafe.dev is mapped to guest tier (50k limit)."""
        legacy_payload = {
            "tier": "free",
            "email": "community@tokensafe.dev",
            "max_tokens_monthly": 500000
        }
        with mock.patch.object(LicenseManager, "_resolve_active_license_record", return_value=(("key", legacy_payload), "key")):
            tier, payload = LicenseManager.get_active_license()
        self.assertEqual(tier, "guest")
        self.assertEqual(payload["tier"], "guest")
        self.assertEqual(payload["max_tokens_monthly"], 50000)
        self.assertTrue(payload.get("is_legacy_community"))

    def test_verified_user_community_key_retains_500k(self):
        """Personal account key with verified email gets free tier with 500,000 monthly quota."""
        personal_payload = {
            "tier": "free",
            "email": "developer@example.com",
            "max_tokens_monthly": 500000
        }
        with mock.patch.object(LicenseManager, "_resolve_active_license_record", return_value=(("key", personal_payload), "key")):
            tier, payload = LicenseManager.get_active_license()
        self.assertEqual(tier, "free")
        self.assertEqual(payload["tier"], "free")
        self.assertEqual(payload["max_tokens_monthly"], 500000)

    def test_guest_quota_warning_thresholds(self):
        """Tests that guest mode alerts appear at 80% and exceed at 50k with login CTA."""
        mock_tracker = mock.MagicMock()

        with mock.patch.object(LicenseManager, "get_active_license", return_value=("guest", {"tier": "guest", "max_tokens_monthly": 50000})):
            # Below 80% (35,000 tokens)
            mock_tracker.get_monthly_saved_tokens.return_value = 35000
            q1 = LicenseManager.check_quota(mock_tracker)
            self.assertFalse(q1["is_quota_exceeded"])
            self.assertIsNone(q1["warning"])
            self.assertEqual(q1["monthly_limit"], 50000)

            # At 84% (42,000 tokens) -> 80% warning notice
            mock_tracker.get_monthly_saved_tokens.return_value = 42000
            q2 = LicenseManager.check_quota(mock_tracker)
            self.assertFalse(q2["is_quota_exceeded"])
            self.assertIsNotNone(q2["warning"])
            self.assertIn("Guest mode is at 84%", q2["warning"])
            self.assertIn("tokensafe login", q2["warning"])

            # At 100% (50,000 tokens) -> quota exceeded alert
            mock_tracker.get_monthly_saved_tokens.return_value = 50000
            q3 = LicenseManager.check_quota(mock_tracker)
            self.assertTrue(q3["is_quota_exceeded"])
            self.assertIsNotNone(q3["warning"])
            self.assertIn("Guest limit reached", q3["warning"])
            self.assertIn("50,000/50,000", q3["warning"])
            self.assertIn("tokensafe login", q3["warning"])

    def test_guest_state_backup_floor(self):
        """Verifies guest_state.json records the highest saved tokens floor for the calendar month."""
        mock_tracker = mock.MagicMock()

        with mock.patch.object(LicenseManager, "get_active_license", return_value=("guest", {"tier": "guest", "max_tokens_monthly": 50000})):
            mock_tracker.get_monthly_saved_tokens.return_value = 45000
            q1 = LicenseManager.check_quota(mock_tracker)
            self.assertEqual(q1["monthly_saved"], 45000)

            # Check that guest_state.json was written
            gstate_file = os.path.join(self.data_dir, "guest_state.json")
            self.assertTrue(os.path.exists(gstate_file))
            with open(gstate_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["saved_tokens"], 45000)

            # Even if tracker returns 0 (e.g. metrics.db reset), floor preserves 45k
            mock_tracker.get_monthly_saved_tokens.return_value = 0
            q2 = LicenseManager.check_quota(mock_tracker)
            self.assertEqual(q2["monthly_saved"], 45000)

    def test_scoped_search_soft_degradation_on_quota(self):
        """When quota is exceeded, scoped_search does not crash and returns top 3 matches with notice."""
        quota_exceeded = {
            "tier": "guest",
            "is_quota_exceeded": True,
            "monthly_limit": 50000,
            "monthly_saved": 52000,
            "warning": "⚡ TokenSafe Quota Alert: Guest limit reached"
        }

        # Mock ScopedSearch.search returning markdown
        mock_search_res = {
            "success": True,
            "markdown": "### Results (3 matches):\n- match 1\n- match 2\n- match 3",
            "total_matches": 3
        }

        with mock.patch("src.license_manager.LicenseManager.check_quota", return_value=quota_exceeded), \
             mock.patch("src.scoped_search.ScopedSearch.search", return_value=mock_search_res) as m_search:
            res = _dispatch_tool_call("scoped_search", {"query": "test", "path": "."})

        self.assertNotIn("isError", res)
        text = res["content"][0]["text"]
        self.assertIn("Showing top 3 matches in degraded mode", text)
        self.assertIn("tokensafe login", text)
        self.assertIn("### Results", text)
        m_search.assert_called_once()
        self.assertEqual(m_search.call_args.kwargs["max_results"], 3)

    def test_security_guards_remain_active_on_quota_exceeded(self):
        """Security Guard inspect_file_write and inspect_command still block leaks when quota is exceeded."""
        quota_exceeded = {
            "tier": "guest",
            "is_quota_exceeded": True,
            "monthly_limit": 50000,
            "monthly_saved": 55000
        }

        with mock.patch("src.license_manager.LicenseManager.check_quota", return_value=quota_exceeded):
            # Attempt to commit private key
            guard_write = TokenSafeGuard.inspect_file_write(
                "/path/to/keys.py",
                "PRIVATE_KEY = '-----BEGIN RSA PRIVATE KEY-----'"
            )
            self.assertTrue(guard_write.blocked)

            # Attempt destructive shell command
            guard_cmd = TokenSafeGuard.inspect_command("rm -rf /")
            self.assertTrue(guard_cmd.blocked)

    def test_installer_activation_funnel_prompts(self):
        """Tests the installer maybe_prompt_account_login UX: Y launches login, n triggers nudge."""
        with mock.patch.dict(os.environ, {"TOKENSAFE_TEST_PROMPT": "1"}), \
             mock.patch("src.license_manager.LicenseManager.get_active_license", return_value=("guest", {"email": ""})), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch("sys.stdout.isatty", return_value=True), \
             mock.patch("sys.stdin.isatty", return_value=True):

            # Case 1: user presses Enter / Y -> calls cli_main(["login"])
            with mock.patch("sys.stdin.readline", return_value="y\n"), \
                 mock.patch("src.cli.main") as m_cli_main:
                AgentInstaller.maybe_prompt_account_login(context="install")
                m_cli_main.assert_called_once_with(["login"])

            # Case 2: user enters 'n' then accepts nudge 'y' -> calls cli_main(["login"])
            with mock.patch("sys.stdin.readline", side_effect=["n\n", "y\n"]), \
                 mock.patch("src.cli.main") as m_cli_main:
                AgentInstaller.maybe_prompt_account_login(context="install")
                m_cli_main.assert_called_once_with(["login"])

            # Case 3: user enters 'n' and declines nudge 'n' -> finishes in guest mode without error
            with mock.patch("sys.stdin.readline", side_effect=["n\n", "n\n"]), \
                 mock.patch("src.cli.main") as m_cli_main:
                AgentInstaller.maybe_prompt_account_login(context="install")
                m_cli_main.assert_not_called()


if __name__ == "__main__":
    unittest.main()
