"""The graph cache is bounded: one .db per workspace root, forever, is a leak.

Measured on a working machine on 2026-11-28: ~/.tokensafe/data/graph_cache held
4,994 files / 10 GB because nothing ever evicted a root that stopped being used.
"""
import os
import shutil
import tempfile
import time
import unittest

from src.graph_engine import GraphEngine


class GraphCachePruneTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tokensafe_graph_cache_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def make(self, name, age_hours=2.0):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as handle:
            handle.write(b"x" * 1024)
        stamp = time.time() - age_hours * 3600
        os.utime(path, (stamp, stamp))
        return path

    def test_old_entries_beyond_the_cap_are_removed_and_the_newest_survive(self):
        for idx in range(6):
            self.make(f"cache{idx}.db", age_hours=2.0 + idx)
        keep = self.make("current.db", age_hours=0.0)
        original = GraphEngine._CACHE_MAX_FILES
        GraphEngine._CACHE_MAX_FILES = 3
        try:
            removed = GraphEngine._prune_cache(self.dir, keep)
        finally:
            GraphEngine._CACHE_MAX_FILES = original
        survivors = sorted(name for name in os.listdir(self.dir) if name.endswith(".db"))
        self.assertGreater(removed, 0)
        self.assertIn("current.db", survivors)
        self.assertLessEqual(len(survivors), 3)

    def test_entries_younger_than_an_hour_are_never_evicted(self):
        fresh = [self.make(f"fresh{idx}.db", age_hours=0.1) for idx in range(4)]
        original = GraphEngine._CACHE_MAX_FILES
        GraphEngine._CACHE_MAX_FILES = 1
        try:
            GraphEngine._prune_cache(self.dir, fresh[0])
        finally:
            GraphEngine._CACHE_MAX_FILES = original
        for path in fresh:
            self.assertTrue(os.path.exists(path), path)

    def test_the_legacy_json_cache_is_pruned_too(self):
        self.make("legacy.json", age_hours=48.0)
        GraphEngine._prune_cache(self.dir, self.make("current.db", age_hours=0.0), max_age_days=1.0)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "legacy.json")))


if __name__ == "__main__":
    unittest.main()
