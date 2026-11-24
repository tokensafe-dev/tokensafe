#!/usr/bin/env python3
"""
Antigravity Lifecycle Hook: SessionStart
Fires once when a new session starts. Accepts the same injectSteps step types
as PreInvocation (hooks.md contract) and carries the one-shot context that
used to run inside PreInvocation on every turn — mirroring the SessionStart
pattern of Claude Code / Cursor / Gemini CLI: session-scoped state is injected
once as a persistent systemMessage, not re-sent on every turn.

Injects:
1. TokenSafe protocol directives (persistent systemMessage — replaces the
   per-turn ephemeral reminders and the every-5th-turn re-nudge)
2. Quota / community-limit warning banner (user-facing markdown)
3. Environment bloat warning (heavy MCP servers, oversized rule files)
Also kicks off the background OS update notification check and the
zero-config community-license issuance (anonymous free-tier identity).
"""

import sys
import os
import json

HOOK_DIR = os.path.dirname(os.path.realpath(__file__))
PROJECT_ROOT = os.path.dirname(HOOK_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.metrics import MetricsTracker
from src.license_manager import LicenseManager
from src.environment_auditor import EnvironmentAuditor
from src.adapter_core import TOKENSAFE_DIRECTIVES as PROTOCOL_DIRECTIVES


def _user_markdown(markdown: str) -> dict:
    """systemMessage step rendered as inline markdown to the user; per the
    hooks.md contract the model sees only the systemMessage text (empty here)."""
    return {
        "systemMessage": {
            "systemMessage": "",
            "metadata": {"user_markdown": markdown},
        }
    }


def _guest_pitch_step(quota: dict):
    """One-shot Guest-mode pitch: fires on the first session of a machine that
    runs below the 500K free tier (offline guest or a fresh anonymous 50K
    community license), then never again (marker file in the data dir). The
    80%/100% quota banners cover every later nudge, so this must not become
    per-turn nagging. Anonymous guest machines never see the interactive
    installer prompt, so this is their only 500K-for-login touchpoint."""
    try:
        limit = quota.get("monthly_limit")
        if limit is None or int(limit) >= 500000:
            return None
        from src.paths import resolve_data_dir
        marker_path = os.path.join(resolve_data_dir(), ".guest_pitch_shown")
        if os.path.exists(marker_path):
            return None
        os.makedirs(os.path.dirname(marker_path), exist_ok=True)
        with open(marker_path, "w", encoding="utf-8") as marker:
            marker.write("1")
        return _user_markdown(
            "⚡ **TokenSafe Guest mode**: savings pause at 50,000 tokens/month. "
            "Run `tokensafe login` in your terminal to unlock 500,000 free tokens/month (10x) "
            "plus your personal savings dashboard. Takes about a minute."
        )
    except Exception:
        return None


def main():
    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            print(json.dumps({}))
            return
        payload = json.loads(raw_input)
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        print(json.dumps({}))
        return

    # hooks.md: hook CWD is the plugin directory, not the workspace — use the
    # workspacePaths from the payload for project-scoped audits.
    raw_ws = payload.get("workspacePaths")
    workspace_paths = raw_ws if isinstance(raw_ws, list) else []
    workspace_root = str(workspace_paths[0]) if workspace_paths else os.getcwd()
    try:
        from src.paths import save_active_workspace, ensure_tokensafe_gitignore
        save_active_workspace(workspace_root)
        ensure_tokensafe_gitignore(workspace_root)
    except Exception:
        pass

    directives = PROTOCOL_DIRECTIVES
    # Cold-start brief (project-gated): the God Nodes banner is mapped only
    # when the host actually opened a workspace folder. Plain chats (no
    # workspacePaths) previously scanned the hook CWD (plugin/system dir) —
    # junk topology + wasted scan time; they now skip the banner entirely.
    # Opened-folder sessions behave exactly as before (regression-minimal
    # gate: folder-opened is the ONLY condition — no project-marker sniffing).
    try:
        if workspace_paths:
            from src.graph_engine import GraphEngine
            graph = GraphEngine.get_instance(workspace_root)
            banner = graph.format_topology_banner()
            if banner:
                directives = (
                    f"{directives}\n\n"
                    f"{banner}\n"
                    "⚡ Core God Nodes and dependency hubs mapped above. "
                    "Call 'tokensafe:get_architecture_graph(project_path)' for full dependency trees."
                )
    except Exception:
        pass

    system_sections = [directives]

    try:
        from src.mistakes import MistakesLedger
        mistake_guard = MistakesLedger.get_guard_prompt(project_root=workspace_root)
        if mistake_guard:
            system_sections.append(mistake_guard)
    except Exception:
        pass

    inject_steps = [
        {"systemMessage": {"systemMessage": "\n\n".join(system_sections), "metadata": {}}}
    ]

    try:
        tracker = MetricsTracker()
        quota = LicenseManager.check_quota(tracker)
        if quota.get("warning"):
            inject_steps.append(_user_markdown(quota["warning"]))
        pitch = _guest_pitch_step(quota)
        if pitch:
            inject_steps.append(pitch)
    except Exception:
        pass

    try:
        from src.version_checker import VersionChecker
        VersionChecker.notify_os_update_background()
    except Exception:
        pass

    try:
        import threading
        threading.Thread(target=LicenseManager.ensure_community_license, daemon=True).start()
    except Exception:
        pass

    try:
        bloat_warning = EnvironmentAuditor.get_bloat_warning(workspace_root=workspace_root)
        if bloat_warning:
            inject_steps.append(_user_markdown(bloat_warning))
    except Exception:
        pass

    print(json.dumps({"injectSteps": inject_steps}))


if __name__ == "__main__":
    main()
