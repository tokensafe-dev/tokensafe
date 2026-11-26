"""Runs benchmark arms and writes the measured report."""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.bench import go_target, react_target
from src.bench.driver import BudgetExceeded, DeepSeekClient, run_agent


def run_one(
    arm: str,
    task_id: str,
    model: str = "deepseek-chat",
    max_steps: int = 14,
    budget_usd: float = 1.0,
) -> Dict[str, Any]:
    ws = go_target.GoWorkspace(task_id)
    started = time.time()
    try:
        before = ws.oracle()
        client = DeepSeekClient(model=model, temperature=0.0)
        if arm == "oumv":
            system, tools = go_target.SYSTEM_OUMV, go_target.OUMV_TOOLS
        elif arm == "tokensafe":
            system, tools = go_target.SYSTEM_TOKENSAFE, go_target.TOKENSAFE_TOOLS
        else:
            system, tools = go_target.SYSTEM_BASELINE, go_target.BASE_TOOLS
        registry = go_target.registry(ws, arm)

        result = run_agent(
            client=client,
            system_prompt=system,
            task_prompt=go_target.task_prompt(task_id),
            tools=tools,
            registry=registry,
            max_steps=max_steps,
            budget_usd=budget_usd,
            after_step=lambda _i: ws.oracle(),
        )
        after = ws.oracle()
        return {
            "arm": arm,
            "task": task_id,
            "oracle_before": {"passed": before[0], "failed": before[1]},
            "oracle_after": {"passed": after[0], "failed": after[1]},
            "solved": after[0] == 12,
            "wall_clock_s": round(time.time() - started, 1),
            **result,
        }
    except BudgetExceeded as exc:
        return {
            "arm": arm,
            "task": task_id,
            "solved": False,
            "stop_reason": "budget_exceeded",
            "error": str(exc),
            "usage": getattr(exc, "usage", {}),
            "wall_clock_s": round(time.time() - started, 1),
        }
    finally:
        ws.cleanup()


def run_matrix(
    tasks: List[str],
    arms: List[str],
    runs: int = 1,
    model: str = "deepseek-chat",
    max_steps: int = 14,
    budget_usd: float = 1.0,
) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    spent = 0.0
    aborted: Optional[str] = None
    for run_idx in range(1, runs + 1):
        for task_id in tasks:
            for arm in arms:
                if spent >= budget_usd:
                    aborted = f"global budget ${budget_usd} exhausted after ${spent:.4f}"
                    break
                per_run_budget = max(budget_usd - spent, 0.01)
                row = run_one(arm, task_id, model=model, max_steps=max_steps, budget_usd=per_run_budget)
                row["run"] = run_idx
                results.append(row)
                spent += float((row.get("usage") or {}).get("usd") or 0.0)
            if aborted:
                break
        if aborted:
            break
    return {
        "target": "go",
        "tasks": tasks,
        "arms": arms,
        "runs": runs,
        "model": model,
        "budget_usd": budget_usd,
        "spent_usd": round(spent, 6),
        "aborted": aborted,
        "results": results,
        "provenance": _provenance("go", tasks, arms),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }



def run_react_one(
    task_id: str,
    arm: str = "baseline",
    model: str = "deepseek-chat",
    max_steps: int = 14,
    budget_usd: float = 1.0,
) -> Dict[str, Any]:
    ws = react_target.ReactWorkspace(task_id)
    started = time.time()
    try:
        before = ws.oracle()
        client = DeepSeekClient(model=model, temperature=0.0)
        if arm == "oumv":
            system, tools = react_target.SYSTEM_OUMV, react_target.OUMV_TOOLS
        elif arm == "tokensafe":
            system, tools = react_target.SYSTEM_TOKENSAFE, react_target.TOKENSAFE_TOOLS
        else:
            system, tools = react_target.SYSTEM, react_target.TOOLS
        result = run_agent(
            client=client,
            system_prompt=system,
            task_prompt=react_target.task_prompt(task_id),
            tools=tools,
            registry=react_target.registry(ws, arm),
            max_steps=max_steps,
            budget_usd=budget_usd,
            after_step=lambda _i: ws.oracle(),
        )
        after = ws.oracle()
        return {
            "arm": arm,
            "task": task_id,
            "oracle_before": {"passed": before[0], "failed": before[1]},
            "oracle_after": {"passed": after[0], "failed": after[1]},
            "solved": after[0] == 12,
            "wall_clock_s": round(time.time() - started, 1),
            **result,
        }
    except BudgetExceeded as exc:
        return {"arm": arm, "task": task_id, "solved": False,
                "stop_reason": "budget_exceeded", "error": str(exc),
                "usage": getattr(exc, "usage", {}),
                "wall_clock_s": round(time.time() - started, 1)}
    finally:
        ws.cleanup()


