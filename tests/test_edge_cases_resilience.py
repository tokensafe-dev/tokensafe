"""
Comprehensive Edge-Case and Fault-Tolerance Test Suite.
Verifies the 'Zero Loss Context' guarantee (Exact code context)
and 'Never Harm User' fail-open try/catch directives across all engines:
1. Exact Code Context & Stash Reversibility
2. SQLite FTS5 Engine Edge Cases & Corrupt DB Resilience
3. Graph Engine SQLite Cache Corruption & Route Linking Fault-Tolerance
4. Image Downscale Non-Mutating Fail-Open Resilience
5. Compiler Error Cascade & Stacktrace Cascade Reversibility
6. Superseded Transcript Pruner & Mistakes Ledger Safety
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import unittest

from src.graph_engine import CodeGraph, _normalize_route_path
from src.fts_index import CodeFTSIndex
from src.scoped_search import ScopedSearch
from src.image_downscale import ImageDownscaler
from src.superseded import SupersededPruner
from src.mistakes import MistakesLedger
from src.format_parsers.compiler_errors import CompilerErrorCompactor
from src.stash import stash_block, expand_ref
from src.skeletonizer import Skeletonizer


class TestExactCodeContext(unittest.TestCase):
    """
    Validates our zero-loss context guarantee:
    The targeted function or class body MUST retain every single character,
    comment, docstring, whitespace, and nested construct with 100% byte precision.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="tokensafe_exact_ctx_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_target_symbol_exact_body_preservation_python(self):
        code = '''# Header comment
import os
import re

def helper():
    return 1

def critical_algorithm(data: dict, factor: float = 1.5) -> float:
    """Multi-line docstring
    with formatting and quotes 'test' "double".
    """
    # Important internal comment
    total = 0.0
    for k, v in data.items():
        if factor > 1.0:
            total += v * factor
        else:
            total += v
    # Complex lambda
    calc = lambda x: x ** 2
    return calc(total)

def unrelated():
    pass
'''
        py_file = os.path.join(self.test_dir, "calc.py")
        with open(py_file, "w") as f:
            f.write(code)

        # Skeletonize keeping critical_algorithm
        skel = Skeletonizer.skeletonize_code(code, ".py", keep_symbols={"critical_algorithm"})

        # The full body of critical_algorithm must be completely present
        self.assertIn("def critical_algorithm(", skel)
        self.assertIn('"""Multi-line docstring', skel)
        self.assertIn("calc = lambda x: x ** 2", skel)
        self.assertIn("return calc(total)", skel)

        # Unrelated function must be folded to signature
        self.assertIn("def unrelated():", skel)
        self.assertIn("...", skel)

    def test_target_symbol_exact_body_preservation_typescript(self):
        code = '''import { useState } from 'react';

export function UnrelatedWidget() {
    return <div>Unrelated</div>;
}

export async function processPayment(cart: { id: string; amount: number }): Promise<boolean> {
    // Critical payment handler - MUST NOT BE CORRUPTED
    if (!cart || cart.amount <= 0) {
        throw new Error("Invalid amount");
    }
    const token = await fetch("/api/v1/token");
    const res = await fetch("/api/v1/charge", {
        method: "POST",
        body: JSON.stringify({ cartId: cart.id, token })
    });
    return res.ok;
}
'''
        skel = Skeletonizer.skeletonize_code(code, ".ts", keep_symbols={"processPayment"})

        # Target body must be 100% intact
        self.assertIn("export async function processPayment(cart: { id: string; amount: number }): Promise<boolean> {", skel)
        self.assertIn("// Critical payment handler - MUST NOT BE CORRUPTED", skel)
        self.assertIn('throw new Error("Invalid amount");', skel)
        self.assertIn("return res.ok;", skel)

        # Unrelated function body stripped
        self.assertNotIn("<div>Unrelated</div>", skel)

    def test_design_tokens_css_root_exact_preservation(self):
        css_code = ''':root {
  --primary-color: #3b82f6;
  --secondary-color: #10b981;
  --spacing-base: 8px;
  --font-sans: 'Inter', -apple-system, sans-serif;
}

@theme {
  --color-accent: #f59e0b;
}

.huge-card {
  display: flex;
  padding: 20px;
  margin: 10px;
  background: white;
}
'''
        # Skeletonize without targeting .huge-card
        skel = Skeletonizer.skeletonize_code(css_code, ".css")
        # Design tokens in :root and @theme MUST be completely preserved
        self.assertIn("--primary-color: #3b82f6;", skel)
        self.assertIn("--secondary-color: #10b981;", skel)
        self.assertIn("--color-accent: #f59e0b;", skel)

    def test_stash_expand_ref_byte_perfect_reversibility(self):
        # Test that any folded block is 100% recoverable without losing a single character
        original_text = "ERROR 2026-11-16 01:00:00: Connection pool exhausted\n" * 50
        hash8, marker = stash_block(original_text, source="test_run")

        recovered = expand_ref(hash8)
        self.assertEqual(recovered, original_text)

        # Hash with '#' prefix
        recovered_prefixed = expand_ref(f"#{hash8}")
        self.assertEqual(recovered_prefixed, original_text)


