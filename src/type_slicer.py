"""
TokenSafe 3rd-Party Type Slicer (v3.4.0).

Enables AI coding agents to extract precise method signatures, interfaces, and types
from external packages in node_modules without scanning entire dependency trees.

Architecture:
- Node module upward directory traversal (handles monorepos and Turborepo)
- Symlink resolution for pnpm virtual store (.pnpm) and bun
- Deep re-export following (export * from './...') up to 2 hops
- Local First (L1 memory, L2 SQLite type_cache.db with WAL) + L3 Team Sync (3RD_PARTY_DTS)
"""

import os
import re
import json
import sqlite3
import time
import hashlib
import threading
from collections import OrderedDict
from typing import Dict, Any, List, Optional, Tuple

from src.paths import resolve_data_dir


class TypeSlicer:
    """Targeted extractor of external library types and interfaces."""

    _mem_cache: "OrderedDict[str, str]" = OrderedDict()
    _mem_lock = threading.Lock()
    _db_init_done = False
    _db_init_lock = threading.Lock()

    @classmethod
    def clear_memory_cache(cls):
        with cls._mem_lock:
            cls._mem_cache.clear()

    @classmethod
    def _get_db_path(cls) -> str:
        data_dir = resolve_data_dir()
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, "type_cache.db")

    @classmethod
    def _init_db(cls):
        db_path = cls._get_db_path()
        with cls._db_init_lock:
            if cls._db_init_done == db_path and os.path.exists(db_path):
                return
            conn = None
            try:
                conn = sqlite3.connect(db_path, timeout=5.0)
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA busy_timeout=5000;")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS type_cache (
                        type_key TEXT PRIMARY KEY,
                        package_name TEXT NOT NULL,
                        version TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        type_code TEXT NOT NULL,
                        cached_at INTEGER NOT NULL,
                        hit_count INTEGER DEFAULT 1
                    );
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_type_pkg ON type_cache(package_name);")
                conn.commit()
                cls._db_init_done = db_path
            except Exception:
                pass
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass

    @classmethod
    def find_package_dir(cls, package_name: str, start_dir: Optional[str] = None) -> Optional[str]:
        """
        Traverses upward to locate the package directory in node_modules.
        Resolves symlinks for pnpm/bun and supports scoped packages (@scope/pkg).
        """
        curr = os.path.abspath(start_dir or os.getcwd())
        parts = package_name.strip().split("/")
        
        for _ in range(8):
            node_modules = os.path.join(curr, "node_modules")
            if os.path.isdir(node_modules):
                cand = os.path.join(node_modules, *parts)
                if os.path.isdir(cand):
                    try:
                        return os.path.realpath(cand)
                    except Exception:
                        return cand
            parent = os.path.dirname(curr)
            if parent == curr:
                break
            curr = parent
        return None

    @classmethod
    def locate_type_file(cls, pkg_dir: str, pkg_name: str) -> Optional[str]:
        """Finds primary .d.ts declaration file via package.json or @types fallback."""
        pkg_json_path = os.path.join(pkg_dir, "package.json")
        types_rel: Optional[str] = None
        if os.path.isfile(pkg_json_path):
            try:
                with open(pkg_json_path, "r", encoding="utf-8", errors="replace") as f:
                    meta = json.load(f)
                types_rel = meta.get("types") or meta.get("typings")
                if not types_rel and isinstance(meta.get("exports"), dict):
                    root_export = meta["exports"].get(".")
                    if isinstance(root_export, dict):
                        types_rel = root_export.get("types")
            except Exception:
                pass

        if types_rel:
            resolved = os.path.realpath(os.path.join(pkg_dir, types_rel))
            if os.path.isfile(resolved):
                return resolved
            if os.path.isfile(resolved + ".d.ts"):
                return resolved + ".d.ts"

        # Check standard index.d.ts
        standard_index = os.path.join(pkg_dir, "index.d.ts")
        if os.path.isfile(standard_index):
            return standard_index

        # Check @types/<pkg> fallback in node_modules
        node_modules_parent = os.path.dirname(pkg_dir)
        if os.path.basename(node_modules_parent) != "node_modules":
            # Scoped package parent
            node_modules_parent = os.path.dirname(node_modules_parent)

        if os.path.basename(node_modules_parent) == "node_modules":
            types_scope_name = pkg_name.replace("@", "").replace("/", "__")
            types_cand = os.path.join(node_modules_parent, "@types", types_scope_name, "index.d.ts")
            if os.path.isfile(types_cand):
                return types_cand

        return None

    @classmethod
    def _extract_symbol_block(cls, content: str, symbol: str) -> Optional[str]:
        """Extracts full definition block for interface, type, class, function, or enum."""
        pattern = re.compile(
            rf'(?:export\s+)?(?:declare\s+)?(?:default\s+)?(interface|type|class|enum|function|const|namespace)\s+{re.escape(symbol)}\b',
            re.MULTILINE
        )
        match = pattern.search(content)
        if not match:
            return None

        start_pos = match.start()
        # Find start of block or statement
        rest = content[start_pos:]
        lines = rest.splitlines()

        collected: List[str] = []
        open_braces = 0
        brace_started = False

        for line in lines:
            collected.append(line)
            open_braces += line.count("{") - line.count("}")
            if "{" in line:
                brace_started = True

            # For type alias (type X = ...;) or single-line declarations
            if not brace_started and line.strip().endswith(";"):
                break
            if brace_started and open_braces <= 0:
                break
            if len(collected) > 120:  # safety cap
                break

        return "\n".join(collected)

    @classmethod
    def slice_symbol_from_file(cls, dts_path: str, symbol: str, depth: int = 0) -> Optional[str]:
        """Slices symbol from declaration file, following relative re-exports up to 2 hops."""
        if not dts_path or not os.path.isfile(dts_path):
            return None

        try:
            with open(dts_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception:
            return None

        # 1. Direct match
        block = cls._extract_symbol_block(content, symbol)
        if block:
            return block

        # 2. Check relative re-exports (export * from './...') if depth < 2
        if depth < 2:
            base_dir = os.path.dirname(dts_path)
            re_export_pattern = re.compile(r'export\s+(?:\*|\{[^}]+\})\s+from\s+[\'"](\.[^\'"]+)[\'"]')
            for match in re_export_pattern.finditer(content):
                rel_target = match.group(1)
                cand_paths = [
                    os.path.join(base_dir, rel_target),
                    os.path.join(base_dir, f"{rel_target}.d.ts"),
                    os.path.join(base_dir, rel_target, "index.d.ts"),
                ]
                for p in cand_paths:
                    if os.path.isfile(p):
                        found = cls.slice_symbol_from_file(p, symbol, depth=depth + 1)
                        if found:
                            return found

        return None

    @classmethod
    def slice_package_type(
        cls,
        package_name: str,
        symbol: str,
        project_root: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Targeted resolution of external library types.
        Checks L1, L2 SQLite, L3 Team Cache, then parses declaration AST.
        """
        pkg_name = package_name.strip()
        sym = symbol.strip()
        if not pkg_name or not sym:
            return {"found": False, "error": "package_name and symbol are required"}

        pkg_dir = cls.find_package_dir(pkg_name, start_dir=project_root)
        version = "unknown"
        if pkg_dir:
            pkg_json = os.path.join(pkg_dir, "package.json")
            if os.path.isfile(pkg_json):
                try:
                    with open(pkg_json, "r", encoding="utf-8") as f:
                        version = json.load(f).get("version", "unknown")
                except Exception:
                    pass

        type_key = f"{pkg_name}@{version}::{sym}"

        # 1. Check L1 Memory
        with cls._mem_lock:
            if type_key in cls._mem_cache:
                cls._mem_cache.move_to_end(type_key)
                return {
                    "found": True,
                    "package": pkg_name,
                    "version": version,
                    "symbol": sym,
                    "type_code": cls._mem_cache[type_key],
                    "source": "L1_memory"
                }

        # 2. Check L2 SQLite
        cls._init_db()
        db_path = cls._get_db_path()
        l2_conn = None
        try:
            l2_conn = sqlite3.connect(db_path, timeout=5.0)
            l2_conn.execute("PRAGMA busy_timeout=5000;")
            row = l2_conn.execute(
                "SELECT type_code FROM type_cache WHERE type_key = ?",
                (type_key,)
            ).fetchone()
            if row:
                code = row[0]
                with cls._mem_lock:
                    cls._mem_cache[type_key] = code
                return {
                    "found": True,
                    "package": pkg_name,
                    "version": version,
                    "symbol": sym,
                    "type_code": code,
                    "source": "L2_sqlite"
                }
        except Exception:
            pass
        finally:
            if l2_conn:
                try:
                    l2_conn.close()
                except Exception:
                    pass

        # 3. Check L3 Team Sync
        try:
            from src.team_cache import TeamCache
            team_val = TeamCache.get("3RD_PARTY_DTS", type_key, timeout=0.5)
            if team_val:
                with cls._mem_lock:
                    cls._mem_cache[type_key] = team_val
                # Backfill L2
                back_conn = None
                try:
                    back_conn = sqlite3.connect(db_path, timeout=5.0)
                    back_conn.execute("""
                        INSERT OR REPLACE INTO type_cache (type_key, package_name, version, symbol, type_code, cached_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (type_key, pkg_name, version, sym, team_val, int(time.time())))
                    back_conn.commit()
                except Exception:
                    pass
                finally:
                    if back_conn:
                        try:
                            back_conn.close()
                        except Exception:
                            pass
                return {
                    "found": True,
                    "package": pkg_name,
                    "version": version,
                    "symbol": sym,
                    "type_code": team_val,
                    "source": "L3_team_cache"
                }
        except Exception:
            pass

        # 4. Parse from disk
        if not pkg_dir:
            return {
                "found": False,
                "package": pkg_name,
                "symbol": sym,
                "error": f"Package '{pkg_name}' not found in node_modules."
            }

        dts_file = cls.locate_type_file(pkg_dir, pkg_name)
        if not dts_file:
            return {
                "found": False,
                "package": pkg_name,
                "symbol": sym,
                "error": f"No .d.ts declaration file located for '{pkg_name}'."
            }

        code_block = cls.slice_symbol_from_file(dts_file, sym)
        if not code_block:
            return {
                "found": False,
                "package": pkg_name,
                "symbol": sym,
                "dts_file": os.path.basename(dts_file),
                "error": f"Symbol '{sym}' not found in type definitions of '{pkg_name}'."
            }

        # 5. Save to L1, L2 SQLite, and broadcast to L3 Team Sync
        with cls._mem_lock:
            cls._mem_cache[type_key] = code_block
            if len(cls._mem_cache) > 100:
                cls._mem_cache.popitem(last=False)

        save_conn = None
        try:
            save_conn = sqlite3.connect(db_path, timeout=5.0)
            save_conn.execute("""
                INSERT OR REPLACE INTO type_cache (type_key, package_name, version, symbol, type_code, cached_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (type_key, pkg_name, version, sym, code_block, int(time.time())))
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
            TeamCache.put("3RD_PARTY_DTS", type_key, code_block, tokens_saved=max(30, len(code_block)//4))
        except Exception:
            pass

        return {
            "found": True,
            "package": pkg_name,
            "version": version,
            "symbol": sym,
            "type_code": code_block,
            "source": "parsed_live"
        }