def run_react_matrix(
    tasks: List[str],
    arms: List[str],
    runs: int = 1,
    model: str = "deepseek-chat",
    max_steps: int = 14,
    budget_usd: float = 1.0,
) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    spent = 0.0
    aborted: Optional[str] = None
    for run_idx in range(1, runs + 1):
        for task_id in tasks:
            for arm in arms:
                if spent >= budget_usd:
                    aborted = f"global budget ${budget_usd} exhausted after ${spent:.4f}"
                    break
                row = run_react_one(task_id, arm, model=model, max_steps=max_steps,
                                    budget_usd=max(budget_usd - spent, 0.01))
                row["run"] = run_idx
                results.append(row)
                spent += float((row.get("usage") or {}).get("usd") or 0.0)
            if aborted:
                break
        if aborted:
            break
    return {
        "target": "react",
        "tasks": tasks,
        "arms": arms,
        "runs": runs,
        "model": model,
        "budget_usd": budget_usd,
        "spent_usd": round(spent, 6),
        "aborted": aborted,
        "results": results,
        "note": "React arm runs the shipped static controller over stdio (no browser tab).",
        "provenance": _provenance("react", tasks, arms),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _provenance(target: str, tasks: List[str], arms: List[str]) -> Dict[str, Any]:
    """Frozen prompts + harness revision, so a report can be audited later."""
    import hashlib
    import subprocess

    module = react_target if target == "react" else go_target
    harness_repo = Path(__file__).resolve().parents[2]
    systems = {
        "baseline": getattr(module, "SYSTEM_BASELINE", getattr(module, "SYSTEM", "")),
        "tokensafe": getattr(module, "SYSTEM_TOKENSAFE", ""),
        "oumv": getattr(module, "SYSTEM_OUMV", ""),
    }
    prompts = {task: module.task_prompt(task) for task in tasks}
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(harness_repo), capture_output=True, text=True, timeout=10
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"], cwd=str(harness_repo), capture_output=True, text=True, timeout=10
            ).stdout.strip()
        )
    except Exception:
        commit, dirty = "", False
    return {
        "prompt_sha256": {task: hashlib.sha256(prompt.encode()).hexdigest() for task, prompt in prompts.items()},
        "system_sha256": {
            arm: hashlib.sha256(systems.get(arm, "").encode()).hexdigest() for arm in arms
        },
        "harness_commit": commit,
        "harness_dirty": dirty,
        "price_snapshot": "2026-11-26",
    }


