"""
TokenSafe Scoped Search Engine.
High-efficiency, token-bounded symbol and text search across codebases.
Replaces uncontrolled grep/find commands with bounded, clean, token-efficient results.
"""
import os
import re
import stat
import fnmatch
import time
from typing import Dict, List, Optional, Any, Tuple

IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
    ".next", ".turbo", ".idea", ".vscode", "coverage", ".pytest_cache",
    ".mypy_cache", ".gemini", ".system_generated", "data", "tokensafe.egg-info",
    "Library", "Applications", "Pictures", "Movies", "Music", "Downloads",
    ".cache", ".npm", ".bun", ".rustup", ".cargo", ".local", ".output", ".vite"
}

IGNORED_EXTENSIONS = {
    ".pyc", ".pyo", ".pyd", ".so", ".dll", ".dylib", ".exe", ".bin",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg",
    ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3", ".wav",
    ".zip", ".tar", ".gz", ".tgz", ".whl", ".jar", ".db", ".sqlite",
    ".sqlite3", ".wasm"
}

LOCKFILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Pipfile.lock", "composer.lock", "Cargo.lock", "flake.lock"
}

CODE_EXTENSIONS = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".py", ".pyw", ".go", ".rs", ".java", ".cpp", ".cc", ".c", ".h", ".hpp",
    ".php", ".rb", ".vue", ".svelte", ".swift", ".kt", ".kts", ".cs", ".scala",
    ".sh", ".bash", ".zsh"
}

CONFIG_EXTENSIONS = {
    ".json", ".toml", ".yaml", ".yml", ".ini", ".conf", ".cfg", ".xml"
}

CONFIG_FILENAMES = {
    "package.json", "pyproject.toml", "plugin.json", "tsconfig.json",
    "dockerfile", "dockerfile.cloud", "docker-compose.yml", ".env.example",
    "vite.config.ts", "vite.config.js", "next.config.js", "webpack.config.js"
}


