"""The benchmark CLI must never spend money on a typo.

Regression: `tokensafe bench-oumv --help` ignored the unknown flag, fell through to
the defaults (all tasks, $1 budget) and started a paid DeepSeek run.
"""

import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "bin", "tokensafe")


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, CLI, "bench-oumv", *args],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=ROOT,
    )


class TestBenchCliGuards(unittest.TestCase):
    def test_help_prints_usage_and_exits(self):
        proc = _run("--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("bench-oumv", proc.stdout)
        self.assertIn("--dry-run", proc.stdout)
        self.assertNotIn("OUMV bench (go)", proc.stdout)

    def test_dry_run_lists_the_matrix_without_calling_the_provider(self):
        proc = _run(
            "--target", "go",
            "--arms", "baseline",
            "--tasks", "product-create-status",
            "--runs", "1",
            "--dry-run",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("1 sessions", proc.stdout)
        self.assertIn("no provider call was made", proc.stdout)
        self.assertNotIn("OUMV bench (go)", proc.stdout)


if __name__ == "__main__":
    unittest.main()
