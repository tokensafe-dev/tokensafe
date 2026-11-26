"""Deterministic protocol-mechanics measurements (no provider calls).

The agent matrix measures what a model chooses to do. These numbers measure the
runtime itself, so a reader can separate "the protocol is cheap" from "the model
happened to take few steps":

* discovery payload — observe L2 vs the same fact obtained by search + read;
* slicing — full-file getSource vs one-symbol getSource;
* verification — one in-process probe vs one oracle/test-suite run;
* safety — a stale OCC version and a broken edit are rejected.

Everything runs against a throwaway copy of the benchmark sandbox, so the numbers
are reproducible on a laptop without an API key.
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.bench import go_target, react_target


def _ms(fn, repeats: int) -> Dict[str, float]:
    samples: List[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000)
    return {
        "median_ms": round(statistics.median(samples), 2),
        "min_ms": round(min(samples), 2),
        "max_ms": round(max(samples), 2),
        "samples": repeats,
    }


def _chars(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False))


def measure_go(task_id: str = "product-create-status", repeats: int = 15) -> Dict[str, Any]:
    ws = go_target.GoWorkspace(task_id)
    probe = {"target": "POST /api/products", "action": "probe", "payload": {"title": "probe", "price": 1}}
    source_path = go_target.SANDBOX_FILE
    try:
        ws.oumv("oumv/verify", probe)  # warm the helper and the Go build cache
        full = ws.oumv("oumv/getSource", {"path": source_path})
        sliced = ws.oumv("oumv/getSource", {"path": source_path, "symbol": "NewVanillaApp"})
        l2 = ws.oumv("oumv/observe", {"level": "L2", "target": "POST /api/products"})
        l3 = ws.oumv("oumv/observe", {"level": "L3", "target": "POST /api/products"})
        hits = ws.search_files("StatusOK")
        hit_line = 0
        if hits.get("hits"):
            hit_line = int(hits["hits"][0].split(":")[1])
        window = ws.read_file(source_path, max(1, hit_line - 10), hit_line + 40)

        # A stale version must be rejected before anything is written.
        stale = ws.oumv(
            "oumv/applyEdit",
            {
                "path": source_path,
                "replacement": full.get("source") or "",
                "expectedVersion": "sha256:" + "0" * 64,
            },
        )
        # A syntactically broken replacement must be rejected by the pre-flight.
        broken = ws.oumv(
            "oumv/applyEdit",
            {
                "path": source_path,
                "replacement": "package vanilla_gin\nfunc broken( {\n",
                "expectedVersion": full.get("version") or "",
            },
        )

        verify = _ms(lambda: ws.oumv("oumv/verify", probe), repeats)
        verify_sample = ws.oumv("oumv/verify", probe)
        test_run = _ms(
            lambda: ws.run_command("go test ./benchmark -run TestGroundTruthOracleVanilla -count=1"),
            3,
        )
        return {
            "target": "go",
            "task": task_id,
            "discovery": {
                "oumv_l2_chars": _chars(l2),
                "oumv_l3_chars": _chars(l3),
                "oumv_handler_chars": len(str((l3.get("data") or {}).get("source") or "")),
                "baseline_search_chars": _chars(hits),
                "baseline_read_window_chars": _chars(window),
                "baseline_total_chars": _chars(hits) + _chars(window),
            },
            "slicing": {
                "full_file_chars": _chars(full),
                "symbol_chars": _chars(sliced),
                "symbol": sliced.get("symbol"),
                "start_line": sliced.get("startLine"),
                "end_line": sliced.get("endLine"),
            },
            "verification": {
                "oumv_verify_end_to_end": verify,
                "oumv_verify_reported_in_process_ms": verify_sample.get("durationMs"),
                "oracle_run": test_run,
                "speedup": round(test_run["median_ms"] / verify["median_ms"], 1) if verify["median_ms"] else None,
            },
            "safety": {
                "stale_version_rejected": bool((stale.get("error") or {}).get("code") == -32001),
                "broken_edit_rejected": bool((broken.get("error") or {}).get("code") == -32002),
                "stale_error": (stale.get("error") or {}).get("code"),
                "broken_error": (broken.get("error") or {}).get("code"),
            },
        }
    finally:
        ws.cleanup()


def measure_react(task_id: str = "search-filter", repeats: int = 15) -> Dict[str, Any]:
    ws = react_target.ReactWorkspace(task_id)
    store = "sandboxes/react-task-manager/src/store.ts"
    try:
        full = ws.oumv("oumv/getSource", {"path": store})
        sliced = ws.oumv("oumv/getSource", {"path": store, "symbol": "selectFilteredTasks"})
        l1 = ws.oumv("oumv/observe", {"level": "L1"})
        hits = ws.search_files("searchQuery")
        hit_line = 0
        if hits.get("hits"):
            hit_line = int(hits["hits"][0].split(":")[1].split(":")[0]) if ":" in hits["hits"][0] else 0
        window = ws.read_file(store, max(1, hit_line - 10), hit_line + 40)
        stale = ws.oumv(
            "oumv/applyEdit",
            {"path": store, "replacement": full.get("source") or "", "expectedVersion": "sha256:" + "0" * 64},
        )
        oracle = _ms(lambda: ws.run_command("npx tsx src/bin/oracle-runner.ts"), 3)
        read = _ms(lambda: ws.oumv("oumv/getSource", {"path": store, "symbol": "selectFilteredTasks"}), repeats)
        return {
            "target": "react",
            "task": task_id,
            "discovery": {
                "oumv_l1_chars": _chars(l1),
                "baseline_search_chars": _chars(hits),
                "baseline_read_window_chars": _chars(window),
                "baseline_total_chars": _chars(hits) + _chars(window),
            },
            "slicing": {
                "full_file_chars": _chars(full),
                "symbol_chars": _chars(sliced),
                "symbol": sliced.get("symbol"),
                "start_line": sliced.get("startLine"),
                "end_line": sliced.get("endLine"),
            },
            "verification": {
                "oumv_get_source": read,
                "oracle_run": oracle,
                "speedup": round(oracle["median_ms"] / read["median_ms"], 1) if read["median_ms"] else None,
            },
            "safety": {
                "stale_version_rejected": bool((stale.get("error") or {}).get("code") == -32001),
                "stale_error": (stale.get("error") or {}).get("code"),
            },
        }
    finally:
        ws.cleanup()


def render_markdown(results: List[Dict[str, Any]]) -> str:
    lines = ["# OUMV protocol mechanics (deterministic, no model)", ""]
    for row in results:
        lines += [f"## {row['target']} (`{row['task']}`)", ""]
        discovery = row["discovery"]
        slicing = row["slicing"]
        lines += [
            "| Measurement | Value |",
            "| :--- | ---: |",
            f"| OUMV observe payload | {discovery.get('oumv_l3_chars') or discovery.get('oumv_l2_chars') or discovery.get('oumv_l1_chars')} chars |",
            *(
                [f"| …of which the handler body | {discovery['oumv_handler_chars']} chars |"]
                if discovery.get("oumv_handler_chars")
                else []
            ),
            f"| Baseline search + read window | {discovery['baseline_total_chars']} chars |",
            f"| `getSource` full file | {slicing['full_file_chars']} chars |",
            f"| `getSource` one symbol | {slicing['symbol_chars']} chars |",
            "",
        ]
        verification = row["verification"]
        timing_label = (
            "OUMV probe (end-to-end, one process)"
            if verification.get("oumv_verify_end_to_end")
            else "getSource, one symbol (end-to-end)"
        )
        timing_value = verification.get("oumv_verify_end_to_end") or verification.get("oumv_get_source") or {}
        lines += [
            "| Verification | Median |",
            "| :--- | ---: |",
            f"| {timing_label} | {timing_value.get('median_ms')} ms |",
            f"| Oracle / test suite run | {verification['oracle_run']['median_ms']} ms |",
            "",
        ]
        safety = row["safety"]
        lines += [
            f"Safety: stale `expectedVersion` rejected: **{safety['stale_version_rejected']}**"
            + (f"; broken edit rejected: **{safety['broken_edit_rejected']}**" if "broken_edit_rejected" in safety else "")
            + ".",
            "",
        ]
    return "\n".join(lines)


def run(repeats: int = 15) -> List[Dict[str, Any]]:
    return [measure_go(repeats=repeats), measure_react(repeats=repeats)]
