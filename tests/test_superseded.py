"""
Unit tests for TokenSafe Superseded Tool Output Pruning.
"""

import unittest
import os
import shutil
import tempfile
import json
from src.superseded import SupersededPruner
from src.stash import retrieve_block


class TestSupersededPruner(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="tokensafe_superseded_")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_prune_superseded_reads(self):
        raw_big_code = "export const config = { enabled: true };\n" * 40  # ~1600 chars

        steps = [
            {
                "step_index": 1,
                "tool_calls": [{"name": "read_file", "args": {"Path": "/app/src/config.ts"}}],
                "content": raw_big_code
            },
            {
                "step_index": 2,
                "tool_calls": [{"name": "edit_file", "args": {"Path": "/app/src/config.ts"}}],
                "content": "Modified config.ts"
            },
            {
                "step_index": 3,
                "tool_calls": [{"name": "read_file", "args": {"Path": "/app/src/config.ts"}}],
                "content": raw_big_code
            }
        ]

        pruned, orig_t, pruned_t, stats = SupersededPruner.prune_steps(steps)

        self.assertEqual(stats["pruned_count"], 1)
        self.assertGreater(stats["saved_tokens"], 100)

        # Step 1 should have been tombstoned
        self.assertIn("[TokenSafe: Output superseded by step #2", pruned[0]["content"])
        self.assertIn("TokenSafe folded: #", pruned[0]["content"])

        # Step 3 should remain untombstoned
        self.assertEqual(pruned[2]["content"], raw_big_code)

    def test_prune_transcript_file(self):
        t_path = os.path.join(self.tmp_dir, "transcript.jsonl")
        raw_big_code = "function test() { return 42; }\n" * 50

        steps = [
            {"tool_calls": [{"name": "read_file", "args": {"Path": "/app/test.js"}}], "content": raw_big_code},
            {"tool_calls": [{"name": "edit_file", "args": {"Path": "/app/test.js"}}], "content": "edited"},
            {"tool_calls": [{"name": "read_file", "args": {"Path": "/app/test.js"}}], "content": "function test() { return 100; }\n"}
        ]

        with open(t_path, "w", encoding="utf-8") as f:
            for s in steps:
                f.write(json.dumps(s) + "\n")

        res = SupersededPruner.prune_transcript(t_path)
        self.assertTrue(res["success"])
        self.assertEqual(res["pruned_count"], 1)
        self.assertGreater(res["saved_tokens"], 100)

        with open(t_path, "r", encoding="utf-8") as f:
            lines = [json.loads(ln.strip()) for ln in f if ln.strip()]

        self.assertIn("superseded", lines[0]["content"])


if __name__ == "__main__":
    unittest.main()
