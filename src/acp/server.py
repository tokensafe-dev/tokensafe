"""Agent Client Protocol (ACP) server over stdio.

ACP is how editors that do not host their own agent run an external one: Zed
spawns the agent as a subprocess and speaks JSON-RPC 2.0 over stdin/stdout;
JetBrains AI Assistant does the same via a custom agent entry; Neovim
(CodeCompanion, avante) and Emacs (agent-shell) have ACP clients too. Shipping
this adapter gives TokenSafe a native agent inside those editors without
anyone building an editor.

Scope of this server: sessions, prompts, streaming updates, tool call
visibility and cancellation. Embedded context (text blocks) is accepted;
images are rejected with an explanation because the loop has no vision path.
There is no interactive permission prompt yet — tool calls run under the same
guard as the MCP server (destructive commands and secret reads are refused),
which is the documented v1 boundary.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, IO, List, Optional

from src.agent.loop import DEFAULT_SYSTEM_PROMPT, AgentConfig, AgentLoop, RunResult
from src.agent.providers import Provider, ProviderError, resolve_provider

PROTOCOL_VERSION = 1
SERVER_NAME = "tokensafe-agent"

_STOP_REASONS = {"end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"}


def _map_stop(reason: str) -> str:
    if reason in _STOP_REASONS:
        return reason
    if reason == "budget_exceeded":
        return "max_turn_requests"
    if reason == "empty_prompt":
        return "end_turn"
    return "end_turn"


@dataclass
class _Session:
    session_id: str
    root: str
    loop: AgentLoop
    cancel: threading.Event = field(default_factory=threading.Event)


class AcpServer:
    """Minimal, testable ACP server.

    ``provider_factory`` exists so tests (and embedders) can inject a
    deterministic provider instead of reaching the network.
    """

    def __init__(
        self,
        reader: IO[str],
        writer: IO[str],
        provider_factory: Optional[Callable[[str, str], Provider]] = None,
        client_name: str = "",
    ):
        self._reader = reader
        self._writer = writer
        self._write_lock = threading.Lock()
        self._sessions: Dict[str, _Session] = {}
        self._workers: List[threading.Thread] = []
        self._provider_factory = provider_factory or _default_provider
        self._client_name = client_name

    # -- io ----------------------------------------------------------------

    def _write(self, message: Dict[str, Any]) -> None:
        payload = json.dumps(message, ensure_ascii=False)
        with self._write_lock:
            try:
                self._writer.write(payload + "\n")
                self._writer.flush()
            except Exception:
                pass

    def _respond(self, request_id: Any, result: Dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _error(self, request_id: Any, code: int, message: str) -> None:
        self._write({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    def _notify(self, method: str, params: Dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    # -- main loop ---------------------------------------------------------

    def serve(self) -> None:
        for line in self._reader:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except Exception:
                continue
            if isinstance(message, dict):
                self.handle(message)
        self.wait_idle()

    def wait_idle(self, timeout: float = 30.0) -> None:
        """Join in-flight prompt workers (used by tests and graceful shutdown)."""
        for worker in list(self._workers):
            worker.join(timeout)

    # -- dispatch ----------------------------------------------------------

    def handle(self, message: Dict[str, Any]) -> None:
        method = message.get("method")
        if not isinstance(method, str):
            return  # client responses to agent requests are not used yet
        request_id = message.get("id")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}

        if request_id is None:
            self._handle_notification(method, params)
            return

        try:
            if method == "initialize":
                self._respond(request_id, self._initialize(params))
            elif method == "authenticate":
                self._respond(request_id, {})
            elif method == "session/new":
                self._respond(request_id, self._new_session(params))
            elif method == "session/prompt":
                self._start_prompt(request_id, params)
            else:
                self._error(request_id, -32601, f"Method not found: {method}")
        except ProviderError as exc:
            self._error(request_id, -32000, str(exc))
        except Exception as exc:
            self._error(request_id, -32603, f"{type(exc).__name__}: {exc}")

    def _handle_notification(self, method: str, params: Dict[str, Any]) -> None:
        if method == "session/cancel":
            session = self._sessions.get(str(params.get("sessionId") or ""))
            if session:
                session.cancel.set()
        # Client notifications we do not model (fs/terminal capability updates)
        # are ignored rather than answered: notifications must never get a reply.

    # -- methods -----------------------------------------------------------

    def _initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        requested = params.get("protocolVersion")
        version = requested if requested == PROTOCOL_VERSION else PROTOCOL_VERSION
        client_info = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
        if client_info.get("name"):
            self._client_name = str(client_info["name"])
        return {
            "protocolVersion": version,
            "agentCapabilities": {
                "promptCapabilities": {"image": False, "audio": False, "embeddedContext": True},
                "loadSession": False,
            },
            "agentInfo": {"name": SERVER_NAME, "version": _version()},
            "authMethods": [],
        }

    def _new_session(self, params: Dict[str, Any]) -> Dict[str, Any]:
        cwd = str(params.get("cwd") or "").strip()
        root = os.path.realpath(cwd) if cwd and os.path.isdir(cwd) else os.path.realpath(os.getcwd())
        session_id = f"acp-{uuid.uuid4().hex[:12]}"
        provider = self._provider_factory(root, session_id)
        config = AgentConfig(
            system_prompt=(
                DEFAULT_SYSTEM_PROMPT
                + f"\n\nActive workspace: {root}\n"
                + "All paths are resolved against this root. Absolute paths outside it are refused."
            )
        )
        loop = AgentLoop(
            root=root,
            session_id=f"acp:{session_id}",
            provider=provider,
            config=config,
            event_sink=lambda event: self._forward_update(session_id, event),
        )
        self._sessions[session_id] = _Session(session_id=session_id, root=root, loop=loop)
        return {"sessionId": session_id}

    def _start_prompt(self, request_id: Any, params: Dict[str, Any]) -> None:
        session_id = str(params.get("sessionId") or "")
        session = self._sessions.get(session_id)
        if not session:
            self._error(request_id, -32602, f"unknown sessionId '{session_id}'")
            return
        text, rejected = _prompt_text(params.get("prompt"))
        if rejected:
            self._notify(
                "session/update",
                {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": rejected},
                    },
                },
            )
        if not text.strip():
            self._respond(request_id, {"stopReason": "end_turn"})
            return

        session.cancel.clear()

        def worker() -> None:
            try:
                result: RunResult = session.loop.run(text, cancel=session.cancel)
                self._respond(request_id, {"stopReason": _map_stop(result.stop_reason)})
            except ProviderError as exc:
                self._error(request_id, -32000, str(exc))
            except Exception as exc:
                self._error(request_id, -32603, f"{type(exc).__name__}: {exc}")

        thread = threading.Thread(target=worker, name=f"acp-prompt-{session_id}", daemon=True)
        self._workers.append(thread)
        thread.start()

    def _forward_update(self, session_id: str, event: Dict[str, Any]) -> None:
        kind = event.get("type")
        update: Optional[Dict[str, Any]] = None
        if kind == "text" and event.get("text"):
            update = {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": event["text"]},
            }
        elif kind == "thinking" and event.get("text"):
            update = {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": event["text"]},
            }
        elif kind == "tool_call":
            update = {
                "sessionUpdate": "tool_call",
                "toolCallId": event.get("id") or "",
                "title": _tool_title(str(event.get("name") or ""), event.get("arguments")),
                "kind": event.get("kind") or "other",
                "status": "in_progress",
                "rawInput": event.get("arguments"),
            }
        elif kind == "tool_result":
            update = {
                "sessionUpdate": "tool_call_update",
                "toolCallId": event.get("id") or "",
                "status": "completed" if event.get("ok") else "failed",
                "content": [
                    {"type": "content", "content": {"type": "text", "text": event.get("text") or ""}}
                ],
            }
        if update is not None:
            self._notify("session/update", {"sessionId": session_id, "update": update})


def _prompt_text(blocks: Any) -> tuple[str, str]:
    """Flatten ACP content blocks into one prompt; return (text, rejection_note)."""
    if isinstance(blocks, str):
        return blocks, ""
    if not isinstance(blocks, list):
        return "", ""
    parts: List[str] = []
    notes: List[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            parts.append(str(block.get("text") or ""))
        elif block_type == "resource_link":
            uri = str(block.get("uri") or "")
            parts.append(f"[resource] {uri}")
        elif block_type == "image":
            notes.append("Image input is not supported by the TokenSafe agent yet; describe it in text.")
        elif block_type == "resource":
            resource = block.get("resource") or {}
            if isinstance(resource, dict) and resource.get("text"):
                parts.append(str(resource["text"]))
    return "\n".join(part for part in parts if part).strip(), " ".join(notes)


def _tool_title(name: str, arguments: Any) -> str:
    if isinstance(arguments, dict):
        target = (
            arguments.get("path")
            or arguments.get("query")
            or arguments.get("pattern")
            or arguments.get("action")
            or ""
        )
        if target:
            return f"{name}: {str(target)[:120]}"
    return name or "tool"


def _default_provider(root: str, session_id: str) -> Provider:
    return resolve_provider()


def _version() -> str:
    try:
        from src.version_checker import CURRENT_VERSION
        return CURRENT_VERSION
    except Exception:
        return "0.0.0"


def run_acp_server(provider_factory: Optional[Callable[[str, str], Provider]] = None) -> None:
    server = AcpServer(sys.stdin, sys.stdout, provider_factory=provider_factory)
    server.serve()


def main() -> None:
    run_acp_server()


if __name__ == "__main__":
    main()
