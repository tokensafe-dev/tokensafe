"""
TokenSafe Multi-Agent Transcript Analyzer (v3.5.0).

Inspects offline session transcripts across Google Antigravity, Claude Code,
Codex, Cursor, and Kimi to benchmark wall-clock latency, turn counts,
tool batching factors, prompt-cache decay zones, and disk spills.

Supports:
- Auto-discovery across local agent storage directories (~/.gemini, ~/.claude, ~/.codex).
- Turn & tool call decomposition.
- Antigravity 'output.txt' disk spill detection.
- Inter-turn elapsed time analysis (>60s cache-drop warning).
- Before vs. After A/B task comparison (--compare before.jsonl after.jsonl).
"""

import os
import glob
import json
import datetime
from typing import Dict, Any, List, Optional, Tuple


def _parse_iso_ts(ts_str: Optional[str]) -> Optional[datetime.datetime]:
    if not ts_str:
        return None
    try:
        # Normalize trailing Z or offsets
        clean = ts_str.replace("Z", "+00:00")
        return datetime.datetime.fromisoformat(clean)
    except Exception:
        return None


def discover_transcript_paths(
    agent: str = "auto",
    explicit_path: Optional[str] = None,
    limit: int = 20
) -> List[Tuple[str, str, float]]:
    """
    Finds transcript files sorted newest first.
    Returns list of (agent_type, file_path, mtime).
    """
    if explicit_path:
        p = os.path.abspath(explicit_path)
        if os.path.isfile(p):
            return [("custom", p, os.path.getmtime(p))]
        elif os.path.isdir(p):
            found = []
            for root, _, files in os.walk(p):
                for f in files:
                    if f.endswith(".jsonl"):
                        fp = os.path.join(root, f)
                        found.append(("custom", fp, os.path.getmtime(fp)))
            found.sort(key=lambda x: x[2], reverse=True)
            return found[:limit]

    home = os.path.expanduser("~")
    candidates: List[Tuple[str, str, float]] = []

    # 1. Google Antigravity transcripts
    if agent in ("auto", "antigravity", "gemini"):
        agy_patterns = [
            os.path.join(home, ".gemini", "antigravity", "brain", "*", ".system_generated", "logs", "transcript.jsonl"),
            os.path.join(home, ".gemini", "antigravity", "brain", "*", "transcript.jsonl"),
        ]
        for pat in agy_patterns:
            for fp in glob.glob(pat):
                try:
                    candidates.append(("antigravity", fp, os.path.getmtime(fp)))
                except Exception:
                    pass

    # 2. Claude Code transcripts
    if agent in ("auto", "claude", "claude_code"):
        claude_pattern = os.path.join(home, ".claude", "projects", "*", "transcripts", "*.jsonl")
        for fp in glob.glob(claude_pattern):
            try:
                candidates.append(("claude", fp, os.path.getmtime(fp)))
            except Exception:
                pass
        claude_sess = os.path.join(home, ".claude", "sessions", "*.jsonl")
        for fp in glob.glob(claude_sess):
            try:
                candidates.append(("claude", fp, os.path.getmtime(fp)))
            except Exception:
                pass

    # 3. Codex transcripts
    if agent in ("auto", "codex"):
        codex_pattern = os.path.join(home, ".codex", "sessions", "*.jsonl")
        for fp in glob.glob(codex_pattern):
            try:
                candidates.append(("codex", fp, os.path.getmtime(fp)))
            except Exception:
                pass

    candidates.sort(key=lambda x: x[2], reverse=True)
    return candidates[:limit]


def analyze_transcript(file_path: str, agent_type: str = "antigravity") -> Dict[str, Any]:
    """
    Parses a single transcript.jsonl file and extracts latency, turns, tool calls, and spills.
    """
    if not os.path.isfile(file_path):
        return {"error": f"File not found: {file_path}"}

    total_lines = 0
    llm_turns = 0
    total_tool_calls = 0
    tool_counts: Dict[str, int] = {}
    disk_spills = 0
    tokensafe_calls = 0

    timestamps: List[datetime.datetime] = []
    long_turn_gaps = 0

    prev_ts: Optional[datetime.datetime] = None

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                total_lines += 1
                try:
                    step = json.loads(line)
                except Exception:
                    continue

                # Antigravity schema
                s_type = step.get("type")
                created_at = _parse_iso_ts(step.get("created_at"))
                if created_at:
                    timestamps.append(created_at)
                    if prev_ts:
                        gap_sec = (created_at - prev_ts).total_seconds()
                        if gap_sec > 60:
                            long_turn_gaps += 1
                    prev_ts = created_at

                tool_calls = step.get("tool_calls") or []
                if s_type == "PLANNER_RESPONSE" or tool_calls:
                    llm_turns += 1

                if isinstance(tool_calls, list):
                    total_tool_calls += len(tool_calls)
                    for tc in tool_calls:
                        if isinstance(tc, dict):
                            t_name = tc.get("name") or tc.get("tool") or "unknown"
                            tool_counts[t_name] = tool_counts.get(t_name, 0) + 1
                            if "tokensafe" in t_name.lower():
                                tokensafe_calls += 1

                content = step.get("content") or ""
                if isinstance(content, str) and "The output was large and was saved to:" in content:
                    disk_spills += 1
    except Exception as e:
        return {"error": f"Failed to parse transcript: {e}"}

    duration_sec = 0.0
    if len(timestamps) >= 2:
        duration_sec = max(0.0, (timestamps[-1] - timestamps[0]).total_seconds())

    batch_factor = round(total_tool_calls / max(1, llm_turns), 2)

    return {
        "file_path": file_path,
        "agent": agent_type,
        "total_steps": total_lines,
        "llm_turns": llm_turns,
        "total_tool_calls": total_tool_calls,
        "tool_batch_factor": batch_factor,
        "tool_breakdown": tool_counts,
        "disk_spills": disk_spills,
        "tokensafe_calls": tokensafe_calls,
        "long_turn_gaps_gt_60s": long_turn_gaps,
        "duration_seconds": round(duration_sec, 1)
    }