class TestFtsIndexEdgeCasesAndFaultTolerance(unittest.TestCase):
    """
    Verifies SQLite FTS5 fault-tolerance, search sanitization,
    and fail-safe behavior when the database is corrupted or missing.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="tokensafe_fts_edge_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_fts_query_with_special_sqlite_characters(self):
        # Queries with quotes, brackets, asterisks, AND, OR, NOT, emojis
        src_file = os.path.join(self.test_dir, "special.py")
        with open(src_file, "w") as f:
            f.write('def find_user():\n    return "user_42" + "!@#$%^&*()"\n')

        fts = CodeFTSIndex(self.test_dir)
        fts.index_files()

        # Unbalanced quotes
        res1 = fts.search('"""user_42"""')
        self.assertIsInstance(res1, list)

        # Operators like AND / OR / NOT in query
        res2 = fts.search("find_user AND user_42")
        self.assertIsInstance(res2, list)

        # Emojis and unicode
        res3 = fts.search("🚀🔥⚡")
        self.assertEqual(res3, [])

        # Empty string and whitespace
        self.assertEqual(fts.search(""), [])
        self.assertEqual(fts.search("    "), [])

    def test_fts_corrupt_database_fail_safe(self):
        fts = CodeFTSIndex(self.test_dir)
        fts.index_files()

        # Corrupt the SQLite file deliberately by writing garbage bytes
        with open(fts.db_path, "wb") as f:
            f.write(b"GARBAGE_NOT_A_SQLITE_DATABASE_HEADER_AT_ALL_XXXXX")

        # Calling search() on a corrupted database must NEVER crash or throw
        res = fts.search("find_user")
        self.assertEqual(res, [])

        # ScopedSearch must also gracefully fall back without crashing
        scoped = ScopedSearch.search("find_user", project_path=self.test_dir)
        self.assertIsInstance(scoped, dict)
        self.assertTrue(scoped.get("success", False))

    def test_fts_extreme_long_lines_protection(self):
        # A file with a 50,000-character single line (e.g. minified JS or base64)
        huge_file = os.path.join(self.test_dir, "huge_minified.js")
        with open(huge_file, "w") as f:
            f.write("var a = 1;\n" + "x" * 50000 + "\nvar b = 2;\n")

        fts = CodeFTSIndex(self.test_dir)
        stats = fts.index_files()
        self.assertGreaterEqual(stats["scanned_files"], 1)

        # Searching for normal identifiers must succeed
        res = fts.search("var a")
        self.assertTrue(any("huge_minified.js" in r["file"] for r in res))


class TestGraphEngineResilienceAndRoutes(unittest.TestCase):
    """
    Verifies graph cache corruption recovery and API route normalization edge cases.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="tokensafe_graph_edge_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_corrupted_sqlite_graph_cache_recovery(self):
        f = os.path.join(self.test_dir, "sample.py")
        with open(f, "w") as fp:
            fp.write("def sample_fn(): return 1\n")

        graph = CodeGraph(self.test_dir, use_disk_cache=True)
        graph.scan()
        cache_file = graph._cache_path()
        self.assertTrue(os.path.exists(cache_file))

        # Corrupt the cache file
        with open(cache_file, "wb") as fp:
            fp.write(b"CORRUPTED_CACHE_BYTES")

        # New CodeGraph instance must not crash when loading corrupted SQLite cache
        graph2 = CodeGraph(self.test_dir, use_disk_cache=True)
        # Should gracefully re-scan and repopulate symbols
        reindexed = graph2.scan()
        self.assertEqual(reindexed, 1)
        self.assertIn("sample.py::sample_fn", graph2.symbols)

    def test_route_normalization_edge_cases(self):
        # None and empty
        self.assertEqual(_normalize_route_path(""), "")
        self.assertEqual(_normalize_route_path(None), "")
        self.assertEqual(_normalize_route_path(123), "")

        # Full URL with query and hash
        self.assertEqual(
            _normalize_route_path("https://api.example.com:8080/api/v1/users?page=1&limit=10#top"),
            "/api/v1/users"
        )

        # Multiple consecutive slashes
        self.assertEqual(_normalize_route_path("///api///v1///users///"), "/api/v1/users")

        # Static assets must return empty (not routes)
        self.assertEqual(_normalize_route_path("/static/logo.svg"), "")
        self.assertEqual(_normalize_route_path("/images/banner.png"), "")
        self.assertEqual(_normalize_route_path("/styles/app.css"), "")
        self.assertEqual(_normalize_route_path("/fonts/inter.woff2"), "")

        # Root paths and star must return empty
        self.assertEqual(_normalize_route_path("/"), "")
        self.assertEqual(_normalize_route_path("*"), "")
        self.assertEqual(_normalize_route_path("/*"), "")

        # Complex template strings and express params
        self.assertEqual(_normalize_route_path("/api/v1/users/${userId}/posts/${postId}"), "/api/v1/users/*/posts/*")
        self.assertEqual(_normalize_route_path("/api/v1/users/:userId/posts/:postId"), "/api/v1/users/*/posts/*")
        self.assertEqual(_normalize_route_path("/api/v1/users/[userId]/posts/[...slug]"), "/api/v1/users/*/posts/*")


