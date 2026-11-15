"""
Unit tests for SQLite-based graph caching in TokenSafe CodeGraph.
Verifies SQLite storage, roundtrip persistence, incremental re-scans,
and legacy JSON-to-SQLite cache migration.
"""

import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest

from src.graph_engine import CodeGraph, _CACHE_SCHEMA_VERSION


class TestGraphSqliteCache(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="tokensafe_cache_test_")
        self.file_a = os.path.join(self.test_dir, "core.py")
        with open(self.file_a, "w") as f:
            f.write("def core_fn():\n    return 42\n")

        self.file_b = os.path.join(self.test_dir, "app.py")
        with open(self.file_b, "w") as f:
            f.write("import core\ndef main():\n    return core.core_fn()\n")

    def tearDown(self):
        # Clean up any cache files
        graph = CodeGraph(self.test_dir, use_disk_cache=True)
        db_path = graph._cache_path()
        json_path = graph._legacy_json_cache_path()
        if os.path.exists(db_path):
            try:
                os.remove(db_path)
            except OSError:
                pass
        if os.path.exists(json_path):
            try:
                os.remove(json_path)
            except OSError:
                pass
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_sqlite_cache_creation_and_tables(self):
        graph = CodeGraph(self.test_dir, use_disk_cache=True)
        reindexed = graph.scan()
        self.assertEqual(reindexed, 2)

        cache_path = graph._cache_path()
        self.assertTrue(cache_path.endswith(".db"))
        self.assertTrue(os.path.exists(cache_path))

        # Inspect SQLite schema directly
        con = sqlite3.connect(cache_path)
        try:
            cur = con.cursor()
            tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            expected_tables = {
                "graph_meta",
                "file_stats",
                "symbols",
                "file_symbols",
                "file_imports",
                "edges",
                "api_endpoints",
                "api_calls",
            }
            self.assertTrue(expected_tables.issubset(tables), f"Missing tables: {expected_tables - tables}")

            meta = dict(cur.execute("SELECT key, val FROM graph_meta").fetchall())
            self.assertEqual(int(meta["version"]), _CACHE_SCHEMA_VERSION)
            self.assertEqual(meta["root_dir"], os.path.abspath(self.test_dir))

            symbols = cur.execute("SELECT sym_key, name, kind FROM symbols").fetchall()
            sym_keys = {r[0] for r in symbols}
            self.assertIn("core.py::core_fn", sym_keys)
            self.assertIn("app.py::main", sym_keys)
        finally:
            con.close()

    def test_sqlite_cache_roundtrip_zero_rescan(self):
        graph1 = CodeGraph(self.test_dir, use_disk_cache=True)
        self.assertEqual(graph1.scan(), 2)

        # Fresh graph pointing to same directory should load directly from SQLite
        graph2 = CodeGraph(self.test_dir, use_disk_cache=True)
        self.assertEqual(graph2.scan(), 0)
        self.assertIn("core.py::core_fn", graph2.symbols)
        self.assertIn("app.py::main", graph2.symbols)
        self.assertIn("file:core.py", graph2.edges.get("file:app.py", set()))

    def test_sqlite_incremental_update(self):
        graph = CodeGraph(self.test_dir, use_disk_cache=True)
        graph.scan()

        # Add a new file
        file_c = os.path.join(self.test_dir, "extra.py")
        with open(file_c, "w") as f:
            f.write("def extra_fn():\n    return 'extra'\n")

        # Second scan should reindex exactly 1 file
        reindexed = graph.scan()
        self.assertEqual(reindexed, 1)
        self.assertIn("extra.py::extra_fn", graph.symbols)

        # Verify it persisted into SQLite
        con = sqlite3.connect(graph._cache_path())
        try:
            cur = con.cursor()
            rows = cur.execute("SELECT sym_key FROM symbols WHERE rel_path='extra.py'").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0], "extra.py::extra_fn")
        finally:
            con.close()

    def test_legacy_json_migration_to_sqlite(self):
        graph = CodeGraph(self.test_dir, use_disk_cache=False)
        graph.scan()

        json_path = graph._legacy_json_cache_path()
        db_path = graph._cache_path()
        os.makedirs(os.path.dirname(json_path), exist_ok=True)

        # Write legacy JSON format
        legacy_data = {
            "version": 2,
            "root_dir": os.path.abspath(self.test_dir),
            "backend": graph.backend,
            "last_scanned": time.time(),
            "file_stats": {k: [v[0], v[1]] for k, v in graph.file_stats.items()},
            "file_symbols": graph.file_symbols,
            "file_imports": {k: sorted(v) for k, v in graph.file_imports.items()},
            "symbols": {k: v.to_dict() for k, v in graph.symbols.items()},
            "edges": {k: sorted(v) for k, v in graph.edges.items()},
            "rev_edges": {k: sorted(v) for k, v in graph.rev_edges.items()},
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(legacy_data, f)

        if os.path.exists(db_path):
            os.remove(db_path)

        # New CodeGraph should detect legacy JSON, migrate to SQLite .db, and delete .json
        migrated_graph = CodeGraph(self.test_dir, use_disk_cache=True)
        self.assertIn("core.py::core_fn", migrated_graph.symbols)
        self.assertTrue(os.path.exists(db_path))
        self.assertFalse(os.path.exists(json_path))

        # Re-scan should be 0 because cache was successfully loaded and migrated
        self.assertEqual(migrated_graph.scan(), 0)


if __name__ == "__main__":
    unittest.main()
