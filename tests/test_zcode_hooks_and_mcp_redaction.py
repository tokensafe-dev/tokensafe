"""Tests for ZCode hook registration (installer) and the MCP-boundary egress redactor.

1. install_hooks_config('zcode') registers config-file hooks with enabled=true
   (ZCode runs configuration hooks only when enabled) into ~/.zcode/cli/config.json.
2. The bundled hook scripts already speak the ZCode contract (camelCase payloads,
   injectSteps/systemMessage outputs, decision/reason) — verified by running them.
3. _redact_result_secrets strips recognized secrets from MCP tool results.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Runtime-concatenated fake secrets: never write scanner-matching literals in
# test sources (GitHub Push Protection lesson from the Booster session).
FAKE_GH_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
FAKE_STRIPE_KEY = "sk" + "_live_" + "51Hx9yZ2aB3cD4eF5gH6iJ7kL8mN9oP0"


class TestInstallerZcodeHooks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ts_zcode_hooks_")
        self.cli_dir = os.path.join(self.tmp, ".zcode", "cli")
        os.makedirs(self.cli_dir, exist_ok=True)
        self.config_file = os.path.join(self.cli_dir, "config.json")
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump({"mcp": {"existing": True}, "plugins": {}}, f)
        patcher = patch("src.installer.HOME", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _load(self):
        with open(self.config_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_registers_all_events_with_enabled_true(self):
        from src.installer import AgentInstaller
        ok, msg = AgentInstaller.install_hooks_config("zcode")
        self.assertTrue(ok, msg)
        data = self._load()
        hooks = data.get("hooks") or {}
        self.assertTrue(hooks.get("enabled") is True, "ZCode config hooks must carry enabled=true")
        events = hooks.get("events") or {}
        for event, script in (
            ("SessionStart", "session_start.py"),
            ("UserPromptSubmit", "pre_invocation.py"),
            ("PreToolUse", "pre_tool_use.py"),
            ("PostToolUse", "post_tool_use.py"),
            ("Stop", "stop_hook.py"),
        ):
            entries = events.get(event)
            self.assertIsInstance(entries, list, f"{event} missing")
            args = entries[0]["hooks"][0]["args"]
            self.assertTrue(args[-1].endswith(script), f"{event} should run {script}")
            self.assertEqual(entries[0]["hooks"][0].get("type"), "process")
        # Existing config keys preserved
        self.assertTrue(data["mcp"]["existing"])

    def test_idempotent_second_call(self):
        from src.installer import AgentInstaller
        self.assertTrue(AgentInstaller.install_hooks_config("zcode")[0])
        ok, msg = AgentInstaller.install_hooks_config("zcode")
        self.assertTrue(ok)
        self.assertIn("already registered", msg)
        data = self._load()
        for event in ("SessionStart", "PreToolUse", "Stop"):
            self.assertEqual(len((data["hooks"]["events"] or {}).get(event) or []), 1)

    def test_stale_script_path_is_repaired(self):
        from src.installer import AgentInstaller
        AgentInstaller.install_hooks_config("zcode")
        data = self._load()
        entry = data["hooks"]["events"]["PreToolUse"][0]
        entry["hooks"][0]["args"][-1] = "/old/stale/path/hooks/pre_tool_use.py"
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(data, f)
        ok, msg = AgentInstaller.install_hooks_config("zcode")
        self.assertTrue(ok)
        data = self._load()
        args = data["hooks"]["events"]["PreToolUse"][0]["hooks"][0]["args"]
        self.assertTrue(args[-1].endswith(os.path.join("hooks", "pre_tool_use.py")))
        self.assertIn("hooks", args[-1])


class TestZcodeHookScriptContract(unittest.TestCase):
    """The bundled scripts must speak the ZCode contract out of the box."""

    def _run(self, script: str, payload: dict):
        proc = subprocess.run(
            [sys.executable, os.path.join(PROJECT_ROOT, "hooks", script)],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True,
            timeout=30,
        )
        return proc

    def test_session_start_emits_inject_steps(self):
        proc = self._run("session_start.py", {"workspacePaths": [tempfile.mkdtemp()]})
        self.assertEqual(proc.returncode, 0, proc.stderr.decode()[:300])
        out = json.loads(proc.stdout.decode() or "{}")
        self.assertIn("injectSteps", out)
        self.assertTrue(len(out["injectSteps"]) >= 1)

    def test_pre_tool_use_denies_destructive_command(self):
        payload = {
            "toolCall": {"name": "run_command", "args": {"CommandLine": "rm -rf /"}},
            "conversationId": f"zcode_destructive_{uuid.uuid4().hex[:8]}",
        }
        proc = self._run("pre_tool_use.py", payload)
        out = json.loads(proc.stdout.decode() or "{}")
        self.assertEqual(out.get("decision"), "deny", out)
        self.assertIn("TokenSafe", out.get("reason", ""))

    def test_pre_tool_use_allows_clean_command(self):
        payload = {
            "toolCall": {"name": "run_command", "args": {"CommandLine": "ls -1"}},
            "conversationId": f"zcode_clean_{uuid.uuid4().hex[:8]}",
        }
        proc = self._run("pre_tool_use.py", payload)
        out = json.loads(proc.stdout.decode() or "{}")
        self.assertEqual(out.get("decision"), "allow", out)


class TestMcpEgressRedaction(unittest.TestCase):
    def setUp(self):
        from src import mcp_server
        self.mcp = mcp_server

    def test_strips_secrets_from_nested_result(self):
        result = {
            "content": [{"type": "text", "text": f"token: {FAKE_GH_TOKEN} and stripe {FAKE_STRIPE_KEY}"}],
            "meta": {"nested": ["clean", f"key={FAKE_GH_TOKEN}"]},
            "count": 5,
        }
        redacted = self.mcp._redact_result_secrets(result)
        flat = json.dumps(redacted)
        self.assertNotIn(FAKE_GH_TOKEN, flat)
        self.assertNotIn(FAKE_STRIPE_KEY, flat)
        self.assertIn("clean", flat)
        self.assertEqual(redacted["count"], 5)
        # Original result object untouched
        self.assertIn(FAKE_GH_TOKEN, json.dumps(result))

    def test_passthrough_for_clean_and_none(self):
        self.assertIsNone(self.mcp._redact_result_secrets(None))
        clean = {"content": [{"type": "text", "text": "no secrets here"}]}
        self.assertEqual(self.mcp._redact_result_secrets(clean), clean)


if __name__ == "__main__":
    unittest.main()
