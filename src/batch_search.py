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
- 'git': read-only git queries (status, diff, log, show, branch, rev-parse,
  ls-files, blame, shortlog, describe, tag) with writing/escaping flags blocked.

`path` may point to a directory or to a single file. A missing path, an
uncompilable regex, and an unknown `file_type` are reported as per-query
errors instead of silent zero-hit results.
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
    "-c", "core.quotePath=false",
    "-c", "diff.external=",
    "-c", "gpg.program=false",
    "--no-pager",
]

# Subcommands that support --no-textconv (a per-command option, not a global one)
_TEXTCONV_SUBCOMMANDS = frozenset({"diff", "log", "show", "blame"})


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

    # Block destructive, config-overriding, escaping, or helper-executing options.
    # --no-index/--contents/--pathspec-from-file read arbitrary files outside the
    # workspace root the MCP handler confines sub-queries to; --textconv runs
    # repo-configured helpers, so it is blocked and --no-textconv is forced below.
    _BLOCKED_GIT_PREFIXES = (
        "-c", "-C", "--git-dir", "--work-tree", "--exec-path", "--output",
        "--ext-diff", "--exec", "--no-index", "--contents", "--pathspec-from-file",
        "--textconv",
    )
    _READ_ONLY_BRANCH_TAG_FLAGS = {
        "-l", "--list", "-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose",
        "--show-current", "--contains", "--no-contains", "--merged", "--no-merged",
        "--sort", "--format", "--points-at", "--color", "--no-color", "--abbrev",
    }
    for idx, tok in enumerate(tokens[1:], start=1):
        low_tok = tok.lower()
        if tok in ("--delete", "-d", "-D", "-m", "-M", "--hard", "--reset", "--force", "-f", "--interactive", "-i"):
            return False, f"Destructive flag '{tok}' is disallowed in batch_search.", []
        if any(tok == p or tok.startswith(p + "=") for p in _BLOCKED_GIT_PREFIXES):
            return False, f"Git flag '{tok}' is disallowed in batch_search.", []
        if subcmd in ("branch", "tag") and not tok.startswith("-"):
            prev = tokens[idx - 1]
            if prev not in ("--contains", "--no-contains", "--merged", "--no-merged", "--sort", "--format", "--points-at", "-l", "--list"):
                return False, f"Positional argument '{tok}' with git {subcmd} is disallowed (read-only flags only).", []
        elif subcmd in ("branch", "tag") and tok.startswith("-") and tok.split("=", 1)[0] not in _READ_ONLY_BRANCH_TAG_FLAGS:
            return False, f"Flag '{tok}' with git {subcmd} is not in read-only list.", []

    return True, "", tokens


def _run_git_query(pattern: str, cwd: str, timeout_sec: int = 5, max_results: int = 50) -> Dict[str, Any]:
    """Runs a read-only git query."""
    is_safe, err_msg, tokens = _is_git_command_safe(pattern)
    if not is_safe:
        return {"error": err_msg, "matches": [], "count": 0}

    git_bin = shutil.which("git")
    if not git_bin:
        return {"error": "git executable not found in PATH", "matches": [], "count": 0}

    # --no-textconv is a per-subcommand option (diff/log/show/blame), so it must
    # follow the subcommand, not sit in the global option block.
    if tokens and tokens[0].lower() in _TEXTCONV_SUBCOMMANDS:
        cmd = [git_bin, *HARDENED_GIT_BASE_ARGS, tokens[0], "--no-textconv", *tokens[1:]]
    else:
        cmd = [git_bin, *HARDENED_GIT_BASE_ARGS, *tokens]

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"
    # Never let git consume the MCP server's own stdin (rev-parse --parseopt blocks on it).
    stdin_devnull = subprocess.DEVNULL

    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            stdin=stdin_devnull,
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
        limited = lines[:max_results]
        return {
            "matches": limited,
            # count is the true number of matching lines; matches may be truncated.
            "count": len(lines),
            "truncated": len(lines) > len(limited),
            "raw": "\n".join(limited),
        }
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
    max_results: int = 25,
    scope: str = "all"
) -> Optional[List[Dict[str, Any]]]:
    """Attempts fast search via ripgrep if available."""
    rg_bin = shutil.which("rg")
    if not rg_bin:
        return None

    # --with-filename is mandatory: for a single-file target ripgrep omits the
    # path prefix, so every output line would be unparseable below (silent 0 hits).
    cmd = [rg_bin, "--no-heading", "--with-filename", "--line-number", "--color=never", "--max-count", str(max_results)]
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

    target_path = os.path.normpath(os.path.join(cwd, path_filter)) if path_filter else cwd
    cmd.extend(["--", pattern, target_path])

    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5
        )
        err_text = (proc.stderr or "").strip()
        # rg exits 1 for "no matches" (not an error) and 2 for real failures.
        if proc.returncode not in (0, 1) and err_text:
            raise RuntimeError(f"ripgrep failed: {err_text.splitlines()[0]}")
        scope_all = str(scope or "all").lower() in ("", "all")
        matches = []
        _rg_line_re = re.compile(r"^(.*):(\d+):(.*)$")
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            m_line = _rg_line_re.match(line)
            if m_line:
                raw_file, raw_lnum, raw_txt = m_line.groups()
                f_path = os.path.relpath(raw_file, cwd)
                # ripgrep has no notion of TokenSafe scopes: apply the same matcher
                # the Python fallback uses so results do not change with rg presence.
                if not scope_all:
                    try:
                        from src.scoped_search import ScopedSearch
                        if not ScopedSearch.is_match_file(f_path, f_path, str(scope).lower()):
                            continue
                    except Exception:
                        pass
                try:
                    l_num = int(raw_lnum)
                except ValueError:
                    l_num = 0
                snippet = raw_txt.strip()[:180]
                matches.append({"file": f_path, "line": l_num, "content": snippet})
            if len(matches) >= max_results:
                break
        return matches
    except Exception:
        return None