class ScopedSearch:
    """Safe, bounded search engine replacing terminal grep/find."""

    @classmethod
    def is_match_file(cls, filename: str, rel_path: str, scope: str) -> bool:
        """Determines if a file matches the target search scope."""
        basename = os.path.basename(filename)
        lower_base = basename.lower()
        ext = os.path.splitext(filename)[1].lower()

        # Reject binary / lockfiles
        if ext in IGNORED_EXTENSIONS or lower_base in LOCKFILES:
            return False

        if scope == "code":
            return ext in CODE_EXTENSIONS
        elif scope == "config":
            return lower_base in CONFIG_FILENAMES or ext in CONFIG_EXTENSIONS
        elif scope == "all":
            return True
        elif "*" in scope or "?" in scope:
            return fnmatch.fnmatch(lower_base, scope) or fnmatch.fnmatch(rel_path.lower(), scope)
        elif scope.startswith("."):
            return ext == scope.lower()
        elif re.fullmatch(r"[a-z0-9]{1,10}", scope):
            return ext == f".{scope}"
        return False

    @classmethod
    def _looks_like_regex(cls, query: str) -> bool:
        """Conservative auto-detection for use_regex=None (omitted): only
        unambiguous regex constructs switch the mode — alternation pipes and
        backslash character classes. Plain dots/brackets stay literal."""
        if "|" in query:
            return True
        if re.search(r"\\[dwsbDWSB]", query):
            return True
        return False

    @classmethod
    def _resolve_per_file(cls, per_file: int, max_results: int) -> int:
        """Hits kept per file. One hot file must not eat the whole result budget:
        a 20-hit query used to render 2 KB of near-identical lines from 2 files.
        The cap is what the model actually pays for, so it is set to the number
        of hits a caller can still act on (4-8), not to the result budget."""
        try:
            per_file = int(per_file or 0)
        except (TypeError, ValueError):
            per_file = 0
        if per_file > 0:
            return per_file
        return max(4, min(8, max_results // 3))

    @classmethod
    def _graph_symbol_index(cls, root_dir: str):
        """Cache-only view of the AST symbol index; never starts a repository crawl."""
        try:
            from src.graph_engine import CodeGraph
            graph = CodeGraph(root_dir, use_disk_cache=True)
        except Exception:
            return None
        return graph if graph.symbols else None

    @classmethod
    def _symbol_matches(
        cls,
        query: str,
        root_dir: str,
        scope: str,
        max_results: int,
        per_file: int,
    ) -> Optional[List[Dict[str, Any]]]:
        """Exact declaration lookup across every indexed file.

        An identifier query is a lookup, not a grep: the disk walk stops at a file
        budget (2,500 by default) and therefore never reaches later packages, which
        is why a symbol declared in `paidz-workers` answered "0 matches" while the
        graph index held it. Returns None when this is not a symbol-shaped query or
        no cache exists — the caller then falls back to the bounded disk scan.
        """
        if not re.fullmatch(r"[A-Za-z_$][\w$]{2,}", query or ""):
            return None
        graph = cls._graph_symbol_index(root_dir)
        if graph is None:
            return None
        q = query.lower()
        suffix_ok = any(ch.isupper() for ch in query) or "_" in query
        hits = []
        for sym in graph.symbols.values():
            name = sym.name.lower()
            if name == q:
                rank = 0
            elif suffix_ok and name.endswith(q):
                rank = 1
            else:
                continue
            if not cls.is_match_file(os.path.basename(sym.file_path), sym.file_path, scope):
                continue
            hits.append((rank, sym))
        if not hits:
            return None
        hits.sort(key=lambda item: (item[0], item[1].file_path, item[1].line_start))
        per_file_cap = cls._resolve_per_file(per_file, max_results)
        per_file_counts: Dict[str, int] = {}
        matches: List[Dict[str, Any]] = []
        for _, sym in hits:
            if len(matches) >= max_results:
                break
            seen = per_file_counts.get(sym.file_path, 0)
            if seen >= per_file_cap:
                continue
            per_file_counts[sym.file_path] = seen + 1
            text = f"{sym.kind} {sym.name}"
            if sym.signature:
                text += f" — {sym.signature}"
            if len(text) > 100:
                text = text[:97] + "..."
            matches.append({"file": sym.file_path, "line": sym.line_start, "text": text})
        return matches

    @classmethod
    def search(
        cls,
        query: str,
        project_path: Optional[str] = None,
        scope: str = "all",
        max_results: int = 25,
        case_sensitive: bool = False,
        use_regex: Optional[bool] = None,
        per_file: int = 0
    ) -> Dict[str, Any]:
        """
        Executes a bounded code/config search and returns structured results and compact markdown.
        """
        if not query or not query.strip():
            return {
                "success": False,
                "error": "Query string cannot be empty",
                "matches": [],
                "total_matches": 0,
                "markdown": "No search query provided."
            }

        root_dir = os.path.abspath(project_path or os.getcwd())
        system_roots = {"/", "/Users", "/home", "/root", os.path.expanduser("~")}
        if root_dir in system_roots:
            return {
                "success": False,
                "error": f"Refusing to search system root or home directory ({root_dir}). Please provide an explicit project directory.",
                "matches": [],
                "total_matches": 0,
                "markdown": f"Search aborted: '{root_dir}' is a system root. Please specify an explicit project directory path."
            }

        if not os.path.isdir(root_dir):
            return {
                "success": False,
                "error": f"Invalid project path: {root_dir}",
                "matches": [],
                "total_matches": 0,
                "markdown": f"Invalid project path: {root_dir}"
            }

        query = query.strip()
        scope = (scope or "all").lower().strip()
        # use_regex=None (omitted) -> auto-detect; explicit False forces literal, True forces regex
        auto_regex = use_regex is None and cls._looks_like_regex(query)
        regex_mode = bool(use_regex) if use_regex is not None else auto_regex
        if max_results is None or isinstance(max_results, bool):
            max_results = 25
        else:
            try:
                max_results = min(100, max(1, int(max_results)))
            except (TypeError, ValueError):
                max_results = 25

        # Compile search pattern
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            if regex_mode:
                pattern = re.compile(query, flags)
            else:
                pattern = re.compile(re.escape(query), flags)
        except Exception as e:
            if regex_mode and use_regex is None:
                # Malformed auto-detected regex: degrade to literal instead of failing
                regex_mode = False
                auto_regex = False
                pattern = re.compile(re.escape(query), flags)
            else:
                return {
                    "success": False,
                    "error": f"Invalid regex pattern: {str(e)}",
                    "matches": [],
                    "total_matches": 0,
                    "markdown": f"Regex compilation error: {str(e)}"
                }

        # Fast-path: SQLite FTS5 trigram BM25 search for plain case-insensitive queries
        if not regex_mode and not case_sensitive and len(query) >= 3 and not any(c in query for c in "*?[]{}()^$+\\|"):
            try:
                from src.fts_index import CodeFTSIndex
                fts = CodeFTSIndex(root_dir)
                fts_results = fts.search(query, limit=max_results)
                if fts_results:
                    per_file_cap = cls._resolve_per_file(per_file, max_results)
                    per_file_counts: Dict[str, int] = {}
                    overflow: Dict[str, int] = {}
                    fts_matches = []
                    fts_hit_files = set()
                    for r in fts_results:
                        f_rel = r["file"]
                        if cls.is_match_file(os.path.basename(f_rel), f_rel, scope):
                            seen = per_file_counts.get(f_rel, 0)
                            if seen >= per_file_cap:
                                overflow[f_rel] = overflow.get(f_rel, 0) + 1
                                continue
                            clean_line = str(r.get("content") or "").strip()
                            if len(clean_line) > 100:
                                clean_line = clean_line[:97] + "..."
                            per_file_counts[f_rel] = seen + 1
                            fts_matches.append({
                                "file": f_rel,
                                "line": r["line"],
                                "text": clean_line
                            })
                            fts_hit_files.add(f_rel)
                    if fts_matches:
                        return {
                            "success": True,
                            "query": query,
                            "scope": scope,
                            "source": "fts_trigram",
                            "files_matched": len(fts_hit_files),
                            "scanned_files": len(fts_hit_files),
                            "total_matches": len(fts_matches),
                            "overflow_matches": sum(overflow.values()),
                            "per_file_cap": per_file_cap,
                            "timed_out": False,
                            "partial": False,
                            "matches": fts_matches,
                            "markdown": cls._render_markdown(
                                query, fts_matches, len(fts_hit_files), False, max_results,
                                hit_files=fts_hit_files, scope=scope, root_dir=root_dir,
                                overflow=overflow, per_file_cap=per_file_cap,
                            ),
                        }
            except Exception:
                pass  # Seamless fallback to disk scan

        # Symbol fast path: an identifier is a lookup, and the AST index covers every
        # file while the bounded disk walk below covers only the first 2,500.
        if not regex_mode and not case_sensitive:
            symbol_matches = cls._symbol_matches(query, root_dir, scope, max_results, per_file)
            if symbol_matches is not None:
                hit_files = {m["file"] for m in symbol_matches}
                indexed = 0
                try:
                    graph = cls._graph_symbol_index(root_dir)
                    indexed = len(graph.file_symbols) if graph else 0
                except Exception:
                    indexed = 0
                markdown = cls._render_markdown(
                    query, symbol_matches, indexed or len(hit_files), False, max_results,
                    hit_files=hit_files, scope=scope, root_dir=root_dir,
                )
                if indexed:
                    markdown += (
                        f"\n_Answered from the AST symbol index ({indexed} indexed files): "
                        f"declarations only, complete across the repository._"
                    )
                return {
                    "success": True,
                    "query": query,
                    "scope": scope,
                    "source": "graph_symbol_index",
                    "files_matched": len(hit_files),
                    "scanned_files": indexed or len(hit_files),
                    "total_matches": len(symbol_matches),
                    "overflow_matches": 0,
                    "per_file_cap": cls._resolve_per_file(per_file, max_results),
                    "timed_out": False,
                    "partial": False,
                    "matches": symbol_matches,
                    "markdown": markdown,
                }


        matches: List[Dict[str, Any]] = []
        total_scanned_files = 0
        hit_files = set()
        per_file_cap = cls._resolve_per_file(per_file, max_results)
        per_file_counts: Dict[str, int] = {}
        overflow: Dict[str, int] = {}
        start_time = time.monotonic()
        timeout_seconds = 4.0
        max_scanned_files = 2500
        timed_out = False

        for dirpath, dirnames, filenames in os.walk(root_dir):
            if time.monotonic() - start_time > timeout_seconds:
                timed_out = True
                break
            if total_scanned_files >= max_scanned_files:
                break

            # Prune ignored directories in-place (allow safe dot-config dirs like .github/.claude/.gemini)
            dirnames[:] = [
                d for d in sorted(dirnames)
                if d not in IGNORED_DIRS and (not d.startswith(".") or d in (".github", ".gitlab", ".claude", ".gemini", ".agents"))
            ]

            manifest_priority = {"package.json", "pyproject.toml", "cargo.toml", "go.mod"}
            for fname in sorted(filenames, key=lambda s: (0 if s.lower() in manifest_priority else 1, s.lower())):
                if time.monotonic() - start_time > timeout_seconds:
                    timed_out = True
                    break
                if total_scanned_files >= max_scanned_files:
                    break
                full_path = os.path.join(dirpath, fname)
                rel_path = os.path.relpath(full_path, root_dir).replace("\\", "/")

                if not cls.is_match_file(fname, rel_path, scope):
                    continue

                # Skip non-regular files (FIFOs/pipes/devices), >1MB files, and binary files (NUL byte probe)
                try:
                    st = os.stat(full_path)
                    if not stat.S_ISREG(st.st_mode) or st.st_size > 1024 * 1024:
                        continue
                    with open(full_path, "rb") as bf:
                        if b"\x00" in bf.read(2048):
                            continue
                except OSError:
                    continue

                total_scanned_files += 1

                try:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        for line_idx, line in enumerate(f, 1):
                            if len(line) > 8192:
                                continue
                            if pattern.search(line):
                                clean_line = line.strip()
                                # Truncate long lines to 100 characters for compact token output
                                if len(clean_line) > 100:
                                    clean_line = clean_line[:97] + "..."

                                seen_in_file = per_file_counts.get(rel_path, 0)
                                if seen_in_file >= per_file_cap:
                                    overflow[rel_path] = overflow.get(rel_path, 0) + 1
                                    continue
                                per_file_counts[rel_path] = seen_in_file + 1
                                matches.append({
                                    "file": rel_path,
                                    "line": line_idx,
                                    "text": clean_line
                                })
                                hit_files.add(rel_path)

                                if len(matches) >= max_results:
                                    break
                except Exception:
                    continue

                if len(matches) >= max_results:
                    break
            if len(matches) >= max_results:
                break

        markdown = cls._render_markdown(
            query,
            matches,
            total_scanned_files,
            timed_out,
            max_results,
            hit_files=hit_files,
            scope=scope,
            root_dir=root_dir,
            auto_regex=auto_regex,
            regex_mode=regex_mode,
            timeout_seconds=timeout_seconds,
            max_scanned_files=max_scanned_files,
            overflow=overflow,
            per_file_cap=per_file_cap,
        )

        partial = bool(timed_out or total_scanned_files >= max_scanned_files)
        if partial and not matches:
            markdown += (
                "\n_Scan stopped at its safety budget, so coverage was partial: the match may live "
                "in a file that was not reached. Narrow 'path'/'project_path' for an exhaustive scan._"
            )
            if re.fullmatch(r"[A-Za-z_$][\w$]{2,}", query or ""):
                markdown += (
                    f"\n_Declaration lookup: `oumv(action='index', query='{query}')` searches the complete "
                    "AST symbol index instead of the bounded disk walk._"
                )

        return {
            "success": True,
            "query": query,
            "scope": scope,
            "source": "disk_scan",
            "total_matches": len(matches),
            "overflow_matches": sum(overflow.values()),
            "per_file_cap": per_file_cap,
            "files_matched": len(hit_files),
            "scanned_files": total_scanned_files,
            "timed_out": timed_out,
            "partial": partial,
            "coverage": {
                "file_budget": max_scanned_files,
                "time_budget_seconds": timeout_seconds,
                "stopped_early": partial,
            },
            "matches": matches,
            "markdown": markdown,
        }

    @classmethod
    def _render_markdown(
        cls,
        query: str,
        matches: List[Dict[str, Any]],
        total_scanned_files: int,
        timed_out: bool,
        max_results: int,
        hit_files: Optional[set] = None,
        scope: str = "all",
        root_dir: str = "",
        auto_regex: bool = False,
        regex_mode: bool = False,
        timeout_seconds: float = 4.0,
        max_scanned_files: int = 2500,
        overflow: Optional[Dict[str, int]] = None,
        per_file_cap: int = 0,
    ) -> str:
        """Generates compact bounded Markdown output for both FTS5 and disk search paths.

        Hits are grouped per file: the path is printed once per file instead of on
        every line, which keeps a 20-hit query near 1 KB instead of 2 KB.
        """
        if hit_files is None:
            hit_files = {m["file"] for m in matches if "file" in m}
        overflow = overflow or {}
        display_root = os.path.basename(root_dir) or root_dir or "."
        mode_suffix = ", auto-regex" if auto_regex and regex_mode else ""
        more_hidden = sum(overflow.values())
        count_label = f"{len(matches)} match{'es' if len(matches) != 1 else ''}"
        if more_hidden:
            count_label = f"{len(matches)} shown, {more_hidden} more"
        lines = [
            f"### TokenSafe Scoped Search: '{query}' ({count_label} in {len(hit_files)} file{'s' if len(hit_files) != 1 else ''}, dir: `{display_root}`, scope: {scope}{mode_suffix})"
        ]
        if not matches:
            lines.append(f"_No matches found across {total_scanned_files} scanned files in `{root_dir}`._")
            if auto_regex and regex_mode:
                lines.append("_Auto-regex mode found nothing; pass use_regex=false to force a literal search._")
        else:
            by_file: Dict[str, List[Dict[str, Any]]] = {}
            for m in matches:
                by_file.setdefault(m.get("file", ""), []).append(m)
            multi_file = len(by_file) > 1
            for fpath, file_matches in by_file.items():
                if multi_file:
                    lines.append(f"**{fpath}**")
                for m in file_matches:
                    txt = m.get("text") if "text" in m else m.get("content", "")
                    if multi_file:
                        lines.append(f"  - L{m['line']}: `{txt}`")
                    else:
                        lines.append(f"- `{fpath}:{m['line']}`: `{txt}`")
                hidden = overflow.get(fpath, 0)
                if hidden:
                    lines.append(f"  … +{hidden} more in this file")

        if len(matches) >= max_results:
            lines.append(f"_Results capped at {max_results} matches._")
        elif more_hidden:
            lines.append(f"_Kept at most {per_file_cap} hits per file; raise `per_file` for every occurrence._")
        elif timed_out:
            lines.append(f"_Search bounded at {timeout_seconds}s safety limit ({total_scanned_files} files scanned). Narrow 'project_path' for exhaustive scan._")
        elif total_scanned_files >= max_scanned_files:
            lines.append(f"_Scan bounded at {max_scanned_files} files limit. Narrow 'project_path' if needed._")

        return "\n".join(lines)
