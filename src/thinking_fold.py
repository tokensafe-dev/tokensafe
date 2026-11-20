"""
TokenSafe Aged Thinking Fold (proxy stage 1.6, OPT-IN, DEFAULT OFF).

Collapses extended-thinking payloads of AGED assistant turns into one-line
stubs inside the proxy pipeline. The ``_tokensafe_thinking`` side-channel key
stays present holding a valid text block, so shape-sensitive mirrors (the
z.ai 1214 class) keep accepting the rebuilt tool_use turns — only the mass
shrinks. Recent turns (last ``keep_recent`` messages) are never touched, and
the fold is idempotent via an in-content marker (KV-cache stable re-runs).

DEFAULT OFF on purpose: enable with TOKENSAFE_FOLD_THINKING=1 only after the
upstream shape probe matrix passes (z.ai weekly quota resets 2026-11-24).
"""

import os
from typing import Any, Dict, List, Tuple

from src.tokenizer import count_tokens

THINKING_FOLD_MARKER = "[TokenSafe: aged thinking folded"
DEFAULT_KEEP_RECENT = 6
DEFAULT_MIN_CHARS = 2000


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def _thinking_text_len(blocks: Any) -> int:
    if not isinstance(blocks, list):
        return 0
    total = 0
    for b in blocks:
        if isinstance(b, dict):
            total += len(str(b.get("text") or b.get("thinking") or ""))
        elif isinstance(b, str):
            total += len(b)
    return total


def _has_marker(blocks: Any) -> bool:
    if not isinstance(blocks, list):
        return False
    for b in blocks:
        text = str(b.get("text") or b.get("thinking") or "") if isinstance(b, dict) else str(b)
        if THINKING_FOLD_MARKER in text:
            return True
    return False


def fold_aged_thinking(
    messages: List[Dict[str, Any]],
    keep_recent: int = DEFAULT_KEEP_RECENT,
    min_chars: int = DEFAULT_MIN_CHARS,
    record: bool = True,
    session_id: str = "proxy_session",
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Folds aged ``_tokensafe_thinking`` payloads in place. Returns (messages, stats).

    Gates: TOKENSAFE_FOLD_THINKING=1 required (DEFAULT OFF); thinking inside
    the last ``keep_recent`` messages is never folded; payloads under
    ``min_chars`` stay verbatim; already-folded turns are never re-folded."""
    stats: Dict[str, Any] = {"folded": 0, "orig_tokens": 0, "new_tokens": 0, "saved_tokens": 0}
    if not messages or not _env_flag("TOKENSAFE_FOLD_THINKING"):
        return messages, stats

    protected_from = max(0, len(messages) - keep_recent)
    for idx, msg in enumerate(messages):
        if idx >= protected_from or not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        blocks = msg.get("_tokensafe_thinking")
        if not isinstance(blocks, list) or not blocks or _has_marker(blocks):
            continue
        orig_len = _thinking_text_len(blocks)
        if orig_len < min_chars:
            continue
        orig_tokens = count_tokens(
            "".join(
                str(b.get("text") or b.get("thinking") or "") if isinstance(b, dict) else str(b)
                for b in blocks
            )
        )
        stub_text = f"{THINKING_FOLD_MARKER} (~{orig_tokens} tok) — current turn's thinking was consumed; history prefix kept for shape validity]"
        msg["_tokensafe_thinking"] = [{"type": "text", "text": stub_text}]
        new_tokens = count_tokens(stub_text)
        stats["folded"] += 1
        stats["orig_tokens"] += orig_tokens
        stats["new_tokens"] += new_tokens
        stats["saved_tokens"] += max(0, orig_tokens - new_tokens)

    if stats["folded"] and record:
        try:
            from src.metrics import MetricsTracker
            MetricsTracker().record_event(
                session_id=session_id,
                event_type="THINKING_FOLD",
                original_tokens=stats["orig_tokens"],
                optimized_tokens=stats["new_tokens"],
                details={"folded": stats["folded"]},
                measurement="measured",
            )
        except Exception:
            pass

    return messages, stats
