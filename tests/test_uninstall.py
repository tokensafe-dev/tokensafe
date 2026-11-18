import unittest
from unittest.mock import patch, MagicMock
import os
import json
import tempfile
import shutil
from src.installer import AgentInstaller, AGENTS_REGISTRY

class TestUninstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_remove_global_rule_strips_block_and_preserves_surrounding(self):
        fpath = os.path.join(self.tmp, "AGENTS.md")
        with open(fpath, "w", encoding="utf-8") as f:
            f.write("# Project Instructions\n\n<!-- tokensafe -->\n## TOKENSAFE PROTOCOL\nNever raw grep\n<!-- /tokensafe -->\n\n## Custom Rules\nBe awesome.\n")

        res = AgentInstaller._remove_global_rule(fpath)
        self.assertTrue(res)
        self.assertTrue(os.path.exists(fpath))
        with open(fpath, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("<!-- tokensafe -->", content)
        self.assertNotIn("TOKENSAFE PROTOCOL", content)
        self.assertIn("# Project Instructions", content)
        self.assertIn("## Custom Rules", content)

    def test_remove_global_rule_deletes_empty_file(self):
        fpath = os.path.join(self.tmp, "CLAUDE.md")
        with open(fpath, "w", encoding="utf-8") as f:
            f.write("<!-- tokensafe -->\n## TOKENSAFE PROTOCOL\n<!-- /tokensafe -->\n")

        res = AgentInstaller._remove_global_rule(fpath)
        self.assertTrue(res)
        self.assertFalse(os.path.exists(fpath), "Empty instruction file should be removed")

    def test_cursor_uninstall_mcp_config(self):
        fake_config = os.path.join(self.tmp, "mcp.json")
        fake_skills = os.path.join(self.tmp, "skills", "token-optimizer")
        fake_rules = os.path.join(self.tmp, "rules", "tokensafe.mdc")
        fake_ext = os.path.join(self.tmp, "extensions", "tokensafe-1.0.0")

        os.makedirs(fake_skills)
        os.makedirs(os.path.dirname(fake_rules), exist_ok=True)
        with open(fake_rules, "w") as f:
            f.write("rule")
        os.makedirs(fake_ext)
        with open(fake_config, "w") as f:
            json.dump({"mcpServers": {"tokensafe": {"command": "python3"}, "other": {"command": "node"}}}, f)

        orig_entry = AGENTS_REGISTRY["cursor"]
        AGENTS_REGISTRY["cursor"] = {
            "name": "Cursor",
            "type": "cursor_env",
            "config_path": fake_config,
            "skills_path": fake_skills,
            "rules_path": fake_rules,
            "extension_path": fake_ext,
            "detect_path": self.tmp
        }
        try:
            ok, msg = AgentInstaller.uninstall_mcp_config("cursor")
            self.assertTrue(ok)
            with open(fake_config, "r") as f:
                data = json.load(f)
            self.assertNotIn("tokensafe", data["mcpServers"])
            self.assertIn("other", data["mcpServers"])
            self.assertFalse(os.path.exists(fake_skills))
            self.assertFalse(os.path.exists(fake_rules))
            self.assertFalse(os.path.exists(fake_ext))
        finally:
            AGENTS_REGISTRY["cursor"] = orig_entry

    def test_codex_toml_uninstall_mcp_config(self):
        fake_config = os.path.join(self.tmp, "config.toml")
        fake_skills = os.path.join(self.tmp, "skills", "token-optimizer")
        fake_plugin = os.path.join(self.tmp, "plugins", "tokensafe")
        fake_agents_md = os.path.join(self.tmp, "AGENTS.md")

        with open(fake_config, "w", encoding="utf-8") as f:
            f.write('model = "o3"\n\n[mcp_servers.tokensafe]\ncommand = "python3"\nargs = ["srv.py"]\n\n[features]\nmulti_agent = true\n')
        os.makedirs(fake_skills)
        os.makedirs(fake_plugin)
        with open(fake_agents_md, "w", encoding="utf-8") as f:
            f.write("<!-- tokensafe -->\nrule\n<!-- /tokensafe -->\n")

        orig_entry = AGENTS_REGISTRY["codex"]
        AGENTS_REGISTRY["codex"] = {
            "name": "OpenAI Codex",
            "type": "codex_toml",
            "config_path": fake_config,
            "skills_path": fake_skills,
            "plugin_path": fake_plugin,
            "agents_md_path": fake_agents_md,
            "detect_path": self.tmp
        }
        try:
            ok, msg = AgentInstaller.uninstall_mcp_config("codex")
            self.assertTrue(ok)
            with open(fake_config, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertNotIn("[mcp_servers.tokensafe]", content)
            self.assertIn('model = "o3"', content)
            self.assertIn("[features]", content)
            self.assertFalse(os.path.exists(fake_skills))
            self.assertFalse(os.path.exists(fake_plugin))
            self.assertFalse(os.path.exists(fake_agents_md))
        finally:
            AGENTS_REGISTRY["codex"] = orig_entry

    def test_kimi_uninstall_mcp_and_hooks(self):
        fake_home = os.path.join(self.tmp, "home")
        kimi_dir = os.path.join(fake_home, ".kimi-code")
        os.makedirs(kimi_dir, exist_ok=True)
        config_path = os.path.join(kimi_dir, "config.toml")
        mcp_path = os.path.join(kimi_dir, "mcp.json")
        installed_path = os.path.join(kimi_dir, "plugins", "installed.json")
        os.makedirs(os.path.dirname(installed_path), exist_ok=True)

        with open(config_path, "w", encoding="utf-8") as f:
            f.write(
                'custom_key = "value"\n\n'
                '# TokenSafe Lifecycle Hooks\n'
                '[[hooks]]\nevent = "SessionStart"\ncommand = "python3 /path/hooks/kimi_hook.py"\ntimeout = 10\n\n'
                '[[hooks]]\nevent = "UserHook"\ncommand = "my_cmd"\ntimeout = 5\n'
            )
        with open(mcp_path, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {"tokensafe": {"command": "py"}, "other": {"command": "n"}}}, f)
        with open(installed_path, "w", encoding="utf-8") as f:
            json.dump({"plugins": [{"id": "tokensafe", "enabled": True}, {"id": "other", "enabled": True}]}, f)

        orig_entry = AGENTS_REGISTRY["kimi"]
        AGENTS_REGISTRY["kimi"] = {
            "name": "Kimi Code",
            "type": "kimi_plugin",
            "config_path": mcp_path,
            "installed_json_path": installed_path,
            "managed_plugin_path": os.path.join(kimi_dir, "plugins", "managed", "tokensafe"),
            "skills_path": os.path.join(kimi_dir, "skills", "token-optimizer"),
            "agents_md_path": os.path.join(kimi_dir, "AGENTS.md"),
            "detect_path": kimi_dir
        }
        try:
            with patch("src.installer.HOME", fake_home):
                m_ok, _ = AgentInstaller.uninstall_mcp_config("kimi")
                h_ok, _ = AgentInstaller.uninstall_hooks_config("kimi")
                self.assertTrue(m_ok)
                self.assertTrue(h_ok)

                # Verify mcp.json
                with open(mcp_path, "r") as f:
                    mcp = json.load(f)
                self.assertNotIn("tokensafe", mcp["mcpServers"])
                self.assertIn("other", mcp["mcpServers"])

                # Verify installed.json
                with open(installed_path, "r") as f:
                    inst = json.load(f)
                self.assertEqual(len(inst["plugins"]), 1)
                self.assertEqual(inst["plugins"][0]["id"], "other")

                # Verify config.toml
                with open(config_path, "r") as f:
                    cfg = f.read()
                self.assertNotIn("kimi_hook.py", cfg)
                self.assertNotIn("TokenSafe Lifecycle Hooks", cfg)
                self.assertIn("UserHook", cfg)
                self.assertIn('custom_key = "value"', cfg)
        finally:
            AGENTS_REGISTRY["kimi"] = orig_entry

    def test_claude_code_uninstall_hooks(self):
        fake_home = os.path.join(self.tmp, "home")
        claude_dir = os.path.join(fake_home, ".claude")
        os.makedirs(claude_dir, exist_ok=True)
        settings_path = os.path.join(claude_dir, "settings.json")

        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump({
                "hooks": {
                    "SessionStart": [{"hooks": [{"type": "command", "command": "python3 claude_code_hook.py"}]}],
                    "UserPromptSubmit": [
                        {"hooks": [{"type": "command", "command": "python3 claude_code_hook.py"}]},
                        {"hooks": [{"type": "command", "command": "my_custom_hook.sh"}]}
                    ]
                }
            }, f)

        with patch("src.installer.HOME", fake_home):
            ok, msg = AgentInstaller.uninstall_hooks_config("claude_code")
            self.assertTrue(ok)
            with open(settings_path, "r") as f:
                data = json.load(f)
            self.assertNotIn("SessionStart", data.get("hooks", {}))
            self.assertIn("UserPromptSubmit", data["hooks"])
            self.assertEqual(len(data["hooks"]["UserPromptSubmit"]), 1)
            self.assertEqual(data["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"], "my_custom_hook.sh")

    def test_zcode_uninstall_hooks(self):
        fake_home = os.path.join(self.tmp, "home")
        zcode_dir = os.path.join(fake_home, ".zcode", "cli")
        os.makedirs(zcode_dir, exist_ok=True)
        config_path = os.path.join(zcode_dir, "config.json")

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({
                "hooks": {
                    "enabled": True,
                    "events": {
                        "SessionStart": [{"hooks": [{"type": "process", "args": ["python3", "session_start.py"]}]}],
                        "Custom": [{"hooks": [{"type": "process", "args": ["custom.py"]}]}]
                    }
                }
            }, f)

        with patch("src.installer.HOME", fake_home):
            ok, msg = AgentInstaller.uninstall_hooks_config("zcode")
            self.assertTrue(ok)
            with open(config_path, "r") as f:
                data = json.load(f)
            self.assertNotIn("SessionStart", data["hooks"]["events"])
            self.assertIn("Custom", data["hooks"]["events"])

    def test_uninstall_project(self):
        proj_dir = os.path.join(self.tmp, "project")
        os.makedirs(proj_dir, exist_ok=True)

        # 1. Rule files
        agents_md = os.path.join(proj_dir, "AGENTS.md")
        with open(agents_md, "w") as f:
            f.write("# Rules\n<!-- tokensafe -->\nprotocol\n<!-- /tokensafe -->\n")

        cursor_mdc = os.path.join(proj_dir, ".cursor", "rules", "tokensafe.mdc")
        os.makedirs(os.path.dirname(cursor_mdc), exist_ok=True)
        with open(cursor_mdc, "w") as f:
            f.write("mdc rule")

        # 2. .cursor/mcp.json
        cursor_mcp = os.path.join(proj_dir, ".cursor", "mcp.json")
        with open(cursor_mcp, "w") as f:
            json.dump({"mcpServers": {"tokensafe": {"command": "py"}, "other": {"command": "n"}}}, f)

        # 3. .agents/plugins.json
        plugins_json = os.path.join(proj_dir, ".agents", "plugins.json")
        os.makedirs(os.path.dirname(plugins_json), exist_ok=True)
        with open(plugins_json, "w") as f:
            json.dump({"entries": [{"path": "tokensafe"}, {"path": "other"}]}, f)

        # 4. .gitignore
        gitignore = os.path.join(proj_dir, ".gitignore")
        with open(gitignore, "w") as f:
            f.write("node_modules\n.tokensafe/\n# TokenSafe local metrics\n.env\n")

        res = AgentInstaller.uninstall_project(proj_dir)
        self.assertTrue(res["success"])

        # Verify rules cleaned
        with open(agents_md, "r") as f:
            self.assertNotIn("<!-- tokensafe -->", f.read())
        self.assertFalse(os.path.exists(cursor_mdc))

        # Verify mcp cleaned
        with open(cursor_mcp, "r") as f:
            mcp = json.load(f)
        self.assertNotIn("tokensafe", mcp["mcpServers"])
        self.assertIn("other", mcp["mcpServers"])

        # Verify plugins.json cleaned
        with open(plugins_json, "r") as f:
            plg = json.load(f)
        self.assertEqual(len(plg["entries"]), 1)
        self.assertEqual(plg["entries"][0]["path"], "other")

        # Verify gitignore cleaned
        with open(gitignore, "r") as f:
            gi = f.read()
        self.assertNotIn(".tokensafe", gi)
        self.assertNotIn("TokenSafe", gi)
        self.assertIn("node_modules", gi)
        self.assertIn(".env", gi)

    def test_uninstall_all_end_to_end(self):
        with patch.object(AgentInstaller, "proxy_off", return_value={"ok": True}) as mock_proxy, \
             patch.object(AgentInstaller, "detect_available_agents", return_value={}) as mock_detect, \
             patch.object(AgentInstaller, "restart_mcp_daemons", return_value=0) as mock_restart, \
             patch.object(AgentInstaller, "uninstall_project", return_value={"cleaned_rules": ["AGENTS.md"]}) as mock_proj:
            res = AgentInstaller.uninstall_all(force=True, purge=False)
            mock_proxy.assert_called_once()
            mock_restart.assert_called_once()
            mock_proj.assert_called_once()
            self.assertIn("AGENTS.md", res["project_rules"])

if __name__ == "__main__":
    unittest.main()
