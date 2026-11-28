"""Per-tier OUMV profile: observation free, write methods licensed.

The read-only deployment profile is a capability boundary, not a quota: the
adapters drop `mutate`/`preview`/`verify`/`build` from the registry entirely, so
the engine refuses the same four tools for guest/free instead of letting a free
license drive a hand-mounted read-write runtime.
"""

import os
import unittest
from unittest.mock import patch

from src import mcp_server, oumv_policy
from src.auto_inject import backend_env
from src.license_manager import LicenseManager


def _license(tier, **payload):
    return (tier, {"tier": tier, **payload})


class ProfileResolutionTest(unittest.TestCase):
    def test_free_and_guest_are_read_only(self):
        for tier in ("guest", "free", ""):
            self.assertEqual(oumv_policy.profile_for(tier), oumv_policy.PROFILE_READ_ONLY)

    def test_paid_tiers_are_full(self):
        for tier in ("solo", "team", "enterprise"):
            self.assertEqual(oumv_policy.profile_for(tier), oumv_policy.PROFILE_FULL)

    def test_env_switch_wins_over_a_paid_tier(self):
        with patch.dict(os.environ, {"TOKENSAFE_OUMV_READ_ONLY": "1"}):
            self.assertEqual(oumv_policy.profile_for("team"), oumv_policy.PROFILE_READ_ONLY)

    def test_falsy_env_does_not_unlock_a_free_tier(self):
        with patch.dict(os.environ, {"TOKENSAFE_OUMV_READ_ONLY": "0"}):
            self.assertEqual(oumv_policy.profile_for("free"), oumv_policy.PROFILE_READ_ONLY)

    def test_signed_profile_overrides_the_tier_map(self):
        self.assertEqual(
            oumv_policy.profile_for("free", {"oumv_profile": "full"}),
            oumv_policy.PROFILE_FULL,
        )
        self.assertEqual(
            oumv_policy.profile_for("team", {"oumv_profile": "read_only"}),
            oumv_policy.PROFILE_READ_ONLY,
        )

    def test_spawn_env_sets_the_flags_only_for_read_only(self):
        with patch.object(LicenseManager, "get_active_license", return_value=_license("free")):
            env = oumv_policy.spawn_env({})
            self.assertEqual(env.get("TOKENSAFE_OUMV_READ_ONLY"), "1")
            self.assertEqual(env.get("OUMV_READ_ONLY"), "1")
        with patch.object(LicenseManager, "get_active_license", return_value=_license("solo")):
            env = oumv_policy.spawn_env({})
            self.assertNotIn("TOKENSAFE_OUMV_READ_ONLY", env)
            self.assertNotIn("OUMV_READ_ONLY", env)

    def test_backend_env_carries_the_read_only_switch(self):
        with patch.object(LicenseManager, "get_active_license", return_value=_license("free")):
            env = backend_env({})
        self.assertEqual(env.get("TOKENSAFE_OUMV_READ_ONLY"), "1")
        self.assertEqual(env.get("TOKENSAFE_OUMV_AUTOMOUNT"), "1")


class WriteToolGateTest(unittest.TestCase):
    WRITE_CALLS = (
        ("oumv_mutate", {"path": "src/app.py", "replacement": "x"}),
        ("oumv_preview", {"path": "src/app.py", "replacement": "x"}),
        ("oumv_verify", {"target": "GET /health"}),
        ("oumv_build", {}),
    )

    def _assert_refused(self, tier):
        with patch.object(LicenseManager, "get_active_license", return_value=_license(tier)):
            for tool, args in self.WRITE_CALLS:
                with self.subTest(tier=tier, tool=tool):
                    result = mcp_server._dispatch_tool_call(tool, dict(args))
                    self.assertTrue(result.get("isError"), result)
                    text = result["content"][0]["text"]
                    self.assertIn("read-only", text)
                    self.assertIn("Solo or higher", text)

    def test_guest_write_tools_are_refused(self):
        self._assert_refused("guest")

    def test_free_write_tools_are_refused(self):
        self._assert_refused("free")

    def test_consolidated_oumv_mutate_is_refused_too(self):
        with patch.object(LicenseManager, "get_active_license", return_value=_license("free")):
            result = mcp_server._dispatch_tool_call(
                "oumv", {"action": "mutate", "path": "src/app.py", "replacement": "x"}
            )
        self.assertTrue(result.get("isError"), result)
        self.assertIn("read-only", result["content"][0]["text"])

    def test_paid_tier_passes_the_policy_gate(self):
        with patch.object(LicenseManager, "get_active_license", return_value=_license("solo")), \
                patch("src.oumv_gateway.OUMVGateway.mutate", return_value={"ok": True}) as mutate:
            result = mcp_server._dispatch_tool_call(
                "oumv_mutate", {"path": "src/app.py", "replacement": "x"}
            )
        self.assertFalse(result.get("isError"), result)
        mutate.assert_called_once()

    def test_observation_is_never_gated(self):
        with patch.object(LicenseManager, "get_active_license", return_value=_license("free")), \
                patch("src.oumv_gateway.OUMVGateway.observe", return_value={"ok": True}):
            result = mcp_server._dispatch_tool_call("oumv", {"action": "observe", "level": "L1"})
        self.assertFalse(result.get("isError"), result)


if __name__ == "__main__":
    unittest.main()
