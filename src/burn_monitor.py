"""
TokenSafe Multi-Agent Live Burn Monitor & Cache-Break ROI Engine.

Provides:
1. Provider/model token multipliers and Z.ai Peak-Hours awareness.
2. Cache-aware ROI gate (evaluate_fold_roi) shared by proxy folding stages
   (result_ledger.py, thinking_fold.py) so mutating a warm KV-cache prefix
   only fires when expected future savings outweigh the cache-break cost.
3. Fast tail-reading Live Burn Monitor across ZCode, Claude Code, Antigravity,
   Codex, Cursor, and Kimi transcripts/logs (works with or without the proxy).
"""

import datetime
import glob
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from src.tokenizer import count_tokens

# (M_input, M_cache, M_output) per 10,000 tokens (Z.ai) or relative cost ratio.
_MODEL_MULTIPLIERS: List[Tuple[str, Tuple[float, float, float]]] = [
    ("glm-5.3-flash", (1.15, 0.28, 4.0)),
    ("glm-5.3", (6.9, 1.7, 24.0)),
    ("glm-5.4", (4.6, 1.1, 16.0)),
    ("glm-5", (4.6, 1.1, 16.0)),
    ("claude", (1.25, 0.10, 5.0)),
    ("deepseek", (1.0, 0.10, 2.0)),
    ("gpt-5", (1.0, 0.10, 4.0)),
    ("gpt-4", (1.0, 0.50, 4.0)),
    ("o1", (1.0, 0.50, 4.0)),
    ("o3", (1.0, 0.25, 4.0)),
    ("o4", (1.0, 0.25, 4.0)),
    ("gemini", (1.0, 0.25, 4.0)),
]

_DEFAULT_MULTIPLIERS = (1.0, 0.25, 4.0)
_ZAI_MODELS = ("glm-5.3", "glm-5.4", "glm-5.3-flash", "glm-5")
_RATE_LIMIT_CODES = ("1310", "1308", "1302")


def get_model_multipliers(model: str = "", provider: str = "") -> Tuple[float, float, float]:
    """Returns (M_input, M_cache, M_output) multipliers for a model/provider."""
    m = (model or "").strip().lower()
    p = (provider or "").strip().lower()
    for prefix, mults in _MODEL_MULTIPLIERS:
        if prefix in m:
            return mults
    if "anthropic" in p:
        return (1.25, 0.10, 5.0)
    if "deepseek" in p:
        return (1.0, 0.10, 2.0)
    if "openai" in p or "gemini" in p:
        return (1.0, 0.25, 4.0)
    return _DEFAULT_MULTIPLIERS


def is_zai_peak_hour(now: Optional[datetime.datetime] = None) -> bool:
    """True during Z.ai Peak Hours: Mon-Fri 14:00-18:00 SST (UTC+8)."""
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    sst = now.astimezone(datetime.timezone(datetime.timedelta(hours=8)))
    return sst.weekday() < 5 and 14 <= sst.hour < 18


def estimate_zai_credits(
    uncached_input: int,
    cache_read: int,
    output_tokens: int,
    model: str = "glm-5.4",
    peak: Optional[bool] = None,
) -> float:
    """Estimates Z.ai Coding Plan credit consumption using the official formula."""
    m_in, m_cache, m_out = get_model_multipliers(model)
    raw = (
        max(0, uncached_input) * m_in
        + max(0, cache_read) * m_cache
        + max(0, output_tokens) * m_out
    ) / 10000.0
    if peak is None:
        peak = is_zai_peak_hour()
    return round(raw if peak else raw * 0.5, 2)


def _msg_tokens(msg: Any) -> int:
    if not isinstance(msg, dict):
        return 0
    total = 0
    content = msg.get("content")
    if isinstance(content, str):
        total += count_tokens(content)
    elif isinstance(content, list):
        for blk in content:
            if isinstance(blk, dict):
                txt = blk.get("text") or blk.get("thinking") or ""
                if isinstance(txt, str):
                    total += count_tokens(txt)
    for tc in msg.get("tool_calls") or []:
        if isinstance(tc, dict):
            fn = tc.get("function") or {}
            total += count_tokens(str(fn.get("name") or "") + str(fn.get("arguments") or ""))
    for tb in msg.get("_tokensafe_thinking") or []:
        if isinstance(tb, dict):
            total += count_tokens(str(tb.get("text") or tb.get("thinking") or ""))
    return total


