#!/usr/bin/env python3
"""
TokenSafe adapter for Kimi Code lifecycle hooks.

Registered via kimi.plugin.json or ~/.kimi-code/config.toml:
  SessionStart -> injects TokenSafe protocol directives once per session.
  UserPromptSubmit -> compacts prompt into TOCP intent expression.
  PreToolUse (Bash/Write/Read) -> denies secret leaks, destructive commands,
                                  and unindexed lockfile/bundle reads.

Contract: stdin = one JSON payload, stdout = one JSON response.
Fail-open: any internal error emits `{}`.
"""

import sys
import json
import os

HOOK_DIR = os.path.dirname(os.path.realpath(__file__))
PROJECT_ROOT = os.path.dirname(HOOK_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.adapter_core import (
    TOKENSAFE_DIRECTIVES,
    compact_for_injection,
    evaluate_cc_pre_tool_use,
)

AGENT = "kimi_code"


def handle(event: str, payload: dict) -> dict:
    session_id = str(payload.get("session_id") or "") or f"hook_{AGENT}"

    if event == "SessionStart":
        extra = TOKENSAFE_DIRECTIVES
        try:
            from src.version_checker import VersionChecker
            VersionChecker.notify_os_update_background()
        except Exception:
            pass
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
        try:
            from src.burn_monitor import get_burn_warnings
            burn_warn = get_burn_warnings(
                agent=AGENT,
                session_id=session_id,
                transcript_path=str(payload.get("transcript_path") or ""),
            )
            if burn_warn:
                extra = f"{extra}\n\n{burn_warn}"
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
        try:
            from src.burn_monitor import get_burn_warnings
            burn_warn = get_burn_warnings(
                agent=AGENT,
                session_id=session_id,
                transcript_path=str(payload.get("transcript_path") or ""),
            )
            if burn_warn:
                parts.append(burn_warn)
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
        return evaluate_cc_pre_tool_use(payload, agent=AGENT)

    if event == "PostToolUse":
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
