import os
import shutil
import tempfile
import unittest

from src.competitors import CompetitorAuditor, COMPETITORS_SPEC


class TestCompetitorAuditor(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="tokensafe_comp_test_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_audit_empty(self):
        # In a clean mock environment where no binaries/files match
        detected = CompetitorAuditor.audit()
        # Should return a list (empty or whatever happens to be on real system PATH)
        self.assertIsInstance(detected, list)

    def test_cleanup_hook(self):
        # Create a mock rtk hook file
        mock_hook = os.path.join(self.test_dir, "rtk-rewrite.sh")
        with open(mock_hook, "w") as f:
            f.write("#!/bin/bash\n")

        # Temporarily patch spec hook path
        orig_hooks = COMPETITORS_SPEC["rtk"]["hooks"]
        try:
            COMPETITORS_SPEC["rtk"]["hooks"] = [mock_hook]
            res = CompetitorAuditor.cleanup("rtk")
            self.assertTrue(res["success"])
            self.assertIn(mock_hook, res["removed"])
            self.assertFalse(os.path.exists(mock_hook))
        finally:
            COMPETITORS_SPEC["rtk"]["hooks"] = orig_hooks


if __name__ == "__main__":
    unittest.main()
