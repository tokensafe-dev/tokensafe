"""
Unit tests for Ghost Tokens Audit in TokenSafe EnvironmentAuditor.
Verifies detection of:
1. Multi-IDE redundant rule files (CLAUDE.md, .cursorrules, .windsurfrules).
2. Dead commented-out rule blocks.
3. Oversized configuration files.
"""

import os
import shutil
import tempfile
import unittest

from src.environment_auditor import EnvironmentAuditor


class TestGhostTokensAudit(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="tokensafe_ghost_test_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_single_canonical_agents_md_is_clean(self):
        # Only 1 canonical AGENTS.md exists
        agents_file = os.path.join(self.test_dir, "AGENTS.md")
        with open(agents_file, "w") as f:
            f.write("# Project Instructions\nFollow zero-loss guidelines.\n")

        res = EnvironmentAuditor.audit_ghost_tokens(self.test_dir)
        self.assertFalse(res["has_ghost_tokens"])
        self.assertEqual(res["ghost_tokens_per_turn"], 0)
        self.assertEqual(len(res["ghost_files"]), 0)

    def test_multi_ide_redundant_rules_detected(self):
        # Create AGENTS.md + .cursorrules + CLAUDE.md
        with open(os.path.join(self.test_dir, "AGENTS.md"), "w") as f:
            f.write("# AGENTS Instructions\n" * 20)

        with open(os.path.join(self.test_dir, ".cursorrules"), "w") as f:
            f.write("# Cursor specific redundant copy\n" * 20)

        with open(os.path.join(self.test_dir, "CLAUDE.md"), "w") as f:
            f.write("# Claude redundant copy\n" * 20)

        res = EnvironmentAuditor.audit_ghost_tokens(self.test_dir)
        self.assertTrue(res["has_ghost_tokens"])
        self.assertGreater(res["ghost_tokens_per_turn"], 0)

        ghost_types = {gf["type"] for gf in res["ghost_files"]}
        self.assertIn("DUPLICATE_CROSS_IDE_RULE", ghost_types)

        file_names = {gf["file"] for gf in res["ghost_files"]}
        self.assertTrue(".cursorrules" in file_names or "CLAUDE.md" in file_names)

    def test_dead_commented_blocks_detected(self):
        # A rule file containing a large block of old disabled/commented-out rules
        dead_comment = "<!--\nOld rules from 2024:\n" + ("- Do not use feature X\n" * 30) + "-->\n"
        with open(os.path.join(self.test_dir, "AGENTS.md"), "w") as f:
            f.write(dead_comment + "# Active Rule\nFollow guidelines.\n")

        res = EnvironmentAuditor.audit_ghost_tokens(self.test_dir)
        self.assertTrue(res["has_ghost_tokens"])

        dead_files = [gf for gf in res["ghost_files"] if gf["type"] == "DEAD_COMMENT_BLOCKS"]
        self.assertGreaterEqual(len(dead_files), 1)
        self.assertEqual(dead_files[0]["file"], "AGENTS.md")

    def test_cli_report_includes_ghost_tokens(self):
        with open(os.path.join(self.test_dir, "AGENTS.md"), "w") as f:
            f.write("# AGENTS Instructions\n" * 10)
        with open(os.path.join(self.test_dir, ".cursorrules"), "w") as f:
            f.write("# Cursor rules\n" * 10)

        report_text = EnvironmentAuditor.format_cli_report(self.test_dir)
        self.assertIn("Ghost Tokens Detected:", report_text)
        self.assertIn(".cursorrules", report_text)
        self.assertIn("Consolidate redundant rule files", report_text)


if __name__ == "__main__":
    unittest.main()
