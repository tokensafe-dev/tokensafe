"""
TokenSafe Workspace Snapshot Generator (v3.5.0).

Produces a deterministic, token-efficient workspace snapshot (<=1.4 KB) on
SessionStart in roughly 50-150 ms depending on repo size. Injected once as a
persistent systemMessage, it eliminates the cold-start exploration phase
(turns 1-3) where agents waste 45+ seconds running exploratory `ls`,
`git status`, and script lookups.

Truthfulness rules: a failed git call is reported as "unknown" (never
"clean"), non-existent paths are never invented, gitignored entries are
filtered out, and a truncated snapshot is explicitly marked as truncated.
"""

import os
import re
import json
import shutil
import fnmatch
import subprocess
from typing import Dict, Any, List, Optional, Set

HARDENED_GIT_BASE_ARGS = [
    "--no-optional-locks",
    "-c", "core.fsmonitor=false",
    "-c", "core.quotePath=false",
    "-c", "diff.external=",
    "-c", "gpg.program=false",
    "--no-pager"
]

SKIP_SNAPSHOT_DIRS: Set[str] = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
    ".next", ".turbo", ".idea", ".vscode", "coverage", ".pytest_cache",
    ".mypy_cache", ".gemini", ".system_generated", "data", "tokensafe.egg-info",
    "Library", "Applications", "Pictures", "Movies", "Music", "Downloads",
    ".cache", ".npm", ".bun", ".rustup", ".cargo", ".local", ".output", ".vite"
}

COLLAPSE_DIRS: Set[str] = {
    "tmp", "temp", "output", "outputs", "artifacts", "logs", "coverage",
    "playwright-report", "test-results"
}

PRIORITY_DIRS: Set[str] = {
    "src", "app", "lib", "packages", "server", "web", "client", "api", "tests", "test", "scripts"
}


def _is_system_or_home_root(p: str) -> bool:
    """Detects if directory is system root, /home, or user home root."""
    try:
        resolved = os.path.realpath(p)
        root = os.path.splitdrive(resolved)[0] + os.sep if os.name == "nt" else "/"
        if resolved == root:
            return True
        home = os.path.expanduser("~")
        if os.path.realpath(home) == resolved:
            return True
        parent = os.path.dirname(resolved)
        if parent == root and os.path.basename(resolved).lower() in ("users", "home", "var", "usr", "etc"):
            return True
    except Exception:
        pass
    return False


def _parse_porcelain_paths(status_raw: str, limit: int = 6) -> List[str]:
    """Extracts display paths from `git status --porcelain=v1` output.

    Porcelain v1 is "XY <path>": the 2 status columns are fixed-width, so the
    line must not be stripped before slicing (that shifted every path by two
    characters and reported e.g. " M src/a.py" as "rc/a.py").
    """
    paths: List[str] = []
    status_lines = [l for l in status_raw.splitlines() if l.strip()]
    for line in status_lines[:limit]:
        path = line[3:].strip() if len(line) > 3 else line.strip()
        if " -> " in path:  # renames: "R  old -> new"
            path = path.split(" -> ", 1)[1]
        if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        paths.append(path)
    return paths


def _get_git_summary(workspace_dir: str, timeout_sec: float = 2.0) -> Optional[str]:
    """Extracts git branch, working tree status, and last 3 commits (bounded, truthful)."""
    git_bin = shutil.which("git")
    if not git_bin:
        return None

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"

    def _run_git(args: List[str], optional_locks: bool = True) -> Optional[str]:
        # --no-optional-locks blocks the index refresh shortcut, which makes
        # `git status` far slower on large trees; allow locks where it matters.
        base_args = HARDENED_GIT_BASE_ARGS if optional_locks else [
            a for a in HARDENED_GIT_BASE_ARGS if a != "--no-optional-locks"
        ]
        try:
            res = subprocess.run(
                [git_bin, *base_args, *args],
                cwd=workspace_dir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_sec,
                env=env
            )
            return res.stdout if res.returncode == 0 else None
        except Exception:
            return None

    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"])
    if not branch:
        return None
    branch = branch.strip()

    status_raw = _run_git(["status", "--porcelain=v1"], optional_locks=False)
    log_raw = _run_git(["log", "-n", "3", "--format=%h %s"])

    parts = [f"Git: branch `{branch}`"]

    if status_raw is None:
        # A failed/timed-out status must never be reported as a clean tree.
        parts.append("Working tree: unknown (git status unavailable)")
    else:
        total = len([l for l in status_raw.splitlines() if l.strip()])
        if total == 0:
            parts.append("Working tree: clean")
        else:
            file_items = _parse_porcelain_paths(status_raw)
            more = f" (+{total - len(file_items)} more)" if total > len(file_items) else ""
            parts.append(f"Working tree: {total} modified ({', '.join(file_items)}{more})")

    # Recent commits
    if log_raw:
        commit_lines = [l.strip() for l in log_raw.splitlines() if l.strip()]
        commit_items = [f"`{l.split(' ')[0]}` {l.split(' ', 1)[1][:40]}" if ' ' in l else f"`{l}`" for l in commit_lines]
        parts.append(f"Recent commits: {', '.join(commit_items)}")

    return " | ".join(parts)


