"""Agent kernel: streaming reassembly, tool dispatch, loop and session binding."""

import json
import os
import tempfile
import threading
import unittest

from src.agent.loop import AgentConfig, AgentLoop
from src.agent.providers import (
    AnthropicProvider,
    OpenAICompatibleProvider,
    PRESETS,
    StreamEvent,
    ToolCall,
    to_anthropic_messages,
)
from src.agent.tools import dispatch_tool, tool_schemas
from src.session_context import current_root, current_session_id


def _openai_lines(*chunks):
    lines = []
    for chunk in chunks:
        lines.append(f"data: {json.dumps(chunk)}")
        lines.append("")
    lines.append("data: [DONE]")
    return lines


def _anthropic_lines(*events):
    return [f"data: {json.dumps(event)}" for event in events]


class TestOpenAIStreaming(unittest.TestCase):
    def _provider(self, lines):
        provider = OpenAICompatibleProvider(
            PRESETS["deepseek"], model="deepseek-chat", api_key="test-key"
        )
        provider._open = lambda payload: iter(lines)
        return provider

    def test_text_deltas_and_usage(self):
        provider = self._provider(
            _openai_lines(
                {"choices": [{"delta": {"content": "Hel"}}]},
                {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 7,
                                          "prompt_cache_hit_tokens": 40}},
            )
        )
        result = provider.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(result["content"], "Hello")
        self.assertEqual(result["usage"].input_tokens, 100)
        self.assertEqual(result["usage"].cache_hit_tokens, 40)
        self.assertEqual(result["usage"].cache_miss_tokens, 60)
        self.assertTrue(result["usage"].as_dict("deepseek-chat")["priced"])

    def test_fragmented_tool_arguments_are_reassembled(self):
        provider = self._provider(
            _openai_lines(
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1",
                                                         "function": {"name": "scoped_search", "arguments": "{\"query\": \"USD_"}}]}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0,
                                                         "function": {"arguments": "PER_TOKEN\", \"scope\": \"code\"}"}}]},
                              "finish_reason": "tool_calls"},]},
            )
        )
        result = provider.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(len(result["tool_calls"]), 1)
        call = result["tool_calls"][0]
        self.assertEqual(call.id, "call_1")
        self.assertEqual(call.name, "scoped_search")
        self.assertEqual(call.arguments, {"query": "USD_PER_TOKEN", "scope": "code"})
        self.assertEqual(result["stop_reason"], "tool_calls")


class TestAnthropicStreaming(unittest.TestCase):
    def test_tool_use_block_is_reassembled(self):
        provider = AnthropicProvider(
            PRESETS["anthropic"], model="claude-test", api_key="test-key"
        )
        provider._open = lambda payload: iter(
            _anthropic_lines(
                {"type": "message_start", "message": {"usage": {"input_tokens": 55}}},
                {"type": "content_block_start", "index": 0,
                 "content_block": {"type": "tool_use", "id": "toolu_1", "name": "smart_read_file"}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "input_json_delta", "partial_json": "{\"path\": \"src/"}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "input_json_delta", "partial_json": "metrics.py\", \"symbol\": \"USD_PER_TOKEN\"}"}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
                 "usage": {"output_tokens": 21}},
                {"type": "message_stop"},
            )
        )
        result = provider.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(len(result["tool_calls"]), 1)
        self.assertEqual(result["tool_calls"][0].arguments,
                         {"path": "src/metrics.py", "symbol": "USD_PER_TOKEN"})
        self.assertEqual(result["usage"].input_tokens, 55)
        self.assertEqual(result["usage"].output_tokens, 21)
        self.assertEqual(result["stop_reason"], "tool_use")

    def test_message_conversion_merges_parallel_tool_results(self):
        system, messages = to_anthropic_messages(
            [
                {"role": "system", "content": "rules"},
                {"role": "user", "content": "go"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "a", "type": "function", "function": {"name": "x", "arguments": "{}"}},
                    {"id": "b", "type": "function", "function": {"name": "y", "arguments": "{}"}},
                ]},
                {"role": "tool", "tool_call_id": "a", "content": "A"},
                {"role": "tool", "tool_call_id": "b", "content": "B"},
            ]
        )
        self.assertEqual(system, "rules")
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user"])
        self.assertEqual(len(messages[2]["content"]), 2)
        self.assertEqual(messages[1]["content"][0]["type"], "tool_use")

    def test_payload_uses_input_schema(self):
        provider = AnthropicProvider(PRESETS["anthropic"], model="claude-test", api_key="k")
        payload = provider._payload(
            [{"role": "user", "content": "hi"}],
            [{"name": "t", "description": "d", "input_schema": {"type": "object"}}],
        )
        self.assertEqual(payload["tools"][0]["input_schema"], {"type": "object"})


class ScriptedProvider:
    model = "fake-model"

    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = []

    def stream(self, messages, tools=None, cancel=None):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        events = self.turns.pop(0) if self.turns else [StreamEvent("done", stop_reason="end_turn")]
        for event in events:
            yield event


