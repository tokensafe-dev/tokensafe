"""
TokenSafe SQLite FTS5 Code Search Engine (v3.3.0).

High-performance, BM25-ranked full-text search across codebases using SQLite's
native FTS5 module with the trigram tokenizer.
- Zero external dependencies: uses Python's standard `sqlite3` library.
- Sub-millisecond substring & identifier matching (camelCase, snake_case, symbols).
- Built-in BM25 relevance ranking.
- Incremental indexing: checks file mtime & size to only re-index modified files.
"""

import os
import sqlite3
import time
import hashlib
import shutil
from typing import List, Dict, Any, Optional, Tuple

from src.paths import resolve_data_dir

# Ignored directories and extensions reused for clean index
IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
    ".next", ".turbo", ".idea", ".vscode", "coverage", ".pytest_cache",
    ".mypy_cache", ".gemini", ".system_generated", "data", "tokensafe.egg-info",
    "Library", "Applications", "Downloads", ".cache", ".npm", ".bun", ".rustup", ".cargo"
}

IGNORED_EXTS = {
    ".pyc", ".pyo", ".pyd", ".so", ".dll", ".dylib", ".exe", ".bin",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg",
    ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3", ".wav",
    ".zip", ".tar", ".gz", ".tgz", ".whl", ".jar", ".db", ".sqlite",
    ".sqlite3", ".wasm"
}

CODE_EXTS = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".py", ".pyw", ".go", ".rs", ".java", ".cpp", ".cc", ".c", ".h", ".hpp",
    ".php", ".rb", ".vue", ".svelte", ".swift", ".kt", ".kts", ".cs",
    ".json", ".toml", ".yaml", ".yml", ".md", ".sql", ".sh", ".bash"
}


from src.paths import resolve_data_dir, ensure_tokensafe_gitignore


def _resolve_fts_db_path(project_root: str) -> str:
    """Returns path to the SQLite FTS database for a project.

    Always stores index in global user cache ~/.tokensafe/cache/fts/<project_hash>.db
    so the project working tree is never polluted with binary database files.
    """
    ensure_tokensafe_gitignore(project_root)

    # Legacy cleanup: if fts.db was mistakenly created in project .tokensafe, migrate or remove
    proj_tokensafe = os.path.join(project_root, ".tokensafe")
    legacy_file = os.path.join(proj_tokensafe, "fts.db")

    cache_dir = os.path.join(resolve_data_dir(), "cache", "fts")
    os.makedirs(cache_dir, exist_ok=True)
    p_hash = hashlib.sha256(os.path.abspath(project_root).encode("utf-8")).hexdigest()[:16]
    target_path = os.path.join(cache_dir, f"fts_{p_hash}.db")

    if os.path.isfile(legacy_file) and not os.path.isfile(target_path):
        try:
            shutil.move(legacy_file, target_path)
            for extra in (f"{legacy_file}-wal", f"{legacy_file}-shm"):
                if os.path.isfile(extra):
                    os.remove(extra)
        except Exception:
            pass

    return target_path


