"""
Shared core for cross-agent lifecycle-hook adapters (Claude Code, Cursor, ZCode).

Design rules (see METRICS.md):
- Savings are only recorded for REAL reductions. Blocked reads/commands never
  reach the model, but their "would-have-been" mass is a heuristic guess, so
  PROMPT injections and GUARD_*/COMMAND_FILTER/LOOP_GUARD events are labeled
  estimated; events whose original/optimized tokens come from real bytes or
  metered I/O stay measured.
- Adapters ALWAYS fail open: an adapter crash must never block the host agent.
  (Cursor additionally blocks on invalid JSON, so adapters return valid
  allow-JSON from the exception handler.)
"""

import os
import sys
import re
import json
import time
from typing import Dict, Any, Optional, Tuple

from src.tokenizer import count_tokens

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# One-shot session directives, injected once per session by every agent's
# SessionStart surface (Antigravity hooks/session_start.py as a persistent
# systemMessage, Claude Code as hookSpecificOutput.additionalContext).
TOKENSAFE_DIRECTIVES = (
    "[TokenSafe Mandatory Directives]:\n"
    "• Terminal code searches (grep/rg/find on source files) are BLOCKED by TokenSafe Guard.\n"
    "• To search keywords, symbols, or configs: call 'tokensafe:scoped_search(query, scope)'.\n"
    "• To inspect component or function definitions: call 'tokensafe:smart_read_file(path, symbol)' "
    "(1-hop deps default to compact signatures via dep_mode='signature'; pass dep_mode='full' only when "
    "dependency bodies are needed, or force_full=true if a cache pointer references content missing from context).\n"
    "• Deduplication & Cache Pointers: Never re-read unchanged files repeatedly; reuse earlier context or pointer references.\n"
    "• Zero-Fluff Egress: Omit conversational filler, courtesy preambles, and wrap-ups. Format responses in dense, technical prose.\n"
    "• Surgical Code Diffs: Never reprint full unmodified files or lengthy boilerplate; cite line ranges and provide targeted diffs.\n"
    "• Compact Test Execution: Run unit tests with concise flags or via 'tokensafe tests [cmd]' so passing suites emit a 1-line summary.\n"
    "• Mandatory Architecture Graph & God Nodes: For any codebase inquiry, navigation, or multi-component work, ALWAYS inspect God Nodes via 'tokensafe:get_architecture_graph(project_path)' first before diving into files (pass target project's absolute path in multi-repo setups).\n"
    "• To inspect all exported types/interfaces: call 'tokensafe:get_api_surface(project_path)'.\n"
    "• Before large multi-step tasks: call 'tokensafe:context_budget(model=...)' to check context headroom and Quality Score (Grade S-F).\n"
    "• When context_budget reports warning/critical or Grade C/F: call 'tokensafe:compact_conversation(transcript_path=...)' to fold history before continuing.\n"
    "• Prohibited File Edits: NEVER use 'sed -i', 'perl -i', or stream editors to modify source files; ALWAYS use 'edit_file' with exact string targets. NEVER suppress errors with '2>/dev/null'.\n"
    "• Prefer 'edit_file' over 'write_file' for existing files."
)