def evaluate_fold_roi(
    messages: List[Dict[str, Any]],
    earliest_fold_idx: int,
    saved_tokens: int,
    model: str = "",
    provider: str = "",
    cache_warm: Optional[bool] = None,
    remaining_turns: int = 8,
    force_fold: bool = False,
) -> Dict[str, Any]:
    """Evaluates whether mutating history at `earliest_fold_idx` beats the KV-cache break cost.

    Compares:
      cache_break_cost = suffix_tokens * (M_in - M_cache)
      expected_savings = saved_tokens * M_cache * remaining_turns
    Cold prefixes (force_fold=True, cache_warm=False, or small <20k sessions when
    cache_warm is unknown) fold unconditionally.
    """
    m_in, m_cache, _ = get_model_multipliers(model, provider)
    suffix_tokens = sum(_msg_tokens(m) for m in messages[earliest_fold_idx + 1 :])
    total_tokens = sum(_msg_tokens(m) for m in messages)

    if force_fold:
        is_warm = False
    elif cache_warm is not None:
        is_warm = bool(cache_warm)
    else:
        is_warm = total_tokens >= 20000

    cache_break_cost = round(suffix_tokens * max(0.0, m_in - m_cache), 2) if is_warm else 0.0
    expected_savings = round(max(0, saved_tokens) * max(0.05, m_cache) * max(1, remaining_turns), 2)
    should_fold = (not is_warm) or (expected_savings > cache_break_cost)

    return {
        "should_fold": should_fold,
        "cache_warm": is_warm,
        "suffix_tokens": suffix_tokens,
        "total_tokens": total_tokens,
        "saved_tokens": saved_tokens,
        "cache_break_cost": cache_break_cost,
        "expected_savings": expected_savings,
        "m_in": m_in,
        "m_cache": m_cache,
    }