def summarize(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: Dict[str, Dict[str, Any]] = {}
    for result in report["results"]:
        key = result["arm"]
        row = rows.setdefault(
            key,
            {"arm": key, "runs": 0, "solved": 0, "input": 0, "output": 0, "usd": 0.0, "steps": 0, "seconds": 0.0},
        )
        row["runs"] += 1
        row["solved"] += 1 if result.get("solved") else 0
        usage = result.get("usage") or {}
        row["input"] += int(usage.get("input_tokens") or 0)
        row["output"] += int(usage.get("output_tokens") or 0)
        row["usd"] += float(usage.get("usd") or 0.0)
        row["steps"] += len(result.get("steps") or [])
        row["seconds"] += float(result.get("wall_clock_s") or 0.0)
    for row in rows.values():
        row["mean_steps"] = round(row["steps"] / row["runs"], 1) if row["runs"] else 0.0
        row["mean_seconds"] = round(row["seconds"] / row["runs"], 1) if row["runs"] else 0.0
        row["mean_input"] = round(row["input"] / row["runs"]) if row["runs"] else 0
        row["mean_output"] = round(row["output"] / row["runs"]) if row["runs"] else 0
    return sorted(rows.values(), key=lambda r: r["arm"])


def render_markdown(report: Dict[str, Any]) -> str:
    target = report.get("target", "go")
    oracle = (
        "the 12-assertion TaskManagerOracle (`npx tsx src/bin/oracle-runner.ts`)"
        if target == "react"
        else "the real 12-assertion oracle (`go test ./benchmark -run TestGroundTruthOracleVanilla`)"
    )
    sandbox = "the `agent-dom` React benchmark sandbox" if target == "react" else "the `tokensafe-backend/go` module"
    lines = [
        f"# OUMV benchmark — {target} sandbox (measured)",
        "",
        f"> Model: `{report['model']}` · runs: {report['runs']} · budget: ${report['budget_usd']} · "
        f"spent: ${report['spent_usd']} · {report['generated_at']}",
        "",
        "## Methodology",
        "",
        f"- Each arm starts from a throwaway copy of {sandbox} with one deliberate regression.",
        f"- Correctness is {oracle}, re-run after every agent step.",
        "- Token and cost numbers come from the provider `usage` payload; prices are a pinned snapshot printed in the JSON artifact.",
        "- `baseline` uses file/command tools only; `oumv` gets the same tools plus observe/getSource/applyEdit/understand served by the shipped OUMV adapter.",
        "",
        "## Results",
        "",
        "| Arm | Runs | Solved | Mean steps | Mean wall clock | Mean input tokens | Mean output tokens | Cost |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in summarize(report):
        lines.append(
            f"| `{row['arm']}` | {row['runs']} | {row['solved']}/{row['runs']} | {row['mean_steps']} | "
            f"{row['mean_seconds']}s | {row['mean_input']:,} | {row['mean_output']:,} | ${row['usd']:.4f} |"
        )
    if report.get("aborted"):
        lines += ["", f"> Stopped early: {report['aborted']}"]
    rows = {row["arm"]: row for row in summarize(report)}
    if "baseline" in rows and "oumv" in rows:
        baseline, oumv = rows["baseline"], rows["oumv"]
        def delta(before: float, after: float) -> str:
            if not before:
                return "—"
            return f"{(after - before) / before * 100:+.1f}%"
        lines += [
            "",
            "## OUMV vs baseline",
            "",
            "| Metric | Baseline | OUMV | Delta |",
            "| :--- | ---: | ---: | ---: |",
            f"| Solved | {baseline['solved']}/{baseline['runs']} | {oumv['solved']}/{oumv['runs']} | — |",
            f"| Mean input tokens | {baseline['mean_input']:,} | {oumv['mean_input']:,} | {delta(baseline['mean_input'], oumv['mean_input'])} |",
            f"| Mean output tokens | {baseline['mean_output']:,} | {oumv['mean_output']:,} | {delta(baseline['mean_output'], oumv['mean_output'])} |",
            f"| Mean steps | {baseline['mean_steps']} | {oumv['mean_steps']} | {delta(baseline['mean_steps'], oumv['mean_steps'])} |",
            f"| Mean wall clock | {baseline['mean_seconds']}s | {oumv['mean_seconds']}s | {delta(baseline['mean_seconds'], oumv['mean_seconds'])} |",
            f"| Cost | ${baseline['usd']:.4f} | ${oumv['usd']:.4f} | {delta(baseline['usd'], oumv['usd'])} |",
        ]
    lines += ["", "## Per-run detail", "", "| Run | Arm | Task | Oracle before | Oracle after | Stop | Steps |", "| ---: | :--- | :--- | :--- | :--- | :--- | ---: |"]
    for result in report["results"]:
        before = result.get("oracle_before") or {}
        after = result.get("oracle_after") or {}
        lines.append(
            f"| {result.get('run')} | `{result['arm']}` | {result['task']} | "
            f"{before.get('passed', '—')}/12 | {after.get('passed', '—')}/12 | "
            f"{result.get('stop_reason', '—')} | {len(result.get('steps') or [])} |"
        )
    return "\n".join(lines) + "\n"


def write_artifacts(report: Dict[str, Any], out_dir: Optional[Path] = None) -> Dict[str, str]:
    from src.paths import resolve_data_dir

    base = Path(out_dir) if out_dir else Path(resolve_data_dir()) / "bench"
    base.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    target = report.get("target", "go")
    json_path = base / f"{target}-{stamp}.json"
    md_path = base / f"{target}-{stamp}.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(md_path)}
