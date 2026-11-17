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

    def test_claude_code_trailing_tool_result_pruned(self):
        """CC transcripts: the read's tool_use and its tool_result live in
        DIFFERENT steps — the tombstone must land on the result step."""
        big = "export const legacy = {};\n" * 40  # ~1000 chars
        steps = [
            {"message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "/app/a.ts"}}]}},
            {"message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": big}]}},
            {"message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_2", "name": "Edit", "input": {"file_path": "/app/a.ts"}}]}},
            {"message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_2", "content": "ok"}]}},
        ]
        pruned, orig_t, new_t, stats = SupersededPruner.prune_steps(steps)
        self.assertEqual(stats["pruned_count"], 1, stats)
        block = pruned[1]["message"]["content"][0]
        self.assertIn("superseded by step #3", block["content"])
        self.assertIn("TokenSafe folded: #", block["content"])
        self.assertIn("edit a.ts", block["content"])
        # The edit's own result must stay untouched
        self.assertEqual(pruned[3]["message"]["content"][0]["content"], "ok")
        # Reversibility: the stashed original is retrievable
        import re as _re
        m = _re.search(r"#([0-9a-f]{8})", block["content"])
        self.assertIsNotNone(m)
        self.assertEqual(retrieve_block(m.group(1)), big)

    def test_claude_code_list_content_tool_result_pruned(self):
        """tool_result.content can be a list of text blocks — tombstone it too."""
        big_text = "def old_impl():\n    pass\n" * 45
        steps = [
            {"message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/x/f.py"}}]}},
            {"message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": big_text}]}]}},
            {"message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t2", "name": "Write", "input": {"file_path": "/x/f.py"}}]}},
        ]
        pruned, _, _, stats = SupersededPruner.prune_steps(steps)
        self.assertEqual(stats["pruned_count"], 1, stats)
        block = pruned[1]["message"]["content"][0]
        self.assertIsInstance(block["content"], str)
        self.assertIn("superseded by step #3", block["content"])

    def test_unrelated_results_stay_untouched(self):
        """A superseded READ must not tombstone results of OTHER tool_use ids."""
        steps = [
            {"message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "ls"}},
                {"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": "/app/a.ts"}}]}},
            {"message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "b1", "content": "file1\nfile2\n" * 30},
                {"type": "tool_result", "tool_use_id": "r1", "content": "old body " * 100}]}},
            {"message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "w1", "name": "Write", "input": {"file_path": "/app/a.ts"}}]}},
        ]
        pruned, _, _, stats = SupersededPruner.prune_steps(steps)
        self.assertEqual(stats["pruned_count"], 1, stats)
        # content[0] = bash result (NOT a read — stays intact),
        # content[1] = superseded read result (tombstoned)
        self.assertIn("file1", pruned[1]["message"]["content"][0]["content"])
        self.assertNotIn("superseded", pruned[1]["message"]["content"][0]["content"])
        self.assertIn("superseded", pruned[1]["message"]["content"][1]["content"])


if __name__ == "__main__":
    unittest.main()