def _read_tail_lines(path: str, max_bytes: int = 65536) -> List[str]:
    """Reads up to the last `max_bytes` of a file safely and fast."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                chunk = f.read()
                nl = chunk.find(b"\n")
                if nl != -1:
                    chunk = chunk[nl + 1 :]
            else:
                chunk = f.read()
        return [ln for ln in chunk.decode("utf-8", errors="replace").splitlines() if ln.strip()]
    except Exception:
        return []


def _parse_iso(ts: Optional[str]) -> Optional[float]:
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _scan_zcode_tail(home: str, session_id: str = "", explicit_path: str = "") -> Optional[Dict[str, Any]]:
    rollout_dir = os.path.join(home, ".zcode", "cli", "rollout")
    if explicit_path and os.path.isfile(explicit_path) and "model-io-" in os.path.basename(explicit_path):
        candidates = [explicit_path]
    elif os.path.isdir(rollout_dir):
        candidates = glob.glob(os.path.join(rollout_dir, "model-io-*.jsonl"))
    else:
        return None
    if not candidates:
        return None

    now = time.time()
    active_parallel = 0
    best_path = None
    best_mtime = 0.0
    for fp in candidates:
        try:
            mt = os.path.getmtime(fp)
            if now - mt <= 90.0:
                active_parallel += 1
            if session_id and session_id in os.path.basename(fp):
                best_path = fp
                best_mtime = mt
                break
            if mt > best_mtime:
                best_mtime = mt
                best_path = fp
        except Exception:
            continue

    if not best_path or (now - best_mtime > 3600 and not explicit_path):
        return None

    lines = _read_tail_lines(best_path, max_bytes=131072)
    models_seen: List[str] = []
    last_in = last_cr = last_out = last_reason_chars = last_text_chars = 0
    prev_model = ""
    model_switches = 0
    zero_usage = 0
    tool_schema_tokens = 0
    turns = 0

    for ln in lines[-20:]:
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        turns += 1
        m_info = obj.get("model")
        m_id = (m_info.get("modelId") or m_info.get("id") or "") if isinstance(m_info, dict) else str(m_info or "")
        if m_id:
            if prev_model and m_id != prev_model:
                model_switches += 1
            prev_model = m_id
            models_seen.append(m_id)
        resp = obj.get("response") or {}
        u = resp.get("usage") or {}
        in_t = int(u.get("inputTokens") or u.get("input_tokens") or 0)
        cr = int(u.get("cacheReadTokens") or u.get("cache_read_input_tokens") or 0)
        out_t = int(u.get("outputTokens") or u.get("output_tokens") or 0)
        if in_t == 0:
            zero_usage += 1
        else:
            last_in = in_t
            last_cr = cr
            last_out = out_t
        last_reason_chars = len(resp.get("reasoningText") or "")
        last_text_chars = len(resp.get("text") or "")
        req_body = (obj.get("request") or {}).get("body") or {}
        if isinstance(req_body, dict) and isinstance(req_body.get("tools"), list) and not tool_schema_tokens:
            tool_schema_tokens = len(json.dumps(req_body["tools"], ensure_ascii=False)) // 4

    # Check recent ZCode daily log for 1302/1308/1310 rate limits
    rate_limit_hits: Dict[str, int] = {}
    log_dir = os.path.join(home, ".zcode", "cli", "log")
    if os.path.isdir(log_dir):
        logs = sorted(glob.glob(os.path.join(log_dir, "zcode-*.jsonl")), key=os.path.getmtime, reverse=True)
        if logs:
            for ln in _read_tail_lines(logs[0], max_bytes=65536):
                if "model.request.failed" in ln or "rate_limit" in ln:
                    for code in _RATE_LIMIT_CODES:
                        if code in ln:
                            rate_limit_hits[code] = rate_limit_hits.get(code, 0) + 1

    cache_hit_pct = round((last_cr / last_in * 100.0), 1) if last_in > 0 else 100.0
    return {
        "agent": "zcode",
        "file_path": best_path,
        "model": prev_model,
        "turns_sampled": turns,
        "last_input_tokens": last_in,
        "last_cache_read_tokens": last_cr,
        "last_output_tokens": last_out,
        "cache_hit_pct": cache_hit_pct,
        "model_switches": model_switches,
        "zero_usage_turns": zero_usage,
        "reasoning_chars": last_reason_chars,
        "visible_chars": last_text_chars,
        "tool_schema_tokens": tool_schema_tokens,
        "active_parallel_sessions": active_parallel,
        "rate_limit_hits": rate_limit_hits,
    }


def _scan_claude_or_kimi_tail(transcript_path: str, agent: str) -> Optional[Dict[str, Any]]:
    if not transcript_path or not os.path.isfile(transcript_path):
        return None
    lines = _read_tail_lines(transcript_path, max_bytes=65536)
    last_in = last_cr = last_cw = last_out = 0
    prev_model = ""
    model_switches = 0
    turns = 0
    prev_ts: Optional[float] = None
    max_gap_s = 0.0

    for ln in lines[-30:]:
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        ts = _parse_iso(obj.get("timestamp") or obj.get("created_at"))
        if ts is not None:
            if prev_ts is not None:
                max_gap_s = max(max_gap_s, ts - prev_ts)
            prev_ts = ts
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
        u = msg.get("usage") if isinstance(msg, dict) else None
        m_id = str((msg.get("model") if isinstance(msg, dict) else "") or obj.get("model") or "")
        if m_id:
            if prev_model and m_id != prev_model:
                model_switches += 1
            prev_model = m_id
        if isinstance(u, dict):
            turns += 1
            raw_in = int(u.get("input_tokens") or 0)
            cr = int(u.get("cache_read_input_tokens") or 0)
            cw = int(u.get("cache_creation_input_tokens") or 0)
            tot_in = raw_in + cr + cw
            if tot_in > 0:
                last_in = tot_in
                last_cr = cr
                last_cw = cw
                last_out = int(u.get("output_tokens") or 0)

    if turns == 0 and not lines:
        return None
    cache_hit_pct = round((last_cr / last_in * 100.0), 1) if last_in > 0 else 100.0
    return {
        "agent": agent,
        "file_path": transcript_path,
        "model": prev_model,
        "turns_sampled": turns,
        "last_input_tokens": last_in,
        "last_cache_read_tokens": last_cr,
        "last_cache_write_tokens": last_cw,
        "last_output_tokens": last_out,
        "cache_hit_pct": cache_hit_pct,
        "model_switches": model_switches,
        "max_gap_seconds": round(max_gap_s, 1),
        "active_parallel_sessions": 1,
        "rate_limit_hits": {},
    }


def _scan_generic_transcript_tail(transcript_path: str, agent: str) -> Optional[Dict[str, Any]]:
    if not transcript_path or not os.path.isfile(transcript_path):
        return None
    lines = _read_tail_lines(transcript_path, max_bytes=65536)
    turns = 0
    spills = 0
    total_chars = 0
    prev_ts: Optional[float] = None
    max_gap_s = 0.0
    for ln in lines:
        total_chars += len(ln)
        try:
            step = json.loads(ln)
        except Exception:
            continue
        ts = _parse_iso(step.get("created_at") or step.get("timestamp"))
        if ts is not None:
            if prev_ts is not None:
                max_gap_s = max(max_gap_s, ts - prev_ts)
            prev_ts = ts
        if step.get("type") == "PLANNER_RESPONSE" or step.get("tool_calls") or step.get("role") == "assistant":
            turns += 1
        content = step.get("content") or ""
        if isinstance(content, str) and "The output was large and was saved to:" in content:
            spills += 1
    return {
        "agent": agent,
        "file_path": transcript_path,
        "model": "",
        "turns_sampled": turns,
        "last_input_tokens": total_chars // 4,
        "last_cache_read_tokens": 0,
        "last_output_tokens": 0,
        "cache_hit_pct": 100.0,
        "model_switches": 0,
        "disk_spills": spills,
        "max_gap_seconds": round(max_gap_s, 1),
        "active_parallel_sessions": 1,
        "rate_limit_hits": {},
    }


def collect_live_burn_snapshot(
    agent: str = "auto",
    session_id: str = "",
    transcript_path: str = "",
    home_dir: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Collects a fast tail snapshot of the active session for any supported agent."""
    home = home_dir or os.path.expanduser("~")
    ag = (agent or "auto").lower()

    if ag == "zcode" or (transcript_path and "model-io-" in os.path.basename(transcript_path)):
        return _scan_zcode_tail(home, session_id=session_id, explicit_path=transcript_path)

    if ag in ("claude_code", "claude", "kimi", "kimi_code"):
        if transcript_path and os.path.isfile(transcript_path):
            return _scan_claude_or_kimi_tail(transcript_path, ag)
        if ag in ("claude_code", "claude"):
            proj_files = glob.glob(os.path.join(home, ".claude", "projects", "*", "*.jsonl"))
            if proj_files:
                newest = max(proj_files, key=os.path.getmtime)
                if time.time() - os.path.getmtime(newest) <= 3600:
                    return _scan_claude_or_kimi_tail(newest, "claude_code")
        return None

    if transcript_path and os.path.isfile(transcript_path):
        return _scan_generic_transcript_tail(transcript_path, ag)

    if ag == "auto":
        z = _scan_zcode_tail(home, session_id=session_id)
        if z:
            return z
    return None