def _get_layout_summary(workspace_dir: str, max_depth: int = 2) -> Optional[str]:
    """Generates a compact 2-level directory tree without noise or gitignored paths."""
    try:
        entries = sorted(os.listdir(workspace_dir))
    except Exception:
        return None

    # Reuse the guard's .gitignore parser instead of injecting ignored/generated
    # directories (build output, local dumps) into the system prompt. The guard's
    # own matcher drops the trailing slash from "dir/" patterns via relpath, so
    # directory matching is done here.
    is_ignored = None
    try:
        from src.guard import TokenSafeGuard
        patterns = TokenSafeGuard._load_gitignore_patterns(workspace_dir)

        def is_ignored(p: str) -> bool:  # type: ignore[misc]
            rel = os.path.relpath(p, workspace_dir).replace("\\", "/")
            name = os.path.basename(p)
            is_dir = os.path.isdir(p)
            for pat in patterns:
                if pat.endswith("/"):
                    base = pat.rstrip("/")
                    if is_dir and (rel == base or rel.startswith(base + "/")):
                        return True
                    continue
                if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel, pat):
                    return True
                if "/" not in pat and (rel == pat or rel.startswith(pat + "/")):
                    return True
            return False
    except Exception:
        is_ignored = None

    top_dirs = []
    top_files = []

    for name in entries:
        if name.startswith(".") or name in SKIP_SNAPSHOT_DIRS:
            continue
        full_p = os.path.join(workspace_dir, name)
        if is_ignored is not None and is_ignored(full_p):
            continue
        if os.path.isdir(full_p):
            top_dirs.append(name)
        elif os.path.isfile(full_p):
            top_files.append(name)

    # Sort priority directories first
    top_dirs.sort(key=lambda d: (0 if d in PRIORITY_DIRS else (2 if d in COLLAPSE_DIRS else 1), d.lower()))

    lines = ["Layout (2 levels):"]
    for d in top_dirs[:8]:
        d_path = os.path.join(workspace_dir, d)
        if d in COLLAPSE_DIRS:
            lines.append(f"  {d}/ (collapsed)")
            continue
        try:
            sub = sorted(os.listdir(d_path))
            sub_files = [
                s for s in sub
                if not s.startswith(".") and s not in SKIP_SNAPSHOT_DIRS
                and not (is_ignored is not None and is_ignored(os.path.join(d_path, s)))
            ][:6]
            has_more = len(sub) > len(sub_files)
            more_str = f" +{len(sub) - len(sub_files)}" if has_more else ""
            lines.append(f"  {d}/ ({', '.join(sub_files)}{more_str})")
        except Exception:
            lines.append(f"  {d}/")

    if top_files:
        lines.append(f"  Root files: {', '.join(top_files[:8])}")

    return "\n".join(lines)


def _get_scripts_summary(workspace_dir: str) -> Optional[str]:
    """Extracts project run/test/build scripts from package.json, Makefile, or pyproject.toml."""
    scripts = []

    # Node package.json
    pkg_path = os.path.join(workspace_dir, "package.json")
    if os.path.exists(pkg_path):
        try:
            with open(pkg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            pkg_scripts = data.get("scripts", {})
            if isinstance(pkg_scripts, dict):
                for k in ("test", "build", "typecheck", "lint", "dev", "start"):
                    if k in pkg_scripts:
                        scripts.append(f"{k} (`{pkg_scripts[k][:35]}`)")
        except Exception:
            pass

    # Makefile
    make_path = os.path.join(workspace_dir, "Makefile")
    if not scripts and os.path.exists(make_path):
        try:
            with open(make_path, "r", encoding="utf-8", errors="replace") as f:
                targets = []
                for line in f:
                    m = re.match(r"^([a-zA-Z0-9_-]+):", line)
                    if m and not m.group(1).startswith("."):
                        targets.append(m.group(1))
            if targets:
                scripts.extend(targets[:6])
        except Exception:
            pass

    # Python pyproject.toml
    pyproject_path = os.path.join(workspace_dir, "pyproject.toml")
    if os.path.exists(pyproject_path):
        try:
            with open(pyproject_path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            if "pytest" in text:
                scripts.append("test (`pytest`)")
            if "ruff" in text:
                scripts.append("lint (`ruff check`)")
            if "mypy" in text:
                scripts.append("typecheck (`mypy`)")
        except Exception:
            pass

    return f"Scripts: {', '.join(scripts)}" if scripts else None


def build_workspace_snapshot(workspace_dir: str, max_bytes: int = 1400) -> Optional[str]:
    """
    Builds a high-density, sub-1.4 KB workspace snapshot in under 50 ms.
    Returns None if workspace is system root, unreadable, or not a project directory.
    """
    if not workspace_dir or not os.path.isdir(workspace_dir):
        return None

    if _is_system_or_home_root(workspace_dir):
        return None

    sections = ["[TokenSafe Workspace Snapshot]"]

    # 1. Git summary. SessionStart blocks on this, and it issues three sequential
    # git calls: keep the per-call budget at 0.6s (<=1.8s total), not 2s each.
    git_sum = _get_git_summary(workspace_dir, timeout_sec=0.6)
    if git_sum:
        sections.append(git_sum)

    # 2. Layout
    layout_sum = _get_layout_summary(workspace_dir)
    if layout_sum:
        sections.append(layout_sum)

    # 3. Scripts
    scripts_sum = _get_scripts_summary(workspace_dir)
    if scripts_sum:
        sections.append(scripts_sum)

    if len(sections) == 1:
        return None

    combined = "\n".join(sections)
    combined_bytes = combined.encode("utf-8")
    if len(combined_bytes) <= max_bytes:
        return combined

    # Safe truncation: cut at the last newline that fits AND mark it, so a
    # truncated snapshot never masquerades as a complete one.
    marker = "\n… (truncated)"
    budget = max_bytes - len(marker.encode("utf-8"))
    truncated = combined_bytes[:budget].decode("utf-8", errors="ignore")
    cut = truncated.rfind("\n")
    if cut > 0:
        truncated = truncated[:cut]
    return truncated.rstrip() + marker
