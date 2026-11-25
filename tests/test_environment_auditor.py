import unittest
import tempfile
import json
import os
from unittest.mock import patch
from src.environment_auditor import EnvironmentAuditor, RULE_FILE_LIMIT_BYTES

class TestEnvironmentAuditor(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.temp_dir.name, "mcp_config.json")
        self.schemas_dir = os.path.join(self.temp_dir.name, "mcp")
        os.makedirs(self.schemas_dir, exist_ok=True)

        # Create sample config
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump({
                "mcpServers": {
                    "test_server": {"command": "echo"},
                    "bloated_server": {"command": "echo"}
                }
            }, f)

        # Create schema dirs
        ts_dir = os.path.join(self.schemas_dir, "test_server")
        os.makedirs(ts_dir, exist_ok=True)
        for i in range(5):
            with open(os.path.join(ts_dir, f"tool_{i}.json"), "w") as f:
                f.write("{}")

        bloat_dir = os.path.join(self.schemas_dir, "bloated_server")
        os.makedirs(bloat_dir, exist_ok=True)
        for i in range(50):
            with open(os.path.join(bloat_dir, f"tool_{i}.json"), "w") as f:
                f.write("{}")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_audit_detects_bloat(self):
        with patch.object(EnvironmentAuditor, "get_config_paths", return_value=[self.config_path]), \
             patch.object(EnvironmentAuditor, "get_mcp_schemas_dir", return_value=self.schemas_dir), \
             patch.object(EnvironmentAuditor, "get_skills_dirs", return_value=[]):
            report = EnvironmentAuditor.audit()
            self.assertTrue(report["has_critical_bloat"])
            self.assertEqual(len(report["bloated_servers"]), 1)
            self.assertEqual(report["bloated_servers"][0]["name"], "bloated_server")
            self.assertEqual(report["bloated_servers"][0]["tool_count"], 50)

    def test_bloat_warning(self):
        with patch.object(EnvironmentAuditor, "get_config_paths", return_value=[self.config_path]), \
             patch.object(EnvironmentAuditor, "get_mcp_schemas_dir", return_value=self.schemas_dir), \
             patch.object(EnvironmentAuditor, "get_skills_dirs", return_value=[]):
            warning = EnvironmentAuditor.get_bloat_warning()
            self.assertIsNotNone(warning)
            self.assertIn("bloated_server", warning)
            self.assertIn("50 tools", warning)

    def test_disable_and_enable_mcp(self):
        with patch.object(EnvironmentAuditor, "get_config_paths", return_value=[self.config_path]), \
             patch.object(EnvironmentAuditor, "get_mcp_schemas_dir", return_value=self.schemas_dir):
            # Disable
            res = EnvironmentAuditor.disable_mcp("bloated_server")
            self.assertTrue(res["success"])

            with open(self.config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            self.assertNotIn("bloated_server", cfg["mcpServers"])
            self.assertIn("bloated_server", cfg["disabledMcpServers"])
            self.assertTrue(os.path.isdir(os.path.join(self.schemas_dir, "bloated_server.disabled")))

            # Enable back
            res_en = EnvironmentAuditor.enable_mcp("bloated_server")
            self.assertTrue(res_en["success"])

            with open(self.config_path, "r", encoding="utf-8") as f:
                cfg_en = json.load(f)
            self.assertIn("bloated_server", cfg_en["mcpServers"])
            self.assertNotIn("bloated_server", cfg_en.get("disabledMcpServers", {}))
            self.assertTrue(os.path.isdir(os.path.join(self.schemas_dir, "bloated_server")))

class TestConfiguredServersMergeAndDisabledPlugins(unittest.TestCase):
    def _make_plugin(self, plugins_dir, name, server_name, manifest=None):
        d = os.path.join(plugins_dir, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "mcp_config.json"), "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {server_name: {"command": "echo"}}}, f)
        if manifest is not None:
            with open(os.path.join(d, "plugin.json"), "w", encoding="utf-8") as f:
                json.dump(manifest, f)

    def test_disabled_plugins_are_not_audited(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugins = os.path.join(tmp, "plugins")
            self._make_plugin(plugins, "good", "good_server")
            self._make_plugin(plugins, "bad1", "bad1_server", manifest={"disabled": True})
            self._make_plugin(plugins, "bad2", "bad2_server")
            state_path = os.path.join(tmp, "config.json")
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump({"plugins": {"bad2": {"enabled": False}}}, f)

            with patch.object(EnvironmentAuditor, "get_plugins_dir", return_value=plugins), \
                 patch.object(EnvironmentAuditor, "get_enable_state_path", return_value=state_path), \
                 patch.object(EnvironmentAuditor, "get_config_paths", return_value=[]):
                servers = EnvironmentAuditor.get_configured_mcp_servers()

            self.assertIn("good_server", servers)
            self.assertNotIn("bad1_server", servers)
            self.assertNotIn("bad2_server", servers)

    def test_both_config_paths_are_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg1 = os.path.join(tmp, "one.json")
            cfg2 = os.path.join(tmp, "two.json")
            with open(cfg1, "w", encoding="utf-8") as f:
                json.dump({"mcpServers": {"a": {"command": "echo"}}}, f)
            with open(cfg2, "w", encoding="utf-8") as f:
                json.dump({"mcpServers": {"b": {"command": "echo"}}}, f)

            with patch.object(EnvironmentAuditor, "get_config_paths", return_value=[cfg1, cfg2]), \
                 patch.object(EnvironmentAuditor, "get_plugins_dir", return_value=os.path.join(tmp, "nope")):
                servers = EnvironmentAuditor.get_configured_mcp_servers()

            self.assertIn("a", servers)
            self.assertIn("b", servers)


class TestAuditorWorkspaceAndRules(unittest.TestCase):
    """rules.md contract: 24 KB per rule file (after @[label](path) expansion),
    20,000-token shared always-on budget; workspace `.agents/` counts as a
    skills root."""

    def test_get_skills_dirs_includes_workspace_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, ".agents"))
            dirs = EnvironmentAuditor.get_skills_dirs(workspace_root=tmp)
            self.assertIn(os.path.join(tmp, ".agents"), dirs)

    def test_rules_audit_include_expansion_and_per_file_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "inc.md"), "w", encoding="utf-8") as f:
                f.write("B" * 500)
            agents = os.path.join(tmp, "AGENTS.md")
            with open(agents, "w", encoding="utf-8") as f:
                f.write("A" * 300 + "\n@[docs](inc.md)\n")

            rep = EnvironmentAuditor.audit_rules(workspace_root=tmp)
            self.assertEqual(rep["files"], 1)
            self.assertEqual(rep["violations"], [])
            # effective text = 300 A's + 500 B's; raw file is only ~320 bytes
            self.assertGreater(rep["total_tokens"], 100)

            with open(agents, "w", encoding="utf-8") as f:
                f.write("A" * (RULE_FILE_LIMIT_BYTES + 500))
            rep2 = EnvironmentAuditor.audit_rules(workspace_root=tmp)
            self.assertEqual(len(rep2["violations"]), 1)
            self.assertEqual(rep2["violations"][0]["path"], "AGENTS.md")
            self.assertGreater(rep2["violations"][0]["effective_bytes"], RULE_FILE_LIMIT_BYTES)

    def test_rules_budget_exceeded_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "AGENTS.md"), "w", encoding="utf-8") as f:
                f.write("word " * 500)
            with patch("src.environment_auditor.RULES_BUDGET_TOKENS", 10):
                rep = EnvironmentAuditor.audit_rules(workspace_root=tmp)
            self.assertTrue(rep["budget_exceeded"])

    def test_bloat_warning_mentions_rule_violation(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "AGENTS.md"), "w", encoding="utf-8") as f:
                f.write("A" * (RULE_FILE_LIMIT_BYTES + 10))
            with patch.object(EnvironmentAuditor, "get_config_paths", return_value=[]), \
                 patch.object(EnvironmentAuditor, "get_mcp_schemas_dir", return_value=os.path.join(tmp, "mcp")):
                warning = EnvironmentAuditor.get_bloat_warning(workspace_root=tmp)
            self.assertIsNotNone(warning)
            self.assertIn("AGENTS.md", warning)
            self.assertIn("byte", warning)

    def test_multi_agent_json_and_codex_toml_config_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            claude_json = os.path.join(tmp, ".claude.json")
            codex_toml = os.path.join(tmp, "config.toml")
            with open(claude_json, "w", encoding="utf-8") as f:
                json.dump({"mcpServers": {"github": {"command": "npx"}}}, f)
            with open(codex_toml, "w", encoding="utf-8") as f:
                f.write('[mcp_servers.prisma]\ncommand = "npx"\n')
            with patch.object(EnvironmentAuditor, "get_config_paths", return_value=[claude_json, codex_toml]), \
                 patch.object(EnvironmentAuditor, "get_plugins_dir", return_value=os.path.join(tmp, "no_plugins")):
                servers = EnvironmentAuditor.get_configured_mcp_servers()
            self.assertIn("github", servers)
            self.assertIn("prisma", servers)

    def test_codex_toml_subtables_not_counted_as_servers_and_autofix(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_toml = os.path.join(tmp, "config.toml")
            with open(codex_toml, "w", encoding="utf-8") as f:
                f.write(
                    '[mcp_servers.context7]\nurl = "https://x"\n'
                    '[mcp_servers.context7.http_headers]\nX-Key = "1"\n\n'
                    '[mcp_servers.rea]\ncommand = "rea"\n'
                    '[mcp_servers.rea.env]\nFOO = "BAR"\n\n'
                    '[mcp_servers.tokensafe]\ncommand = "python3"\nargs = ["/old/mcp_server.py"]\n\n'
                    '["/old/mcp_server.py"]\n'
                )
            with open(os.path.join(tmp, "AGENTS.md"), "w", encoding="utf-8") as f:
                f.write("## Canonical AGENTS rules\n- Always verify before push.\n" * 10)
            with open(os.path.join(tmp, ".cursorrules"), "w", encoding="utf-8") as f:
                f.write("## Redundant cursor rules\n- Duplicate instruction text.\n" * 15)

            with patch.object(EnvironmentAuditor, "get_config_paths", return_value=[codex_toml]), \
                 patch.object(EnvironmentAuditor, "get_plugins_dir", return_value=os.path.join(tmp, "no_plugins")), \
                 patch.object(EnvironmentAuditor, "get_mcp_schemas_dir", return_value=os.path.join(tmp, "no_schemas")), \
                 patch.object(EnvironmentAuditor, "get_skills_dirs", return_value=[]):
                servers = EnvironmentAuditor.get_configured_mcp_servers()
                self.assertIn("context7", servers)
                self.assertIn("rea", servers)
                self.assertIn("tokensafe", servers)
                self.assertNotIn("context7.http_headers", servers)
                self.assertNotIn("rea.env", servers)

                rep = EnvironmentAuditor.format_cli_report(workspace_root=tmp)
                self.assertIn("tokensafe doctor --fix", rep)
                self.assertNotIn("Environment healthy", rep)

                fix_res = EnvironmentAuditor.auto_fix(workspace_root=tmp)
                self.assertTrue(len(fix_res["fixed"]) >= 2)
                # Verify config.toml is valid TOML now & .cursorrules is lean pointer
                try:
                    import tomllib
                except ImportError:
                    tomllib = None
                if tomllib is not None:
                    parsed = tomllib.loads(open(codex_toml, "r", encoding="utf-8").read())
                    self.assertIn("tokensafe", parsed["mcp_servers"])
                ghost_after = EnvironmentAuditor.audit_ghost_tokens(tmp)
                self.assertFalse(ghost_after["has_ghost_tokens"])

    def test_doctor_static_score_and_skills_disable_enable(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills_root = os.path.join(tmp, "skills")
            sk_a = os.path.join(skills_root, "my-unused-skill")
            os.makedirs(sk_a, exist_ok=True)
            with open(os.path.join(sk_a, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write("---\nname: my-unused-skill\ndescription: Unused helper skill\n---\nBody\n")

            with patch.object(EnvironmentAuditor, "get_config_paths", return_value=[]), \
                 patch.object(EnvironmentAuditor, "get_plugins_dir", return_value=os.path.join(tmp, "no_plugins")), \
                 patch.object(EnvironmentAuditor, "get_mcp_schemas_dir", return_value=os.path.join(tmp, "no_schemas")), \
                 patch.object(EnvironmentAuditor, "get_skills_dirs", return_value=[skills_root]):
                rep_clean = EnvironmentAuditor.format_cli_report(workspace_root=tmp)
                self.assertIn("Environment Quality Score:", rep_clean)
                self.assertNotIn("12% headroom", rep_clean)

                skills = EnvironmentAuditor.list_skills(workspace_root=tmp)
                self.assertEqual(len(skills), 1)
                self.assertFalse(skills[0]["disabled"])

                dis = EnvironmentAuditor.disable_skill("my-unused-skill", workspace_root=tmp)
                self.assertTrue(dis["success"])
                self.assertEqual(EnvironmentAuditor.audit(workspace_root=tmp)["total_skills"], 0)

                ena = EnvironmentAuditor.enable_skill("my-unused-skill", workspace_root=tmp)
                self.assertTrue(ena["success"])
                self.assertEqual(EnvironmentAuditor.audit(workspace_root=tmp)["total_skills"], 1)

if __name__ == "__main__":
    unittest.main()
