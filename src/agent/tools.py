"""Tool surface for the in-process loop.

The loop calls ``handle_tool_call`` directly. There is no stdio JSON-RPC hop
and, more importantly, no second copy of the schemas: the same ``TOOLS`` list
that the MCP client sees is the list the model sees. One registry, one
behaviour, one place to change an argument contract.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

DEFAULT_TOOL_NAMES: Sequence[str] = (
    "smart_read_file",
    "scoped_search",
    "apply_edits",
    "repo_map",
    "session_context",
    "oumv",
    "run_helper",
)

# Tool output above this is truncated before it re-enters the model context.
# The MCP server already caps its own replies, but a direct call bypasses the
# transport, so the loop enforces its own ceiling.
RESULT_MAX_CHARS = int(os.environ.get("TOKENSAFE_AGENT_TOOL_RESULT_CHARS", "24000"))

_TOOL_KINDS = {
    "smart_read_file": "read",
    "scoped_search": "search",
    "repo_map": "search",
    "apply_edits": "edit",
    "run_helper": "other",
    "session_context": "other",
    "oumv": "other",
}


@dataclass
class ToolResult:
    name: str
    ok: bool
    text: str
    raw: Dict[str, Any] = field(default_factory=dict)
    truncated: bool = False


def tool_schemas(names: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    """OpenAI/Anthropic-neutral schemas straight from the MCP tool registry."""
    from src.mcp_server import TOOLS

    index = {tool["name"]: tool for tool in TOOLS}
    selected = list(names or DEFAULT_TOOL_NAMES)
    schemas: List[Dict[str, Any]] = []
    for name in selected:
        spec = index.get(name)
        if not spec:
            continue
        schemas.append(
            {
                "name": name,
                "description": spec.get("description", ""),
                "input_schema": spec.get("inputSchema") or {"type": "object", "properties": {}},
            }
        )
    return schemas


def tool_kind(name: str) -> str:
    return _TOOL_KINDS.get(name, "other")


def dispatch_tool(name: str, arguments: Dict[str, Any]) -> ToolResult:
    """Execute one tool call through the canonical MCP handler."""
    from src.mcp_server import handle_tool_call

    try:
        result = handle_tool_call(name, dict(arguments or {}))
    except Exception as exc:  # the handler catches internally; this is belt-and-braces
        return ToolResult(name=name, ok=False, text=f"{type(exc).__name__}: {exc}")

    parts = result.get("content") or []
    text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
    ok = not result.get("isError", False)
    if not text:
        text = "(no output)"
    truncated = False
    if len(text) > RESULT_MAX_CHARS:
        omitted = len(text) - RESULT_MAX_CHARS
        text = (
            text[:RESULT_MAX_CHARS]
            + f"\n// [TokenSafe: tool result truncated, {omitted:,} chars omitted. "
              f"Narrow the call instead of re-reading.]"
        )
        truncated = True
    return ToolResult(name=name, ok=ok, text=text, raw=result, truncated=truncated)