class TestAgentLoop(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        with open(os.path.join(self.root, "needle.py"), "w", encoding="utf-8") as f:
            f.write("def find_me():\n    return 'NEEDLE_42'\n")

    def test_text_only_turn(self):
        provider = ScriptedProvider([[
            StreamEvent("text", text="done"),
            StreamEvent("done", stop_reason="end_turn"),
        ]])
        loop = AgentLoop(root=self.root, session_id="t:1", provider=provider)
        result = loop.run("hello")
        self.assertEqual(result.text, "done")
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertEqual(result.steps, 1)
        self.assertEqual([m["role"] for m in result.history], ["user", "assistant"])

    def test_tool_call_dispatches_inside_session_scope(self):
        events = []
        provider = ScriptedProvider([
            [StreamEvent("tool_call", tool_call=ToolCall("c1", "scoped_search", {"query": "NEEDLE_42"})),
             StreamEvent("done", stop_reason="tool_calls")],
            [StreamEvent("text", text="found"), StreamEvent("done", stop_reason="end_turn")],
        ])
        loop = AgentLoop(root=self.root, session_id="t:2", provider=provider, event_sink=events.append)
        result = loop.run("find the needle")

        tool_messages = [m for m in result.history if m.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertIn("needle.py", tool_messages[0]["content"])
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertEqual(result.steps, 2)

        # The tool ran under the loop's session, not the process global marker.
        self.assertEqual(current_root(), None)
        self.assertEqual(current_session_id("x"), "x")
        self.assertTrue(any(e["type"] == "tool_call" for e in events))
        self.assertTrue(any(e["type"] == "tool_result" and e["ok"] for e in events))

    def test_second_call_sees_tool_result(self):
        provider = ScriptedProvider([
            [StreamEvent("tool_call", tool_call=ToolCall("c1", "scoped_search", {"query": "NEEDLE_42"})),
             StreamEvent("done", stop_reason="tool_calls")],
            [StreamEvent("text", text="ok"), StreamEvent("done", stop_reason="end_turn")],
        ])
        loop = AgentLoop(root=self.root, session_id="t:3", provider=provider)
        loop.run("find it")
        second_messages = provider.calls[1]["messages"]
        roles = [m["role"] for m in second_messages]
        self.assertEqual(roles[0], "system")
        self.assertIn("tool", roles)
        self.assertIn("needle.py", json.dumps(second_messages))

    def test_cancel_before_start(self):
        provider = ScriptedProvider([])
        loop = AgentLoop(root=self.root, session_id="t:4", provider=provider)
        cancel = threading.Event()
        cancel.set()
        result = loop.run("hello", cancel=cancel)
        self.assertEqual(result.stop_reason, "cancelled")
        self.assertEqual(provider.calls, [])

    def test_invalid_arguments_never_reach_the_tool(self):
        provider = ScriptedProvider([
            [StreamEvent("tool_call", tool_call=ToolCall("c1", "scoped_search", {}, raw_arguments="{oops",
                                                         parse_error="arguments were not valid JSON")),
             StreamEvent("done", stop_reason="tool_calls")],
            [StreamEvent("text", text="recovered"), StreamEvent("done", stop_reason="end_turn")],
        ])
        loop = AgentLoop(root=self.root, session_id="t:5", provider=provider)
        result = loop.run("go")
        tool_message = [m for m in result.history if m.get("role") == "tool"][0]
        self.assertIn("rejected", tool_message["content"])
        self.assertEqual(result.stop_reason, "end_turn")

    def test_step_cap(self):
        provider = ScriptedProvider([
            [StreamEvent("tool_call", tool_call=ToolCall(f"c{i}", "repo_map", {"view": "tree"})),
             StreamEvent("done", stop_reason="tool_calls")]
            for i in range(3)
        ])
        loop = AgentLoop(
            root=self.root, session_id="t:6", provider=provider,
            config=AgentConfig(max_steps=3),
        )
        result = loop.run("loop forever")
        self.assertEqual(result.stop_reason, "max_turn_requests")
        self.assertEqual(result.steps, 3)

    def test_orphan_tool_messages_are_scrubbed_before_the_call(self):
        provider = ScriptedProvider([[StreamEvent("text", text="ok"),
                                      StreamEvent("done", stop_reason="end_turn")]])
        loop = AgentLoop(root=self.root, session_id="t:7", provider=provider)
        loop.history = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "kept", "type": "function", "function": {"name": "x", "arguments": "{}"}},
                {"id": "lost", "type": "function", "function": {"name": "y", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "kept", "content": "A"},
            {"role": "tool", "tool_call_id": "orphan", "content": "B"},
        ]
        loop.run("continue")
        sent = provider.calls[0]["messages"]
        assistant = [m for m in sent if m.get("role") == "assistant"][0]
        self.assertEqual([call["id"] for call in assistant["tool_calls"]], ["kept"])
        tool_ids = [m.get("tool_call_id") for m in sent if m.get("role") == "tool"]
        self.assertEqual(tool_ids, ["kept"])


class TestToolSurface(unittest.TestCase):
    def test_schemas_come_from_the_mcp_registry(self):
        schemas = tool_schemas()
        names = [s["name"] for s in schemas]
        self.assertIn("smart_read_file", names)
        self.assertIn("scoped_search", names)
        for schema in schemas:
            self.assertIn("input_schema", schema)

    def test_dispatch_reports_errors_without_raising(self):
        result = dispatch_tool("smart_read_file", {"path": "/definitely/not/here.py"})
        self.assertFalse(result.ok)
        self.assertIn("not found", result.text.lower())


if __name__ == "__main__":
    unittest.main()
