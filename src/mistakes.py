"""
TokenSafe Persistent Revert & Failure Memory (Mistakes Ledger) (v3.3.0).

Prevents AI coding agents from repeating known dead-ends, failed hypotheses,
and reverted code patterns across turns and sessions.

Synergizes directly with:
1. `LoopDetector`: Captures repetitive tool-execution loops and converts them to persistent memory.
2. Git Rollback Tracking: Detects git reverts/checkouts and logs the abandoned direction.
3. Anti-Stuck Prompt Guard: Injects a compact (<80 tokens) warning into context before execution.
"""

import os
import json
import time
import hashlib
import threading
import subprocess
from typing import Dict, Any, List, Optional, Tuple
from src.paths import resolve_data_dir, ensure_tokensafe_gitignore
from src.secret_redactor import SecretRedactor


def _find_project_mistakes_file(project_root: Optional[str] = None) -> str:
    """Finds or creates .tokensafe/mistakes.jsonl in project or user data dir."""
    if project_root and os.path.isdir(project_root):
        proj_dir = os.path.join(project_root, ".tokensafe")
        try:
            os.makedirs(proj_dir, exist_ok=True)
            ensure_tokensafe_gitignore(project_root)
            return os.path.join(proj_dir, "mistakes.jsonl")
        except Exception:
            pass
    # Fallback to global data dir
    global_dir = resolve_data_dir()
    os.makedirs(global_dir, exist_ok=True)
    return os.path.join(global_dir, "mistakes.jsonl")


