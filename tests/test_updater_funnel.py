import unittest
import os
import sys
import io
from unittest import mock

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.installer import AgentInstaller
from src.license_manager import LicenseManager


class TestUpdaterFunnel(unittest.TestCase):
    def test_update_prompts_guest_with_usage_and_accepts(self):
        quota_data = {"tier": "guest", "monthly_saved": 12345, "monthly_limit": 50000, "is_quota_exceeded": False}
        with mock.patch.dict(os.environ, {"TOKENSAFE_TEST_PROMPT": "1"}), \
             mock.patch("src.license_manager.LicenseManager.get_active_license", return_value=("guest", {"email": ""})), \
             mock.patch("src.license_manager.LicenseManager.check_quota", return_value=quota_data), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch("sys.stdout.isatty", return_value=True), \
             mock.patch("sys.stdin.isatty", return_value=True):

            captured_out = io.StringIO()
            with mock.patch("sys.stdout", captured_out), \
                 mock.patch("sys.stdin.readline", return_value="y\n"), \
                 mock.patch("src.cli.main") as m_cli_main:

                AgentInstaller.maybe_prompt_account_login(context="update")

                output = captured_out.getvalue()
                self.assertIn("TOKENSAFE UPDATED", output)
                self.assertIn("12,345 / 50,000 tokens used", output)
                self.assertIn("500,000 free tokens every month", output)
                m_cli_main.assert_called_once_with(["login"])

    def test_update_guest_declines_cleanly_without_nag(self):
        quota_data = {"tier": "guest", "monthly_saved": 30000, "monthly_limit": 50000, "is_quota_exceeded": False}
        with mock.patch.dict(os.environ, {"TOKENSAFE_TEST_PROMPT": "1"}), \
             mock.patch("src.license_manager.LicenseManager.get_active_license", return_value=("guest", {"email": ""})), \
             mock.patch("src.license_manager.LicenseManager.check_quota", return_value=quota_data), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch("sys.stdout.isatty", return_value=True), \
             mock.patch("sys.stdin.isatty", return_value=True):

            captured_out = io.StringIO()
            with mock.patch("sys.stdout", captured_out), \
                 mock.patch("sys.stdin.readline", return_value="n\n"), \
                 mock.patch("src.cli.main") as m_cli_main:

                AgentInstaller.maybe_prompt_account_login(context="update")

                output = captured_out.getvalue()
                self.assertIn("Continuing in Guest Mode (50,000 tokens/mo limit)", output)
                # Verify strict 1-step decline: NO second-chance "Are you sure?" prompt
                self.assertNotIn("Are you sure?", output)
                m_cli_main.assert_not_called()

    def test_update_skips_when_authenticated(self):
        with mock.patch.dict(os.environ, {"TOKENSAFE_TEST_PROMPT": "1"}), \
             mock.patch("src.license_manager.LicenseManager.get_active_license", return_value=("free", {"tier": "free", "email": "dev@example.com"})):

            captured_out = io.StringIO()
            with mock.patch("sys.stdout", captured_out), \
                 mock.patch("src.cli.main") as m_cli_main:

                AgentInstaller.maybe_prompt_account_login(context="update")

                output = captured_out.getvalue()
                self.assertEqual(output.strip(), "")
                m_cli_main.assert_not_called()

    def test_update_skips_non_interactive(self):
        with mock.patch.dict(os.environ, {"TOKENSAFE_TEST_PROMPT": "1"}), \
             mock.patch("src.license_manager.LicenseManager.get_active_license", return_value=("guest", {"email": ""})), \
             mock.patch("sys.stdin.isatty", return_value=False), \
             mock.patch("os.path.exists", return_value=False):

            captured_out = io.StringIO()
            with mock.patch("sys.stdout", captured_out), \
                 mock.patch("src.cli.main") as m_cli_main:

                AgentInstaller.maybe_prompt_account_login(context="update")

                output = captured_out.getvalue()
                self.assertIn("tokensafe login", output)
                self.assertIn("activate your Community license", output)
                self.assertNotIn("Activate your free 500k", output)
                m_cli_main.assert_not_called()

    def test_cli_check_update_shows_guest_quota_hint(self):
        quota_data = {"tier": "guest", "monthly_saved": 7500, "monthly_limit": 50000, "is_quota_exceeded": False}
        with mock.patch("src.version_checker.VersionChecker.check_version", return_value={"latest_version": "4.1.9", "update_available": False}), \
             mock.patch("src.version_checker.VersionChecker.get_banner", return_value=None), \
             mock.patch("src.license_manager.LicenseManager.get_active_license", return_value=("guest", {"email": ""})), \
             mock.patch("src.license_manager.LicenseManager.check_quota", return_value=quota_data):

            from src.cli import main as cli_main
            captured_out = io.StringIO()
            with mock.patch("sys.stdout", captured_out):
                cli_main(["check-update"])

            output = captured_out.getvalue()
            self.assertIn("Guest Mode", output)
            self.assertIn("7,500 / 50,000 tokens used", output)
            self.assertIn("tokensafe login", output)


if __name__ == "__main__":
    unittest.main()
