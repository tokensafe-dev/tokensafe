"""Aged thinking fold (proxy stage 1.6): DEFAULT OFF, opt-in via
TOKENSAFE_FOLD_THINKING=1; keeps the _tokensafe_thinking side-channel shape
valid for strict mirrors (the z.ai 1214 class)."""
import os
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src import metrics as metrics_mod
from src.metrics import MetricsTracker
from src.thinking_fold import THINKING_FOLD_MARKER, fold_aged_thinking


def _fresh_env(tmp: str):
    os.environ["TOKENSAFE_DATA_DIR"] = tmp
    os.environ["TOKENSAFE_DB_PATH"] = os.path.join(tmp, "metrics.db")
    os.environ["TOKENSAFE_TELEMETRY_DISABLED"] = "1"
    metrics_mod._INITIALIZED_METRICS_DBS.clear()


def _messages():
    big = "reasoning step about the dependency graph and its god nodes " * 40  # >2000 chars
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q0"},
        # Aged assistant turns — outside the last keep_recent=6 window.
        {"role": "assistant", "content": "a1",
         "_tokensafe_thinking": [{"type": "text", "text": big + " old-one"}]},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a2",
         "_tokensafe_thinking": [{"type": "text", "text": big + " old-two"}]},
        {"role": "user", "content": "q2"},
    ]
    # Filler turns push the aged assistants outside the last-6 window.
    for i in range(4):
        msgs.append({"role": "user", "content": f"filler {i}"})
        msgs.append({"role": "assistant", "content": f"filler answer {i}"})
    # Recent assistant turn (inside the last 6) — must stay verbatim.
    msgs.append({"role": "user", "content": "q-recent"})
    msgs.append({"role": "assistant", "content": "a-recent",
                 "_tokensafe_thinking": [{"type": "text", "text": big + " recent-turn"}]})
    return msgs


class TestThinkingFold(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="tokensafe_think_")
        _fresh_env(self._tmp)
        self.addCleanup(os.environ.pop, "TOKENSAFE_DATA_DIR", None)
        self.addCleanup(os.environ.pop, "TOKENSAFE_DB_PATH", None)
        self.addCleanup(os.environ.pop, "TOKENSAFE_TELEMETRY_DISABLED", None)
        self.addCleanup(os.environ.pop, "TOKENSAFE_FOLD_THINKING", None)

    def test_default_enabled(self):
        msgs = _messages()
        out, stats = fold_aged_thinking(msgs)
        self.assertEqual(stats["folded"], 2)
        self.assertIn(THINKING_FOLD_MARKER, out[2]["_tokensafe_thinking"][0]["text"])

    def test_flag_disabled_is_noop(self):
        os.environ["TOKENSAFE_FOLD_THINKING"] = "0"
        msgs = _messages()
        out, stats = fold_aged_thinking(msgs)
        self.assertEqual(stats["folded"], 0)
        self.assertEqual(out[2]["_tokensafe_thinking"][0]["text"], msgs[2]["_tokensafe_thinking"][0]["text"])

    def test_flag_on_folds_aged_only(self):
        os.environ["TOKENSAFE_FOLD_THINKING"] = "1"
        msgs = _messages()
        out, stats = fold_aged_thinking(msgs)
        self.assertEqual(stats["folded"], 2)
        self.assertGreater(stats["saved_tokens"], 0)
        stub = out[2]["_tokensafe_thinking"]
        self.assertIsInstance(stub, list)
        self.assertEqual(len(stub), 1)
        self.assertEqual(stub[0]["type"], "text")
        self.assertIn(THINKING_FOLD_MARKER, stub[0]["text"])
        self.assertIn(THINKING_FOLD_MARKER, out[4]["_tokensafe_thinking"][0]["text"])
        # Recent turn (inside the last keep_recent window) untouched.
        self.assertIn("recent-turn", out[-1]["_tokensafe_thinking"][0]["text"])

    def test_idempotent(self):
        os.environ["TOKENSAFE_FOLD_THINKING"] = "1"
        msgs = _messages()
        fold_aged_thinking(msgs)
        _, stats2 = fold_aged_thinking(msgs)
        self.assertEqual(stats2["folded"], 0)

    def test_event_recorded_when_folded(self):
        os.environ["TOKENSAFE_FOLD_THINKING"] = "1"
        fold_aged_thinking(_messages())
        tracker = MetricsTracker(db_path=os.path.join(self._tmp, "metrics.db"))
        events = [e for e in tracker.get_recent_events(limit=20) if e.get("event_type") == "THINKING_FOLD"]
        self.assertTrue(events, "THINKING_FOLD event not recorded")


if __name__ == "__main__":
    unittest.main()
