"""Offline OUMV mutations honour `symbol` — they used to replace the whole file.

`oumv_mutate(path, symbol, replacement)` sends applyEdit to a live adapter, but with
no runtime it fell back to "write `replacement` as the file". For a symbol-scoped
call that silently truncated every other declaration in the target file.
"""
import hashlib
import os
import shutil
import tempfile
import unittest

from src.oumv_gateway import OUMVGateway

SOURCE = """package store

func NewStore() *Store {
\treturn &Store{}
}

func (s *Store) OverdriveContest(season string) bool {
\treturn season != ""
}

func (s *Store) Other(season string) bool {
\treturn season == ""
}
"""


class OfflineMutateSymbolTest(unittest.TestCase):
    def setUp(self):
        self._orig_data = os.environ.get("TOKENSAFE_DATA_DIR")
        self._orig_db = os.environ.get("TOKENSAFE_DB_PATH")
        self.ws = tempfile.mkdtemp(prefix="tokensafe_mutate_symbol_")
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        data = os.path.join(self.ws, "_data")
        os.makedirs(data, exist_ok=True)
        os.environ["TOKENSAFE_DATA_DIR"] = data
        os.environ["TOKENSAFE_DB_PATH"] = os.path.join(data, "metrics.db")
        rel = "internal/store/contest.go"
        self.path = os.path.join(self.ws, rel)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(SOURCE)
        self.rel = rel

    def tearDown(self):
        for key, value in (("TOKENSAFE_DATA_DIR", self._orig_data), ("TOKENSAFE_DB_PATH", self._orig_db)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def read(self):
        with open(self.path, encoding="utf-8") as handle:
            return handle.read()

    def version(self, text):
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()

    def test_a_symbol_edit_keeps_the_other_declarations(self):
        replacement = "\n".join([
            "func (s *Store) OverdriveContest(season string) bool {",
            "\treturn season == \"open\"",
            "}",
        ])
        res = OUMVGateway.mutate(
            path=self.rel,
            symbol="OverdriveContest",
            replacement=replacement,
            expected_version=self.version(SOURCE),
            workspace_dir=self.ws,
        )
        text = self.read()
        self.assertTrue(res["success"], res)
        self.assertEqual(res["mode"], "OUT_OF_THE_BOX_DIRECT_EDIT")
        self.assertIn('season == "open"', text)
        self.assertIn("func NewStore() *Store {", text)
        self.assertIn("func (s *Store) Other(season string) bool {", text)
        self.assertEqual(text.count("package store"), 1)
        self.assertEqual(res["symbolRange"]["startLine"], 7)
        self.assertEqual(res["symbolRange"]["endLine"], 9)
        self.assertEqual(res["newVersion"], self.version(text))

    def test_a_stale_occ_token_blocks_the_edit(self):
        with self.assertRaises(RuntimeError) as ctx:
            OUMVGateway.mutate(
                path=self.rel,
                symbol="OverdriveContest",
                replacement="func (s *Store) OverdriveContest(season string) bool { return true }",
                expected_version="sha256:" + "0" * 64,
                workspace_dir=self.ws,
            )
        self.assertIn("CONFLICT", str(ctx.exception))
        self.assertEqual(self.read(), SOURCE)

    def test_an_unknown_symbol_is_refused_not_written_whole(self):
        with self.assertRaises(RuntimeError) as ctx:
            OUMVGateway.mutate(
                path=self.rel,
                symbol="NoSuchSymbol",
                replacement="func NoSuchSymbol() {}",
                workspace_dir=self.ws,
            )
        self.assertIn("SYMBOL_NOT_FOUND", str(ctx.exception))
        self.assertIn("OverdriveContest", str(ctx.exception))
        self.assertEqual(self.read(), SOURCE)

    def test_a_whole_file_edit_still_works_without_a_symbol(self):
        replacement = "package store\n\n// rewritten wholesale\n"
        res = OUMVGateway.mutate(path=self.rel, replacement=replacement, workspace_dir=self.ws)
        self.assertTrue(res["success"], res)
        self.assertEqual(self.read(), replacement)


if __name__ == "__main__":
    unittest.main()
