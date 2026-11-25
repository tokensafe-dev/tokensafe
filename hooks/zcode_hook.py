#!/usr/bin/env python3
"""
TokenSafe adapter for ZCode (Z.ai Code) lifecycle hooks (~/.zcode/cli/config.json).

Speaks the ZCode / Claude Code JSON hook contract:
  SessionStart     -> injects TokenSafe directives, God Nodes banner, and burn alerts.
  UserPromptSubmit -> compacts prompt (TOCP) + emits live burn warnings from model-io-*.jsonl.
  PreToolUse       -> LoopDetector, MistakesLedger, Bash/Write/Edit/Read guards, stale-edit check.
  PostToolUse      -> compacts verbose shell output, records FileState + command failures.
  Stop             -> flushes telemetry and runs optional fold-at-rest.

Contract: stdin = one JSON payload, stdout = one JSON response ({} on clean allow).
"""

import json
import os
import sys

HOOK_DIR = os.path.dirname(os.path.realpath(__file__))
PROJECT_ROOT = os.path.dirname(HOOK_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from hooks.claude_code_hook import handle as _cc_handle  # noqa: E402

AGENT = "zcode"


def handle(event: str, payload: dict) -> dict:
    # Normalize ZCode-specific camelCase aliases onto CC fields while preserving existing keys.
    norm = dict(payload)
    if not norm.get("session_id") and norm.get("conversationId"):
        norm["session_id"] = norm["conversationId"]
    if not norm.get("prompt") and norm.get("lastUserInput"):
        norm["prompt"] = norm["lastUserInput"]
    if not norm.get("transcript_path") and norm.get("transcriptPath"):
        norm["transcript_path"] = norm["transcriptPath"]
    if not norm.get("tool_name") and isinstance(norm.get("toolCall"), dict):
        norm["tool_name"] = norm["toolCall"].get("name") or ""
        if not norm.get("tool_input") and isinstance(norm["toolCall"].get("args"), dict):
            norm["tool_input"] = norm["toolCall"]["args"]
    return _cc_handle(event, norm, agent=AGENT)


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
        event = str(payload.get("hook_event_name") or payload.get("hookEventName") or "")
        if not event:
            if "tool_name" in payload or "toolCall" in payload:
                event = "PostToolUse" if ("tool_response" in payload or "toolResponse" in payload) else "PreToolUse"
            elif "prompt" in payload or "lastUserInput" in payload:
                event = "UserPromptSubmit"
            elif "terminationReason" in payload or "fullyIdle" in payload:
                event = "Stop"
            else:
                event = "SessionStart"
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
