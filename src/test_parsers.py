"""
TokenSafe Universal Test Output Compactor (v3.3.0).

Parses verbose stdout/stderr of test frameworks:
- pytest (Python)
- unittest (Python)
- vitest / jest (JavaScript/TypeScript)
- cargo test (Rust)
- go test (Go)

Compacts passing suites to 1 line (-99% tokens).
For failing suites, preserves passing counts + exact failure blocks and stashes
the raw traceback to the reversible local stash with a hash anchor.
"""

import re
from typing import Dict, Any, List, Optional, Tuple
from src.stash import stash_block

# Runner signatures
_PYTEST_SUMMARY_RE = re.compile(
    r"=+\s*(?:(?P<failed>\d+)\s+failed)?(?:,\s*)?(?P<passed>\d+)\s+passed(?:,\s*(?P<skipped>\d+)\s+skipped)?.*?\s+in\s+(?P<dur>[0-9.]+[ms]*)\s*=+",
    re.IGNORECASE
)
_VITEST_FILES_RE = re.compile(
    r"Test Files\s+(?:(?P<failed>\d+)\s+failed\s*\|\s*)?(?P<passed>\d+)\s+passed\s*\((?P<total>\d+)\)",
    re.IGNORECASE
)
_VITEST_TESTS_RE = re.compile(
    r"Tests\s+(?:(?P<failed>\d+)\s+failed\s*\|\s*)?(?P<passed>\d+)\s+passed\s*\((?P<total>\d+)\)",
    re.IGNORECASE
)
_JEST_TESTS_RE = re.compile(
    r"Tests:\s*(?:(?P<failed>\d+)\s+failed,\s*)?(?:(?P<passed>\d+)\s+passed,\s*)?(?P<total>\d+)\s+total",
    re.IGNORECASE
)
_CARGO_TEST_RE = re.compile(
    r"test result:\s*(?P<status>ok|FAILED)\.\s*(?P<passed>\d+)\s+passed;\s*(?P<failed>\d+)\s+failed;",
    re.IGNORECASE
)
_CARGO_DUR_RE = re.compile(r"finished in\s+(?P<dur>[0-9.]+[a-zA-Z]*)", re.IGNORECASE)
_GO_TEST_OK_RE = re.compile(r"^ok\s+(\S+)\s+([0-9.]+[ms]+)", re.MULTILINE)
_GO_TEST_FAIL_RE = re.compile(r"^FAIL\s+(\S+)\s+([0-9.]+[ms]+)", re.MULTILINE)

# bun test regexes
_BUN_FILES_SUMMARY_RE = re.compile(
    r"(?P<passed_files>\d+)/(?P<total_files>\d+)\s+test files passed in\s+(?P<dur>[0-9.]+[a-zA-Z]*)",
    re.IGNORECASE
)
_BUN_RAN_RE = re.compile(
    r"Ran\s+(?P<tests>\d+)\s+tests?\s+across\s+(?P<files>\d+)\s+files?\.?\s*\[(?P<dur>[0-9.]+[a-zA-Z]*)\]",
    re.IGNORECASE
)
_BUN_PASS_RE = re.compile(r"^\s*(?P<pass>\d+)\s+pass\b", re.MULTILINE | re.IGNORECASE)
_BUN_FAIL_RE = re.compile(r"^\s*(?P<fail>\d+)\s+fail\b", re.MULTILINE | re.IGNORECASE)
_BUN_EXPECTS_RE = re.compile(r"^\s*(?P<expects>\d+)\s+expect\(\)\s+calls\b", re.MULTILINE | re.IGNORECASE)
_GIT_COMMIT_CONFIRM_RE = re.compile(
    r"(\[[^\]\r\n]+\s+[0-9a-f]{7,40}\][^\r\n]*(?:\n\s*\d+\s+files?\s+changed[^\r\n]*)?)",
    re.IGNORECASE
)


