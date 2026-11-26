"""Streaming model providers for the agent loop.

Two wire formats cover every backend TokenSafe needs: OpenAI-compatible
``/chat/completions`` (DeepSeek, OpenAI, Ollama, LM Studio, OpenRouter) and
Anthropic ``/v1/messages``. Both are parsed into one event stream, so the loop
never branches on provider and the UI gets the same deltas either way.

The hard part of streaming is not the HTTP call, it is reassembly: tool-call
arguments arrive as JSON fragments across chunks (OpenAI) or as
``input_json_delta`` pieces inside a content block (Anthropic). That logic
lives here, next to the wire format that defines it, and is covered by tests
with canned SSE fixtures.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

from src.agent.pricing import usd as price_usd
from src.json_repair import parse_tool_arguments


class ProviderError(RuntimeError):
    """Configuration or transport failure the caller should surface verbatim."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""
    parse_error: str = ""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit_tokens: int = 0
    calls: int = 0

    @property
    def cache_miss_tokens(self) -> int:
        return max(0, self.input_tokens - self.cache_hit_tokens)

    def add(self, payload: Dict[str, Any]) -> None:
        if not payload:
            return
        prompt = payload.get("prompt_tokens", payload.get("input_tokens"))
        completion = payload.get("completion_tokens", payload.get("output_tokens"))
        if prompt is not None:
            self.input_tokens = max(self.input_tokens, int(prompt))
        if completion is not None:
            self.output_tokens = max(self.output_tokens, int(completion))
        details = payload.get("prompt_tokens_details") or payload.get("cache_read_input_tokens_details") or {}
        cached = (
            payload.get("prompt_cache_hit_tokens")
            or payload.get("cache_read_input_tokens")
            or (details.get("cached_tokens") if isinstance(details, dict) else None)
        )
        if cached is not None:
            self.cache_hit_tokens = max(self.cache_hit_tokens, int(cached))

    def as_dict(self, model: str) -> Dict[str, Any]:
        cost = price_usd(model, self.cache_miss_tokens, self.cache_hit_tokens, self.output_tokens)
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "calls": self.calls,
            "usd": cost["usd"],
            "priced": cost["priced"],
            "price_snapshot": cost["price_snapshot"],
        }


@dataclass
class StreamEvent:
    type: str  # text | thinking | tool_call | usage | done
    text: str = ""
    tool_call: Optional[ToolCall] = None
    usage: Dict[str, Any] = field(default_factory=dict)
    stop_reason: str = ""
    detail: str = ""


@dataclass
class ProviderConfig:
    name: str
    wire: str  # "openai" | "anthropic"
    base_url: str
    api_key_env: str = ""
    default_model: str = ""


