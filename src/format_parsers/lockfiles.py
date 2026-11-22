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
            "pnpm-lock.yaml",
            "yarn.lock",
            "cargo.lock",
            "poetry.lock",
            "gemfile.lock",
            "composer.lock",
            "go.sum",
        )

    @classmethod
    def reduce(cls, content: str, filename: str = "package-lock.json") -> Tuple[str, bool]:
        """Reduces lockfile content to a clean summary. Returns (compacted_text, was_compacted)."""
        lines = content.splitlines()
        if len(lines) < 30:
            return content, False

        fname = filename.lower().split("/")[-1].split("\\")[-1]

        if fname == "package-lock.json":
            return cls._reduce_npm_lock(content)
        elif fname == "cargo.lock":
            return cls._reduce_cargo_lock(content)
        elif fname == "poetry.lock":
            return cls._reduce_poetry_lock(content)

        return cls._generic_reduce(content, fname)

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
        """Locates the nearest lockfile in directory or parent directories up to git root."""
        target = directory or start_dir or os.getcwd()
        start = os.path.abspath(target)
        curr = start
        candidates = (
            "package-lock.json", "bun.lock", "bun.lockb",
            "pnpm-lock.yaml", "yarn.lock", "Cargo.lock", "poetry.lock"
        )
        for _ in range(6):
            for c in candidates:
                cand_path = os.path.join(curr, c)
                if os.path.isfile(cand_path):
                    return cand_path
            parent = os.path.dirname(curr)
            if parent == curr:
                break
            curr = parent
        return None

    @classmethod
    def _get_db_path(cls) -> str:
        data_dir = resolve_data_dir()
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, "lockfile_cache.db")

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
                res["source"] = "L2_cache"
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
                res["source"] = "L3_team_cache"
                # Store back to L2
                back_conn = None
                try:
                    back_conn = sqlite3.connect(db_path, timeout=5.0)
                    back_conn.execute("""
                        INSERT OR REPLACE INTO lockfile_cache (entry_key, lockfile_hash, package_name, json_result, cached_at)
                        VALUES (?, ?, ?, ?, ?)
                    """, (entry_key, lockfile_hash, package_name.strip().lower(), team_val, int(time.time())))
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

        # 4. Save to L2 SQLite and sync to L3 Team Cache
        save_conn = None
        try:
            raw_json = json.dumps(res, ensure_ascii=False)
            save_conn = sqlite3.connect(db_path, timeout=5.0)
            save_conn.execute("""
                INSERT OR REPLACE INTO lockfile_cache (entry_key, lockfile_hash, package_name, json_result, cached_at)
                VALUES (?, ?, ?, ?, ?)
            """, (entry_key, lockfile_hash, package_name.strip().lower(), raw_json, int(time.time())))
            save_conn.commit()
        except Exception:
            pass
        finally:
            if save_conn:
                try:
                    save_conn.close()
                except Exception:
                    pass

        try:
            from src.team_cache import TeamCache
            TeamCache.put("LOCKFILE_INDEX", entry_key, json.dumps(res, ensure_ascii=False), tokens_saved=250)
        except Exception:
            pass

        res["source"] = "parsed_live"
        return res

    @classmethod
    def _parse_dependency(cls, fname: str, content: str, pkg_name: str) -> Dict[str, Any]:
        """Parses dependency entry from npm, cargo, poetry, or pnpm."""
        norm_pkg = pkg_name.strip()
        if "package-lock" in fname or "bun.lock" in fname:
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
