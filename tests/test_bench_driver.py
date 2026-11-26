"""Tests for the measured benchmark harness (no network, no API key needed)."""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from src.bench import go_target, react_target
from src.bench.driver import PRICES, Usage, resolve_api_key
from src.bench.runner import render_markdown, summarize


class TestUsageAccounting(unittest.TestCase):
    def test_cost_splits_cache_hit_and_miss(self):
        usage = Usage()
        usage.add({
            "prompt_tokens": 1_000_000,
            "prompt_cache_hit_tokens": 750_000,
            "completion_tokens": 100_000,
        })
        price = PRICES["deepseek-chat"]
        expected = round(
            (250_000 / 1_000_000) * price["input_miss"]
            + (750_000 / 1_000_000) * price["input_hit"]
            + (100_000 / 1_000_000) * price["output"],
            6,
        )
        self.assertEqual(usage.cache_miss_tokens, 250_000)
        self.assertEqual(usage.usd("deepseek-chat"), expected)
        payload = usage.as_dict("deepseek-chat")
        self.assertEqual(payload["calls"], 1)
        self.assertEqual(payload["input_tokens"], 1_000_000)


class TestKeyResolution(unittest.TestCase):
    def test_env_wins(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "env-key"}):
            self.assertEqual(resolve_api_key(), "env-key")

    def test_falls_back_to_tokensafe_config(self):
        home = tempfile.mkdtemp(prefix="tokensafe_bench_home_")
        try:
            data = os.path.join(home, ".tokensafe", "data")
            os.makedirs(data)
            with open(os.path.join(data, "config.json"), "w", encoding="utf-8") as f:
                json.dump({"deepseek_api_key": "cfg-key"}, f)
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "", "HOME": home}):
                self.assertEqual(resolve_api_key(), "cfg-key")
        finally:
            shutil.rmtree(home, ignore_errors=True)


class TestReportRendering(unittest.TestCase):
    def test_markdown_labels_measured_and_aggregates(self):
        report = {
            "model": "deepseek-chat",
            "runs": 1,
            "budget_usd": 0.3,
            "spent_usd": 0.01,
            "generated_at": "2026-11-26T00:00:00Z",
            "aborted": None,
            "results": [
                {
                    "arm": "baseline", "task": "t1", "run": 1, "solved": True,
                    "oracle_before": {"passed": 11}, "oracle_after": {"passed": 12},
                    "stop_reason": "solved", "steps": [{"index": 1}],
                    "usage": {"input_tokens": 100, "output_tokens": 20, "usd": 0.001},
                },
                {
                    "arm": "oumv", "task": "t1", "run": 1, "solved": False,
                    "oracle_before": {"passed": 11}, "oracle_after": {"passed": 11},
                    "stop_reason": "max_steps", "steps": [{"index": 1}, {"index": 2}],
                    "usage": {"input_tokens": 200, "output_tokens": 30, "usd": 0.002},
                },
            ],
        }
        rows = {row["arm"]: row for row in summarize(report)}
        self.assertEqual(rows["baseline"]["solved"], 1)
        self.assertEqual(rows["oumv"]["solved"], 0)
        self.assertEqual(rows["oumv"]["steps"], 2)
        markdown = render_markdown(report)
        self.assertIn("measured", markdown.lower())
        self.assertIn("| `baseline` | 1 | 1/1 |", markdown)


@unittest.skipUnless(shutil.which("npx") and react_target.PACKAGE.is_dir(), "node/npx or benchmark package missing")
class TestReactTargetBreaks(unittest.TestCase):
    """The React sandbox must be genuinely React and genuinely broken per task."""

    def test_each_react_task_fails_the_oracle(self):
        for task_id in react_target.TASKS:
            with self.subTest(task=task_id):
                ws = react_target.ReactWorkspace(task_id)
                try:
                    passed, failed, status = ws.oracle()
                    self.assertLess(passed, 12, f"{task_id} is not broken: {passed}/12 {status}")
                    self.assertTrue(failed, f"{task_id} produced no failing assertion ids")
                finally:
                    ws.cleanup()


@unittest.skipUnless(shutil.which("go") and go_target.GO_ROOT.is_dir(), "go toolchain or module missing")
class TestGoTargetBreaks(unittest.TestCase):
    """Every task must start broken — otherwise it measures nothing."""

    def test_each_task_fails_the_oracle_before_the_agent_runs(self):
        for task_id in go_target.TASKS:
            with self.subTest(task=task_id):
                ws = go_target.GoWorkspace(task_id)
                try:
                    passed, failed, status = ws.oracle()
                    self.assertLess(passed, 12, f"{task_id} is not broken: {passed}/12")
                    self.assertTrue(failed, f"{task_id} produced no failing assertion ids")
                finally:
                    ws.cleanup()

    def test_oumv_tools_see_the_sandbox(self):
        ws = go_target.GoWorkspace("order-conflict-status")
        try:
            surface = ws.oumv("oumv/observe", {"level": "L1"})
            paths = [e["path"] for e in surface["data"]["endpoints"]]
            self.assertIn("/api/products", paths)
            source = ws.oumv("oumv/getSource", {"path": go_target.SANDBOX_FILE})
            self.assertTrue(source["version"].startswith("sha256:"))
            verify = ws.oumv("oumv/verify", {"target": "POST /api/products", "payload": {"id": 1}})
            self.assertIn(verify.get("status"), ("passed", "failed"))
            self.assertIn("checked", verify.get("scope", {}))
        finally:
            ws.cleanup()

    def test_l3_resolves_an_endpoint_to_its_handler(self):
        ws = go_target.GoWorkspace("product-create-status")
        try:
            result = ws.oumv("oumv/observe", {"level": "L3", "target": "POST /api/products"})
            self.assertNotIn("error", result)
            data = result["data"]
            self.assertEqual(data["path"], go_target.SANDBOX_FILE)
            self.assertIn('r.POST("/api/products"', data["source"])
            self.assertIn("StatusOK", data["source"])
            self.assertTrue(data["version"].startswith("sha256:"))
        finally:
            ws.cleanup()

    def test_verify_sees_edits_made_in_the_workspace(self):
        """The runtime must serve the edited code, not a binary built from the pristine app."""
        from pathlib import Path

        ws = go_target.GoWorkspace("product-create-status")
        try:
            before = ws.oumv("oumv/verify", {"target": "POST /api/products", "payload": {"title": "x", "price": 1}})
            self.assertEqual(before.get("statusCode"), 200)
            source = ws.oumv("oumv/getSource", {"path": go_target.SANDBOX_FILE})
            full = Path(ws.dir, go_target.SANDBOX_FILE).read_text(encoding="utf-8")
            fixed = full.replace("c.JSON(http.StatusOK, p)", "c.JSON(http.StatusCreated, p)")
            applied = ws.oumv(
                "oumv/applyEdit",
                {"path": go_target.SANDBOX_FILE, "replacement": fixed, "expectedVersion": source["version"]},
            )
            self.assertEqual(applied.get("status"), "APPLIED")
            after = ws.oumv("oumv/verify", {"target": "POST /api/products", "payload": {"title": "x", "price": 1}})
            self.assertEqual(after.get("statusCode"), 201)
        finally:
            ws.cleanup()


if __name__ == "__main__":
    unittest.main()

