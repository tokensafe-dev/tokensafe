import os
import shutil
import tempfile
import unittest

from src.test_parsers import TestOutputParser
from src.stash import retrieve_block


class TestTestOutputParser(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="tokensafe_test_parsers_")
        self.old_data_dir = os.environ.get("TOKENSAFE_DATA_DIR")
        os.environ["TOKENSAFE_DATA_DIR"] = self.test_dir

    def tearDown(self):
        if self.old_data_dir is not None:
            os.environ["TOKENSAFE_DATA_DIR"] = self.old_data_dir
        else:
            os.environ.pop("TOKENSAFE_DATA_DIR", None)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_pytest_pass(self):
        raw = """
============================= test session starts ==============================
platform darwin -- Python 3.11.4, pytest-7.4.0
rootdir: /path/to/project
collected 412 items

tests/test_a.py ........................................................ [ 13%]
tests/test_b.py ........................................................ [ 27%]
tests/test_c.py ........................................................ [100%]

======================== 412 passed, 2 skipped in 3.42s ========================
"""
        compacted, was_compacted = TestOutputParser.compact(raw, runner="pytest")
        self.assertTrue(was_compacted)
        self.assertIn("✓ PASS: 412 passed, 2 skipped (pytest in 3.42s)", compacted)

    def test_pytest_fail(self):
        raw = """
============================= test session starts ==============================
collected 20 items

tests/test_a.py ..F.................                                    [100%]

=================================== FAILURES ===================================
__________________________________ test_login __________________________________
    def test_login():
>       assert login() == 200
E       AssertionError: assert 401 == 200
tests/test_a.py:12: AssertionError
=========================== short test summary info ============================
FAILED tests/test_a.py::test_login - AssertionError: assert 401 == 200
========================= 1 failed, 19 passed in 0.85s =========================
"""
        compacted, was_compacted = TestOutputParser.compact(raw, runner="pytest")
        self.assertTrue(was_compacted)
        self.assertIn("✗ FAIL: 1 failed, 19 passed (pytest in 0.85s)", compacted)
        self.assertIn("AssertionError", compacted)
        self.assertIn("[TokenSafe folded: #", compacted)

        # Ensure raw content was stashed and can be retrieved
        restored = retrieve_block(compacted)
        self.assertEqual(restored, raw)

    def test_cargo_test_pass(self):
        raw = """
running 45 tests
test tests::test_init ... ok
test tests::test_auth ... ok
test tests::test_parse ... ok

test result: ok. 45 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.23s
"""
        compacted, was_compacted = TestOutputParser.compact(raw, runner="cargo test")
        self.assertTrue(was_compacted)
        self.assertIn("✓ PASS: 45 passed (cargo test in 0.23s)", compacted)

    def test_cargo_test_fail(self):
        raw = """
running 10 tests
test tests::test_ok ... ok
test tests::test_bad ... FAILED

failures:
---- tests::test_bad stdout ----
thread 'main' panicked at 'assertion failed: 1 == 2', src/lib.rs:10:5

failures:
    tests::test_bad

test result: FAILED. 9 passed; 1 failed; 0 ignored; 0 measured; finished in 0.05s
"""
        compacted, was_compacted = TestOutputParser.compact(raw, runner="cargo test")
        self.assertTrue(was_compacted)
        self.assertIn("✗ FAIL: 1 failed, 9 passed", compacted)
        self.assertIn("tests::test_bad", compacted)
        self.assertIn("[TokenSafe folded: #", compacted)

    def test_vitest_pass(self):
        raw = """
 RUN  v1.3.0 /path/to/project

 ✓ src/index.test.ts (12 tests)
 ✓ src/auth.test.ts (8 tests)

 Test Files  2 passed (2)
      Tests  20 passed (20)
   Start at  00:00:00
   Duration  1.20s (transform 80ms, setup 0ms, collect 120ms, tests 400ms, environment 0ms, prepare 200ms)
"""
        compacted, was_compacted = TestOutputParser.compact(raw, runner="vitest")
        self.assertTrue(was_compacted)
        self.assertIn("✓ PASS: 20/20 tests passed (vitest)", compacted)

    def test_go_test_pass(self):
        raw = """
=== RUN   TestAuth
--- PASS: TestAuth (0.00s)
=== RUN   TestLogin
--- PASS: TestLogin (0.01s)
PASS
ok  	github.com/org/repo/pkg/auth	0.021s
ok  	github.com/org/repo/pkg/api	0.045s
"""
        compacted, was_compacted = TestOutputParser.compact(raw, runner="go test")
        self.assertTrue(was_compacted)
        self.assertIn("✓ PASS: all 2 packages passed (go test)", compacted)

    def test_bun_test_pass_single_file(self):
        raw = """
bun test v1.3.10 (30e609e0)

src/modules/ads/ads.test.ts:
✓ Ads Controller & API > GET /ads/networks should return networks list even without token [16.85ms]
✓ Ads Controller & API > POST /ads/start and /ads/watch should reject AdLoop direct watch [9.51ms]

 47 pass
 0 fail
 207 expect() calls
Ran 47 tests across 1 file. [1125.00ms]
"""
        compacted, was_compacted = TestOutputParser.compact(raw)
        self.assertTrue(was_compacted)
        self.assertIn("✓ PASS: 47 tests passed across 1 file(s) in 1125.00ms (207 expects) [bun test]", compacted)

    def test_bun_test_pass_multi_file(self):
        raw = """
 8 pass
 0 fail
 27 expect() calls
Ran 8 tests across 1 file. [57.00ms]
✅ src/lib/violation-ledger-refresh.test.ts
bun test v1.3.10 (30e609e0)

55/55 test files passed in 57.0s
"""
        compacted, was_compacted = TestOutputParser.compact(raw)
        self.assertTrue(was_compacted)
        self.assertIn("✓ PASS: 55/55 test files passed", compacted)
        self.assertIn("[bun test]", compacted)

    def test_bun_test_pass_with_commit_info(self):
        raw = """
 55/55 test files passed in 57.0s
[main 6bf2fd0] feat(ads): add TeleMonix ad network configuration and velocity thresholds
 5 files changed, 43 insertions(+), 3 deletions(-)
 create mode 100644 scripts/seed_telemonix.ts
"""
        compacted, was_compacted = TestOutputParser.compact(raw)
        self.assertTrue(was_compacted)
        self.assertIn("✓ PASS: 55/55 test files passed in 57.0s [bun test]", compacted)
        self.assertIn("[main 6bf2fd0] feat(ads): add TeleMonix ad network configuration and velocity thresholds", compacted)
        self.assertIn("5 files changed, 43 insertions(+), 3 deletions(-)", compacted)

    def test_bun_test_fail(self):
        raw = """
bun test v1.3.10 (30e609e0)

sample.test.ts:
✓ passes
error: expect(received).toBe(expected)

Expected: 3
Received: 2

      at <anonymous> (/tmp/sample.test.ts:6:17)
✗ fails [0.69ms]

 1 pass
 1 fail
 2 expect() calls
Ran 2 tests across 1 file. [12.00ms]
"""
        compacted, was_compacted = TestOutputParser.compact(raw)
        self.assertTrue(was_compacted)
        self.assertIn("✗ FAIL: 1 failed, 1 passed in 12.00ms [bun test]", compacted)
        self.assertIn("Expected: 3", compacted)
        self.assertIn("Received: 2", compacted)
        self.assertIn("[TokenSafe folded: #", compacted)

        restored = retrieve_block(compacted)
        self.assertEqual(restored, raw)

    def test_unittest_pass_with_status(self):
        raw = """
....................
----------------------------------------------------------------------
Ran 710 tests in 23.009s

OK (skipped=16)
"""
        compacted, was_compacted = TestOutputParser.compact(raw)
        self.assertTrue(was_compacted)
        self.assertIn("✓ Ran 710 tests in 23.009s — all tests passed (OK (skipped=16))", compacted)


if __name__ == "__main__":
    unittest.main()
