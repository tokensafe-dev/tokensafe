"""Shared context-governance kernel.

Both the lifecycle hooks and the in-process agent loop call these functions.
The hook path and the agent path must never drift into two different
compaction policies: that is how a product ends up with two metrics stories
and two definitions of "saved tokens".
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from src.compactor import PromptCompactor
from src.governor import ContextBudget, ContextGovernor
from src.history_compactor import HistoryCompactor
from src.tokenizer import count_tokens

# Below this saving a prompt rewrite is noise: it burns a rewrite and changes
# the model's instructions for less than a line of text.
MIN_PROMPT_SAVING = 5


def compact_prompt(text: str, enable_distill: bool = True) -> Dict[str, Any]:
    """Compile a user prompt into TOCP intent form (same call the hook makes)."""
    return PromptCompactor.compact(text, enable_distill=enable_distill)


def fold_history(transcript_path: str, invocation_num: int) -> Tuple[Optional[str], int, int, Dict[str, Any]]:
    """Fold past turns into a Consolidated State Ledger."""
    return HistoryCompactor.fold_transcript(transcript_path, invocation_num)


def measure_transcript(transcript_path: str) -> int:
    """Real token mass of a session transcript (0 when unreadable)."""
    try:
        return int(HistoryCompactor.measure_transcript(transcript_path) or 0)
    except Exception:
        return 0


def estimate_budget(
    tracker,
    session_id: str,
    model: str = "",
    history_tokens: int = 0,
    transcript_path: str = "",
) -> Dict[str, Any]:
    """Context Budget HUD, shared by hooks and the in-loop governor."""
    return ContextBudget.estimate(
        tracker,
        session_id=session_id,
        model=model,
        history_tokens=history_tokens,
        transcript_path=transcript_path,
    )


def govern_messages(
    messages: List[Dict[str, Any]],
    session_id: str,
    model: str = "",
    max_tokens: Optional[int] = None,
    target_tokens: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Trim the live message list before a model call.

    ``ContextGovernor`` is the single implementation: it compacts code blocks,
    folds deep history into a ledger and records the CONTEXT_GOVERNOR metric.
    """
    return ContextGovernor.govern_messages(
        messages,
        max_tokens=max_tokens,
        target_tokens=target_tokens,
        session_id=session_id,
        model=model,
    )


def resolve_window(model: str = "", window_tokens: Optional[int] = None) -> int:
    return ContextBudget.resolve_window(model, window_tokens)


def messages_tokens(messages: List[Dict[str, Any]]) -> int:
    """Cheap token mass of a message list (content + serialized tool calls)."""
    total = 0
    for msg in messages:
        if not isinstance(msg, dict):
            total += count_tokens(str(msg))
            continue
        content = msg.get("content")
        if isinstance(content, str):
            total += count_tokens(content)
        elif content is not None:
            total += count_tokens(json.dumps(content, ensure_ascii=False))
        for call in msg.get("tool_calls") or []:
            total += count_tokens(json.dumps(call, ensure_ascii=False))
        total += 4  # role/separator overhead
    return total
