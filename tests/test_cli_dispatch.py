import unittest
import sys
import os
import io
import tempfile
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.cli import main


class TestCLIDispatch(unittest.TestCase):
    def test_cli_help(self):
        fake_out = io.StringIO()
        with patch("sys.argv", ["tokensafe", "help"]), patch("sys.stdout", fake_out):
            main()
        self.assertIn("TokenSafe CLI", fake_out.getvalue())
        self.assertIn("expand-ref", fake_out.getvalue())
        self.assertIn("doctor", fake_out.getvalue())

    def test_cli_expand_ref_found(self):
        from src.stash import stash_block
        _, marker = stash_block("SECRET_BLOCK_XYZ", source="cli_test")
        hash8 = marker.split("#")[1].split(" ")[0]
        fake_out = io.StringIO()
        with patch("sys.argv", ["tokensafe", "expand-ref", hash8]), patch("sys.stdout", fake_out):
            main()
        self.assertIn("SECRET_BLOCK_XYZ", fake_out.getvalue())

    def test_cli_expand_ref_missing(self):
        with patch("sys.argv", ["tokensafe", "expand-ref", "nonexist8"]):
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 1)

    def test_cli_optimize_image(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"not a real image")
            tmp = f.name
        try:
            fake_out = io.StringIO()
            with patch("sys.argv", ["tokensafe", "optimize-image", tmp]), patch("sys.stdout", fake_out):
                main()
            self.assertIn("Using original file", fake_out.getvalue())
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def test_cli_prune_superseded(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"type": "tool_result", "content": "hello"}\n')
            tmp = f.name
        try:
            fake_out = io.StringIO()
            with patch("sys.argv", ["tokensafe", "prune-superseded", tmp]), patch("sys.stdout", fake_out):
                main()
            self.assertIn("Pruned 0 superseded", fake_out.getvalue())
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def test_cli_mistakes_flow(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_out = io.StringIO()
            with patch("os.getcwd", return_value=tmpdir), \
                 patch("sys.argv", ["tokensafe", "mistakes", "record", "--file", "app.py", "--reason", "Dead end test"]), \
                 patch("sys.stdout", fake_out):
                main()
            self.assertIn("Recorded mistake", fake_out.getvalue())

            fake_out2 = io.StringIO()
            with patch("os.getcwd", return_value=tmpdir), \
                 patch("sys.argv", ["tokensafe", "mistakes", "list"]), \
                 patch("sys.stdout", fake_out2):
                main()
            self.assertIn("Dead end test", fake_out2.getvalue())

            fake_out3 = io.StringIO()
            with patch("os.getcwd", return_value=tmpdir), \
                 patch("sys.argv", ["tokensafe", "mistakes", "clear"]), \
                 patch("sys.stdout", fake_out3):
                main()
            self.assertIn("Cleared 1 mistake", fake_out3.getvalue())

    def test_cli_search_and_index(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = os.path.join(tmpdir, "test.py")
            with open(fpath, "w", encoding="utf-8") as f:
                f.write("def unique_search_keyword_777(): pass\n")

            fake_out = io.StringIO()
            with patch("sys.argv", ["tokensafe", "index", tmpdir]), patch("sys.stdout", fake_out):
                main()
            self.assertIn("SQLite FTS5 Full-Text Indexer", fake_out.getvalue())

            fake_out2 = io.StringIO()
            with patch("sys.argv", ["tokensafe", "search", "unique_search_keyword_777", "--path", tmpdir]), patch("sys.stdout", fake_out2):
                main()
            self.assertIn("unique_search_keyword_777", fake_out2.getvalue())


if __name__ == "__main__":
    unittest.main()
