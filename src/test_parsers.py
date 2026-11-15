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


class TestOutputParser:
    """Parses and folds test runner terminal outputs."""

    @classmethod
    def detect_runner(cls, cmd_or_output: str) -> Optional[str]:
        s = cmd_or_output.lower()
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

        if detected == "pytest":
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
            return f"✓ {ran} — all tests passed", True

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