class CodeFTSIndex:
    """SQLite FTS5 Full-Text Code Search Index with BM25 Ranking."""

    def __init__(self, project_root: str, db_path: Optional[str] = None):
        self.project_root = os.path.abspath(project_root)
        self.db_path = db_path or _resolve_fts_db_path(self.project_root)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        con = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            con.execute("PRAGMA journal_mode = WAL;")
            con.execute("PRAGMA synchronous = NORMAL;")
        except Exception:
            con.close()
            raise
        return con

    def _init_db(self):
        try:
            con = self._get_connection()
            try:
                with con:
                    con.execute("""
                        CREATE TABLE IF NOT EXISTS file_meta (
                            rel_path TEXT PRIMARY KEY,
                            mtime REAL,
                            size INTEGER
                        );
                    """)
                    table_check = con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='code_lines';"
                    ).fetchone()
                    if not table_check:
                        try:
                            con.execute("""
                                CREATE VIRTUAL TABLE code_lines USING fts5(
                                    rel_path,
                                    line_no UNINDEXED,
                                    content,
                                    tokenize = "trigram"
                                );
                            """)
                        except sqlite3.OperationalError:
                            con.execute("""
                                CREATE VIRTUAL TABLE code_lines USING fts5(
                                    rel_path,
                                    line_no UNINDEXED,
                                    content,
                                    tokenize = "unicode61"
                                );
                            """)
            finally:
                con.close()
        except Exception:
            pass

    def index_files(self, max_files: int = 5000, max_file_size_bytes: int = 512 * 1024) -> Dict[str, Any]:
        """
        Incrementally scans the project directory and updates the FTS5 index.
        Returns indexing statistics.
        """
        t0 = time.time()
        scanned_files = 0
        updated_files = 0
        indexed_lines = 0

        con = self._get_connection()
        try:
            with con:
                # Load existing metadata
                cur = con.execute("SELECT rel_path, mtime, size FROM file_meta;")
                known_meta = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
                found_paths = set()

                files_to_index = []

                for root, dirs, files in os.walk(self.project_root):
                    dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith(".")]
                    for f in files:
                        ext = os.path.splitext(f)[1].lower()
                        if ext in IGNORED_EXTS or (ext not in CODE_EXTS and not f.startswith("Dockerfile")):
                            continue

                        full_path = os.path.join(root, f)
                        rel_path = os.path.relpath(full_path, self.project_root)
                        found_paths.add(rel_path)
                        scanned_files += 1

                        try:
                            stat_res = os.stat(full_path)
                            f_mtime = stat_res.st_mtime
                            f_size = stat_res.st_size
                        except OSError:
                            continue

                        if f_size > max_file_size_bytes:
                            continue

                        prev = known_meta.get(rel_path)
                        if prev and abs(prev[0] - f_mtime) < 0.001 and prev[1] == f_size:
                            continue  # File unchanged

                        if len(files_to_index) < max_files:
                            files_to_index.append((rel_path, full_path, f_mtime, f_size))

                # Index modified/new files in batches
                for rel_path, full_path, f_mtime, f_size in files_to_index:
                    try:
                        with open(full_path, "r", encoding="utf-8", errors="replace") as fp:
                            lines = fp.splitlines() if hasattr(fp, "splitlines") else fp.read().splitlines()
                    except Exception:
                        continue

                    # Remove previous lines for this file
                    con.execute("DELETE FROM code_lines WHERE rel_path = ?;", (rel_path,))

                    # Insert lines with content (bounded to avoid huge minified/base64 lines)
                    line_batch = [
                        (rel_path, idx + 1, line[:2000])
                        for idx, line in enumerate(lines)
                        if line.strip() and len(line) <= 10000
                    ]
                    if line_batch:
                        con.executemany(
                            "INSERT INTO code_lines (rel_path, line_no, content) VALUES (?, ?, ?);",
                            line_batch
                        )
                        indexed_lines += len(line_batch)

                    con.execute(
                        "INSERT OR REPLACE INTO file_meta (rel_path, mtime, size) VALUES (?, ?, ?);",
                        (rel_path, f_mtime, f_size)
                    )
                    updated_files += 1

                # Prune deleted files
                deleted_paths = set(known_meta.keys()) - found_paths
                if deleted_paths:
                    for del_path in deleted_paths:
                        con.execute("DELETE FROM code_lines WHERE rel_path = ?;", (del_path,))
                        con.execute("DELETE FROM file_meta WHERE rel_path = ?;", (del_path,))

                con.commit()
        finally:
            con.close()

        duration_ms = (time.time() - t0) * 1000
        return {
            "scanned_files": scanned_files,
            "updated_files": updated_files,
            "indexed_lines": indexed_lines,
            "pruned_files": len(deleted_paths) if deleted_paths else 0,
            "duration_ms": duration_ms
        }

    def search(
        self,
        query: str,
        limit: int = 25,
        path_filter: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Performs BM25-ranked full-text search.
        Returns matching lines with relevance ranking.
        """
        clean_q = query.strip()
        if not clean_q:
            return []

        # Format FTS5 query
        # If query has spaces, search as a phrase or AND expression
        escaped_q = clean_q.replace('"', '""')
        fts_query = f'"{escaped_q}"'

        def _escape_like(val: str) -> str:
            return val.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

        results = []
        try:
            con = self._get_connection()
            try:
                if path_filter:
                    sql = """
                        SELECT rel_path, line_no, content, bm25(code_lines) AS rank
                        FROM code_lines
                        WHERE code_lines MATCH ? AND rel_path LIKE ? ESCAPE '\\'
                        ORDER BY rank
                        LIMIT ?;
                    """
                    cur = con.execute(sql, (fts_query, f"%{_escape_like(path_filter)}%", limit))
                else:
                    sql = """
                        SELECT rel_path, line_no, content, bm25(code_lines) AS rank
                        FROM code_lines
                        WHERE code_lines MATCH ?
                        ORDER BY rank
                        LIMIT ?;
                    """
                    cur = con.execute(sql, (fts_query, limit))

                for row in cur.fetchall():
                    results.append({
                        "file": row[0],
                        "line": row[1],
                        "content": row[2].strip(),
                        "bm25_rank": row[3]
                    })

                # Phrase search can miss when the user types several loose words.
                # Strictly additive retry: AND of individually quoted terms —
                # only runs when the exact-phrase pass returned nothing.
                if not results and " " in clean_q.strip():
                    and_query = " AND ".join(
                        f'"{term.replace(chr(34), chr(34) * 2)}"' for term in clean_q.split()
                    )
                    params2 = ([f"%{_escape_like(path_filter)}%"] if path_filter else []) + [limit]
                    cur = con.execute(sql, (and_query, *params2))
                    for row in cur.fetchall():
                        results.append({
                            "file": row[0],
                            "line": row[1],
                            "content": row[2].strip(),
                            "bm25_rank": row[3]
                        })
            except sqlite3.OperationalError:
                # If trigram MATCH fails (e.g. query < 3 chars), fallback to escaped LIKE
                escaped_q_like = f"%{_escape_like(clean_q)}%"
                if path_filter:
                    like_sql = """
                        SELECT rel_path, line_no, content, 0.0 AS rank
                        FROM code_lines
                        WHERE content LIKE ? ESCAPE '\\' AND rel_path LIKE ? ESCAPE '\\'
                        LIMIT ?;
                    """
                    cur = con.execute(like_sql, (escaped_q_like, f"%{_escape_like(path_filter)}%", limit))
                else:
                    like_sql = """
                        SELECT rel_path, line_no, content, 0.0 AS rank
                        FROM code_lines
                        WHERE content LIKE ? ESCAPE '\\'
                        LIMIT ?;
                    """
                    cur = con.execute(like_sql, (escaped_q_like, limit))
                for row in cur.fetchall():
                    results.append({
                        "file": row[0],
                        "line": row[1],
                        "content": row[2].strip(),
                        "bm25_rank": 0.0
                    })
            finally:
                con.close()
        except Exception:
            return []

        return results

    def clear(self):
        """Clears index and removes database file."""
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass
