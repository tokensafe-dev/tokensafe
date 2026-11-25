#!/usr/bin/env python3
"""
Antigravity Lifecycle Hook: PreToolUse
1. Loop Guard: Detects identical repeated tool calls and triggers force_ask
2. Output Guard: Blocks write_file on existing large files, enforcing surgical edit_file
3. AST Skeletonizer & Line Slicer: Protects read_file operations
4. Command Sanitizer: Cleans up git diff, git log, pytest, find, grep
5. Records all savings in SQLite
"""

import sys
import os
import json

HOOK_DIR = os.path.dirname(os.path.realpath(__file__))
PROJECT_ROOT = os.path.dirname(HOOK_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.loop_detector import LoopDetector
from src.metrics import MetricsTracker
from src.tokenizer import count_tokens
from src.license_manager import LicenseManager
from src.adapter_core import guard_write


def _trigger_bg_sync():
    try:
        from src.telemetry_sync import TelemetrySync
        TelemetrySync.sync_background(throttle_seconds=20.0)
    except Exception:
        pass


def _resolve_hook_path(raw_path: str, hook_cwd: str) -> str:
    if not raw_path:
        return ""
    if os.path.isabs(raw_path):
        return raw_path
    return os.path.abspath(os.path.join(hook_cwd, raw_path))


def _handle(payload: dict) -> dict:
    # If invoked by ZCode / Claude Code (tool_name + tool_input without Antigravity toolCall),
    # route through the CC/ZCode PreToolUse evaluator so we return {} / hookSpecificOutput
    # instead of {"decision": "allow"} (which causes hook.run.failed in ZCode).
    if not isinstance(payload.get("toolCall"), dict) and payload.get("tool_name"):
        from src.adapter_core import evaluate_cc_pre_tool_use
        return evaluate_cc_pre_tool_use(payload, agent=str(payload.get("agent") or "zcode"))

    tool_call = payload.get("toolCall")
    if not isinstance(tool_call, dict):
        tool_call = {}
    tool_name = str(tool_call.get("name") or "")
    args = tool_call.get("args")
    if not isinstance(args, dict):
        args = {}
    from src.adapter_core import canonical_tool_name
    canon_tool = canonical_tool_name(tool_name)
    conversation_id = str(payload.get("conversationId") or "default_session")
    last_user_input = str(payload.get("lastUserInput") or "")
    # Working directory of the tool call: lets CommandSanitizer peek at
    # package.json and Guard audit git staging in the user's repo (never plugin CWD).
    workspace_paths = [str(w) for w in payload["workspacePaths"] if str(w).strip()] if isinstance(payload.get("workspacePaths"), list) else []
    explicit_cwd = str(payload.get("cwd") or "").strip()
    hook_cwd = explicit_cwd or (workspace_paths[0] if workspace_paths else "") or os.getcwd()
    try:
        from src.paths import save_active_workspace
        if workspace_paths:
            save_active_workspace(workspace_paths[0])
        elif explicit_cwd and any(
            os.path.exists(os.path.join(explicit_cwd, m))
            for m in (".git", "package.json", "pyproject.toml", "go.mod", "Cargo.toml", "requirements.txt")
        ):
            save_active_workspace(explicit_cwd)
    except Exception:
        pass

    tracker = MetricsTracker()
    quota = LicenseManager.check_quota(tracker)
    is_quota_exceeded = bool(quota.get("is_quota_exceeded", False))

    # 1. Loop Detector Guard (Applies to all tools)
    is_loop, repeat_count, loop_msg = LoopDetector.check_loop(conversation_id, tool_name, args)
    if is_loop:
        try:
            from src.mistakes import MistakesLedger
            MistakesLedger.record_from_loop(tool_name, args, repeat_count, project_root=hook_cwd)
        except Exception:
            pass
        tracker.record_event(
            session_id=conversation_id,
            event_type="LOOP_GUARD",
            original_tokens=5000,
            optimized_tokens=100,
            details={"tool": tool_name, "repeats": repeat_count}
        )
        _trigger_bg_sync()
        return {"decision": "force_ask", "reason": loop_msg}

    response: dict = {"decision": "allow"}

    # 1.5 Stale-file edit guard: when the target changed on disk since the
    #     model last saw it (build output, linters, another session), the edit
    #     either corrupts content or fails and forces a costly full re-read.
    #     Surface it BEFORE the edit lands — force_ask, never deny.
    #     Kill switch: TOKENSAFE_STALE_GUARD=0.
    if canon_tool == "edit":
        try:
            from src.file_state import FileState
            if FileState.guard_enabled():
                raw_path = str(args.get("Path") or args.get("TargetFile") or args.get("path") or args.get("file_path") or "")
                stale = FileState.check_stale(_resolve_hook_path(raw_path, hook_cwd)) if raw_path else None
                if stale:
                    tracker.record_event(
                        session_id=conversation_id,
                        event_type="STALE_EDIT_ASK",
                        original_tokens=FileState.estimate_reread_tokens(_resolve_hook_path(raw_path, hook_cwd)),
                        optimized_tokens=0,
                        details={
                            "tool": tool_name,
                            "target_file": os.path.basename(raw_path),
                            "age_s": stale.get("age_s", 0),
                        },
                        measurement="estimated",
                    )
                    _trigger_bg_sync()
                    return {
                        "decision": "force_ask",
                        "reason": (
                            f"[TokenSafe] {os.path.basename(raw_path)} changed on disk since it was last read "
                            f"(external edit: build/linter/another session?). Your edit may target outdated content. "
                            f"Verify the current file state (read_file the target region) before re-applying the edit."
                        ),
                    }
        except Exception:
            pass

    # 2. TokenSafe Guard: Inspect file writes and edits for security violations
    if canon_tool in ("write", "edit"):
        from src.guard import TokenSafeGuard
        from src.adapter_core import extract_write_content
        raw_path = str(args.get("Path") or args.get("TargetFile") or args.get("path") or args.get("file_path") or "")
        path = _resolve_hook_path(raw_path, hook_cwd)
        code_to_check = (
            (args.get("Content") or args.get("content") or "")
            if canon_tool == "write"
            else (extract_write_content(tool_name, args) or "")
        )
        guard_res = TokenSafeGuard.inspect_file_write(path, code_to_check)
        if guard_res.blocked:
            tracker.record_event(
                session_id=conversation_id,
                event_type=f"GUARD_{guard_res.check_name}",
                original_tokens=count_tokens(code_to_check),
                optimized_tokens=0,
                details={
                    "tool": tool_name,
                    "target_file": os.path.basename(path) if path else "unknown",
                    "reason": guard_res.reason
                }
            )
            _trigger_bg_sync()
            return {"decision": "deny", "reason": f"🛡️ TokenSafe Guard: {guard_res.reason}"}

        # Anti-Stuck hybrid escalation check
        try:
            from src.mistakes import MistakesLedger
            is_blocked, m_reason = MistakesLedger.should_force_ask(path or raw_path, action_type="edit_file", project_root=hook_cwd)
            if is_blocked:
                return {"decision": "force_ask", "reason": f"[TokenSafe Anti-Stuck Guard] {m_reason}"}
            elif m_reason:
                response["reason"] = m_reason
        except Exception:
            pass

    # 3. Output Guard (shared guard_write, v3.0.0): deny wholesale overwrite of
    #    large existing files to enforce edit_file; ask on small-delta writes.
    if canon_tool == "write":
        raw_path = str(args.get("Path") or args.get("path") or args.get("file_path") or "")
        path = _resolve_hook_path(raw_path, hook_cwd)
        content = args.get("Content") or args.get("content") or ""
        is_append = args.get("Append", args.get("append", False))
        verdict = guard_write(path, str(content or ""), bool(is_append), "antigravity", conversation_id)
        if verdict["decision"] in ("deny", "ask"):
            _trigger_bg_sync()
            return {"decision": verdict["decision"], "reason": verdict["reason"]}

    # 3. read_file Optimization (AST Windowing & Skeletonization).
    #    'view_file' and 'Read' route through the same optimization pipeline.
    elif canon_tool == "read":
        from src.slicer import ToolSlicer
        from src.skeletonizer import Skeletonizer
        raw_path = str(args.get("Path") or args.get("path") or args.get("FilePath") or args.get("file_path") or "")
        path = _resolve_hook_path(raw_path, hook_cwd)
        start_line = args.get("StartLine")
        if start_line is None:
            start_line = args.get("startLine")
        end_line = args.get("EndLine")
        if end_line is None:
            end_line = args.get("endLine")
        # The overwrite merge is shallow top-level key replacement — we must
        # rewrite the SAME keys the agent actually sent, or a "path"/"FilePath"
        # read would get the optimized content under a brand-new "Path" key
        # while its original argument still points at the full file.
        path_key = next((k for k in ("Path", "path", "FilePath") if k in args), "Path")
        start_key = next((k for k in ("StartLine", "startLine") if k in args), "StartLine")
        end_key = next((k for k in ("EndLine", "endLine") if k in args), "EndLine")

        if is_quota_exceeded:
            # Free tier quota reached: pass through raw read_file without AST skeletonization
            if LicenseManager.should_emit_quota_banner(tracker, conversation_id, every_n_turns=5) and quota.get("warning"):
                response["reason"] = quota["warning"]
            return response

        # Step 3-Image: Downscale oversized vision images (>1024px)
        if path and os.path.exists(path) and path.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            try:
                from src.image_downscale import ImageDownscaler
                opt_res = ImageDownscaler.optimize(path, max_edge=1024)
                if opt_res.get("success") and opt_res.get("was_downscaled"):
                    response["overwrite"] = {path_key: opt_res["optimized_path"]}
                    response["reason"] = f"Token Optimizer: Downscaled image to {opt_res['new_dimensions']} (Saved ~{opt_res['saved_tokens']} vision tokens)"
                    tracker.record_event(
                        session_id=conversation_id,
                        event_type="IMAGE_DOWNSCALE",
                        original_tokens=opt_res.get("saved_tokens", 0) + 300,
                        optimized_tokens=300,
                        details={
                            "target_file": os.path.basename(path),
                            "orig_dim": opt_res.get("orig_dimensions"),
                            "new_dim": opt_res.get("new_dimensions"),
                            "saved_tokens": opt_res.get("saved_tokens")
                        }
                    )
                    return response
            except Exception:
                pass

        # Step 3-Lockfile: Intercept raw lockfile reads and redirect to generated summary
        if path and os.path.exists(path):
            try:
                from src.format_parsers.lockfiles import LockfileReducer
                if LockfileReducer.is_lockfile(path):
                    if start_line is None or end_line is None or (int(end_line) - int(start_line) > 50):
                        summary_path = LockfileReducer.generate_lockfile_summary(path)
                        if summary_path and os.path.exists(summary_path):
                            orig_size = os.path.getsize(path)
                            orig_t = max(orig_size // 4, 1000)
                            with open(summary_path, "r", encoding="utf-8", errors="ignore") as sf:
                                sum_t = count_tokens(sf.read())
                            saved = max(orig_t - sum_t, 500)
                            response["overwrite"] = {path_key: summary_path}
                            response["reason"] = (
                                f"Token Optimizer: Sliced lockfile to summary view (Saved ~{saved} tokens). "
                                f"Use query_dependency(lockfile_path, package_name) to look up specific packages."
                            )
                            tracker.record_event(
                                session_id=conversation_id,
                                event_type="LOCKFILE_SUMMARY",
                                original_tokens=orig_t,
                                optimized_tokens=sum_t,
                                details={"target_file": os.path.basename(path), "saved_tokens": saved}
                            )
                            return response
            except Exception:
                pass

        # Step 3A: If model explicitly specified line range, respect it
        if start_line is not None and end_line is not None:
            return response

        # Step 3B: If a specific symbol/function is targeted in user prompt, check graph-guided skeleton first
        slicer_args = dict(args)
        if path:
            slicer_args[path_key] = path
        overwrite_args, metrics = ToolSlicer.optimize_read_file(slicer_args, context_hint=last_user_input)
        target_sym = metrics.get("target_symbol")
        if target_sym and path and os.path.exists(path):
            skel_path, orig_t, skel_t = Skeletonizer.get_skeleton_file(path, target_symbol=target_sym)
            if skel_path and orig_t > skel_t:
                saved = orig_t - skel_t
                response["overwrite"] = {path_key: skel_path}
                response["reason"] = f"Token Optimizer: Graph-guided AST view for '{target_sym}' (Saved ~{saved} tokens)"
                tracker.record_event(
                    session_id=conversation_id,
                    event_type="GRAPH_SKELETON",
                    original_tokens=orig_t,
                    optimized_tokens=skel_t,
                    details={
                        "target_file": os.path.basename(path),
                        "symbol": target_sym,
                        "saved_tokens": saved
                    }
                )
                return response

        if overwrite_args and metrics.get("saved_tokens", 0) > 0:
            response["overwrite"] = {
                start_key: overwrite_args["StartLine"],
                end_key: overwrite_args["EndLine"],
            }
            response["reason"] = (
                f"Token Optimizer: Sliced read window to lines "
                f"{overwrite_args['StartLine']}-{overwrite_args['EndLine']} (Saved ~{metrics['saved_tokens']} tokens)"
            )

            tracker.record_event(
                session_id=conversation_id,
                event_type="TOOL_READ",
                original_tokens=metrics["original_tokens"],
                optimized_tokens=metrics["optimized_tokens"],
                details={
                    "target_file": os.path.basename(path),
                    "lines_total": metrics.get("lines_total", 0),
                    "lines_sliced": metrics.get("lines_sliced", 0)
                }
            )
            return response

        # Step 3C: If entire file requested with no symbol target, provide AST Skeleton (mtime-cached)
        if path and os.path.exists(path):
            skeleton_path, orig_t, skel_t = Skeletonizer.get_skeleton_file(path)
            if skeleton_path and orig_t > skel_t:
                saved = orig_t - skel_t
                response["overwrite"] = {path_key: skeleton_path}
                response["reason"] = (
                    f"Token Optimizer: Provided AST skeleton view (Saved ~{saved} tokens). "
                    f"Call read_file with StartLine/EndLine to inspect specific method bodies."
                )

                tracker.record_event(
                    session_id=conversation_id,
                    event_type="AST_SKELETON",
                    original_tokens=orig_t,
                    optimized_tokens=skel_t,
                    details={
                        "target_file": os.path.basename(path),
                        "saved_tokens": saved
                    }
                )
                return response

    # 4. run_command Optimization & TokenSafe Guard
    elif canon_tool == "bash":
        from src.guard import TokenSafeGuard
        from src.diff_slicer import DiffSlicer
        from src.command_sanitizer import CommandSanitizer
        cmd = str(args.get("CommandLine") or args.get("command") or "")
        guard_res = TokenSafeGuard.inspect_command(cmd, cwd=hook_cwd)
        if guard_res.blocked:
            tracker.record_event(
                session_id=conversation_id,
                event_type=f"GUARD_{guard_res.check_name}",
                original_tokens=count_tokens(cmd),
                optimized_tokens=0,
                details={"cmd": cmd[:100], "reason": guard_res.reason}
            )
            _trigger_bg_sync()
            return {"decision": "deny", "reason": f"🛡️ TokenSafe Guard: {guard_res.reason}"}
        elif guard_res.warning_only:
            response["reason"] = f"⚠️ TokenSafe Guard Warning: {guard_res.reason}"

        # Anti-Stuck hybrid escalation check
        try:
            from src.mistakes import MistakesLedger
            is_blocked, m_reason = MistakesLedger.should_force_ask(cmd, action_type="command", project_root=hook_cwd)
            if is_blocked:
                return {"decision": "force_ask", "reason": f"[TokenSafe Anti-Stuck Guard] {m_reason}"}
            elif m_reason:
                response["reason"] = m_reason
        except Exception:
            pass

        if is_quota_exceeded:
            # Free tier quota reached: skip Pro diff/command optimization while Security Guards remain active
            if LicenseManager.should_emit_quota_banner(tracker, conversation_id, every_n_turns=5) and quota.get("warning"):
                response["reason"] = response.get("reason") or quota["warning"]
            return response

        # Check AST Diff Slicer first (per-stage rewrite of git diff stages),
        # then run the Command Sanitizer on the result so other stages
        # (git log, pytest, rg, ...) in the same compound command still
        # get their rules applied.
        diff_cmd, diff_saved, diff_reason = DiffSlicer.optimize_git_diff_command(cmd)
        if diff_cmd:
            cmd = diff_cmd
        new_cmd, san_saved, san_reason = CommandSanitizer.sanitize(cmd, cwd=hook_cwd)
        if new_cmd:
            est_saved = san_saved + (diff_saved if diff_cmd else 0)
            reason = (f"{diff_reason}; {san_reason}" if diff_cmd else san_reason)
        elif diff_cmd:
            new_cmd, est_saved, reason = diff_cmd, diff_saved, diff_reason

        if new_cmd and est_saved > 0:
            cmd_key = "command" if "command" in args and "CommandLine" not in args else "CommandLine"
            response["overwrite"] = {cmd_key: new_cmd}
            response["reason"] = f"Token Optimizer: {reason} (Saved ~{est_saved} tokens)"

            tracker.record_event(
                session_id=conversation_id,
                event_type="COMMAND_FILTER",
                original_tokens=est_saved + 50,
                optimized_tokens=50,
                details={
                    "original_cmd": cmd[:50],
                    "sanitized_cmd": new_cmd[:50],
                    "reason": reason
                }
            )

    return response


def main():
    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            print(json.dumps({"decision": "allow"}))
            return
        payload = json.loads(raw_input)
        if not isinstance(payload, dict):
            print(json.dumps({"decision": "allow"}))
            return
        response = _handle(payload)
        if not isinstance(response, dict):
            response = {"decision": "allow"}
    except Exception:
        # Hooks must always fail open so an internal error never blocks the IDE/agent.
        response = {"decision": "allow"}

    print(json.dumps(response))

if __name__ == "__main__":
    main()