def evaluate_burn_warnings(
    snapshot: Optional[Dict[str, Any]],
    now: Optional[datetime.datetime] = None,
) -> List[Dict[str, str]]:
    """Evaluates structured burn warnings from a live snapshot."""
    if not snapshot:
        return []
    warnings: List[Dict[str, str]] = []
    model = str(snapshot.get("model") or "")
    m_lower = model.lower()
    in_tok = int(snapshot.get("last_input_tokens") or 0)
    cache_hit = float(snapshot.get("cache_hit_pct") or 100.0)
    turns = int(snapshot.get("turns_sampled") or 0)
    switches = int(snapshot.get("model_switches") or 0)
    parallel = int(snapshot.get("active_parallel_sessions") or 0)
    reason_c = int(snapshot.get("reasoning_chars") or 0)
    vis_c = int(snapshot.get("visible_chars") or 0)
    tool_tok = int(snapshot.get("tool_schema_tokens") or 0)
    rl_hits = snapshot.get("rate_limit_hits") or {}

    if in_tok >= 120000:
        warnings.append({
            "code": "context_bloat",
            "message": (
                f"Session context reached {in_tok:,} tokens — even cached turns burn high quota. "
                f"Consider starting a fresh session or compacting."
            ),
        })

    if turns >= 2 and in_tok >= 20000 and cache_hit < 50.0:
        extra = " (triggered by mid-session model switch)" if switches > 0 else ""
        warnings.append({
            "code": "cache_crash",
            "message": (
                f"Prompt cache hit dropped to {cache_hit:.0f}% on {in_tok:,} input tokens{extra} — "
                f"uncached reads burn ~4x more quota."
            ),
        })

    if "glm-5.3" in m_lower and "flash" not in m_lower and is_zai_peak_hour(now):
        warnings.append({
            "code": "zai_peak_glm53",
            "message": (
                f"Running {model} during Z.ai Peak Hours (14:00-18:00 UTC+8, 2x rate vs off-peak). "
                f"GLM-5.4 is ~33% cheaper and GLM-5.3-Flash is ~6x cheaper."
            ),
        })

    if reason_c > 8000 and reason_c > max(1, vis_c) * 4:
        warnings.append({
            "code": "hidden_reasoning",
            "message": (
                f"Hidden thinking consumed {reason_c:,} chars vs {vis_c:,} visible chars on last turn "
                f"(output tokens carry the highest multiplier)."
            ),
        })

    if parallel >= 2:
        warnings.append({
            "code": "parallel_sessions",
            "message": (
                f"{parallel} active sessions are drawing from the same quota pool concurrently "
                f"(risk of 1302 concurrency throttling and multiplied burn)."
            ),
        })

    if tool_tok >= 15000:
        warnings.append({
            "code": "tool_schema_bloat",
            "message": (
                f"Declared tool schemas add ~{tool_tok:,} tokens on every turn. "
                f"Disable unused MCP servers to cut baseline burn."
            ),
        })

    if rl_hits:
        summary = ", ".join(f"{k}x{v}" for k, v in sorted(rl_hits.items()))
        warnings.append({
            "code": "rate_limit_recent",
            "message": f"Recent Z.ai rate/quota errors detected in logs ({summary}).",
        })

    return warnings


