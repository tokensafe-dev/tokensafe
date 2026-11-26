"""ACP server: handshake, session lifecycle, streaming updates, cancellation."""

import io
import json
import os
import tempfile
import threading
import time
import unittest

from src.acp.server import AcpServer
from src.agent.providers import StreamEvent, ToolCall


class ScriptedProvider:
    model = "fake-model"

    def __init__(self, turns):
        self.turns = list(turns)

    def stream(self, messages, tools=None, cancel=None):
        events = self.turns.pop(0) if self.turns else [StreamEvent("done", stop_reason="end_turn")]
        for event in events:
            yield event


class SlowProvider:
    model = "slow-model"

    def __init__(self):
        self.release = threading.Event()

    def stream(self, messages, tools=None, cancel=None):
        yield StreamEvent("text", text="working…")
        deadline = time.time() + 5
        while time.time() < deadline:
            if (cancel is not None and cancel.is_set()) or self.release.is_set():
                break
            time.sleep(0.01)
        yield StreamEvent("done", stop_reason="cancelled" if cancel and cancel.is_set() else "end_turn")


class AcpHarness:
    def __init__(self, provider):
        self.out = io.StringIO()
        self.server = AcpServer(
            reader=io.StringIO(""),
            writer=self.out,
            provider_factory=lambda root, sid: provider,
        )
        self._id = 0

    def send(self, method, params=None, notify=False):
        message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self._id += 1
            message["id"] = self._id
        self.server.handle(message)
        return message.get("id")

    def messages(self):
        return [json.loads(line) for line in self.out.getvalue().splitlines() if line.strip()]

    def response(self, request_id):
        for message in self.messages():
            if message.get("id") == request_id and ("result" in message or "error" in message):
                return message
        return None


class TestAcpServer(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        with open(os.path.join(self.root, "app.py"), "w", encoding="utf-8") as f:
            f.write("def target():\n    return 'NEEDLE_ACP'\n")

    def test_initialize_advertises_capabilities(self):
        harness = AcpHarness(ScriptedProvider([]))
        request_id = harness.send("initialize", {"protocolVersion": 1, "clientInfo": {"name": "zed"}})
        response = harness.response(request_id)
        self.assertEqual(response["result"]["protocolVersion"], 1)
        self.assertEqual(response["result"]["agentInfo"]["name"], "tokensafe-agent")
        self.assertTrue(response["result"]["agentCapabilities"]["promptCapabilities"]["embeddedContext"])

    def test_prompt_streams_text_and_returns_stop_reason(self):
        provider = ScriptedProvider([[
            StreamEvent("text", text="hello"),
            StreamEvent("text", text=" world"),
            StreamEvent("done", stop_reason="end_turn"),
        ]])
        harness = AcpHarness(provider)
        session_id = harness.response(harness.send("session/new", {"cwd": self.root}))["result"]["sessionId"]
        request_id = harness.send("session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": "say hi"}],
        })
        harness.server.wait_idle(5)
        self.assertEqual(harness.response(request_id)["result"]["stopReason"], "end_turn")
        chunks = [
            message["params"]["update"]["content"]["text"]
            for message in harness.messages()
            if message.get("method") == "session/update"
            and message["params"]["update"]["sessionUpdate"] == "agent_message_chunk"
        ]
        self.assertEqual("".join(chunks), "hello world")

    def test_tool_call_is_visible_to_the_editor(self):
        provider = ScriptedProvider([
            [StreamEvent("tool_call", tool_call=ToolCall("c1", "scoped_search", {"query": "NEEDLE_ACP"})),
             StreamEvent("done", stop_reason="tool_calls")],
            [StreamEvent("text", text="found it"), StreamEvent("done", stop_reason="end_turn")],
        ])
        harness = AcpHarness(provider)
        session_id = harness.response(harness.send("session/new", {"cwd": self.root}))["result"]["sessionId"]
        request_id = harness.send("session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": "find NEEDLE_ACP"}],
        })
        harness.server.wait_idle(10)
        self.assertEqual(harness.response(request_id)["result"]["stopReason"], "end_turn")
        updates = [m["params"]["update"] for m in harness.messages() if m.get("method") == "session/update"]
        tool_calls = [u for u in updates if u["sessionUpdate"] == "tool_call"]
        tool_updates = [u for u in updates if u["sessionUpdate"] == "tool_call_update"]
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["kind"], "search")
        self.assertEqual(len(tool_updates), 1)
        self.assertEqual(tool_updates[0]["status"], "completed")

    def test_cancel_notification_stops_the_turn(self):
        provider = SlowProvider()
        harness = AcpHarness(provider)
        session_id = harness.response(harness.send("session/new", {"cwd": self.root}))["result"]["sessionId"]
        request_id = harness.send("session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": "long task"}],
        })
        time.sleep(0.05)
        harness.send("session/cancel", {"sessionId": session_id}, notify=True)
        harness.server.wait_idle(5)
        self.assertEqual(harness.response(request_id)["result"]["stopReason"], "cancelled")

    def test_unknown_session_is_rejected(self):
        harness = AcpHarness(ScriptedProvider([]))
        request_id = harness.send("session/prompt", {"sessionId": "nope", "prompt": [{"type": "text", "text": "x"}]})
        self.assertEqual(harness.response(request_id)["error"]["code"], -32602)

    def test_image_block_is_reported_not_crashed(self):
        provider = ScriptedProvider([[StreamEvent("done", stop_reason="end_turn")]])
        harness = AcpHarness(provider)
        session_id = harness.response(harness.send("session/new", {"cwd": self.root}))["result"]["sessionId"]
        request_id = harness.send("session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "image", "data": "…", "mimeType": "image/png"}],
        })
        harness.server.wait_idle(5)
        self.assertEqual(harness.response(request_id)["result"]["stopReason"], "end_turn")
        chunks = [
            m["params"]["update"]["content"]["text"]
            for m in harness.messages()
            if m.get("method") == "session/update"
        ]
        self.assertTrue(any("not supported" in chunk for chunk in chunks))

    def test_unknown_method_is_an_error(self):
        harness = AcpHarness(ScriptedProvider([]))
        request_id = harness.send("session/load", {"sessionId": "x"})
        self.assertEqual(harness.response(request_id)["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
