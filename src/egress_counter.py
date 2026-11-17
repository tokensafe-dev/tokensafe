"""
TokenSafe Egress Counter: day-keyed outbound-request transparency ledger.

Tracks HTTP requests that carry user content or machine metadata off the
device (Cloud Prompt Booster distillation, telemetry sync) so
`tokensafe status` can show a verifiable "what left this machine today" line.
Deliberately local-only, dependency-free, and fail-open: a counter failure
must never break the network path it observes.

Storage: <data_dir>/egress.json — {"YYYY-MM-DD": {"booster": N, "telemetry": M}}
(only the trailing 14 days are kept to bound the file size).
"""
import datetime
import json
import os
import threading
from typing import Dict

from src.paths import resolve_data_dir

_LOCK = threading.Lock()
KINDS = ("booster", "telemetry")


def _egress_path() -> str:
    return os.path.join(resolve_data_dir(), "egress.json")


def _load(path: str) -> Dict[str, Dict[str, int]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def increment(kind: str) -> None:
    """Records one outbound request of `kind` for today (fail-open, never raises)."""
    if kind not in KINDS:
        return
    with _LOCK:
        try:
            path = _egress_path()
            day = datetime.date.today().isoformat()
            data = _load(path)
            day_row = data.get(day) or {}
            if not isinstance(day_row, dict):
                day_row = {}
            day_row[kind] = int(day_row.get(kind, 0)) + 1
            data[day] = day_row
            for stale_day in sorted(k for k in data if k != day)[:-14]:
                data.pop(stale_day, None)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, path)
        except Exception:
            pass


def today_counts() -> Dict[str, int]:
    """Returns {"booster": N, "telemetry": M} for today (zeros on any failure)."""
    try:
        day = datetime.date.today().isoformat()
        row = _load(_egress_path()).get(day) or {}
        if not isinstance(row, dict):
            row = {}
        return {kind: int(row.get(kind, 0)) for kind in KINDS}
    except Exception:
        return {kind: 0 for kind in KINDS}