# High-precision deny patterns: secrets and irreversible destruction only.
_SECRET_PATTERNS = [
    r"sk_live_[0-9a-zA-Z]{10,}",
    r"AKIA[0-9A-Z]{16}",
    r"ghp_[0-9a-zA-Z]{30,}",
    r"xox[baprs]-[0-9a-zA-Z-]{10,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
]
_DESTRUCTIVE_PATTERNS = [
    r"rm\s+(-[a-zA-Z]*\s+)*-?[rf]{2,}[a-zA-Z]*\s+(--\s+)?/(?:\s|$)",   # rm -rf /
    r"rm\s+(-[a-zA-Z]*\s+)*-?[rf]{2,}[a-zA-Z]*\s+~(?:/\s*)?(?:\s|$)",  # rm -rf ~
    r"dd\s+.*of=/dev/[sd]",
    r"mkfs\.\w+",
    r"chmod\s+-R\s+777\s+/(?:\s|$)",
    r":\(\)\{.*\};:",                                                  # fork bomb
]
_TLS_BYPASS_PATTERNS = [
    r"curl\s+(-[a-zA-Z]+\s+)*-k\b",
    r"curl\s+.*--insecure",
    r"NODE_TLS_REJECT_UNAUTHORIZED\s*=\s*0",
    r"wget\s+.*--no-check-certificate",
    r"git\s+-c\s+[a-z.]*sslVerify\s*=\s*false",
    r"GIT_SSL_NO_VERIFY\s*=\s*(?:true|1)",
]
# Commands that never terminate on their own — an agent burning a turn on them
# stalls until timeout. Intentional runs can still proceed via the "ask" path.
_INFINITE_PATTERNS = [
    r"\btail\b[^|;&]*\s-f\b",
    r"\bwatch\s+",
    r"\bdocker\s+logs\b[^|;&]*\s(?:-f\b|--follow)",
    r"\bkubectl\s+logs\b[^|;&]*\s-f\b",
    r"\bdocker\s+attach\b",
    # vitest runs an interactive watcher unless the `run` subcommand is given
    r"\bvitest\b(?![^|;&]*\brun\b)",
    r"\b(?:jest|vitest|bun)\b[^|;&]*--watch\b",
]

# Lockfiles/junk that must never be dumped into an agent context.
_JUNK_READ_PATTERNS = [
    r"(^|/)(bun\.lock|package-lock\.json|pnpm-lock\.yaml|yarn\.lock|Cargo\.lock|go\.sum|composer\.lock)$",
    r"\.(min\.js|min\.css|bundle\.js|map)$",
    r"(^|/)dist/",
    r"(^|/)node_modules/",
]


def _get_tracker():
    try:
        from src.metrics import MetricsTracker
        return MetricsTracker()
    except Exception:
        return None


# --- Model attribution -------------------------------------------------------
# Lifecycle hooks never receive the host model, so event details resolve it
# best-effort: 1) the session transcript (CC-family JSONL stores message.model
# on assistant entries — read the file tail only, cached by mtime so repeated
# hook invocations stay cheap), 2) the local proxy's state file (the proxy sees
# every request and stamps its last-seen model), 3) host env (MODEL /
# AGY_MODEL). Anything unknown stays "" — the landing feed renders "—" instead
# of a fabricated name (see telemetry_sync / record_telemetry).
_MODEL_TRANSCRIPT_CACHE: Dict[str, Tuple[float, str]] = {}
_PROXY_STATE_TTL_S = 600
_TRANSCRIPT_TAIL_BYTES = 262144


def _sniff_transcript_model(transcript_path: str) -> str:
    path = str(transcript_path or "")
    if not path:
        return ""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    cached = _MODEL_TRANSCRIPT_CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    model = ""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _TRANSCRIPT_TAIL_BYTES))
            tail = f.read().decode("utf-8", errors="ignore")
        for line in reversed(tail.splitlines()):
            if '"model"' not in line:
                continue
            m = re.search(r'"model"\s*:\s*"([^"]+)"', line)
            if m and m.group(1):
                model = m.group(1)[:64]
                break
    except OSError:
        model = ""
    _MODEL_TRANSCRIPT_CACHE[path] = (mtime, model)
    return model


def _read_proxy_state_model() -> str:
    try:
        from src.paths import resolve_data_dir
        state_path = os.path.join(resolve_data_dir(), "proxy_state.json")
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
        if time.time() - float(state.get("ts") or 0) > _PROXY_STATE_TTL_S:
            return ""
        return str(state.get("model") or "")[:64]
    except Exception:
        return ""


def resolve_event_model(transcript_path: str = "", session_id: str = "") -> str:
    """Best-effort model attribution for recorded events. Fails open to ''."""
    model = _sniff_transcript_model(transcript_path)
    if model:
        return model
    model = _read_proxy_state_model()
    if model:
        return model
    return os.environ.get("MODEL") or os.environ.get("AGY_MODEL") or ""


