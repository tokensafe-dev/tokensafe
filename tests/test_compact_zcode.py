"""`tokensafe compact` must understand ZCode event-log transcripts.

Regression: ZCode writes `{"type": "...", "payload": {...}}` records, while the
compactor only read Claude/Antigravity shapes, so every real ZCode transcript was
reported as "not compactable".
"""

import json
import os
import tempfile
import unittest

from src.history_compactor import HistoryCompactor


def _write_zcode_transcript(path: str) -> None:
    records = [
        {"type": "turn_started", "payload": {"turnNumber": 1, "input": "Fix the login redirect bug in the auth flow"}},
        {"type": "model_complete", "payload": {"content": "I will inspect the auth handler and the redirect builder. " * 60}},
        {"type": "tool_call_scheduled", "payload": {"toolName": "read_file", "input": {"path": "src/auth/handler.ts"}}},
        {"type": "tool_call_scheduled", "payload": {"toolName": "run_command", "input": {"cmd": "npm test -- auth"}}},
        {"type": "model_complete", "payload": {"content": "The redirect lost the returnTo parameter; patching it now. " * 60}},
        {"type": "tool_call_scheduled", "payload": {"toolName": "edit_file", "input": {"path": "src/auth/handler.ts"}}},
    ]
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


class TestZCodeTranscriptCompaction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tokensafe_compact_")
        self.path = os.path.join(self.tmp, "transcript.jsonl")
        _write_zcode_transcript(self.path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_zcode_event_log_is_compactable(self):
        ledger, orig_tokens, folded_tokens, stats = HistoryCompactor.fold_transcript(self.path, invocation_num=3)

        self.assertIsNotNone(ledger, "ZCode transcript must produce a ledger")
        self.assertGreater(orig_tokens, 1_000)
        self.assertLess(folded_tokens, orig_tokens)
        # The ledger lists basenames, not full paths.
        self.assertIn("handler.ts", ledger)
        self.assertIn("Fix the login redirect bug", ledger)

    def test_unknown_format_stays_uncompactable(self):
        path = os.path.join(self.tmp, "empty.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "noise", "payload": {}}) + "\n")
        ledger, *_ = HistoryCompactor.fold_transcript(path, invocation_num=3)
        self.assertIsNone(ledger)


def _model_io_record(messages, text="", reasoning="", tool_calls=None, kind="delta"):
    return {
        "type": "model_io",
        "request": {"messages": messages, "messagesKind": kind, "messageCount": len(messages)},
        "response": {"text": text, "reasoningText": reasoning, "toolCalls": tool_calls or []},
    }


def _write_model_io_transcript(path: str) -> None:
    filler = "Inspecting the login redirect builder and its call sites. " * 40
    records = [
        _model_io_record(
            [{"role": "system", "content": "You are ZCode."},
             {"role": "user", "content": "Fix the login redirect bug in the auth flow"}],
            reasoning="I will locate the redirect builder first. " * 20,
            tool_calls=[{"name": "Read", "input": {"file_path": "src/auth/handler.ts"}}],
            kind="full",
        ),
        _model_io_record(
            [{"role": "assistant", "content": [{"type": "reasoning", "text": filler}]},
             {"role": "tool", "content": [{"type": "tool_result", "content": filler}]}]
        ),
        _model_io_record(
            [{"role": "assistant", "content": filler},
             {"role": "tool", "content": filler}],
            text="The redirect dropped the returnTo parameter; patching it now. " * 20,
            tool_calls=[{"name": "Write", "input": {"file_path": "src/auth/handler.ts"}}],
        ),
        _model_io_record(
            [{"role": "assistant", "content": filler},
             {"role": "tool", "content": filler}],
            tool_calls=[{"name": "Bash", "input": {"command": "npm test -- auth"}}],
        ),
    ]
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


class TestModelIOTranscriptCompaction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tokensafe_compact_io_")
        self.path = os.path.join(self.tmp, "model-io.jsonl")
        _write_model_io_transcript(self.path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_model_io_request_response_is_compactable(self):
        ledger, orig_tokens, folded_tokens, stats = HistoryCompactor.fold_transcript(self.path, invocation_num=3)

        self.assertIsNotNone(ledger, "model-io transcript must produce a ledger")
        self.assertGreater(orig_tokens, 1_000)
        self.assertLess(folded_tokens, orig_tokens)
        self.assertIn("handler.ts", ledger)
        # The newest user message of a turn is the active goal, not the tool output.
        self.assertIn("Fix the login redirect bug", ledger)
        self.assertIn("Executed npm", ledger)
        self.assertGreaterEqual(stats["executed_commands_count"], 1)

    def test_model_io_turn_boundary_is_a_user_step(self):
        ledger, *_ = HistoryCompactor.fold_transcript(self.path, invocation_num=3)
        self.assertIn("Active Goal: Fix the login redirect bug", ledger)


if __name__ == "__main__":
    unittest.main()
