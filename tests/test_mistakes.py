"""
Unit tests for TokenSafe Mistakes Ledger (Revert & Anti-Stuck Synergy Memory).
"""

import unittest
import os
import shutil
import tempfile
from src.mistakes import MistakesLedger
from src.loop_detector import LoopDetector


class TestMistakesLedger(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="tokensafe_mistakes_")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_record_and_list_mistakes(self):
        e1 = MistakesLedger.record_revert(
            file_path="src/server.ts",
            reason="Deadlock when using sync mutex in async handler",
            project_root=self.tmp_dir
        )
        self.assertIsNotNone(e1["id"])
        self.assertEqual(e1["file"], "server.ts")

        entries = MistakesLedger.list_mistakes(project_root=self.tmp_dir)
        self.assertEqual(len(entries), 1)
        self.assertIn("Deadlock", entries[0]["summary"])

        guard = MistakesLedger.get_guard_prompt(project_root=self.tmp_dir)
        self.assertIsNotNone(guard)
        self.assertIn("server.ts", guard)
        self.assertIn("Deadlock", guard)

    def test_loop_detector_synergy(self):
        # Configure LoopDetector to use a temporary state file in self.tmp_dir
        LoopDetector.STATE_FILE = os.path.join(self.tmp_dir, "loop_state.json")
        cid = "conv_test_synergy"
        tool = "read_file"
        args = {"Path": "/app/non_existent.py"}

        # Simulate 3 identical invocations to trigger LoopDetector
        LoopDetector.check_loop(cid, tool, args)
        LoopDetector.check_loop(cid, tool, args)
        is_loop, repeats, msg = LoopDetector.check_loop(cid, tool, args)

        self.assertTrue(is_loop)
        self.assertEqual(repeats, 3)

        # Check that LoopDetector logged the failure into global/local mistakes
        # (It calls MistakesLedger.record_from_loop)
        entries = MistakesLedger.list_mistakes()
        self.assertTrue(any("non_existent.py" in e.get("summary", "") for e in entries))

    def test_clear_mistakes(self):
        MistakesLedger.record_revert("foo.py", "reason 1", project_root=self.tmp_dir)
        self.assertEqual(len(MistakesLedger.list_mistakes(project_root=self.tmp_dir)), 1)

        cleared = MistakesLedger.clear_mistakes(project_root=self.tmp_dir)
        self.assertEqual(cleared, 1)
        self.assertEqual(len(MistakesLedger.list_mistakes(project_root=self.tmp_dir)), 0)


if __name__ == "__main__":
    unittest.main()
