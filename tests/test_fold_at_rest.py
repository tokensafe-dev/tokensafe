"""Fold-at-rest: the CC Stop hook prunes superseded reads from the idle
transcript (opt-in TOKENSAFE_FOLD_AT_REST=1, structure-preserving tombstones,
one-file .tokensafe.bak backup)."""
import json
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


def _big_read_payload(target_kb: int = 130) -> str:
    chunk = "def fn_%d():\n    return validate_and_transform(payload_%d, mode='x')\n"
    lines = []
    size = 0
    i = 0
    while size < target_kb * 1024:
        lines.append(chunk % (i, i))
        size += len(chunk % (i, i))
        i += 1
    return "".join(lines)


def _build_cc_transcript(path: str, big_text: str) -> None:
    lines = [
        {"type": "user", "message": {"role": "user", "content": "please inspect the module"}},
        {"parentUuid": None, "uuid": "u1", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Read",
             "input": {"file_path": "/app/big.py"}},
        ]}},
        {"parentUuid": "u1", "uuid": "u2", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": big_text},
        ]}},
        {"parentUuid": "u2", "uuid": "u3", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_2", "name": "Edit",
             "input": {"file_path": "/app/big.py", "old_string": "a", "new_string": "b"}},
        ]}},
        {"parentUuid": "u3", "uuid": "u4", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_2", "content": "ok"},
        ]}},
        {"parentUuid": "u4", "uuid": "u5", "message": {"role": "assistant", "content": "done"}},
    ]
    with open(path, "w", encoding="utf-8") as f:
        for ln in lines:
            f.write(json.dumps(ln, ensure_ascii=False) + "\n")


class TestFoldAtRest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="tokensafe_rest_")
        _fresh_env(self._tmp)
        self.addCleanup(os.environ.pop, "TOKENSAFE_DATA_DIR", None)
        self.addCleanup(os.environ.pop, "TOKENSAFE_DB_PATH", None)
        self.addCleanup(os.environ.pop, "TOKENSAFE_TELEMETRY_DISABLED", None)
        self.addCleanup(os.environ.pop, "TOKENSAFE_FOLD_AT_REST", None)

    def _transcript(self) -> str:
        tp = os.path.join(self._tmp, "session.jsonl")
        _build_cc_transcript(tp, _big_read_payload())
        return tp

    def test_flag_off_leaves_transcript_untouched(self):
        from hooks import claude_code_hook
        tp = self._transcript()
        before = open(tp, "r", encoding="utf-8").read()
        res = claude_code_hook.handle("Stop", {"session_id": "rest_off", "transcript_path": tp})
        self.assertEqual(res, {})
        self.assertEqual(open(tp, "r", encoding="utf-8").read(), before)

    def test_flag_on_prunes_superseded_read(self):
        from hooks import claude_code_hook
        os.environ["TOKENSAFE_FOLD_AT_REST"] = "1"
        tp = self._transcript()
        res = claude_code_hook.handle(
            "Stop", {"session_id": "rest_on", "transcript_path": tp}
        )
        self.assertEqual(res, {})
        pruned = open(tp, "r", encoding="utf-8").read()
        self.assertIn("[TokenSafe: Output superseded", pruned)
        # Structure preservation: same line count, UUID chain intact.
        self.assertEqual(len(pruned.strip().splitlines()), 6)
        self.assertIn('"uuid": "u5"', pruned)
        self.assertNotIn("def fn_0():", pruned)
        # One-file backup of the pre-fold bytes.
        self.assertTrue(os.path.exists(tp + ".tokensafe.bak"))
        tracker = MetricsTracker(db_path=os.path.join(self._tmp, "metrics.db"))
        events = [e for e in tracker.get_recent_events(limit=20) if e.get("event_type") == "FOLD_AT_REST"]
        self.assertTrue(events, "FOLD_AT_REST event not recorded")

    def test_idempotent_second_run_is_noop(self):
        from hooks import claude_code_hook
        os.environ["TOKENSAFE_FOLD_AT_REST"] = "1"
        tp = self._transcript()
        claude_code_hook.handle("Stop", {"session_id": "rest_idem_1", "transcript_path": tp})
        after_first = open(tp, "r", encoding="utf-8").read()
        claude_code_hook.handle("Stop", {"session_id": "rest_idem_2", "transcript_path": tp})
        self.assertEqual(open(tp, "r", encoding="utf-8").read(), after_first)

    def test_small_transcript_skipped(self):
        from hooks import claude_code_hook
        from src.superseded import SupersededPruner
        tp = os.path.join(self._tmp, "small.jsonl")
        _build_cc_transcript(tp, "tiny read result")
        os.environ["TOKENSAFE_FOLD_AT_REST"] = "1"
        res = SupersededPruner.fold_at_rest(tp)
        self.assertFalse(res.get("success"))
        self.assertIn("threshold", res.get("skipped", ""))


if __name__ == "__main__":
    unittest.main()
