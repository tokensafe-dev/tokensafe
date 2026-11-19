"""Event model attribution: transcript sniff > proxy state > env > ''.

Hosts never pass the model to lifecycle hooks, so recorded events resolve the
model best-effort and fail open to an empty value — the landing feed renders
"—" instead of a fabricated name.
"""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from src.adapter_core import resolve_event_model


class TestEventModelAttribution(unittest.TestCase):
    def test_transcript_sniff_wins_over_env(self):
        with tempfile.TemporaryDirectory() as td:
            tp = os.path.join(td, "session.jsonl")
            with open(tp, "w", encoding="utf-8") as f:
                f.write(json.dumps({"message": {"model": "claude-opus-5-5"}}) + "\n")
            env = {"MODEL": "env-model", "AGY_MODEL": ""}
            with tempfile.TemporaryDirectory() as data_dir, \
                 mock.patch.dict(os.environ, env), \
                 mock.patch("src.paths.resolve_data_dir", return_value=data_dir):
                self.assertEqual(resolve_event_model(tp, "s1"), "claude-opus-5-5")

    def test_transcript_tail_only(self):
        with tempfile.TemporaryDirectory() as td:
            tp = os.path.join(td, "big.jsonl")
            with open(tp, "w", encoding="utf-8") as f:
                f.write(json.dumps({"message": {"model": "old-first-model"}}) + "\n")
                f.write("x" * 300_000 + "\n")
                f.write(json.dumps({"message": {"model": "latest-model"}}) + "\n")
            env = {"MODEL": "", "AGY_MODEL": ""}
            with tempfile.TemporaryDirectory() as data_dir, \
                 mock.patch.dict(os.environ, env), \
                 mock.patch("src.paths.resolve_data_dir", return_value=data_dir):
                self.assertEqual(resolve_event_model(tp, "s1"), "latest-model")

    def test_proxy_state_fallback(self):
        env = {"MODEL": "", "AGY_MODEL": ""}
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "proxy_state.json")
            with open(state, "w", encoding="utf-8") as f:
                json.dump({"model": "glm-5.3-flash", "ts": time.time()}, f)
            with mock.patch.dict(os.environ, env), \
                 mock.patch("src.paths.resolve_data_dir", return_value=td):
                self.assertEqual(resolve_event_model("", "s1"), "glm-5.3-flash")

    def test_stale_proxy_state_ignored(self):
        env = {"MODEL": "", "AGY_MODEL": ""}
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "proxy_state.json")
            with open(state, "w", encoding="utf-8") as f:
                json.dump({"model": "stale-model", "ts": time.time() - 3600}, f)
            with mock.patch.dict(os.environ, env), \
                 mock.patch("src.paths.resolve_data_dir", return_value=td):
                self.assertEqual(resolve_event_model("", "s1"), "")

    def test_empty_when_nothing_known(self):
        env = {"MODEL": "", "AGY_MODEL": ""}
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict(os.environ, env), \
             mock.patch("src.paths.resolve_data_dir", return_value=td):
            self.assertEqual(resolve_event_model("", "s1"), "")


if __name__ == "__main__":
    unittest.main()
