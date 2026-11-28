"""A file path is an L1 target, not a second spelling of the workspace root.

The 2026-11-28 paidz session asked for one file's surface while the Go runtime was
offline and got `target: null` plus a 33 KB dump of every endpoint in the repo.
"""
import hashlib
import json
import os
import shutil
import tempfile
import unittest

from src.graph_engine import GraphEngine


GO_SOURCE = """package store

type Store struct {
\tpath string
}

func NewStore(path string) *Store {
\treturn &Store{path: path}
}

func (s *Store) OverdriveContest(season string) bool {
\treturn season != ""
}
"""

BUNDLE = "export const V0=()=>fetch('/api/leaked');\n"

API_SOURCE = """export async function fetchProfile() {
  return fetch('/api/player');
}
"""


class OumvFileTargetTest(unittest.TestCase):
    def setUp(self):
        self._orig_data = os.environ.get("TOKENSAFE_DATA_DIR")
        self._orig_db = os.environ.get("TOKENSAFE_DB_PATH")
        self.ws = tempfile.mkdtemp(prefix="tokensafe_file_target_")
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        os.makedirs(os.path.join(self.ws, ".git"), exist_ok=True)
        data = os.path.join(self.ws, "_data")
        os.makedirs(data, exist_ok=True)
        os.environ["TOKENSAFE_DATA_DIR"] = data
        os.environ["TOKENSAFE_DB_PATH"] = os.path.join(data, "metrics.db")
        self.go_file = self._write("apps/server/internal/store/overdrive_contest.go", GO_SOURCE)
        self.api_file = self._write("apps/web/src/lib/api.ts", API_SOURCE)
        self.bundle = self._write(
            "apps/web/android/app/src/main/assets/public/assets/index-otG6xuvO.js", BUNDLE
        )
        self.graph = GraphEngine.get_instance(self.ws)
        self.graph.scan(force=True)

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

    def test_l1_on_a_file_answers_for_that_file_only(self):
        rel = "apps/server/internal/store/overdrive_contest.go"
        res = self.graph.get_static_oumv_slice(level="L1", target=rel)
        self.assertEqual(res["targetKind"], "file")
        self.assertTrue(res["targetResolved"])
        self.assertEqual(res["target"], rel)
        names = {sym["name"] for sym in res["matchingSymbols"]}
        self.assertIn("OverdriveContest", names)
        self.assertTrue(all(sym["file"] == rel for sym in res["matchingSymbols"]))
        self.assertEqual(res["endpoints"], [])
        self.assertEqual(res["clientCalls"], [])

    def test_file_facts_carry_the_occ_token_and_mutation_howto(self):
        rel = "apps/server/internal/store/overdrive_contest.go"
        res = self.graph.get_static_oumv_slice(level="L1", target=rel)
        expected = "sha256:" + hashlib.sha256(GO_SOURCE.encode("utf-8")).hexdigest()
        self.assertEqual(res["file"]["version"], expected)
        self.assertEqual(res["file"]["symbols"], len(res["matchingSymbols"]))
        self.assertEqual(res["mutation"]["expectedVersion"], expected)
        self.assertIn("oumv(action='mutate'", res["mutation"]["howTo"])

    def test_an_absolute_path_resolves_like_the_relative_one(self):
        abs_target = self.go_file
        res = self.graph.get_static_oumv_slice(level="L1", target=abs_target)
        self.assertTrue(res["targetResolved"])
        self.assertEqual(res["target"], "apps/server/internal/store/overdrive_contest.go")

    def test_an_unknown_path_never_dumps_the_repository(self):
        res = self.graph.get_static_oumv_slice(level="L1", target="apps/server/internal/store/missing.go")
        self.assertFalse(res["targetResolved"])
        self.assertEqual(res["endpoints"], [])
        self.assertEqual(res["clientCalls"], [])
        self.assertEqual(res["relationships"], [])
        self.assertEqual(res["matchingSymbols"], [])
        self.assertIn("No static index entry", res["hint"])

    def test_l0_with_a_file_target_reports_the_file_not_only_totals(self):
        rel = "apps/server/internal/store/overdrive_contest.go"
        res = self.graph.get_static_oumv_slice(level="L0", target=rel)
        self.assertEqual(res["target"], rel)
        self.assertTrue(res["targetResolved"])
        self.assertIn("version", res["file"])

    def test_a_compiled_webview_bundle_is_not_indexed(self):
        rel = "apps/web/android/app/src/main/assets/public/assets/index-otG6xuvO.js"
        self.assertNotIn(rel, self.graph.file_symbols)
        calls = [c for c in self.graph.file_api_calls.get(rel, [])]
        self.assertEqual(calls, [])
        # ...while the source that produced it still is.
        self.assertIn("apps/web/src/lib/api.ts", self.graph.file_symbols)

    def test_dispatch_accepts_path_as_the_observe_target(self):
        from src.mcp_server import _dispatch_tool_call
        res = _dispatch_tool_call(
            "oumv",
            {"action": "observe", "level": "L1", "path": self.go_file},
        )
        text = res["content"][0]["text"]
        self.assertNotIn("isError", res, text)
        payload = json.loads(text)
        self.assertEqual(payload["targetKind"], "file")
        self.assertEqual(payload["target"], "apps/server/internal/store/overdrive_contest.go")
        self.assertTrue(payload["targetResolved"])
        self.assertLess(len(text), 4000, "a one-file answer must not be a repo dump")


if __name__ == "__main__":
    unittest.main()
