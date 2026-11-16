"""
Tests for TokenSafe gitignore registration and project clean working tree.
"""

import os
import tempfile
import unittest
from src.paths import ensure_tokensafe_gitignore
from src.fts_index import _resolve_fts_db_path


class TestGitignoreGuard(unittest.TestCase):

    def test_ensure_tokensafe_gitignore_creates_inner_and_updates_root(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            proj_tokensafe = os.path.join(tmp_dir, ".tokensafe")
            os.makedirs(proj_tokensafe, exist_ok=True)
            root_gitignore = os.path.join(tmp_dir, ".gitignore")
            with open(root_gitignore, "w", encoding="utf-8") as f:
                f.write("node_modules/\ndist/\n")

            updated = ensure_tokensafe_gitignore(tmp_dir)
            self.assertTrue(updated)

            # Check inner .gitignore
            inner_ignore = os.path.join(proj_tokensafe, ".gitignore")
            self.assertTrue(os.path.isfile(inner_ignore))
            with open(inner_ignore, "r", encoding="utf-8") as f:
                inner_content = f.read()
            self.assertIn("*", inner_content)
            self.assertIn("!.gitignore", inner_content)

            # Check root .gitignore
            with open(root_gitignore, "r", encoding="utf-8") as f:
                root_content = f.read()
            self.assertIn(".tokensafe/", root_content)

    def test_resolve_fts_db_path_stays_in_global_cache(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Even if .tokensafe exists in project, fts.db must NOT be created inside it
            os.makedirs(os.path.join(tmp_dir, ".tokensafe"), exist_ok=True)
            db_path = _resolve_fts_db_path(tmp_dir)
            self.assertFalse(db_path.startswith(tmp_dir))
            self.assertIn("cache", db_path)
            self.assertIn("fts", db_path)


if __name__ == "__main__":
    unittest.main()