def compact_for_injection(prompt: str, agent: str, session_id: str, transcript_path: str = "") -> Tuple[str, Dict[str, Any]]:
    """Compacts a user prompt into its TOCP form for context injection.

    Returns (context_text, meta). context_text is '' when compaction is not
    worthwhile. Savings are recorded as estimated: the compressed form is
    injected alongside the original (no verified replace in host agents)."""
    meta: Dict[str, Any] = {"agent": agent, "compacted": False}
    if not prompt or len(prompt.strip()) < 5:
        return "", meta
    try:
        from src.compactor import PromptCompactor
        from src.tokenizer import count_tokens
        compaction = PromptCompactor.compact(prompt, enable_distill=False)
        saved = compaction.get("saved_tokens", 0)
        if saved < 5:
            return "", meta
        tracker = _get_tracker()
        if tracker:
            try:
                tracker.record_event(
                    session_id=session_id or f"hook_{agent}",
                    event_type="PROMPT",
                    original_tokens=compaction["original_tokens"],
                    optimized_tokens=compaction["compact_tokens"],
                    details={"agent": agent, "mode": "hook_inject", "tier": compaction.get("tier", "REGEX"),
                             "model": resolve_event_model(transcript_path, session_id)},
                )
            except Exception:
                pass
        meta.update({"compacted": True, "saved_tokens": saved, "reduction_pct": compaction.get("reduction_pct", 0)})
        return f"[TOCP Compressed Intent]: {compaction['compact_text']}", meta
    except Exception:
        return "", meta


