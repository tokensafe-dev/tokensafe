import os
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch

from src.installer import AgentInstaller, AGENTS_REGISTRY


class TestKimiIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tokensafe_kimi_test_")
        self.project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_kimi_plugin_manifest_exists_and_valid(self):
        manifest_path = os.path.join(self.project_root, "kimi.plugin.json")
        self.assertTrue(os.path.isfile(manifest_path), "kimi.plugin.json must exist in project root")
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data.get("name"), "tokensafe")
        self.assertIn("skills", data)
        self.assertIn("mcpServers", data)
        self.assertIn("tokensafe", data["mcpServers"])
        self.assertIn("hooks", data)
        self.assertGreaterEqual(len(data["hooks"]), 3)

    def test_kimi_agent_in_registry(self):
        self.assertIn("kimi", AGENTS_REGISTRY)
        kimi_info = AGENTS_REGISTRY["kimi"]
        self.assertEqual(kimi_info["type"], "kimi_plugin")
        self.assertEqual(kimi_info["name"], "Kimi Code")

    def test_kimi_detection_via_detect_path(self):
        fake_kimi_home = os.path.join(self.tmp, ".kimi-code")
        os.makedirs(fake_kimi_home, exist_ok=True)
        with patch.dict(AGENTS_REGISTRY["kimi"], {"detect_path": fake_kimi_home}):
            detected = AgentInstaller.detect_available_agents()
            self.assertTrue(detected["kimi"]["installed"])

    def test_kimi_detection_via_app_fallback(self):
        nonexistent_home = os.path.join(self.tmp, ".nonexistent-kimi")
        with patch.dict(AGENTS_REGISTRY["kimi"], {"detect_path": nonexistent_home}), \
             patch("os.path.exists", side_effect=lambda p: p == "/Applications/Kimi Code.app" or p == nonexistent_home):
            # When /Applications/Kimi Code.app exists, detection should succeed
            detected = AgentInstaller.detect_available_agents()
            self.assertTrue(detected["kimi"]["installed"])

    def test_kimi_install_mcp_and_plugin(self):
        fake_home = os.path.join(self.tmp, "home")
        kimi_dir = os.path.join(fake_home, ".kimi-code")
        os.makedirs(kimi_dir, exist_ok=True)

        mcp_path = os.path.join(kimi_dir, "mcp.json")
        installed_path = os.path.join(kimi_dir, "plugins", "installed.json")
        managed_plugin_path = os.path.join(kimi_dir, "plugins", "managed", "tokensafe")
        skills_path = os.path.join(kimi_dir, "skills", "token-optimizer")
        agents_md_path = os.path.join(kimi_dir, "AGENTS.md")

        custom_registry_entry = {
            "name": "Kimi Code",
            "type": "kimi_plugin",
            "config_path": mcp_path,
            "installed_json_path": installed_path,
            "managed_plugin_path": managed_plugin_path,
            "skills_path": skills_path,
            "agents_md_path": agents_md_path,
            "detect_path": kimi_dir,
        }

        with patch.dict(AGENTS_REGISTRY, {"kimi": custom_registry_entry}), \
             patch("src.installer.HOME", fake_home):
            ok, msg = AgentInstaller.install_mcp_config("kimi")
            self.assertTrue(ok)

            # Check mcp.json
            self.assertTrue(os.path.isfile(mcp_path))
            with open(mcp_path, "r", encoding="utf-8") as f:
                mcp_data = json.load(f)
            self.assertIn("tokensafe", mcp_data.get("mcpServers", {}))

            # Check installed.json
            self.assertTrue(os.path.isfile(installed_path))
            with open(installed_path, "r", encoding="utf-8") as f:
                inst_data = json.load(f)
            plugins = inst_data.get("plugins", [])
            self.assertTrue(any(p.get("id") == "tokensafe" for p in plugins))

            # Check managed plugin symlink
            self.assertTrue(os.path.exists(managed_plugin_path))

            # Check skill symlink
            self.assertTrue(os.path.exists(skills_path))

            # Check AGENTS.md rule
            self.assertTrue(os.path.isfile(agents_md_path))
            with open(agents_md_path, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("TOKENSAFE", content)

    def test_kimi_install_hooks_config(self):
        fake_home = os.path.join(self.tmp, "home")
        kimi_dir = os.path.join(fake_home, ".kimi-code")
        os.makedirs(kimi_dir, exist_ok=True)
        config_path = os.path.join(kimi_dir, "config.toml")

        with patch("src.installer.HOME", fake_home):
            ok, msg = AgentInstaller.install_hooks_config("kimi")
            self.assertTrue(ok)
            self.assertTrue(os.path.isfile(config_path))
            with open(config_path, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("[[hooks]]", content)
            self.assertIn("SessionStart", content)
            self.assertIn("UserPromptSubmit", content)
            self.assertIn("PreToolUse", content)
            self.assertIn("kimi_hook.py", content)

            # Idempotence
            ok2, msg2 = AgentInstaller.install_hooks_config("kimi")
            self.assertTrue(ok2)
            self.assertIn("already registered", msg2)


if __name__ == "__main__":
    unittest.main()
