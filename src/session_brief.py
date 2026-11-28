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
        "frontend {provider}/{framework}: OUMV live via `tokensafe dev --framework vue` "
        "(zero repo footprint, L0-L4, OCC edits; certified L3 in a browser). Call "
        "tokensafe:oumv(action='observe', level='L1') before reading files; run "
        "`tokensafe dev --framework vue` if no runtime is live."
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
    "elysia": (
        "backend elysia/{framework} (Bun): install `tokensafe-backend-node` and add "
        "`app.use(require('tokensafe-backend-node/elysia').oumv())` before `app.listen()`; "
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


_PAYLOAD_ROOT_KEYS = (
    "workspacePaths",      # ZCode / Antigravity: every open folder, not just the first
    "workspace_roots",
    "workspaceRoots",
    "workspace_dirs",
    "additionalDirectories",
    "additional_directories",
    "roots",
)


def workspace_roots_from_payload(payload: Any) -> List[str]:
    """Every workspace root the host reported, primary first.

    Hosts that support several open folders (ZCode sends ``workspacePaths``)
    were previously reduced to ``workspacePaths[0]``; the remaining repos then
    looked like they did not exist, and the brief's single-root verdict read as
    a global "OUMV is unavailable here"."""
    roots: List[str] = []
    if not isinstance(payload, dict):
        return roots
    for key in _PAYLOAD_ROOT_KEYS:
        value = payload.get(key)
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            continue
        for item in value:
            if isinstance(item, str) and item.strip():
                roots.append(item.strip())
    return roots


def workspace_roots_from_env(env: Optional[Dict[str, str]] = None) -> List[str]:
    """Explicit extra roots for hosts that expose no multi-root payload."""
    raw = str((env or os.environ).get("TOKENSAFE_WORKSPACES") or "").strip()
    if not raw:
        return []
    return [p.strip() for p in raw.replace("\n", os.pathsep).split(os.pathsep) if p.strip()]


def _profile_supported(kind: str, entry: Dict[str, Any]) -> bool:
    """Adapter availability for one detected profile.

    ``detect_project_stack`` ships the authoritative boolean (``oumv_supported``);
    the template tables only cover a few providers and the Vue template is itself
    a negative line, so template presence alone must not imply "ready"."""
    flag = entry.get("oumv_supported")
    if isinstance(flag, bool):
        return flag
    table = _FRONTEND_LINES if kind == "frontend" else _BACKEND_LINES
    template = table.get(str(entry.get("provider") or ""))
    return bool(template) and "no OUMV adapter" not in template


def _stack_labels(stack: Dict[str, Any]) -> List[tuple]:
    """(kind, provider, framework, supported, adapter) per detected profile.

    A routing template decides when one exists (it says how to mount, or says
    there is no adapter); the detection flag only fills the gap for providers
    the templates do not cover yet (e.g. Solid)."""
    labels: List[tuple] = []
    for kind, table in (("frontend", _FRONTEND_LINES), ("backend", _BACKEND_LINES)):
        entry = stack.get(kind) or {}
        provider = str(entry.get("provider") or "")
        if not provider:
            continue
        framework = str(entry.get("framework") or provider)
        template = table.get(provider)
        if template is not None:
            supported = "no OUMV adapter" not in template
        else:
            supported = _profile_supported(kind, entry)
        labels.append((kind, provider, framework, supported, str(entry.get("adapter") or "")))
    return labels


def _is_root(path: str) -> bool:
    try:
        from src.paths import is_project_root
        return bool(is_project_root(path))
    except Exception:
        return False


def _extra_root_lines(roots: List[str], seen: set) -> List[str]:
    """One compact line per non-primary root: ready → exact call, else fallback."""
    try:
        from src.retrofit import detect_project_stack
    except Exception:
        return []
    lines: List[str] = []
    for raw in roots:
        if not raw:
            continue
        path = os.path.abspath(str(raw))
        if not os.path.isdir(path):
            continue
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        if not _is_root(path):
            continue
        try:
            stack = detect_project_stack(path)
        except Exception:
            continue
        for kind, provider, framework, supported, adapter in _stack_labels(stack):
            if supported:
                lines.append(
                    f"- {path}: {kind} {provider}/{framework} — OUMV ready; "
                    f"tokensafe:oumv(workspace_dir='{path}') for status/observe/mutate."
                )
            else:
                lines.append(
                    f"- {path}: {kind} {provider}/{framework} — no OUMV adapter; "
                    f"repo_map/scoped_search with project_path='{path}'."
                )
    return lines


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
    extra_workspaces: Optional[List[str]] = None,
) -> str:
    """Builds the compact routing brief. Returns "" for non-project directories.

    ``extra_workspaces`` covers the other roots an agent can edit in the same
    session (ZCode ``workspacePaths[1:]``, ``TOKENSAFE_WORKSPACES``). Every root
    is classified on its own: the primary verdict must never be read as a
    statement about another repository, so the brief always states its scope.
    """
    if not workspace or not os.path.isdir(workspace):
        return ""
    if not _is_root(workspace):
        return ""

    try:
        from src.retrofit import detect_project_stack
        stack = detect_project_stack(workspace)
    except Exception:
        return ""

    frontend = stack.get("frontend") or {}
    backend = stack.get("backend") or {}
    if not frontend and not backend:
        return ""

    seen = {os.path.realpath(workspace)}
    extra_lines = _extra_root_lines(extra_workspaces or [], seen)

    lines: List[str] = ["[TokenSafe OUMV routing brief]"]
    if extra_lines:
        lines.append(
            "- scope: per-root verdicts below; for any unlisted repo call "
            "tokensafe:oumv(action='status', workspace_dir='<repo>')."
        )
    else:
        lines.append(
            "- scope: this verdict covers this root only; before editing another repo call "
            "tokensafe:oumv(action='status', workspace_dir='<repo>')."
        )
    for kind, entry in (("frontend", frontend), ("backend", backend)):
        if not entry:
            continue
        provider = str(entry.get("provider") or "")
        framework = str(entry.get("framework") or provider)
        supported = _profile_supported(kind, entry)
        table = _FRONTEND_LINES if kind == "frontend" else _BACKEND_LINES
        template = table.get(provider)
        if template:
            lines.append("- " + template.format(provider=provider, framework=framework))
        elif _profile_supported(kind, entry):
            # Provider without a routing template (e.g. Solid) but with a
            # runtime-reported adapter: route through OUMV instead of greps.
            adapter = str(entry.get("adapter") or "")
            via = f" via {adapter}" if adapter else ""
            live = "; start live bridge via `tokensafe dev`" if entry.get("vite") else ""
            lines.append(
                f"- {kind} {provider}/{framework}: OUMV ready{via}{live}; call "
                "tokensafe:oumv(action='status'|'observe'|'mutate') before reading files."
            )
        else:
            lines.append(
                f"- {provider}/{framework}: OUMV has no adapter yet — use "
                "tokensafe:repo_map / scoped_search / smart_read_file instead of oumv."
            )

    if extra_lines:
        lines.append("- other workspace roots:")
        lines.extend(extra_lines)

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
        # Cut on a line boundary: a half-written path in a truncated root line
        # is worse than a dropped tail line (the scope sentence stays at the top).
        cut = brief[: max_chars - 1]
        if "\n" in cut:
            cut = cut.rsplit("\n", 1)[0]
        brief = cut.rstrip() + "…"
    return brief