class MistakesLedger:
    """Persistent ledger of failed hypotheses and reverted code patterns."""

    MAX_ENTRIES = 50

    @classmethod
    def record_failure(
        cls,
        summary: str,
        file_path: Optional[str] = None,
        source: str = "loop_guard",
        details: Optional[str] = None,
        project_root: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Records a failed attempt or dead-end in the persistent ledger.
        """
        ledger_path = _find_project_mistakes_file(project_root)
        f_basename = os.path.basename(file_path) if file_path else ""

        entry = {
            "id": hashlib.sha256(f"{f_basename}:{summary}:{time.time()}".encode("utf-8")).hexdigest()[:12],
            "timestamp": time.time(),
            "source": source,
            "file": f_basename,
            "file_full": file_path or "",
            "summary": summary.strip()[:200],
            "details": (details or "").strip()[:500],
            "active": True
        }

        entries = cls.list_mistakes(project_root=project_root, active_only=False)

        # Deduplicate identical summary + file
        filtered = [
            e for e in entries
            if not (e.get("file") == entry["file"] and e.get("summary") == entry["summary"])
        ]
        filtered.append(entry)

        # Cap to MAX_ENTRIES
        if len(filtered) > cls.MAX_ENTRIES:
            filtered = filtered[-cls.MAX_ENTRIES:]

        tmp_path = f"{ledger_path}.tmp.{os.getpid()}"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                for e in filtered:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
            os.replace(tmp_path, ledger_path)
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass

        # Broadcast to L3 Team Sync (scrubbed, non-blocking)
        try:
            from src.team_cache import TeamCache
            safe_summary, _, _ = SecretRedactor.redact_text(summary.strip())
            h = hashlib.sha256(f"{f_basename}:{safe_summary}".encode("utf-8")).hexdigest()[:16]
            payload = json.dumps({"file": f_basename, "summary": safe_summary[:160], "timestamp": int(time.time())})
            TeamCache.put("TEAM_MISTAKE", h, payload, tokens_saved=120)
        except Exception:
            pass

        return entry

    @classmethod
    def record_from_loop(
        cls,
        tool_name: str,
        args: Dict[str, Any],
        repeats: int,
        project_root: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Anti-stuck synergy: converts a tripped LoopDetector event into a persistent mistake record.
        """
        target = args.get("Path") or args.get("path") or args.get("CommandLine") or args.get("command") or ""
        target_str = str(target)
        if len(target_str) > 80:
            target_str = target_str[:80] + "..."

        summary = f"Tool '{tool_name}' stuck in {repeats}x loop with target: {target_str}"
        fpath = args.get("Path") or args.get("path")
        return cls.record_failure(
            summary=summary,
            file_path=str(fpath) if fpath else None,
            source="loop_detector",
            details=f"Identical parameters executed {repeats} times in a row without progress.",
            project_root=project_root
        )

    @classmethod
    def record_revert(
        cls,
        file_path: str,
        reason: str,
        project_root: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Records a reverted code change or git rollback.
        """
        summary = f"Reverted change: {reason.strip()}"
        return cls.record_failure(
            summary=summary,
            file_path=file_path,
            source="git_revert",
            project_root=project_root
        )

    @classmethod
    def list_mistakes(
        cls,
        project_root: Optional[str] = None,
        active_only: bool = True
    ) -> List[Dict[str, Any]]:
        """Loads all recorded mistakes from the ledger."""
        ledger_path = _find_project_mistakes_file(project_root)
        if not os.path.exists(ledger_path):
            return []

        entries = []
        try:
            with open(ledger_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            item = json.loads(line)
                            if not active_only or item.get("active", True):
                                entries.append(item)
                        except Exception:
                            pass
        except Exception:
            return []

        return entries

    @classmethod
    def clear_mistakes(cls, project_root: Optional[str] = None) -> int:
        """Clears all entries from the mistakes ledger."""
        ledger_path = _find_project_mistakes_file(project_root)
        if os.path.exists(ledger_path):
            try:
                entries = cls.list_mistakes(project_root=project_root, active_only=False)
                os.remove(ledger_path)
                return len(entries)
            except Exception:
                pass
        return 0

    _team_mistakes_cache: List[Dict[str, Any]] = []
    _team_mistakes_fetched_at: float = 0.0
    _team_mistakes_lock = threading.Lock()

    @classmethod
    def get_team_mistakes(cls, timeout: float = 0.6) -> List[Dict[str, Any]]:
        """Retrieves scrubbed dead-end summaries shared across team members with 300s TTL cache."""
        now = time.time()
        with cls._team_mistakes_lock:
            if cls._team_mistakes_cache and (now - cls._team_mistakes_fetched_at < 300.0):
                return list(cls._team_mistakes_cache)

        try:
            from src.team_cache import TeamCache
            raw_entries = TeamCache.list_entries("TEAM_MISTAKE", limit=10, timeout=timeout)
            parsed: List[Dict[str, Any]] = []
            for item in raw_entries:
                val = item.get("value") or item.get("cached_value")
                if isinstance(val, str):
                    try:
                        p_val = json.loads(val)
                        if isinstance(p_val, dict):
                            parsed.append(p_val)
                        else:
                            parsed.append({"summary": str(val)[:120]})
                    except Exception:
                        parsed.append({"summary": str(val)[:120]})
                elif isinstance(val, dict):
                    parsed.append(val)

            with cls._team_mistakes_lock:
                cls._team_mistakes_cache = parsed
                cls._team_mistakes_fetched_at = now
            return list(parsed)
        except Exception:
            return []

    @classmethod
    def should_force_ask(
        cls,
        target: str,
        action_type: str = "command",
        project_root: Optional[str] = None
    ) -> Tuple[bool, str]:
        """
        Two-tier hybrid escalation policy:
        - Returns (True, reason) ONLY if identical action/target failed > 2 times in last 30 minutes.
        - Returns (False, reason) if 1-2 failures exist (soft advisory warning).
        - Returns (False, "") if no recent failure detected.
        """
        if not target:
            return False, ""
        norm_target = target.strip().lower()
        now = time.time()
        window = 1800.0  # 30 minutes
        mistakes = cls.list_mistakes(project_root=project_root, active_only=True)

        matching = []
        for m in mistakes:
            if now - float(m.get("timestamp", 0) or 0) > window:
                continue
            f = (m.get("file") or "").lower()
            s = (m.get("summary") or "").lower()
            det = (m.get("details") or "").lower()

            matched = False
            if len(norm_target) >= 12:
                # Substring matching for longer specific commands or file paths
                if (f and f in norm_target) or (norm_target in s) or (norm_target in det):
                    matched = True
            else:
                # Short targets (e.g. "npm", "ls"): exact equality against file, summary, or extracted command prefix
                cmd_part = s.split(":", 1)[0].strip() if ":" in s else s.split()[0] if s.split() else ""
                if (f and norm_target == f) or (norm_target == s) or (norm_target == cmd_part):
                    matched = True

            if matched:
                matching.append(m)

        count = len(matching)
        if count > 2:
            last = matching[-1].get("summary", "")
            return True, f"Dead-End Loop: Action on '{target[:60]}' repeatedly failed {count}x in last 30m. Prior failure: {last}"
        elif count > 0:
            last = matching[-1].get("summary", "")
            return False, f"Advisory: Prior failure recorded on '{target[:50]}': {last}"
        return False, ""

    @classmethod
    def get_guard_prompt(
        cls,
        project_root: Optional[str] = None,
        max_items: int = 3
    ) -> Optional[str]:
        """
        Generates a compact (<100 tokens) prompt guard from recent active local and team mistakes.
        Injectable into system instructions or PreInvocation hooks.
        """
        local_mistakes = cls.list_mistakes(project_root=project_root, active_only=True)
        team_mistakes = cls.get_team_mistakes()
        if not local_mistakes and not team_mistakes:
            return None

        lines = ["[TokenSafe Anti-Stuck Guard: Prior Dead-Ends to Avoid]:"]
        local_summaries = set()

        # Take up to max_items most recent active local entries
        for m in reversed(local_mistakes[-max_items:]):
            s = m.get("summary", "")
            local_summaries.add(s.strip().lower())
            f = m.get("file")
            f_prefix = f"{f}: " if f else ""
            lines.append(f"• {f_prefix}{s}")

        # Merge up to 2 unique team mistakes
        team_added = 0
        for tm in team_mistakes:
            if team_added >= 2:
                break
            ts = (tm.get("summary") or "").strip()
            if not ts or ts.lower() in local_summaries:
                continue
            tf = tm.get("file")
            tf_prefix = f"{tf}: " if tf else ""
            lines.append(f"• team: {tf_prefix}{ts}")
            local_summaries.add(ts.lower())
            team_added += 1

        if len(lines) == 1:
            return None

        return "\n".join(lines)
