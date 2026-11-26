#!/usr/bin/env python3
"""
TokenSafe adapter for Claude Code lifecycle hooks.

Registered via ~/.claude/settings.json:
  UserPromptSubmit -> compacts the prompt and INJECTS the TOCP form as
                     additionalContext (verified inject point in the
                     official Claude Code hooks schema).
  PreToolUse (Bash) -> denies secret-bearing / destructive commands,
                     asks on TLS-verification bypasses.

Contract: stdin = one JSON payload, stdout = one JSON response.
Fail-open: any internal error emits `{}` (Claude treats empty stdout as allow).
"""

import sys
import json
import os

HOOK_DIR = os.path.dirname(os.path.realpath(__file__))
PROJECT_ROOT = os.path.dirname(HOOK_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.adapter_core import TOKENSAFE_DIRECTIVES, compact_for_injection, evaluate_cc_pre_tool_use

AGENT = "claude_code"


def handle(event: str, payload: dict, agent: str = AGENT) -> dict:
    session_id = str(payload.get("session_id") or "") or f"hook_{agent}"

    if event == "SessionStart":
        # One-shot session injection (mirrors the Antigravity SessionStart
        # hook): persistent directives instead of per-turn re-injection.
        # No matcher on `source` — inject for startup/resume/clear/compact.
        # Well under the 10k-char additionalContext cap.
        try:
            from src.version_checker import VersionChecker
            VersionChecker.notify_os_update_background()
        except Exception:
            pass
        extra = TOKENSAFE_DIRECTIVES
        try:
            from src.metrics import MetricsTracker
            from src.license_manager import LicenseManager
            quota = LicenseManager.check_quota(MetricsTracker())
            if quota.get("warning"):
                extra = f"{extra}\n\n{quota['warning']}"
        except Exception:
            pass
        try:
            from src.mistakes import MistakesLedger
            guard = MistakesLedger.get_guard_prompt(project_root=str(payload.get("cwd") or os.getcwd()))
            if guard:
                extra = f"{extra}\n\n{guard}"
        except Exception:
            pass
        # Cold-start brief (project-gated): one-shot God Nodes topology banner,
        # parity with the Antigravity SessionStart hook. Non-project cwd
        # (plain chats, plugin/system dirs) stays banner-free.
        try:
            from src.paths import is_project_root
            start_cwd = str(payload.get("cwd") or os.getcwd())
            if is_project_root(start_cwd):
                from src.graph_engine import GraphEngine
                banner = GraphEngine.get_instance(start_cwd).format_topology_banner()
                if banner:
                    extra = (
                        f"{extra}\n\n{banner}\n"
                        "⚡ Core God Nodes and dependency hubs mapped above. "
                        "Call 'tokensafe:repo_map(view=\"graph\", project_path=...)' for full dependency trees."
                    )
        except Exception:
            pass
        # Routing brief: which stack is worked through OUMV vs TokenSafe tools.
        # Hosts that report several roots get all of them classified, not just cwd.
        try:
            from src.paths import is_project_root as _is_project_root
            from src.session_brief import (
                build_workspace_brief,
                workspace_roots_from_env,
                workspace_roots_from_payload,
            )
            cwd_for_brief = str(payload.get("cwd") or os.getcwd())
            if _is_project_root(cwd_for_brief):
                routing = build_workspace_brief(
                    cwd_for_brief,
                    include_live=True,
                    extra_workspaces=workspace_roots_from_payload(payload) + workspace_roots_from_env(),
                )
                if routing:
                    extra = f"{extra}\n\n{routing}"
        except Exception:
            pass
        try:
            from src.burn_monitor import get_burn_warnings
            burn_warn = get_burn_warnings(
                agent=agent,
                session_id=session_id,
                transcript_path=str(payload.get("transcript_path") or ""),
            )
            if burn_warn:
                extra = f"{extra}\n\n{burn_warn}"
        except Exception:
            pass
        try:
            from src.adapter_core import format_cumulative_banner
            cum_banner = format_cumulative_banner()
            if cum_banner:
                extra = f"{extra}\n\n{cum_banner}"
        except Exception:
            pass
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": extra,
            }
        }

    if event == "UserPromptSubmit":
        prompt = str(payload.get("prompt") or "")
        parts = []
        quota = {"is_quota_exceeded": False, "warning": None}
        tracker = None
        try:
            from src.metrics import MetricsTracker
            from src.license_manager import LicenseManager
            tracker = MetricsTracker()
            quota = LicenseManager.check_quota(tracker)
        except Exception:
            pass

        if quota.get("is_quota_exceeded"):
            try:
                from src.license_manager import LicenseManager
                if LicenseManager.should_emit_quota_banner(tracker, session_id, every_n_turns=5) and quota.get("warning"):
                    parts.append(quota["warning"])
            except Exception:
                pass
            if parts:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": "\n\n".join(parts),
                    }
                }
            return {}

        context, meta = compact_for_injection(prompt, agent, session_id, transcript_path=str(payload.get("transcript_path") or ""))
        if context and meta.get("compacted"):
            parts.append(
                f"{context}\n"
                f"(TokenSafe: compacted ~{meta['saved_tokens']} prompt tokens, "
                f"-{meta.get('reduction_pct', 0)}% — original intent preserved.)"
            )

        transcript_path = str(payload.get("transcript_path") or "")
        # Routing brief is a session-level injection (SessionStart + MCP
        # instructions). The per-turn copy that used to live here wrote to an
        # undefined `extra` and was swallowed by its own except: reviving it
        # would re-send ~1 KB on every prompt, so it stays out.
        try:
            from src.burn_monitor import get_burn_warnings
            burn_warn = get_burn_warnings(
                agent=agent,
                session_id=session_id,
                transcript_path=transcript_path,
            )
            if burn_warn:
                parts.append(burn_warn)
        except Exception:
            pass
        if transcript_path:
            try:
                from src.agent import context as kernel
                from src.metrics import MetricsTracker
                turn = 0
                with open(transcript_path, "r", encoding="utf-8", errors="replace") as tf:
                    turn = sum(1 for ln in tf if ln.strip())
                if turn >= 3:
                    folded, orig_hist, folded_tok, stats = kernel.fold_history(
                        transcript_path, turn
                    )
                    if folded and stats.get("saved_tokens", 0) > 0:
                        if tracker is None:
                            tracker = MetricsTracker()
                        prev_event = tracker.get_last_event(session_id, "HISTORY_FOLD")
                        prev_orig = 0
                        if prev_event and isinstance(prev_event.get("details"), dict):
                            prev_orig = prev_event["details"].get("total_accum_orig", 0) or prev_event.get("original_tokens", 0)
                        delta_orig = max(0, orig_hist - prev_orig)
                        if prev_event is None or delta_orig >= 1500:
                            inc_orig = orig_hist if prev_event is None else delta_orig
                            inc_saved = max(0, inc_orig - folded_tok)
                            tracker.record_event(
                                session_id=session_id,
                                event_type="HISTORY_FOLD",
                                original_tokens=inc_orig,
                                optimized_tokens=folded_tok,
                                details={"turns": turn, "total_accum_orig": orig_hist, "agent": agent},
                            )
                            parts.append(
                                f"{folded}\n"
                                f"(TokenSafe History Compactor: stabilized {orig_hist:,} tokens "
                                f"of previous turns — saved {stats['saved_tokens']:,}. "
                                f"Estimated per METRICS.md.)"
                            )
            except Exception:
                pass

        if parts:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "\n\n".join(parts),
                }
            }
        return {}

    if event == "PreToolUse":
        return evaluate_cc_pre_tool_use(payload, agent=agent)

    if event == "PostToolUse":
        # Auto-mute noisy stdout of successful shell commands (tests/linters/builds):
        # feed the model a compact summary instead of letting it parse the full dump.
        # Fail-open: {} leaves the raw output untouched.
        tool = str(payload.get("tool_name") or "")
        compacted, orig, new = None, 0, 0
        try:
            from src.command_sanitizer import CommandSanitizer
            res = CommandSanitizer.compact_tool_output(tool, payload.get("tool_response"))
            if res:
                compacted, orig, new = res
        except Exception:
            compacted = None
        if compacted:
            try:
                from src.metrics import MetricsTracker
                from src.tokenizer import count_tokens
                MetricsTracker().record_event(
                    session_id=session_id,
                    event_type="TOOL_OUTPUT_COMPACT",
                    original_tokens=count_tokens(str(payload.get("tool_response") or "")),
                    optimized_tokens=count_tokens(compacted),
                    details={"agent": agent, "tool": tool, "chars": f"{orig}->{new}"},
                )
            except Exception:
                pass
            return {
                "decision": "block",
                "reason": (
                    f"[TokenSafe] {tool} output auto-compacted "
                    f"({orig:,} → {new:,} chars). Output summary:\n{compacted}"
                ),
            }
        # File-state tracking for the stale-edit guard: snapshot the on-disk
        # hash of every file the model has just seen (Read) or rewritten
        # (Write/Edit) so PreToolUse can detect external changes later.
        try:
            if tool in ("Read", "view_file", "read_file", "Write", "write_file",
                        "Edit", "MultiEdit", "edit_file", "replace_file_content"):
                ti = payload.get("tool_input")
                if isinstance(ti, dict):
                    track_fp = str(ti.get("file_path") or ti.get("Path") or ti.get("path") or ti.get("FilePath") or "")
                    if track_fp:
                        from src.file_state import FileState
                        FileState.record_seen(track_fp)
        except Exception:
            pass
        return {}

    if event == "Stop":
        # Fold-at-rest (opt-in TOKENSAFE_FOLD_AT_REST=1): when the session goes
        # idle, prune superseded intermediate read results from the transcript
        # IN PLACE (structure/UUID-preserving tombstones + one-file backup), so
        # the next resume starts light without any proxy involvement.
        if os.environ.get("TOKENSAFE_FOLD_AT_REST", "").strip().lower() in ("1", "true", "yes"):
            try:
                rest_tp = str(payload.get("transcript_path") or "")
                if rest_tp:
                    from src.superseded import SupersededPruner
                    res = SupersededPruner.fold_at_rest(rest_tp)
                    if res.get("success") and res.get("pruned_count", 0) > 0:
                        from src.metrics import MetricsTracker
                        MetricsTracker().record_event(
                            session_id=session_id,
                            event_type="FOLD_AT_REST",
                            original_tokens=res.get("orig_tokens", 0),
                            optimized_tokens=res.get("pruned_tokens", 0),
                            details={
                                "agent": agent,
                                "pruned": res.get("pruned_count", 0),
                                "transcript": os.path.basename(rest_tp),
                            },
                            measurement="measured",
                        )
            except Exception:
                pass
        return {}

    return {}


def main():
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        print(json.dumps({}))
        return
    event = ""
    try:
        event = str(payload.get("hook_event_name") or "")
        response = handle(event, payload)
        if not isinstance(response, dict):
            response = {}
    except Exception:
        response = {}
    if event in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"):
        try:
            from src.telemetry_sync import TelemetrySync
            TelemetrySync.sync_background(throttle_seconds=0.0 if event == "Stop" else 20.0)
        except Exception:
            pass
    print(json.dumps(response))


if __name__ == "__main__":
    main()
