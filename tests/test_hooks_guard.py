import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json
import tempfile
import unittest
from unittest.mock import patch

from hooks.pre_tool_use import _handle as handle_pre_tool_use
from hooks.pre_invocation import _handle as handle_pre_invocation
from src.mistakes import MistakesLedger
from src import metrics as metrics_mod


class TestHooksGuard(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.orig_data_dir = os.environ.get("TOKENSAFE_DATA_DIR")
        self.orig_db_path = os.environ.get("TOKENSAFE_DB_PATH")
        self.orig_telem = os.environ.get("TOKENSAFE_TELEMETRY_DISABLED")
        os.environ["TOKENSAFE_DATA_DIR"] = self.tmp_dir.name
        os.environ["TOKENSAFE_DB_PATH"] = os.path.join(self.tmp_dir.name, "metrics.db")
        os.environ["TOKENSAFE_TELEMETRY_DISABLED"] = "1"
        metrics_mod._INITIALIZED_METRICS_DBS.clear()

    def tearDown(self):
        if self.orig_data_dir is not None:
            os.environ["TOKENSAFE_DATA_DIR"] = self.orig_data_dir
        else:
            os.environ.pop("TOKENSAFE_DATA_DIR", None)
        if self.orig_db_path is not None:
            os.environ["TOKENSAFE_DB_PATH"] = self.orig_db_path
        else:
            os.environ.pop("TOKENSAFE_DB_PATH", None)
        if self.orig_telem is not None:
            os.environ["TOKENSAFE_TELEMETRY_DISABLED"] = self.orig_telem
        else:
            os.environ.pop("TOKENSAFE_TELEMETRY_DISABLED", None)
        metrics_mod._INITIALIZED_METRICS_DBS.clear()
        self.tmp_dir.cleanup()

    def test_pre_tool_use_lockfile_interception(self):
        # Create a lockfile with 100 lines
        lock_path = os.path.join(self.tmp_dir.name, "package-lock.json")
        with open(lock_path, "w", encoding="utf-8") as f:
            f.write('{\n  "name": "test",\n' + '  "packages": {}\n}' * 50)

        payload = {
            "conversationId": "test_session_1",
            "cwd": self.tmp_dir.name,
            "toolCall": {
                "name": "read_file",
                "args": {"Path": lock_path}
            }
        }

        res = handle_pre_tool_use(payload)
        self.assertEqual(res["decision"], "allow")
        self.assertIn("overwrite", res)
        # Should overwrite Path to points to summary file
        overwritten_path = res["overwrite"].get("Path")
        self.assertIsNotNone(overwritten_path)
        self.assertTrue(overwritten_path.endswith(".summary.txt"))
        self.assertIn("query_dependency", res.get("reason", ""))

    def test_pre_tool_use_anti_stuck_hybrid_escalation(self):
        cmd = "npm run test:flaky"
        # Record 3 failures in mistakes ledger
        for i in range(3):
            MistakesLedger.record_failure(
                summary=f"Flaky test failed run {i}",
                source="command",
                details=cmd,
                project_root=self.tmp_dir.name
            )

        payload = {
            "conversationId": "test_session_2",
            "cwd": self.tmp_dir.name,
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": cmd}
            }
        }

        res = handle_pre_tool_use(payload)
        # Should escalate to force_ask
        self.assertEqual(res["decision"], "force_ask")
        self.assertIn("Dead-End Loop", res.get("reason", ""))

    def test_pre_invocation_injects_guard_prompt(self):
        # Record a mistake
        MistakesLedger.record_failure(
            summary="Port 5432 in use by zombie postgres",
            file_path="docker-compose.yml",
            project_root=self.tmp_dir.name
        )

        payload = {
            "conversationId": "test_session_3",
            "cwd": self.tmp_dir.name,
            "lastUserInput": "Deploy database",
            "injectSteps": []
        }

        res = handle_pre_invocation(payload)
        steps = res.get("injectSteps", [])
        self.assertTrue(len(steps) > 0)
        step_str = str(steps)
        self.assertIn("Anti-Stuck Guard", step_str)
        self.assertIn("Port 5432 in use", step_str)

    def test_mcp_query_dependency_and_slice_type(self):
        from src.mcp_server import handle_tool_call

        # 1. Test query_dependency via MCP
        lock_path = os.path.join(self.tmp_dir.name, "package-lock.json")
        with open(lock_path, "w", encoding="utf-8") as f:
            json.dump({
                "name": "mcp-test",
                "lockfileVersion": 3,
                "packages": {
                    "node_modules/dotenv": {"version": "16.4.5"}
                }
            }, f)

        res_dep = handle_tool_call("query_dependency", {"lockfile_path": lock_path, "package_name": "dotenv"})
        self.assertIn("content", res_dep)
        self.assertIn("16.4.5", res_dep["content"][0]["text"])

        # 2. Test slice_external_type via MCP
        lib_dir = os.path.join(self.tmp_dir.name, "node_modules", "ts-lib")
        os.makedirs(lib_dir, exist_ok=True)
        with open(os.path.join(lib_dir, "package.json"), "w") as f:
            json.dump({"name": "ts-lib", "version": "2.0.0", "types": "index.d.ts"}, f)
        with open(os.path.join(lib_dir, "index.d.ts"), "w") as f:
            f.write("export interface PluginConfig { enabled: boolean; }")

        with patch("os.getcwd", return_value=self.tmp_dir.name):
            res_type = handle_tool_call("slice_external_type", {"package_name": "ts-lib", "type_name": "PluginConfig"})
            self.assertIn("content", res_type)
            self.assertIn("interface PluginConfig", res_type["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
