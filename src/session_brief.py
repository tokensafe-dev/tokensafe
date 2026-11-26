"""Per-workspace routing brief injected into the agent's context.

The brief is the single source of truth for "what do I work through here?":

* OUMV-supported stacks (React/Vue + Vite, FastAPI, Go/Gin|Chi) get the exact
  activation command and the ``oumv`` tool verbs to use;
* unsupported stacks (Node/Express, Django, ...) are named so the agent falls back
  to the TokenSafe tools instead of wasting turns probing ``oumv``;
* everything is delivered through hooks / MCP instructions — nothing is written
  into the repository.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

MAX_BRIEF_CHARS = 1200

_FRONTEND_LINES = {
    "react": (
        "frontend {provider}/{framework}: OUMV live via `tokensafe dev` "
        "(zero repo footprint, L0-L4, OCC edits). Call tokensafe:oumv(action='observe', level='L1') "
        "before reading files; run `tokensafe dev` if no runtime is live."
    ),
    "vue": (
        "frontend {provider}/{framework}: no OUMV adapter yet (the Vite plugin serves no "
        "OUMV endpoints for Vue). Use tokensafe:repo_map, smart_read_file and scoped_search; "
        "verify UI changes with the app's own tests."
    ),
}

_BACKEND_LINES = {
    "fastapi": (
        "backend fastapi/fastapi: OUMV auto-mounts when the dev server runs through "
        "`tokensafe run-backend uvicorn <module:app> --reload` (RPC at /__oumv/rpc). "
        "Prefer tokensafe:oumv(action='observe'|'understand'|'mutate'|'verify') over grep/curl."
    ),
    "go": (
        "backend {provider}/{framework}: OUMV mounts with one AutoMountAndServe line "
        "(discovery included; `tokensafe inject --status` shows it, `--revert` removes it). "
        "Use tokensafe:oumv for endpoints/source/impact, oumv_verify for in-process checks."
    ),
    "node": (
        "backend node/{framework}: install `tokensafe-backend-node` and mount it "
        "(`mountTokensafe(app)` for Express, `registerTokensafe(app)` for Fastify); "
        "then use tokensafe:oumv for endpoints/impact and oumv_verify for live checks."
    ),
}


def _read_permissions(workspace: str) -> Dict[str, Any]:
    path = os.path.join(workspace, ".tokensafe", "oumv.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("permissions", {}) or {}
    except Exception:
        return {}


def _activation(workspace: str, stack: Dict[str, Any]) -> List[str]:
    """Exact next command(s) when an adapter is missing — no guessing."""
    hints: List[str] = []
    try:
        from src.retrofit import run_doctor
        doctor = run_doctor(workspace)
    except Exception:
        return hints

    readiness = doctor.get("adapter_readiness") or {}
    for profile, item in readiness.items():
        if item.get("ready"):
            continue
        if profile == "frontend":
            hints.append("activate frontend: run `tokensafe dev` (installs nothing into the repo)")
        elif item.get("provider") == "fastapi":
            hints.append('activate backend: `pip install "tokensafe-backend-python @ git+https://github.com/tokensafe-dev/tokensafe-backend.git#subdirectory=python"` then run the server via `tokensafe run-backend ...`')
        elif item.get("provider") == "go":
            hints.append(
                "activate backend: `go get github.com/tokensafe-dev/tokensafe-backend/go@v0.4.3` "
                "then `tokensafe inject` (one line, reversible)"
            )
    if not hints and not readiness:
        hints.append("run `tokensafe inject --status` for the exact wiring plan")
    return hints


def build_workspace_brief(
    workspace: Optional[str] = None,
    include_live: bool = False,
    max_chars: int = MAX_BRIEF_CHARS,
) -> str:
    """Builds the compact routing brief. Returns "" for non-project directories."""
    if not workspace or not os.path.isdir(workspace):
        return ""
    try:
        from src.paths import is_project_root
        if not is_project_root(workspace):
            return ""
    except Exception:
        pass

    try:
        from src.retrofit import detect_project_stack
        stack = detect_project_stack(workspace)
    except Exception:
        return ""

    frontend = stack.get("frontend") or {}
    backend = stack.get("backend") or {}
    if not frontend and not backend:
        return ""

    lines: List[str] = ["[TokenSafe OUMV routing brief]"]
    for entry in (frontend, backend):
        if not entry:
            continue
        provider = str(entry.get("provider") or "")
        framework = str(entry.get("framework") or "")
        template = _FRONTEND_LINES.get(provider) if entry is frontend else _BACKEND_LINES.get(provider)
        if template:
            lines.append("- " + template.format(provider=provider, framework=framework))
        else:
            lines.append(
                f"- {provider}/{framework}: OUMV has no adapter yet — use "
                "tokensafe:repo_map / scoped_search / smart_read_file instead of oumv."
            )

    permissions = _read_permissions(workspace)
    if permissions:
        mutate = permissions.get("mutate", True)
        lines.append(
            "- mutations: "
            + ("enabled with guardrails (OCC version, path jail, syntax pre-flight, backups)"
               if mutate else "disabled by .tokensafe/oumv.json (permissions.mutate=false)")
        )

    if include_live:
        try:
            from src.oumv_gateway import OUMVGateway
            status = OUMVGateway.status(workspace_dir=workspace)
            live = []
            for profile in ("frontend", "backend"):
                item = status.get(profile) or {}
                if item.get("url") or item.get("status") in ("OK", "LIVE"):
                    live.append(f"{profile}={item.get('url') or item.get('name', 'live')}")
            if live:
                lines.append("- live runtimes: " + ", ".join(live) + " — inspect/verify via OUMV first")
        except Exception:
            pass

    hints = _activation(workspace, stack)
    if hints:
        lines.append("- " + "; ".join(hints))

    brief = "\n".join(lines)
    if len(brief) > max_chars:
        brief = brief[: max_chars - 1].rstrip() + "…"
    return brief
