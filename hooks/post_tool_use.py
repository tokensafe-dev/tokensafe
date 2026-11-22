#!/usr/bin/env python3
"""
Antigravity Lifecycle Hook: PostToolUse
Fires after tool execution.
1. Fulfills Antigravity PostToolUse contract by returning `{}`.
2. Triggers detached background telemetry sync if pending events exist.
"""

import os
import sys
import json

import time

HOOK_DIR = os.path.dirname(os.path.realpath(__file__))
PROJECT_ROOT = os.path.dirname(HOOK_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

def _should_skip_fast() -> bool:
    if os.environ.get("TOKENSAFE_TELEMETRY_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return True
    try:
        data_dir = os.environ.get("TOKENSAFE_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".tokensafe", "data")
        state_file = os.path.join(data_dir, "sync_state.json")
        if os.path.isfile(state_file):
            with open(state_file, "r", encoding="utf-8") as f:
                st = json.load(f)
            if isinstance(st, dict) and (time.time() - float(st.get("last_attempt_at", 0) or 0)) < 20.0:
                return True
    except Exception:
        pass
    return False

def main():
    raw = ""
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""

    # Stale-edit guard tracking: snapshot the on-disk hash of files the model
    # has just seen (read) or rewritten (write/edit). Tolerant payload parsing
    # — this one script serves Antigravity/ZCode (toolCall shape) and stays
    # harmless under CC-shaped payloads. Output contract is untouched: {}.
    try:
        payload = json.loads(raw) if raw.strip() else {}
        if isinstance(payload, dict):
            tool_call = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
            tool_name = str(tool_call.get("name") or payload.get("tool_name") or "")
            args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else (
                payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
            )
            if tool_name in ("read_file", "view_file", "Read", "smart_read_file", "write_file",
                             "edit_file", "Write", "Edit", "MultiEdit", "replace_file_content",
                             "NotebookEdit"):
                tracked_path = ""
                for key in ("Path", "TargetFile", "path", "FilePath", "file_path", "notebook_path"):
                    val = args.get(key)
                    if isinstance(val, str) and val.strip():
                        tracked_path = val
                        break
                if tracked_path:
                    from src.file_state import FileState
                    FileState.record_seen(tracked_path)

            # Anti-Stuck Failure Tracking: record command or test failure
            if tool_name in ("run_command", "Bash", "ExecuteCommand", "terminal"):
                tool_res = (
                    payload.get("toolResponse")
                    or payload.get("tool_result")
                    or payload.get("result")
                    or payload.get("response")
                    or {}
                )
                exit_code = None
                output_str = ""
                if isinstance(tool_res, dict):
                    exit_code = tool_res.get("exitCode") if "exitCode" in tool_res else tool_res.get("exit_code")
                    output_str = str(tool_res.get("output") or tool_res.get("error") or tool_res.get("stderr") or "")
                elif isinstance(tool_res, str):
                    output_str = tool_res

                cmd = str(args.get("CommandLine") or args.get("command") or "")
                is_failed = False
                if exit_code is not None and int(exit_code) != 0:
                    is_failed = True
                elif ("FAIL:" in output_str or "FAILED (" in output_str or "Traceback (most recent call last):" in output_str) and len(output_str) > 20:
                    is_failed = True

                if is_failed and cmd:
                    try:
                        from src.mistakes import MistakesLedger
                        hook_cwd = str(payload.get("cwd") or os.getcwd())
                        summary = f"Command failed (exit {exit_code or 'err'}): {cmd[:60]}"
                        MistakesLedger.record_failure(
                            summary=summary,
                            source="command_failure",
                            details=output_str[:300],
                            project_root=hook_cwd
                        )
                    except Exception:
                        pass
    except Exception:
        pass

    # Non-blocking telemetry sync trigger (detached background process)
    if not _should_skip_fast():
        try:
            from src.telemetry_sync import TelemetrySync
            TelemetrySync.sync_background(throttle_seconds=20.0)
        except Exception:
            pass

    # PostToolUse contract expects an empty JSON object {}
    print(json.dumps({}))

if __name__ == "__main__":
    main()
