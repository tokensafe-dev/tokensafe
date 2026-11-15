"""
TokenSafe Superseded Tool Output Pruning (v3.3.0).

Identifies intermediate file read results in multi-turn transcripts that have
since been modified by subsequent edits or re-read in subsequent steps.
Replaces obsolete intermediate payloads with a 1-line reversible tombstone
pointing to TokenSafe's local stash, recovering 30-50% context window mass.
"""

import os
import json
from typing import List, Dict, Any, Tuple, Optional, Set
from src.stash import stash_block
from src.tokenizer import count_tokens

_READ_TOOLS = {
    "read_file", "view_file", "Read", "smart_read_file",
}
_EDIT_TOOLS = {
    "edit_file", "write_file", "Edit", "Write", "MultiEdit", "NotebookEdit", "replace_file_content"
}
_PATH_KEYS = ("Path", "path", "FilePath", "file_path", "notebook_path")


def _extract_path_from_args(args: Any) -> Optional[str]:
    if not isinstance(args, dict):
        return None
    for k in _PATH_KEYS:
        val = args.get(k)
        if isinstance(val, str) and val.strip():
            return os.path.abspath(val.strip())
    return None


class SupersededPruner:
    """Detects and prunes outdated tool outputs in agent conversations."""

    @classmethod
    def prune_steps(cls, steps: List[dict]) -> Tuple[List[dict], int, int, Dict[str, Any]]:
        """
        Analyzes sequential conversation steps, identifies reads that were superseded
        by subsequent edits or reads of the same file, and replaces them with a stash tombstone.

        Returns: (pruned_steps, orig_tokens, pruned_tokens, stats)
        """
        if not steps or len(steps) < 3:
            return steps, 0, 0, {"pruned_count": 0, "saved_tokens": 0}

        # Track file actions: map file_path -> list of (step_index, action_type, call_dict)
        # action_type: "READ" or "EDIT"
        file_actions: Dict[str, List[Tuple[int, str]]] = {}
        step_read_files: Dict[int, str] = {}

        for idx, step in enumerate(steps):
            if not isinstance(step, dict):
                continue

            # Antigravity format: tool_calls
            tool_calls = step.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    tname = tc.get("name", "")
                    args = tc.get("args")
                    fpath = _extract_path_from_args(args)
                    if not fpath:
                        continue
                    if tname in _READ_TOOLS:
                        file_actions.setdefault(fpath, []).append((idx, "READ"))
                        step_read_files[idx] = fpath
                    elif tname in _EDIT_TOOLS:
                        file_actions.setdefault(fpath, []).append((idx, "EDIT"))

            # Claude Code format: message.content blocks
            msg = step.get("message")
            if isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            tname = b.get("name", "")
                            inp = b.get("input")
                            fpath = _extract_path_from_args(inp)
                            if not fpath:
                                continue
                            if tname in _READ_TOOLS:
                                file_actions.setdefault(fpath, []).append((idx, "READ"))
                                step_read_files[idx] = fpath
                            elif tname in _EDIT_TOOLS:
                                file_actions.setdefault(fpath, []).append((idx, "EDIT"))

        # Identify superseded read step indices:
        # A read at step i is superseded if there is an EDIT at step j > i,
        # OR another READ at step k > i of the same file.
        superseded_map: Dict[int, Tuple[int, str, str]] = {}  # step_idx -> (superseded_by_step, action, fpath)

        for fpath, actions in file_actions.items():
            for i, (step_idx, action) in enumerate(actions):
                if action == "READ":
                    # Check if there is any subsequent action on this file
                    subsequent = actions[i + 1:]
                    if subsequent:
                        next_step_idx, next_action = subsequent[0]
                        superseded_map[step_idx] = (next_step_idx, next_action, fpath)

        if not superseded_map:
            return steps, 0, 0, {"pruned_count": 0, "saved_tokens": 0}

        # Apply tombstoning to the tool results of the superseded steps
        pruned_steps: List[dict] = []
        orig_tokens_total = 0
        pruned_tokens_total = 0
        pruned_count = 0

        for idx, step in enumerate(steps):
            if idx not in superseded_map:
                pruned_steps.append(step)
                continue

            by_step, by_action, fpath = superseded_map[idx]
            fname = os.path.basename(fpath)
            new_step = json.loads(json.dumps(step))  # deep copy
            modified = False

            # Check Antigravity output/content in this step or subsequent tool response step
            content = new_step.get("content")
            if isinstance(content, str) and len(content) > 150:
                orig_t = count_tokens(content)
                _, marker = stash_block(content, source="superseded_read")
                tombstone = (
                    f"[TokenSafe: Output superseded by step #{by_step + 1} ({by_action.lower()} {fname}) — {marker}]"
                )
                new_step["content"] = tombstone
                pruned_t = count_tokens(tombstone)
                orig_tokens_total += orig_t
                pruned_tokens_total += pruned_t
                modified = True

            # Claude Code message content tool_result blocks
            msg = new_step.get("message")
            if isinstance(msg, dict):
                mc = msg.get("content")
                if isinstance(mc, list):
                    for block in mc:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            tc = block.get("content")
                            if isinstance(tc, str) and len(tc) > 150:
                                orig_t = count_tokens(tc)
                                _, marker = stash_block(tc, source="superseded_read")
                                tombstone = (
                                    f"[TokenSafe: Output superseded by step #{by_step + 1} ({by_action.lower()} {fname}) — {marker}]"
                                )
                                block["content"] = tombstone
                                pruned_t = count_tokens(tombstone)
                                orig_tokens_total += orig_t
                                pruned_tokens_total += pruned_t
                                modified = True

            if modified:
                pruned_count += 1
            pruned_steps.append(new_step)

        saved_tokens = max(0, orig_tokens_total - pruned_tokens_total)
        stats = {
            "pruned_count": pruned_count,
            "saved_tokens": saved_tokens,
            "orig_tokens": orig_tokens_total,
            "pruned_tokens": pruned_tokens_total
        }
        return pruned_steps, orig_tokens_total, pruned_tokens_total, stats

    @classmethod
    def prune_transcript(
        cls,
        transcript_path: str,
        output_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Loads a JSONL transcript, prunes superseded reads, and saves the result.
        If output_path is omitted or identical to transcript_path, overwrites safely.
        """
        if not os.path.exists(transcript_path):
            return {"success": False, "error": f"Transcript not found: {transcript_path}"}

        steps = []
        try:
            with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            steps.append(json.loads(line))
                        except Exception:
                            pass
        except Exception as e:
            return {"success": False, "error": str(e)}

        pruned_steps, orig_t, pruned_t, stats = cls.prune_steps(steps)
        if stats["pruned_count"] > 0:
            target = output_path or transcript_path
            tmp_target = f"{target}.tmp.{os.getpid()}"
            try:
                with open(tmp_target, "w", encoding="utf-8") as f:
                    for s in pruned_steps:
                        f.write(json.dumps(s, ensure_ascii=False) + "\n")
                os.replace(tmp_target, target)
            except Exception as e:
                try:
                    if os.path.exists(tmp_target):
                        os.unlink(tmp_target)
                except OSError:
                    pass
                return {"success": False, "error": f"Failed to save pruned transcript: {e}"}

        return {
            "success": True,
            "transcript_path": transcript_path,
            **stats
        }
