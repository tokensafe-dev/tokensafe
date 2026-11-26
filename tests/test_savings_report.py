"""Savings numbers must come from the canonical data dir, never from a stray DB."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.metrics import MetricsTracker
from src.savings import build_savings_report

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestSavingsReport(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="tokensafe_savings_")
        self.env = patch.dict(os.environ, {"TOKENSAFE_DATA_DIR": self.data_dir})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        import shutil
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def test_report_uses_canonical_data_dir(self):
        tracker = MetricsTracker()
        tracker.record_event(
            session_id="t1",
            event_type="SCOPED_SEARCH",
            original_tokens=1000,
            optimized_tokens=200,
            details={"file": "a.py"},
            measurement="measured",
        )

        report = build_savings_report()
        self.assertEqual(report["source"], "canonical")
        self.assertEqual(report["data_dir"], self.data_dir)
        self.assertTrue(report["db_path"].startswith(self.data_dir))
        self.assertGreaterEqual(report["saved_tokens_live"], 800)
        self.assertEqual(report["last_event"]["event_type"], "SCOPED_SEARCH")
        self.assertGreaterEqual(report["last_event"]["saved_tokens"], 800)
        self.assertEqual(
            report["cumulative_saved_tokens"],
            report["saved_tokens_live"] + report["pruned_synced_saved_tokens"],
        )

    def test_cli_json_is_machine_readable(self):
        tracker = MetricsTracker()
        tracker.record_event(
            session_id="t2",
            event_type="TOOL_READ",
            original_tokens=500,
            optimized_tokens=100,
            details={},
            measurement="measured",
        )

        proc = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "bin", "tokensafe"), "savings", "--json"],
            capture_output=True,
            text=True,
            env={**os.environ, "TOKENSAFE_DATA_DIR": self.data_dir},
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-500:])
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["source"], "canonical")
        self.assertGreaterEqual(payload["saved_tokens_live"], 400)

    def test_empty_data_dir_reports_zero_not_garbage(self):
        report = build_savings_report()
        self.assertEqual(report["saved_tokens_live"], 0)
        self.assertEqual(report["cumulative_saved_tokens"], 0)
        self.assertIsNone(report["last_event"])


if __name__ == "__main__":
    unittest.main()
