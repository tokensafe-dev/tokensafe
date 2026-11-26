"""
Automated Test Suite for TokenSafe Accelerator & Latency Optimization Features.
Covers:
- Batch Editor Engine (apply_edits, best_effort, inline verify, secret leak guard).
- Batch Search Engine (concurrent text, regex, glob, git read-only queries).
- Workspace Snapshot (SessionStart injection, git status, tree, scripts).
- Zero-Spill Guard (<=8800 bytes cap with stash & expand_ref anchor).
- Multi-Agent Transcript Analyzer (Antigravity/Claude transcripts, A/B comparison).
"""

import os
import sys
import json
import shutil
import stat
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.batch_editor import execute_batch_edits
from src.batch_search import execute_batch_search, _run_python_text_search
from src.snapshot import build_workspace_snapshot
from src.mcp_server import _cap_mcp_output_size
from src.transcript_analyzer import analyze_transcript, compare_transcripts, render_transcripts_report


class TestAcceleratorFeatures(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="tokensafe_accel_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_batch_editor_atomic_success(self):
        """Tests multi-file, multi-hunk atomic edits applying cleanly."""
        file_a = os.path.join(self.tmp_dir, "a.py")
        file_b = os.path.join(self.tmp_dir, "b.ts")

        with open(file_a, "w", encoding="utf-8") as f:
            f.write("def foo():\n    return 1\n")
        with open(file_b, "w", encoding="utf-8") as f:
            f.write("export const x = 10;\nexport const y = 20;\n")

        edits = [
            {
                "path": "a.py",
                "hunks": [{"target": "return 1", "replacement": "return 42"}]
            },
            {
                "path": "b.ts",
                "hunks": [{"target": "export const x = 10;", "replacement": "export const x = 100;"}]
            },
            {
                "path": "c.txt",
                "create": "Hello new file!\n"
            }
        ]

        res = execute_batch_edits(edits=edits, cwd=self.tmp_dir)
        self.assertTrue(res["success"])
        self.assertEqual(res["applied_count"], 3)
        self.assertEqual(res["failed_count"], 0)

        with open(file_a, "r", encoding="utf-8") as f:
            self.assertIn("return 42", f.read())
        with open(file_b, "r", encoding="utf-8") as f:
            self.assertIn("x = 100", f.read())
        with open(os.path.join(self.tmp_dir, "c.txt"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "Hello new file!\n")

    def test_batch_editor_atomic_failure_rollback(self):
        """Tests that when 1 hunk fails in atomic mode, 0 files are touched on disk."""
        file_a = os.path.join(self.tmp_dir, "a.py")
        file_b = os.path.join(self.tmp_dir, "b.py")

        with open(file_a, "w", encoding="utf-8") as f:
            f.write("v1 = 1\n")
        with open(file_b, "w", encoding="utf-8") as f:
            f.write("v2 = 2\n")

        edits = [
            {
                "path": "a.py",
                "hunks": [{"target": "v1 = 1", "replacement": "v1 = 10"}]
            },
            {
                "path": "b.py",
                "hunks": [{"target": "NON_EXISTENT_STRING", "replacement": "v2 = 20"}]
            }
        ]

        res = execute_batch_edits(edits=edits, best_effort=False, cwd=self.tmp_dir)
        self.assertFalse(res["success"])
        self.assertEqual(res["failed_count"], 1)

        # File A must remain unchanged because atomic mode rejected the batch
        with open(file_a, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "v1 = 1\n")

    def test_batch_editor_best_effort(self):
        """Tests that best_effort=True applies valid files and returns drift hint for invalid."""
        file_a = os.path.join(self.tmp_dir, "a.py")
        file_b = os.path.join(self.tmp_dir, "b.py")

        with open(file_a, "w", encoding="utf-8") as f:
            f.write("v1 = 1\n")
        with open(file_b, "w", encoding="utf-8") as f:
            f.write("v2 = 2\n")

        edits = [
            {
                "path": "a.py",
                "hunks": [{"target": "v1 = 1", "replacement": "v1 = 10"}]
            },
            {
                "path": "b.py",
                "hunks": [{"target": "NON_EXISTENT", "replacement": "v2 = 20"}]
            }
        ]

        res = execute_batch_edits(edits=edits, best_effort=True, cwd=self.tmp_dir)
        self.assertEqual(res["applied_count"], 1)
        self.assertEqual(res["failed_count"], 1)

        # File A was applied under best-effort
        with open(file_a, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "v1 = 10\n")

    def test_batch_editor_with_verify(self):
        """Tests that inline verify command executes and formats pass status."""
        file_a = os.path.join(self.tmp_dir, "calc.py")
        with open(file_a, "w", encoding="utf-8") as f:
            f.write("val = 1\n")

        edits = [
            {
                "path": "calc.py",
                "hunks": [{"target": "val = 1", "replacement": "val = 2"}]
            }
        ]

        verify_cmd = "python3 -m py_compile calc.py"
        res = execute_batch_edits(edits=edits, verify=verify_cmd, cwd=self.tmp_dir)
        self.assertTrue(res["success"])
        self.assertIn("verify", res)
        self.assertTrue(res["verify"]["passed"])
        self.assertEqual(res["verify"]["exit_code"], 0)

    def test_batch_editor_verify_blocks_shell_injection_and_escape(self):
        """Regression tests for CWE-78 / CWE-863 on apply_edits.verify and cwd."""
        file_a = os.path.join(self.tmp_dir, "safe.py")
        with open(file_a, "w", encoding="utf-8") as f:
            f.write("x = 1\n")
        edits = [{"path": "safe.py", "hunks": [{"target": "x = 1", "replacement": "x = 2"}]}]

        hostile_cmds = [
            "sh -c 'id; uname -a; cat ~/.ssh/id_rsa 2>/dev/null | head -c 200'",
            "bash -c 'echo pwned'",
            "python3 -c 'import os; os.system(\"id\")'",
            "pytest && cat ~/.ssh/id_rsa",
            "pytest; id",
            "id",
            "/bin/sh -c id",
            "cat ~/.ssh/id_rsa",
        ]
        for bad_cmd in hostile_cmds:
            res = execute_batch_edits(edits=edits, verify=bad_cmd, cwd=self.tmp_dir)
            self.assertFalse(res["success"], f"Command should be blocked: {bad_cmd}")
            self.assertTrue(res.get("blocked"), f"Expected blocked=True for {bad_cmd}")
            with open(file_a, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "x = 1\n", f"File must NOT be modified on blocked verify: {bad_cmd}")

        # Hostile system cwd must also fail closed
        res_root = execute_batch_edits(edits=edits, verify="pytest", cwd="/")
        self.assertFalse(res_root["success"])

        # MCP dispatcher must also reject PoC (system cwd + hostile sh -c verify)
        from src.mcp_server import _dispatch_tool_call
        mcp_res = _dispatch_tool_call("apply_edits", {
            "edits": edits,
            "verify": "sh -c 'id; uname -a; cat ~/.ssh/id_rsa 2>/dev/null | head -c 200'",
            "cwd": "/",
        })
        self.assertTrue(mcp_res.get("isError"))
        mcp_res_ws = _dispatch_tool_call("apply_edits", {
            "edits": edits,
            "verify": "sh -c 'id; uname -a; cat ~/.ssh/id_rsa 2>/dev/null | head -c 200'",
            "cwd": self.tmp_dir,
        })
        self.assertTrue(mcp_res_ws.get("isError"))
        self.assertIn("disallowed", mcp_res_ws["content"][0]["text"].lower())

    def test_batch_editor_secret_leak_blocked(self):
        """Tests that hardcoded private keys or secrets in replacements are blocked."""
        file_a = os.path.join(self.tmp_dir, "auth.py")
        with open(file_a, "w", encoding="utf-8") as f:
            f.write("API_KEY = ''\n")

        # Dynamically construct test pattern so file writer does not trip guard
        test_secret = "ghp_" + ("a1b2c3d4" * 4) + "1234"
        edits = [
            {
                "path": "auth.py",
                "hunks": [{"target": "API_KEY = ''", "replacement": f"API_KEY = os.environ.get('KEY', '{test_secret}')"}]
            }
        ]

        res = execute_batch_edits(edits=edits, cwd=self.tmp_dir)
        self.assertFalse(res["success"])
        self.assertTrue(any("Security Guard" in err.get("error", "") for err in res["failed_files"]))

    def test_batch_editor_traversal_symlinks_crlf_and_dedup(self):
        # Reject absolute paths, ../sibling escapes, and symlinks outside workspace
        res_abs = execute_batch_edits([{"path": "/etc/passwd", "create": "x"}], cwd=self.tmp_dir)
        self.assertFalse(res_abs["success"])
        res_rel = execute_batch_edits([{"path": "../outside_evil.py", "create": "x"}], cwd=self.tmp_dir)
        self.assertFalse(res_rel["success"])
        outside_dir = tempfile.mkdtemp(prefix="tokensafe_outside_")
        self.addCleanup(__import__("shutil").rmtree, outside_dir, ignore_errors=True)
        sym_path = os.path.join(self.tmp_dir, "escape_link")
        try:
            os.symlink(outside_dir, sym_path)
            res_sym = execute_batch_edits([{"path": "escape_link/pwn.py", "create": "x"}], cwd=self.tmp_dir)
            self.assertFalse(res_sym["success"])
            self.assertFalse(os.path.exists(os.path.join(outside_dir, "pwn.py")))
        except OSError:
            pass

        # Preserve CRLF line endings & apply sequential edits on same file without losing first edit
        crlf_file = os.path.join(self.tmp_dir, "win.txt")
        with open(crlf_file, "wb") as f:
            f.write(b"line1 = 1\r\nline2 = 2\r\n")
        res_crlf = execute_batch_edits([
            {"path": "win.txt", "hunks": [{"target": "line1 = 1", "replacement": "line1 = 10\nline1b = 15"}]},
            {"path": "win.txt", "hunks": [{"target": "line2 = 2", "replacement": "line2 = 20"}]},
        ], cwd=self.tmp_dir)
        self.assertTrue(res_crlf["success"], res_crlf)
        with open(crlf_file, "rb") as f:
            raw = f.read()
        self.assertEqual(raw, b"line1 = 10\r\nline1b = 15\r\nline2 = 20\r\n")

    def test_batch_editor_lone_surrogate_does_not_truncate_file(self):
        """Regression: a JSON-RPC lone surrogate must not leave a truncated file."""
        target = os.path.join(self.tmp_dir, "surrogate.py")
        original = "value = 1\nsecond = 2\n"
        with open(target, "w", encoding="utf-8") as f:
            f.write(original)

        res = execute_batch_edits(edits=[{
            "path": "surrogate.py",
            "hunks": [{"target": "value = 1", "replacement": "value = " + "\ud800"}],
        }], cwd=self.tmp_dir)

        self.assertFalse(res["success"])
        with open(target, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), original)

    def test_batch_editor_rejects_non_utf8_file(self):
        """Regression: non-UTF-8 bytes must never be silently rewritten as U+FFFD."""
        target = os.path.join(self.tmp_dir, "legacy.txt")
        original = "привет мир\n".encode("cp1251")
        with open(target, "wb") as f:
            f.write(original)

        res = execute_batch_edits(edits=[{
            "path": "legacy.txt",
            "hunks": [{"target": "hello", "replacement": "bye"}],
        }], cwd=self.tmp_dir)

        self.assertFalse(res["success"])
        self.assertIn("not valid UTF-8", res["failed_files"][0]["error"])
        with open(target, "rb") as f:
            self.assertEqual(f.read(), original)

    def test_batch_editor_eol_matches_file_not_replacement(self):
        """Regression: an LF file must not receive CRLF from the replacement text."""
        lf_file = os.path.join(self.tmp_dir, "unix.py")
        with open(lf_file, "wb") as f:
            f.write(b"first = 1\nsecond = 2\n")

        res = execute_batch_edits(edits=[{
            "path": "unix.py",
            "hunks": [{
                "target": "first = 1\nsecond = 2",
                "replacement": "first = 10\r\nsecond = 20",
            }],
        }], cwd=self.tmp_dir)

        self.assertTrue(res["success"], res)
        with open(lf_file, "rb") as f:
            self.assertEqual(f.read(), b"first = 10\nsecond = 20\n")

    def test_batch_editor_noop_hunk_and_invalid_bounds(self):
        """Regression: no-op hunks are not counted; invalid bounds fail per-file, not as a crash."""
        target = os.path.join(self.tmp_dir, "noop.py")
        with open(target, "w", encoding="utf-8") as f:
            f.write("a = 1\nb = 2\n")

        noop = execute_batch_edits(edits=[{
            "path": "noop.py",
            "hunks": [{"target": "a = 1", "replacement": "a = 1"}],
        }], cwd=self.tmp_dir)
        self.assertTrue(noop["success"])
        self.assertEqual(noop["applied_files"][0]["hunks"], 0)
        self.assertEqual(noop["applied_files"][0]["noop_hunks"], 1)
        self.assertEqual(noop["changed_hunks"], 0)
        self.assertTrue(noop["applied_files"][0]["unchanged"])

        for bad in ({"start_line": 0}, {"end_line": -1}, {"start_line": "abc"}):
            hunk = {"target": "a = 1", "replacement": "a = 2"}
            hunk.update(bad)
            res = execute_batch_edits(edits=[{"path": "noop.py", "hunks": [hunk]}], cwd=self.tmp_dir)
            self.assertFalse(res["success"], bad)
            self.assertTrue(res["failed_files"], bad)

        # end_line past EOF is clamped (clients pass start+limit), never a hard failure,
        # and the clamp is reported so the shrunken region is not silent.
        clamped = execute_batch_edits(edits=[{
            "path": "noop.py",
            "hunks": [{"target": "b = 2", "replacement": "b = 3", "end_line": 99}],
        }], cwd=self.tmp_dir)
        self.assertTrue(clamped["success"], clamped)
        self.assertEqual(clamped["applied_files"][0]["clamped_hunks"], 1)
        with open(target, encoding="utf-8") as f:
            self.assertEqual(f.read(), "a = 1\nb = 3\n")

    def test_batch_editor_create_conflicts_with_hunks(self):
        """Regression: 'create' must not silently discard hunks already applied to the same path."""
        target = os.path.join(self.tmp_dir, "mixed.py")
        with open(target, "w", encoding="utf-8") as f:
            f.write("x = 1\n")

        res = execute_batch_edits(edits=[
            {"path": "mixed.py", "hunks": [{"target": "x = 1", "replacement": "x = 2"}]},
            {"path": "mixed.py", "create": "x = 3\n", "overwrite": True},
        ], cwd=self.tmp_dir)

        self.assertFalse(res["success"])
        self.assertTrue(any("Cannot also 'create'" in e["error"] for e in res["failed_files"]))
        with open(target, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "x = 1\n")

    def test_batch_editor_verify_failure_rolls_back(self):
        """Atomic mode means the verify step cannot leave half-applied edits behind."""
        target = os.path.join(self.tmp_dir, "rollback.py")
        original = "val = 1\n"
        with open(target, "w", encoding="utf-8") as f:
            f.write(original)

        edits = [{"path": "rollback.py", "hunks": [{"target": "val = 1", "replacement": "val = 2"}]}]
        failing_verify = "python3 -m py_compile does_not_exist_xyz.py"

        res = execute_batch_edits(edits=edits, verify=failing_verify, cwd=self.tmp_dir)
        self.assertFalse(res["success"])
        self.assertTrue(res.get("rolled_back"))
        self.assertEqual(res["applied_count"], 0)
        with open(target, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), original)

        # best_effort keeps the edits and says so
        res_be = execute_batch_edits(edits=edits, verify=failing_verify, best_effort=True, cwd=self.tmp_dir)
        self.assertFalse(res_be["success"])
        self.assertFalse(res_be.get("rolled_back"))
        self.assertTrue(res_be.get("edits_kept"))
        with open(target, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "val = 2\n")

    def test_batch_editor_preserves_file_mode(self):
        """Regression: rewriting a file must keep its permission bits."""
        target = os.path.join(self.tmp_dir, "script.sh")
        with open(target, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho one\n")
        os.chmod(target, 0o750)

        res = execute_batch_edits(edits=[{
            "path": "script.sh",
            "hunks": [{"target": "echo one", "replacement": "echo two"}],
        }], cwd=self.tmp_dir)

        self.assertTrue(res["success"], res)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o750)

    def test_batch_search_queries(self):
        """Tests concurrent batch search across files."""
        sub = os.path.join(self.tmp_dir, "src")
        os.makedirs(sub, exist_ok=True)
        with open(os.path.join(sub, "service.ts"), "w", encoding="utf-8") as f:
            f.write("export async function handleRequest() {\n    return 'ok';\n}\n")
        with open(os.path.join(sub, "utils.py"), "w", encoding="utf-8") as f:
            f.write("def helper():\n    return 42\n")

        queries = [
            {"pattern": "handleRequest", "type": "text"},
            {"pattern": r"def\s+helper", "type": "regex"},
            {"pattern": "*.ts", "type": "glob"},
            {"pattern": "util", "type": "fuzzy"}
        ]

        res = execute_batch_search(queries=queries, cwd=self.tmp_dir)
        self.assertTrue(res["success"])
        self.assertEqual(res["query_count"], 4)
        self.assertIn("handleRequest", res["summary_md"])
        self.assertIn("helper", res["summary_md"])

    def test_batch_search_file_scoped_path(self):
        """Regression: an explicit file in 'path' must be searched, not walked as a directory.

        A directory walk over a file path yields zero hits (silently), and rg without
        --with-filename emits no path prefix, which the parser then drops.
        """
        sub = os.path.join(self.tmp_dir, "src")
        os.makedirs(sub, exist_ok=True)
        with open(os.path.join(sub, "service.ts"), "w", encoding="utf-8") as f:
            f.write("export function handleRequest() {}\nexport function other() {}\n")
        with open(os.path.join(sub, "decoy.ts"), "w", encoding="utf-8") as f:
            f.write("export function handleRequest() {}\n")

        res = execute_batch_search(queries=[
            {"pattern": "handleRequest", "type": "text", "path": "src/service.ts"},
            {"pattern": "*.ts", "type": "glob", "path": "src/service.ts"},
            {"pattern": "service", "type": "fuzzy", "path": "src/service.ts"},
        ], cwd=self.tmp_dir)

        self.assertTrue(res["success"])
        text_q, glob_q, fuzzy_q = res["results"]
        self.assertEqual(text_q["count"], 1)
        self.assertEqual(text_q["matches"][0]["file"], os.path.join("src", "service.ts"))
        self.assertEqual(glob_q["matches"], [os.path.join("src", "service.ts")])
        self.assertEqual(fuzzy_q["matches"], [os.path.join("src", "service.ts")])

    def test_python_text_search_file_scoped(self):
        """Regression: the pure-Python fallback (used when rg is absent) searches a single file."""
        target = os.path.join(self.tmp_dir, "single.py")
        with open(target, "w", encoding="utf-8") as f:
            f.write("def target_fn():\n    return 1\n")

        matches = _run_python_text_search("target_fn", cwd=self.tmp_dir, path_filter="single.py")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["file"], "single.py")
        self.assertEqual(matches[0]["line"], 1)

    def test_batch_search_reports_errors_instead_of_silent_zeros(self):
        """Regression: missing path / bad regex / unknown file_type are errors, not empty results."""
        sub = os.path.join(self.tmp_dir, "src")
        os.makedirs(sub, exist_ok=True)
        with open(os.path.join(sub, "a.py"), "w", encoding="utf-8") as f:
            f.write("def alpha():\n    return 1\n")
        with open(os.path.join(sub, "b.ts"), "w", encoding="utf-8") as f:
            f.write("export function alpha() {}\n")

        res = execute_batch_search(queries=[
            {"pattern": "alpha", "type": "text", "path": "no/such/dir"},
            {"pattern": "alpha(", "type": "regex"},
            {"pattern": "alpha", "type": "text", "file_type": "cobol"},
        ], cwd=self.tmp_dir)

        self.assertFalse(res["success"])
        for q in res["results"]:
            self.assertIn("error", q, q)

    def test_python_fallback_honors_file_type(self):
        """Regression: file_type must not be ignored when rg is unavailable."""
        sub = os.path.join(self.tmp_dir, "src")
        os.makedirs(sub, exist_ok=True)
        with open(os.path.join(sub, "a.py"), "w", encoding="utf-8") as f:
            f.write("marker here\n")
        with open(os.path.join(sub, "b.ts"), "w", encoding="utf-8") as f:
            f.write("marker here\n")

        py_only = _run_python_text_search("marker", cwd=self.tmp_dir, file_type="python")
        self.assertEqual([m["file"] for m in py_only], [os.path.join("src", "a.py")])

        ts_only = _run_python_text_search("marker", cwd=self.tmp_dir, file_type="typescript")
        self.assertEqual([m["file"] for m in ts_only], [os.path.join("src", "b.ts")])

        with self.assertRaises(ValueError):
            _run_python_text_search("marker", cwd=self.tmp_dir, file_type="cobol")

        with self.assertRaises(ValueError):
            _run_python_text_search("marker(", cwd=self.tmp_dir, is_regex=True)

    def test_batch_search_git_blocks_escaping_flags(self):
        """Regression: read-only-but-escaping git options must be rejected."""
        blocked = [
            "git diff --no-index /etc/passwd README.md",
            "git blame --contents=/etc/passwd README.md",
            "git log --pathspec-from-file=/etc/passwd",
            "git diff --textconv",
        ]
        for cmd in blocked:
            res = execute_batch_search([{"pattern": cmd, "type": "git"}], cwd=self.tmp_dir)
            self.assertTrue(res["results"][0].get("error"), f"Expected block for: {cmd}")

    def test_batch_search_honors_max_results_and_success(self):
        """Regression: rendering must not cap at 15 items; all-error batches are failures."""
        target = os.path.join(self.tmp_dir, "many.txt")
        with open(target, "w", encoding="utf-8") as f:
            f.write("needle\n" * 40)

        res = execute_batch_search(
            [{"pattern": "needle", "type": "text", "path": "many.txt", "max_results": 30}],
            cwd=self.tmp_dir,
        )
        self.assertTrue(res["success"])
        rendered = res["summary_md"].count("- `many.txt:")
        self.assertEqual(rendered, 30, res["summary_md"][:400])

    def test_workspace_snapshot(self):
        """Tests workspace snapshot generation for project directory."""
        with open(os.path.join(self.tmp_dir, "package.json"), "w", encoding="utf-8") as f:
            json.dump({"scripts": {"test": "vitest", "build": "vite build"}}, f)

        src_dir = os.path.join(self.tmp_dir, "src")
        os.makedirs(src_dir, exist_ok=True)
        with open(os.path.join(src_dir, "main.ts"), "w", encoding="utf-8") as f:
            f.write("console.log('hi');\n")

        snap = build_workspace_snapshot(self.tmp_dir, max_bytes=1400)
        self.assertIsNotNone(snap)
        self.assertIn("[TokenSafe Workspace Snapshot]", snap)
        self.assertIn("Layout (2 levels):", snap)
        self.assertIn("Scripts: test", snap)
        self.assertLessEqual(len(snap.encode("utf-8")), 1400)

    def test_snapshot_porcelain_paths_and_gitignore(self):
        """Regression: porcelain paths keep their prefix; gitignored dirs stay out of the snapshot."""
        from src.snapshot import _parse_porcelain_paths
        from src.snapshot import build_workspace_snapshot as build_snap

        raw = (
            " M src/index.ts\n"
            "R  old/name.py -> new/name.py\n"
            "?? untracked.md\n"
        )
        self.assertEqual(
            _parse_porcelain_paths(raw),
            ["src/index.ts", "new/name.py", "untracked.md"],
        )

        with open(os.path.join(self.tmp_dir, ".gitignore"), "w", encoding="utf-8") as f:
            f.write("generated-out/\n")
        os.makedirs(os.path.join(self.tmp_dir, "generated-out"), exist_ok=True)
        with open(os.path.join(self.tmp_dir, "generated-out", "junk.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        os.makedirs(os.path.join(self.tmp_dir, "src"), exist_ok=True)

        snap = build_snap(self.tmp_dir, max_bytes=1400)
        self.assertIsNotNone(snap)
        self.assertNotIn("generated-out", snap)

    def test_snapshot_unknown_on_git_failure_and_truncation_marker(self):
        """Regression: a failed 'git status' is 'unknown', never 'clean'; truncation is marked."""
        from unittest import mock
        from src import snapshot as snapshot_mod

        class _Res:
            def __init__(self, stdout: str, returncode: int):
                self.stdout = stdout
                self.returncode = returncode

        def fake_run(cmd, *args, **kwargs):
            joined = " ".join(str(c) for c in cmd)
            if "rev-parse" in joined:
                return _Res("main\n", 0)
            if "status" in joined:
                return _Res("", 128)
            return _Res("abc1234 first commit\n", 0)

        with mock.patch.object(snapshot_mod.subprocess, "run", side_effect=fake_run):
            summary = snapshot_mod._get_git_summary(self.tmp_dir)

        self.assertIn("unknown", summary)
        self.assertNotIn("clean", summary)

        os.makedirs(os.path.join(self.tmp_dir, "src"), exist_ok=True)
        for name in ("a.py", "b.py", "c.py"):
            with open(os.path.join(self.tmp_dir, "src", name), "w", encoding="utf-8") as f:
                f.write("x = 1\n")
        snap = snapshot_mod.build_workspace_snapshot(self.tmp_dir, max_bytes=60)
        self.assertIsNotNone(snap)
        self.assertLessEqual(len(snap.encode("utf-8")), 60)
        self.assertIn("(truncated)", snap)

    def test_transcript_claude_message_id_dedupe_and_unknown_schema(self):
        """Regression: Claude repeats usage per content block; unknown schemas are honest."""
        claude_file = os.path.join(self.tmp_dir, "claude.jsonl")
        usage = {"input_tokens": 10, "cache_creation_input_tokens": 100,
                 "cache_read_input_tokens": 50, "output_tokens": 7}
        with open(claude_file, "w", encoding="utf-8") as f:
            # Same message id across three content-block lines (real Claude layout)
            for block in ({"type": "text", "text": "hi"},
                          {"type": "tool_use", "name": "Bash"},
                          {"type": "text", "text": "done"}):
                f.write(json.dumps({
                    "type": "assistant",
                    "timestamp": "2026-11-24T12:00:00Z",
                    "message": {"id": "msg_1", "role": "assistant", "model": "claude-x",
                                "usage": usage, "content": [block]},
                }) + "\n")

        res = analyze_transcript(claude_file, agent_type="claude")
        self.assertEqual(res["llm_turns"], 1)
        self.assertEqual(res["input_tokens"], 160)   # counted once, not 3x
        self.assertEqual(res["total_tool_calls"], 1)

        unknown_file = os.path.join(self.tmp_dir, "weird.jsonl")
        with open(unknown_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({"totally": "different", "format": True}) + "\n")
            f.write(json.dumps({"another": [1, 2, 3]}) + "\n")

        unknown = analyze_transcript(unknown_file, agent_type="auto")
        self.assertEqual(unknown["agent"], "unknown")
        self.assertTrue(unknown.get("schema_warning"))
        self.assertEqual(unknown["llm_turns"], 0)

        # A line that HAS an unrecognised "type" used to count as a matched schema
        # (the Antigravity catch-all), hiding the warning behind all-zero metrics.
        typed_file = os.path.join(self.tmp_dir, "typed_unknown.jsonl")
        with open(typed_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "SOMETHING_UNKNOWN", "foo": 1}) + "\n")
            f.write(json.dumps({"type": "ANOTHER_UNKNOWN", "created_at": "2026-11-24T12:00:00Z"}) + "\n")
        typed = analyze_transcript(typed_file, agent_type="auto")
        self.assertEqual(typed["agent"], "unknown")
        self.assertTrue(typed.get("schema_warning"))
        self.assertEqual(typed["llm_turns"], 0)

        # An explicitly requested label must not be silently rewritten
        custom = analyze_transcript(claude_file, agent_type="custom")
        self.assertEqual(custom["agent"], "custom")

    def test_transcript_codex_null_info_and_antigravity_gaps(self):
        """Regression: null token_count is not a turn; non-turn steps do not create gaps."""
        codex_file = os.path.join(self.tmp_dir, "codex.jsonl")
        with open(codex_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "event_msg", "timestamp": "2026-11-24T12:00:00Z",
                                "payload": {"type": "token_count", "info": None}}) + "\n")
            f.write(json.dumps({"type": "event_msg", "timestamp": "2026-11-24T12:00:05Z",
                                "payload": {"type": "token_count",
                                            "info": {"last_token_usage": {"input_tokens": 5,
                                                                          "output_tokens": 2}}}}) + "\n")
        codex = analyze_transcript(codex_file, agent_type="codex")
        self.assertEqual(codex["llm_turns"], 1)

        ag_file = os.path.join(self.tmp_dir, "agy.jsonl")
        with open(ag_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "PLANNER_RESPONSE", "created_at": "2026-11-24T12:00:00Z",
                                "tool_calls": [{"name": "read_file"}]}) + "\n")
            f.write(json.dumps({"type": "PLANNER_RESPONSE", "created_at": "2026-11-24T12:00:10Z",
                                "tool_calls": [{"name": "edit_file"}]}) + "\n")
            # Long-running status step: must not be counted as a 5-minute turn gap
            f.write(json.dumps({"type": "GENERIC", "created_at": "2026-11-24T12:05:10Z"}) + "\n")
        agy = analyze_transcript(ag_file, agent_type="antigravity")
        self.assertEqual(agy["llm_turns"], 2)
        self.assertEqual(agy["long_turn_gaps_gt_60s"], 0)

    def test_zero_spill_guard_capping(self):
        """Tests that outputs > 8800 bytes are folded and tagged with stash anchor."""
        huge_text = "Line content item: " + ("x" * 80) + "\n"
        huge_payload = huge_text * 200  # ~16 KB

        result = {
            "content": [
                {"type": "text", "text": huge_payload}
            ]
        }

        capped = _cap_mcp_output_size(result, tool_name="test_tool", max_bytes=8800)
        capped_text = capped["content"][0]["text"]
        capped_bytes = len(capped_text.encode("utf-8"))

        self.assertLessEqual(capped_bytes, 8800)
        self.assertIn("TokenSafe Zero-Spill Guard", capped_text)
        self.assertIn("[TokenSafe folded: #", capped_text)

    def test_zero_spill_guard_per_response_and_fail_closed(self):
        """Regression: the cap applies to the whole response and survives stash failure."""
        blocks = [{"type": "text", "text": ch * 5000} for ch in ("A", "B", "C")]
        capped = _cap_mcp_output_size({"content": blocks}, tool_name="multi")
        total = sum(len(b["text"].encode("utf-8")) for b in capped["content"])
        self.assertLessEqual(total, 8800)
        self.assertLess(total, sum(len(b["text"].encode()) for b in blocks))

        # Many medium blocks must not slip through either
        many = {"content": [{"type": "text", "text": "y" * 1000} for _ in range(20)]}
        capped_many = _cap_mcp_output_size(many, tool_name="many")
        self.assertLessEqual(
            sum(len(b["text"].encode("utf-8")) for b in capped_many["content"]), 8800
        )

        # Stash unavailable: content must still be truncated (never passed through)
        import src.stash as stash_mod

        original = stash_mod.stash_block

        def _boom(*args, **kwargs):
            raise RuntimeError("stash offline")

        stash_mod.stash_block = _boom
        try:
            big = {"content": [{"type": "text", "text": "X" * 20000}]}
            capped_big = _cap_mcp_output_size(big, tool_name="nostash")
        finally:
            stash_mod.stash_block = original

        text = capped_big["content"][0]["text"]
        self.assertLessEqual(len(text.encode("utf-8")), 8800)
        self.assertIn("Zero-Spill", text)

    @staticmethod
    def _envelope_bytes(result) -> int:
        """Size of the JSON-RPC tools/call envelope that actually goes over stdio."""
        return len(json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}, ensure_ascii=False).encode("utf-8"))

    def test_zero_spill_guard_counts_json_escaping(self):
        """Newline-heavy output used to pass the cap raw and then double on the wire."""
        text = "a\n" * 3900  # 7800 raw UTF-8 bytes, ~11700 bytes JSON-escaped
        capped = _cap_mcp_output_size({"content": [{"type": "text", "text": text}]}, tool_name="nl")
        self.assertLessEqual(self._envelope_bytes(capped), 8800)
        self.assertNotEqual(capped["content"][0]["text"], text)

    def test_zero_spill_guard_handles_multibyte_text(self):
        text = ("строка вывода — тест\n" * 900)
        capped = _cap_mcp_output_size({"content": [{"type": "text", "text": text}]}, tool_name="ru")
        self.assertLessEqual(self._envelope_bytes(capped), 8800)
        kept = capped["content"][0]["text"]
        self.assertTrue(kept.startswith("строка") or kept.startswith("...") or "folded" in kept)

    def test_transcript_analyzer(self):
        """Tests parsing of JSONL transcript and A/B comparison."""
        transcript_a = os.path.join(self.tmp_dir, "transcript_a.jsonl")
        transcript_b = os.path.join(self.tmp_dir, "transcript_b.jsonl")

        with open(transcript_a, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "read_file"}], "created_at": "2026-11-24T12:00:00Z"}) + "\n")
            f.write(json.dumps({"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "edit_file"}], "created_at": "2026-11-24T12:00:10Z"}) + "\n")
            f.write(json.dumps({"type": "PLANNER_RESPONSE", "content": "The output was large and was saved to: file:///tmp/output.txt", "created_at": "2026-11-24T12:00:20Z"}) + "\n")

        with open(transcript_b, "w", encoding="utf-8") as f:
            # Batch call: 1 turn, 2 tools, 0 spills
            f.write(json.dumps({"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "tokensafe_apply_edits"}, {"name": "tokensafe_batch_search"}], "created_at": "2026-11-24T12:00:00Z"}) + "\n")

        analysis_a = analyze_transcript(transcript_a)
        self.assertEqual(analysis_a["llm_turns"], 3)
        self.assertEqual(analysis_a["disk_spills"], 1)

        analysis_b = analyze_transcript(transcript_b)
        self.assertEqual(analysis_b["llm_turns"], 1)
        self.assertEqual(analysis_b["disk_spills"], 0)
        self.assertEqual(analysis_b["tokensafe_calls"], 2)

        comp = compare_transcripts(transcript_a, transcript_b)
        self.assertEqual(comp["llm_turns"]["before"], 3)
        self.assertEqual(comp["llm_turns"]["after"], 1)
        self.assertEqual(comp["disk_spills"]["delta"], -1)


if __name__ == "__main__":
    unittest.main()
