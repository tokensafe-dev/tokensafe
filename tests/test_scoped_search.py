"""
Unit tests for TokenSafe Scoped Search Engine and MCP integration.
"""
import unittest
import os
import json
from src.scoped_search import ScopedSearch
from src.mcp_server import handle_tool_call


class TestScopedSearch(unittest.TestCase):

    def setUp(self):
        import shutil
        import tempfile
        from src.paths import save_active_workspace, get_active_workspace
        self._orig_ws = get_active_workspace()
        self._orig_data_dir = os.environ.get("TOKENSAFE_DATA_DIR")
        self._orig_db_path = os.environ.get("TOKENSAFE_DB_PATH")
        self._tmp_data = tempfile.mkdtemp(prefix="tokensafe_ss_metrics_")
        self.addCleanup(shutil.rmtree, self._tmp_data, ignore_errors=True)
        os.environ["TOKENSAFE_DATA_DIR"] = self._tmp_data
        os.environ["TOKENSAFE_DB_PATH"] = os.path.join(self._tmp_data, "metrics.db")
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        save_active_workspace(project_root)

    def tearDown(self):
        from src.paths import save_active_workspace
        if self._orig_data_dir is None:
            os.environ.pop("TOKENSAFE_DATA_DIR", None)
        else:
            os.environ["TOKENSAFE_DATA_DIR"] = self._orig_data_dir
        if self._orig_db_path is None:
            os.environ.pop("TOKENSAFE_DB_PATH", None)
        else:
            os.environ["TOKENSAFE_DB_PATH"] = self._orig_db_path
        if self._orig_ws:
            save_active_workspace(self._orig_ws)

    def test_scoped_search_basic(self):
        res = ScopedSearch.search("USD_PER_TOKEN", scope="code")
        self.assertTrue(res["success"])
        self.assertGreater(res["total_matches"], 0)
        self.assertTrue(any("src/metrics.py" in m["file"] for m in res["matches"]))
        self.assertIn("TokenSafe Scoped Search", res["markdown"])

    def test_scoped_search_empty_query(self):
        res = ScopedSearch.search("")
        self.assertFalse(res["success"])
        self.assertIn("error", res)

    def test_scoped_search_config_scope(self):
        res = ScopedSearch.search("tokensafe", scope="config")
        self.assertTrue(res["success"])
        # Should match package.json or pyproject.toml
        files = [m["file"] for m in res["matches"]]
        self.assertTrue(any("package.json" in f or "pyproject.toml" in f for f in files))

    def test_scoped_search_max_results_capping(self):
        res = ScopedSearch.search("import", scope="code", max_results=5)
        self.assertTrue(res["success"])
        self.assertLessEqual(len(res["matches"]), 5)

    def test_scoped_search_case_sensitive(self):
        needle = "usd" + "_per_token"
        res_lower = ScopedSearch.search(needle, scope="code", case_sensitive=True)
        self.assertEqual(res_lower["total_matches"], 0)

        res_upper = ScopedSearch.search("USD_PER_TOKEN", scope="code", case_sensitive=True)
        self.assertGreater(res_upper["total_matches"], 0)

    def test_mcp_scoped_search_integration(self):
        res = handle_tool_call("scoped_search", {"query": "USD_PER_TOKEN", "scope": "code"})
        self.assertNotIn("isError", res)
        self.assertIn("content", res)
        text = res["content"][0]["text"]
        self.assertIn("TokenSafe Scoped Search", text)
        self.assertIn("src/metrics.py", text)

    def test_scoped_search_path_alias(self):
        src_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
        res = handle_tool_call("scoped_search", {"query": "ScopedSearch", "path": src_dir, "scope": "code"})
        self.assertNotIn("isError", res)
        text = res["content"][0]["text"]
        self.assertIn("scoped_search.py", text)

    def test_scoped_search_rejects_system_root(self):
        res = ScopedSearch.search("anything", project_path="/")
        self.assertFalse(res["success"])
        self.assertIn("system root", res["markdown"])
        home_res = ScopedSearch.search("anything", project_path=os.path.expanduser("~"))
        self.assertFalse(home_res["success"])

    def test_scoped_search_extension_scope(self):
        res = ScopedSearch.search("class ScopedSearch", scope="py")
        self.assertTrue(res["success"])
        self.assertTrue(any("src/scoped_search.py" in m["file"] for m in res["matches"]))

    def test_mcp_scoped_search_subfolder_in_scope(self):
        # Testing agent passing subfolder in 'scope' (e.g. scope="src")
        res = handle_tool_call("scoped_search", {"query": "ScopedSearch", "scope": "src"})
        self.assertNotIn("isError", res)
        text = res["content"][0]["text"]
        self.assertIn("ScopedSearch", text)

    def test_mcp_scoped_search_relative_path(self):
        # Testing agent passing relative path in 'path'
        res = handle_tool_call("scoped_search", {"query": "ScopedSearch", "path": "src"})
        self.assertNotIn("isError", res)
        text = res["content"][0]["text"]
        self.assertIn("ScopedSearch", text)

    def test_mcp_scoped_search_cwd_outside_session_workspace(self):
        # Agents editing an additional working directory pass cwd + a repo-relative
        # path; that used to be joined to the session workspace and fail.
        import shutil
        import tempfile
        repo = tempfile.mkdtemp(prefix="tokensafe_ss_cwd_")
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        os.makedirs(os.path.join(repo, "pkg"))
        with open(os.path.join(repo, "pkg", "mod.py"), "w") as f:
            f.write("def marker_cwd_probe():\n    return 1\n")
        res = handle_tool_call("scoped_search", {"query": "marker_cwd_probe", "cwd": repo, "path": "pkg"})
        self.assertNotIn("isError", res, res)
        self.assertIn("mod.py", res["content"][0]["text"])

    def test_mcp_scoped_search_missing_relative_path_names_the_resolution(self):
        import shutil
        import tempfile
        repo = tempfile.mkdtemp(prefix="tokensafe_ss_missing_")
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        res = handle_tool_call("scoped_search", {"query": "anything", "cwd": repo, "path": "no_such_dir_xyz"})
        self.assertTrue(res.get("isError"))
        text = res["content"][0]["text"]
        self.assertIn("does not exist", text)
        self.assertIn("no_such_dir_xyz", text)

    def test_regex_modes_tmp_project(self):
        import shutil
        import tempfile
        root = tempfile.mkdtemp(prefix="tokensafe_ss_regex_")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        with open(os.path.join(root, "app.py"), "w", encoding="utf-8") as f:
            f.write("alpha_one = 1\nbeta_two = 2\nid_42 = 42\n")
        # Auto mode (use_regex omitted): alternation '|' enables regex
        res = ScopedSearch.search("alpha_one|beta_two", project_path=root)
        self.assertEqual(res["total_matches"], 2)
        self.assertIn("auto-regex", res["markdown"])
        # Auto mode: backslash character class enables regex
        res_cls = ScopedSearch.search(r"id_\d+", project_path=root)
        self.assertEqual(res_cls["total_matches"], 1)
        self.assertIn("auto-regex", res_cls["markdown"])
        # Explicit false forces literal: pipe string does not exist in code
        res_lit = ScopedSearch.search("alpha_one|beta_two", project_path=root, use_regex=False)
        self.assertEqual(res_lit["total_matches"], 0)
        self.assertNotIn("auto-regex", res_lit["markdown"])
        # Empty auto-regex result hints at literal escape hatch
        res_empty = ScopedSearch.search("gamma_nope|delta_nope", project_path=root)
        self.assertEqual(res_empty["total_matches"], 0)
        self.assertIn("force a literal search", res_empty["markdown"])
        # Explicit true still works
        res_re = ScopedSearch.search(r"alpha_\w+", project_path=root, use_regex=True)
        self.assertEqual(res_re["total_matches"], 1)
        # Malformed auto-detected regex degrades to literal instead of erroring
        res_bad = ScopedSearch.search("alpha_(|", project_path=root)
        self.assertTrue(res_bad["success"])

    def test_mcp_scoped_search_auto_regex_passthrough(self):
        # MCP layer must pass omitted use_regex as None (auto), not coerced False
        res = handle_tool_call("scoped_search", {"query": "USD_PER_TOKEN|reduction_pct", "scope": "code"})
        self.assertNotIn("isError", res)
        self.assertIn("auto-regex", res["content"][0]["text"])

    def test_per_file_cap_keeps_results_diverse_and_compact(self):
        import shutil
        import tempfile
        root = tempfile.mkdtemp(prefix="tokensafe_ss_cap_")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        with open(os.path.join(root, "hot.py"), "w", encoding="utf-8") as f:
            f.write("".join(f"needle_{i} = 'needle'\n" for i in range(30)))
        with open(os.path.join(root, "cold.py"), "w", encoding="utf-8") as f:
            f.write("value = 'needle'\n")
        res = ScopedSearch.search("needle", project_path=root, scope="code", max_results=20, per_file=3)
        self.assertTrue(res["success"])
        self.assertEqual(res["per_file_cap"], 3)
        hot = [m for m in res["matches"] if m["file"] == "hot.py"]
        self.assertEqual(len(hot), 3)
        self.assertGreater(res["overflow_matches"], 0)
        self.assertIn("+", res["markdown"])
        self.assertIn("more in this file", res["markdown"])
        self.assertTrue(any(m["file"] == "cold.py" for m in res["matches"]))

    def test_render_groups_hits_per_file(self):
        import shutil
        import tempfile
        root = tempfile.mkdtemp(prefix="tokensafe_ss_group_")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        for name in ("a.py", "b.py"):
            with open(os.path.join(root, name), "w", encoding="utf-8") as f:
                f.write("token = 1\ntoken = 2\n")
        res = ScopedSearch.search("token", project_path=root, scope="code")
        md = res["markdown"]
        self.assertIn("**a.py**", md)
        self.assertIn("**b.py**", md)
        self.assertIn("L1:", md)
        # the path is printed once per file, not on every hit line
        self.assertEqual(md.count("a.py"), 1)

    def test_scoped_search_fts_fast_path_renders_markdown(self):
        import shutil
        import tempfile
        from src.fts_index import CodeFTSIndex
        root = tempfile.mkdtemp(prefix="tokensafe_fts_md_")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        with open(os.path.join(root, "mod.py"), "w", encoding="utf-8") as f:
            f.write("def uniqueFtsTargetSymbol():\n    return 'ok'\n")
        CodeFTSIndex(root).index_files()
        res = ScopedSearch.search("uniqueFtsTargetSymbol", project_path=root, scope="code")
        self.assertTrue(res["success"])
        self.assertGreater(res["total_matches"], 0)
        self.assertIn("uniqueFtsTargetSymbol", res["markdown"])
        self.assertIn("text", res["matches"][0])
        self.assertIn("scanned_files", res)
        self.assertFalse(ScopedSearch.is_match_file("a.py", "a.py", "unknown/scope"))


if __name__ == "__main__":
    unittest.main()