def _iter_search_files(search_root: str, cwd: str):
    """Yields (full_path, rel_path) for an explicit file target or a directory tree."""
    if os.path.isfile(search_root):
        yield search_root, os.path.relpath(search_root, cwd)
        return
    for root, dirs, files in os.walk(search_root):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith(".")]
        for f in files:
            full_path = os.path.join(root, f)
            yield full_path, os.path.relpath(full_path, cwd)


# Approximation of ripgrep's --type map for the pure-Python fallback, so the
# file_type filter is not silently ignored when rg is unavailable.
FILE_TYPE_EXTENSIONS = {
    "typescript": {".ts", ".tsx", ".mts", ".cts"},
    "ts": {".ts", ".tsx", ".mts", ".cts"},
    "javascript": {".js", ".jsx", ".mjs", ".cjs"},
    "js": {".js", ".jsx", ".mjs", ".cjs"},
    "python": {".py", ".pyi"},
    "py": {".py", ".pyi"},
    "rust": {".rs"},
    "go": {".go"},
    "java": {".java"},
    "ruby": {".rb"},
    "php": {".php"},
    "c": {".c", ".h"},
    "cpp": {".cpp", ".cc", ".cxx", ".hpp", ".hh"},
    "csharp": {".cs"},
    "markdown": {".md", ".markdown"},
    "md": {".md", ".markdown"},
    "json": {".json"},
    "yaml": {".yaml", ".yml"},
    "toml": {".toml"},
    "html": {".html", ".htm"},
    "css": {".css", ".scss", ".sass", ".less"},
    "sql": {".sql"},
    "shell": {".sh", ".bash", ".zsh"},
    "swift": {".swift"},
    "kotlin": {".kt", ".kts"},
}


