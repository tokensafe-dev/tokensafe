"""
TokenSafe Reversible Hash Stash (v3.3.0).

Stores raw tool, command, MCP, and file content folded or truncated by TokenSafe.
Each folded block is tagged with an 8-character deterministic hex hash:
    [TokenSafe folded: #a1b2c3d4 (184 lines omitted) — call tokensafe:expand_ref('a1b2c3d4') to restore raw content]

Any agent or developer can immediately restore the exact original bytes via:
- MCP Tool: tokensafe:expand_ref(hash="a1b2c3d4")
- CLI: tokensafe expand-ref a1b2c3d4
"""

import os
import re
import json
import time
import hashlib
from typing import Tuple, Optional, Dict, Any

from src.paths import resolve_data_dir

STASH_DIRNAME = "stash"
MAX_STASH_CHARS = 5 * 1024 * 1024  # Cap single artifact at 5 MB
DEFAULT_MAX_AGE_DAYS = 7
DEFAULT_MAX_TOTAL_MB = 50
_HASH_PATTERN = re.compile(r"#?([0-9a-fA-F]{8})\b")
_LAST_CLEANUP_TS = 0.0


def get_stash_dir() -> str:
    """Returns absolute path to the active stash directory."""
    d = os.path.join(resolve_data_dir(), STASH_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return d


def compute_hash(text: str) -> Tuple[str, str]:
    """Returns (hash8, sha256) for content."""
    sha = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    return sha[:8], sha


def stash_block(content: str, source: str = "tool") -> Tuple[str, str]:
    """
    Saves raw content to the local stash directory and returns (hash8, fold_marker).
    """
    global _LAST_CLEANUP_TS
    if not content:
        return "", ""

    hash8, full_sha = compute_hash(content)
    lines_count = len(content.splitlines())
    chars_count = len(content)

    marker = (
        f"[TokenSafe folded: #{hash8} ({lines_count} lines / {chars_count} chars omitted) "
        f"— call tokensafe:expand_ref('{hash8}') to restore raw content]"
    )

    if chars_count > MAX_STASH_CHARS:
        # Don't exhaust local disk on massive blobs > 5MB
        return hash8, marker

    try:
        stash_dir = get_stash_dir()
        path = os.path.join(stash_dir, f"{hash8}.json")
        if not os.path.exists(path):
            payload = {
                "hash": hash8,
                "sha256": full_sha,
                "timestamp": int(time.time()),
                "source": source,
                "lines": lines_count,
                "chars": chars_count,
                "content": content,
            }
            tmp_path = f"{path}.tmp.{os.getpid()}"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp_path, path)

        # Trigger passive cleanup once per hour
        now = time.time()
        if now - _LAST_CLEANUP_TS > 3600.0:
            _LAST_CLEANUP_TS = now
            cleanup_old_stashes()
    except Exception:
        pass

    return hash8, marker


def retrieve_block(hash_or_marker: str) -> Optional[str]:
    """
    Retrieves full raw content by 8-character hex hash or by a fold marker string.
    Returns None if not found or corrupted.
    """
    if not hash_or_marker:
        return None

    clean_str = str(hash_or_marker).strip()
    match = _HASH_PATTERN.search(clean_str)
    if not match:
        return None

    hash8 = match.group(1).lower()
    stash_dir = get_stash_dir()
    path = os.path.join(stash_dir, f"{hash8}.json")

    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "content" in data:
                return data["content"]
        except Exception:
            pass

    # Fallback to tool_ledger (legacy 16-char ledger files)
    try:
        ledger_dir = os.path.join(resolve_data_dir(), "tool_ledger")
        if os.path.isdir(ledger_dir):
            for fname in os.listdir(ledger_dir):
                if fname.startswith(hash8) and fname.endswith(".json"):
                    with open(os.path.join(ledger_dir, fname), "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict) and "content" in data:
                        return data["content"]
    except Exception:
        pass

    return None


# Alias for expand_ref
expand_ref = retrieve_block


def cleanup_old_stashes(
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    max_total_mb: int = DEFAULT_MAX_TOTAL_MB,
) -> int:
    """
    Prunes files older than max_age_days or when total stash size exceeds max_total_mb.
    Returns number of pruned files.
    """
    pruned = 0
    try:
        stash_dir = get_stash_dir()
        if not os.path.isdir(stash_dir):
            return 0

        now = time.time()
        max_age_seconds = max_age_days * 86400
        entries = []
        total_size = 0

        for fname in os.listdir(stash_dir):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(stash_dir, fname)
            try:
                st = os.stat(fpath)
                entries.append((fpath, st.st_mtime, st.st_size))
                total_size += st.st_size
            except OSError:
                continue

        # 1. Prune by age
        remaining = []
        for fpath, mtime, size in entries:
            if (now - mtime) > max_age_seconds:
                try:
                    os.remove(fpath)
                    pruned += 1
                    total_size -= size
                except OSError:
                    remaining.append((fpath, mtime, size))
            else:
                remaining.append((fpath, mtime, size))

        # 2. Prune by total size if still over limit
        max_bytes = max_total_mb * 1024 * 1024
        if total_size > max_bytes:
            # Sort by mtime ascending (oldest first)
            remaining.sort(key=lambda x: x[1])
            for fpath, _, size in remaining:
                if total_size <= (max_bytes * 0.8):
                    break
                try:
                    os.remove(fpath)
                    pruned += 1
                    total_size -= size
                except OSError:
                    pass
    except Exception:
        pass

    return pruned
