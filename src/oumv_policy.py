"""Per-tier OUMV capability policy.

Observation is free on every plan. Changing or verifying a running app
(``mutate``, ``preview``, ``verify``, ``build``) needs a paid tier, because those
are the runtime's write path: the adapters implement the read-only deployment
profile by not registering the methods at all, so a read-only runtime answers
``-32601`` before session validation and advertises ``capabilities.readOnly`` in
the handshake (spec §8.5).

Precedence:

1. ``TOKENSAFE_OUMV_READ_ONLY`` truthy -> read-only (operator switch, always wins)
2. a signed license payload key ``oumv_profile`` (``read_only`` / ``full``)
3. the tier map: guest/free -> read-only, solo/team/enterprise -> full

The tier comes from an Ed25519-signed payload, so the map is policy, not an
entitlement check: it cannot be edited into a paid tier. A future server-signed
``oumv_profile`` needs no engine change because ``validate_license`` returns the
signed payload unfiltered.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

PROFILE_READ_ONLY = "read_only"
PROFILE_FULL = "full"

FULL_TIERS = frozenset({"solo", "team", "enterprise"})

# Tools whose adapters are absent in the read-only deployment profile. Mirrors
# READ_ONLY_ABSENT_METHODS in tokensafe-backend (python/.../core/runtime.py).
WRITE_TOOLS = frozenset({
    "oumv_mutate",
    "oumv_preview",
    "oumv_verify",
    "oumv_build",
})

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_READ_ONLY_ALIASES = frozenset({"read_only", "read-only", "readonly"})


def _env_forces_read_only() -> bool:
    raw = (os.environ.get("TOKENSAFE_OUMV_READ_ONLY") or "").strip().lower()
    return raw in _TRUTHY


def _signed_profile(payload: Optional[Dict[str, Any]]) -> Optional[str]:
    raw = str((payload or {}).get("oumv_profile") or "").strip().lower()
    if raw in _READ_ONLY_ALIASES:
        return PROFILE_READ_ONLY
    if raw == PROFILE_FULL:
        return PROFILE_FULL
    return None


def profile_for(tier: Optional[str] = None, payload: Optional[Dict[str, Any]] = None) -> str:
    """Resolves the OUMV profile for a tier (and an optional signed payload)."""
    if _env_forces_read_only():
        return PROFILE_READ_ONLY
    signed = _signed_profile(payload)
    if signed is not None:
        return signed
    if str(tier or "").strip().lower() in FULL_TIERS:
        return PROFILE_FULL
    return PROFILE_READ_ONLY


def current() -> Tuple[str, str]:
    """(profile, tier) for the active license. License reads are cached upstream."""
    from src.license_manager import LicenseManager

    try:
        tier, payload = LicenseManager.get_active_license()
    except Exception:
        return PROFILE_READ_ONLY, "guest"
    return profile_for(tier, payload), str(tier or "guest").strip().lower() or "guest"


def is_read_only(tier: Optional[str] = None, payload: Optional[Dict[str, Any]] = None) -> bool:
    if tier is None and payload is None:
        return current()[0] == PROFILE_READ_ONLY
    return profile_for(tier, payload) == PROFILE_READ_ONLY


def spawn_env(env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Environment for a spawned runtime: read-only flags only when licensed so.

    Node/Go/.NET and the generated Python sitecustomize read
    ``TOKENSAFE_OUMV_READ_ONLY``; the PHP sidecar reads ``OUMV_READ_ONLY``. Java
    takes the CLI flag instead (see ``sidecar_run_command``).
    """
    base = dict(os.environ if env is None else env)
    if current()[0] == PROFILE_READ_ONLY:
        base["TOKENSAFE_OUMV_READ_ONLY"] = "1"
        base["OUMV_READ_ONLY"] = "1"
    return base


def deny_text(target: str) -> str:
    """Refusal that mirrors the adapter's absent method instead of a quota banner."""
    action = str(target or "").strip()
    if action.startswith("oumv_"):
        action = action[len("oumv_"):]
    action = action or "mutate"
    return (
        f"'{action}' is a write-profile method and this license is read-only (free plan). "
        "Observation stays free: use status, observe, understand, graph or index. "
        "Change and verify the running app on Solo or higher "
        "(https://tokensafe.dev/pricing). Restart the dev server after upgrading so "
        "the runtime re-registers the write methods."
    )
