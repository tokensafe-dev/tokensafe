"""Unit tests for OUMV monorepo boundaries, dev auto-rewrite, read/edit nudges, and installer sync."""

import json
import os
import shutil
import socket
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

from src.adapter_core import TOKENSAFE_DIRECTIVES, evaluate_cc_pre_tool_use, get_oumv_read_edit_advisory
from src.command_sanitizer import CommandSanitizer
from src.guard import TokenSafeGuard
from src.installer import AgentInstaller, POINTER_RULE, UNIFIED_TOKENSAFE_RULE
from src.oumv_gateway import OUMVGateway
from src.retrofit import (
    detect_project_stack,
    format_oumv_workspace_brief,
    is_path_in_supported_oumv_scope,
)


class TestOumvEnforcementAndMonorepo(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="oumv_enforce_")
        self.tmp_home = tempfile.mkdtemp(prefix="oumv_home_")
        self.tmp_data = tempfile.mkdtemp(prefix="oumv_data_")
        from src.retrofit import _UI_CLI_CACHE
        _UI_CLI_CACHE.clear()

    def tearDown(self):
        OUMVGateway._cached_fe = None
        OUMVGateway._cached_be = None
        OUMVGateway._last_scan_time = 0.0
        OUMVGateway._port_liveness.clear()
        shutil.rmtree(self.test_dir, ignore_errors=True)
        shutil.rmtree(self.tmp_home, ignore_errors=True)
        shutil.rmtree(self.tmp_data, ignore_errors=True)

    def _start_listener(self):
        """Binds a real TCP listener on an ephemeral port (needed for liveness checks)."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)
        self.addCleanup(srv.close)
        return srv.getsockname()[1]

    def _dead_port(self):
        """Returns a port that was just bound and released, so nothing listens on it."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.close()
        return port

    def _create_react_plus_node_monorepo(self):
        web_dir = os.path.join(self.test_dir, "apps", "web")
        api_dir = os.path.join(self.test_dir, "apps", "api")
        os.makedirs(os.path.join(web_dir, "src"), exist_ok=True)
        os.makedirs(os.path.join(api_dir, "src"), exist_ok=True)
        with open(os.path.join(self.test_dir, "package.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "mono", "private": True, "workspaces": ["apps/*"]}, f)
        with open(os.path.join(web_dir, "package.json"), "w", encoding="utf-8") as f:
            json.dump({
                "name": "web",
                "dependencies": {"react": "^19.0.0"},
                "devDependencies": {"vite": "^6.1.0"},
                "scripts": {"dev": "vite"},
            }, f)
        with open(os.path.join(api_dir, "package.json"), "w", encoding="utf-8") as f:
            json.dump({
                "name": "api",
                "dependencies": {"express": "^4.19.0"},
            }, f)
        with open(os.path.join(web_dir, "src", "App.tsx"), "w", encoding="utf-8") as f:
            f.write("export function App() { return <button>Save</button>; }\n")
        with open(os.path.join(api_dir, "src", "server.js"), "w", encoding="utf-8") as f:
            f.write("const express = require('express'); const app = express();\n")
        return web_dir, api_dir

    def test_monorepo_react_supported_vs_node_unsupported(self):
        web_dir, api_dir = self._create_react_plus_node_monorepo()
        stack = detect_project_stack(self.test_dir)

        self.assertIsNotNone(stack["frontend"])
        self.assertEqual(stack["frontend"]["provider"], "react")
        self.assertTrue(stack["frontend"]["oumv_supported"])

        self.assertIsNotNone(stack["backend"])
        self.assertEqual(stack["backend"]["provider"], "node")
        self.assertEqual(stack["backend"]["framework"], "express")
        self.assertFalse(stack["backend"]["oumv_supported"])

        brief = format_oumv_workspace_brief(self.test_dir, include_live=False)
        self.assertIn("react/react (apps/web): OUMV SUPPORTED", brief)
        self.assertIn("node/express (apps/api): OUMV UNSUPPORTED yet", brief)

        self.assertIsNotNone(
            is_path_in_supported_oumv_scope(self.test_dir, os.path.join(web_dir, "src", "App.tsx"))
        )
        self.assertIsNone(
            is_path_in_supported_oumv_scope(self.test_dir, os.path.join(api_dir, "src", "server.js"))
        )

        with patch.object(OUMVGateway, "_probe_candidates", return_value=None):
            status = OUMVGateway.status(workspace_dir=self.test_dir)
            self.assertEqual(status["backend"]["status"], "UNSUPPORTED")
            be_adapter = [a for a in status["adapters"] if a["profile"] == "backend"][0]
            self.assertEqual(be_adapter["lifecycle"]["state"], "UNSUPPORTED")

            obs = OUMVGateway.observe(level="L1", runtime="backend", workspace_dir=self.test_dir)
            self.assertEqual(obs.get("unsupportedRuntime"), "backend/node")
            self.assertEqual(obs.get("supportedProfiles"), ["frontend/react"])

    def test_command_sanitizer_rewrites_vite_dev_to_tokensafe_dev(self):
        web_dir, _ = self._create_react_plus_node_monorepo()
        with patch.object(CommandSanitizer, "_tokensafe_ui_available", return_value=True):
            rewritten, _, reason = CommandSanitizer.sanitize("bun run dev --port=5175", cwd=web_dir)
            self.assertEqual(rewritten, "tokensafe dev --port=5175")
            self.assertIn("zero-footprint", reason.lower())

            vite_direct, _, _ = CommandSanitizer.sanitize("vite --host", cwd=web_dir)
            self.assertEqual(vite_direct, "tokensafe dev --host")

            # Space-separated port is normalized instead of becoming a directory
            spaced, _, _ = CommandSanitizer.sanitize("vite --port 5175", cwd=web_dir)
            self.assertEqual(spaced, "tokensafe dev --port=5175")

            # Do NOT rewrite vite build as dev server
            build_cmd, _, _ = CommandSanitizer.sanitize("vite build", cwd=web_dir)
            self.assertNotIn("tokensafe dev", build_cmd or "")

            # Skip if vite.config.ts already wires tokensafe-ui
            with open(os.path.join(web_dir, "vite.config.ts"), "w", encoding="utf-8") as f:
                f.write("import { tokensafeReact } from 'tokensafe-ui/vite';\nexport default { plugins: [tokensafeReact()] };\n")
            skipped, _, _ = CommandSanitizer.sanitize("bun run dev", cwd=web_dir)
            self.assertIsNone(skipped)

    def test_command_sanitizer_keeps_dev_flags_it_cannot_reproduce(self):
        web_dir, _ = self._create_react_plus_node_monorepo()
        with patch.object(CommandSanitizer, "_tokensafe_ui_available", return_value=True):
            for cmd in (
                "npm run dev -- --mode staging",
                "vite --mode staging --port 3000",
                "vite --config vite.enterprise.ts",
                "npx vite dev --mode=staging",
            ):
                out, _, _ = CommandSanitizer.sanitize(cmd, cwd=web_dir)
                self.assertNotIn("tokensafe dev", out or "", f"{cmd} must not be rewritten")

        # Without the companion CLI the command stays untouched: rewriting would
        # make the dev server fetch tokensafe-ui via npx (or fail offline).
        with patch.object(CommandSanitizer, "_tokensafe_ui_available", return_value=False):
            out2, _, _ = CommandSanitizer.sanitize("npm run dev", cwd=web_dir)
            self.assertNotIn("tokensafe dev", out2 or "")

    def test_guard_blocks_raw_localhost_curl_when_live_oumv_active(self):
        port = self._start_listener()
        with patch.dict(os.environ, {"TOKENSAFE_HOME": self.tmp_home}):
            rt = OUMVGateway.global_runtime_file(self.test_dir)
            os.makedirs(os.path.dirname(rt), exist_ok=True)
            with open(rt, "w", encoding="utf-8") as f:
                json.dump({"frontend": {"name": "react", "url": f"http://127.0.0.1:{port}/__oumv/rpc"}}, f)

            blocked = TokenSafeGuard.inspect_command(f"curl http://127.0.0.1:{port}/api/users", cwd=self.test_dir)
            self.assertTrue(blocked.blocked)
            self.assertEqual(blocked.check_name, "OUMV_BYPASS")

            # Health checks and unrelated ports stay allowed
            ok_health = TokenSafeGuard.inspect_command(f"curl http://127.0.0.1:{port}/health", cwd=self.test_dir)
            self.assertFalse(ok_health.blocked)
            ok_other = TokenSafeGuard.inspect_command("curl http://localhost:9999/test", cwd=self.test_dir)
            self.assertFalse(ok_other.blocked)

            # Kill switch
            with patch.dict(os.environ, {"TOKENSAFE_OUMV_CURL_GUARD": "0"}):
                bypass = TokenSafeGuard.inspect_command(f"curl http://127.0.0.1:{port}/api/users", cwd=self.test_dir)
                self.assertFalse(bypass.blocked)

    def test_stale_runtime_descriptor_does_not_block_or_report_connected(self):
        dead_port = self._dead_port()
        with patch.dict(os.environ, {"TOKENSAFE_HOME": self.tmp_home, "TOKENSAFE_DATA_DIR": self.tmp_data}):
            rt = OUMVGateway.global_runtime_file(self.test_dir)
            os.makedirs(os.path.dirname(rt), exist_ok=True)
            with open(rt, "w", encoding="utf-8") as f:
                json.dump({"frontend": {"name": "dead", "url": f"http://127.0.0.1:{dead_port}/__oumv/rpc"}}, f)

            # A descriptor pointing at a closed port is not a live runtime
            self.assertEqual(OUMVGateway.get_live_ports(self.test_dir), [])
            self.assertFalse(
                TokenSafeGuard.inspect_command(f"curl http://127.0.0.1:{dead_port}/api/users", cwd=self.test_dir).blocked
            )

            with patch.object(OUMVGateway, "_probe_candidates", return_value=None):
                st = OUMVGateway.status(workspace_dir=self.test_dir)
            self.assertNotEqual(st["frontend"].get("status", ""), "CONNECTED")
            self.assertIsNone(OUMVGateway._cached_fe)
            self.assertTrue(st.get("staleRuntimes"))

    def test_expired_runtime_descriptor_is_ignored(self):
        port = self._start_listener()
        with patch.dict(os.environ, {"TOKENSAFE_HOME": self.tmp_home}):
            rt = OUMVGateway.global_runtime_file(self.test_dir)
            os.makedirs(os.path.dirname(rt), exist_ok=True)
            with open(rt, "w", encoding="utf-8") as f:
                json.dump({"frontend": {"name": "react", "url": f"http://127.0.0.1:{port}/__oumv/rpc"}}, f)
            old = time.time() - (OUMVGateway._DESCRIPTOR_MAX_AGE_SEC + 60)
            os.utime(rt, (old, old))

            self.assertEqual(OUMVGateway.get_live_ports(self.test_dir), [])
            self.assertFalse(
                TokenSafeGuard.inspect_command(f"curl http://127.0.0.1:{port}/api/users", cwd=self.test_dir).blocked
            )

    def test_read_edit_advisory_fires_once_for_supported_stack_only(self):
        web_dir, api_dir = self._create_react_plus_node_monorepo()
        tsx_path = os.path.join(web_dir, "src", "App.tsx")
        node_path = os.path.join(api_dir, "src", "server.js")

        with patch.dict(os.environ, {"TOKENSAFE_HOME": self.tmp_home, "TOKENSAFE_DATA_DIR": self.tmp_data}):
            # Unsupported Node file gets no OUMV nudge
            self.assertIsNone(get_oumv_read_edit_advisory(self.test_dir, node_path, session_id="s1"))

            # First Read on React file emits one-shot advisory via CC PreToolUse
            res1 = evaluate_cc_pre_tool_use(
                {"tool_name": "Read", "tool_input": {"file_path": tsx_path}, "cwd": self.test_dir, "session_id": "s1"},
                agent="claude_code",
            )
            ctx1 = (res1.get("hookSpecificOutput") or {}).get("additionalContext", "")
            self.assertIn("TokenSafe OUMV", ctx1)

            # Second Read in same session stays silent
            res2 = evaluate_cc_pre_tool_use(
                {"tool_name": "Read", "tool_input": {"file_path": tsx_path}, "cwd": self.test_dir, "session_id": "s1"},
                agent="claude_code",
            )
            self.assertEqual(res2, {})

            # Separate session stays silent once OUMV has been called in workspace
            OUMVGateway.mark_oumv_used(self.test_dir)
            self.assertIsNone(get_oumv_read_edit_advisory(self.test_dir, tsx_path, session_id="s2"))

    def test_edit_in_oumv_stack_never_auto_approves(self):
        """An OUMV nudge must never ride on permissionDecision:"allow" for edits.

        "allow" bypasses the host permission prompt, so an advisory would silently
        auto-approve every edit inside a supported stack. The nudge is delivered via
        additionalContext alone, which keeps the normal permission flow intact.
        """
        web_dir, _ = self._create_react_plus_node_monorepo()
        tsx_path = os.path.join(web_dir, "src", "App.tsx")
        payload = {
            "tool_name": "Edit",
            "tool_input": {"file_path": tsx_path, "old_string": "Save", "new_string": "Store"},
            "cwd": self.test_dir,
            "session_id": "s1",
        }
        with patch.dict(os.environ, {"TOKENSAFE_HOME": self.tmp_home, "TOKENSAFE_DATA_DIR": self.tmp_data}):
            with patch("src.adapter_core.get_oumv_read_edit_advisory", return_value="[TokenSafe OUMV]: nudge"):
                res = evaluate_cc_pre_tool_use(dict(payload), agent="claude_code")
        self.assertNotIn("permissionDecision", res.get("hookSpecificOutput", {}))
        self.assertIn("TokenSafe OUMV", res["hookSpecificOutput"]["additionalContext"])

        # Kill switch
        with patch.dict(os.environ, {
            "TOKENSAFE_HOME": self.tmp_home,
            "TOKENSAFE_DATA_DIR": self.tmp_data,
            "TOKENSAFE_OUMV_EDIT_NUDGE": "0",
        }):
            with patch("src.adapter_core.get_oumv_read_edit_advisory", return_value="[TokenSafe OUMV]: nudge"):
                off = evaluate_cc_pre_tool_use(dict(payload), agent="claude_code")
        self.assertEqual(off, {})

        # Hosts without a documented additionalContext contract stay silent on edit.
        zcode_payload = dict(payload)
        zcode_payload["session_id"] = "s_zcode"  # separate session: avoid the loop detector
        with patch.dict(os.environ, {"TOKENSAFE_HOME": self.tmp_home, "TOKENSAFE_DATA_DIR": self.tmp_data}):
            with patch("src.adapter_core.get_oumv_read_edit_advisory", return_value="[TokenSafe OUMV]: nudge"):
                zcode = evaluate_cc_pre_tool_use(zcode_payload, agent="zcode")
        self.assertEqual(zcode, {})

    def test_installer_ensures_tokensafe_ui_global_and_updates_rules(self):
        fake_npm = "/usr/local/bin/npm"
        calls = []

        def fake_run(cmd, **_kwargs):
            calls.append(cmd)
            m = MagicMock()
            m.returncode = 0
            m.stdout = "" if "list" in cmd else "added 1 package"
            m.stderr = ""
            return m

        with patch.object(AgentInstaller, "_find_npm", return_value=fake_npm), patch("subprocess.run", side_effect=fake_run):
            res = AgentInstaller._ensure_tokensafe_ui_global(install_if_missing=True)
            self.assertTrue(res["ok"])
            self.assertEqual(res["action"], "installed")
            self.assertTrue(any("tokensafe-ui@latest" in " ".join(c) for c in calls))

        self.assertIn("tokensafe dev", UNIFIED_TOKENSAFE_RULE)
        self.assertIn("tokensafe:oumv", POINTER_RULE)
        self.assertIn("backend/node", TOKENSAFE_DIRECTIVES)


class TestFastApiDevRewrite(unittest.TestCase):
    """`uvicorn` in a FastAPI workspace must go through the autoload wrapper."""

    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="tokensafe_fastapi_ws_")
        with open(os.path.join(self.ws, "requirements.txt"), "w", encoding="utf-8") as f:
            f.write("fastapi\nuvicorn\n")

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_rewrites_uvicorn_in_fastapi_workspace(self):
        from src.command_sanitizer import CommandSanitizer

        rewritten, _saved, reason = CommandSanitizer.sanitize("uvicorn app:app --reload", cwd=self.ws)
        self.assertEqual(rewritten, "tokensafe run-backend uvicorn app:app --reload")
        self.assertIn("auto-mounts OUMV", reason)

    def test_rewrites_python_dash_m_uvicorn(self):
        from src.command_sanitizer import CommandSanitizer

        rewritten, _saved, _reason = CommandSanitizer.sanitize("python3 -m uvicorn app.main:app", cwd=self.ws)
        self.assertEqual(rewritten, "tokensafe run-backend python3 -m uvicorn app.main:app")

    def test_leaves_uvicorn_alone_outside_fastapi(self):
        from src.command_sanitizer import CommandSanitizer

        other = tempfile.mkdtemp(prefix="tokensafe_plain_ws_")
        try:
            rewritten, _saved, _reason = CommandSanitizer.sanitize("uvicorn app:app", cwd=other)
            self.assertIsNone(rewritten)
        finally:
            shutil.rmtree(other, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
