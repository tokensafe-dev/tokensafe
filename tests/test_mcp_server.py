import unittest
import json
import io
import time
from unittest.mock import patch
from src.mcp_server import handle_tool_call, _execute_with_timeout, run_mcp_server, TOOLS, FULL_TOOLS

class TestMCPServer(unittest.TestCase):
    def setUp(self):
        import os
        import shutil
        import tempfile
        from src.paths import get_active_workspace, save_active_workspace
        self._orig_ws = get_active_workspace()
        self._orig_data_dir = os.environ.get("TOKENSAFE_DATA_DIR")
        self._orig_db_path = os.environ.get("TOKENSAFE_DB_PATH")
        self._tmp_data = tempfile.mkdtemp(prefix="tokensafe_mcp_test_")
        self.addCleanup(shutil.rmtree, self._tmp_data, ignore_errors=True)
        os.environ["TOKENSAFE_DATA_DIR"] = self._tmp_data
        os.environ["TOKENSAFE_DB_PATH"] = os.path.join(self._tmp_data, "metrics.db")
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        save_active_workspace(repo_root)

    def tearDown(self):
        import os
        from src.paths import save_active_workspace
        if self._orig_data_dir is None:
            os.environ.pop("TOKENSAFE_DATA_DIR", None)
        else:
            os.environ["TOKENSAFE_DATA_DIR"] = self._orig_data_dir
        if self._orig_db_path is None:
            os.environ.pop("TOKENSAFE_DB_PATH", None)
        else:
            os.environ["TOKENSAFE_DB_PATH"] = self._orig_db_path
        if self._orig_ws:
            save_active_workspace(self._orig_ws)

    def test_lean_profile_advertises_only_the_core_tools(self):
        """Every advertised tool is re-sent in every request; `lean` is the profile the
        docs recommend for a coding session."""
        from src.mcp_server import TOOLS, _LEAN_TOOL_NAMES
        lean = [t for t in TOOLS if t["name"] in _LEAN_TOOL_NAMES]
        self.assertEqual(
            sorted(t["name"] for t in lean),
            ["apply_edits", "oumv", "repo_map", "scoped_search", "smart_read_file"],
        )
        self.assertLess(len(lean), len(TOOLS))

    def test_tools_list_has_all_schemas(self):
        tool_names = [t["name"] for t in TOOLS]
        expected_tools = [
            "smart_read_file", "scoped_search", "apply_edits",
            "repo_map", "session_context", "oumv", "run_helper",
        ]
        self.assertEqual(len(tool_names), 7)
        for t in expected_tools:
            self.assertIn(t, tool_names)
            matching = [s for s in TOOLS if s["name"] == t][0]
            self.assertTrue(len(matching["description"]) > 10)
            self.assertEqual(matching["inputSchema"]["type"], "object")
        self.assertEqual(len(FULL_TOOLS), 28)

    def test_consolidated_tools_routing_and_legacy_aliases(self):
        # 1. repo_map -> tree/graph/api
        tree_res = handle_tool_call("repo_map", {"view": "tree", "path": "src", "depth": 2})
        self.assertNotIn("isError", tree_res)
        self.assertIn("adapter_core.py", tree_res["content"][0]["text"])

        # 2. run_helper -> compress/guard/sanitize
        comp_res = handle_tool_call("run_helper", {"action": "compress", "prompt": "Слушай, перепиши функцию get_balance в wallet.ts"})
        self.assertNotIn("isError", comp_res)
        self.assertIn("compact_text", json.loads(comp_res["content"][0]["text"]))

        guard_res = handle_tool_call("run_helper", {"action": "guard", "command": "git add .env"})
        self.assertTrue(json.loads(guard_res["content"][0]["text"]).get("blocked"))

        # 3. session_context -> metrics / budget
        met_res = handle_tool_call("session_context", {"action": "metrics"})
        self.assertNotIn("isError", met_res)
        bud_res = handle_tool_call("session_context", {"action": "budget", "model": "claude-sonnet-4"})
        self.assertNotIn("isError", bud_res)

        # 4. oumv -> status
        oumv_res = handle_tool_call("oumv", {"action": "status"})
        self.assertNotIn("isError", oumv_res)

        # 5. scoped_search batch mode
        batch_res = handle_tool_call("scoped_search", {"queries": [{"pattern": "def handle_tool_call", "type": "text", "path": "src"}]})
        self.assertNotIn("isError", batch_res)
        self.assertIn("mcp_server.py", batch_res["content"][0]["text"])

        # 6. tools/list full profile opt-in
        import os
        stdin_data = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
        fake_out = io.StringIO()
        with patch.dict(os.environ, {"TOKENSAFE_MCP_PROFILE": "full"}), \
             patch("sys.stdin", io.StringIO(stdin_data)), \
             patch("sys.stdout", fake_out):
            run_mcp_server()
        resp = json.loads(fake_out.getvalue().strip())
        self.assertEqual(len(resp["result"]["tools"]), 28)

    def test_compress_prompt_mcp(self):
        res = handle_tool_call("compress_prompt", {"prompt": "Слушай, перепиши функцию get_balance в wallet.ts"})
        self.assertNotIn("isError", res)
        content = res["content"][0]["text"]
        data = json.loads(content)
        self.assertIn("compact_text", data)

    @patch("src.license_manager.LicenseManager.check_quota", return_value={"is_quota_exceeded": False})
    def test_sanitize_command_mcp(self, mock_quota):
        res = handle_tool_call("sanitize_command", {"command": "git diff HEAD~1"})
        self.assertNotIn("isError", res)
        content = res["content"][0]["text"]
        data = json.loads(content)
        self.assertIn("':!package-lock.json'", data["sanitized_command"])
        self.assertFalse(data.get("blocked", False))

    @patch("src.license_manager.LicenseManager.check_quota", return_value={"is_quota_exceeded": False})
    def test_sanitize_command_cmd_alias_mcp(self, mock_quota):
        res = handle_tool_call("sanitize_command", {"cmd": "npx tsc --noEmit"})
        self.assertNotIn("isError", res)
        content = res["content"][0]["text"]
        data = json.loads(content)
        self.assertIn("tokensafe wrap", data["sanitized_command"])
        self.assertIn("npx tsc --noEmit", data["sanitized_command"])
        self.assertFalse(data.get("blocked", False))

    def test_sanitize_command_guard_blocked_mcp(self):
        res = handle_tool_call("sanitize_command", {"command": "rm -rf /"})
        self.assertNotIn("isError", res)
        content = res["content"][0]["text"]
        data = json.loads(content)
        self.assertTrue(data.get("blocked", False))
        self.assertEqual(data.get("check_name"), "DESTRUCTIVE_RM")

    def test_guard_inspect_command_mcp(self):
        res = handle_tool_call("guard_inspect", {"command": "git add .env"})
        self.assertNotIn("isError", res)
        content = res["content"][0]["text"]
        data = json.loads(content)
        self.assertTrue(data.get("blocked"))
        self.assertFalse(data.get("passed"))
        self.assertEqual(data["checks"][0]["check_name"], "GIT_SECRET_STAGING")

    def test_guard_inspect_cmd_alias_mcp(self):
        res = handle_tool_call("guard_inspect", {"cmd": "git add .env"})
        self.assertNotIn("isError", res)
        content = res["content"][0]["text"]
        data = json.loads(content)
        self.assertTrue(data.get("blocked"))
        self.assertFalse(data.get("passed"))
        self.assertEqual(data["checks"][0]["check_name"], "GIT_SECRET_STAGING")

    def test_guard_inspect_code_mcp(self):
        bad_code = 'const key = process.env.JWT_SECRET || "production_secret_token_123456789";'
        res = handle_tool_call("guard_inspect", {"code": bad_code, "file_path": "src/auth.ts"})
        self.assertNotIn("isError", res)
        content = res["content"][0]["text"]
        data = json.loads(content)
        self.assertTrue(data.get("blocked"))
        self.assertEqual(data["checks"][0]["check_name"], "SECRET_FALLBACK")

    def test_list_dir_mcp(self):
        res = handle_tool_call("list_dir", {"path": "src", "depth": 2})
        self.assertNotIn("isError", res)
        text = res["content"][0]["text"]
        self.assertIn("📁 **src/**", text)
        self.assertIn("adapter_core.py", text)
        self.assertIn("command_sanitizer.py", text)

    def test_tool_exception_returns_fallback_guidance(self):
        with patch("src.mcp_server._dispatch_tool_call", side_effect=RuntimeError("boom")):
            res = handle_tool_call("list_dir", {"path": "src"})
        self.assertTrue(res.get("isError"))
        text = res["content"][0]["text"]
        self.assertIn("Try an alternative approach", text)
        self.assertIn("RuntimeError: boom", text)

    def test_execute_with_timeout_recovers_without_hanging(self):
        def _slow_handler(*args, **kwargs):
            time.sleep(0.5)
            return {"content": [{"type": "text", "text": "ok"}]}

        with patch("src.mcp_server.handle_tool_call", side_effect=_slow_handler):
            res = _execute_with_timeout("scoped_search", {"query": "test"}, timeout=0.05)
        self.assertTrue(res.get("isError"))
        self.assertIn("timeout", res["content"][0]["text"])
        self.assertIn("Try an alternative approach", res["content"][0]["text"])

    def test_notifications_cancelled_ignored_without_error_response(self):
        stdin_data = (
            json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}) + "\n" +
            json.dumps({"jsonrpc": "2.0", "id": 99, "method": "tools/list"}) + "\n"
        )
        fake_in = io.StringIO(stdin_data)
        fake_out = io.StringIO()
        with patch("sys.stdin", fake_in), patch("sys.stdout", fake_out):
            run_mcp_server()
        lines = [line for line in fake_out.getvalue().strip().splitlines() if line]
        self.assertEqual(len(lines), 1)
        resp = json.loads(lines[0])
        self.assertEqual(resp["id"], 99)
        self.assertIn("tools", resp["result"])

    def test_run_mcp_server_exits_cleanly_after_disk_upgrade(self):
        # Two tools/call requests queued, but watched mtimes change after the 1st request
        stdin_data = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_dir", "arguments": {"path": "src"}}}) + "\n" +
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_dir", "arguments": {"path": "src"}}}) + "\n"
        )
        fake_in = io.StringIO(stdin_data)
        fake_out = io.StringIO()
        mtimes_seq = [(100.0, 100.0, None), (200.0, 100.0, None)]

        with patch("sys.stdin", fake_in), \
             patch("sys.stdout", fake_out), \
             patch("src.mcp_server._get_watched_mtimes", side_effect=mtimes_seq):
            run_mcp_server()

        lines = [line for line in fake_out.getvalue().strip().splitlines() if line]
        # Only the 1st request is answered; server then exits cleanly so host IDE respawns new code
        self.assertEqual(len(lines), 1)
        resp = json.loads(lines[0])
        self.assertEqual(resp["id"], 1)
        self.assertFalse(resp["result"].get("isError", False))

    def test_inline_verify_gets_its_own_timeout_budget(self):
        """The edit is on disk before verify runs: cutting the call at the 8s transport
        default loses the verdict and reports 'could not complete' about work that did."""
        from src.mcp_server import _tool_timeout, MCP_TOOL_TIMEOUT
        self.assertEqual(_tool_timeout("smart_read_file", {}), MCP_TOOL_TIMEOUT)
        self.assertGreater(_tool_timeout("apply_edits", {"verify": "auto"}), MCP_TOOL_TIMEOUT)
        self.assertGreater(_tool_timeout("apply_edits", {"verify": "go test", "verify_timeout_sec": 60}), 60)
        self.assertLessEqual(_tool_timeout("apply_edits", {"verify": "x", "verify_timeout_sec": 900}), 180.0)
        self.assertEqual(_tool_timeout("apply_edits", {"edits": []}), MCP_TOOL_TIMEOUT)

    def test_small_file_is_returned_raw_not_skeletonized(self):
        """A skeleton below the crossover costs a second call: the caller asked for the
        file, got signatures, and re-read the body. Measured on a 155-line Go app."""
        import os
        import tempfile
        import uuid
        from src.session_context import session_scope
        from src.skeletonizer import Skeletonizer
        with tempfile.TemporaryDirectory() as tmpdir:
            old_db = os.environ.get("TOKENSAFE_DB_PATH")
            old_data = os.environ.get("TOKENSAFE_DATA_DIR")
            os.environ["TOKENSAFE_DATA_DIR"] = tmpdir
            os.environ["TOKENSAFE_DB_PATH"] = os.path.join(tmpdir, "metrics.db")
            try:
                small = os.path.join(tmpdir, "small.py")
                body = "".join(f"    value_{i} = {i}\n" for i in range(Skeletonizer.MIN_SKELETON_LINES - 40))
                with open(small, "w", encoding="utf-8") as f:
                    f.write(f"def handler():\n{body}    return value_0\n")
                big = os.path.join(tmpdir, "big.py")
                with open(big, "w", encoding="utf-8") as f:
                    f.write(f"def handler():\n{body}\n{body}\n    return value_0\n")
                with session_scope(root=tmpdir, session_id=f"test:smallfile:{uuid.uuid4().hex[:8]}"):
                    res_small = handle_tool_call("smart_read_file", {"path": small})
                    res_big = handle_tool_call("smart_read_file", {"path": big})
                txt_small = res_small["content"][0]["text"]
                txt_big = res_big["content"][0]["text"]
                self.assertNotIn("AST Skeleton", txt_small)
                self.assertIn("value_7 = 7", txt_small)
                self.assertIn("AST Skeleton", txt_big)
            finally:
                if old_db is None:
                    os.environ.pop("TOKENSAFE_DB_PATH", None)
                else:
                    os.environ["TOKENSAFE_DB_PATH"] = old_db
                if old_data is None:
                    os.environ.pop("TOKENSAFE_DATA_DIR", None)
                else:
                    os.environ["TOKENSAFE_DATA_DIR"] = old_data

    def test_smart_read_file_symbol_inside_closure_returns_enclosing_body(self):
        """A `symbol` that is not a declaration must not yield a folded whole-file
        skeleton claiming "Full body": the caller cannot edit from it and re-reads
        the file (measured as zero read savings in the OUMV agent matrix)."""
        import os
        import tempfile
        import uuid
        from src.session_context import session_scope
        with tempfile.TemporaryDirectory() as tmpdir:
            old_db = os.environ.get("TOKENSAFE_DB_PATH")
            old_data = os.environ.get("TOKENSAFE_DATA_DIR")
            os.environ["TOKENSAFE_DATA_DIR"] = tmpdir
            os.environ["TOKENSAFE_DB_PATH"] = os.path.join(tmpdir, "metrics.db")
            try:
                app = os.path.join(tmpdir, "app.go")
                lines = ["package demo", "", 'import "net/http"', "", "type App struct {", "\tMux *http.ServeMux", "}", "",
                         "func NewApp(mux *http.ServeMux) *App {"]
                for i in range(20):
                    lines.append(f'\tmux.HandleFunc("/api/items{i}", func(w http.ResponseWriter, r *http.Request) {{')
                    lines.append("\t\tw.WriteHeader(http.StatusOK)")
                    lines.append("\t})")
                lines += ["\treturn &App{Mux: mux}", "}", ""]
                with open(app, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines))
                with session_scope(root=tmpdir, session_id=f"test:anchor:{uuid.uuid4().hex[:8]}"):
                    res = handle_tool_call("smart_read_file", {"path": app, "symbol": "items7"})
                txt = res["content"][0]["text"]
                self.assertIn("Text-Anchor Slice", txt)
                self.assertNotIn("Full body", txt)
                self.assertIn("NewApp", txt)
                self.assertIn("items7", txt)
                self.assertLess(len(txt), len("\n".join(lines)))
            finally:
                if old_db is None:
                    os.environ.pop("TOKENSAFE_DB_PATH", None)
                else:
                    os.environ["TOKENSAFE_DB_PATH"] = old_db
                if old_data is None:
                    os.environ.pop("TOKENSAFE_DATA_DIR", None)
                else:
                    os.environ["TOKENSAFE_DATA_DIR"] = old_data

    def test_smart_read_file_endpoint_target_anchors_on_route(self):
        import os
        import tempfile
        import uuid
        from src.session_context import session_scope
        with tempfile.TemporaryDirectory() as tmpdir:
            old_db = os.environ.get("TOKENSAFE_DB_PATH")
            old_data = os.environ.get("TOKENSAFE_DATA_DIR")
            os.environ["TOKENSAFE_DATA_DIR"] = tmpdir
            os.environ["TOKENSAFE_DB_PATH"] = os.path.join(tmpdir, "metrics.db")
            try:
                app = os.path.join(tmpdir, "routes.go")
                lines = ["package demo", "", 'import "net/http"', "", "func NewApp(mux *http.ServeMux) *http.ServeMux {"]
                for i in range(25):
                    name = f"/api/items{i}"
                    lines.append(f'\tmux.HandleFunc("{name}", func(w http.ResponseWriter, r *http.Request) {{')
                    lines.append(f"\t\tw.WriteHeader(http.StatusAccepted)  // {name}")
                    lines.append("\t})")
                lines.append('\tmux.HandleFunc("/api/orders", func(w http.ResponseWriter, r *http.Request) {')
                lines.append("\t\tw.WriteHeader(http.StatusAccepted)  // /api/orders")
                lines.append("\t})")
                lines += ["\treturn mux", "}", ""]
                with open(app, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines))
                with session_scope(root=tmpdir, session_id=f"test:anchor:{uuid.uuid4().hex[:8]}"):
                    res = handle_tool_call("smart_read_file", {"path": app, "symbol": "POST /api/orders"})
                txt = res["content"][0]["text"]
                self.assertIn("Text-Anchor Slice", txt)
                # exactly one route matched (not every HandleFunc line)
                self.assertRegex(txt, r"it occurs at L\d+ \(matched")
                self.assertIn("/api/orders", txt)
                self.assertLess(len(txt), len("\n".join(lines)) * 0.6)
            finally:
                if old_db is None:
                    os.environ.pop("TOKENSAFE_DB_PATH", None)
                else:
                    os.environ["TOKENSAFE_DB_PATH"] = old_db
                if old_data is None:
                    os.environ.pop("TOKENSAFE_DATA_DIR", None)
                else:
                    os.environ["TOKENSAFE_DATA_DIR"] = old_data

    def test_smart_read_file_absent_symbol_reports_declarations(self):
        import os
        import tempfile
        import uuid
        from src.session_context import session_scope
        with tempfile.TemporaryDirectory() as tmpdir:
            old_db = os.environ.get("TOKENSAFE_DB_PATH")
            old_data = os.environ.get("TOKENSAFE_DATA_DIR")
            os.environ["TOKENSAFE_DATA_DIR"] = tmpdir
            os.environ["TOKENSAFE_DB_PATH"] = os.path.join(tmpdir, "metrics.db")
            try:
                mod = os.path.join(tmpdir, "svc.py")
                with open(mod, "w", encoding="utf-8") as f:
                    f.write("def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n")
                with session_scope(root=tmpdir, session_id=f"test:anchor:{uuid.uuid4().hex[:8]}"):
                    res = handle_tool_call("smart_read_file", {"path": mod, "symbol": "gamma"})
                txt = res["content"][0]["text"]
                self.assertIn("does not occur", txt)
                self.assertIn("alpha", txt)
                self.assertLess(len(txt), 400)
            finally:
                if old_db is None:
                    os.environ.pop("TOKENSAFE_DB_PATH", None)
                else:
                    os.environ["TOKENSAFE_DB_PATH"] = old_db
                if old_data is None:
                    os.environ.pop("TOKENSAFE_DATA_DIR", None)
                else:
                    os.environ["TOKENSAFE_DATA_DIR"] = old_data

    def test_smart_read_file_declared_symbol_still_uses_skeleton(self):
        import os
        import tempfile
        import uuid
        from src.session_context import session_scope
        with tempfile.TemporaryDirectory() as tmpdir:
            old_db = os.environ.get("TOKENSAFE_DB_PATH")
            old_data = os.environ.get("TOKENSAFE_DATA_DIR")
            os.environ["TOKENSAFE_DATA_DIR"] = tmpdir
            os.environ["TOKENSAFE_DB_PATH"] = os.path.join(tmpdir, "metrics.db")
            try:
                mod = os.path.join(tmpdir, "service.py")
                body = "".join(f"    value_{i} = {i}\n" for i in range(40))
                with open(mod, "w", encoding="utf-8") as f:
                    f.write(f"def target_fn():\n{body}    return value_0\n\n\ndef other_fn():\n    return 0\n")
                with session_scope(root=tmpdir, session_id=f"test:anchor:{uuid.uuid4().hex[:8]}"):
                    res = handle_tool_call("smart_read_file", {"path": mod, "symbol": "target_fn"})
                txt = res["content"][0]["text"]
                self.assertNotIn("Text-Anchor Slice", txt)
                self.assertIn("target_fn", txt)
            finally:
                if old_db is None:
                    os.environ.pop("TOKENSAFE_DB_PATH", None)
                else:
                    os.environ["TOKENSAFE_DB_PATH"] = old_db
                if old_data is None:
                    os.environ.pop("TOKENSAFE_DATA_DIR", None)
                else:
                    os.environ["TOKENSAFE_DATA_DIR"] = old_data

    def test_smart_read_file_dep_mode_signature_smaller_than_full(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            old_db = os.environ.get("TOKENSAFE_DB_PATH")
            old_data = os.environ.get("TOKENSAFE_DATA_DIR")
            os.environ["TOKENSAFE_DATA_DIR"] = tmpdir
            os.environ["TOKENSAFE_DB_PATH"] = os.path.join(tmpdir, "metrics.db")
            try:
                mod_path = os.path.join(tmpdir, "service.py")
                with open(mod_path, "w", encoding="utf-8") as f:
                    f.write(
                        "import os\nimport sys\nimport json\nimport time\nimport re\nimport math\n\n"
                        "def helper_dep(x):\n" + "    x = x + 1\n" * 20 + "    return x\n\n"
                        "def target_fn(v):\n    return helper_dep(v)\n"
                    )
                res_sig = handle_tool_call("smart_read_file", {
                    "path": mod_path, "symbol": "target_fn", "dep_mode": "signature"
                })
                res_full = handle_tool_call("smart_read_file", {
                    "path": mod_path, "symbol": "target_fn", "dep_mode": "full"
                })
                txt_sig = res_sig["content"][0]["text"]
                txt_full = res_full["content"][0]["text"]
                self.assertIn("def target_fn", txt_sig)
                self.assertIn("def helper_dep", txt_sig)
                self.assertIn("Collapsed 6 imports", txt_sig)
                self.assertLess(len(txt_sig), len(txt_full))
            finally:
                if old_db is None:
                    os.environ.pop("TOKENSAFE_DB_PATH", None)
                else:
                    os.environ["TOKENSAFE_DB_PATH"] = old_db
                if old_data is None:
                    os.environ.pop("TOKENSAFE_DATA_DIR", None)
                else:
                    os.environ["TOKENSAFE_DATA_DIR"] = old_data
                if old_data is None:
                    os.environ.pop("TOKENSAFE_DATA_DIR", None)
                else:
                    os.environ["TOKENSAFE_DATA_DIR"] = old_data

    def test_zero_config_pointer_and_force_full_over_mcp(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            old_db = os.environ.get("TOKENSAFE_DB_PATH")
            old_data = os.environ.get("TOKENSAFE_DATA_DIR")
            os.environ["TOKENSAFE_DATA_DIR"] = tmpdir
            os.environ["TOKENSAFE_DB_PATH"] = os.path.join(tmpdir, "metrics.db")
            try:
                target = os.path.join(tmpdir, "big.py")
                with open(target, "w", encoding="utf-8") as f:
                    f.write("def target():\n    return 1\n\n" + "\n".join(f"def extra_{i}(): return {i}" for i in range(40)))
                transcript = os.path.join(tmpdir, "transcript.jsonl")
                with open(transcript, "w", encoding="utf-8") as tf:
                    for _ in range(5):
                        tf.write(json.dumps({"content": "long context message " * 100}) + "\n")
                stdin_data = "\n".join([
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "claude-code"}}}),
                    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                    json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "smart_read_file", "arguments": {"path": target}}}),
                    json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "smart_read_file", "arguments": {"path": target}}}),
                    json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "smart_read_file", "arguments": {"path": target, "force_full": True}}}),
                    json.dumps({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "compact_conversation", "arguments": {"transcript_path": transcript, "invocation_num": 5}}}),
                    json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "smart_read_file", "arguments": {"path": target}}}),
                ]) + "\n"
                fake_in = io.StringIO(stdin_data)
                fake_out = io.StringIO()
                with patch("sys.stdin", fake_in), patch("sys.stdout", fake_out):
                    run_mcp_server()
                resps = [json.loads(ln) for ln in fake_out.getvalue().strip().splitlines() if ln.strip()]
                by_id = {r["id"]: r["result"] for r in resps}
                t3 = by_id[3]["content"][0]["text"]
                t4 = by_id[4]["content"][0]["text"]
                t5 = by_id[5]["content"][0]["text"]
                t7 = by_id[7]["content"][0]["text"]
                self.assertIn("def target", t3)
                self.assertIn("Pointer Hit", t4)
                self.assertIn("force_full=true", t4)
                self.assertIn("def target", t5)
                self.assertNotIn("Pointer Hit", t5)
                # After compact_conversation increments _EPOCH_GEN, pointer resets automatically
                self.assertIn("def target", t7)
                self.assertNotIn("Pointer Hit", t7)
            finally:
                if old_db is None:
                    os.environ.pop("TOKENSAFE_DB_PATH", None)
                else:
                    os.environ["TOKENSAFE_DB_PATH"] = old_db
                if old_data is None:
                    os.environ.pop("TOKENSAFE_DATA_DIR", None)
                else:
                    os.environ["TOKENSAFE_DATA_DIR"] = old_data

    def test_unknown_client_without_tools_list_skips_pointer_safely(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            old_db = os.environ.get("TOKENSAFE_DB_PATH")
            old_data = os.environ.get("TOKENSAFE_DATA_DIR")
            os.environ["TOKENSAFE_DATA_DIR"] = tmpdir
            os.environ["TOKENSAFE_DB_PATH"] = os.path.join(tmpdir, "metrics.db")
            try:
                target = os.path.join(tmpdir, "plain.py")
                with open(target, "w", encoding="utf-8") as f:
                    f.write("def target():\n    return 1\n\n" + "\n".join(f"def extra_{i}(): return {i}" for i in range(40)))
                stdin_data = "\n".join([
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "custom-minimal-bot"}}}),
                    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "smart_read_file", "arguments": {"path": target}}}),
                    json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "smart_read_file", "arguments": {"path": target}}}),
                ]) + "\n"
                fake_in = io.StringIO(stdin_data)
                fake_out = io.StringIO()
                with patch("sys.stdin", fake_in), patch("sys.stdout", fake_out):
                    run_mcp_server()
                resps = {r["id"]: r["result"] for r in (json.loads(ln) for ln in fake_out.getvalue().strip().splitlines() if ln.strip())}
                t3 = resps[3]["content"][0]["text"]
                self.assertIn("def target", t3)
                self.assertNotIn("Pointer Hit", t3)
            finally:
                if old_db is None:
                    os.environ.pop("TOKENSAFE_DB_PATH", None)
                else:
                    os.environ["TOKENSAFE_DB_PATH"] = old_db
                if old_data is None:
                    os.environ.pop("TOKENSAFE_DATA_DIR", None)
                else:
                    os.environ["TOKENSAFE_DATA_DIR"] = old_data

    def test_smart_read_file_relative_path_with_active_workspace(self):
        import os
        import tempfile
        from src.session_context import session_scope
        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = os.path.join(tmpdir, "service.py")
            with open(fpath, "w", encoding="utf-8") as f:
                f.write("def helper():\n    return 42\n")
            with session_scope(root=tmpdir, session_id="test:smart_read"):
                res = handle_tool_call("smart_read_file", {"path": "service.py", "symbol": "helper"})
            self.assertNotIn("isError", res)
            self.assertIn("def helper", res["content"][0]["text"])

    def test_list_dir_relative_path_with_active_workspace(self):
        import os
        import tempfile
        from src.session_context import session_scope
        with tempfile.TemporaryDirectory() as tmpdir:
            subdir = os.path.join(tmpdir, "sub")
            os.makedirs(subdir)
            with open(os.path.join(subdir, "file.txt"), "w") as f:
                f.write("hello")
            with session_scope(root=tmpdir, session_id="test:list_dir"):
                res = handle_tool_call("list_dir", {"path": "sub"})
            self.assertNotIn("isError", res)
            self.assertIn("file.txt", res["content"][0]["text"])

    def test_expand_ref_mcp_call(self):
        from src.stash import stash_block
        _, marker = stash_block("SECRET_FULL_PAYLOAD_ABC_123", source="test")
        hash8 = marker.split("#")[1].split(" ")[0]
        res = handle_tool_call("expand_ref", {"hash": hash8})
        self.assertNotIn("isError", res)
        self.assertEqual(res["content"][0]["text"], "SECRET_FULL_PAYLOAD_ABC_123")

    def test_get_mistakes_mcp_call(self):
        from src.mistakes import MistakesLedger
        MistakesLedger.record_revert("test_file.py", "Loop guard regression", project_root="/tmp")
        res = handle_tool_call("get_mistakes", {"project_path": "/tmp"})
        self.assertNotIn("isError", res)
        data = json.loads(res["content"][0]["text"])
        self.assertIn("entries", data)
        self.assertIn("active_guard", data)

    def test_optimize_image_mcp_call(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"not an image")
            tmp = f.name
        try:
            res = handle_tool_call("optimize_image", {"image_path": tmp})
            self.assertNotIn("isError", res)
            text = res["content"][0]["text"]
            self.assertIn(tmp, text)
        finally:
            import os
            if os.path.exists(tmp):
                os.unlink(tmp)

    def test_prune_superseded_mcp_call(self):
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"type": "tool_result", "content": "read 1"}\n')
            tmp = f.name
        try:
            res = handle_tool_call("prune_superseded", {"transcript_path": tmp})
            self.assertNotIn("isError", res)
            data = json.loads(res["content"][0]["text"])
            self.assertTrue(data.get("success"))
        finally:
            import os
            if os.path.exists(tmp):
                os.unlink(tmp)

    def test_read_web_page_mcp_call(self):
        res = handle_tool_call("read_web_page", {"url": "http://127.0.0.1:80/probe"})
        # Refused internal SSRF probe triggers fail-safe error message
        self.assertIn("Refused to fetch potentially unsafe or internal URL", res["content"][0]["text"])

    @patch("src.license_manager.LicenseManager.check_quota", return_value={"is_quota_exceeded": False})
    def test_get_architecture_graph_mcp_call(self, _mock_quota):
        res = handle_tool_call("get_architecture_graph", {"project_path": "."})
        self.assertNotIn("isError", res)
        self.assertIn("god_nodes", res["content"][0]["text"])

    @patch("src.license_manager.LicenseManager.check_quota", return_value={"is_quota_exceeded": False})
    def test_get_api_surface_mcp_call(self, _mock_quota):
        res = handle_tool_call("get_api_surface", {"project_path": "."})
        self.assertNotIn("isError", res)
        self.assertIn("API Surface", res["content"][0]["text"])

    @patch("src.license_manager.LicenseManager.check_quota", return_value={"is_quota_exceeded": False})
    def test_smart_bundle_mcp_call(self, _mock_quota):
        res = handle_tool_call("smart_bundle", {"targets": [{"path": "src/cli.py", "symbol": "main"}]})
        self.assertNotIn("isError", res)
        self.assertIn("def main", res["content"][0]["text"])

    def test_context_budget_mcp_call(self):
        res = handle_tool_call("context_budget", {"model": "claude-sonnet-4.0"})
        self.assertNotIn("isError", res)
        data = json.loads(res["content"][0]["text"])
        self.assertIn("window_tokens", data)
        self.assertIn("threshold", data)

    def test_slice_diff_mcp_call(self):
        diff_text = """diff --git a/src/cli.py b/src/cli.py
--- a/src/cli.py
+++ b/src/cli.py
@@ -10,3 +10,3 @@
-import sys
+import sys, os
"""
        res = handle_tool_call("slice_diff", {"diff": diff_text, "max_lines": 50})
        self.assertNotIn("isError", res)
        self.assertIn("diff --git", res["content"][0]["text"])

    def test_scoped_search_mcp_call(self):
        res = handle_tool_call("scoped_search", {"query": "TokenSafe", "path": ".", "scope": "code"})
        self.assertNotIn("isError", res)
        self.assertIn("Scoped Search", res["content"][0]["text"])

    @patch("src.metrics.MetricsTracker.record_event")
    @patch("src.format_parsers.lockfiles.LockfileReducer.query_dependency")
    def test_query_dependency_honest_metrics(self, mock_query, mock_record):
        # Found package: honest tokens derived from _lockfile_bytes
        mock_query.return_value = {
            "found": True,
            "package": "express",
            "version": "4.19.2",
            "_lockfile_bytes": 40000
        }
        res = handle_tool_call("query_dependency", {"package_name": "express"})
        self.assertNotIn("isError", res)
        data = json.loads(res["content"][0]["text"])
        self.assertNotIn("_lockfile_bytes", data)

        self.assertTrue(mock_record.called)
        ev = mock_record.call_args[1]
        self.assertEqual(ev["original_tokens"], 10000)  # 40000 // 4
        self.assertNotEqual(ev["original_tokens"], 50000)  # NOT hardcoded 50k!

        # Missing package: saved tokens must be 0 (orig == opt)
        mock_record.reset_mock()
        mock_query.return_value = {
            "found": False,
            "package": "non-existent",
            "_lockfile_bytes": 40000
        }
        res_missing = handle_tool_call("query_dependency", {"package_name": "non-existent"})
        ev_missing = mock_record.call_args[1]
        self.assertEqual(ev_missing["original_tokens"], ev_missing["optimized_tokens"])

    @patch("src.metrics.MetricsTracker.record_event")
    @patch("src.type_slicer.TypeSlicer.slice_package_type")
    def test_slice_external_type_honest_metrics(self, mock_slice, mock_record):
        # Found type: honest tokens derived from _dts_bytes
        mock_slice.return_value = {
            "found": True,
            "package": "stripe",
            "symbol": "StripeConfig",
            "type_code": "export interface StripeConfig { key: string; }",
            "_dts_bytes": 20000
        }
        res = handle_tool_call("slice_external_type", {"package_name": "stripe", "symbol": "StripeConfig"})
        self.assertNotIn("isError", res)
        data = json.loads(res["content"][0]["text"])
        self.assertNotIn("_dts_bytes", data)

        self.assertTrue(mock_record.called)
        ev = mock_record.call_args[1]
        self.assertEqual(ev["original_tokens"], 5000)  # 20000 // 4
        self.assertNotEqual(ev["original_tokens"], 15000)  # NOT hardcoded 15k!

        # Missing type: saved tokens must be 0
        mock_record.reset_mock()
        mock_slice.return_value = {
            "found": False,
            "package": "stripe",
            "symbol": "NonExistent",
            "_dts_bytes": 20000
        }
        res_missing = handle_tool_call("slice_external_type", {"package_name": "stripe", "symbol": "NonExistent"})
        ev_missing = mock_record.call_args[1]
        self.assertEqual(ev_missing["original_tokens"], ev_missing["optimized_tokens"])

    @patch("src.mistakes.MistakesLedger.get_team_mistakes")
    @patch("src.mistakes.MistakesLedger.list_mistakes")
    def test_get_mistakes_returns_team_entries(self, mock_list, mock_team):
        mock_list.return_value = [{"summary": "Local mistake"}]
        mock_team.return_value = [{"summary": "Team mistake from L3"}]

        res = handle_tool_call("get_mistakes", {})
        self.assertNotIn("isError", res)
        data = json.loads(res["content"][0]["text"])
        self.assertIn("team_entries", data)
        self.assertEqual(len(data["team_entries"]), 1)
        self.assertEqual(data["team_entries"][0]["summary"], "Team mistake from L3")

    def test_mcp_ping_and_malformed_json_args(self):
        stdin_data = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n" +
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_dir", "arguments": "not-json{{"}}) + "\n"
        )
        fake_in = io.StringIO(stdin_data)
        fake_out = io.StringIO()
        with patch("sys.stdin", fake_in), patch("sys.stdout", fake_out):
            run_mcp_server()
        lines = [json.loads(l) for l in fake_out.getvalue().strip().splitlines() if l.strip()]
        self.assertEqual(lines[0]["result"], {})
        self.assertEqual(lines[1]["id"], 2)
        self.assertTrue(lines[1]["result"].get("isError"))

    def test_scoped_search_blocks_scope_traversal_and_unsafe_batch_git(self):
        from src.batch_search import _is_git_command_safe
        res_trav = handle_tool_call("scoped_search", {"query": "x", "path": ".", "scope": "../"})
        self.assertTrue(res_trav.get("isError"))
        ok_b, err_b, _ = _is_git_command_safe("branch evil_new_branch")
        self.assertFalse(ok_b)
        ok_ro, _, _ = _is_git_command_safe("branch --show-current")
        self.assertTrue(ok_ro)


if __name__ == "__main__":
    unittest.main()
