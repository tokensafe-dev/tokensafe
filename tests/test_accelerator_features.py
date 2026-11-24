"""
Automated Test Suite for TokenSafe Accelerator & Latency Optimization Features.
Covers:
- Batch Editor Engine (apply_edits, best_effort, inline verify, secret leak guard).
- Batch Search Engine (concurrent text, regex, glob, git read-only queries).
- Workspace Snapshot (SessionStart injection, git status, tree, scripts).
- Zero-Spill Guard (<=8800 bytes cap with stash & expand_ref anchor).
- Multi-Agent Transcript Analyzer (Antigravity/Claude transcripts, A/B comparison).
"""

import os
import sys
import json
import shutil
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.batch_editor import execute_batch_edits
from src.batch_search import execute_batch_search
from src.snapshot import build_workspace_snapshot
from src.mcp_server import _cap_mcp_output_size
from src.transcript_analyzer import analyze_transcript, compare_transcripts, render_transcripts_report


class TestAcceleratorFeatures(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="tokensafe_accel_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_batch_editor_atomic_success(self):
        """Tests multi-file, multi-hunk atomic edits applying cleanly."""
        file_a = os.path.join(self.tmp_dir, "a.py")
        file_b = os.path.join(self.tmp_dir, "b.ts")

        with open(file_a, "w", encoding="utf-8") as f:
            f.write("def foo():\n    return 1\n")
        with open(file_b, "w", encoding="utf-8") as f:
            f.write("export const x = 10;\nexport const y = 20;\n")

        edits = [
            {
                "path": "a.py",
                "hunks": [{"target": "return 1", "replacement": "return 42"}]
            },
            {
                "path": "b.ts",
                "hunks": [{"target": "export const x = 10;", "replacement": "export const x = 100;"}]
            },
            {
                "path": "c.txt",
                "create": "Hello new file!\n"
            }
        ]

        res = execute_batch_edits(edits=edits, cwd=self.tmp_dir)
        self.assertTrue(res["success"])
        self.assertEqual(res["applied_count"], 3)
        self.assertEqual(res["failed_count"], 0)

        with open(file_a, "r", encoding="utf-8") as f:
            self.assertIn("return 42", f.read())
        with open(file_b, "r", encoding="utf-8") as f:
            self.assertIn("x = 100", f.read())
        with open(os.path.join(self.tmp_dir, "c.txt"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "Hello new file!\n")

    def test_batch_editor_atomic_failure_rollback(self):
        """Tests that when 1 hunk fails in atomic mode, 0 files are touched on disk."""
        file_a = os.path.join(self.tmp_dir, "a.py")
        file_b = os.path.join(self.tmp_dir, "b.py")

        with open(file_a, "w", encoding="utf-8") as f:
            f.write("v1 = 1\n")
        with open(file_b, "w", encoding="utf-8") as f:
            f.write("v2 = 2\n")

        edits = [
            {
                "path": "a.py",
                "hunks": [{"target": "v1 = 1", "replacement": "v1 = 10"}]
            },
            {
                "path": "b.py",
                "hunks": [{"target": "NON_EXISTENT_STRING", "replacement": "v2 = 20"}]
            }
        ]

        res = execute_batch_edits(edits=edits, best_effort=False, cwd=self.tmp_dir)
        self.assertFalse(res["success"])
        self.assertEqual(res["failed_count"], 1)

        # File A must remain unchanged because atomic mode rejected the batch
        with open(file_a, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "v1 = 1\n")

    def test_batch_editor_best_effort(self):
        """Tests that best_effort=True applies valid files and returns drift hint for invalid."""
        file_a = os.path.join(self.tmp_dir, "a.py")
        file_b = os.path.join(self.tmp_dir, "b.py")

        with open(file_a, "w", encoding="utf-8") as f:
            f.write("v1 = 1\n")
        with open(file_b, "w", encoding="utf-8") as f:
            f.write("v2 = 2\n")

        edits = [
            {
                "path": "a.py",
                "hunks": [{"target": "v1 = 1", "replacement": "v1 = 10"}]
            },
            {
                "path": "b.py",
                "hunks": [{"target": "NON_EXISTENT", "replacement": "v2 = 20"}]
            }
        ]

        res = execute_batch_edits(edits=edits, best_effort=True, cwd=self.tmp_dir)
        self.assertEqual(res["applied_count"], 1)
        self.assertEqual(res["failed_count"], 1)

        # File A was applied under best-effort
        with open(file_a, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "v1 = 10\n")

    def test_batch_editor_with_verify(self):
        """Tests that inline verify command executes and formats pass status."""
        file_a = os.path.join(self.tmp_dir, "calc.py")
        with open(file_a, "w", encoding="utf-8") as f:
            f.write("val = 1\n")

        edits = [
            {
                "path": "calc.py",
                "hunks": [{"target": "val = 1", "replacement": "val = 2"}]
            }
        ]

        verify_cmd = f"python3 -c 'import calc; assert calc.val == 2'"
        res = execute_batch_edits(edits=edits, verify=verify_cmd, cwd=self.tmp_dir)
        self.assertTrue(res["success"])
        self.assertIn("verify", res)
        self.assertTrue(res["verify"]["passed"])
        self.assertEqual(res["verify"]["exit_code"], 0)

    def test_batch_editor_secret_leak_blocked(self):
        """Tests that hardcoded private keys or secrets in replacements are blocked."""
        file_a = os.path.join(self.tmp_dir, "auth.py")
        with open(file_a, "w", encoding="utf-8") as f:
            f.write("API_KEY = ''\n")

        # Dynamically construct test pattern so file writer does not trip guard
        test_secret = "ghp_" + ("a1b2c3d4" * 4) + "1234"
        edits = [
            {
                "path": "auth.py",
                "hunks": [{"target": "API_KEY = ''", "replacement": f"API_KEY = os.environ.get('KEY', '{test_secret}')"}]
            }
        ]

        res = execute_batch_edits(edits=edits, cwd=self.tmp_dir)
        self.assertFalse(res["success"])
        self.assertTrue(any("Security Guard" in err.get("error", "") for err in res["failed_files"]))

    def test_batch_search_queries(self):
        """Tests concurrent batch search across files."""
        sub = os.path.join(self.tmp_dir, "src")
        os.makedirs(sub, exist_ok=True)
        with open(os.path.join(sub, "service.ts"), "w", encoding="utf-8") as f:
            f.write("export async function handleRequest() {\n    return 'ok';\n}\n")
        with open(os.path.join(sub, "utils.py"), "w", encoding="utf-8") as f:
            f.write("def helper():\n    return 42\n")

        queries = [
            {"pattern": "handleRequest", "type": "text"},
            {"pattern": r"def\s+helper", "type": "regex"},
            {"pattern": "*.ts", "type": "glob"},
            {"pattern": "util", "type": "fuzzy"}
        ]

        res = execute_batch_search(queries=queries, cwd=self.tmp_dir)
        self.assertTrue(res["success"])
        self.assertEqual(res["query_count"], 4)
        self.assertIn("handleRequest", res["summary_md"])
        self.assertIn("helper", res["summary_md"])

    def test_workspace_snapshot(self):
        """Tests workspace snapshot generation for project directory."""
        with open(os.path.join(self.tmp_dir, "package.json"), "w", encoding="utf-8") as f:
            json.dump({"scripts": {"test": "vitest", "build": "vite build"}}, f)

        src_dir = os.path.join(self.tmp_dir, "src")
        os.makedirs(src_dir, exist_ok=True)
        with open(os.path.join(src_dir, "main.ts"), "w", encoding="utf-8") as f:
            f.write("console.log('hi');\n")

        snap = build_workspace_snapshot(self.tmp_dir, max_bytes=1400)
        self.assertIsNotNone(snap)
        self.assertIn("[TokenSafe Workspace Snapshot]", snap)
        self.assertIn("Layout (2 levels):", snap)
        self.assertIn("Scripts: test", snap)
        self.assertLessEqual(len(snap.encode("utf-8")), 1400)

    def test_zero_spill_guard_capping(self):
        """Tests that outputs > 8800 bytes are folded and tagged with stash anchor."""
        huge_text = "Line content item: " + ("x" * 80) + "\n"
        huge_payload = huge_text * 200  # ~16 KB

        result = {
            "content": [
                {"type": "text", "text": huge_payload}
            ]
        }

        capped = _cap_mcp_output_size(result, tool_name="test_tool", max_bytes=8800)
        capped_text = capped["content"][0]["text"]
        capped_bytes = len(capped_text.encode("utf-8"))

        self.assertLessEqual(capped_bytes, 8800)
        self.assertIn("TokenSafe Zero-Spill Guard", capped_text)
        self.assertIn("[TokenSafe folded: #", capped_text)

    def test_transcript_analyzer(self):
        """Tests parsing of JSONL transcript and A/B comparison."""
        transcript_a = os.path.join(self.tmp_dir, "transcript_a.jsonl")
        transcript_b = os.path.join(self.tmp_dir, "transcript_b.jsonl")

        with open(transcript_a, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "read_file"}], "created_at": "2026-11-24T12:00:00Z"}) + "\n")
            f.write(json.dumps({"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "edit_file"}], "created_at": "2026-11-24T12:00:10Z"}) + "\n")
            f.write(json.dumps({"type": "PLANNER_RESPONSE", "content": "The output was large and was saved to: file:///tmp/output.txt", "created_at": "2026-11-24T12:00:20Z"}) + "\n")

        with open(transcript_b, "w", encoding="utf-8") as f:
            # Batch call: 1 turn, 2 tools, 0 spills
            f.write(json.dumps({"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "tokensafe_apply_edits"}, {"name": "tokensafe_batch_search"}], "created_at": "2026-11-24T12:00:00Z"}) + "\n")

        analysis_a = analyze_transcript(transcript_a)
        self.assertEqual(analysis_a["llm_turns"], 3)
        self.assertEqual(analysis_a["disk_spills"], 1)

        analysis_b = analyze_transcript(transcript_b)
        self.assertEqual(analysis_b["llm_turns"], 1)
        self.assertEqual(analysis_b["disk_spills"], 0)
        self.assertEqual(analysis_b["tokensafe_calls"], 2)

        comp = compare_transcripts(transcript_a, transcript_b)
        self.assertEqual(comp["llm_turns"]["before"], 3)
        self.assertEqual(comp["llm_turns"]["after"], 1)
        self.assertEqual(comp["disk_spills"]["delta"], -1)


if __name__ == "__main__":
    unittest.main()
