"""
TokenSafe Compiler Error & Stacktrace Cascade Compactor (v3.3.0).

Specialized compactor for:
1. `tsc` / `tsc --noEmit` (TypeScript Compiler output):
   - Deduplicates cascading type errors (e.g. 50 missing symbol errors caused by 1 import issue).
   - Preserves root errors while folding duplicate cascades into the reversible stash.
2. Stacktrace cascade deduplicator:
   - Compresses repetitive recursive or nested framework tracebacks.
"""

import re
from typing import Dict, Any, List, Tuple, Optional
from src.stash import stash_block
from src.format_parsers.ansi import strip_ansi

# Regex patterns for tsc output:
# Format A: src/file.ts(12,5): error TS2322: Type 'x' is not assignable...
# Format B: src/file.ts:12:5 - error TS2304: Cannot find name 'y'...
_TSC_LINE_RE = re.compile(
    r"^(?P<file>[^(\s:]+?)(?:\((?P<line1>\d+),(?P<col1>\d+)\)|:(?P<line2>\d+):(?P<col2>\d+)):?\s*(?:-\s*)?error\s+(?P<code>TS\d+):\s*(?P<msg>.+)$",
    re.MULTILINE
)


class CompilerErrorCompactor:
    """Parses, deduplicates, and compacts compiler errors and stack traces."""

    @classmethod
    def is_tsc_output(cls, raw: str) -> bool:
        if not raw:
            return False
        clean = strip_ansi(raw)
        if "Found " in clean and " error" in clean:
            return True
        matches = _TSC_LINE_RE.findall(clean)
        return len(matches) >= 2 or ("error TS" in clean and len(clean.splitlines()) > 5)

    @classmethod
    def compact_tsc(cls, raw: str) -> Tuple[str, bool]:
        """
        Compacts TypeScript compiler error cascades.
        Returns (compacted_output, was_compacted).
        """
        if not raw:
            return raw, False
        raw = strip_ansi(raw)
        lines = raw.splitlines()
        if len(lines) <= 6:
            return raw, False

        errors: List[Dict[str, Any]] = []
        for line in lines:
            m = _TSC_LINE_RE.match(line.strip())
            if m:
                gd = m.groupdict()
                f = gd.get("file", "")
                line_no = gd.get("line1") or gd.get("line2") or "?"
                col_no = gd.get("col1") or gd.get("col2") or "?"
                code = gd.get("code", "")
                msg = gd.get("msg", "")
                errors.append({
                    "file": f,
                    "line": line_no,
                    "col": col_no,
                    "code": code,
                    "msg": msg,
                    "raw_line": line.strip()
                })

        if not errors:
            return raw, False

        total_errors = len(errors)
        unique_files = {e["file"] for e in errors}
        errors_by_code: Dict[str, List[Dict[str, Any]]] = {}
        for e in errors:
            errors_by_code.setdefault(e["code"], []).append(e)

        # If 4 or fewer errors, keep them all
        if total_errors <= 4:
            return raw, False

        # Stash the full raw compiler output
        _, marker = stash_block(raw, source="tsc_errors")

        # Pick top distinct error types (up to 3 distinct codes, up to 2 instances each)
        shown_errors: List[str] = []
        shown_count = 0

        # Sort codes by frequency descending (major cascades first)
        sorted_codes = sorted(errors_by_code.items(), key=lambda kv: len(kv[1]), reverse=True)

        for code, err_list in sorted_codes[:4]:
            shown_errors.append(f"  [{code}] ({len(err_list)} occurrences):")
            for e in err_list[:2]:
                shown_errors.append(f"    • {e['file']}:{e['line']}:{e['col']} — {e['msg']}")
                shown_count += 1
            if len(err_list) > 2:
                shown_errors.append(f"    ... (+{len(err_list) - 2} more {code} errors)")

        folded_count = total_errors - shown_count

        out = [
            f"✗ TypeScript compilation failed: {total_errors} errors across {len(unique_files)} files.",
            f"Primary error cascades ({shown_count} of {total_errors} shown):",
            *shown_errors,
            f"\n[+{folded_count} cascading errors folded — see details in stash]",
            marker
        ]
        return "\n".join(out), True

    @classmethod
    def compact_stacktrace(cls, raw: str) -> Tuple[str, bool]:
        """
        Deduplicates deep recursive or repetitive stack traces.
        """
        if not raw:
            return raw, False
        raw = strip_ansi(raw)

        is_trace = (
            "Traceback (most recent call last):" in raw
            or "Exception in thread " in raw
            or ("\n    at " in raw and "Error" in raw)
            or ("\tat " in raw and "Exception" in raw)
        )
        if not is_trace:
            return raw, False

        lines = raw.splitlines()
        if len(lines) < 20:
            return raw, False

        # Look for repeated frames
        frame_counts: Dict[str, int] = {}
        for line in lines:
            s = line.strip()
            if s.startswith("File ") or s.startswith("at ") or "\tat " in s:
                frame_counts[s] = frame_counts.get(s, 0) + 1

        # Check if there is massive recursion / repetition (> 4 identical frames)
        max_rep = max(frame_counts.values()) if frame_counts else 0
        if max_rep < 4 and len(lines) < 35:
            return raw, False

        # Extract head (first 5 frames) + tail (last 5 frames)
        _, marker = stash_block(raw, source="stacktrace_cascade")
        head = lines[:6]
        tail = lines[-6:]
        omitted = len(lines) - 12

        out = [
            *head,
            f"  ... [TokenSafe: {omitted} deep recursive/middle stack frames omitted]",
            *tail,
            marker
        ]
        return "\n".join(out), True
