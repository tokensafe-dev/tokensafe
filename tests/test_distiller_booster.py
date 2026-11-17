"""
Unit tests for the Cloud Prompt Booster: consent flag semantics, explicit
paid-tier gating (guest fix), and egress hygiene (secret redaction, provider
key in header, egress counter).
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.distiller import UniversalDistiller
from src import egress_counter
from src.license_manager import LicenseManager


class BoosterFlagTestCase(unittest.TestCase):
    """Data-dir isolation + license mocking for flag semantics tests."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ts_booster_test_")
        self._prev_dir = os.environ.get("TOKENSAFE_DATA_DIR")
        self._prev_env = os.environ.get("TOKENSAFE_CLOUD_DISTILL")
        self._prev_selfhost = os.environ.get("TOKENSAFE_SELF_HOSTED")
        self._prev_gemini = os.environ.get("GEMINI_API_KEY")
        os.environ["TOKENSAFE_DATA_DIR"] = self.tmp
        os.environ.pop("TOKENSAFE_CLOUD_DISTILL", None)
        os.environ.pop("TOKENSAFE_SELF_HOSTED", None)
        os.environ.pop("GEMINI_API_KEY", None)

    def tearDown(self):
        if self._prev_dir is None:
            os.environ.pop("TOKENSAFE_DATA_DIR", None)
        else:
            os.environ["TOKENSAFE_DATA_DIR"] = self._prev_dir
        if self._prev_env is not None:
            os.environ["TOKENSAFE_CLOUD_DISTILL"] = self._prev_env
        if self._prev_selfhost is not None:
            os.environ["TOKENSAFE_SELF_HOSTED"] = self._prev_selfhost
        if self._prev_gemini is not None:
            os.environ["GEMINI_API_KEY"] = self._prev_gemini

    def _tier(self, tier):
        return patch.object(LicenseManager, "get_active_license", return_value=(tier, {}))

    def test_paid_tiers_constant(self):
        self.assertEqual(UniversalDistiller.PAID_TIERS, ("solo", "team", "enterprise"))

    def test_guest_hard_off_even_with_byok_keys(self):
        # The guest tier split (v4.1.10) must not open BYOK distillation:
        # explicit paid set, not a != "free" string check.
        os.environ["TOKENSAFE_SELF_HOSTED"] = "1"
        os.environ["GEMINI_API_KEY"] = "fake-guest-key-1234567890"
        with self._tier("guest"):
            self.assertEqual(UniversalDistiller.get_configured_providers(), {})
            enabled, explicitly_set = UniversalDistiller.cloud_booster_enabled("guest")
            self.assertFalse(enabled)

    def test_free_hard_off(self):
        with self._tier("free"):
            self.assertEqual(UniversalDistiller.get_configured_providers(), {})
            self.assertFalse(UniversalDistiller.cloud_booster_enabled("free")[0])

    def test_paid_unset_defaults_on_with_disclosure_marker(self):
        with self._tier("solo"):
            providers = UniversalDistiller.get_configured_providers()
            self.assertIn("cloud", providers)
            self.assertEqual(providers["cloud"]["name"], "Cloud Prompt Booster")
            enabled, explicitly_set = UniversalDistiller.cloud_booster_enabled("solo")
            self.assertTrue(enabled)
            self.assertFalse(explicitly_set)  # CLI uses this to show the disclosure

    def test_paid_opt_out_via_config(self):
        self.assertTrue(UniversalDistiller.write_config_value("cloud_distill", False))
        with self._tier("solo"):
            self.assertEqual(UniversalDistiller.get_configured_providers(), {})
            enabled, explicitly_set = UniversalDistiller.cloud_booster_enabled("solo")
            self.assertFalse(enabled)
            self.assertTrue(explicitly_set)

    def test_paid_opt_in_via_config(self):
        self.assertTrue(UniversalDistiller.write_config_value("cloud_distill", True))
        with self._tier("team"):
            self.assertIn("cloud", UniversalDistiller.get_configured_providers())

    def test_env_override_beats_config(self):
        self.assertTrue(UniversalDistiller.write_config_value("cloud_distill", True))
        os.environ["TOKENSAFE_CLOUD_DISTILL"] = "0"
        with self._tier("solo"):
            enabled, explicitly_set = UniversalDistiller.cloud_booster_enabled("solo")
            self.assertFalse(enabled)
            self.assertTrue(explicitly_set)

    def test_config_write_preserves_other_keys(self):
        config_path = os.path.join(self.tmp, "config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"preferred_provider": "gemini"}, f)
        self.assertTrue(UniversalDistiller.write_config_value("cloud_distill", False))
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["preferred_provider"], "gemini")
        self.assertIs(data["cloud_distill"], False)


