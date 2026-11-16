import unittest
import tempfile
import json
import os
from src.history_compactor import HistoryCompactor

class TestHistoryCompactor(unittest.TestCase):
    def setUp(self):
        self.temp_file = tempfile.NamedTemporaryFile("w+", delete=False, suffix=".jsonl")
        # Write dummy multi-turn transcript steps
        for i in range(1, 10):
            step = {
                "step_index": i,
                "type": "PLANNER_RESPONSE",
                "content": f"Exploring step {i} with lots of context and repeated details " * 20,
                "tool_calls": [
                    {"name": "read_file", "args": {"Path": f"/app/src/service_{i % 3}.ts"}},
                    {"name": "run_command", "args": {"CommandLine": "npm test"}}
                ]
            }
            self.temp_file.write(json.dumps(step) + "\n")
        self.temp_file.flush()
        self.temp_file.close()

    def tearDown(self):
        if os.path.exists(self.temp_file.name):
            os.remove(self.temp_file.name)

    def test_fold_transcript(self):
        folded_text, orig_t, fold_t, stats = HistoryCompactor.fold_transcript(
            self.temp_file.name, invocation_num=5
        )
        self.assertIsNotNone(folded_text)
        self.assertIn("[TOCP State Ledger]:", folded_text)
        self.assertIn("service_0.ts", folded_text)
        self.assertIn("[Post-Compaction Orientation Digest]:", folded_text)
        self.assertIn("1. Active Goal:", folded_text)
        self.assertIn("2. Last State:", folded_text)
        self.assertIn("3. Next Focus:", folded_text)
        self.assertIn("orientation_digest", stats)
        self.assertGreater(stats["saved_tokens"], 500)
        self.assertLess(fold_t, orig_t)

    def test_orientation_digest_with_user_goal_and_edits(self):
        from src.history_compactor import _clean_goal_text
        raw_prompt = "<USER_REQUEST>\n1. Fix authentication retry bug in src/auth.ts\n</USER_REQUEST>"
        cleaned = _clean_goal_text(raw_prompt)
        self.assertEqual(cleaned, "Fix authentication retry bug in src/auth.ts")

        steps = [
            {"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>Fix authentication retry bug in src/auth.ts</USER_REQUEST>"},
            {"source": "MODEL", "type": "PLANNER_RESPONSE", "tool_calls": [
                {"name": "smart_read_file", "args": {"Path": "src/auth.ts", "symbol": "retryAuth"}}
            ]},
            {"source": "MODEL", "type": "PLANNER_RESPONSE", "tool_calls": [
                {"name": "edit_file", "args": {"Path": "src/auth.ts"}}
            ]},
        ]
        digest_text, digest_dict = HistoryCompactor.build_orientation_digest(
            steps, read_files={"auth.ts"}, edited_files={"auth.ts"}, executed_commands=[]
        )
        self.assertEqual(digest_dict["active_goal"], "Fix authentication retry bug in src/auth.ts")
        self.assertIn("Modified auth.ts", digest_dict["last_state"])
        self.assertIn("Verify modifications with tests", digest_dict["next_focus"])
        self.assertIn("1. Active Goal: Fix authentication retry bug in src/auth.ts", digest_text)
        self.assertIn("2. Last State:", digest_text)
        self.assertIn("3. Next Focus:", digest_text)

    def test_early_turns_skip(self):
        # Turns < 3 must not fold
        folded_text, orig_t, fold_t, stats = HistoryCompactor.fold_transcript(
            self.temp_file.name, invocation_num=2
        )
        self.assertIsNone(folded_text)

if __name__ == "__main__":
    unittest.main()
