#!/usr/bin/env python3
"""
TokenSafe adapter for Cursor agent hooks (~/.cursor/hooks.json).

Honest capability matrix (per official Cursor hooks docs):
  beforeShellExecution -> allow/deny/ask: blocks secret-bearing,
                          destructive and TLS-bypassing commands.
  beforeReadFile       -> allow/deny: blocks lockfile/bundle/dist dumps
                          that would burn ~50k context tokens.
  beforeSubmitPrompt   -> allow/deny ONLY (no injection in Cursor yet):
                          records nothing, always allows. Kept as a no-op
                          so the hook wiring is future-proof.
  beforeEditFile       -> allow/deny: secret/TLS/SQL gate on edit payloads,
                          wired opportunistically (inert where the host
                          never fires this event).
  stop / afterFileEdit -> observational no-ops.

IMPORTANT: Cursor BLOCKS the action when a hook prints invalid JSON.
Fail-open here means printing a valid per-event ALLOW response from the
exception handler, never an empty string.
"""

import sys
import json
import os

HOOK_DIR = os.path.dirname(os.path.realpath(__file__))
PROJECT_ROOT = os.path.dirname(HOOK_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.adapter_core import extract_write_content, guard_command, guard_file_content, guard_read

AGENT = "cursor"


def _allow(event: str) -> dict:
    if event == "beforeSubmitPrompt":
        return {"continue": True}
    return {"permission": "allow"}


def handle(event: str, payload: dict) -> dict:
    session_id = str(payload.get("conversation_id") or payload.get("session_id") or "") or f"hook_{AGENT}"
    cwd = str(payload.get("cwd") or "")

    # Loop guard across Cursor pre-tool hooks
    if event in ("beforeShellExecution", "beforeReadFile", "beforeEditFile"):
        try:
            from src.loop_detector import LoopDetector
            is_loop, _cnt, loop_msg = LoopDetector.check_loop(session_id, event, payload)
            if is_loop:
                if event == "beforeShellExecution":
                    return {"permission": "ask", "agent_message": loop_msg}
                return {"permission": "deny", "user_message": loop_msg}
        except Exception:
            pass

    if event == "beforeShellExecution":
        command = str(payload.get("command") or "")
        verdict = guard_command(command, AGENT, session_id, cwd=cwd, transcript_path=str(payload.get("transcript_path") or ""))
        if verdict["decision"] == "deny":
            return {"permission": "deny", "user_message": verdict["reason"]}
        if verdict["decision"] == "ask":
            return {"permission": "ask", "agent_message": verdict["reason"]}
        try:
            from src.mistakes import MistakesLedger
            is_blocked, m_reason = MistakesLedger.should_force_ask(command, action_type="command", project_root=cwd or os.getcwd())
            if is_blocked:
                return {"permission": "ask", "agent_message": f"[TokenSafe Anti-Stuck Guard] {m_reason}"}
        except Exception:
            pass
        return {"permission": "allow"}

    if event == "beforeReadFile":
        file_path = str(payload.get("file_path") or "")
        verdict = guard_read(file_path, AGENT, session_id, transcript_path=str(payload.get("transcript_path") or ""))
        if verdict["decision"] == "deny":
            return {"permission": "deny", "user_message": verdict["reason"]}
        return {"permission": "allow"}

    if event == "beforeEditFile":
        # Opportunistic security gate on edit payloads (hardcoded secrets /
        # TLS bypass / SQLi / CORS — parity with the Antigravity path).
        # Unknown/absent content fields fail open to allow.
        edit_path = str(payload.get("file_path") or payload.get("path") or "")
        tool_hint = "MultiEdit" if isinstance(payload.get("edits"), list) else "Edit"
        edit_content = extract_write_content(tool_hint, payload)
        if edit_content:
            verdict = guard_file_content(edit_path, edit_content, AGENT, session_id, transcript_path=str(payload.get("transcript_path") or ""))
            if verdict["decision"] == "deny":
                return {"permission": "deny", "user_message": verdict["reason"]}
        try:
            from src.mistakes import MistakesLedger
            is_blocked, m_reason = MistakesLedger.should_force_ask(edit_path, action_type="edit_file", project_root=cwd or os.getcwd())
            if is_blocked:
                return {"permission": "deny", "user_message": f"[TokenSafe Anti-Stuck Guard] {m_reason}"}
        except Exception:
            pass
        return {"permission": "allow"}

    if event == "beforeSubmitPrompt":
        # Cursor cannot inject context (allow/deny only). Nothing honest to
        # record: the model still sees the full prompt. Always allow.
        return {"continue": True}

    return _allow(event)


def main():
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        print(json.dumps(_allow("")))
        return
    event = ""
    try:
        event = str(payload.get("hook_event_name") or "")
        response = handle(event, payload)
    except Exception:
        response = _allow(event)
    if not isinstance(response, dict) or not response:
        response = _allow(event)
    if event in ("beforeShellExecution", "beforeEditFile", "beforeReadFile", "beforeSubmitPrompt", "stop"):
        try:
            from src.telemetry_sync import TelemetrySync
            TelemetrySync.sync_background(throttle_seconds=0.0 if event == "stop" else 20.0)
        except Exception:
            pass
    print(json.dumps(response))


if __name__ == "__main__":
    main()