PRESETS: Dict[str, ProviderConfig] = {
    "deepseek": ProviderConfig("deepseek", "openai", "https://api.deepseek.com", "DEEPSEEK_API_KEY", "deepseek-chat"),
    "openai": ProviderConfig("openai", "openai", "https://api.openai.com/v1", "OPENAI_API_KEY", ""),
    "openrouter": ProviderConfig("openrouter", "openai", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", ""),
    "ollama": ProviderConfig("ollama", "openai", "http://127.0.0.1:11434/v1", "", "qwen2.5-coder:7b"),
    "lmstudio": ProviderConfig("lmstudio", "openai", "http://127.0.0.1:1234/v1", "", ""),
    "anthropic": ProviderConfig("anthropic", "anthropic", "https://api.anthropic.com", "ANTHROPIC_API_KEY", ""),
}


def _infer_provider(model: str) -> str:
    m = (model or "").lower()
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith(("gpt", "o1", "o3", "o4", "chatgpt")):
        return "openai"
    if m.startswith("deepseek"):
        return "deepseek"
    explicit = os.environ.get("TOKENSAFE_AGENT_PROVIDER", "").strip().lower()
    return explicit if explicit in PRESETS else "deepseek"


class Provider:
    """Base class: wire parsing lives in subclasses, config plumbing here."""

    wire = ""

    def __init__(
        self,
        config: ProviderConfig,
        model: str = "",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 120.0,
        max_output_tokens: Optional[int] = None,
    ):
        self.config = config
        self.model = model or config.default_model or os.environ.get("TOKENSAFE_AGENT_MODEL", "")
        if not self.model:
            raise ProviderError(
                f"no model selected for provider '{config.name}': pass --model or set TOKENSAFE_AGENT_MODEL"
            )
        self.base_url = (base_url or os.environ.get("TOKENSAFE_AGENT_BASE_URL") or config.base_url).rstrip("/")
        self.api_key = api_key if api_key is not None else (
            os.environ.get(config.api_key_env, "") if config.api_key_env else ""
        )
        if config.api_key_env and not self.api_key:
            raise ProviderError(
                f"missing API key: set {config.api_key_env} (provider '{config.name}')"
            )
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens or int(os.environ.get("TOKENSAFE_AGENT_MAX_OUTPUT", "4096"))

    # -- transport ---------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _endpoint(self) -> str:
        raise NotImplementedError

    def _payload(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
        raise NotImplementedError

    def _open(self, payload: Dict[str, Any]) -> Iterator[str]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self._endpoint(), data=body, headers=self._headers(), method="POST")
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:600]
            except Exception:
                pass
            raise ProviderError(f"{self.config.name} HTTP {exc.code}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"{self.config.name} connection failed: {exc.reason}") from exc
        try:
            for raw_line in response:
                yield raw_line.decode("utf-8", "replace").rstrip("\n")
        finally:
            try:
                response.close()
            except Exception:
                pass

    # -- public ------------------------------------------------------------

    def stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        cancel: Optional[threading.Event] = None,
    ) -> Iterator[StreamEvent]:
        raise NotImplementedError

    def complete(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        cancel: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        """Drain the stream into one assistant message (used by tests and the CLI)."""
        text_parts: List[str] = []
        tool_calls: List[ToolCall] = []
        usage = Usage()
        stop_reason = ""
        for event in self.stream(messages, tools=tools, cancel=cancel):
            if event.type == "text":
                text_parts.append(event.text)
            elif event.type == "tool_call" and event.tool_call:
                tool_calls.append(event.tool_call)
            elif event.type == "usage":
                usage.add(event.usage)
            elif event.type == "done":
                stop_reason = event.stop_reason or stop_reason
        return {
            "content": "".join(text_parts),
            "tool_calls": tool_calls,
            "usage": usage,
            "stop_reason": stop_reason,
        }


class OpenAICompatibleProvider(Provider):
    wire = "openai"

    def _endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _payload(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": float(os.environ.get("TOKENSAFE_AGENT_TEMPERATURE", "0")),
        }
        if tools:
            body["tools"] = [{"type": "function", "function": tool} for tool in tools]
        return body

    def stream(self, messages, tools=None, cancel=None):
        pending: Dict[int, Dict[str, str]] = {}
        stop_reason = ""
        for line in self._open(self._payload(messages, tools)):
            if cancel is not None and cancel.is_set():
                yield StreamEvent("done", stop_reason="cancelled")
                return
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                chunk = json.loads(data)
            except Exception:
                continue
            if chunk.get("usage"):
                yield StreamEvent("usage", usage=chunk["usage"])
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    yield StreamEvent("text", text=str(delta["content"]))
                if delta.get("reasoning_content"):
                    yield StreamEvent("thinking", text=str(delta["reasoning_content"]))
                for fragment in delta.get("tool_calls") or []:
                    index = int(fragment.get("index", 0) or 0)
                    slot = pending.setdefault(index, {"id": "", "name": "", "args": ""})
                    if fragment.get("id"):
                        slot["id"] = str(fragment["id"])
                    function = fragment.get("function") or {}
                    name = str(function.get("name") or "")
                    if name:
                        if not slot["name"]:
                            slot["name"] = name
                        elif name not in slot["name"]:
                            slot["name"] += name
                    if function.get("arguments"):
                        slot["args"] += str(function["arguments"])
                if choice.get("finish_reason"):
                    stop_reason = str(choice["finish_reason"])
        for index in sorted(pending):
            slot = pending[index]
            if not slot["name"]:
                continue
            yield StreamEvent("tool_call", tool_call=_finalize_tool_call(slot["id"], slot["name"], slot["args"]))
        if stop_reason == "tool_calls" or pending:
            stop_reason = "tool_calls" if pending else stop_reason
        yield StreamEvent("done", stop_reason=stop_reason or "end_turn")


class AnthropicProvider(Provider):
    wire = "anthropic"

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "anthropic-version": os.environ.get("ANTHROPIC_VERSION", "2023-06-01"),
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    def _endpoint(self) -> str:
        base = self.base_url
        if base.endswith("/v1"):
            return f"{base}/messages"
        return f"{base}/v1/messages"

    def _payload(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
        system, converted = to_anthropic_messages(messages)
        body: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_output_tokens,
            "messages": converted,
            "stream": True,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [
                {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("input_schema") or tool.get("inputSchema") or {"type": "object"},
                }
                for tool in tools
            ]
        return body

    def stream(self, messages, tools=None, cancel=None):
        current: Dict[int, Dict[str, Any]] = {}
        stop_reason = ""
        usage_payload: Dict[str, Any] = {}
        for line in self._open(self._payload(messages, tools)):
            if cancel is not None and cancel.is_set():
                yield StreamEvent("done", stop_reason="cancelled")
                return
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            try:
                event = json.loads(data)
            except Exception:
                continue
            kind = event.get("type")
            if kind == "content_block_start":
                block = event.get("content_block") or {}
                index = int(event.get("index", 0) or 0)
                if block.get("type") == "tool_use":
                    current[index] = {
                        "id": str(block.get("id") or ""),
                        "name": str(block.get("name") or ""),
                        "args": "",
                    }
            elif kind == "content_block_delta":
                index = int(event.get("index", 0) or 0)
                delta = event.get("delta") or {}
                delta_type = delta.get("type")
                if delta_type == "text_delta":
                    yield StreamEvent("text", text=str(delta.get("text") or ""))
                elif delta_type == "thinking_delta":
                    yield StreamEvent("thinking", text=str(delta.get("thinking") or ""))
                elif delta_type == "input_json_delta" and index in current:
                    current[index]["args"] += str(delta.get("partial_json") or "")
            elif kind == "content_block_stop":
                index = int(event.get("index", 0) or 0)
                slot = current.pop(index, None)
                if slot and slot.get("name"):
                    yield StreamEvent(
                        "tool_call",
                        tool_call=_finalize_tool_call(slot["id"], slot["name"], slot["args"]),
                    )
            elif kind == "message_start":
                message = event.get("message") or {}
                if message.get("usage"):
                    usage_payload.update(message["usage"])
                    yield StreamEvent("usage", usage=message["usage"])
            elif kind == "message_delta":
                delta = event.get("delta") or {}
                if delta.get("stop_reason"):
                    stop_reason = str(delta["stop_reason"])
                if event.get("usage"):
                    usage_payload.update(event["usage"])
                    yield StreamEvent("usage", usage=event["usage"])
            elif kind == "message_stop":
                break
        yield StreamEvent("done", stop_reason=stop_reason or "end_turn")


def _finalize_tool_call(call_id: str, name: str, raw_args: str) -> ToolCall:
    raw = raw_args or "{}"
    parsed = parse_tool_arguments(raw)
    if parsed is None:
        return ToolCall(
            id=call_id or f"call_{name}",
            name=name,
            arguments={},
            raw_arguments=raw,
            parse_error="arguments were not valid JSON",
        )
    return ToolCall(id=call_id or f"call_{name}", name=name, arguments=parsed, raw_arguments=raw)


def to_anthropic_messages(messages: List[Dict[str, Any]]) -> tuple[str, List[Dict[str, Any]]]:
    """Convert canonical (OpenAI-shaped) messages into Anthropic's block format."""
    system_parts: List[str] = []
    converted: List[Dict[str, Any]] = []

    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role == "system":
            if isinstance(content, str) and content:
                system_parts.append(content)
            continue
        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": message.get("tool_call_id") or "",
                "content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
            }
            converted.append({"role": "user", "content": [block]})
            continue
        if role == "assistant":
            blocks: List[Dict[str, Any]] = []
            if isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                raw_args = function.get("arguments") or "{}"
                parsed = parse_tool_arguments(raw_args) or {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id") or "",
                        "name": function.get("name") or "",
                        "input": parsed,
                    }
                )
            converted.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
            continue
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        converted.append({"role": "user", "content": [{"type": "text", "text": text}]})

    # Anthropic requires alternating roles; parallel tool results arrive as
    # consecutive user messages, so merge adjacent blocks of the same role.
    merged: List[Dict[str, Any]] = []
    for message in converted:
        if merged and merged[-1]["role"] == message["role"]:
            merged[-1]["content"].extend(message["content"])
        else:
            merged.append(message)
    return "\n\n".join(system_parts), merged


def resolve_provider(
    model: str = "",
    provider: str = "",
    base_url: str = "",
    api_key: Optional[str] = None,
) -> Provider:
    """Pick a provider from an explicit name, the model prefix, or the env."""
    selected = (provider or "").strip().lower()
    if not selected:
        configured = os.environ.get("TOKENSAFE_AGENT_PROVIDER", "").strip().lower()
        selected = configured if configured in PRESETS else _infer_provider(model)
    if selected not in PRESETS:
        raise ProviderError(
            f"unknown provider '{selected}'. Known: {', '.join(sorted(PRESETS))}"
        )
    config = PRESETS[selected]
    resolved_model = model or os.environ.get("TOKENSAFE_AGENT_MODEL", "") or config.default_model
    cls = AnthropicProvider if config.wire == "anthropic" else OpenAICompatibleProvider
    return cls(config, model=resolved_model, api_key=api_key, base_url=base_url or "")
