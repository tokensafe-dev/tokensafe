"""
TokenSafe File-State Tracker (stale-edit guard).

Records a content-hash snapshot of every file the model has just SEEN (Read)
or just REWRITTEN (Write/Edit) — machine-level state shared by all agents —
so PreToolUse hooks can detect externally-changed edit targets (build output,
linters/formatters, another concurrent session) BEFORE the edit lands.

Why: an edit against a stale file either corrupts content (hosts without
native protection) or fails and forces the model to re-read the whole file,
burning a full file-worth of context on every occurrence.

Design rules:
- Fail-open everywhere: any internal error returns None/False so the guard
  degrades to "no opinion" and never blocks a tool call by itself.
- ``ask``/``force_ask`` only, never deny (same philosophy as guard_write).
- Kill switch: TOKENSAFE_STALE_GUARD=0 disables the PreToolUse checks;
  tracking (PostToolUse hashing) stays harmless and keeps running.
- State lives in <data_dir>/file_state.json, atomic writes, LRU-capped.
"""

import os
import json
import time
import hashlib
import tempfile
from typing import Any, Dict, Optional

from src.paths import resolve_data_dir

STATE_FILENAME = "file_state.json"
_MAX_ENTRIES = 512
_MAX_FILE_BYTES = 1_000_000
_ENTRY_TTL_S = 7 * 86400

# Files that are never tracked: binaries, generated bundles, lockfiles and
# anything under TokenSafe's own data dir (self-edits would poison the map).
_SKIP_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".tar",
    ".gz", ".bz2", ".7z", ".rar", ".mp4", ".mov", ".mp3", ".wav", ".woff",
    ".woff2", ".ttf", ".otf", ".eot", ".db", ".sqlite", ".sqlite3", ".pyc",
    ".pyo", ".class", ".jar", ".so", ".dylib", ".node", ".wasm", ".exe",
    ".dll", ".bin", ".iso", ".dmg", ".min.js", ".min.css", ".map", ".lock",
)
_SKIP_PARTS = (
    ".git", "node_modules", "dist", "build", "out", "__pycache__",
    ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache", "vendor",
)


def guard_enabled() -> bool:
    """Kill switch for the PreToolUse stale checks (tracking is always safe)."""
    return os.environ.get("TOKENSAFE_STALE_GUARD", "").strip().lower() not in ("0", "false", "no")


def _state_path() -> str:
    return os.path.join(resolve_data_dir(), STATE_FILENAME)


def _skip_path(path: str) -> bool:
    try:
        real = os.path.realpath(path)
        data_root = os.path.realpath(resolve_data_dir())
        if real == data_root or real.startswith(data_root + os.sep):
            return True
        home = os.path.realpath(os.path.expanduser("~"))
        if real.startswith(os.path.join(home, ".tokensafe")):
            return True
        lowered = real.lower()
        if lowered.endswith(_SKIP_SUFFIXES):
            return True
        parts = {p.lower() for p in os.path.normpath(real).split(os.sep)}
        if parts & set(_SKIP_PARTS):
            return True
        return os.path.getsize(real) > _MAX_FILE_BYTES
    except OSError:
        return True


def _load_state() -> Dict[str, Any]:
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    try:
        target = _state_path()
        os.makedirs(os.path.dirname(target), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".file_state_", dir=os.path.dirname(target))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f)
            os.replace(tmp, target)
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass
    except Exception:
        pass


def _prune_state(state: Dict[str, Any]) -> Dict[str, Any]:
    now = time.time()
    live = {
        k: v for k, v in state.items()
        if isinstance(v, dict) and (now - float(v.get("ts") or 0)) < _ENTRY_TTL_S
    }
    if len(live) > _MAX_ENTRIES:
        ranked = sorted(live.items(), key=lambda kv: float(kv[1].get("ts") or 0), reverse=True)
        live = dict(ranked[:_MAX_ENTRIES])
    return live


def record_seen(path: str) -> bool:
    """Snapshots the current on-disk hash of ``path`` (post-Read/post-Write).

    Returns True when a snapshot was recorded. A missing file drops its entry
    (deleted targets can never be stale)."""
    if not path:
        return False
    abs_path = os.path.abspath(path)
    try:
        with open(abs_path, "rb") as f:
            blob = f.read()
    except FileNotFoundError:
        try:
            state = _prune_state(_load_state())
            if abs_path in state:
                state.pop(abs_path, None)
                _save_state(state)
        except Exception:
            pass
        return False
    except Exception:
        return False
    if _skip_path(abs_path):
        return False
    try:
        sha = hashlib.sha256(blob).hexdigest()
        state = _prune_state(_load_state())
        state[abs_path] = {"sha": sha, "ts": time.time(), "size": len(blob)}
        # Trim AFTER insertion so the cap holds on the saved snapshot too.
        state = _prune_state(state)
        _save_state(state)
        return True
    except Exception:
        return False


def check_stale(path: str) -> Optional[Dict[str, Any]]:
    """Returns staleness info when ``path`` changed on disk since the last
    snapshot, else None (never seen, unchanged, deleted, or untrackable)."""
    if not path:
        return None
    try:
        abs_path = os.path.abspath(path)
        if _skip_path(abs_path):
            return None
        entry = _load_state().get(abs_path)
        if not isinstance(entry, dict):
            return None
        with open(abs_path, "rb") as f:
            blob = f.read()
        sha = hashlib.sha256(blob).hexdigest()
        if sha == entry.get("sha"):
            return None
        return {
            "age_s": round(time.time() - float(entry.get("ts") or 0)),
            "size_seen": int(entry.get("size") or 0),
            "size_now": len(blob),
        }
    except Exception:
        return None


def estimate_reread_tokens(path: str, cap: int = 50_000) -> int:
    """Cheap counterfactual for the wasted re-read mass (bytes/4 heuristic)."""
    try:
        return min(cap, os.path.getsize(os.path.abspath(path)) // 4)
    except Exception:
        return 0


class FileState:
    """Namespace facade so hook call sites read naturally
    (``FileState.record_seen(path)``) while the module-level functions above
    stay directly importable for tests."""

    guard_enabled = staticmethod(guard_enabled)
    record_seen = staticmethod(record_seen)
    check_stale = staticmethod(check_stale)
    estimate_reread_tokens = staticmethod(estimate_reread_tokens)
