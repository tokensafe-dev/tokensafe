"""
TokenSafe Tabular & Data Dump Reducer (v3.3.0).

Compacts large structured data files:
- CSV / TSV files: outputs column schema, types, and top 10 sample rows (-95% tokens)
- SQL dumps: preserves schema (CREATE/ALTER TABLE) while folding repetitive INSERT batches
- HAR (HTTP Archive) files: summarizes network requests (method, URL, status, size) without multi-MB payloads
All folded content is stashed in TokenSafe reversible stash.
"""

import csv
import io
import json
import re
from typing import Tuple, Dict, Any, List, Optional
from src.stash import stash_block


class TabularReducer:
    """Reduces CSV, TSV, SQL dump, and HAR data blobs."""

    @classmethod
    def is_tabular_or_dump(cls, filename: str) -> bool:
        fn = filename.lower()
        return fn.endswith((".csv", ".tsv", ".sql", ".har"))

    @classmethod
    def reduce_csv(cls, content: str, delimiter: Optional[str] = None) -> Tuple[str, bool]:
        """Reduces CSV/TSV content to a compact schema + sample rows."""
        lines = content.splitlines()
        if len(lines) < 25:
            return content, False

        # Detect delimiter if not given
        sample = "\n".join(lines[:10])
        if delimiter is None:
            if "\t" in lines[0] and lines[0].count("\t") > lines[0].count(","):
                delimiter = "\t"
            elif ";" in lines[0] and lines[0].count(";") > lines[0].count(","):
                delimiter = ";"
            else:
                delimiter = ","

        try:
            reader = csv.reader(io.StringIO(content), delimiter=delimiter)
            all_rows = []
            for idx, row in enumerate(reader):
                all_rows.append(row)
                if idx > 50000:
                    break
        except Exception:
            return content, False

        total_rows = len(all_rows)
        if total_rows < 25:
            return content, False

        header = all_rows[0]
        sample_rows = all_rows[1:11]

        # Infer column types from sample
        col_types = []
        for col_idx in range(len(header)):
            vals = [r[col_idx] for r in sample_rows if col_idx < len(r) and r[col_idx].strip()]
            inferred = "string"
            if vals:
                if all(v.isdigit() for v in vals):
                    inferred = "int"
                elif all(re.match(r"^-?\d+(?:\.\d+)?$", v) for v in vals):
                    inferred = "float"
                elif all(v.lower() in ("true", "false", "0", "1") for v in vals):
                    inferred = "bool"
                elif all("-" in v and len(v) in (10, 19, 20, 24) for v in vals):
                    inferred = "date/time"
            col_types.append(inferred)

        hash8, marker = stash_block(content, source="tabular:csv")
        out = [
            f"# [TokenSafe CSV Summary: {total_rows - 1} rows × {len(header)} columns]",
            f"Columns: {', '.join(f'`{h}` ({t})' for h, t in zip(header, col_types))}",
            "",
            "## Sample Data (first 10 rows):",
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(["---"] * len(header)) + " |"
        ]
        for row in sample_rows:
            # Pad row if short
            padded = row + [""] * (len(header) - len(row))
            clean_cells = [c.replace("\n", " ").replace("|", "\\|")[:40] for c in padded[:len(header)]]
            out.append("| " + " | ".join(clean_cells) + " |")

        out.append("")
        out.append(f"... [{total_rows - 11} rows omitted by TokenSafe] ...")
        out.append("")
        out.append(marker)
        return "\n".join(out), True

    @classmethod
    def reduce_sql_dump(cls, content: str) -> Tuple[str, bool]:
        """Folds repetitive INSERT INTO statements while preserving DDL schemas."""
        lines = content.splitlines()
        if len(lines) < 30 or "INSERT INTO" not in content.upper():
            return content, False

        kept_lines = []
        insert_counts: Dict[str, int] = {}
        insert_pattern = re.compile(r"^\s*INSERT\s+INTO\s+[`\"']?([a-zA-Z0-9_.-]+)[`\"']?", re.IGNORECASE)

        for ln in lines:
            m = insert_pattern.match(ln)
            if m:
                tbl = m.group(1)
                insert_counts[tbl] = insert_counts.get(tbl, 0) + 1
            else:
                kept_lines.append(ln)

        total_inserts = sum(insert_counts.values())
        if total_inserts < 15:
            return content, False

        hash8, marker = stash_block(content, source="sql_dump")
        out = [
            f"-- [TokenSafe SQL Dump Summary: {len(kept_lines)} DDL/meta lines | {total_inserts} INSERT statements folded]",
            "-- INSERT batches omitted per table:"
        ]
        for tbl, cnt in insert_counts.items():
            out.append(f"--   • `{tbl}`: {cnt} rows folded")
        out.append(f"-- {marker}")
        out.append("")
        out.extend(kept_lines[:200])  # keep schema DDL
        if len(kept_lines) > 200:
            out.append(f"\n-- ... [{len(kept_lines) - 200} DDL lines omitted] ...")
        return "\n".join(out), True

    @classmethod
    def reduce_har(cls, content: str) -> Tuple[str, bool]:
        """Summarizes HTTP Archive JSON down to request URLs and HTTP statuses."""
        try:
            data = json.loads(content)
        except Exception:
            return content, False

        log = data.get("log")
        if not isinstance(log, dict) or "entries" not in log:
            return content, False

        entries = log.get("entries", [])
        if len(entries) < 5:
            return content, False

        summary_rows = []
        for e in entries:
            req = e.get("request", {})
            resp = e.get("response", {})
            method = req.get("method", "GET")
            url = req.get("url", "")
            status = resp.get("status", 0)
            body_size = resp.get("bodySize", 0)
            time_ms = int(e.get("time", 0))

            # Strip query params if too long
            clean_url = url.split("?")[0] if len(url) > 80 else url
            summary_rows.append(f"- `[{method}]` {status} | {body_size} B | {time_ms}ms | {clean_url}")

        hash8, marker = stash_block(content, source="har_archive")
        out = [
            f"# [TokenSafe HAR Archive Summary: {len(entries)} HTTP transactions]",
            "",
            *summary_rows[:60],
        ]
        if len(summary_rows) > 60:
            out.append(f"... [{len(summary_rows) - 60} requests omitted] ...")
        out.append("")
        out.append(marker)
        return "\n".join(out), True