class EgressHygieneTestCase(unittest.TestCase):
    """Redaction + key header + UA on the outbound distill path."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ts_egress_test_")
        self._prev_dir = os.environ.get("TOKENSAFE_DATA_DIR")
        os.environ["TOKENSAFE_DATA_DIR"] = self.tmp

    def tearDown(self):
        if self._prev_dir is None:
            os.environ.pop("TOKENSAFE_DATA_DIR", None)
        else:
            os.environ["TOKENSAFE_DATA_DIR"] = self._prev_dir

    @staticmethod
    def _capture_request():
        captured = {}

        class _FakeResp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(
                    {"success": True, "distilled": "(DEBUG auth.py validate_token [jwt] \"verify jwt\")"}
                ).encode("utf-8")

        def _fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResp()

        return captured, _fake_urlopen

    def test_call_cloud_distill_redacts_secrets(self):
        captured, fake = self._capture_request()
        # Assembled at runtime: a contiguous sk_live_ literal in source trips
        # GitHub push protection even though this key is purely synthetic.
        fake_secret = "sk_" + "live_" + "9f3b1c77e2b34d08a91c6d5e7f0b2a3d"
        with patch("urllib.request.urlopen", side_effect=fake):
            res = UniversalDistiller._call_cloud_distill(
                f"Fix the billing code, the key {fake_secret} broke "
                "validate_token in auth.py so it verifies jwt properly",
                lic_key="TS2.test.solo",
            )
        self.assertIsNotNone(res)
        sent_text = captured["body"]["prompt"]
        self.assertNotIn("sk_live_9f3b", sent_text)
        self.assertIn("MOCK", sent_text.upper())
        self.assertIn("auth.py", sent_text)

    def test_call_cloud_distill_counts_egress_and_ua_version(self):
        from src.version_checker import CURRENT_VERSION

        captured, fake = self._capture_request()
        before = egress_counter.today_counts()["booster"]
        with patch("urllib.request.urlopen", side_effect=fake):
            UniversalDistiller._call_cloud_distill("prompt text long enough", lic_key="TS2.test.solo")
        self.assertEqual(egress_counter.today_counts()["booster"], before + 1)
        ua = captured["headers"].get("User-agent") or captured["headers"].get("User-Agent")
        self.assertEqual(ua, f"TokenSafe/{CURRENT_VERSION}")

    def test_call_gemini_key_in_header_not_url(self):
        captured, fake = self._capture_request()
        conf = {
            "provider": "gemini",
            "model": "gemini-flash-lite-latest",
            "api_key": "FAKE-GEMINI-KEY-abcdef123456",
            "timeout": 2.5,
            "max_tokens": 60,
            "endpoint": UniversalDistiller.PROVIDERS["gemini"]["endpoint"],
        }
        with patch("urllib.request.urlopen", side_effect=fake):
            UniversalDistiller._call_gemini(conf, "fix validate_token in auth.py")
        self.assertNotIn("key=", captured["url"])
        self.assertEqual(captured["headers"].get("X-goog-api-key"), "FAKE-GEMINI-KEY-abcdef123456")


class EgressCounterTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ts_egressctr_test_")
        self._prev_dir = os.environ.get("TOKENSAFE_DATA_DIR")
        os.environ["TOKENSAFE_DATA_DIR"] = self.tmp

    def tearDown(self):
        if self._prev_dir is None:
            os.environ.pop("TOKENSAFE_DATA_DIR", None)
        else:
            os.environ["TOKENSAFE_DATA_DIR"] = self._prev_dir

    def test_increment_and_today_counts(self):
        self.assertEqual(egress_counter.today_counts(), {"booster": 0, "telemetry": 0})
        egress_counter.increment("booster")
        egress_counter.increment("booster")
        egress_counter.increment("telemetry")
        egress_counter.increment("bogus-kind")  # ignored
        counts = egress_counter.today_counts()
        self.assertEqual(counts["booster"], 2)
        self.assertEqual(counts["telemetry"], 1)

    def test_persists_across_reload(self):
        egress_counter.increment("booster")
        path = os.path.join(self.tmp, "egress.json")
        self.assertTrue(os.path.exists(path))
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        today_key = sorted(data.keys())[-1]
        self.assertEqual(data[today_key]["booster"], 1)


if __name__ == "__main__":
    unittest.main()
