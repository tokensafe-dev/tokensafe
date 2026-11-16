"""Unit tests for Context Quality Score (Grade S/A/B/C/F) engine."""

import unittest
from unittest.mock import MagicMock, patch

from src.quality_score import ContextQualityScore


class TestContextQualityScore(unittest.TestCase):
    def test_grade_s_optimal(self):
        budget = {
            "used_pct": 5.0,
            "used_estimated": 10_000,
            "window_tokens": 200_000,
        }
        audit = {
            "total_static_tokens_per_turn": 1200,
            "bloated_servers": [],
            "ghost_tokens": {"has_ghost_tokens": False},
            "rules": {"budget_exceeded": False, "violations": []},
        }
        res = ContextQualityScore.calculate(budget=budget, audit=audit)
        self.assertEqual(res["grade"], "S")
        self.assertEqual(res["label"], "Optimal")
        self.assertGreaterEqual(res["score"], 90)
        self.assertEqual(res["headroom_pct"], 95.0)
        self.assertIn("Grade: S (Optimal, 95% headroom", res["summary"])

    def test_grade_a_healthy(self):
        budget = {
            "used_pct": 20.0,
            "used_estimated": 40_000,
            "window_tokens": 200_000,
        }
        audit = {
            "total_static_tokens_per_turn": 3500,
            "bloated_servers": [],
            "ghost_tokens": {"has_ghost_tokens": False},
            "rules": {"budget_exceeded": False, "violations": []},
        }
        res = ContextQualityScore.calculate(budget=budget, audit=audit)
        self.assertEqual(res["grade"], "A")
        self.assertEqual(res["label"], "Healthy")
        self.assertTrue(80 <= res["score"] < 90)
        self.assertEqual(res["headroom_pct"], 80.0)

    def test_grade_b_moderate(self):
        budget = {
            "used_pct": 40.0,
            "used_estimated": 80_000,
            "window_tokens": 200_000,
        }
        audit = {
            "total_static_tokens_per_turn": 5000,
            "bloated_servers": [],
            "ghost_tokens": {"has_ghost_tokens": False},
        }
        res = ContextQualityScore.calculate(budget=budget, audit=audit)
        self.assertEqual(res["grade"], "B")
        self.assertEqual(res["label"], "Moderate")
        self.assertTrue(60 <= res["score"] < 75)

    def test_grade_c_constrained(self):
        budget = {
            "used_pct": 55.0,
            "used_estimated": 110_000,
            "window_tokens": 200_000,
        }
        audit = {
            "total_static_tokens_per_turn": 12_000,
            "bloated_servers": [],
            "ghost_tokens": {"has_ghost_tokens": True},
        }
        res = ContextQualityScore.calculate(budget=budget, audit=audit)
        self.assertEqual(res["grade"], "C")
        self.assertEqual(res["label"], "Constrained")
        self.assertTrue(40 <= res["score"] < 60)
        self.assertTrue(res["has_ghost_tokens"])
        self.assertGreater(len(res["deductions"]), 0)

    def test_grade_f_critical(self):
        budget = {
            "used_pct": 96.0,
            "used_estimated": 192_000,
            "window_tokens": 200_000,
        }
        audit = {
            "total_static_tokens_per_turn": 25_000,
            "bloated_servers": [
                {"name": "heavy_1", "tool_count": 60},
                {"name": "heavy_2", "tool_count": 80},
            ],
            "ghost_tokens": {"has_ghost_tokens": True},
            "rules": {"budget_exceeded": True},
        }
        res = ContextQualityScore.calculate(budget=budget, audit=audit)
        self.assertEqual(res["grade"], "F")
        self.assertEqual(res["label"], "Critical")
        self.assertLess(res["score"], 40)

    def test_competitor_conflicts_deduction(self):
        budget = {"used_pct": 10.0, "window_tokens": 200_000}
        audit = {"total_static_tokens_per_turn": 1000}
        conflicts = [{"name": "chisle", "class": "stdio proxy", "traces": []}]
        res = ContextQualityScore.calculate(budget=budget, audit=audit, conflicts=conflicts)
        self.assertIn("Conflicting / orphan tool hooks detected (-3 pts)", res["deductions"])

    @patch("src.governor.ContextBudget.estimate")
    @patch("src.environment_auditor.EnvironmentAuditor.audit")
    @patch("src.competitors.CompetitorAuditor.audit")
    def test_evaluate_integration(self, mock_comp_audit, mock_env_audit, mock_budget_est):
        mock_budget_est.return_value = {"used_pct": 15.0, "used_estimated": 30000, "window_tokens": 200000}
        mock_env_audit.return_value = {"total_static_tokens_per_turn": 2500, "bloated_servers": []}
        mock_comp_audit.return_value = []

        res = ContextQualityScore.evaluate()
        self.assertIn(res["grade"], ("S", "A"))
        self.assertEqual(res["headroom_pct"], 85.0)


if __name__ == "__main__":
    unittest.main()
