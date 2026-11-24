"""
TokenSafe Batch Search Engine (v3.5.0).

Executes up to 20 heterogeneous code, path, and read-only git searches
concurrently in one MCP call, cutting round-trip LLM turns and avoiding
per-command remote safety evaluation overheads.

Supported query types:
- 'text': literal substring search.
- 'regex': regular expression pattern matching.
- 'glob': file name and path pattern matching.
- 'fuzzy': case-insensitive fuzzy path and symbol search.
- 'git': read-only git queries (status, diff, log, show, branch, rev-parse, ls-files).
"""

import os
import re
import fnmatch
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, List, Optional, Tuple

from src.scoped_search import ScopedSearch, IGNORED_DIRS, IGNORED_EXTENSIONS, CODE_EXTENSIONS
from src.format_parsers.ansi import strip_ansi

MAX_BATCH_QUERIES = 20
MAX_OUTPUT_CHARS = 8000
DEFAULT_QUERY_RESULTS = 25

ALLOWED_GIT_COMMANDS = {
    "status", "diff", "log", "show", "branch", "rev-parse",
    "ls-files", "blame", "shortlog", "describe", "tag"
}

HARDENED_GIT_BASE_ARGS = [
    "--no-optional-locks",
    "-c", "core.fsmonitor=false",
    "-c", "diff.external=",
    "-c", "gpg.program=false",
    "--no-pager"
]


def _is_git_command_safe(args_str: str) -> Tuple[bool, str, List[str]]:
    """Checks if git command is strictly read-only and returns tokens."""
    tokens = [t for t in re.split(r"\s+", args_str.strip()) if t]
    if not tokens:
        return False, "Empty git command.", []
    subcmd = tokens[0].lower()
    if subcmd.startswith("git"):
        tokens = tokens[1:]
        if not tokens:
            return False, "Empty git command.", []
        subcmd = tokens[0].lower()

    if subcmd not in ALLOWED_GIT_COMMANDS:
        return False, f"Git subcommand '{subcmd}' is not in the read-only allowlist ({', '.join(sorted(ALLOWED_GIT_COMMANDS))}).", []

    # Block destructive or interactive flags
    for tok in tokens:
        if tok in ("--delete", "-d", "-D", "-m", "-M", "--hard", "--reset", "--force", "-f", "--interactive", "-i"):
            return False, f"Destructive flag '{tok}' is disallowed in batch_search.", []

    return True, "", tokens


def _run_git_query(pattern: str, cwd: str, timeout_sec: int = 5) -> Dict[str, Any]:
    """Runs a read-only git query."""
    is_safe, err_msg, tokens = _is_git_command_safe(pattern)
    if not is_safe:
        return {"error": err_msg, "matches": [], "count": 0}

    git_bin = shutil.which("git")
    if not git_bin:
        return {"error": "git executable not found in PATH", "matches": [], "count": 0}

    cmd = [git_bin, *HARDENED_GIT_BASE_ARGS, *tokens]
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"

    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_sec,
            env=env
        )
        if proc.returncode != 0:
            err = proc.stderr.strip() or f"git exited with code {proc.returncode}"
            return {"error": err, "matches": [], "count": 0}

        raw_out = strip_ansi(proc.stdout or "").strip()
        lines = [l for l in raw_out.splitlines() if l.strip()]
        return {"matches": lines[:50], "count": len(lines), "raw": "\n".join(lines[:50])}
    except subprocess.TimeoutExpired:
        return {"error": f"git query timed out after {timeout_sec}s", "matches": [], "count": 0}
    except Exception as e:
        return {"error": f"git execution error: {e}", "matches": [], "count": 0}


