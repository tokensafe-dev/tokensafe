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


if __name__ == "__main__":
    unittest.main()