def _run_python_text_search(
    pattern: str,
    cwd: str,
    path_filter: Optional[str] = None,
    is_regex: bool = False,
    case_sensitive: bool = False,
    scope: str = "all",
    glob_filter: Optional[str] = None,
    file_type: Optional[str] = None,
    max_results: int = 25
) -> List[Dict[str, Any]]:
    """Pure Python search fallback. Raises ValueError for an unusable regex or file_type."""
    search_root = os.path.normpath(os.path.join(cwd, path_filter)) if path_filter else cwd
    if not os.path.exists(search_root):
        return []

    type_exts = None
    if file_type:
        type_exts = FILE_TYPE_EXTENSIONS.get(str(file_type).lower().lstrip("."))
        if type_exts is None:
            raise ValueError(
                f"unsupported file_type '{file_type}' (known: {', '.join(sorted(FILE_TYPE_EXTENSIONS))})"
            )

    regex = None
    if is_regex:
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            # Never degrade to a literal search: that silently returns bogus hits.
            raise ValueError(f"invalid regular expression: {e}")

    matches = []
    target_lower = pattern if case_sensitive else pattern.lower()
    explicit_file = os.path.isfile(search_root)

    for full_path, rel_p in _iter_search_files(search_root, cwd):
        # An explicitly named file is searched as-is: extension, scope, and glob
        # filters only make sense when walking a directory tree.
        if not explicit_file:
            ext = os.path.splitext(full_path)[1].lower()
            if ext in IGNORED_EXTENSIONS:
                continue
            # One matcher for every scope ("code", "config", extensions, globs):
            # reusing ScopedSearch keeps the fallback identical to the rg path.
            if str(scope or "all").lower() not in ("", "all"):
                try:
                    from src.scoped_search import ScopedSearch
                    if not ScopedSearch.is_match_file(full_path, rel_p, str(scope).lower()):
                        continue
                except Exception:
                    if scope == "code" and ext not in CODE_EXTENSIONS:
                        continue
            if type_exts is not None and ext not in type_exts:
                continue
            if glob_filter and not (fnmatch.fnmatch(os.path.basename(full_path), glob_filter) or fnmatch.fnmatch(rel_p, glob_filter)):
                continue

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

    if os.path.isfile(search_root):
        rel_p = os.path.relpath(search_root, cwd)
        if fnmatch.fnmatch(rel_p, pattern) or fnmatch.fnmatch(os.path.basename(search_root), pattern):
            return [rel_p]
        return []

    results = []
    for full_path, rel_p in _iter_search_files(search_root, cwd):
        if fnmatch.fnmatch(rel_p, pattern) or fnmatch.fnmatch(os.path.basename(full_path), pattern):
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
    for _, rel_p in _iter_search_files(search_root, cwd):
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
    scope = str(query_dict.get("scope") or "all").lower()

    if not pattern and q_type != "git":
        return {"type": q_type, "pattern": pattern, "error": "Empty pattern.", "matches": [], "count": 0}

    # A path that does not exist must be an explicit error: an empty result list
    # is indistinguishable from a genuine zero-hit search.
    if path_filter:
        search_root = os.path.normpath(os.path.join(cwd, str(path_filter)))
        if not os.path.exists(search_root):
            return {
                "type": q_type,
                "pattern": pattern,
                "error": f"path does not exist: {path_filter}",
                "matches": [],
                "count": 0,
            }

    if q_type == "git":
        git_cwd = os.path.normpath(os.path.join(cwd, str(path_filter))) if path_filter else cwd
        res = _run_git_query(pattern, git_cwd, max_results=max_res)
        return {"type": "git", "pattern": pattern, **res}

    if q_type == "glob":
        matched_paths = _run_glob_search(pattern, cwd, path_filter=path_filter, max_results=max_res)
        if output_mode == "count":
            return {"type": "glob", "pattern": pattern, "count": len(matched_paths)}
        return {"type": "glob", "pattern": pattern, "matches": matched_paths, "count": len(matched_paths)}

    if q_type == "fuzzy":
        matched_paths = _run_fuzzy_search(pattern, cwd, path_filter=path_filter, max_results=max_res)
        if output_mode == "count":
            return {"type": "fuzzy", "pattern": pattern, "count": len(matched_paths)}
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
        max_results=max_res,
        scope=scope
    )

    try:
        matches = rg_matches if rg_matches is not None else _run_python_text_search(
            pattern=pattern,
            cwd=cwd,
            path_filter=path_filter,
            is_regex=is_regex,
            case_sensitive=case_sensitive,
            scope=scope,
            glob_filter=glob_filter,
            file_type=file_type,
            max_results=max_res
        )
    except ValueError as e:
        return {"type": q_type, "pattern": pattern, "error": str(e), "matches": [], "count": 0}

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
                md_lines.append("```text\n" + raw_git + "\n```")
            elif not matches:
                md_lines.append("*(clean / no output)*")
        elif isinstance(matches, list):
            # Honor the caller's max_results instead of a fixed 15-item cap;
            # the Zero-Spill Guard folds oversized payloads at the MCP boundary.
            for m in matches:
                if isinstance(m, dict):
                    f_name = m.get("file", "")
                    l_num = m.get("line", "")
                    content = m.get("content", "")
                    md_lines.append(f"- `{f_name}:{l_num}`: {content}")
                elif isinstance(m, str):
                    md_lines.append(f"- `{m}`")

    summary_md = "\n".join(md_lines)
    errored = [r for r in results if isinstance(r, dict) and r.get("error")]
    return {
        "success": not (errored and len(errored) == len(results)),
        "query_count": len(queries),
        "results": results,
        "summary_md": summary_md
    }
