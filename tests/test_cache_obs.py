"""CACHE_OBS cache-stability telemetry: aggregation math, mutation/throttle
behavior of the proxy recorder, and dashboard panel rendering."""
import os
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src import metrics as metrics_mod
from src.metrics import MetricsTracker


def _fresh_env(tmp: str):
    os.environ["TOKENSAFE_DATA_DIR"] = tmp
    os.environ["TOKENSAFE_DB_PATH"] = os.path.join(tmp, "metrics.db")
    os.environ["TOKENSAFE_TELEMETRY_DISABLED"] = "1"
    metrics_mod._INITIALIZED_METRICS_DBS.clear()


class TestCacheObsAggregation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="tokensafe_cacheobs_")
        _fresh_env(self._tmp)
        self.addCleanup(os.environ.pop, "TOKENSAFE_DATA_DIR", None)
        self.addCleanup(os.environ.pop, "TOKENSAFE_DB_PATH", None)
        self.addCleanup(os.environ.pop, "TOKENSAFE_TELEMETRY_DISABLED", None)

    def _obs(self, details: dict):
        MetricsTracker().record_event(
            "proxy_session", "CACHE_OBS", 0, 0, details=details, measurement="measured"
        )

    def test_anthropic_split(self):
        self._obs({"provider": "anthropic", "input": 1000, "cache_read": 9000,
                   "cache_write": 0, "mutations": []})
        self._obs({"provider": "anthropic", "input": 1000, "cache_read": 5000,
                   "cache_write": 0, "mutations": ["ledger"]})
        res = MetricsTracker().get_cache_obs()
        self.assertEqual(res["total"], 2)
        self.assertEqual(res["clean"]["n"], 1)
        self.assertAlmostEqual(res["clean"]["hit_pct"], 90.0)  # 9000/(1000+9000)
        self.assertEqual(res["mutated"]["n"], 1)
        self.assertAlmostEqual(res["mutated"]["hit_pct"], 83.3, places=1)  # 5000/6000

    def test_openai_denom_includes_cache(self):
        self._obs({"provider": "openai", "input": 10000, "cache_read": 8000,
                   "cache_write": 0, "mutations": []})
        res = MetricsTracker().get_cache_obs()
        self.assertAlmostEqual(res["clean"]["hit_pct"], 80.0)

    def test_zero_denominator_skipped(self):
        self._obs({"provider": "anthropic", "input": 0, "cache_read": 0,
                   "cache_write": 0, "mutations": []})
        res = MetricsTracker().get_cache_obs()
        self.assertEqual(res["total"], 1)
        self.assertIsNone(res["clean"]["hit_pct"])
        self.assertEqual(res["clean"]["n"], 0)

    def test_empty_ledger(self):
        res = MetricsTracker().get_cache_obs()
        self.assertEqual(res["total"], 0)
        self.assertIsNone(res["clean"]["hit_pct"])


class TestProxyRecorder(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="tokensafe_cacheobs_px_")
        _fresh_env(self._tmp)
        self.addCleanup(os.environ.pop, "TOKENSAFE_DATA_DIR", None)
        self.addCleanup(os.environ.pop, "TOKENSAFE_DB_PATH", None)
        self.addCleanup(os.environ.pop, "TOKENSAFE_TELEMETRY_DISABLED", None)
        import src.proxy as proxy_mod
        proxy_mod._last_cache_obs_ts = 0.0
        self.addCleanup(setattr, proxy_mod, "_last_cache_obs_ts", 0.0)
        self._proxy_mod = proxy_mod

    def test_clean_throttled_mutated_always(self):
        calls = []

        class FakeTracker:
            def record_event(self, *args, **kwargs):
                calls.append(kwargs.get("event_type") or args[1] if args else kwargs.get("event_type"))

        self._proxy_mod._record_cache_obs(FakeTracker(), "anthropic", [], 100, 0, 0)
        self._proxy_mod._record_cache_obs(FakeTracker(), "anthropic", [], 100, 0, 0)
        self.assertEqual(len(calls), 1, "second clean sample must be throttled")
        self._proxy_mod._record_cache_obs(FakeTracker(), "anthropic", ["ledger"], 100, 0, 0)
        self.assertEqual(len(calls), 2, "mutated requests must always record")

    def test_details_shape(self):
        captured = {}

        class FakeTracker:
            def record_event(self, session_id, event_type, original_tokens, optimized_tokens, details=None, measurement=None):
                captured.update({
                    "session_id": session_id, "event_type": event_type,
                    "orig": original_tokens, "opt": optimized_tokens, "details": details,
                })

        self._proxy_mod._record_cache_obs(FakeTracker(), "anthropic", ["thinking"], 1200, 800, 50)
        self.assertEqual(captured["event_type"], "CACHE_OBS")
        self.assertEqual(captured["orig"], 0)
        self.assertEqual(captured["details"]["mutations"], ["thinking"])
        self.assertEqual(captured["details"]["cache_read"], 800)


class TestDashboardPanel(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="tokensafe_cacheobs_dash_")
        _fresh_env(self._tmp)
        self.addCleanup(os.environ.pop, "TOKENSAFE_DATA_DIR", None)
        self.addCleanup(os.environ.pop, "TOKENSAFE_DB_PATH", None)
        self.addCleanup(os.environ.pop, "TOKENSAFE_TELEMETRY_DISABLED", None)
        t = MetricsTracker()
        t.record_event("proxy_session", "CACHE_OBS", 0, 0,
                       details={"provider": "anthropic", "input": 1000, "cache_read": 9000,
                                "cache_write": 0, "mutations": []},
                       measurement="measured")
        t.record_event("proxy_session", "CACHE_OBS", 0, 0,
                       details={"provider": "anthropic", "input": 1000, "cache_read": 5000,
                                "cache_write": 0, "mutations": ["ledger"]},
                       measurement="measured")

    def test_terminal_panel_rendered_and_breakdown_filtered(self):
        from src.dashboard import Dashboard
        text = Dashboard(MetricsTracker()).render_terminal()
        self.assertIn("Cache Stability (proxy KV-cache)", text)
        self.assertIn("Clean requests", text)
        self.assertIn("Mutated (folded) requests", text)
        # Zero-savings observability event must NOT appear in the savings breakdown.
        self.assertEqual(text.count("Cache Stability (proxy)"), 0)

    def test_markdown_panel_rendered_and_breakdown_filtered(self):
        from src.dashboard import Dashboard
        md = Dashboard(MetricsTracker()).render_markdown()
        self.assertIn("### Cache Stability (proxy KV-cache)", md)
        self.assertNotIn("Cache Stability (proxy)", md.replace("Cache Stability (proxy KV-cache)", ""))


if __name__ == "__main__":
    unittest.main()
