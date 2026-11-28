"""apply_edits hunk windows: start_line/end_line disambiguate, they do not clip.

The 2026-11-28 paidz session lost a four-file batch because one hunk's end_line
was two lines short of a multi-line target: the window cut the snippet, the target
was declared "not found", and the whole atomic batch rolled back. The snippet was
unique in the file, so the range was merely mis-measured, not ambiguous.
"""
import os
import shutil
import tempfile
import unittest

from src.mcp_server import handle_tool_call

SOURCE = """export function first() {
  const lock = await redis.set(key, "1", "NX");
  if (!lock) {
    return false;
  }
  return true;
}

export function second() {
  return value();
}

export function third() {
  return value();
}
"""


class ApplyEditsHunkWindowTest(unittest.TestCase):
    def setUp(self):
        self._orig_data_dir = os.environ.get("TOKENSAFE_DATA_DIR")
        self._orig_db_path = os.environ.get("TOKENSAFE_DB_PATH")
        self.tmp = tempfile.mkdtemp(prefix="tokensafe_hunk_window_")
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

    def apply(self, hunk):
        return handle_tool_call(
            "apply_edits",
            {"cwd": self.tmp, "edits": [{"path": "app.js", "hunks": [hunk]}]},
        )

    def test_a_unique_target_applies_when_the_window_is_too_short(self):
        # end_line=4 cuts the target (which runs to line 6), exactly the paidz hunk.
        target = (
            "  const lock = await redis.set(key, \"1\", \"NX\");\n"
            "  if (!lock) {\n"
            "    return false;\n"
            "  }\n"
        )
        result = self.apply(
            {
                "target": target,
                "replacement": "  const lock = await redis.set(key, \"2\", \"NX\");\n",
                "start_line": 2,
                "end_line": 4,
            }
        )
        text = result["content"][0]["text"]
        self.assertNotIn("isError", result, text)
        self.assertIn('redis.set(key, "2", "NX")', self.read())
        self.assertNotIn("return false;", self.read().split("export function second")[0])

    def test_a_unique_target_applies_even_when_the_window_points_elsewhere(self):
        result = self.apply(
            {
                "target": "  return true;\n",
                "replacement": "  return ok;\n",
                "start_line": 9,
                "end_line": 11,
            }
        )
        text = result["content"][0]["text"]
        self.assertNotIn("isError", result, text)
        self.assertIn("return ok;", self.read())

    def test_a_duplicated_target_outside_the_window_names_the_real_lines(self):
        result = self.apply(
            {
                "target": "  return value();\n",
                "replacement": "  return other();\n",
                "start_line": 2,
                "end_line": 4,
            }
        )
        text = result["content"][0]["text"]
        self.assertTrue(result.get("isError"), text)
        self.assertIn("not found in lines 2..4", text)
        self.assertIn("occurs 2 times elsewhere in the file", text)
        self.assertIn("near lines 10, 14", text)  # both real occurrences are named
        self.assertIn("start_line=10", text)
        self.assertEqual(self.read(), SOURCE)

    def test_a_missing_target_reports_a_whole_file_hint_not_a_window_hint(self):
        result = self.apply(
            {
                "target": "  return valuez();\n",
                "replacement": "  return other();\n",
                "start_line": 2,
                "end_line": 4,
            }
        )
        text = result["content"][0]["text"]
        self.assertTrue(result.get("isError"), text)
        self.assertIn("nor anywhere in the file", text)
        # The closest line is reported from the file, so its number is usable.
        self.assertIn("L10", text)


if __name__ == "__main__":
    unittest.main()
