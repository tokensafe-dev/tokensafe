"""TokenSafe MCP client for the `tokensafe` benchmark arm.

The third arm measures the product's own tools (AST skeleton reader, scoped
search, architecture map) without OUMV. It speaks the real MCP server over stdio —
the same process `tokensafe mcp` starts for an agent — so the arm is not a
strawman reimplementation.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

TOKENSAFE_BIN = Path(__file__).resolve().parents[2] / "bin" / "tokensafe"


class TokenSafeMcp:
    def __init__(self, workspace: Path, timeout: float = 120.0, client_name: str = "oumv-bench"):
        self.timeout = timeout
        self.proc = subprocess.Popen(
            [sys.executable, str(TOKENSAFE_BIN), "mcp"],
            cwd=str(workspace),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self._id = 0
        self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": client_name, "version": "0"},
            },
        )
        self._notify("notifications/initialized", {})

    def _send(self, payload: Dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def _notify(self, method: str, params: Dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _rpc(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self._id += 1
        request_id = self._id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        assert self.proc.stdout is not None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                return {"error": "MCP server closed the stream"}
            try:
                body = json.loads(line)
            except json.JSONDecodeError:
                continue
            if body.get("id") != request_id:
                continue
            return body

    def tool_schemas(self, exclude: Optional[set] = None) -> List[Dict[str, Any]]:
        """The tools the server actually advertises, in OpenAI function-calling shape.

        The arm must show the model the *shipped* schema: its size and wording are part
        of the product's cost (re-sent on every request), and a hand-written stand-in
        both hides that cost and measures a configuration no user runs.
        """
        body = self._rpc("tools/list", {})
        tools = (body.get("result") or {}).get("tools") or []
        out: List[Dict[str, Any]] = []
        for tool in tools:
            name = str(tool.get("name") or "")
            if not name or (exclude and name in exclude):
                continue
            out.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(tool.get("description") or ""),
                    "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
                },
            })
        return out

    def call(self, name: str, arguments: Dict[str, Any]) -> Any:
        body = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if body.get("error"):
            return {"error": body["error"]}
        result = body.get("result") or {}
        if result.get("isError"):
            content = result.get("content") or []
            text = content[0].get("text") if content else "MCP tool failed"
            return {"error": text}
        content = result.get("content") or []
        return content[0].get("text") if content else ""

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def _client_name(workspace: Path) -> str:
    """One MCP session per benchmark run.

    The server keys its dedup cache, loop guard and pointer state by session, and the
    session id is derived from `clientInfo.name`. A constant name would make run 2 of
    the same task inherit run 1's "you already read this" state — the agent would get
    a pointer to content it never saw.
    """
    digest = hashlib.sha1(str(workspace).encode("utf-8")).hexdigest()[:10]
    return f"oumv-bench-{digest}"


def open_client(workspace: Path) -> Optional[TokenSafeMcp]:
    """Best-effort: a missing CLI means the arm cannot run, not that the bench crashes."""
    if not TOKENSAFE_BIN.exists():
        return None
    try:
        return TokenSafeMcp(workspace, client_name=_client_name(workspace))
    except Exception:
        return None