def _run_rg_query(
    pattern: str,
    cwd: str,
    path_filter: Optional[str] = None,
    is_regex: bool = False,
    case_sensitive: bool = False,
    file_type: Optional[str] = None,
    glob_filter: Optional[str] = None,
    max_results: int = 25
) -> Optional[List[Dict[str, Any]]]:
    """Attempts fast search via ripgrep if available."""
    rg_bin = shutil.which("rg")
    if not rg_bin:
        return None

    cmd = [rg_bin, "--no-heading", "--line-number", "--color=never", "--max-count", str(max_results)]
    if not is_regex:
        cmd.append("--fixed-strings")
    if not case_sensitive:
        cmd.append("--ignore-case")
    else:
        cmd.append("--case-sensitive")

    if file_type:
        cmd.extend(["--type", file_type])
    if glob_filter:
        cmd.extend(["--glob", glob_filter])

    # Ignore default heavy folders
    for d in ("node_modules", ".git", "dist", "build", ".venv", "__pycache__"):
        cmd.extend(["--glob", f"!{d}/**"])

    cmd.append(pattern)
    target_path = os.path.normpath(os.path.join(cwd, path_filter)) if path_filter else cwd
    cmd.append(target_path)

    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5
        )
        matches = []
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split(":", 2)
            if len(parts) >= 3:
                f_path = os.path.relpath(parts[0], cwd)
                try:
                    l_num = int(parts[1])
                except ValueError:
                    l_num = 0
                snippet = parts[2].strip()
                matches.append({"file": f_path, "line": l_num, "content": snippet})
            if len(matches) >= max_results:
                break
        return matches
    except Exception:
        return None


def _run_python_text_search(
    pattern: str,
    cwd: str,
    path_filter: Optional[str] = None,
    is_regex: bool = False,
    case_sensitive: bool = False,
    scope: str = "code",
    max_results: int = 25
) -> List[Dict[str, Any]]:
    """Pure Python search fallback."""
    search_root = os.path.normpath(os.path.join(cwd, path_filter)) if path_filter else cwd
    if not os.path.exists(search_root):
        return []

    regex = None
    if is_regex:
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(pattern, flags)
        except re.error:
            regex = None

    matches = []
    target_lower = pattern if case_sensitive else pattern.lower()

    for root, dirs, files in os.walk(search_root):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith(".")]
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in IGNORED_EXTENSIONS:
                continue
            if scope == "code" and ext not in CODE_EXTENSIONS:
                continue

            full_path = os.path.join(root, f)
            rel_p = os.path.relpath(full_path, cwd)

            try:
                with open(full_path, "r", encoding="utf-8", errors="replace") as fp:
                    for line_idx, line in enumerate(fp):
                        hit = False
                        if regex:
                            hit = bool(regex.search(line))
                        elif case_sensitive:
                            hit = pattern in line
                        else:
                            hit = target_lower in line.lower()

                        if hit:
                            matches.append({
                                "file": rel_p,
                                "line": line_idx + 1,
                                "content": line.strip()[:180]
                            })
                            if len(matches) >= max_results:
                                return matches
            except Exception:
                continue

    return matches


def _run_glob_search(pattern: str, cwd: str, path_filter: Optional[str] = None, max_results: int = 50) -> List[str]:
    """Finds matching files by glob pattern."""
    search_root = os.path.normpath(os.path.join(cwd, path_filter)) if path_filter else cwd
    if not os.path.exists(search_root):
        return []

    results = []
    for root, dirs, files in os.walk(search_root):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith(".")]
        for f in files:
            rel_p = os.path.relpath(os.path.join(root, f), cwd)
            if fnmatch.fnmatch(rel_p, pattern) or fnmatch.fnmatch(f, pattern):
                results.append(rel_p)
                if len(results) >= max_results:
                    return results
    return results


def _run_fuzzy_search(pattern: str, cwd: str, path_filter: Optional[str] = None, max_results: int = 30) -> List[str]:
    """Case-insensitive fuzzy match across relative file paths."""
    search_root = os.path.normpath(os.path.join(cwd, path_filter)) if path_filter else cwd
    if not os.path.exists(search_root):
        return []

    pat_lower = pattern.lower().replace(" ", "")
    results = []
    for root, dirs, files in os.walk(search_root):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith(".")]
        for f in files:
            rel_p = os.path.relpath(os.path.join(root, f), cwd)
            name_lower = rel_p.lower()
            # Subsequence match
            it = iter(name_lower)
            if all(ch in it for ch in pat_lower):
                results.append(rel_p)
                if len(results) >= max_results:
                    return results
    return results