def guard_command(command: str, agent: str, session_id: str = "", cwd: str = "", transcript_path: str = "") -> Dict[str, Any]:
    """Pre-execution shell guard. Returns:
    {decision: allow|deny|ask, reason, saved_tokens} where saved_tokens is the
    MEASURED mass of blocked output (0 for allowed commands)."""
    result = {"decision": "allow", "reason": "", "saved_tokens": 0}
    if not command or not command.strip():
        return result
    cmd = command.strip()
    text = cmd

    def blocked(kind: str, reason: str, decision: str = "deny"):
        est = max(50, len(cmd) // 4)  # blocked mass never reaches the model
        tracker = _get_tracker()
        if tracker:
            try:
                tracker.record_event(
                    session_id=session_id or f"hook_{agent}",
                    event_type=f"GUARD_{kind}",
                    original_tokens=est,
                    optimized_tokens=0,
                    details={"agent": agent, "command": cmd[:120],
                             "model": resolve_event_model(transcript_path, session_id)},
                )
            except Exception:
                pass
        result.update({"decision": decision, "reason": reason, "saved_tokens": est})

    for pat in _SECRET_PATTERNS:
        if re.search(pat, text):
            blocked("SECRET", f"Blocked: command contains a hardcoded secret ({pat[:12]}... pattern)")
            return result
    for pat in _DESTRUCTIVE_PATTERNS:
        if re.search(pat, text):
            blocked("DESTRUCTIVE", "Blocked: irreversible/destructive operation")
            return result
    # Full TokenSafeGuard delegation: the Antigravity path (pre_tool_use.py) runs
    # TokenSafeGuard.inspect_command, and the Claude Code / Cursor adapters must
    # honor the SAME policy suite (GIT_SECRET_STAGING, SQL_INJECTION,
    # INSECURE_CORS, DATA_EXFILTRATION, DESTRUCTIVE_RM flag/target variants, and
    # raw code search with the MCP-tools guidance) instead of a regex subset.
    # Fail-open on import problems, like every adapter guard path.
    try:
        from src.guard import TokenSafeGuard
        guard_res = TokenSafeGuard.inspect_command(cmd, cwd=cwd or None)
    except Exception:
        guard_res = None
    if guard_res is not None and getattr(guard_res, "blocked", False):
        blocked(guard_res.check_name or "GUARD", guard_res.reason)
        return result
    for pat in _TLS_BYPASS_PATTERNS:
        if re.search(pat, text):
            blocked("TLS_VERIFY_BYPASS", "Certificate verification disabled — confirm this is intentional", decision="ask")
            return result
    for pat in _INFINITE_PATTERNS:
        if re.search(pat, text):
            blocked("INFINITE", "Command never terminates on its own (follow/streaming) — run it in the background or confirm intent", decision="ask")
            return result
    return result


def guard_read(file_path: str, agent: str, session_id: str = "", transcript_path: str = "") -> Dict[str, Any]:
    """Pre-read guard: denies lockfiles/bundles/dist junk that would burn the
    context window. Blocked reads are recorded as measured savings."""
    result = {"decision": "allow", "reason": "", "saved_tokens": 0}
    if not file_path:
        return result
    normalized = file_path.replace("\\", "/")
    for pat in _JUNK_READ_PATTERNS:
        if re.search(pat, normalized):
            est = 4000  # typical lockfile dump size floor
            tracker = _get_tracker()
            if tracker:
                try:
                    tracker.record_event(
                        session_id=session_id or f"hook_{agent}",
                        event_type="GUARD_JUNK_READ",
                        original_tokens=est,
                        optimized_tokens=0,
                        details={"agent": agent, "file": os.path.basename(normalized),
                                 "model": resolve_event_model(transcript_path, session_id)},
                    )
                except Exception:
                    pass
            result.update({
                "decision": "deny",
                "reason": f"TokenSafe: '{os.path.basename(normalized)}' is machine-generated bulk "
                          f"(~{est // 1000}k tokens). Inspect it via 'tokensafe:scoped_search' or grep a symbol instead.",
                "saved_tokens": est,
            })
            return result
    return result


def guard_write(file_path: str, content: str, append: bool = False, agent: str = "", session_id: str = "", transcript_path: str = "") -> Dict[str, Any]:
    """Pre-write egress guard (v3.0.0): full-file Write over an existing file
    burns expensive OUTPUT tokens. Policy:
    - new file / append / no content -> allow
    - wholesale overwrite of a >=40-line file -> deny (parity with the
      Antigravity Output Guard, hooks/pre_tool_use.py)
    - small delta vs the current file (<25% of lines AND <80 lines) -> ask,
      steering the model to a surgical Edit instead (quality untouched).
    Recorded as OUTPUT_GUARD with honest estimated output-token savings."""
    result = {"decision": "allow", "reason": "", "saved_tokens": 0}
    if not file_path or append or not isinstance(content, str) or not content:
        return result
    path = file_path.replace("\\", "/")
    if not os.path.isfile(path):
        return result
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            existing_text = f.read()
    except Exception:
        return result
    existing_lines = existing_text.splitlines()
    if len(existing_lines) < 40:
        return result

    new_lines = content.splitlines()
    import difflib
    delta_lines = sum(
        1 for ln in difflib.unified_diff(existing_lines, new_lines, lineterm="", n=0)
        if ln[:1] in ("+", "-") and ln[:3] not in ("+++", "---")
    )
    content_tokens = max(1, count_tokens(content))
    edit_tokens = max(1, count_tokens("\n".join(new_lines[:80])) // 3)
    tracker = _get_tracker()

    def _emit(decision: str, reason: str) -> Dict[str, Any]:
        if tracker:
            try:
                tracker.record_event(
                    session_id=session_id or f"hook_{agent}",
                    event_type="OUTPUT_GUARD",
                    original_tokens=content_tokens,
                    optimized_tokens=edit_tokens if decision == "ask" else 80,
                    details={
                        "agent": agent,
                        "target_file": os.path.basename(path),
                        "existing_lines": len(existing_lines),
                        "delta_lines": delta_lines,
                        "decision": decision,
                        "model": resolve_event_model(transcript_path, session_id),
                    },
                )
            except Exception:
                pass
        return {"decision": decision, "reason": reason, "saved_tokens": max(0, content_tokens - edit_tokens)}

    if delta_lines < max(1, int(len(existing_lines) * 0.25)) and delta_lines < 80:
        return _emit(
            "ask",
            f"TokenSafe Output Guard: '{os.path.basename(path)}' already exists ({len(existing_lines)} lines) "
            f"and your Write is only a ~{delta_lines}-line delta. Use 'edit_file'/Edit with the exact old block "
            f"instead — wholesale rewrite burns expensive output tokens (~{content_tokens} vs ~{edit_tokens}).",
        )
    return _emit(
        "deny",
        f"TokenSafe Output Guard: '{os.path.basename(path)}' already exists with {len(existing_lines)} lines. "
        f"Wholesale rewriting via write_file burns expensive output tokens. "
        f"Please use 'edit_file' (surgical Edit) with Target and Replacement blocks instead.",
    )