def compare_transcripts(before_path: str, after_path: str) -> Dict[str, Any]:
    """
    Compares two transcripts (A/B testing before vs after TokenSafe optimization).
    """
    a = analyze_transcript(before_path)
    b = analyze_transcript(after_path)

    if "error" in a:
        return {"error": f"Before transcript error: {a['error']}"}
    if "error" in b:
        return {"error": f"After transcript error: {b['error']}"}

    def _delta(k: str) -> Dict[str, Any]:
        val_a = a.get(k, 0)
        val_b = b.get(k, 0)
        diff = val_b - val_a
        pct = (diff / val_a * 100) if val_a else 0.0
        return {"before": val_a, "after": val_b, "delta": diff, "pct": round(pct, 1)}

    return {
        "before_file": before_path,
        "after_file": after_path,
        "llm_turns": _delta("llm_turns"),
        "total_tool_calls": _delta("total_tool_calls"),
        "tool_batch_factor": _delta("tool_batch_factor"),
        "disk_spills": _delta("disk_spills"),
        "long_turn_gaps_gt_60s": _delta("long_turn_gaps_gt_60s"),
        "duration_seconds": _delta("duration_seconds")
    }


def render_transcripts_report(results: List[Dict[str, Any]]) -> str:
    """Renders formatted terminal and markdown report for a list of analyzed transcripts."""
    if not results:
        return "No transcripts found or analyzed."

    lines = []
    lines.append("╔══════════════════════════════════════════════════════════════════════════════════════════════╗")
    lines.append("║                    📊 TOKENSAFE TRANSCRIPT BENCHMARK ANALYZER                                ║")
    lines.append("╚══════════════════════════════════════════════════════════════════════════════════════════════╝\n")

    lines.append(f"{'#':<3} | {'Agent':<12} | {'Turns':<6} | {'Tools':<6} | {'Batching':<8} | {'Spills':<6} | {'Gaps >60s':<10} | {'Duration':<10} | {'Session Path'}")
    lines.append("-" * 96)

    for i, res in enumerate(results):
        if "error" in res:
            lines.append(f"{i+1:<3} | Error: {res['error']}")
            continue
        agent = res.get("agent", "unknown")[:12]
        turns = res.get("llm_turns", 0)
        tools = res.get("total_tool_calls", 0)
        batch = f"{res.get('tool_batch_factor', 0.0):.2f}x"
        spills = res.get("disk_spills", 0)
        gaps = res.get("long_turn_gaps_gt_60s", 0)
        dur = f"{res.get('duration_seconds', 0.0):.0f}s"
        parts = res.get("file_path", "").split(os.sep)
        sess_name = parts[-4] if len(parts) >= 4 and parts[-2] == "logs" else (parts[-2] if len(parts) >= 2 else "session")
        if len(sess_name) > 18:
            sess_name = sess_name[:15] + "..."
        lines.append(f"{i+1:<3} | {agent:<12} | {turns:<6} | {tools:<6} | {batch:<8} | {spills:<6} | {gaps:<10} | {dur:<10} | {sess_name}")

    lines.append("")
    return "\n".join(lines)


def render_comparison_report(comp: Dict[str, Any]) -> str:
    """Renders before vs after comparison."""
    if "error" in comp:
        return f"Comparison failed: {comp['error']}"

    lines = []
    lines.append("╔══════════════════════════════════════════════════════════════════════════════════╗")
    lines.append("║             🏆 TOKENSAFE TRANSCRIPT A/B COMPARISON REPORT                        ║")
    lines.append("╚══════════════════════════════════════════════════════════════════════════════════╝\n")
    lines.append(f"• Baseline: {comp['before_file']}")
    lines.append(f"• Optimized: {comp['after_file']}\n")

    headers = f"{'Metric':<26} | {'Baseline':<12} | {'TokenSafe':<12} | {'Improvement':<14}"
    lines.append(headers)
    lines.append("-" * len(headers))

    metrics = [
        ("LLM Turns", comp["llm_turns"]),
        ("Total Tool Calls", comp["total_tool_calls"]),
        ("Tool Batch Factor", comp["tool_batch_factor"]),
        ("Disk Spills (output.txt)", comp["disk_spills"]),
        ("Turn Gaps > 60s", comp["long_turn_gaps_gt_60s"]),
        ("Duration (seconds)", comp["duration_seconds"])
    ]

    for label, m in metrics:
        b_val = m["before"]
        a_val = m["after"]
        pct = m["pct"]
        pct_str = f"{pct:+.1f}%" if pct != 0 else "0.0%"
        lines.append(f"{label:<26} | {b_val:<12} | {a_val:<12} | {pct_str:<14}")

    lines.append("")
    return "\n".join(lines)
