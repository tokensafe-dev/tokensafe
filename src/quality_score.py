"""
Context Quality Score Engine for TokenSafe.
Synthesizes dynamic context window headroom with static environment overhead
(MCP tools, installed skills, rule file limits, ghost tokens, and competitor conflicts)
into a unified Letter Grade (S / A / B / C / F) and visual health indicator.
"""

import os
from typing import Dict, Any, Optional, List

class ContextQualityScore:
    """
    Evaluates and grades context health (S / A / B / C / F).

    Grading Rubric:
    - Dynamic Headroom (60 pts max):
        headroom = max(0, 100 - used_pct)
        score = (headroom / 100) * 60
    - Static Footprint (25 pts max):
        < 2,000 static tokens/turn   -> 25 pts (ideal / lean)
        2,000 - 5,000 tokens/turn    -> 20 pts (standard)
        5,000 - 10,000 tokens/turn   -> 12 pts (moderate)
        10,000 - 20,000 tokens/turn  -> 5 pts (heavy)
        > 20,000 tokens/turn         -> 0 pts (bloated)
    - Hygiene & Deductions (15 pts base):
        -5 per bloated MCP server (tool_count >= 40)
        -6 if ghost tokens detected (> 2,000 tokens wasted in configs/rules)
        -5 if rule files exceed limit (> 24KB file or > 20,000 token budget)
        -4 if competitor / stale conflicts detected
        clamped to [0, 15]

    Grades:
    - S (90 - 100): Optimal (Pristine context, maximal headroom > 90%)
    - A (80 - 89):  Healthy (Ample headroom > 75%, lean static overhead)
    - B (65 - 79):  Moderate (Moderate load, surgical reads advised)
    - C (45 - 64):  Constrained (High consumption or heavy static tax, compaction advised)
    - F (0 - 44):   Critical (Window nearly exhausted or critical bloat, compaction required)
    """

    @classmethod
    def calculate(
        cls,
        budget: Dict[str, Any],
        audit: Optional[Dict[str, Any]] = None,
        conflicts: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """Calculates letter grade and detailed health metrics from budget and audit data."""
        # 1. Dynamic Headroom (0 to 70 pts)
        used_pct = float(budget.get("used_pct", 0.0) or 0.0)
        used_estimated = int(budget.get("used_estimated", 0) or 0)
        window_tokens = int(budget.get("window_tokens", 200_000) or 200_000)
        headroom_pct = max(0.0, round(100.0 - used_pct, 1))

        headroom_score = (headroom_pct / 100.0) * 70.0

        # 2. Static Context Tax (0 to 20 pts)
        audit = audit or {}
        static_tokens = int(audit.get("total_static_tokens_per_turn", 0) or 0)
        if static_tokens < 2000:
            static_score = 20.0
        elif static_tokens < 5000:
            static_score = 16.0
        elif static_tokens < 10000:
            static_score = 12.0
        elif static_tokens < 20000:
            static_score = 6.0
        else:
            static_score = 0.0

        # 3. Hygiene & Deductions (0 to 10 pts)
        hygiene_base = 10.0
        deductions = []

        bloated_servers = audit.get("bloated_servers") or []
        if bloated_servers:
            penalty = len(bloated_servers) * 4.0
            hygiene_base -= penalty
            deductions.append(f"{len(bloated_servers)} bloated MCP server(s) (-{int(penalty)} pts)")

        ghost = audit.get("ghost_tokens") or {}
        if ghost.get("has_ghost_tokens"):
            hygiene_base -= 4.0
            deductions.append("Ghost tokens in configs/rules (-4 pts)")

        rules = audit.get("rules") or {}
        if rules.get("budget_exceeded") or rules.get("violations"):
            hygiene_base -= 3.0
            deductions.append("Rule file budget or size limit exceeded (-3 pts)")

        if conflicts:
            hygiene_base -= 3.0
            deductions.append("Conflicting / orphan tool hooks detected (-3 pts)")

        hygiene_score = max(0.0, min(10.0, hygiene_base))

        total_score = max(0, min(100, int(round(headroom_score + static_score + hygiene_score))))

        # Determine Letter Grade & Label
        if total_score >= 88:
            grade = "S"
            label = "Optimal"
            desc = "Pristine context, maximal headroom (>90%)"
        elif total_score >= 75:
            grade = "A"
            label = "Healthy"
            desc = "Healthy context, ample headroom"
        elif total_score >= 60:
            grade = "B"
            label = "Moderate"
            desc = "Moderate load, surgical reads advised"
        elif total_score >= 40:
            grade = "C"
            label = "Constrained"
            desc = "Constrained context, compaction recommended"
        else:
            grade = "F"
            label = "Critical"
            desc = "Critical exhaustion or severe bloat, immediate compaction required"

        tax_str = f"~{static_tokens:,} tok/turn" if static_tokens else "0 tok/turn"
        summary = f"Grade: {grade} ({label}, {headroom_pct:.0f}% headroom · static tax: {tax_str})"

        return {
            "grade": grade,
            "score": total_score,
            "label": label,
            "descriptor": desc,
            "headroom_pct": headroom_pct,
            "used_pct": used_pct,
            "used_estimated": used_estimated,
            "window_tokens": window_tokens,
            "static_tax_tokens": static_tokens,
            "has_mcp_bloat": bool(bloated_servers),
            "has_ghost_tokens": bool(ghost.get("has_ghost_tokens")),
            "deductions": deductions,
            "summary": summary,
        }

    @classmethod
    def evaluate(
        cls,
        tracker: Optional[Any] = None,
        session_id: str = "mcp_session",
        model: str = "",
        window_tokens: Optional[int] = None,
        workspace_root: Optional[str] = None
    ) -> Dict[str, Any]:
        """Convenience evaluator that fetches current budget, environment audit, and conflicts."""
        from src.governor import ContextBudget
        from src.environment_auditor import EnvironmentAuditor
        from src.competitors import CompetitorAuditor

        if tracker is None:
            from src.metrics import MetricsTracker
            tracker = MetricsTracker()

        budget = ContextBudget.estimate(
            tracker, session_id=session_id, model=model, window_tokens=window_tokens
        )
        try:
            audit = EnvironmentAuditor.audit(workspace_root)
        except Exception:
            audit = {}

        try:
            conflicts = CompetitorAuditor.audit()
        except Exception:
            conflicts = []

        return cls.calculate(budget=budget, audit=audit, conflicts=conflicts)