def _dedup_state_path() -> str:
    try:
        from src.paths import resolve_data_dir
        return os.path.join(resolve_data_dir(), "burn_alerts_state.json")
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".tokensafe", "data", "burn_alerts_state.json")


def get_burn_warnings(
    agent: str = "auto",
    session_id: str = "",
    transcript_path: str = "",
    cooldown_seconds: float = 300.0,
    home_dir: Optional[str] = None,
) -> Optional[str]:
    """Returns a concise formatted burn alert banner (rate-limited per session+code), or None."""
    snap = collect_live_burn_snapshot(
        agent=agent, session_id=session_id, transcript_path=transcript_path, home_dir=home_dir
    )
    warnings = evaluate_burn_warnings(snap)
    if not warnings:
        return None

    now = time.time()
    state_path = _dedup_state_path()
    state: Dict[str, float] = {}
    if cooldown_seconds > 0 and os.path.isfile(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                state = {k: float(v) for k, v in loaded.items() if now - float(v) < 86400}
        except Exception:
            state = {}

    fresh: List[Dict[str, str]] = []
    sess_key = session_id or os.path.basename((snap or {}).get("file_path") or "default")
    for w in warnings:
        key = f"{sess_key}:{w['code']}"
        if cooldown_seconds <= 0 or (now - state.get(key, 0.0)) >= cooldown_seconds:
            fresh.append(w)
            state[key] = now

    if not fresh:
        return None

    if cooldown_seconds > 0:
        try:
            os.makedirs(os.path.dirname(state_path), exist_ok=True)
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(state, f)
        except Exception:
            pass

    lines = ["🔥 **TokenSafe Live Burn Guard:**"]
    for w in fresh[:3]:
        lines.append(f"• {w['message']}")
    return "\n".join(lines)
