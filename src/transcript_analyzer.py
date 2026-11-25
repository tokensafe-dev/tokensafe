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

from src.burn_monitor import estimate_zai_credits, evaluate_burn_warnings


def _parse_iso_ts(ts_str: Optional[str]) -> Optional[datetime.datetime]:
    if not ts_str or not isinstance(ts_str, str):
        return None
    try:
        clean = ts_str.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except Exception:
        return None


def _detect_agent_from_path(file_path: str) -> str:
    norm = file_path.replace("\\", "/")
    if "/.zcode/" in norm or os.path.basename(norm).startswith("model-io-"):
        return "zcode"
    if "/.claude/" in norm:
        return "claude"
    if "/.codex/" in norm:
        return "codex"
    if "/.kimi-code/" in norm:
        return "kimi"
    if "/.cursor/" in norm:
        return "cursor"
    if "/.gemini/" in norm:
        return "antigravity"
    return "auto"


def discover_transcript_paths(
    agent: str = "auto",
    explicit_path: Optional[str] = None,
    limit: int = 20
) -> List[Tuple[str, str, float]]:
    """
    Finds transcript files sorted newest first across Antigravity, ZCode,
    Claude Code, Codex, Kimi, and Cursor.
    Returns list of (agent_type, file_path, mtime).
    """
    if explicit_path:
        p = os.path.abspath(explicit_path)
        if os.path.isfile(p):
            det = _detect_agent_from_path(p)
            return [(det if det != "auto" else "custom", p, os.path.getmtime(p))]
        elif os.path.isdir(p):
            found = []
            for root, _, files in os.walk(p):
                for f in files:
                    if f.endswith(".jsonl"):
                        fp = os.path.join(root, f)
                        det = _detect_agent_from_path(fp)
                        found.append((det if det != "auto" else "custom", fp, os.path.getmtime(fp)))
            found.sort(key=lambda x: x[2], reverse=True)
            return found[:limit]

    home = os.path.expanduser("~")
    candidates: List[Tuple[str, str, float]] = []
    ag = (agent or "auto").lower()

    def _add_glob(ag_label: str, pattern: str, recursive: bool = False) -> None:
        for fp in glob.glob(pattern, recursive=recursive):
            try:
                candidates.append((ag_label, fp, os.path.getmtime(fp)))
            except Exception:
                pass

    # 1. Google Antigravity transcripts
    if ag in ("auto", "antigravity", "gemini"):
        _add_glob("antigravity", os.path.join(home, ".gemini", "antigravity", "brain", "*", ".system_generated", "logs", "transcript.jsonl"))
        _add_glob("antigravity", os.path.join(home, ".gemini", "antigravity", "brain", "*", "transcript.jsonl"))

    # 2. ZCode model-io rollouts
    if ag in ("auto", "zcode", "z.ai"):
        _add_glob("zcode", os.path.join(home, ".zcode", "cli", "rollout", "model-io-*.jsonl"))

    # 3. Claude Code transcripts (subagent rollouts live in <uuid>/subagents/)
    if ag in ("auto", "claude", "claude_code"):
        _add_glob("claude", os.path.join(home, ".claude", "projects", "**", "*.jsonl"), recursive=True)
        _add_glob("claude", os.path.join(home, ".claude", "sessions", "*.jsonl"))

    # 4. Codex transcripts
    if ag in ("auto", "codex"):
        _add_glob("codex", os.path.join(home, ".codex", "sessions", "**", "*.jsonl"), recursive=True)

    # 5. Kimi Code transcripts
    if ag in ("auto", "kimi", "kimi_code"):
        _add_glob("kimi", os.path.join(home, ".kimi-code", "sessions", "**", "*.jsonl"), recursive=True)

    # 6. Cursor transcripts
    if ag in ("auto", "cursor"):
        _add_glob("cursor", os.path.join(home, ".cursor", "projects", "*", "agent-transcripts", "*.jsonl"))

    seen = set()
    unique: List[Tuple[str, str, float]] = []
    for item in candidates:
        if item[1] not in seen:
            seen.add(item[1])
            unique.append(item)

    unique.sort(key=lambda x: x[2], reverse=True)
    return unique[:limit]


