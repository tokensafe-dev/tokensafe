"""
History Compactor: Summarizes older multi-turn tool interactions in Antigravity transcripts
into a dense Consolidated State Ledger (CSL), eliminating the quadratic context explosion.
"""

import os
import json
import re
from typing import Dict, Any, Optional, List, Tuple
from src.tokenizer import count_tokens


def _block_text(content: Any) -> str:
    """Plain text of a message content (plain string or typed block list)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "")
        if btype in ("text", "thinking", "reasoning"):
            text = block.get("text") or block.get("thinking")
            if isinstance(text, str) and text:
                parts.append(text)
        elif btype == "tool_result":
            nested = _block_text(block.get("content"))
            if nested:
                parts.append(nested)
    return " ".join(parts)


def _model_io_parts(step: dict) -> Tuple[str, List[Tuple[str, dict]], str]:
    """Normalize a ZCode `model_io` record into (text, tool_calls, user_prompt).

    One record is one provider call: `request.messages` carries either only the
    new messages (`messagesKind: "delta"`) or a window ending with the newest
    message, while `response` holds that call's assistant output. Only the newest
    message is new state, so folding stays linear instead of re-counting the
    cumulative conversation once per record. `user_prompt` is non-empty when the
    record sits on a turn boundary (the digest's active goal).
    """
    request = step.get("request") if isinstance(step.get("request"), dict) else {}
    response = step.get("response") if isinstance(step.get("response"), dict) else {}

    messages = request.get("messages") if isinstance(request.get("messages"), list) else []
    newest: dict = messages[-1] if messages and isinstance(messages[-1], dict) else {}
    newest_role = str(newest.get("role") or "")
    newest_text = _block_text(newest.get("content"))

    user_prompt = ""
    if newest_role == "user":
        candidate = newest_text.strip()
        if candidate and not _is_injected_prompt(candidate):
            user_prompt = candidate
    if not user_prompt and request.get("messagesKind") in ("full", "delta"):
        # Complete (or purely new) windows: the newest user message is this
        # turn's prompt even when trailing tool results follow it.
        for item in reversed(messages):
            if not isinstance(item, dict) or item.get("role") != "user":
                continue
            candidate = _block_text(item.get("content")).strip()
            if candidate and not _is_injected_prompt(candidate):
                user_prompt = candidate
                break

    assistant_text = response.get("text")
    if not isinstance(assistant_text, str):
        assistant_text = ""
    reasoning_text = response.get("reasoningText")
    if not isinstance(reasoning_text, str):
        reasoning_text = ""

    parts: List[str] = []
    if newest_role == "user" and newest_text.strip():
        parts.append(newest_text)  # the prompt itself leads the step
    elif user_prompt:
        parts.append(user_prompt)  # prompt recovered from deeper in the window
    if assistant_text.strip():
        parts.append(assistant_text)
    if reasoning_text.strip():
        parts.append(reasoning_text)
    if newest_role != "user" and newest_text.strip():
        parts.append(newest_text)

    calls: List[Tuple[str, dict]] = []
    for call in response.get("toolCalls") or []:
        if not isinstance(call, dict):
            continue
        args = call.get("input")
        calls.append((str(call.get("name") or ""), args if isinstance(args, dict) else {}))

    return "\n".join(part for part in parts if part), calls, user_prompt


def _extract_step_text(step: dict) -> str:
    """Step text in any supported transcript format (Antigravity string/list, Claude Code text/thinking/tool_result)."""
    if not isinstance(step, dict):
        return ""
    if step.get("type") == "model_io":
        return _model_io_parts(step)[0]
    content = step.get("content")
    if isinstance(content, str) and content:
        return content
    if isinstance(content, list):
        return " ".join(
            str(b.get("text") or b.get("thinking") or "")
            for b in content
            if isinstance(b, dict)
        ).strip()
    # ZCode event log: {"type": "...", "payload": {...}}
    payload = step.get("payload")
    if isinstance(payload, dict):
        pcontent = payload.get("content")
        if isinstance(pcontent, str) and pcontent:
            return pcontent
        if isinstance(pcontent, list):
            parts = [
                str(b.get("text") or b.get("thinking") or "")
                for b in pcontent
                if isinstance(b, dict)
            ]
            joined = " ".join(parts).strip()
            if joined:
                return joined
        pinput = payload.get("input")
        if isinstance(pinput, str) and pinput:
            return pinput
        if step.get("type") == "turn_started" and isinstance(pinput, dict):
            text = pinput.get("text") or pinput.get("prompt")
            if isinstance(text, str) and text:
                return text

    # ZCode model-io log: {request:{body:{messages}}, response:{body:{choices}}}
    response = step.get("response")
    if isinstance(response, dict):
        try:
            msg = response["body"]["choices"][0]["message"]
        except Exception:
            msg = None
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, str) and content:
                return content
            if isinstance(content, list):
                joined = " ".join(
                    str(b.get("text") or "")
                    for b in content
                    if isinstance(b, dict)
                ).strip()
                if joined:
                    return joined
    request = step.get("request")
    if isinstance(request, dict):
        try:
            messages = request["body"]["messages"]
        except Exception:
            messages = None
        if isinstance(messages, list):
            for item in reversed(messages):
                if isinstance(item, dict) and item.get("role") == "user":
                    content = item.get("content")
                    if isinstance(content, str) and content:
                        return content

    message = step.get("message")
    if not isinstance(message, dict):
        return ""
    mc = message.get("content")
    if isinstance(mc, str) and mc:
        return mc
    if isinstance(mc, list):
        parts: List[str] = []
        for block in mc:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                t = block.get("text")
                if isinstance(t, str) and t:
                    parts.append(t)
            elif btype == "thinking":
                t = block.get("thinking") or block.get("text")
                if isinstance(t, str) and t:
                    parts.append(t)
            elif btype == "tool_result":
                tc = block.get("content")
                if isinstance(tc, str) and tc:
                    parts.append(tc)
                elif isinstance(tc, list):
                    for sub in tc:
                        if isinstance(sub, dict) and isinstance(sub.get("text"), str):
                            parts.append(sub["text"])
        return " ".join(parts)
    return ""


_READ_TOOLS = {
    "read_file", "view_file", "Read", "smart_read_file",
    "scoped_search", "list_dir", "get_architecture_graph", "get_api_surface",
}
_EDIT_TOOLS = {"edit_file", "write_file", "Edit", "Write", "MultiEdit", "NotebookEdit", "replace_file_content"}
_COMMAND_TOOLS = {"run_command", "Bash", "sanitize_command"}
_PATH_KEYS = ("Path", "path", "FilePath", "file_path", "notebook_path", "project_path")
_COMMAND_KEYS = ("CommandLine", "command")


def _arg_value(args: dict, keys) -> str:
    if not isinstance(args, dict):
        return ""
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _extract_step_tool_calls(step: dict) -> List[Tuple[str, dict]]:
    """Tool calls in any supported transcript format."""
    calls: List[Tuple[str, dict]] = []
    if not isinstance(step, dict):
        return calls
    if step.get("type") == "model_io":
        return _model_io_parts(step)[1]
    tc_list = step.get("tool_calls")
    if isinstance(tc_list, list):
        for tc in tc_list:
            if isinstance(tc, dict):
                args = tc.get("args")
                calls.append((str(tc.get("name", "")), args if isinstance(args, dict) else {}))
    response = step.get("response")
    if isinstance(response, dict):
        try:
            message = response["body"]["choices"][0]["message"]
        except Exception:
            message = None
        if isinstance(message, dict):
            for tc in message.get("tool_calls") or []:
                fn = (tc or {}).get("function") or {}
                name = str(fn.get("name") or "")
                raw_args = fn.get("arguments")
                args = {}
                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args)
                    except Exception:
                        args = {}
                elif isinstance(raw_args, dict):
                    args = raw_args
                calls.append((name, args if isinstance(args, dict) else {}))

    payload = step.get("payload")
    if isinstance(payload, dict) and step.get("type") == "tool_call_scheduled":
        args = payload.get("input")
        calls.append((str(payload.get("toolName", "")), args if isinstance(args, dict) else {}))

    message = step.get("message")
    if isinstance(message, dict):
        mc = message.get("content")
        if isinstance(mc, list):
            for block in mc:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    inp = block.get("input")
                    calls.append((str(block.get("name", "")), inp if isinstance(inp, dict) else {}))
    return calls

_INJECTED_PROMPT_MARKERS = (
    "<system-reminder",
    "critical: respond with text",
    "respond with text only",
    "[tokensafe",
    "[oumv",
)


def _is_injected_prompt(text: str) -> bool:
    """True for harness-injected reminders that arrive as user-role messages."""
    lowered = text.lower()
    return any(marker in lowered for marker in _INJECTED_PROMPT_MARKERS)


def _clean_goal_text(text: str) -> str:
    """Extracts a clean, single-sentence goal from raw user text or prompt."""
    if not text:
        return ""
    # Strip XML-style wrapper tags commonly injected into agent transcripts
    cleaned = re.sub(r"</?(?:USER_REQUEST|CONTEXT_SUMMARY|ADDITIONAL_METADATA|SYSTEM_MESSAGE)[^>]*>", " ", text)
    # Remove markdown headers or bolding
    cleaned = re.sub(r"^[#\s*\->]+", "", cleaned.strip())
    # Take first meaningful non-empty line
    lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip() and not ln.strip().startswith(("//", "/*", "<!--"))]
    if not lines:
        return ""
    first_line = lines[0]
    # Remove leading numbering like "1. ", "1) "
    first_line = re.sub(r"^\d+[\.\)]\s*", "", first_line).strip()
    if len(first_line) > 120:
        first_line = first_line[:117] + "..."
    return first_line

class HistoryCompactor:
    """Folds past conversation tool steps into a compact state ledger with an orientation digest."""

    @classmethod
    def build_orientation_digest(
        cls,
        steps: List[dict],
        read_files: set,
        edited_files: set,
        executed_commands: List[str]
    ) -> Tuple[str, Dict[str, str]]:
        """
        Builds a concise 3-line Post-Compaction Orientation Digest:
        1. Active Goal: extracted from latest user prompt
        2. Last State: the most recent action/operation performed
        3. Next Focus: clear immediate resumption directive
        """
        # 1. Active Goal
        active_goal = ""
        for step in reversed(steps):
            source = str(step.get("source") or "")
            stype = str(step.get("type") or "")
            role = str(step.get("role") or "")
            if not role and isinstance(step.get("message"), dict):
                role = str(step["message"].get("role") or "")

            if source == "USER_EXPLICIT" or stype == "USER_INPUT" or role == "user":
                raw_txt = _extract_step_text(step)
                if _is_injected_prompt(raw_txt):
                    continue
                cleaned = _clean_goal_text(raw_txt)
                if cleaned:
                    active_goal = cleaned
                    break

        if not active_goal:
            # Fallback to key turns if any
            for step in steps:
                raw_txt = _extract_step_text(step)
                if _is_injected_prompt(raw_txt):
                    continue
                cleaned = _clean_goal_text(raw_txt)
                if cleaned and len(cleaned) > 15:
                    active_goal = cleaned
                    break

        if not active_goal:
            active_goal = "Resume multi-step task execution"

        # 2. Last State
        last_state = ""
        last_tool = None
        # Find the last tool call executed across all steps
        for step in reversed(steps):
            tcs = _extract_step_tool_calls(step)
            if tcs:
                last_tool = tcs[-1]
                break

        if last_tool:
            tname, targs = last_tool
            p = _arg_value(targs, _PATH_KEYS)
            base_p = os.path.basename(p) if p else ""
            if tname in _EDIT_TOOLS:
                last_state = f"Modified {base_p or 'target files'}; changes applied"
            elif tname in _COMMAND_TOOLS:
                cmd = _arg_value(targs, _COMMAND_KEYS)
                short_cmd = cmd.split()[0] if cmd.split() else cmd
                last_state = f"Executed {short_cmd}; output processed"
            elif tname in _READ_TOOLS:
                last_state = f"Inspected {base_p or 'target code'}; context gathered"
            else:
                last_state = f"Invoked tool {tname}"

        if not last_state:
            if edited_files:
                last_state = f"Modified {', '.join(sorted(edited_files)[:3])}"
            elif executed_commands:
                last_state = f"Executed {', '.join(executed_commands[-2:])}"
            elif read_files:
                last_state = f"Inspected {', '.join(sorted(read_files)[:3])}"
            else:
                last_state = "Processed past conversation turns"

        # 3. Next Focus
        if edited_files:
            next_focus = "Verify modifications with tests and check edge cases."
        elif executed_commands and any("test" in c for c in executed_commands):
            next_focus = "Proceed with remaining tasks or finalize changes."
        elif read_files:
            next_focus = "Apply surgical edits based on inspected symbols."
        else:
            next_focus = "Continue direct execution toward active goal without repeating past reads."

        digest_dict = {
            "active_goal": active_goal,
            "last_state": last_state,
            "next_focus": next_focus,
        }

        digest_lines = [
            "[Post-Compaction Orientation Digest]:",
            f"1. Active Goal: {active_goal}",
            f"2. Last State: {last_state}",
            f"3. Next Focus: {next_focus}",
        ]
        return "\n".join(digest_lines), digest_dict

    @classmethod
    def measure_transcript(cls, transcript_path: str) -> int:
        """Lightweight transcript mass probe: returns the token count of all
        step content without building a ledger. Used by the Context Budget HUD
        to count REAL history mass on early turns where the full fold is not
        yet active (< 3 turns) and by hooks that only need the estimate."""
        if not transcript_path or not os.path.exists(transcript_path):
            return 0
        total = 0
        try:
            with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        step = json.loads(line)
                    except Exception:
                        continue
                    text = _extract_step_text(step)
                    if text:
                        total += count_tokens(text)
        except Exception:
            return 0
        return total

    @classmethod
    def fold_transcript(cls, transcript_path: str, invocation_num: int) -> Tuple[Optional[str], int, int, Dict[str, Any]]:
        """
        Parses transcriptPath and generates a Consolidated State Ledger if turns >= 3.
        Returns: (consolidated_ledger, orig_history_tokens, folded_tokens, stats_dict)
        """
        if invocation_num < 3 or not transcript_path or not os.path.exists(transcript_path):
            return None, 0, 0, {}

        try:
            steps = []
            with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            parsed = json.loads(line)
                        except Exception:
                            continue
                        # A model-io record whose newest message is the user's
                        # prompt is the turn boundary the digest looks for.
                        if (
                            isinstance(parsed, dict)
                            and parsed.get("type") == "model_io"
                            and _model_io_parts(parsed)[2]
                        ):
                            parsed["role"] = "user"
                        steps.append(parsed)
        except Exception:
            return None, 0, 0, {}

        if len(steps) < 4:
            return None, 0, 0, {}

        read_files = set()
        edited_files = set()
        executed_commands = []
        key_turns: List[str] = []
        orig_tokens = 0

        for step in steps:
            if not isinstance(step, dict):
                continue
            text = _extract_step_text(step)
            if text:
                orig_tokens += count_tokens(text)
                if len(key_turns) < 5:
                    first_ln = text.splitlines()[0].strip()
                    if len(first_ln) > 8:
                        key_turns.append(first_ln[:100])

            # Inspect tool calls (Antigravity tool_calls / Claude Code tool_use)
            for name, args in _extract_step_tool_calls(step):
                if name in _READ_TOOLS:
                    p = _arg_value(args, _PATH_KEYS)
                    if p:
                        read_files.add(os.path.basename(p))
                elif name in _EDIT_TOOLS:
                    p = _arg_value(args, _PATH_KEYS)
                    if p:
                        edited_files.add(os.path.basename(p))
                elif name in _COMMAND_TOOLS:
                    cmd = _arg_value(args, _COMMAND_KEYS)
                    if cmd:
                        clean_cmd = cmd.split()[0] if cmd.split() else cmd
                        if clean_cmd not in executed_commands:
                            executed_commands.append(clean_cmd)

        # Only fold if history has accumulated significant mass (> 1,000 tokens)
        if orig_tokens < 1000:
            return None, 0, 0, {}

        # Build Consolidated State Ledger & Post-Compaction Orientation Digest
        digest_text, digest_dict = cls.build_orientation_digest(
            steps, read_files, edited_files, executed_commands
        )

        ledger_lines = ["[TOCP State Ledger]:"]
        if read_files:
            ledger_lines.append(f"• Inspected: {', '.join(sorted(read_files))}")
        if edited_files:
            ledger_lines.append(f"• Modified: {', '.join(sorted(edited_files))}")
        if executed_commands:
            ledger_lines.append(f"• Executed: {', '.join(executed_commands[:5])}")
        if not (read_files or edited_files or executed_commands):
            if not key_turns:
                return None, 0, 0, {}
            for snippet in key_turns[:3]:
                ledger_lines.append(f"• Context: {snippet}")

        if digest_text:
            ledger_lines.append("")
            ledger_lines.append(digest_text)

        consolidated_text = "\n".join(ledger_lines)
        folded_tokens = count_tokens(consolidated_text)

        saved_tokens = max(0, orig_tokens - folded_tokens)
        stats = {
            "read_files_count": len(read_files),
            "edited_files_count": len(edited_files),
            "executed_commands_count": len(executed_commands),
            "saved_tokens": saved_tokens,
            "orig_tokens": orig_tokens,
            "folded_tokens": folded_tokens,
            "orientation_digest": digest_dict,
        }

        return consolidated_text, orig_tokens, folded_tokens, stats
