"""
Unit tests for TokenSafe Compiler Error (tsc) & Stacktrace Compactor.
"""

import unittest
from src.format_parsers.compiler_errors import CompilerErrorCompactor
from src.test_parsers import TestOutputParser


class TestCompilerErrors(unittest.TestCase):

    def test_tsc_cascade_compaction(self):
        raw_tsc = """
src/index.ts(10,5): error TS2322: Type 'string' is not assignable to type 'number'.
src/index.ts(15,8): error TS2322: Type 'boolean' is not assignable to type 'number'.
src/index.ts(20,8): error TS2322: Type 'object' is not assignable to type 'number'.
src/user.ts(5,12): error TS2304: Cannot find name 'UserProfile'.
src/user.ts(12,14): error TS2304: Cannot find name 'UserProfile'.
src/user.ts(25,18): error TS2304: Cannot find name 'UserProfile'.
src/user.ts(35,18): error TS2304: Cannot find name 'UserProfile'.
Found 7 errors in 2 files.
"""
        compacted, was_compacted = CompilerErrorCompactor.compact_tsc(raw_tsc)
        self.assertTrue(was_compacted)
        self.assertIn("TypeScript compilation failed: 7 errors", compacted)
        self.assertIn("[TS2304]", compacted)
        self.assertIn("[TS2322]", compacted)
        self.assertIn("TokenSafe folded: #", compacted)

    def test_test_output_parser_tsc_dispatch(self):
        raw_tsc = """
src/app.ts:1:1 - error TS2304: Cannot find name 'React'.
src/app.ts:2:1 - error TS2304: Cannot find name 'React'.
src/app.ts:3:1 - error TS2304: Cannot find name 'React'.
src/app.ts:4:1 - error TS2304: Cannot find name 'React'.
src/app.ts:5:1 - error TS2304: Cannot find name 'React'.
src/app.ts:6:1 - error TS2304: Cannot find name 'React'.
Found 6 errors.
"""
        compacted, was_compacted = TestOutputParser.compact(raw_tsc)
        self.assertTrue(was_compacted)
        self.assertIn("TypeScript compilation failed", compacted)
        self.assertIn("TokenSafe folded: #", compacted)

    def test_stacktrace_cascade_compaction(self):
        deep_tb = ["Traceback (most recent call last):"]
        for i in range(40):
            deep_tb.append(f'  File "recursive.py", line 10, in recurse')
            deep_tb.append(f'    return recurse(n - 1)')
        deep_tb.append("RecursionError: maximum recursion depth exceeded")
        raw_tb = "\n".join(deep_tb)

        compacted, was_compacted = CompilerErrorCompactor.compact_stacktrace(raw_tb)
        self.assertTrue(was_compacted)
        self.assertIn("deep recursive/middle stack frames omitted", compacted)
        self.assertIn("RecursionError", compacted)
        self.assertIn("TokenSafe folded: #", compacted)


if __name__ == "__main__":
    unittest.main()
