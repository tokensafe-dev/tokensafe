"""Unit tests for CLI login (RFC 8628 device flow), hardware machine fingerprinting, and device token refresh."""

import os
import sys
import json
import time
import unittest
from unittest import mock
import tempfile
import shutil

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.license_manager import LicenseManager


class TestCliLogin(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.patcher = mock.patch("src.license_manager.resolve_data_dir", return_value=self.test_dir)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_machine_fingerprint_stability(self):
        # 1. First probe generates and writes .machine_id
        fp1 = LicenseManager.get_machine_fingerprint()
        self.assertEqual(len(fp1), 32)
        self.assertTrue(all(c in "0123456789abcdef" for c in fp1))

        mid_path = os.path.join(self.test_dir, ".machine_id")
        self.assertTrue(os.path.exists(mid_path))

        # 2. Second probe reuses cached .machine_id
        fp2 = LicenseManager.get_machine_fingerprint()
        self.assertEqual(fp1, fp2)

    def test_save_and_retrieve_device_token(self):
        lic_file = os.path.join(self.test_dir, "license.json")
        with mock.patch("src.license_manager.LicenseManager._candidate_license_files", return_value=[lic_file]):
            # Generate a valid test license
            test_key = LicenseManager.generate_license(tier="free", email="dev@example.com", duration_days=30, quota_monthly=500_000)
            ok, msg = LicenseManager.save_license(test_key, device_token="ts_dev_1234567890abcdef")
            self.assertTrue(ok)

            saved_tok = LicenseManager.get_device_token()
            self.assertEqual(saved_tok, "ts_dev_1234567890abcdef")

    def test_refresh_device_license_success(self):
        lic_file = os.path.join(self.test_dir, "license.json")
        with mock.patch("src.license_manager.LicenseManager._candidate_license_files", return_value=[lic_file]):
            fresh_key = LicenseManager.generate_license(tier="free", email="dev@example.com", duration_days=30, quota_monthly=500_000)
            LicenseManager.save_license(fresh_key, device_token="ts_dev_mock_token")

            # Mock server response for /api/auth/device/refresh
            renewed_key = LicenseManager.generate_license(tier="free", email="dev@example.com", duration_days=30, quota_monthly=500_000)
            mock_resp = mock.MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = json.dumps({"success": True, "license_key": renewed_key}).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp

            with mock.patch("urllib.request.urlopen", return_value=mock_resp) as m_urlopen:
                ok, msg = LicenseManager.refresh_device_license()
                self.assertTrue(ok)
                self.assertIn("Successfully activated", msg)
                self.assertEqual(LicenseManager.get_active_license_key(), renewed_key)
            # SIGSEGV regression: refresh can fire from background sync paths —
            # must reuse the shared SSL context, never build one lazily in-thread.
            from src.ssl_context import shared_ssl_context
            self.assertIs(m_urlopen.call_args.kwargs.get("context"), shared_ssl_context())

    @mock.patch("sys.stdin.isatty", return_value=False)
    @mock.patch("sys.stdout.isatty", return_value=False)
    def test_installer_non_interactive_never_prompts_browser(self, mock_stdout, mock_stdin):
        from src.installer import AgentInstaller
        with mock.patch("src.license_manager.LicenseManager.get_active_license", return_value=("free", {"email": "community@tokensafe.dev"})):
            # Should complete without error or blocking
            AgentInstaller.maybe_prompt_account_login(context="install")
            AgentInstaller.maybe_prompt_account_login(context="update")

    @mock.patch("webbrowser.open")
    def test_cli_login_command_flow(self, mock_webopen):
        from src.cli import main as cli_main

        code_resp = mock.MagicMock()
        code_resp.read.return_value = json.dumps({
            "success": True,
            "device_code": "dev_code_123",
            "user_code": "ABCD-1234",
            "verification_uri": "https://tokensafe.dev/activate",
            "verification_uri_complete": "https://tokensafe.dev/activate?code=ABCD-1234",
            "interval": 0.01
        }).encode("utf-8")
        code_resp.__enter__.return_value = code_resp

        new_key = LicenseManager.generate_license(tier="free", email="alex@company.com", duration_days=30, quota_monthly=500_000)
        poll_resp = mock.MagicMock()
        poll_resp.read.return_value = json.dumps({
            "status": "approved",
            "license_key": new_key,
            "device_token": "ts_dev_xyz",
            "email": "alex@company.com"
        }).encode("utf-8")
        poll_resp.__enter__.return_value = poll_resp

        def urlopen_side_effect(req, *args, **kwargs):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "/api/auth/device/code" in url:
                return code_resp
            elif "/api/auth/device/poll" in url:
                return poll_resp
            return code_resp

        lic_file = os.path.join(self.test_dir, "license.json")
        with mock.patch("urllib.request.urlopen", side_effect=urlopen_side_effect), \
             mock.patch("src.license_manager.LicenseManager._candidate_license_files", return_value=[lic_file]), \
             mock.patch("src.telemetry_sync.TelemetrySync.sync", return_value={"success": True, "synced_events": 5}):
            
            cli_main(["login"])

            self.assertEqual(LicenseManager.get_active_license_key(), new_key)
            self.assertEqual(LicenseManager.get_device_token(), "ts_dev_xyz")
            mock_webopen.assert_called_once()

    def test_cli_logout_command_flow(self):
        from src.cli import main as cli_main
        import src.cli as cli_module
        import src.license_manager as lm

        test_lic = os.path.join(self.test_dir, "data", "license.json")
        test_global = os.path.join(self.test_dir, "global", "license.json")
        os.makedirs(os.path.dirname(test_lic), exist_ok=True)
        os.makedirs(os.path.dirname(test_global), exist_ok=True)

        with mock.patch.object(lm, "LICENSE_FILE", test_lic), \
             mock.patch.object(lm, "GLOBAL_LICENSE_FILE", test_global):
            # Setup an active license file in isolated test dir
            test_key = LicenseManager.generate_license(tier="free", email="dev@test.com", duration_days=30)
            LicenseManager.save_license(test_key, device_token="ts_dev_active_123")

            self.assertTrue(os.path.exists(test_lic))

            unlink_resp = mock.MagicMock()
            unlink_resp.read.return_value = b'{"success": true}'
            unlink_resp.__enter__.return_value = unlink_resp

            with mock.patch("urllib.request.urlopen", return_value=unlink_resp):
                cli_main(["logout"])

            # File is removed and device is logged out
            self.assertFalse(os.path.exists(test_lic))
            self.assertFalse(os.path.exists(test_global))
            self.assertEqual(LicenseManager.get_device_token(), "")


if __name__ == "__main__":
    unittest.main()