class TestOutputParser:
    """Parses and folds test runner terminal outputs."""

    @classmethod
    def detect_runner(cls, cmd_or_output: str) -> Optional[str]:
        s = cmd_or_output.lower()
        if "bun test" in s or "bun:test" in s:
            return "bun test"
        if "expect() calls" in s and ("pass\n" in s or " pass" in s):
            return "bun test"
        if "test files passed in" in s:
            return "bun test"
        if re.search(r"\bran\s+\d+\s+tests?\s+across\s+\d+\s+files?", s):
            return "bun test"
        if "pytest" in s:
            return "pytest"
        if "unittest" in s or ("ran " in s and " tests in " in s):
            return "unittest"
        if "vitest" in s:
            return "vitest"
        if "jest" in s:
            return "jest"
        if "cargo test" in s or "test result:" in s:
            return "cargo test"
        if "go test" in s or ("--- fail:" in s and "--- pass:" in s):
            return "go test"
        if "tsc" in s or "error ts" in s or ("found " in s and " error" in s):
            return "tsc"
        if "traceback (most recent call last):" in s or "recursionerror:" in s:
            return "stacktrace"
        return None

    @classmethod
    def compact(cls, raw: str, runner: Optional[str] = None) -> Tuple[str, bool]:
        """
        Compacts test output.
        Returns (compacted_text, was_compacted).
        """
        if not raw or len(raw.splitlines()) <= 4:
            return raw, False

        detected = runner or cls.detect_runner(raw)
        if not detected:
            return raw, False

        if detected == "bun test":
            return cls._compact_bun_test(raw)
        elif detected == "pytest":
            return cls._compact_pytest(raw)
        elif detected == "unittest":
            return cls._compact_unittest(raw)
        elif detected in ("vitest", "jest"):
            return cls._compact_js_tests(raw, detected)
        elif detected == "cargo test":
            return cls._compact_cargo_test(raw)
        elif detected == "go test":
            return cls._compact_go_test(raw)
        elif detected == "tsc":
            from src.format_parsers.compiler_errors import CompilerErrorCompactor
            return CompilerErrorCompactor.compact_tsc(raw)
        elif detected == "stacktrace":
            from src.format_parsers.compiler_errors import CompilerErrorCompactor
            return CompilerErrorCompactor.compact_stacktrace(raw)

        return raw, False

    @classmethod
    def _compact_pytest(cls, raw: str) -> Tuple[str, bool]:
        match = _PYTEST_SUMMARY_RE.search(raw)
        if not match:
            return raw, False

        gd = match.groupdict()
        passed = int(gd.get("passed") or 0)
        failed = int(gd.get("failed") or 0)
        skipped = int(gd.get("skipped") or 0)
        dur = gd.get("dur") or ""

        if failed == 0:
            skip_str = f", {skipped} skipped" if skipped else ""
            summary = f"✓ PASS: {passed} passed{skip_str} (pytest in {dur})"
            return summary, True

        # Extract failures section
        failures_section = []
        in_failures = False
        lines = raw.splitlines()
        for ln in lines:
            if "=== FAILURES ===" in ln or "= FAILURES =" in ln:
                in_failures = True
                continue
            if in_failures:
                if ln.startswith("=== short test summary") or ln.startswith("=== 1 ") or _PYTEST_SUMMARY_RE.search(ln):
                    break
                failures_section.append(ln)

        fail_text = "\n".join(failures_section[:35]).strip()
        _, marker = stash_block(raw, source="pytest")
        res = [
            f"✗ FAIL: {failed} failed, {passed} passed (pytest in {dur})",
            "Failures excerpt:",
            fail_text or "  (see full traceback)",
            marker
        ]
        return "\n".join(res), True

    @classmethod
    def _compact_bun_test(cls, raw: str) -> Tuple[str, bool]:
        files_m = _BUN_FILES_SUMMARY_RE.search(raw)
        ran_m = _BUN_RAN_RE.search(raw)
        pass_matches = _BUN_PASS_RE.findall(raw)
        fail_matches = _BUN_FAIL_RE.findall(raw)
        expects_matches = _BUN_EXPECTS_RE.findall(raw)

        if not files_m and not ran_m and not pass_matches:
            return raw, False

        passed = int(pass_matches[-1]) if pass_matches else 0
        failed = int(fail_matches[-1]) if fail_matches else 0

        if files_m:
            passed_files = int(files_m.group("passed_files"))
            total_files = int(files_m.group("total_files"))
            dur = files_m.group("dur")
            if passed_files < total_files:
                failed = max(failed, total_files - passed_files)
        else:
            passed_files = total_files = 0
            dur = ran_m.group("dur") if ran_m else ""

        commit_m = _GIT_COMMIT_CONFIRM_RE.search(raw)
        commit_suffix = f"\n{commit_m.group(1)}" if commit_m else ""

        if failed == 0:
            if files_m:
                if ran_m:
                    tests = ran_m.group("tests")
                    exp_str = f", {expects_matches[-1]} expects" if expects_matches else ""
                    summary = f"✓ PASS: {passed_files}/{total_files} test files passed ({tests} pass{exp_str} in {dur}) [bun test]{commit_suffix}"
                else:
                    summary = f"✓ PASS: {passed_files}/{total_files} test files passed in {dur} [bun test]{commit_suffix}"
            elif ran_m:
                tests = ran_m.group("tests")
                files = ran_m.group("files")
                dur = ran_m.group("dur")
                exp_str = f" ({expects_matches[-1]} expects)" if expects_matches else ""
                summary = f"✓ PASS: {tests} tests passed across {files} file(s) in {dur}{exp_str} [bun test]{commit_suffix}"
            else:
                exp_str = f" ({expects_matches[-1]} expects)" if expects_matches else ""
                summary = f"✓ PASS: {passed} tests passed{exp_str} [bun test]{commit_suffix}"
            return summary, True

        # Failed run: extract failures and stash full output
        _, marker = stash_block(raw, source="bun_test")
        lines = raw.splitlines()
        failures = []
        cur_fail = []
        in_fail = False

        for ln in lines:
            s = ln.strip()
            if "✗ " in ln or ln.startswith("error: ") or "Expected:" in ln or "Received:" in ln:
                in_fail = True
            if in_fail:
                cur_fail.append(ln)
                if len(cur_fail) >= 8 or ("✗ " in ln and len(cur_fail) > 1):
                    failures.append("\n".join(cur_fail))
                    cur_fail = []
                    in_fail = False
                    if len(failures) >= 5:
                        break
        if cur_fail and len(failures) < 5:
            failures.append("\n".join(cur_fail))

        dur_str = f" in {dur}" if dur else ""
        out = [
            f"✗ FAIL: {failed} failed, {passed} passed{dur_str} [bun test]",
            "Failures excerpt:"
        ]
        for fb in failures:
            out.append(f"  {fb}")
        out.append(marker)
        if commit_suffix:
            out.append(commit_suffix.strip())
        return "\n".join(out), True

    @classmethod
    def _compact_unittest(cls, raw: str) -> Tuple[str, bool]:
        lines = raw.splitlines()
        ran = status = ""
        for ln in lines:
            s = ln.strip()
            if s.startswith("Ran ") and " test" in s:
                ran = s
            elif s.startswith("OK") or s.startswith("FAILED"):
                status = s

        if not ran:
            return raw, False

        if status.startswith("OK"):
            status_clean = f" ({status})" if status else ""
            return f"✓ {ran} — all tests passed{status_clean}", True

        # Extract failure blocks
        _, marker = stash_block(raw, source="unittest")
        failures = []
        cur_block = []
        in_block = False
        for ln in lines:
            if ln.startswith("FAIL: ") or ln.startswith("ERROR: "):
                if cur_block:
                    failures.append("\n".join(cur_block[:8]))
                    cur_block = []
                in_block = True
            if in_block:
                cur_block.append(ln)
                if len(failures) >= 5:
                    break
        if cur_block and len(failures) < 5:
            failures.append("\n".join(cur_block[:8]))

        out = [
            f"✗ {ran} — {status}",
            "Failed tests summary:"
        ]
        for fb in failures:
            out.append(f"  {fb}")
        out.append(marker)
        return "\n".join(out), True

    @classmethod
    def _compact_js_tests(cls, raw: str, runner: str) -> Tuple[str, bool]:
        lines = raw.splitlines()
        t_match = _VITEST_TESTS_RE.search(raw) or _JEST_TESTS_RE.search(raw)
        if not t_match:
            return raw, False

        gd = t_match.groupdict()
        failed = int(gd.get("failed") or 0)
        passed = int(gd.get("passed") or 0)
        total = int(gd.get("total") or (passed + failed))

        if failed == 0:
            return f"✓ PASS: {passed}/{total} tests passed ({runner})", True

        _, marker = stash_block(raw, source=runner)
        # Grab lines mentioning FAIL
        fails = [ln.strip() for ln in lines if "FAIL " in ln or "● " in ln][:8]
        out = [
            f"✗ FAIL: {failed} failed, {passed} passed ({runner})",
            "Failed tests:"
        ]
        for f in fails:
            out.append(f"  {f}")
        out.append(marker)
        return "\n".join(out), True

    @classmethod
    def _compact_cargo_test(cls, raw: str) -> Tuple[str, bool]:
        match = _CARGO_TEST_RE.search(raw)
        if not match:
            return raw, False

        gd = match.groupdict()
        status = gd.get("status", "")
        passed = int(gd.get("passed") or 0)
        failed = int(gd.get("failed") or 0)
        dur_m = _CARGO_DUR_RE.search(raw)
        dur = dur_m.group("dur") if dur_m else ""

        dur_str = f" in {dur}" if dur else ""
        if status.lower() == "ok" and failed == 0:
            return f"✓ PASS: {passed} passed (cargo test{dur_str})", True

        _, marker = stash_block(raw, source="cargo_test")
        # Grab failure section
        failures = []
        in_failures = False
        for ln in raw.splitlines():
            if ln.strip() == "failures:":
                in_failures = True
                continue
            if in_failures:
                if ln.startswith("test result:") or not ln.strip():
                    if failures:
                        break
                failures.append(ln.strip())
        out = [
            f"✗ FAIL: {failed} failed, {passed} passed (cargo test{dur_str})",
            "Failures:"
        ]
        for f in failures[:10]:
            out.append(f"  {f}")
        out.append(marker)
        return "\n".join(out), True

    @classmethod
    def _compact_go_test(cls, raw: str) -> Tuple[str, bool]:
        fails = _GO_TEST_FAIL_RE.findall(raw)
        oks = _GO_TEST_OK_RE.findall(raw)
        if not fails and not oks:
            return raw, False

        if not fails:
            pkgs = len(oks)
            return f"✓ PASS: all {pkgs} packages passed (go test)", True

        _, marker = stash_block(raw, source="go_test")
        out = [
            f"✗ FAIL: {len(fails)} package(s) failed, {len(oks)} passed (go test)",
            "Failed packages:"
        ]
        for pkg, dur in fails:
            out.append(f"  ✗ {pkg} ({dur})")
        out.append(marker)
        return "\n".join(out), True
