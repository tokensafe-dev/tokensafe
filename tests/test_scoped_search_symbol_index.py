"""Identifier searches use the complete AST index, not the bounded disk walk.

The 2026-11-28 paidz session searched `getOverdriveWindow`, which is declared in
paidz-workers. The disk walk stops after 2,500 files and paidz-backend alone holds
more than that, so the answer was "0 matches" while the graph already knew the
symbol. Disk scans also now report partial coverage instead of implying emptiness.
"""
import os
import shutil
import tempfile
import unittest

from src.graph_engine import GraphEngine
from src.scoped_search import ScopedSearch


LATE_SOURCE = """export function getOverdriveWindow(now: Date = new Date()) {
  return { start: now };
}
"""


class ScopedSearchSymbolIndexTest(unittest.TestCase):
    def setUp(self):
        self._orig_data = os.environ.get("TOKENSAFE_DATA_DIR")
        self._orig_db = os.environ.get("TOKENSAFE_DB_PATH")
        self.ws = tempfile.mkdtemp(prefix="tokensafe_symbol_index_")
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        os.makedirs(os.path.join(self.ws, ".git"), exist_ok=True)
        data = os.path.join(self.ws, "_data")
        os.makedirs(data, exist_ok=True)
        os.environ["TOKENSAFE_DATA_DIR"] = data
        os.environ["TOKENSAFE_DB_PATH"] = os.path.join(data, "metrics.db")
        # Enough earlier files that a 2,500-file budget would never reach the last one.
        for idx in range(40):
            self._write(f"pkg-a/module_{idx}.ts", f"export function helper{idx}() {{ return {idx}; }}\n")
        self.late = self._write("pkg-z/queues/contest-scheduler.ts", LATE_SOURCE)

    def tearDown(self):
        for key, value in (("TOKENSAFE_DATA_DIR", self._orig_data), ("TOKENSAFE_DB_PATH", self._orig_db)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _write(self, rel, content):
        path = os.path.join(self.ws, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path

    def test_a_declaration_is_found_through_the_symbol_index(self):
        GraphEngine.get_instance(self.ws).scan(force=True)
        res = ScopedSearch.search("getOverdriveWindow", project_path=self.ws, scope="code")
        self.assertEqual(res["source"], "graph_symbol_index", res.get("markdown"))
        self.assertFalse(res["partial"])
        self.assertTrue(res["matches"])
        self.assertEqual(res["matches"][0]["file"], "pkg-z/queues/contest-scheduler.ts")
        self.assertIn("getOverdriveWindow", res["matches"][0]["text"])

    def test_prose_queries_still_use_the_disk_scan(self):
        GraphEngine.get_instance(self.ws).scan(force=True)
        res = ScopedSearch.search("return { start", project_path=self.ws, scope="code")
        self.assertEqual(res["source"], "disk_scan")
        self.assertIn("partial", res)
        self.assertFalse(res["partial"])
        self.assertTrue(res["matches"])

    def test_a_cold_repository_falls_back_without_crashing(self):
        cold = tempfile.mkdtemp(prefix="tokensafe_cold_")
        self.addCleanup(shutil.rmtree, cold, ignore_errors=True)
        with open(os.path.join(cold, "early.ts"), "w", encoding="utf-8") as handle:
            handle.write("export const x = 1;\n")
        res = ScopedSearch.search("notDeclaredAnywhere", project_path=cold, scope="code")
        self.assertEqual(res["source"], "disk_scan")
        self.assertEqual(res["total_matches"], 0)
        self.assertIn("partial", res)


if __name__ == "__main__":
    unittest.main()
