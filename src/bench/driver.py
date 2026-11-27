"""Minimal, measured agent loop for OUMV benchmarks.

Deliberately small: one OpenAI-compatible endpoint (DeepSeek by default), explicit
tool schemas, a hard step cap and a hard USD budget. Every token number comes from
the provider's own `usage` payload — nothing is estimated from bytes, and no
fixture traces are involved.

Prices are a pinned snapshot; the report prints them so a reader can recompute the
cost. Update the table when the provider changes prices (and bump `PRICE_SNAPSHOT`).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

PRICE_SNAPSHOT = "2026-11-26"
# USD per 1M tokens: cache-miss input, cache-hit input, output.
PRICES: Dict[str, Dict[str, float]] = {
    "deepseek-chat": {"input_miss": 0.27, "input_hit": 0.07, "output": 1.10},
    "deepseek-flash": {"input_miss": 0.27, "input_hit": 0.07, "output": 1.10},
}
DEFAULT_BASE_URL = "https://api.deepseek.com"


def resolve_api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    path = os.path.expanduser("~/.tokensafe/data/config.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return str(json.load(f).get("deepseek_api_key") or "").strip()
    except Exception:
        return ""


@dataclass
class Usage:
    input_tokens: int = 0
    cache_hit_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    def add(self, payload: Dict[str, Any]) -> None:
        self.calls += 1
        self.input_tokens += int(payload.get("prompt_tokens") or 0)
        self.cache_hit_tokens += int(payload.get("prompt_cache_hit_tokens") or 0)
        self.output_tokens += int(payload.get("completion_tokens") or 0)

    @property
    def cache_miss_tokens(self) -> int:
        return max(self.input_tokens - self.cache_hit_tokens, 0)

    def usd(self, model: str) -> float:
        price = PRICES.get(model, PRICES["deepseek-chat"])
        return round(
            (self.cache_miss_tokens / 1_000_000) * price["input_miss"]
            + (self.cache_hit_tokens / 1_000_000) * price["input_hit"]
            + (self.output_tokens / 1_000_000) * price["output"],
            6,
        )

    def as_dict(self, model: str) -> Dict[str, Any]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "output_tokens": self.output_tokens,
            "usd": self.usd(model),
        }


class BudgetExceeded(RuntimeError):
    def __init__(self, message: str, usage: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.usage = usage or {}


class DeepSeekClient:
    _RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})

    def __init__(
        self,
        model: str = "deepseek-chat",
        temperature: float = 0.0,
        base_url: Optional[str] = None,
        timeout: float = 300.0,
        retries: int = 4,
    ):
        self.model = model
        self.temperature = temperature
        self.base_url = (base_url or os.environ.get("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.retries = max(1, int(retries))
        self.api_key = resolve_api_key()
        if not self.api_key:
            raise RuntimeError(
                "no DeepSeek API key: set DEEPSEEK_API_KEY or write deepseek_api_key "
                "into ~/.tokensafe/data/config.json"
            )

    @staticmethod
    def _backoff(attempt: int) -> float:
        # 5s, 15s, 30s: a paid matrix must survive a flaky read, not restart.
        return min(30.0, 5.0 * (3 ** (attempt - 1)))

    def chat(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        data = json.dumps(body).encode()
        last_error = ""
        for attempt in range(1, self.retries + 1):
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions",
                data=data,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode())
                return payload["choices"][0]["message"], payload.get("usage") or {}
            except urllib.error.HTTPError as exc:
                last_error = f"provider HTTP {exc.code}: {exc.read().decode()[:300]}"
                if exc.code not in self._RETRY_STATUS or attempt == self.retries:
                    raise RuntimeError(last_error) from exc
            except (TimeoutError, urllib.error.URLError, ConnectionResetError, json.JSONDecodeError) as exc:
                # A long generation can outlive the socket read, and a truncated body is
                # not valid JSON. Both are transient and must not kill a paid matrix
                # mid-run: the first v5 attempt died after ~$0.14 with 45 sessions
                # collected and nothing written.
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == self.retries:
                    raise RuntimeError(
                        f"provider call failed after {self.retries} attempts — {last_error}"
                    ) from exc
            time.sleep(self._backoff(attempt))
        raise RuntimeError(f"provider call failed — {last_error}")


ToolFn = Callable[..., Any]


@dataclass
class StepRecord:
    index: int
    tool_calls: List[str] = field(default_factory=list)
    result_chars: List[int] = field(default_factory=list)
    duration_ms: float = 0.0
    oracle_passed: Optional[int] = None
    oracle_failed: List[str] = field(default_factory=list)
    note: str = ""


def run_agent(
    client: DeepSeekClient,
    system_prompt: str,
    task_prompt: str,
    tools: List[Dict[str, Any]],
    registry: Dict[str, ToolFn],
    max_steps: int = 14,
    budget_usd: float = 1.0,
    after_step: Optional[Callable[[int], Tuple[Optional[int], List[str], str]]] = None,
    stop_when_solved: bool = True,
) -> Dict[str, Any]:
    """Runs one tool loop. Returns messages-independent accounting + step log."""
    usage = Usage()
    steps: List[StepRecord] = []
    tool_counts: Dict[str, int] = {}
    tool_chars: Dict[str, int] = {}
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task_prompt},
    ]
    stop_reason = "step_limit"

    for index in range(1, max_steps + 1):
        message, raw_usage = client.chat(messages, tools)
        usage.add(raw_usage)

        calls = message.get("tool_calls") or []
        messages.append(message)

        record = StepRecord(index=index, tool_calls=[c["function"]["name"] for c in calls])
        if not calls:
            stop_reason = "agent_finished"
            steps.append(record)
            break

        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            fn = registry.get(name)
            try:
                result = fn(**args) if fn else {"error": f"unknown tool {name}"}
            except Exception as exc:  # tool errors are observations, not crashes
                result = {"error": f"{type(exc).__name__}: {exc}"}
            content = json.dumps(result, ensure_ascii=False)[:6000]
            tool_counts[name] = tool_counts.get(name, 0) + 1
            tool_chars[name] = tool_chars.get(name, 0) + len(content)
            record.result_chars.append(len(content))
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "content": content,
            })

        if after_step:
            passed, failed, note = after_step(index)
            record.oracle_passed, record.oracle_failed, record.note = passed, failed, note
        steps.append(record)

        if stop_when_solved and record.oracle_passed == 12:
            stop_reason = "solved"
            break

        if usage.usd(client.model) > budget_usd:
            raise BudgetExceeded(
                f"budget ${budget_usd} exceeded at step {index} (${usage.usd(client.model)})",
                usage.as_dict(client.model),
            )
    else:
        stop_reason = "max_steps"

    return {
        "model": client.model,
        "temperature": client.temperature,
        "price_snapshot": PRICE_SNAPSHOT,
        "prices_usd_per_mtok": PRICES.get(client.model),
        "stop_reason": stop_reason,
        "usage": usage.as_dict(client.model),
        "tool_calls": tool_counts,
        "tool_result_chars": tool_chars,
        "steps": [
            {
                "index": s.index,
                "tools": s.tool_calls,
                "result_chars": s.result_chars,
                "oracle_passed": s.oracle_passed,
                "oracle_failed": s.oracle_failed,
                "note": s.note,
            }
            for s in steps
        ],
    }
