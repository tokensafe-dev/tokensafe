"""
Comprehensive tests for:
1. PreToolUse parity across ALL supported agents (ZCode, Claude Code, Kimi, Cursor, Antigravity)
2. Cache-aware ROI gate in result_ledger.py & thinking_fold.py
3. thinking_fold default ON + proxy Effort Clamp (_apply_effort_clamp & pipeline)
4. Multi-agent Live Burn Monitor & transcript_analyzer (ZCode, Claude Code, Codex, Antigravity)
"""

import datetime
import io
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from src import metrics as metrics_mod
from src.burn_monitor import (
    collect_live_burn_snapshot,
    estimate_zai_credits,
    evaluate_burn_warnings,
    evaluate_fold_roi,
    get_burn_warnings,
    is_zai_peak_hour,
)
from src.result_ledger import FOLD_MARKER, fold_tool_results
from src.thinking_fold import THINKING_FOLD_MARKER, fold_aged_thinking
from src.transcript_analyzer import analyze_transcript, discover_transcript_paths


def _fresh_env(tmp: str):
    os.environ["TOKENSAFE_DATA_DIR"] = tmp
    os.environ["TOKENSAFE_DB_PATH"] = os.path.join(tmp, "metrics.db")
    os.environ["TOKENSAFE_TELEMETRY_DISABLED"] = "1"
    metrics_mod._INITIALIZED_METRICS_DBS.clear()


