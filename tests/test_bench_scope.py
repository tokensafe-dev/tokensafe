"""Prevention for -32011 target_not_unique: deterministic auto-scoping.

The measured matrix v5 left one outlier: the model omitted startLine/endLine, the
runtime rejected the hunk, and the model paid a re-read before retrying. These
tests pin the two halves of the fix — the pure decision (never guess between two
candidates) and the one-retry wrapper that uses the window the agent just read.
"""
import os
import unittest
from pathlib import Path

from src.bench.go_target import GoWorkspace, choose_scoped_hunk

APP = "benchmark/sandboxes/vanilla_gin/app.go"


def ambiguity(lines, hunk=0, reason="target_not_unique"):
    return {"reason": reason, "hunk": hunk, "count": len(lines), "lines": list(lines)}


class ChooseScopedHunkTest(unittest.TestCase):
    def test_scopes_to_the_single_occurrence_inside_the_read_window(self):
        hunks = [{"target": "return nil, err", "replacement": "return nil, err"}]
        chosen = choose_scoped_hunk(hunks, ambiguity([12, 40]), (10, 20))

        self.assertIsNotNone(chosen)
        scoped, scope = chosen
        self.assertEqual(scope, {"hunk": 0, "startLine": 10, "endLine": 14})
        self.assertEqual(scoped[0]["startLine"], 10)
        self.assertEqual(scoped[0]["endLine"], 14)
        # The original list is not mutated: a retry must never leak into the error path.
        self.assertNotIn("startLine", hunks[0])

    def test_refuses_to_guess_between_two_occurrences_in_the_window(self):
        hunks = [{"target": "return nil, err", "replacement": "return nil, err"}]
        self.assertIsNone(choose_scoped_hunk(hunks, ambiguity([12, 14]), (10, 20)))

    def test_refuses_when_no_occurrence_is_in_the_window(self):
        hunks = [{"target": "return nil, err", "replacement": "return nil, err"}]
        self.assertIsNone(choose_scoped_hunk(hunks, ambiguity([40, 44]), (10, 20)))

    def test_refuses_without_a_window_and_for_other_error_reasons(self):
        hunks = [{"target": "x", "replacement": "y"}]
        self.assertIsNone(choose_scoped_hunk(hunks, ambiguity([12]), None))
        self.assertIsNone(choose_scoped_hunk(hunks, ambiguity([12], reason="target_not_found"), (10, 20)))
        self.assertIsNone(choose_scoped_hunk([], ambiguity([12]), (10, 20)))

    def test_second_hunk_stays_unscoped_and_the_first_is_preserved(self):
        hunks = [
            {"target": "a", "replacement": "b"},
            {"target": "return nil, err", "replacement": "return nil, fmt.Errorf(\"x: %w\", err)"},
        ]
        chosen = choose_scoped_hunk(hunks, ambiguity([31], hunk=1), (30, 40))

        self.assertIsNotNone(chosen)
        scoped, _ = chosen
        self.assertNotIn("startLine", scoped[0])
        self.assertEqual(scoped[1]["startLine"], 30)
        self.assertEqual(scoped[1]["endLine"], 33)

    def test_multi_line_target_gets_a_window_that_covers_it(self):
        target = "if err != nil {\n\treturn nil, err\n}"
        hunks = [{"target": target, "replacement": "if err != nil {\n\treturn nil, fmt.Errorf(\"wrapped: %w\", err)\n}"}]
        chosen = choose_scoped_hunk(hunks, ambiguity([50]), (45, 60))

        self.assertIsNotNone(chosen)
        _, scope = chosen
        # Three-line target plus the two guard lines on each side.
        self.assertEqual(scope["startLine"], 48)
        self.assertEqual(scope["endLine"], 54)


class ApplyEditScopedTest(unittest.TestCase):
    def make_workspace(self, window):
        ws = object.__new__(GoWorkspace)
        ws.dir = Path("/tmp/oumv-bench-test/go")
        ws._read_windows = {}
        if window is not None:
            ws._read_windows[ws._window_key(APP)] = window
        return ws

    def test_one_deterministic_retry_returns_the_edit_with_auto_scoped_note(self):
        ws = self.make_workspace((10, 20))
        calls = []

        def fake_oumv(method, params):
            calls.append((method, params))
            if len(calls) == 1:
                return {
                    "error": {
                        "code": -32011,
                        "message": "VALIDATION_FAILED: hunk 0 target occurs 2 times",
                        "data": ambiguity([12, 40]),
                    }
                }
            return {"status": "APPLIED", "hunksApplied": 1, "path": APP}

        ws.oumv = fake_oumv
        result = ws.apply_edit_scoped(
            APP,
            "sha256:abc",
            mode="patch",
            hunks=[{"target": "return nil, err", "replacement": "return nil, fmt.Errorf(\"x: %w\", err)"}],
        )

        self.assertEqual(len(calls), 2, "exactly one retry, never a loop")
        self.assertEqual(result["status"], "APPLIED")
        self.assertEqual(result["autoScoped"]["startLine"], 10)
        self.assertEqual(result["autoScoped"]["endLine"], 14)
        self.assertEqual(calls[1][1]["hunks"][0]["startLine"], 10)
        self.assertEqual(calls[1][1]["expectedVersion"], "sha256:abc")

    def test_two_candidates_inside_the_window_go_back_to_the_model(self):
        ws = self.make_workspace((10, 20))
        calls = []

        def fake_oumv(method, params):
            calls.append((method, params))
            return {"error": {"code": -32011, "message": "...", "data": ambiguity([12, 14])}}

        ws.oumv = fake_oumv
        result = ws.apply_edit_scoped(
            APP,
            "sha256:abc",
            mode="patch",
            hunks=[{"target": "return nil, err", "replacement": "x"}],
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(result["error"]["code"], -32011)
        self.assertEqual(result["error"]["data"]["lines"], [12, 14])

    def test_other_error_codes_are_not_retried(self):
        ws = self.make_workspace((10, 20))
        calls = []

        def fake_oumv(method, params):
            calls.append((method, params))
            return {"error": {"code": -32001, "message": "CONFLICT_DETECTED: stale version"}}

        ws.oumv = fake_oumv
        result = ws.apply_edit_scoped(APP, "sha256:old", mode="patch", hunks=[{"target": "x", "replacement": "y"}])

        self.assertEqual(len(calls), 1)
        self.assertEqual(result["error"]["code"], -32001)

    def test_read_windows_are_remembered_from_l3_and_get_source(self):
        ws = self.make_workspace(None)
        ws._remember_read_window(
            "oumv/observe",
            {"level": "L3", "data": {"path": APP, "source": "package main", "startLine": 5, "endLine": 25}},
        )
        ws._remember_read_window(
            "oumv/getSource", {"path": APP, "source": "x", "startLine": 30, "endLine": 33}
        )
        # getSource after observe is the freshest thing the agent read.
        self.assertEqual(ws._read_windows[ws._window_key(APP)], (30, 33))
        # A result without a source body is not a read and must not move the window.
        ws._remember_read_window("oumv/observe", {"level": "L2", "data": {"path": APP, "startLine": 1, "endLine": 2}})
        self.assertEqual(ws._read_windows[ws._window_key(APP)], (30, 33))


if __name__ == "__main__":
    unittest.main()
