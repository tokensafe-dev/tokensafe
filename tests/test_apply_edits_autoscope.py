"""apply_edits auto-scoping: a repeated hunk target resolves in one call.

The 2026-11-27 benchmark outlier was a model that dropped start_line/end_line,
got `-32011 target_not_unique`, then re-read the file before retrying. These
tests pin the product-side fix: when the last read window contains exactly one
occurrence, the hunk is scoped deterministically; otherwise the error names a
concrete retry and the file is untouched.
"""
import os
import shutil
import tempfile
import unittest

from src.mcp_server import TOOLS, _autoscope_edits, _remember_read_window, handle_tool_call

SOURCE = """export function a() {
  return value();
}
export function b() {
  return value();
}
"""


class ApplyEditsAutoScopeTest(unittest.TestCase):
    def setUp(self):
        self._orig_data_dir = os.environ.get("TOKENSAFE_DATA_DIR")
        self._orig_db_path = os.environ.get("TOKENSAFE_DB_PATH")
        self.tmp = tempfile.mkdtemp(prefix="tokensafe_autoscope_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        data_dir = os.path.join(self.tmp, "_data")
        os.makedirs(data_dir, exist_ok=True)
        os.environ["TOKENSAFE_DATA_DIR"] = data_dir
        os.environ["TOKENSAFE_DB_PATH"] = os.path.join(data_dir, "metrics.db")
        self.path = os.path.join(self.tmp, "app.js")
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(SOURCE)

    def tearDown(self):
        if self._orig_data_dir is None:
            os.environ.pop("TOKENSAFE_DATA_DIR", None)
        else:
            os.environ["TOKENSAFE_DATA_DIR"] = self._orig_data_dir
        if self._orig_db_path is None:
            os.environ.pop("TOKENSAFE_DB_PATH", None)
        else:
            os.environ["TOKENSAFE_DB_PATH"] = self._orig_db_path

    def read(self):
        with open(self.path, encoding="utf-8") as handle:
            return handle.read()

    def test_one_window_with_one_occurrence_scopes_the_hunk(self):
        _remember_read_window(self.path, 1, 3)
        result = handle_tool_call(
            "apply_edits",
            {
                "cwd": self.tmp,
                "edits": [{"path": "app.js", "hunks": [{"target": "return value();", "replacement": "return other();"}]}],
            },
        )
        text = result["content"][0]["text"]
        self.assertNotIn("isError", result, text)
        self.assertIn("auto-scoped", text)
        self.assertIn("lines 1-3", text)
        # Only the first occurrence (line 2) changed; line 5 is intact.
        self.assertEqual(
            self.read(),
            "export function a() {\n  return other();\n}\nexport function b() {\n  return value();\n}\n",
        )

    def test_two_candidates_are_not_guessed_and_the_error_names_the_retry(self):
        _remember_read_window(self.path, 1, 5)
        result = handle_tool_call(
            "apply_edits",
            {
                "cwd": self.tmp,
                "edits": [{"path": "app.js", "hunks": [{"target": "return value();", "replacement": "return other();"}]}],
            },
        )
        text = result["content"][0]["text"]
        self.assertTrue(result.get("isError"), text)
        self.assertIn("occurs 2 times", text)
        self.assertIn("Retry with start_line=2", text)
        self.assertEqual(self.read(), SOURCE)

    def test_an_explicit_range_is_never_overridden(self):
        _remember_read_window(self.path, 1, 3)
        edits, notes = _autoscope_edits(
            [
                {
                    "path": "app.js",
                    "hunks": [{"target": "return value();", "replacement": "x", "start_line": 5, "end_line": 5}],
                }
            ],
            self.tmp,
        )
        self.assertEqual(notes, [])
        self.assertEqual(edits[0]["hunks"][0]["start_line"], 5)

    def test_a_unique_target_is_left_alone(self):
        _remember_read_window(self.path, 1, 5)
        edits, notes = _autoscope_edits(
            [{"path": "app.js", "hunks": [{"target": "export function b()", "replacement": "export function c()"}]}],
            self.tmp,
        )
        self.assertEqual(notes, [])
        self.assertNotIn("start_line", edits[0]["hunks"][0])

    def test_two_windows_each_holding_one_occurrence_are_ambiguous(self):
        _remember_read_window(self.path, 1, 2)
        _remember_read_window(self.path, 4, 5)
        edits, notes = _autoscope_edits(
            [{"path": "app.js", "hunks": [{"target": "return value();", "replacement": "x"}]}],
            self.tmp,
        )
        self.assertEqual(notes, [])
        self.assertNotIn("start_line", edits[0]["hunks"][0])

    def test_schema_tells_the_model_where_the_range_comes_from(self):
        apply_edits = next(tool for tool in TOOLS if tool["name"] == "apply_edits")
        hunks = apply_edits["inputSchema"]["properties"]["edits"]["items"]["properties"]["hunks"]
        self.assertIn("start_line/end_line", hunks["description"])
        self.assertIn("auto-scopes", hunks["description"])
        self.assertIn("slice header", hunks["items"]["properties"]["start_line"]["description"])


if __name__ == "__main__":
    unittest.main()
