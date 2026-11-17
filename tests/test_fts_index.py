"""
Unit tests for TokenSafe SQLite FTS5 BM25 Code Search Engine.
"""

import unittest
import os
import shutil
import tempfile
from src.fts_index import CodeFTSIndex
from src.scoped_search import ScopedSearch


class TestFTSIndex(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="tokensafe_fts_test_")
        os.makedirs(os.path.join(self.tmp_dir, "src"), exist_ok=True)
        self.file1 = os.path.join(self.tmp_dir, "src", "gateway.py")
        with open(self.file1, "w") as f:
            f.write("class PaymentGateway:\n    def process_charge(self, amount):\n        return execute_charge(amount)\n")

        self.file2 = os.path.join(self.tmp_dir, "src", "user.py")
        with open(self.file2, "w") as f:
            f.write("class UserManager:\n    def find_user(self, user_id):\n        return db.lookup(user_id)\n")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_indexing_and_bm25_search(self):
        index = CodeFTSIndex(self.tmp_dir)
        stats = index.index_files()

        self.assertEqual(stats["scanned_files"], 2)
        self.assertEqual(stats["updated_files"], 2)
        self.assertGreater(stats["indexed_lines"], 0)

        # Search for 'charge'
        results = index.search("charge")
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0]["file"], "src/gateway.py")
        self.assertIn("charge", results[0]["content"])

        # Search for 'UserManager'
        user_res = index.search("UserManager")
        self.assertEqual(len(user_res), 1)
        self.assertEqual(user_res[0]["file"], "src/user.py")

    def test_multi_word_query_falls_back_to_and_terms(self):
        """Exact-phrase miss for loose words must retry as an AND of quoted terms."""
        index = CodeFTSIndex(self.tmp_dir)
        index.index_files()
        # "process charge" is not a verbatim phrase (source has process_charge),
        # so the phrase pass returns nothing; the AND retry must still find it.
        results = index.search("process charge")
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0]["file"], "src/gateway.py")

    def test_max_files_cap_does_not_prune_unseen_files(self):
        """The per-run cap bounds INDEXING work, not the walk — files beyond the
        cap must stay in found_paths or the prune step deletes them every run."""
        for i in range(12):
            with open(os.path.join(self.tmp_dir, f"m{i}.py"), "w") as f:
                f.write(f"def marker_{i}():\n    return {i}\n")
        index = CodeFTSIndex(self.tmp_dir)
        stats1 = index.index_files(max_files=5)
        self.assertEqual(stats1["updated_files"], 5)
        stats2 = index.index_files(max_files=5)
        self.assertEqual(stats2["updated_files"], 5)
        self.assertEqual(stats2["pruned_files"], 0)  # the old bug pruned 7 here
        stats3 = index.index_files(max_files=5)
        # m10 + m11 + the two src/ files (walk now completes past the cap)
        self.assertEqual(stats3["updated_files"], 4)
        self.assertEqual(stats3["pruned_files"], 0)
        self.assertGreater(len(index.search("marker_11")), 0)
        self.assertEqual(len(index.search("marker_")), 12)

    def test_incremental_indexing(self):
        index = CodeFTSIndex(self.tmp_dir)
        stats1 = index.index_files()
        self.assertEqual(stats1["updated_files"], 2)

        # Second pass without modifying files
        stats2 = index.index_files()
        self.assertEqual(stats2["updated_files"], 0)

        # Modify one file
        with open(self.file2, "a") as f:
            f.write("    def delete_user(self, user_id):\n        pass\n")

        stats3 = index.index_files()
        self.assertEqual(stats3["updated_files"], 1)

        del_res = index.search("delete_user")
        self.assertEqual(len(del_res), 1)
        self.assertEqual(del_res[0]["file"], "src/user.py")

    def test_scoped_search_fts_integration(self):
        # Index project first
        index = CodeFTSIndex(self.tmp_dir)
        index.index_files()

        # Call ScopedSearch
        res = ScopedSearch.search("PaymentGateway", project_path=self.tmp_dir)
        self.assertTrue(res["success"])
        self.assertGreater(len(res["matches"]), 0)
        self.assertEqual(res["matches"][0]["file"], "src/gateway.py")


if __name__ == "__main__":
    unittest.main()
