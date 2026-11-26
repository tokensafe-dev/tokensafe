"""Canonical savings report for agents and hooks.

Agents must never quote savings from a guess or from a stray database file: the
one source of truth is the canonical data dir resolved by ``src.paths``
(``TOKENSAFE_DATA_DIR`` or ``~/.tokensafe/data``). This module exposes it as a
single JSON-able report, including the difference between:
  * live events still in ``events`` / ``lifetime_aggregates``, and
  * totals already pruned from the local DB but counted in the cloud mirror
    (``pruned_synced_*``).

Repository-local ``data/metrics.db`` files are legacy/test artifacts and are NOT
read here.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, Optional

from src.metrics import MetricsTracker
from src.paths import resolve_data_dir


def _pruned_synced(db_path: str) -> Dict[str, int]:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT pruned_synced_orig_tokens, pruned_synced_saved_tokens FROM lifetime_aggregates WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return {"original_tokens": 0, "saved_tokens": 0}
        return {"original_tokens": int(row[0] or 0), "saved_tokens": int(row[1] or 0)}
    except Exception:
        return {"original_tokens": 0, "saved_tokens": 0}


def build_savings_report(tracker: Optional[MetricsTracker] = None) -> Dict[str, Any]:
    """Returns canonical totals, last event, and the cumulative explanation."""
    tracker = tracker or MetricsTracker()
    summary = tracker.get_summary()
    last_events = tracker.get_recent_events(limit=1)

    live_saved = int(summary.get("total_saved_tokens", 0))
    pruned = _pruned_synced(tracker.db_path)
    cumulative = live_saved + pruned["saved_tokens"]

    last_event = None
    if last_events:
        event = last_events[0]
        last_event = {
            "timestamp": event.get("timestamp"),
            "event_type": event.get("event_type"),
            "tool": event.get("event_type"),
            "session_id": event.get("session_id"),
            "saved_tokens": int(event.get("saved_tokens") or 0),
            "original_tokens": int(event.get("original_tokens") or 0),
            "optimized_tokens": int(event.get("optimized_tokens") or 0),
        }

    return {
        "source": "canonical",
        "data_dir": resolve_data_dir(),
        "db_path": tracker.db_path,
        "total_events": int(summary.get("total_events", 0)),
        "sessions": int(summary.get("session_count", 0)),
        "saved_tokens_live": live_saved,
        "saved_tokens_measured": int(summary.get("total_saved_tokens_measured", 0)),
        "saved_tokens_estimated": int(summary.get("total_saved_tokens_estimated", 0)),
        "pruned_synced_saved_tokens": pruned["saved_tokens"],
        "cumulative_saved_tokens": cumulative,
        "usd_saved_approx": summary.get("total_usd_saved", 0.0),
        "reduction_pct": summary.get("overall_reduction_pct", 0.0),
        "last_event": last_event,
        "note": (
            "cumulative = live events + totals pruned locally but already synced; "
            "per-turn delta must be read from last_event, never guessed"
        ),
    }