def analyze_transcript(file_path: str, agent_type: str = "antigravity") -> Dict[str, Any]:
    """
    Parses a single transcript/rollout JSONL file across Antigravity, ZCode,
    Claude Code, Codex, Kimi, or Cursor.
    """
    if not os.path.isfile(file_path):
        return {"error": f"File not found: {file_path}"}

    detected_agent = agent_type
    if detected_agent in ("auto", ""):
        p_agent = _detect_agent_from_path(file_path)
        detected_agent = p_agent if p_agent != "auto" else "unknown"

    total_lines = 0
    llm_turns = 0
    total_tool_calls = 0
    tool_counts: Dict[str, int] = {}
    disk_spills = 0
    tokensafe_calls = 0
    schema_matched = False
    seen_claude_messages = set()

    timestamps: List[datetime.datetime] = []
    long_turn_gaps = 0
    prev_ts: Optional[datetime.datetime] = None

    total_input = 0
    total_cache_read = 0
    total_cache_write = 0
    total_output = 0
    total_reasoning_chars = 0
    total_visible_chars = 0
    tool_schema_tokens = 0
    model_switches = 0
    prev_model = ""
    last_in = last_cr = last_out = last_reason_chars = last_vis_chars = 0
    est_credits_total = 0.0

    def _track_ts(dt: Optional[datetime.datetime]) -> None:
        nonlocal prev_ts, long_turn_gaps
        if dt is None:
            return
        timestamps.append(dt)
        if prev_ts is not None and (dt - prev_ts).total_seconds() > 60:
            long_turn_gaps += 1
        prev_ts = dt

    def _track_tool(t_name: str) -> None:
        nonlocal total_tool_calls, tokensafe_calls
        total_tool_calls += 1
        tool_counts[t_name] = tool_counts.get(t_name, 0) + 1
        if "tokensafe" in t_name.lower():
            tokensafe_calls += 1

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
                if not isinstance(step, dict):
                    continue

                # ZCode model-io schema
                if step.get("type") == "model_io" or ("request" in step and "response" in step and "model" in step):
                    schema_matched = True
                    if detected_agent in ("antigravity", "unknown"):
                        detected_agent = "zcode"
                    llm_turns += 1
                    _track_ts(_parse_iso_ts(step.get("startedAt") or step.get("completedAt")))
                    m_info = step.get("model")
                    m_id = (m_info.get("modelId") or m_info.get("id") or "") if isinstance(m_info, dict) else str(m_info or "")
                    if m_id:
                        if prev_model and m_id != prev_model:
                            model_switches += 1
                        prev_model = m_id

                    req_body = (step.get("request") or {}).get("body") or {}
                    if not tool_schema_tokens and isinstance(req_body, dict) and isinstance(req_body.get("tools"), list):
                        tool_schema_tokens = len(json.dumps(req_body["tools"], ensure_ascii=False)) // 4

                    resp = step.get("response") or {}
                    for tc in resp.get("toolCalls") or []:
                        if isinstance(tc, dict):
                            _track_tool(str(tc.get("toolName") or tc.get("name") or "unknown"))

                    rtxt = resp.get("reasoningText") or ""
                    ttxt = resp.get("text") or ""
                    total_reasoning_chars += len(rtxt)
                    total_visible_chars += len(ttxt)

                    u = resp.get("usage") or {}
                    if "inputTokens" in u:
                        raw_in = int(u.get("inputTokens") or 0)
                        cr = int(u.get("cacheReadTokens") or u.get("cacheReadInputTokens") or u.get("cache_read_input_tokens") or 0)
                        cw = int(u.get("cacheWriteTokens") or u.get("cacheCreationInputTokens") or u.get("cache_creation_input_tokens") or 0)
                        out_t = int(u.get("outputTokens") or u.get("output_tokens") or 0)
                        if ("cacheReadInputTokens" in u or "cache_read_input_tokens" in u) and "cacheReadTokens" not in u:
                            in_t = raw_in + cr + cw
                            uncached_turn = raw_in
                        else:
                            in_t = raw_in
                            uncached_turn = max(0, in_t - cr)
                    else:
                        raw_in = int(u.get("input_tokens") or 0)
                        cr = int(u.get("cache_read_input_tokens") or 0)
                        cw = int(u.get("cache_creation_input_tokens") or 0)
                        in_t = raw_in + cr + cw
                        uncached_turn = raw_in
                        out_t = int(u.get("output_tokens") or 0)
                    if in_t > 0:
                        last_in, last_cr, last_out = in_t, cr, out_t
                        last_reason_chars, last_vis_chars = len(rtxt), len(ttxt)
                    total_input += in_t
                    total_cache_read += cr
                    total_cache_write += cw
                    total_output += out_t
                    if "glm" in m_id.lower():
                        est_credits_total += estimate_zai_credits(uncached_turn, cr, out_t, model=m_id)
                    continue

                # Claude Code / Kimi JSONL schema
                msg = step.get("message") if isinstance(step.get("message"), dict) else None
                if msg is not None:
                    schema_matched = True
                    # Claude Code writes one JSONL line per content block, all sharing
                    # the same message.id and repeating the identical usage object.
                    # Count the turn and its tokens once per message id.
                    msg_key = str(msg.get("id") or step.get("uuid") or "")
                    first_sighting = not msg_key or msg_key not in seen_claude_messages
                    if msg_key:
                        seen_claude_messages.add(msg_key)
                    if first_sighting:
                        _track_ts(_parse_iso_ts(step.get("timestamp") or step.get("created_at")))
                        m_id = str(msg.get("model") or step.get("model") or "")
                        if m_id:
                            if prev_model and m_id != prev_model:
                                model_switches += 1
                            prev_model = m_id
                        if msg.get("role") == "assistant" or step.get("type") == "assistant":
                            llm_turns += 1
                        u = msg.get("usage")
                        if isinstance(u, dict):
                            raw_in = int(u.get("input_tokens") or 0)
                            cr = int(u.get("cache_read_input_tokens") or 0)
                            cw = int(u.get("cache_creation_input_tokens") or 0)
                            out_t = int(u.get("output_tokens") or 0)
                            tot_in = raw_in + cr + cw
                            if tot_in > 0:
                                last_in, last_cr, last_out = tot_in, cr, out_t
                            total_input += tot_in
                            total_cache_read += cr
                            total_cache_write += cw
                            total_output += out_t
                    content = msg.get("content")
                    if isinstance(content, list):
                        for blk in content:
                            if isinstance(blk, dict) and blk.get("type") == "tool_use":
                                _track_tool(str(blk.get("name") or "unknown"))
                            elif isinstance(blk, dict) and blk.get("type") == "thinking":
                                total_reasoning_chars += len(str(blk.get("thinking") or ""))
                            elif isinstance(blk, dict) and blk.get("type") == "text":
                                total_visible_chars += len(str(blk.get("text") or ""))
                    continue

                # Codex event schema
                payload = step.get("payload") if isinstance(step.get("payload"), dict) else {}
                if step.get("type") == "event_msg" and payload.get("type") == "token_count":
                    schema_matched = True
                    last_u = (payload.get("info") or {}).get("last_token_usage") or {}
                    if isinstance(last_u, dict) and last_u:
                        _track_ts(_parse_iso_ts(step.get("timestamp")))
                        llm_turns += 1
                        in_t = int(last_u.get("input_tokens") or 0)
                        cr = int(last_u.get("cached_input_tokens") or 0)
                        out_t = int(last_u.get("output_tokens") or 0)
                        total_input += in_t
                        total_cache_read += cr
                        total_output += out_t
                        if in_t > 0:
                            last_in, last_cr, last_out = in_t, cr, out_t
                    continue

                # Antigravity schema
                s_type = step.get("type")
                tool_calls = step.get("tool_calls") or []
                if s_type or tool_calls or "created_at" in step:
                    schema_matched = True
                    if s_type == "PLANNER_RESPONSE" or tool_calls:
                        llm_turns += 1
                        # Timestamps only on real turns: status/heartbeat steps would
                        # otherwise fabricate "gaps" during long tool executions.
                        _track_ts(_parse_iso_ts(step.get("created_at") or step.get("timestamp")))

                    if isinstance(tool_calls, list):
                        for tc in tool_calls:
                            if isinstance(tc, dict):
                                _track_tool(str(tc.get("name") or tc.get("tool") or "unknown"))

                content = step.get("content") or ""
                if isinstance(content, str) and "The output was large and was saved to:" in content:
                    disk_spills += 1
    except Exception as e:
        return {"error": f"Failed to parse transcript: {e}"}

    duration_sec = 0.0
    if len(timestamps) >= 2:
        duration_sec = max(0.0, (timestamps[-1] - timestamps[0]).total_seconds())

    batch_factor = round(total_tool_calls / max(1, llm_turns), 2)
    cache_hit_pct = round((total_cache_read / total_input * 100.0), 1) if total_input > 0 else 0.0
    uncached_input = max(0, total_input - total_cache_read)
    last_cache_hit = round((last_cr / last_in * 100.0), 1) if last_in > 0 else 100.0
    burn_warnings = evaluate_burn_warnings({
        "agent": detected_agent,
        "model": prev_model,
        "turns_sampled": llm_turns,
        "last_input_tokens": last_in,
        "last_cache_read_tokens": last_cr,
        "last_output_tokens": last_out,
        "cache_hit_pct": last_cache_hit,
        "model_switches": model_switches,
        "reasoning_chars": last_reason_chars,
        "visible_chars": last_vis_chars,
        "tool_schema_tokens": tool_schema_tokens,
        "active_parallel_sessions": 1,
        "rate_limit_hits": {},
    })

    schema_warning = None
    if not schema_matched:
        schema_warning = "no recognized transcript schema matched; all metrics are zero"
        if detected_agent in ("auto", "", "antigravity"):
            detected_agent = "unknown"

    return {
        "file_path": file_path,
        "agent": detected_agent,
        "schema_warning": schema_warning,
        "model": prev_model,
        "total_steps": total_lines,
        "llm_turns": llm_turns,
        "total_tool_calls": total_tool_calls,
        "tool_batch_factor": batch_factor,
        "tool_breakdown": tool_counts,
        "disk_spills": disk_spills,
        "tokensafe_calls": tokensafe_calls,
        "long_turn_gaps_gt_60s": long_turn_gaps,
        "duration_seconds": round(duration_sec, 1),
        "input_tokens": total_input,
        "cache_read_tokens": total_cache_read,
        "cache_write_tokens": total_cache_write,
        "uncached_input_tokens": uncached_input,
        "output_tokens": total_output,
        "reasoning_chars": total_reasoning_chars,
        "cache_hit_pct": cache_hit_pct,
        "est_credits": round(est_credits_total, 1),
        "tool_schema_tokens": tool_schema_tokens,
        "model_switches": model_switches,
        "burn_warnings": burn_warnings,
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

    lines.append(f"{'#':<3} | {'Agent':<11} | {'Model':<14} | {'Turns':<5} | {'Tools':<5} | {'InTok':<8} | {'Cache%':<6} | {'Credits':<7} | {'Duration':<8} | {'Session Path'}")
    lines.append("-" * 106)

    for i, res in enumerate(results):
        if "error" in res:
            lines.append(f"{i+1:<3} | Error: {res['error']}")
            continue
        agent = str(res.get("agent", "unknown"))[:11]
        model = str(res.get("model") or "-")[:14]
        turns = res.get("llm_turns", 0)
        tools = res.get("total_tool_calls", 0)
        in_tok = res.get("input_tokens", 0)
        in_str = f"{in_tok/1000:.0f}k" if in_tok >= 1000 else (str(in_tok) if in_tok else "-")
        cache_str = f"{res.get('cache_hit_pct', 0.0):.0f}%" if in_tok else "-"
        cred = res.get("est_credits", 0.0)
        cred_str = f"{cred:.1f}" if cred else "-"
        dur = f"{res.get('duration_seconds', 0.0):.0f}s"
        parts = res.get("file_path", "").split(os.sep)
        sess_name = parts[-4] if len(parts) >= 4 and parts[-2] == "logs" else os.path.basename(res.get("file_path", "session"))
        if len(sess_name) > 20:
            sess_name = sess_name[:17] + "..."
        lines.append(f"{i+1:<3} | {agent:<11} | {model:<14} | {turns:<5} | {tools:<5} | {in_str:<8} | {cache_str:<6} | {cred_str:<7} | {dur:<8} | {sess_name}")

    flagged = [r for r in results if r.get("burn_warnings")]
    if flagged:
        lines.append("\n🔥 Burn Warnings:")
        for r in flagged[:5]:
            name = os.path.basename(r.get("file_path", ""))
            for w in r["burn_warnings"][:2]:
                lines.append(f"  • [{r.get('agent')}:{name[:24]}] {w['message']}")

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