def _execute_single_query(query_dict: Dict[str, Any], cwd: str) -> Dict[str, Any]:
    """Executes a single search query dict."""
    pattern = str(query_dict.get("pattern") or query_dict.get("query") or "").strip()
    q_type = str(query_dict.get("type") or "text").lower().strip()
    path_filter = query_dict.get("path")
    max_res = int(query_dict.get("max_results") or DEFAULT_QUERY_RESULTS)
    case_sensitive = bool(query_dict.get("case_sensitive"))
    output_mode = str(query_dict.get("output_mode") or "content").lower()
    file_type = query_dict.get("file_type")
    glob_filter = query_dict.get("glob")

    if not pattern and q_type != "git":
        return {"type": q_type, "pattern": pattern, "error": "Empty pattern.", "matches": [], "count": 0}

    if q_type == "git":
        res = _run_git_query(pattern, cwd)
        return {"type": "git", "pattern": pattern, **res}

    if q_type == "glob":
        matched_paths = _run_glob_search(pattern, cwd, path_filter=path_filter, max_results=max_res)
        return {"type": "glob", "pattern": pattern, "matches": matched_paths, "count": len(matched_paths)}

    if q_type == "fuzzy":
        matched_paths = _run_fuzzy_search(pattern, cwd, path_filter=path_filter, max_results=max_res)
        return {"type": "fuzzy", "pattern": pattern, "matches": matched_paths, "count": len(matched_paths)}

    is_regex = (q_type == "regex")
    # Try ripgrep first
    rg_matches = _run_rg_query(
        pattern=pattern,
        cwd=cwd,
        path_filter=path_filter,
        is_regex=is_regex,
        case_sensitive=case_sensitive,
        file_type=file_type,
        glob_filter=glob_filter,
        max_results=max_res
    )

    matches = rg_matches if rg_matches is not None else _run_python_text_search(
        pattern=pattern,
        cwd=cwd,
        path_filter=path_filter,
        is_regex=is_regex,
        case_sensitive=case_sensitive,
        max_results=max_res
    )

    if output_mode == "files_with_matches":
        unique_files = list(dict.fromkeys(m["file"] for m in matches if isinstance(m, dict) and "file" in m))
        return {"type": q_type, "pattern": pattern, "matches": unique_files, "count": len(unique_files)}
    elif output_mode == "count":
        return {"type": q_type, "pattern": pattern, "count": len(matches)}

    return {"type": q_type, "pattern": pattern, "matches": matches, "count": len(matches)}


def execute_batch_search(
    queries: List[Dict[str, Any]],
    cwd: Optional[str] = None
) -> Dict[str, Any]:
    """
    Executes up to 20 mixed search queries concurrently and returns a consolidated report.
    """
    if not isinstance(queries, list) or not queries:
        return {
            "success": False,
            "error": "Queries must be a non-empty array of query specifications.",
            "results": [],
            "summary_md": "Error: queries array is empty."
        }

    if len(queries) > MAX_BATCH_QUERIES:
        return {
            "success": False,
            "error": f"Batch search exceeds maximum of {MAX_BATCH_QUERIES} queries (got {len(queries)}).",
            "results": [],
            "summary_md": "Error: query count exceeds maximum."
        }

    base_dir = os.path.abspath(cwd or os.getcwd())
    results: List[Dict[str, Any]] = [{} for _ in queries]

    # Concurrent execution across queries
    with ThreadPoolExecutor(max_workers=min(len(queries), 8)) as executor:
        future_to_idx = {
            executor.submit(_execute_single_query, q, base_dir): i
            for i, q in enumerate(queries)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                results[idx] = {"error": str(e), "matches": [], "count": 0}

    # Format into markdown summary
    md_lines: List[str] = [f"## TokenSafe Batch Search Report ({len(queries)} queries)"]
    for i, res in enumerate(results):
        q_type = res.get("type", "search")
        pattern = res.get("pattern", "")
        count = res.get("count", 0)
        err = res.get("error")

        if err:
            md_lines.append(f"\n### Q{i+1} [{q_type}] `{pattern}` — Error: {err}")
            continue

        md_lines.append(f"\n### Q{i+1} [{q_type}] `{pattern}` — {count} match(es)")
        matches = res.get("matches") or []

        if q_type == "git":
            raw_git = res.get("raw")
            if raw_git:
                md_lines.append("```text\n" + raw_git[:1200] + "\n```")
            elif not matches:
                md_lines.append("*(clean / no output)*")
        elif isinstance(matches, list):
            for m in matches[:15]:
                if isinstance(m, dict):
                    f_name = m.get("file", "")
                    l_num = m.get("line", "")
                    content = m.get("content", "")
                    md_lines.append(f"- `{f_name}:{l_num}`: {content}")
                elif isinstance(m, str):
                    md_lines.append(f"- `{m}`")
            if len(matches) > 15:
                md_lines.append(f"- *(+{len(matches) - 15} more matches)*")

    summary_md = "\n".join(md_lines)
    return {
        "success": True,
        "query_count": len(queries),
        "results": results,
        "summary_md": summary_md
    }
