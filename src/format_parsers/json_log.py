"""
TokenSafe Structured JSON & NDJSON Log Reducer (v3.3.0).

Filters high-volume JSON log streams (Winston, Bunyan, Zap, Logstash, Docker JSON logs):
- Summarizes total log events by severity level (INFO, WARN, ERROR, DEBUG)
- Drops noisy repetitive DEBUG/INFO lines while preserving all ERROR and WARN events
- Stashes raw log stream into TokenSafe reversible stash with a hash anchor.
"""

import json
from typing import Tuple, Dict, Any, List
from src.stash import stash_block


class JSONLogReducer:
    """Parses and compresses structured NDJSON log streams."""

    @classmethod
    def is_ndjson_log(cls, content: str) -> bool:
        lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
        if len(lines) < 10:
            return False
        # Check first 3 lines
        valid_json = 0
        for ln in lines[:3]:
            if ln.startswith("{") and ln.endswith("}"):
                try:
                    obj = json.loads(ln)
                    if isinstance(obj, dict) and any(k in obj for k in ("level", "msg", "message", "timestamp", "time")):
                        valid_json += 1
                except Exception:
                    pass
        return valid_json >= 2

    @classmethod
    def reduce(cls, content: str) -> Tuple[str, bool]:
        """Reduces JSON log stream down to error/warn events and count summary."""
        lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
        if len(lines) < 15:
            return content, False

        counts = {"DEBUG": 0, "INFO": 0, "WARN": 0, "ERROR": 0, "OTHER": 0}
        important_logs = []

        for ln in lines:
            try:
                obj = json.loads(ln)
                if not isinstance(obj, dict):
                    continue
                lvl = str(obj.get("level") or obj.get("severity") or "OTHER").upper()
                if "ERR" in lvl or lvl in ("50", "FATAL"):
                    counts["ERROR"] += 1
                    important_logs.append((lvl, obj))
                elif "WARN" in lvl or lvl == "40":
                    counts["WARN"] += 1
                    important_logs.append((lvl, obj))
                elif "INFO" in lvl or lvl == "30":
                    counts["INFO"] += 1
                elif "DEBUG" in lvl or lvl == "20" or lvl == "10":
                    counts["DEBUG"] += 1
                else:
                    counts["OTHER"] += 1
            except Exception:
                pass

        total_parsed = sum(counts.values())
        if total_parsed < 10:
            return content, False

        hash8, marker = stash_block(content, source="json_logs")
        counts_str = ", ".join(f"{k}: {v}" for k, v in counts.items() if v > 0)
        out = [
            f"# [TokenSafe JSON Logs: {total_parsed} events ({counts_str})]",
            ""
        ]

        if important_logs:
            out.append(f"## Errors & Warnings ({len(important_logs)} events):")
            for lvl, obj in important_logs[:20]:
                msg = obj.get("msg") or obj.get("message") or str(obj)
                ts = obj.get("time") or obj.get("timestamp") or ""
                ts_str = f"[{ts}] " if ts else ""
                out.append(f"  ✗ {ts_str}{lvl}: {msg}")
        else:
            out.append("✓ No errors or warnings found in log stream.")

        out.append("")
        out.append(marker)
        return "\n".join(out), True
