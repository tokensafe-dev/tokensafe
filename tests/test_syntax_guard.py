"""
Unit tests for the out-of-the-box syntax guard (src/syntax_guard.py).
Verifies pre-flight validation of direct workspace edits: broken code is
rejected before persistence, unknown languages are honestly reported as
"skipped" (never implied as validated).
"""

import shutil
import unittest

from src.syntax_guard import check_syntax


class TestSyntaxGuard(unittest.TestCase):

    def test_valid_python_passes(self):
        res = check_syntax("/tmp/app.py", "def hello():\n    return 'world'\n")
        self.assertEqual(res["status"], "passed")
        self.assertEqual(res["tool"], "python-ast")

    def test_broken_python_fails_with_line_info(self):
        res = check_syntax("/tmp/app.py", "def hello(:\n    return 1\n")
        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["tool"], "python-ast")
        self.assertIn("line", res["error"])

    def test_python_unicode_syntax_error(self):
        res = check_syntax("/tmp/app.py", "x = 'unclosed\n")
        self.assertEqual(res["status"], "failed")

    def test_unknown_extension_is_skipped_honestly(self):
        for ext in (".tsx", ".vue", ".txt", ".go", ".rs", ""):
            res = check_syntax(f"/tmp/file{ext}", "any content")
            self.assertEqual(res["status"], "skipped", ext)
            self.assertIn("no syntax checker", res["error"])

    def test_missing_file_extension_is_skipped(self):
        res = check_syntax("/tmp/noext", "x = 1")
        self.assertEqual(res["status"], "skipped")

    @unittest.skipUnless(shutil.which("node"), "node is required for JS syntax checks")
    def test_valid_javascript_passes_node_check(self):
        res = check_syntax("/tmp/app.js", "function hello() { return 42; }\n")
        self.assertEqual(res["status"], "passed")
        self.assertEqual(res["tool"], "node --check")

    @unittest.skipUnless(shutil.which("node"), "node is required for JS syntax checks")
    def test_broken_javascript_fails_node_check(self):
        res = check_syntax("/tmp/app.js", "function hello( { return 42; }\n")
        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["tool"], "node --check")
        self.assertTrue(res["error"])

    def test_guard_never_raises(self):
        # Pathological inputs must return a status, never raise
        for content in ("", "\x00\x01", "def f(:", "€" * 100):
            res = check_syntax("/tmp/x.py", content)
            self.assertIn(res["status"], ("passed", "failed", "skipped"))


if __name__ == "__main__":
    unittest.main()
