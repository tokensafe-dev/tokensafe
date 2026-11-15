import os
import shutil
import tempfile
import unittest

from src.stash import (
    stash_block,
    retrieve_block,
    compute_hash,
    cleanup_old_stashes,
    get_stash_dir,
)


class TestStash(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="tokensafe_stash_test_")
        self.old_data_dir = os.environ.get("TOKENSAFE_DATA_DIR")
        os.environ["TOKENSAFE_DATA_DIR"] = self.test_dir

    def tearDown(self):
        if self.old_data_dir is not None:
            os.environ["TOKENSAFE_DATA_DIR"] = self.old_data_dir
        else:
            os.environ.pop("TOKENSAFE_DATA_DIR", None)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_compute_hash(self):
        text = "console.log('hello world');"
        hash8, full_sha = compute_hash(text)
        self.assertEqual(len(hash8), 8)
        self.assertEqual(len(full_sha), 64)
        self.assertTrue(full_sha.startswith(hash8))

    def test_stash_and_retrieve_direct(self):
        raw_content = "def test_func():\n    assert 1 == 1\n    return True\n"
        hash8, marker = stash_block(raw_content, source="unit_test")
        self.assertIn(hash8, marker)
        self.assertIn("TokenSafe folded: #", marker)
        self.assertIn("call tokensafe:expand_ref", marker)

        # Retrieve via bare hash
        restored = retrieve_block(hash8)
        self.assertEqual(restored, raw_content)

        # Retrieve via #hash
        restored_hash = retrieve_block(f"#{hash8}")
        self.assertEqual(restored_hash, raw_content)

        # Retrieve via full marker string
        restored_marker = retrieve_block(marker)
        self.assertEqual(restored_marker, raw_content)

    def test_retrieve_nonexistent(self):
        self.assertIsNone(retrieve_block("deadbeef"))
        self.assertIsNone(retrieve_block(""))
        self.assertIsNone(retrieve_block("not_a_hash"))

    def test_cleanup_stashes(self):
        content = "some data to prune"
        hash8, _ = stash_block(content, source="test")
        self.assertIsNotNone(retrieve_block(hash8))

        # Cleanup with max_age_days = 0 should prune files
        pruned = cleanup_old_stashes(max_age_days=-1)
        self.assertEqual(pruned, 1)
        self.assertIsNone(retrieve_block(hash8))


if __name__ == "__main__":
    unittest.main()
