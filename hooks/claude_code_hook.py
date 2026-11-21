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

from src.adapter_core import TOKENSAFE_DIRECTIVES, compact_for_injection, extract_write_content, guard_command, guard_file_content, guard_write

AGENT = "claude_code"


def handle(event: str, payload: dict) -> dict:
    session_id = str(payload.get("session_id") or "") or f"hook_{AGENT}"

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
                        "Call 'tokensafe:get_architecture_graph(project_path)' for full dependency trees."
                    )
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

        context, meta = compact_for_injection(prompt, AGENT, session_id, transcript_path=str(payload.get("transcript_path") or ""))
        if context and meta.get("compacted"):
            parts.append(
                f"{context}\n"
                f"(TokenSafe: compacted ~{meta['saved_tokens']} prompt tokens, "
                f"-{meta.get('reduction_pct', 0)}% — original intent preserved.)"
            )

        # History auto-fold (parity with the Antigravity PreInvocation hook):
        # Claude Code ships transcript_path in every hook payload. Once the
        # transcript holds enough turns, past turns are folded into a State
        # Ledger and injected; savings are recorded incrementally to avoid
        # double-counting the same history mass on every submit.
        transcript_path = str(payload.get("transcript_path") or "")
        if transcript_path:
            try:
                from src.history_compactor import HistoryCompactor
                from src.metrics import MetricsTracker
                turn = 0
                with open(transcript_path, "r", encoding="utf-8", errors="replace") as tf:
                    turn = sum(1 for ln in tf if ln.strip())
                if turn >= 3:
                    folded, orig_hist, folded_tok, stats = HistoryCompactor.fold_transcript(
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
                                details={"turns": turn, "total_accum_orig": orig_hist, "agent": AGENT},
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
        tool = str(payload.get("tool_name") or "")
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            tool_input = {}
        if tool in ("Bash", "Shell", "bash"):
            command = str(tool_input.get("command") or "")
            verdict = guard_command(command, AGENT, session_id, cwd=str(payload.get("cwd") or ""), transcript_path=str(payload.get("transcript_path") or ""))
            if verdict["decision"] == "deny":
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": verdict["reason"],
                    }
                }
            if verdict["decision"] == "ask":
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "ask",
                        "permissionDecisionReason": verdict["reason"],
                    }
                }
        elif tool in ("Write", "write_file", "replace_file_content"):
            # Egress economy (v3.0.0): full-file writes over existing files
            # burn expensive output tokens — steer to surgical Edit instead.
            file_path = str(tool_input.get("file_path") or tool_input.get("Path") or tool_input.get("path") or "")
            content = tool_input.get("content") or tool_input.get("Content") or tool_input.get("file_text") or ""
            append = bool(tool_input.get("append") or tool_input.get("Append") or False)
            # Security first (parity with the Antigravity path): hardcoded
            # secrets / TLS bypass / SQLi / CORS never reach disk, before any
            # egress-economy steering.
            sec_verdict = guard_file_content(file_path, str(content or ""), AGENT, session_id, transcript_path=str(payload.get("transcript_path") or ""))
            if sec_verdict["decision"] == "deny":
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": sec_verdict["reason"],
                    }
                }
            verdict = guard_write(file_path, str(content or ""), append, AGENT, session_id, transcript_path=str(payload.get("transcript_path") or ""))
            if verdict["decision"] == "deny":
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": verdict["reason"],
                    }
                }
            if verdict["decision"] == "ask":
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "ask",
                        "permissionDecisionReason": verdict["reason"],
                    }
                }
        elif tool in ("Edit", "MultiEdit", "edit_file", "replace_file_content"):
            # Security first (parity with the Antigravity path): deny hardcoded
            # secrets / TLS bypass / SQLi / CORS in the replacement text before
            # the stale-file check (deny outranks ask).
            sec_file = str(tool_input.get("file_path") or tool_input.get("Path") or tool_input.get("path") or "")
            sec_verdict = guard_file_content(sec_file, extract_write_content(tool, tool_input), AGENT, session_id, transcript_path=str(payload.get("transcript_path") or ""))
            if sec_verdict["decision"] == "deny":
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": sec_verdict["reason"],
                    }
                }
            # Stale-file edit guard: when the target changed on disk since the
            # model last saw it (build output, linters, another session), the
            # edit either corrupts content or fails and forces a costly full
            # re-read. Surface it BEFORE the edit lands — ask, never deny.
            # Kill switch: TOKENSAFE_STALE_GUARD=0.
            try:
                from src.file_state import FileState
                stale_fp = str(tool_input.get("file_path") or tool_input.get("Path") or tool_input.get("path") or "")
                if stale_fp and FileState.guard_enabled():
                    stale = FileState.check_stale(stale_fp)
                    if stale:
                        try:
                            from src.metrics import MetricsTracker
                            MetricsTracker().record_event(
                                session_id=session_id,
                                event_type="STALE_EDIT_ASK",
                                original_tokens=FileState.estimate_reread_tokens(stale_fp),
                                optimized_tokens=0,
                                details={
                                    "agent": AGENT,
                                    "target_file": os.path.basename(stale_fp),
                                    "age_s": stale.get("age_s", 0),
                                },
                                measurement="estimated",
                            )
                        except Exception:
                            pass
                        return {
                            "hookSpecificOutput": {
                                "hookEventName": "PreToolUse",
                                "permissionDecision": "ask",
                                "permissionDecisionReason": (
                                    f"[TokenSafe] {os.path.basename(stale_fp)} changed on disk since it was last read "
                                    f"(external edit: build/linter/another session?). Your edit may target outdated content. "
                                    f"Approve if you have verified the current file state."
                                ),
                            }
                        }
            except Exception:
                pass
        return {}

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
                    details={"agent": AGENT, "tool": tool, "chars": f"{orig}->{new}"},
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
                                "agent": AGENT,
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
