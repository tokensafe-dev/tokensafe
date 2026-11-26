"""Provider price table for the agent loop.

Prices are a pinned snapshot, not a live feed: the loop reports a USD figure
only for models it can price, and marks everything else ``priced: false``
instead of inventing a number. Override with ``TOKENSAFE_AGENT_PRICES_JSON``:

    {"my-model": {"input_miss": 0.5, "input_hit": 0.1, "output": 2.0}}

Values are USD per 1M tokens, matching how providers publish them.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

PRICE_SNAPSHOT = "2026-11-26"

# Only models whose published prices were verified for this snapshot live here.
# Adding a guess would make the spend report dishonest, which is worse than an
# unlabelled zero.
DEFAULT_PRICES: Dict[str, Dict[str, float]] = {
    "deepseek-chat": {"input_miss": 0.27, "input_hit": 0.07, "output": 1.10},
    "deepseek-flash": {"input_miss": 0.27, "input_hit": 0.07, "output": 1.10},
}


def _overrides() -> Dict[str, Dict[str, float]]:
    raw = os.environ.get("TOKENSAFE_AGENT_PRICES_JSON", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    clean: Dict[str, Dict[str, float]] = {}
    for model, price in parsed.items():
        if isinstance(price, dict):
            try:
                clean[str(model)] = {
                    "input_miss": float(price.get("input_miss", 0.0)),
                    "input_hit": float(price.get("input_hit", 0.0)),
                    "output": float(price.get("output", 0.0)),
                }
            except (TypeError, ValueError):
                continue
    return clean


def price_for(model: str) -> Optional[Dict[str, float]]:
    key = (model or "").strip().lower()
    if not key:
        return None
    table = dict(DEFAULT_PRICES)
    table.update(_overrides())
    return table.get(key)


def usd(
    model: str,
    input_miss_tokens: int,
    cache_hit_tokens: int,
    output_tokens: int,
) -> Dict[str, Any]:
    """Cost for one usage bucket. ``priced`` is False when the model is unknown."""
    price = price_for(model)
    if not price:
        return {"usd": 0.0, "priced": False, "price_snapshot": PRICE_SNAPSHOT}
    cost = (
        max(0, int(input_miss_tokens)) / 1_000_000 * price["input_miss"]
        + max(0, int(cache_hit_tokens)) / 1_000_000 * price["input_hit"]
        + max(0, int(output_tokens)) / 1_000_000 * price["output"]
    )
    return {"usd": round(cost, 6), "priced": True, "price_snapshot": PRICE_SNAPSHOT}
