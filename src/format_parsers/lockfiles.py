"""
TokenSafe Lockfile Parser & Reducer (v3.3.0).

Reduces bloated lockfiles (package-lock.json, Cargo.lock, poetry.lock, pnpm-lock.yaml)
from 10,000+ lines of integrity hashes and URLs down to a compact dependency bill:
- Direct packages and exact resolved versions
- Dependency count summary
- Reversible hash anchor pointing to the full raw file in TokenSafe stash.
"""

import os
import json
import re
import sqlite3
import time
import hashlib
from typing import Tuple, Dict, Any, List, Optional
from src.stash import stash_block
from src.paths import resolve_data_dir


class LockfileReducer:
    """Extracts compact dependency information from verbose lockfiles."""

    @classmethod
    def is_lockfile(cls, filename_or_path: str) -> bool:
        name = filename_or_path.lower().split("/")[-1].split("\\")[-1]
        return name in (
            "package-lock.json",
            "bun.lock",
            "bun.lockb",
            "pnpm-lock.yaml",
            "yarn.lock",
            "cargo.lock",
            "poetry.lock",
            "gemfile.lock",
            "composer.lock",
            "go.sum",
        )

    @classmethod
    def _load_jsonc(cls, text: str) -> Optional[Dict[str, Any]]:
        """Safely parses JSON or JSONC (JSON with comments and trailing commas)."""
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            pass
        try:
            # Strip whole-line comments // ... without corrupting base64 hashes inside strings
            no_comments = re.sub(r'^\s*//.*$', '', text, flags=re.MULTILINE)
            # Strip trailing commas before } or ]
            cleaned = re.sub(r',(\s*[}\]])', r'\1', no_comments)
            return json.loads(cleaned)
        except Exception:
            return None

    @classmethod
    def reduce(cls, content: str, filename: str = "package-lock.json") -> Tuple[str, bool]:
        """Reduces lockfile content to a clean summary. Returns (compacted_text, was_compacted)."""
        lines = content.splitlines()
        if len(lines) < 30:
            return content, False

        fname = filename.lower().split("/")[-1].split("\\")[-1]

        if fname == "package-lock.json":
            return cls._reduce_npm_lock(content)
        elif fname in ("bun.lock", "bun.lockb"):
            return cls._reduce_bun_lock(content, fname)
        elif fname == "cargo.lock":
            return cls._reduce_cargo_lock(content)
        elif fname == "poetry.lock":
            return cls._reduce_poetry_lock(content)

        return cls._generic_reduce(content, fname)

    @classmethod
    def _reduce_bun_lock(cls, content: str, fname: str = "bun.lock") -> Tuple[str, bool]:
        data = cls._load_jsonc(content)
        if not data or not isinstance(data, dict):
            return cls._generic_reduce(content, fname)

        packages = data.get("packages", {})
        workspaces = data.get("workspaces", {})
        root_ws = workspaces.get("") if isinstance(workspaces, dict) else {}
        root_deps = {}
        if isinstance(root_ws, dict):
            root_deps = {**root_ws.get("dependencies", {}), **root_ws.get("devDependencies", {})}

        direct_deps = {}
        total_count = len(packages) if isinstance(packages, dict) else 0

        for dep_name, range_ver in sorted(root_deps.items()):
            resolved_ver = range_ver
            if isinstance(packages, dict) and dep_name in packages:
                val = packages[dep_name]
                if isinstance(val, list) and len(val) > 0 and isinstance(val[0], str):
                    resolved_ver = val[0].rsplit("@", 1)[1] if "@" in val[0] else val[0]
            direct_deps[dep_name] = resolved_ver

        lines = [
            f"# [TokenSafe Lockfile Summary: {fname}]",
            f"Total installed packages: {total_count}",
            "",
            "## Direct Dependencies:"
        ]
        for dname, dver in sorted(direct_deps.items()):
            lines.append(f"  - {dname}: {dver}")

        hash8, marker = stash_block(content, source="lockfile:bun")
        lines.append("")
        lines.append(marker)
        return "\n".join(lines), True

    @classmethod
    def _reduce_npm_lock(cls, content: str) -> Tuple[str, bool]:
        try:
            data = json.loads(content)
        except Exception:
            return cls._generic_reduce(content, "package-lock.json")

        name = data.get("name") or "project"
        lock_version = data.get("lockfileVersion", 1)
        packages = data.get("packages", {})
        dependencies = data.get("dependencies", {})

        direct_deps = {}
        total_count = 0

        # Lockfile v2/v3
        if packages and isinstance(packages, dict):
            root_pkg = packages.get("") or {}
            root_deps = {**root_pkg.get("dependencies", {}), **root_pkg.get("devDependencies", {})}
            total_count = len(packages) - 1
            for dep_name, expected_ver in sorted(root_deps.items()):
                target_key = f"node_modules/{dep_name}"
                resolved_ver = packages.get(target_key, {}).get("version") or expected_ver
                direct_deps[dep_name] = resolved_ver
        elif dependencies and isinstance(dependencies, dict):
            # Lockfile v1
            total_count = len(dependencies)
            for dep_name, info in sorted(dependencies.items()):
                if isinstance(info, dict):
                    direct_deps[dep_name] = info.get("version", "*")

        lines = [
            f"# [TokenSafe Lockfile Summary: package-lock.json (v{lock_version})]",
            f"Project: {name} | Total installed packages: {total_count}",
            "",
            "## Direct Dependencies:"
        ]
        for dname, dver in direct_deps.items():
            lines.append(f"  - {dname}: {dver}")

        hash8, marker = stash_block(content, source="lockfile:npm")
        lines.append("")
        lines.append(marker)
        return "\n".join(lines), True

    @classmethod
    def _reduce_cargo_lock(cls, content: str) -> Tuple[str, bool]:
        # Cargo.lock format: [[package]] name = "..." version = "..."
        pkg_pattern = re.compile(r'\[\[package\]\]\s+name\s*=\s*"([^"]+)"\s+version\s*=\s*"([^"]+)"')
        matches = pkg_pattern.findall(content)
        if not matches:
            return cls._generic_reduce(content, "Cargo.lock")

        lines = [
            f"# [TokenSafe Lockfile Summary: Cargo.lock]",
            f"Total resolved crates: {len(matches)}",
            "",
            "## Crates:"
        ]
        # Show top 40 unique packages
        seen = set()
        for name, ver in sorted(matches):
            if name not in seen:
                seen.add(name)
                lines.append(f"  - {name}: {ver}")

        hash8, marker = stash_block(content, source="lockfile:cargo")
        lines.append("")
        lines.append(marker)
        return "\n".join(lines), True

    @classmethod
    def _reduce_poetry_lock(cls, content: str) -> Tuple[str, bool]:
        pkg_pattern = re.compile(r'\[\[package\]\]\s+name\s*=\s*"([^"]+)"\s+version\s*=\s*"([^"]+)"')
        matches = pkg_pattern.findall(content)
        if not matches:
            return cls._generic_reduce(content, "poetry.lock")

        lines = [
            f"# [TokenSafe Lockfile Summary: poetry.lock]",
            f"Total resolved Python packages: {len(matches)}",
            "",
            "## Packages:"
        ]
        for name, ver in sorted(matches):
            lines.append(f"  - {name}: {ver}")

        hash8, marker = stash_block(content, source="lockfile:poetry")
        lines.append("")
        lines.append(marker)
        return "\n".join(lines), True

    @classmethod
    def _generic_reduce(cls, content: str, fname: str) -> Tuple[str, bool]:
        lines = content.splitlines()
        head = lines[:20]
        tail = lines[-10:]
        omitted = len(lines) - 30
        hash8, marker = stash_block(content, source=f"lockfile:{fname}")
        out = (
            head +
            [f"\n... [{omitted} lines of lockfile metadata omitted by TokenSafe] ...\n"] +
            tail +
            ["", marker]
        )
        return "\n".join(out), True

    @classmethod
    def find_lockfile(cls, directory: Optional[str] = None, start_dir: Optional[str] = None) -> Optional[str]:
        """Locates the nearest lockfile in directory or parent directories up to git root, or downward in monorepos."""
        target = directory or start_dir or os.getcwd()
        start = os.path.abspath(target)
        curr = start
        candidates = (
            "package-lock.json", "bun.lock", "bun.lockb",
            "pnpm-lock.yaml", "yarn.lock", "Cargo.lock", "poetry.lock"
        )
        # 1. Upward search
        for _ in range(6):
            for c in candidates:
                cand_path = os.path.join(curr, c)
                if os.path.isfile(cand_path):
                    return cand_path
            parent = os.path.dirname(curr)
            if parent == curr:
                break
            curr = parent

        # 2. Downward search (bounded: depth <= 2, skip heavy/vendor dirs, cap 60 directories)
        dirs_checked = 0
        skip_dirs = {"node_modules", ".git", "dist", "build", ".next", ".cache", "data", "target", ".venv", "venv", "__pycache__"}
        for root, dirs, files in os.walk(start):
            dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
            dirs_checked += 1
            if dirs_checked > 60:
                break
            rel_path = os.path.relpath(root, start)
            rel_depth = 0 if rel_path == "." else rel_path.count(os.sep) + 1
            if rel_depth > 2:
                dirs[:] = []
                continue
            for c in candidates:
                if c in files:
                    cand_path = os.path.join(root, c)
                    if os.path.isfile(cand_path):
                        return cand_path

        return None

    @classmethod
    def _get_db_path(cls) -> str:
        data_dir = resolve_data_dir()
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, "lockfile_cache.db")

    @classmethod
    def _maybe_evict(cls, conn: sqlite3.Connection, max_rows: int = 5000) -> None:
        """Evicts oldest entries when lockfile cache exceeds max_rows."""
        try:
            row = conn.execute("SELECT COUNT(*) FROM lockfile_cache;").fetchone()
            count = row[0] if row else 0
            if count > max_rows:
                excess = count - max_rows
                conn.execute(
                    "DELETE FROM lockfile_cache WHERE entry_key IN ("
                    "SELECT entry_key FROM lockfile_cache ORDER BY cached_at ASC LIMIT ?"
                    ");", (excess,)
                )
        except Exception:
            pass

    @classmethod
    def _init_db(cls):
        db_path = cls._get_db_path()
        conn = None
        try:
            conn = sqlite3.connect(db_path, timeout=5.0)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS lockfile_cache (
                    entry_key TEXT PRIMARY KEY,
                    lockfile_hash TEXT NOT NULL,
                    package_name TEXT NOT NULL,
                    json_result TEXT NOT NULL,
                    cached_at INTEGER NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lockfile_hash ON lockfile_cache(lockfile_hash);")
            conn.commit()
        except Exception:
            pass
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    @classmethod
    def query_dependency(cls, lockfile_path: Optional[str], package_name: str) -> Dict[str, Any]:
        """Fast targeted resolution of a package in lockfile with Local First + Team Sync caching."""
        target_path = lockfile_path or cls.find_lockfile()
        if not target_path or not os.path.isfile(target_path):
            return {
                "found": False,
                "package": package_name,
                "error": f"No lockfile found in {os.getcwd()} or parent directories."
            }

        fname = os.path.basename(target_path).lower()
        try:
            with open(target_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            _lockfile_bytes = os.path.getsize(target_path)
        except Exception as e:
            return {"found": False, "package": package_name, "error": f"Failed to read {fname}: {e}"}

        lockfile_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        entry_key = f"{lockfile_hash}::{package_name.strip().lower()}"

        # 1. Check L2 SQLite
        cls._init_db()
        db_path = cls._get_db_path()
        conn = None
        try:
            conn = sqlite3.connect(db_path, timeout=5.0)
            conn.execute("PRAGMA busy_timeout=5000;")
            row = conn.execute("SELECT json_result FROM lockfile_cache WHERE entry_key = ?", (entry_key,)).fetchone()
            if row:
                res = json.loads(row[0])
                if res.get("found"):
                    res["source"] = "L2_cache"
                    res["_lockfile_bytes"] = _lockfile_bytes
                    return res
        except Exception:
            pass
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

        # 2. Check L3 Team Sync
        try:
            from src.team_cache import TeamCache
            team_val = TeamCache.get("LOCKFILE_INDEX", entry_key, timeout=0.5)
            if team_val:
                res = json.loads(team_val)
                if res.get("found"):
                    res["source"] = "L3_team_cache"
                    res["_lockfile_bytes"] = _lockfile_bytes
                    # Store back to L2
                    back_conn = None
                    try:
                        back_conn = sqlite3.connect(db_path, timeout=5.0)
                        back_conn.execute("""
                            INSERT OR REPLACE INTO lockfile_cache (entry_key, lockfile_hash, package_name, json_result, cached_at)
                            VALUES (?, ?, ?, ?, ?)
                        """, (entry_key, lockfile_hash, package_name.strip().lower(), team_val, int(time.time())))
                        cls._maybe_evict(back_conn)
                        back_conn.commit()
                    except Exception:
                        pass
                    finally:
                        if back_conn:
                            try:
                                back_conn.close()
                            except Exception:
                                pass
                    return res
        except Exception:
            pass

        # 3. Parse lockfile
        res = cls._parse_dependency(fname, content, package_name)
        res["lockfile"] = fname
        res["lockfile_path"] = target_path
        res["_lockfile_bytes"] = _lockfile_bytes

        # 4. Save to L2 SQLite and sync to L3 Team Cache (only positive found lookups)
        if res.get("found"):
            save_conn = None
            try:
                raw_json = json.dumps(res, ensure_ascii=False)
                save_conn = sqlite3.connect(db_path, timeout=5.0)
                save_conn.execute("""
                    INSERT OR REPLACE INTO lockfile_cache (entry_key, lockfile_hash, package_name, json_result, cached_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (entry_key, lockfile_hash, package_name.strip().lower(), raw_json, int(time.time())))
                cls._maybe_evict(save_conn)
                save_conn.commit()
            except Exception:
                pass
            finally:
                if save_conn:
                    try:
                        save_conn.close()
                    except Exception:
                        pass

            # Broadcast to L3 without leaking local filesystem paths or private byte counts
            try:
                from src.team_cache import TeamCache
                team_payload = dict(res)
                team_payload.pop("lockfile_path", None)
                team_payload.pop("_lockfile_bytes", None)
                TeamCache.put("LOCKFILE_INDEX", entry_key, json.dumps(team_payload, ensure_ascii=False), tokens_saved=250)
            except Exception:
                pass

        res["source"] = "parsed_live"
        return res

    @classmethod
    def _parse_dependency(cls, fname: str, content: str, pkg_name: str) -> Dict[str, Any]:
        """Parses dependency entry from npm, cargo, poetry, pnpm, or bun."""
        norm_pkg = pkg_name.strip()
        if "bun.lock" in fname:
            try:
                data = cls._load_jsonc(content)
                if isinstance(data, dict):
                    packages = data.get("packages")
                    if isinstance(packages, dict):
                        # 1. Direct package key (e.g. "marked", "@tanstack/react-query")
                        val = packages.get(norm_pkg)
                        # 2. Nested or prefixed node_modules/<pkg>
                        if not val:
                            exact_nm = f"node_modules/{norm_pkg}"
                            val = packages.get(exact_nm)
                        if not val:
                            suffix = f"/node_modules/{norm_pkg}"
                            for k, v in packages.items():
                                if k.endswith(suffix):
                                    val = v
                                    break
                        if val and isinstance(val, list) and len(val) > 0:
                            spec = val[0] if isinstance(val[0], str) else ""
                            ver = spec.rsplit("@", 1)[1] if "@" in spec else spec
                            meta = val[2] if len(val) > 2 and isinstance(val[2], dict) else {}
                            integrity = val[3] if len(val) > 3 and isinstance(val[3], str) else None
                            return {
                                "found": True,
                                "package": norm_pkg,
                                "version": ver,
                                "resolved": integrity,
                                "dependencies": meta.get("dependencies", {}),
                                "peerDependencies": meta.get("peerDependencies", {}),
                                "dev": False,
                            }
                    # 3. Fallback to workspaces[""] direct dependencies
                    workspaces = data.get("workspaces", {})
                    root_ws = workspaces.get("") if isinstance(workspaces, dict) else {}
                    if isinstance(root_ws, dict):
                        dev_deps = root_ws.get("devDependencies", {}) or {}
                        prod_deps = root_ws.get("dependencies", {}) or {}
                        all_root = {**prod_deps, **dev_deps}
                        if norm_pkg in all_root:
                            return {
                                "found": True,
                                "package": norm_pkg,
                                "version": all_root[norm_pkg],
                                "resolved": None,
                                "dependencies": {},
                                "peerDependencies": {},
                                "dev": norm_pkg in dev_deps,
                            }
            except Exception:
                pass

        elif "package-lock" in fname:
            try:
                data = json.loads(content)
                packages = data.get("packages")
                if isinstance(packages, dict):
                    exact_key = f"node_modules/{norm_pkg}"
                    if exact_key in packages:
                        p = packages[exact_key]
                        return {
                            "found": True,
                            "package": norm_pkg,
                            "version": p.get("version", "*"),
                            "resolved": p.get("resolved"),
                            "dependencies": p.get("dependencies", {}),
                            "peerDependencies": p.get("peerDependencies", {}),
                            "dev": p.get("dev", False),
                        }
                    # Fallback scan for nested occurrences
                    suffix = f"/{exact_key}"
                    for k, p in packages.items():
                        if k.endswith(suffix):
                            return {
                                "found": True,
                                "package": norm_pkg,
                                "version": p.get("version", "*"),
                                "resolved": p.get("resolved"),
                                "dependencies": p.get("dependencies", {}),
                                "peerDependencies": p.get("peerDependencies", {}),
                                "dev": p.get("dev", False),
                            }
                # Check v1 dependencies
                deps = data.get("dependencies")
                if isinstance(deps, dict) and norm_pkg in deps:
                    d = deps[norm_pkg]
                    return {
                        "found": True,
                        "package": norm_pkg,
                        "version": d.get("version", "*") if isinstance(d, dict) else str(d),
                        "dependencies": d.get("requires", {}) if isinstance(d, dict) else {},
                        "dev": d.get("dev", False) if isinstance(d, dict) else False,
                    }
            except Exception:
                pass

        elif "cargo.lock" in fname or "poetry.lock" in fname:
            pattern = re.compile(
                rf'\[\[package\]\]\s+name\s*=\s*"{re.escape(norm_pkg)}"\s+version\s*=\s*"([^"]+)"'
                r'(?:\s+source\s*=\s*"([^"]*)")?'
                r'(?:\s+dependencies\s*=\s*(\[[^\]]*\]))?',
                re.MULTILINE
            )
            m = pattern.search(content)
            if m:
                ver = m.group(1)
                source_val = m.group(2)
                deps_raw = m.group(3) or "[]"
                try:
                    deps = json.loads(deps_raw) if deps_raw.startswith("[") else []
                except Exception:
                    deps = []
                res_dict = {
                    "found": True,
                    "package": norm_pkg,
                    "version": ver,
                    "dependencies": deps,
                }
                if source_val:
                    res_dict["resolved"] = source_val
                return res_dict

        elif "pnpm-lock" in fname:
            # Pnpm regex lookup: /pkg@ver or 'pkg': ver
            pnpm_re = re.compile(rf'["\']?/?{re.escape(norm_pkg)}[@/]([0-9a-zA-Z._-]+)["\']?:')
            m = pnpm_re.search(content)
            if m:
                return {
                    "found": True,
                    "package": norm_pkg,
                    "version": m.group(1),
                }

        return {
            "found": False,
            "package": norm_pkg,
            "message": f"Package '{norm_pkg}' not found in {fname}."
        }

    @classmethod
    def generate_lockfile_summary(cls, lockfile_path: str) -> str:
        """
        Creates a virtual compact summary file with direct dependencies and MCP query instructions.
        Returns the path to the temporary summary file.
        """
        fname = os.path.basename(lockfile_path)
        try:
            with open(lockfile_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as e:
            return f"Error reading {fname}: {e}"

        reduced_text, _ = cls.reduce(content, filename=fname)
        h = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]

        banner = (
            f"// [TokenSafe Lockfile Guard: {fname}]\n"
            f"// Notice: Full lockfile is {len(content.splitlines())} lines (~{len(content)//4} tokens).\n"
            f"// To query exact version or transitive dependencies of a specific package, call:\n"
            f"//   tokensafe:query_dependency(package_name=\"<name>\")\n"
            f"// --------------------------------------------------------------------------\n\n"
        )
        full_summary = banner + reduced_text

        summaries_dir = os.path.join(resolve_data_dir(), "summaries")
        os.makedirs(summaries_dir, exist_ok=True)
        summary_path = os.path.join(summaries_dir, f"{h}.{fname}.summary.txt")

        try:
            with open(summary_path, "w", encoding="utf-8") as f:
                f.write(full_summary)
            return summary_path
        except Exception:
            return lockfile_path
