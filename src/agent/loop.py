"""The agent turn loop.

One loop, one registry, one context policy. It owns the turn that hooks used to
observe from the outside: call the model, execute tools through the canonical
MCP handler, feed results back, and govern the context between steps.

Deliberate choices:

* ``max_steps`` and ``budget_usd`` are hard caps, not suggestions — an agent
  that can loop forever is a billing incident.
* Tool dispatch happens inside ``session_scope``, so every metric, pointer
  cache entry and relative-path resolution in the tools belongs to *this*
  session, never to a neighbouring one in the same process.
* The user prompt is not rewritten by default. Prompt compaction pays off when
  a harness re-sends the prompt every turn (the hook case); in a loop the
  prompt is sent once, so rewriting it only risks losing the user's words. The
  real bloat is tool output and history, and that is what gets governed.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.agent import context as kernel
from src.agent.providers import Provider, ProviderError, ToolCall, Usage, resolve_provider
from src.agent.tools import DEFAULT_TOOL_NAMES, ToolResult, dispatch_tool, tool_kind, tool_schemas
from src.session_context import current_root, session_scope

EventSink = Callable[[Dict[str, Any]], None]

DEFAULT_SYSTEM_PROMPT = """You are TokenSafe Agent, a coding agent that works inside one workspace.

Non-negotiable working rules:
- Never read whole files to find things. Use scoped_search (text/regex/glob/fuzzy) first, then smart_read_file with symbol= for the exact definition.
- Prefer one surgical call over five broad ones: smart_read_file accepts targets=[{path,symbol}] to bundle 2-8 symbols, repo_map gives the architecture (graph), exports (api) and tree.
- Edit with apply_edits (exact string targets) and cite locations as path:line. Do not print whole files.
- Run noisy commands (tests, builds) through run_helper(action="sanitize", command=...) so passing suites stay one line.
- If a read returns a pointer to content you no longer have, re-read that symbol with force_full=true once; do not guess.
- Stop when the task is done and state what changed. Do not keep exploring to look thorough.
"""


@dataclass(frozen=True)
class AgentConfig:
    model: str = ""
    provider: str = ""
    base_url: str = ""
    max_steps: int = 16
    budget_usd: float = 1.0
    max_context_tokens: Optional[int] = None
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    tool_names: Sequence[str] = DEFAULT_TOOL_NAMES
    compact_prompt_intake: bool = False
    max_tool_result_chars: int = 24000


@dataclass
class RunResult:
    text: str
    stop_reason: str
    steps: int
    usage: Dict[str, Any]
    compactions: List[Dict[str, Any]] = field(default_factory=list)
    history: List[Dict[str, Any]] = field(default_factory=list)
    tool_results: List[Dict[str, Any]] = field(default_factory=list)


class AgentLoop:
    def __init__(
        self,
        root: Optional[str] = None,
        session_id: str = "agent",
        provider: Optional[Provider] = None,
        config: Optional[AgentConfig] = None,
        event_sink: Optional[EventSink] = None,
    ):
        self.config = config or AgentConfig()
        self.root = os.path.realpath(root or current_root() or os.getcwd())
        self.session_id = session_id or "agent"
        self.provider = provider
        self.event_sink = event_sink
        self.history: List[Dict[str, Any]] = []
        self.usage = Usage()
        self._schemas: Optional[List[Dict[str, Any]]] = None

    # -- plumbing ----------------------------------------------------------

    @property
    def model(self) -> str:
        if self.provider is not None:
            return self.provider.model
        return self.config.model or os.environ.get("TOKENSAFE_AGENT_MODEL", "")

    def tool_schemas(self) -> List[Dict[str, Any]]:
        if self._schemas is None:
            self._schemas = tool_schemas(self.config.tool_names)
        return self._schemas

    def num_tools(self) -> int:
        return len(self.tool_schemas())

    def _emit(self, event_type: str, **payload: Any) -> None:
        if not self.event_sink:
            return
        try:
            self.event_sink({"type": event_type, **payload})
        except Exception:
            # A broken UI must never break the agent turn.
            pass

    def _ensure_provider(self) -> Provider:
        if self.provider is None:
            self.provider = resolve_provider(
                model=self.config.model,
                provider=self.config.provider,
                base_url=self.config.base_url,
            )
        return self.provider

    def reset(self) -> None:
        self.history = []
        self.usage = Usage()

    # -- context -----------------------------------------------------------

    def _prepare_messages(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        system = {"role": "system", "content": self.config.system_prompt}
        messages = [system] + self.history
        window = kernel.resolve_window(self.model, self.config.max_context_tokens)
        target = int(window * 0.75)
        stats: Dict[str, Any] = {}
        if kernel.messages_tokens(messages) > target:
            governed, stats = kernel.govern_messages(
                messages,
                session_id=self.session_id,
                model=self.model,
                max_tokens=window,
                target_tokens=target,
            )
            if stats.get("saved_tokens"):
                if governed and governed[0].get("role") == "system":
                    messages = governed
                else:
                    messages = [system] + governed
                self.history = messages[1:]
            else:
                stats = {}
        # Compaction (or a resumed history) can orphan a tool result from the
        # assistant call that declared it; both wire formats reject that with a
        # 400, so pair them up before every call.
        self.history = scrub_tool_pairing(self.history)
        messages = [system] + self.history
        return messages, stats

    # -- one provider call -------------------------------------------------

    def _call_provider(
        self,
        messages: List[Dict[str, Any]],
        cancel: Optional[threading.Event],
    ) -> Tuple[str, List[ToolCall], str]:
        provider = self._ensure_provider()
        text_parts: List[str] = []
        tool_calls: List[ToolCall] = []
        stop_reason = ""
        before = (self.usage.input_tokens, self.usage.output_tokens)
        self.usage.calls += 1

        for event in provider.stream(messages, tools=self.tool_schemas(), cancel=cancel):
            if event.type == "text":
                text_parts.append(event.text)
                self._emit("text", text=event.text)
            elif event.type == "thinking":
                self._emit("thinking", text=event.text)
            elif event.type == "tool_call" and event.tool_call:
                tool_calls.append(event.tool_call)
                self._emit(
                    "tool_call",
                    id=event.tool_call.id,
                    name=event.tool_call.name,
                    kind=tool_kind(event.tool_call.name),
                    arguments=_bounded_json(event.tool_call.arguments, 2000),
                )
            elif event.type == "usage":
                self.usage.add(event.usage)
                if (self.usage.input_tokens, self.usage.output_tokens) != before:
                    self._emit("usage", **self.usage.as_dict(self.model))
                    before = (self.usage.input_tokens, self.usage.output_tokens)
            elif event.type == "done":
                stop_reason = event.stop_reason or stop_reason
        return "".join(text_parts), tool_calls, stop_reason

    # -- turn --------------------------------------------------------------

    def run(self, prompt: str, cancel: Optional[threading.Event] = None) -> RunResult:
        """Run one user turn. Binds the session context for its whole duration."""
        with session_scope(root=self.root, session_id=self.session_id, agent="tokensafe-agent"):
            return self._run(prompt, cancel)

    def _run(self, prompt: str, cancel: Optional[threading.Event]) -> RunResult:
        prompt = (prompt or "").strip()
        if not prompt:
            return self._empty("empty_prompt")
        if self.config.compact_prompt_intake:
            prompt = self._compact_intake(prompt)

        self.history.append({"role": "user", "content": prompt})
        narration: List[str] = []
        compactions: List[Dict[str, Any]] = []
        tool_results: List[Dict[str, Any]] = []
        stop_reason = "end_turn"
        steps = 0

        for step in range(max(1, self.config.max_steps)):
            if cancel is not None and cancel.is_set():
                stop_reason = "cancelled"
                break
            budget = self.usage.as_dict(self.model)
            if budget.get("priced") and float(budget.get("usd") or 0.0) >= self.config.budget_usd:
                stop_reason = "budget_exceeded"
                break

            steps = step + 1
            messages, compaction = self._prepare_messages()
            if compaction:
                compactions.append(compaction)
                self._emit(
                    "compaction",
                    before=compaction.get("initial_tokens", 0),
                    after=compaction.get("current_tokens", 0),
                    actions=compaction.get("actions", []),
                )

            text, calls, provider_stop = self._call_provider(messages, cancel)
            if text:
                narration.append(text)

            assistant: Dict[str, Any] = {"role": "assistant", "content": text}
            if calls:
                assistant["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.raw_arguments or json.dumps(call.arguments)},
                    }
                    for call in calls
                ]
            self.history.append(assistant)

            if not calls:
                stop_reason = provider_stop or "end_turn"
                break
            if cancel is not None and cancel.is_set():
                stop_reason = "cancelled"
                break

            for call in calls:
                result = self._execute(call)
                self.history.append(
                    {"role": "tool", "tool_call_id": call.id, "content": result.text}
                )
                record = {
                    "id": call.id,
                    "name": call.name,
                    "ok": result.ok,
                    "truncated": result.truncated,
                    "chars": len(result.text or ""),
                }
                tool_results.append(record)
                self._emit(
                    "tool_result",
                    id=call.id,
                    name=call.name,
                    ok=result.ok,
                    text=(result.text or "")[:8000],
                    truncated=result.truncated,
                )
        else:
            stop_reason = "max_turn_requests"

        result = RunResult(
            text="\n\n".join(narration).strip(),
            stop_reason=stop_reason,
            steps=steps,
            usage=self.usage.as_dict(self.model),
            compactions=compactions,
            history=list(self.history),
            tool_results=tool_results,
        )
        self._emit("done", stop_reason=stop_reason, steps=steps, usage=result.usage)
        return result

    def _execute(self, call: ToolCall) -> ToolResult:
        if call.parse_error:
            return ToolResult(
                name=call.name,
                ok=False,
                text=(
                    f"Tool call rejected: {call.parse_error}. Re-issue the call with valid JSON "
                    f"arguments for '{call.name}'."
                ),
            )
        return dispatch_tool(call.name, call.arguments)

    def _compact_intake(self, prompt: str) -> str:
        """Optional hook-parity prompt rewrite, off by default (see module docstring)."""
        if len(prompt) < 80:
            return prompt
        try:
            compaction = kernel.compact_prompt(prompt)
        except Exception:
            return prompt
        if compaction.get("saved_tokens", 0) >= 40 and compaction.get("reduction_pct", 0) >= 20:
            self._emit(
                "compaction",
                before=compaction.get("original_tokens", 0),
                after=compaction.get("compact_tokens", 0),
                actions=["compact_prompt"],
            )
            return f"{prompt}\n\n[TOCP intent of the request above]: {compaction['compact_text']}"
        return prompt

    def _empty(self, reason: str) -> RunResult:
        return RunResult(
            text="",
            stop_reason=reason,
            steps=0,
            usage=self.usage.as_dict(self.model),
            history=list(self.history),
        )


def _bounded_json(value: Any, limit: int) -> Any:
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(value)[:limit]
    if len(rendered) <= limit:
        return value
    return rendered[:limit] + "…"


def scrub_tool_pairing(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop tool results nothing declared, and declarations nothing answered.

    Both keep the request valid for OpenAI-compatible and Anthropic endpoints
    after an aggressive compaction pass: a ``tool`` message must answer a
    ``tool_calls`` entry that precedes it, and every declared call must have a
    result. Orphans are removed rather than filled with fake output.
    """
    answered = {
        str(message.get("tool_call_id"))
        for message in history
        if message.get("role") == "tool" and message.get("tool_call_id")
    }
    declared = {
        str(call.get("id"))
        for message in history
        if message.get("role") == "assistant"
        for call in (message.get("tool_calls") or [])
        if call.get("id")
    }
    keep_calls = answered & declared
    scrubbed: List[Dict[str, Any]] = []
    for message in history:
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            calls = [call for call in message["tool_calls"] if str(call.get("id")) in keep_calls]
            if not calls:
                if message.get("content"):
                    scrubbed.append({"role": "assistant", "content": message["content"]})
                continue
            patched = dict(message)
            patched["tool_calls"] = calls
            scrubbed.append(patched)
        elif role == "tool":
            if str(message.get("tool_call_id")) in keep_calls:
                scrubbed.append(message)
        else:
            scrubbed.append(message)
    return scrubbed
