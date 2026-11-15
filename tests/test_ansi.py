"""
Unit tests for TokenSafe ANSI Escape Sequence Stripper.
Verifies removal of terminal colors, styling, cursor controls,
and OSC hyperlink sequences without corrupting code or logs.
"""

import unittest
from src.format_parsers.ansi import strip_ansi, has_ansi


class TestAnsiStripper(unittest.TestCase):
    def test_has_ansi(self):
        self.assertFalse(has_ansi("Plain text with no escapes"))
        self.assertFalse(has_ansi(""))
        self.assertFalse(has_ansi(None))
        self.assertTrue(has_ansi("\x1b[31mRed text\x1b[0m"))
        self.assertTrue(has_ansi("\033[1mBold text\033[0m"))

    def test_strip_basic_colors_and_styles(self):
        colored = "\x1b[31;1mERROR:\x1b[0m \x1b[32mBuild succeeded\x1b[0m"
        clean = strip_ansi(colored)
        self.assertEqual(clean, "ERROR: Build succeeded")

    def test_strip_cursor_and_line_clear_controls(self):
        # \x1b[2K = clear line, \x1b[1A = cursor up
        progress = "\x1b[2K\rLoading package 1...\x1b[2K\rLoading package 2...\x1b[0mDone"
        clean = strip_ansi(progress)
        self.assertNotIn("\x1b", clean)
        self.assertIn("Done", clean)

    def test_strip_osc_hyperlinks(self):
        # OSC 8 hyperlink: \x1b]8;;http://example.com\x1b\Link\x1b]8;;\x1b\
        osc_text = "\x1b]8;;https://tokensafe.dev\x1b\\TokenSafe\x1b]8;;\x1b\\"
        clean = strip_ansi(osc_text)
        self.assertEqual(clean, "TokenSafe")

    def test_realistic_pytest_colored_failure(self):
        colored_pytest = """
\x1b[1m\x1b[31m=================================== FAILURES ===================================\x1b[0m
\x1b[1m\x1b[31m_________________________________ test_payment _________________________________\x1b[0m

    \x1b[94mdef\x1b[39;49;00m \x1b[92mtest_payment\x1b[39;49;00m():
>       \x1b[94massert\x1b[39;49;00m charge(\x1b[94m100\x1b[39;49;00m) == \x1b[94mTrue\x1b[39;49;00m
\x1b[1m\x1b[31mE       AssertionError: assert False == True\x1b[0m

\x1b[1m\x1b[31m=========================== short test summary info ============================\x1b[0m
\x1b[31mFAILED\x1b[0m tests/test_payment.py::test_payment - AssertionError: assert False == True
\x1b[1m\x1b[31m1 failed\x1b[0m, \x1b[32m12 passed\x1b[0m \x1b[33min 0.42s\x1b[0m
"""
        clean = strip_ansi(colored_pytest)
        self.assertNotIn("\x1b", clean)
        self.assertIn("FAILED tests/test_payment.py::test_payment - AssertionError: assert False == True", clean)
        self.assertIn("assert charge(100) == True", clean)
        self.assertIn("1 failed, 12 passed in 0.42s", clean)

    def test_plain_text_identity(self):
        plain = "Normal log without any escape codes at all.\nLine 2.\n"
        self.assertEqual(strip_ansi(plain), plain)


if __name__ == "__main__":
    unittest.main()