class TestPreToolUseAllAgents(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="tokensafe_pretool_all_")
        self.addCleanup(__import__("shutil").rmtree, self._tmp, ignore_errors=True)
        _fresh_env(self._tmp)
        for k in ("TOKENSAFE_DATA_DIR", "TOKENSAFE_DB_PATH", "TOKENSAFE_TELEMETRY_DISABLED"):
            self.addCleanup(os.environ.pop, k, None)

    def test_zcode_hook_pretooluse_contract_and_guards(self):
        from hooks import zcode_hook

        # Clean Read -> {} (never {"decision": "allow"})
        clean = zcode_hook.handle("PreToolUse", {
            "tool_name": "Read",
            "tool_input": {"file_path": "/repo/src/app.py"},
            "session_id": "zc_1",
        })
        self.assertEqual(clean, {})

        # Lockfile Read -> deny via hookSpecificOutput
        denied_read = zcode_hook.handle("PreToolUse", {
            "tool_name": "Read",
            "tool_input": {"file_path": "/repo/package-lock.json"},
            "session_id": "zc_1",
        })
        self.assertEqual(denied_read["hookSpecificOutput"]["permissionDecision"], "deny")

        # Large source file Read without bounds -> allow + additionalContext advisory
        big_py = os.path.join(self._tmp, "big_mod.py")
        with open(big_py, "w", encoding="utf-8") as f:
            for i in range(320):
                f.write(f"def fn_{i}(x):\n    return x + {i}  # padding comment for size\n")
        adv = zcode_hook.handle("PreToolUse", {
            "tool_name": "Read",
            "tool_input": {"file_path": big_py},
            "session_id": "zc_1",
        })
        self.assertEqual(adv["hookSpecificOutput"]["permissionDecision"], "allow")
        self.assertIn("smart_read_file", adv["hookSpecificOutput"]["additionalContext"])

        # Edit with secret -> deny
        edit_deny = zcode_hook.handle("PreToolUse", {
            "tool_name": "Edit",
            "tool_input": {"file_path": "cfg.py", "new_string": 'KEY = "sk_live_' + "a" * 24 + '1"'},
            "session_id": "zc_1",
        })
        self.assertEqual(edit_deny["hookSpecificOutput"]["permissionDecision"], "deny")

        # Bash destructive -> deny
        bash_deny = zcode_hook.handle("PreToolUse", {
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf / --no-preserve-root"},
            "session_id": "zc_1",
        })
        self.assertEqual(bash_deny["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_pre_tool_use_handles_zcode_cc_payload_without_toolcall(self):
        from hooks import pre_tool_use

        # ZCode / CC shape: tool_name without toolCall must return {} on allow
        res = pre_tool_use._handle({
            "tool_name": "Read",
            "tool_input": {"file_path": "/repo/src/ok.ts"},
            "session_id": "legacy_zc",
        })
        self.assertEqual(res, {})

        res_deny = pre_tool_use._handle({
            "tool_name": "Read",
            "tool_input": {"file_path": "/repo/bun.lock"},
            "session_id": "legacy_zc",
        })
        self.assertEqual(res_deny["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_claude_and_kimi_read_and_loop_guard(self):
        from hooks import claude_code_hook, kimi_hook

        # Claude Read lockfile denied
        cc_lock = claude_code_hook.handle("PreToolUse", {
            "tool_name": "Read",
            "tool_input": {"file_path": "/repo/yarn.lock"},
            "session_id": "cc_read_1",
        })
        self.assertEqual(cc_lock["hookSpecificOutput"]["permissionDecision"], "deny")

        # Kimi Edit stale check asks after PostToolUse recorded file (outside data_dir)
        work_dir = tempfile.mkdtemp(prefix="tokensafe_work_")
        self.addCleanup(__import__("shutil").rmtree, work_dir, ignore_errors=True)
        target = os.path.join(work_dir, "module.py")
        with open(target, "w", encoding="utf-8") as f:
            f.write("x = 1\n")
        kimi_hook.handle("PostToolUse", {
            "tool_name": "Read",
            "tool_input": {"file_path": target},
            "session_id": "kimi_stale_1",
        })
        # Modify mtime/content externally
        with open(target, "w", encoding="utf-8") as f:
            f.write("x = 2\n")
        os.utime(target, (os.path.getatime(target), os.path.getmtime(target) + 5))
        kimi_stale = kimi_hook.handle("PreToolUse", {
            "tool_name": "Edit",
            "tool_input": {"file_path": target, "new_string": "x = 3"},
            "session_id": "kimi_stale_1",
        })
        self.assertEqual(kimi_stale["hookSpecificOutput"]["permissionDecision"], "ask")

        # LoopDetector triggers after 3 identical clean calls
        for _ in range(3):
            res_loop = claude_code_hook.handle("PreToolUse", {
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
                "session_id": "cc_loop_1",
            })
        self.assertEqual(res_loop["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_cursor_loop_guard(self):
        from hooks import cursor_hook
        for _ in range(3):
            out = cursor_hook.handle("beforeShellExecution", {
                "command": "npm test",
                "conversation_id": "cur_loop_1",
            })
        self.assertEqual(out["permission"], "ask")

    def test_installer_wires_all_pretooluse_updates(self):
        from src import installer as inst_mod
        from src.installer import AgentInstaller

        fake_home = os.path.join(self._tmp, "home")
        os.makedirs(os.path.join(fake_home, ".zcode", "cli"), exist_ok=True)
        zcode_cfg = os.path.join(fake_home, ".zcode", "cli", "config.json")
        # Legacy pre_tool_use.py entry should be upgraded to zcode_hook.py
        with open(zcode_cfg, "w", encoding="utf-8") as f:
            json.dump({"hooks": {"enabled": False, "events": {
                "PreToolUse": [{"hooks": [{"type": "process", "command": "python3", "args": ["/old/pre_tool_use.py"]}]}]
            }}}, f)

        with patch.object(inst_mod, "HOME", fake_home):
            ok_z, _ = AgentInstaller.install_hooks_config("zcode")
            ok_c, _ = AgentInstaller.install_hooks_config("claude_code")
            ok_k, _ = AgentInstaller.install_hooks_config("kimi")
            ok_cur, _ = AgentInstaller.install_hooks_config("cursor")

        self.assertTrue(ok_z and ok_c and ok_k and ok_cur)
        z_data = json.load(open(zcode_cfg))
        self.assertTrue(z_data["hooks"]["events"]["PreToolUse"][0]["hooks"][0]["args"][-1].endswith("zcode_hook.py"))

        cc_data = json.load(open(os.path.join(fake_home, ".claude", "settings.json")))
        cc_matchers = {e.get("matcher") for e in cc_data["hooks"]["PreToolUse"]}
        self.assertIn("Read", cc_matchers)

        kimi_toml = open(os.path.join(fake_home, ".kimi-code", "config.toml")).read()
        self.assertIn('matcher = "Edit"', kimi_toml)

        cur_data = json.load(open(os.path.join(fake_home, ".cursor", "hooks.json")))
        self.assertIn("beforeEditFile", cur_data["hooks"])


class TestCacheAwareRoiAndEffortClamp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="tokensafe_roi_")
        self.addCleanup(__import__("shutil").rmtree, self._tmp, ignore_errors=True)
        _fresh_env(self._tmp)
        for k in ("TOKENSAFE_DATA_DIR", "TOKENSAFE_DB_PATH", "TOKENSAFE_TELEMETRY_DISABLED", "TOKENSAFE_EFFORT_CLAMP"):
            self.addCleanup(os.environ.pop, k, None)

    def _warm_long_messages(self):
        big_old = "\n".join(f"def old_fn_{i}(x):\n    return x + {i}" for i in range(140))
        huge_suffix = "context line with unique code tokens and module exports " * 6000  # ~60k+ tokens
        return [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": None,
             "_tokensafe_thinking": [{"type": "text", "text": "thinking trace " * 250}],
             "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": big_old},
            {"role": "user", "content": huge_suffix},
            {"role": "assistant", "content": "ack1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "ack2"},
            {"role": "user", "content": "u3"},
            {"role": "assistant", "content": "ack3"},
            {"role": "user", "content": "u4"},
        ]

    def test_roi_defers_warm_cache_fold_when_break_exceeds_savings(self):
        msgs = self._warm_long_messages()
        with patch("src.paths.resolve_data_dir", return_value=self._tmp):
            out_l, stats_l = fold_tool_results(msgs, model="GLM-5.3", cache_warm=True, record_metrics=False)
            out_t, stats_t = fold_aged_thinking(msgs, model="GLM-5.3", cache_warm=True, record=False)

        self.assertTrue(stats_l["deferred_roi"])
        self.assertEqual(stats_l["folded"], 0)
        self.assertNotIn(FOLD_MARKER, out_l[3]["content"])

        self.assertTrue(stats_t["deferred_roi"])
        self.assertEqual(stats_t["folded"], 0)
        self.assertNotIn(THINKING_FOLD_MARKER, out_t[2]["_tokensafe_thinking"][0]["text"])

        # After cache miss / epoch rollover (cache_warm=False), both fold cleanly
        with patch("src.paths.resolve_data_dir", return_value=self._tmp):
            out_l2, stats_l2 = fold_tool_results(msgs, model="GLM-5.3", cache_warm=False, record_metrics=False)
            out_t2, stats_t2 = fold_aged_thinking(out_l2, model="GLM-5.3", cache_warm=False, record=False)

        self.assertEqual(stats_l2["folded"], 1)
        self.assertEqual(stats_t2["folded"], 1)
        self.assertIn(FOLD_MARKER, out_l2[3]["content"])
        self.assertIn(THINKING_FOLD_MARKER, out_t2[2]["_tokensafe_thinking"][0]["text"])

    def test_effort_clamp_in_proxy(self):
        from src.proxy import TokenSafeProxyHandler

        captured = []

        class _FakeResp:
            status = 200
            def read(self):
                return json.dumps({
                    "id": "m1", "type": "message", "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": 100, "cache_read_input_tokens": 90, "output_tokens": 10},
                }).encode("utf-8")
            def getheaders(self):
                return [("Content-Type", "application/json")]
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=30.0):
            captured.append(json.loads(req.data.decode("utf-8")))
            return _FakeResp()

        def _send_anthropic(body_obj):
            raw = json.dumps(body_obj).encode("utf-8")
            h = TokenSafeProxyHandler.__new__(TokenSafeProxyHandler)
            h.headers = {"Content-Length": str(len(raw)), "x-api-key": "test-zai-key"}
            h.rfile = io.BytesIO(raw)
            h.wfile = io.BytesIO()
            h.send_response = MagicMock()
            h.send_header = MagicMock()
            h.end_headers = MagicMock()
            with patch.dict(os.environ, {"TOKENSAFE_UPSTREAM_URL": "https://api.z.ai/api/anthropic/v1/messages"}), \
                 patch("urllib.request.urlopen", side_effect=fake_urlopen):
                self.assertTrue(h._ingest_anthropic_body())
                h.handle_chat_completions()

        # 1. Tool-continuation turn -> clamped to "low"
        _send_anthropic({
            "model": "GLM-5.3",
            "thinking": {"type": "enabled"},
            "messages": [
                {"role": "user", "content": "run tests"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
            ],
        })
        self.assertEqual(captured[-1]["output_config"]["effort"], "low")

        # 2. Routine short user turn -> clamped to "medium"
        _send_anthropic({
            "model": "GLM-5.3",
            "thinking": {"type": "enabled"},
            "messages": [{"role": "user", "content": "add a typo fix to README"}],
        })
        self.assertEqual(captured[-1]["output_config"]["effort"], "medium")

        # 3. Deep architectural prompt -> not clamped
        _send_anthropic({
            "model": "GLM-5.3",
            "thinking": {"type": "enabled"},
            "messages": [{"role": "user", "content": "Design and architect the new distributed auth migration plan"}],
        })
        self.assertNotIn("output_config", captured[-1])


class TestMultiAgentBurnMonitor(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="tokensafe_burn_")
        self.addCleanup(__import__("shutil").rmtree, self._tmp, ignore_errors=True)
        _fresh_env(self._tmp)
        for k in ("TOKENSAFE_DATA_DIR", "TOKENSAFE_DB_PATH", "TOKENSAFE_TELEMETRY_DISABLED"):
            self.addCleanup(os.environ.pop, k, None)

    def test_zcode_rollout_discovery_analysis_and_warnings(self):
        home = os.path.join(self._tmp, "home")
        roll_dir = os.path.join(home, ".zcode", "cli", "rollout")
        log_dir = os.path.join(home, ".zcode", "cli", "log")
        os.makedirs(roll_dir, exist_ok=True)
        os.makedirs(log_dir, exist_ok=True)

        rp1 = os.path.join(roll_dir, "model-io-sess_abc.jsonl")
        rp2 = os.path.join(roll_dir, "model-io-sess_xyz.jsonl")
        rows = [
            {
                "type": "model_io",
                "startedAt": "2026-11-24T08:30:00Z",
                "model": {"modelId": "GLM-5.4"},
                "request": {"body": {"tools": [{"name": f"t{i}", "description": "x" * 1000} for i in range(70)]}},
                "response": {
                    "usage": {"inputTokens": 130000, "cacheReadTokens": 125000, "outputTokens": 500},
                    "reasoningText": "r" * 100,
                    "text": "ok",
                    "toolCalls": [{"toolName": "Read"}],
                },
            },
            {
                "type": "model_io",
                "startedAt": "2026-11-24T08:32:00Z",
                "model": {"modelId": "GLM-5.3"},
                "request": {"body": {}},
                "response": {
                    "usage": {"inputTokens": 135000, "cacheReadTokens": 20000, "outputTokens": 4000},
                    "reasoningText": "r" * 15000,
                    "text": "done",
                    "toolCalls": [{"toolName": "Edit"}],
                },
            },
        ]
        with open(rp1, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        with open(rp2, "w", encoding="utf-8") as f:
            f.write(json.dumps(rows[0]) + "\n")
        with open(os.path.join(log_dir, "zcode-2026-11-24.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"event": "model.request.failed", "context": {"code": "1308"}}) + "\n")

        with patch("os.path.expanduser", return_value=home):
            found = discover_transcript_paths(agent="zcode")
        self.assertEqual(len(found), 2)
        self.assertEqual(found[0][0], "zcode")

        res = analyze_transcript(rp1, agent_type="auto")
        self.assertEqual(res["agent"], "zcode")
        self.assertEqual(res["model"], "GLM-5.3")
        self.assertEqual(res["model_switches"], 1)
        self.assertGreater(res["est_credits"], 0)
        codes = {w["code"] for w in res["burn_warnings"]}
        self.assertTrue({"context_bloat", "cache_crash", "hidden_reasoning", "tool_schema_bloat"} <= codes)

        snap = collect_live_burn_snapshot(agent="zcode", session_id="sess_abc", home_dir=home)
        self.assertEqual(snap["active_parallel_sessions"], 2)
        # Ensure active session count is independent of glob order (Linux CI regression guard)
        with patch("glob.glob", return_value=[rp1, rp2]):
            snap_ordered = collect_live_burn_snapshot(agent="zcode", session_id="sess_abc", home_dir=home)
        self.assertEqual(snap_ordered["active_parallel_sessions"], 2)
        self.assertIn("1308", snap["rate_limit_hits"])
        # Peak hour (Tue 09:00 UTC = 17:00 SST)
        peak_dt = datetime.datetime(2026, 11, 24, 9, 0, tzinfo=datetime.timezone.utc)
        self.assertTrue(is_zai_peak_hour(peak_dt))
        warns = evaluate_burn_warnings(snap, now=peak_dt)
        self.assertTrue(any(w["code"] == "zai_peak_glm53" for w in warns))
        self.assertTrue(any(w["code"] == "parallel_sessions" for w in warns))
        self.assertTrue(any(w["code"] == "rate_limit_recent" for w in warns))

        # Rate-limited alert banner deduplicates across immediate repeat calls
        msg1 = get_burn_warnings(agent="zcode", session_id="sess_abc", home_dir=home)
        msg2 = get_burn_warnings(agent="zcode", session_id="sess_abc", home_dir=home)
        self.assertIn("Live Burn Guard", msg1)
        self.assertIsNone(msg2)

    def test_claude_and_codex_transcript_parsing(self):
        cc_path = os.path.join(self._tmp, "cc.jsonl")
        with open(cc_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "assistant",
                "timestamp": "2026-11-24T10:00:00Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-sonnet-4-5",
                    "usage": {"input_tokens": 2000, "cache_read_input_tokens": 18000, "output_tokens": 300},
                    "content": [{"type": "tool_use", "name": "Read"}],
                },
            }) + "\n")
        res_cc = analyze_transcript(cc_path, agent_type="claude")
        self.assertEqual(res_cc["input_tokens"], 20000)
        self.assertEqual(res_cc["cache_hit_pct"], 90.0)
        self.assertEqual(res_cc["total_tool_calls"], 1)

        # ZCode rollout using Anthropic key (inputTokens excludes cacheReadInputTokens)
        z_ant_path = os.path.join(self._tmp, "z_ant.jsonl")
        with open(z_ant_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "model_io",
                "startedAt": "2026-11-24T10:00:30Z",
                "model": {"modelId": "claude-sonnet-4-5"},
                "response": {
                    "usage": {"inputTokens": 2000, "cacheReadInputTokens": 18000, "outputTokens": 200},
                },
            }) + "\n")
        res_z_ant = analyze_transcript(z_ant_path, agent_type="zcode")
        self.assertEqual(res_z_ant["input_tokens"], 20000)
        self.assertEqual(res_z_ant["cache_hit_pct"], 90.0)
        self.assertLessEqual(res_z_ant["cache_hit_pct"], 100.0)

        codex_path = os.path.join(self._tmp, "codex.jsonl")
        with open(codex_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "event_msg",
                "timestamp": "2026-11-24T10:01:00Z",
                "payload": {"type": "token_count", "info": {"last_token_usage": {
                    "input_tokens": 10000, "cached_input_tokens": 8000, "output_tokens": 200
                }}},
            }) + "\n")
        res_cx = analyze_transcript(codex_path, agent_type="codex")
        self.assertEqual(res_cx["input_tokens"], 10000)
        self.assertEqual(res_cx["cache_read_tokens"], 8000)

    def test_session_start_injects_cumulative_banner_when_events_exist(self):
        from hooks import claude_code_hook, kimi_hook, session_start, zcode_hook
        from src.metrics import MetricsTracker

        tracker = MetricsTracker(db_path=os.environ["TOKENSAFE_DB_PATH"])
        tracker.record_event("s_cum", "SCOPED_SEARCH", 20000, 2000)

        # Claude Code / ZCode / Kimi SessionStart
        for mod in (claude_code_hook, zcode_hook, kimi_hook):
            out = mod.handle("SessionStart", {"session_id": "s_cum"})
            ctx = out["hookSpecificOutput"]["additionalContext"]
            self.assertIn("[TokenSafe Cumulative: 18.0K tokens saved", ctx)

        # Antigravity SessionStart
        with patch("src.license_manager.LicenseManager.check_quota", return_value={"is_quota_exceeded": False}), \
             patch("src.version_checker.VersionChecker.notify_os_update_background"), \
             patch("sys.stdin", io.StringIO(json.dumps({"conversationId": "s_cum", "workspacePaths": [self._tmp]}))), \
             patch("sys.stdout", new_callable=io.StringIO) as out_ag:
            session_start.main()
        ag_data = json.loads(out_ag.getvalue())
        ag_text = ag_data["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("[TokenSafe Cumulative: 18.0K tokens saved", ag_text)


if __name__ == "__main__":
    unittest.main()
