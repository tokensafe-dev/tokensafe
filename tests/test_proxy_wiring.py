"""Unit tests for multi-agent proxy wiring:
1. Per-key upstream route table (one proxy daemon serves several providers).
2. ZCode desktop provider clone wiring (rollback-safe, self-healing flags).
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from src.installer import AgentInstaller
from src import proxy as proxy_mod


class TestProxyRouteTable(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ts_proxy_routes_")
        self.patcher = patch.object(proxy_mod, "resolve_data_dir", return_value=self.tmp)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_routes(self, routes):
        path = os.path.join(self.tmp, "proxy_routes.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(routes, f)

    def test_key_fingerprint_is_stable_and_keyless_safe(self):
        self.assertEqual(proxy_mod._key_fingerprint(" abc "), proxy_mod._key_fingerprint("abc"))
        self.assertNotEqual(proxy_mod._key_fingerprint("a"), proxy_mod._key_fingerprint("b"))
        self.assertEqual(len(proxy_mod._key_fingerprint("k")), 16)

    def test_route_override_matches_by_fingerprint(self):
        key = "zai-key-123"
        fp = proxy_mod._key_fingerprint(key)
        self._write_routes({fp: {"upstream_url": "https://api.z.ai/api/anthropic/v1/messages", "label": "zcode"}})
        self.assertEqual(
            proxy_mod._apply_route_override(key, ""),
            "https://api.z.ai/api/anthropic/v1/messages",
        )
        # Unknown key keeps the env default
        self.assertEqual(proxy_mod._apply_route_override("other-key", "https://default.example"), "https://default.example")
        # No key keeps the env default
        self.assertEqual(proxy_mod._apply_route_override("", "https://default.example"), "https://default.example")

    def test_missing_or_corrupt_routes_file_falls_back(self):
        self.assertEqual(proxy_mod._apply_route_override("k", "https://env.example"), "https://env.example")
        with open(os.path.join(self.tmp, "proxy_routes.json"), "w") as f:
            f.write("{not json")
        self.assertEqual(proxy_mod._apply_route_override("k", "https://env.example"), "https://env.example")


class TestInstallerZcodeWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ts_zcode_wiring_")
        self.v2_config = os.path.join(self.tmp, "config.json")
        self.routes_path = os.path.join(self.tmp, "proxy_routes.json")
        self.enabled = {
            "name": "Z.ai - Coding Plan",
            "kind": "anthropic",
            "enabled": True,
            "options": {"apiKey": "zai-test-key-987", "baseURL": "https://api.z.ai/api/anthropic"},
        }
        self.disabled = {
            "name": "Other",
            "kind": "anthropic",
            "enabled": False,
            "options": {"apiKey": "", "baseURL": "https://open.bigmodel.cn/api/anthropic"},
        }
        with open(self.v2_config, "w", encoding="utf-8") as f:
            json.dump({"provider": {"builtin:zai-coding-plan": self.enabled, "builtin:bigmodel": self.disabled}}, f)

        # Route every installer dependency to the sandbox
        patchers = [
            patch.object(AgentInstaller, "ZCODE_V2_CONFIG", self.v2_config),
            patch.object(AgentInstaller, "proxy_health", return_value=True),
            patch.object(AgentInstaller, "_proxy_routes_path", staticmethod(lambda: self.routes_path)),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _load(self):
        with open(self.v2_config, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_wire_clones_provider_and_registers_route(self):
        ok, msg = AgentInstaller._wire_zcode_provider()
        self.assertTrue(ok, msg)
        data = self._load()
        providers = data["provider"]
        clone = providers["builtin:zai-coding-plan-tokensafe"]
        self.assertEqual(clone["options"]["baseURL"], AgentInstaller.PROXY_BASE_URL)
        self.assertEqual(clone["options"]["apiKey"], "zai-test-key-987")
        self.assertTrue(clone["enabled"])
        self.assertFalse(providers["builtin:zai-coding-plan"]["enabled"])
        self.assertEqual(providers["builtin:bigmodel"]["enabled"], False)  # untouched

        routes = json.load(open(self.routes_path))
        (route,) = routes.values()
        self.assertEqual(route["upstream_url"], "https://api.z.ai/api/anthropic/v1/messages")
        self.assertEqual(route["label"], "zcode:builtin:zai-coding-plan")

    def test_wire_is_idempotent(self):
        self.assertTrue(AgentInstaller._wire_zcode_provider()[0])
        ok, msg = AgentInstaller._wire_zcode_provider()
        self.assertTrue(ok)
        self.assertIn("already wired", msg)
        data = self._load()
        self.assertEqual(len([k for k in data["provider"] if k.endswith("-tokensafe")]), 1)

    def test_wire_reasserts_flags_after_app_update_reenables_original(self):
        self.assertTrue(AgentInstaller._wire_zcode_provider()[0])
        # Simulate a ZCode app update re-enabling the original provider
        data = self._load()
        data["provider"]["builtin:zai-coding-plan"]["enabled"] = True
        data["provider"]["builtin:zai-coding-plan-tokensafe"]["enabled"] = False
        with open(self.v2_config, "w", encoding="utf-8") as f:
            json.dump(data, f)

        ok, msg = AgentInstaller._wire_zcode_provider()
        self.assertTrue(ok, msg)
        self.assertIn("ZCode wired", msg)
        data = self._load()
        self.assertTrue(data["provider"]["builtin:zai-coding-plan-tokensafe"]["enabled"])
        self.assertFalse(data["provider"]["builtin:zai-coding-plan"]["enabled"])

    def test_unwire_restores_original(self):
        self.assertTrue(AgentInstaller._wire_zcode_provider()[0])
        ok, msg = AgentInstaller._unwire_zcode_provider()
        self.assertTrue(ok, msg)
        data = self._load()
        self.assertNotIn("builtin:zai-coding-plan-tokensafe", data["provider"])
        self.assertTrue(data["provider"]["builtin:zai-coding-plan"]["enabled"])
        # Idempotent
        ok2, msg2 = AgentInstaller._unwire_zcode_provider()
        self.assertTrue(ok2)
        self.assertIn("not wired", msg2)


if __name__ == "__main__":
    unittest.main()