class TestImageDownscaleAndFailSafe(unittest.TestCase):
    """
    Verifies that ImageDownscaler NEVER mutates the original file
    and fails open on corrupted image files.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="tokensafe_img_edge_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_corrupted_image_file_fails_open(self):
        fake_png = os.path.join(self.test_dir, "corrupted.png")
        with open(fake_png, "wb") as f:
            f.write(b"THIS IS NOT A VALID PNG IMAGE DATA")

        with open(fake_png, "rb") as fp:
            orig_hash = hashlib.sha256(fp.read()).hexdigest()

        res = ImageDownscaler.optimize(fake_png, max_edge=1024)
        # Must succeed gracefully with was_downscaled=False
        self.assertTrue(res.get("success", False))
        self.assertFalse(res.get("was_downscaled", True))
        self.assertEqual(res.get("optimized_path"), fake_png)

        # Original file MUST NOT be altered
        with open(fake_png, "rb") as fp:
            curr_hash = hashlib.sha256(fp.read()).hexdigest()
        self.assertEqual(orig_hash, curr_hash)

    def test_nonexistent_image_returns_error_dict_not_throw(self):
        res = ImageDownscaler.optimize("/non/existent/path.png")
        self.assertFalse(res["success"])
        self.assertIn("not found", res["error"])


class TestSupersededAndMistakesResilience(unittest.TestCase):
    """
    Verifies transcript pruning with malformed inputs and mistakes ledger atomicity.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="tokensafe_misc_edge_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_prune_transcript_with_corrupted_jsonl(self):
        t_path = os.path.join(self.test_dir, "corrupted_transcript.jsonl")
        with open(t_path, "w") as f:
            f.write('{"step_index": 1, "type": "USER_INPUT"}\n')
            f.write('MALFORMED_JSON_LINE_HERE\n')
            f.write('{"step_index": 2, "type": "PLANNER_RESPONSE"}\n')

        res = SupersededPruner.prune_transcript(t_path)
        # Must not crash, should process valid lines
        self.assertTrue(res["success"])

    def test_mistakes_ledger_deduplication_and_limit(self):
        # Record 60 failures
        for i in range(60):
            MistakesLedger.record_failure(
                summary=f"Failure number {i}",
                file_path=f"src/file_{i % 5}.py",
                source="test",
                project_root=self.test_dir
            )

        mistakes = MistakesLedger.list_mistakes(project_root=self.test_dir)
        # Capped to MAX_ENTRIES = 50
        self.assertLessEqual(len(mistakes), 50)


class TestCompilerErrorsCompactorEdgeCases(unittest.TestCase):
    """
    Verifies that compiler and stacktrace cascades are never lost and revertible.
    """

    def test_tsc_short_errors_not_folded(self):
        # Output with <= 4 errors should not be folded (full context kept)
        raw_tsc = """src/app.ts:10:5 - error TS2322: Type 'string' is not assignable to type 'number'.
src/app.ts:15:3 - error TS2304: Cannot find name 'foo'.
Found 2 errors in src/app.ts.
"""
        compacted, was_compacted = CompilerErrorCompactor.compact_tsc(raw_tsc)
        self.assertFalse(was_compacted)
        self.assertEqual(compacted, raw_tsc)

    def test_tsc_massive_cascade_stashed_and_reversible(self):
        # 30 cascading TS errors
        raw_tsc = "\n".join([
            f"src/module_{i}.ts:{i+1}:4 - error TS2307: Cannot find module './missing' or its corresponding type declarations."
            for i in range(30)
        ]) + "\nFound 30 errors in 30 files.\n"

        compacted, was_compacted = CompilerErrorCompactor.compact_tsc(raw_tsc)
        self.assertTrue(was_compacted)
        self.assertIn("[TokenSafe folded:", compacted)

        # Extract 8-char hash
        m = re.search(r"\[TokenSafe folded: #([a-f0-9]{8})", compacted)
        self.assertIsNotNone(m)
        hash8 = m.group(1)

        # Restore from stash
        recovered = expand_ref(hash8)
        self.assertEqual(recovered, raw_tsc)


if __name__ == "__main__":
    unittest.main()
