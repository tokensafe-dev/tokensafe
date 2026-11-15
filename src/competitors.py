"""
TokenSafe Competitor & Conflict Auditor (v3.3.0).

Detects competing or stale token-optimization tools installed on the host:
- rtk (Rust token killer)
- claudeslim (anti-pattern: breaks Anthropic KV prompt cache)
- codegraph (AST code graph MCP)
- tokensave / codesight

Identifies orphan hook scripts, MCP server entries, and stale rules files.
Allows safe cleanup so coding agents run without conflicting middleware.
"""

import os
import shutil
import json
import re
from typing import List, Dict, Any, Optional

HOME = os.path.expanduser("~")

COMPETITORS_SPEC = {
    "rtk": {
        "name": "RTK",
        "class": "DropInBetter",
        "description": "TokenSafe covers all RTK functions (AST skeletons, tests, command folding) with reversibility.",
        "binary": "rtk",
        "hooks": [
            os.path.join(HOME, ".claude", "hooks", "rtk-rewrite.sh"),
            os.path.join(HOME, ".claude", "hooks", "rtk.sh"),
        ],
        "rules": [
            os.path.join(HOME, ".claude", "RTK.md"),
            os.path.join(HOME, ".cursor", "rules", "rtk.mdc"),
        ],
        "rule_markers": ["@RTK.md", "rtk wrap"],
    },
    "claudeslim": {
        "name": "ClaudeSlim",
        "class": "AntiPattern",
        "description": "Known anti-pattern: rewrites prompt prefixes mid-stream and breaks Anthropic KV-cache.",
        "binary": "claudeslim",
        "hooks": [
            os.path.join(HOME, ".claude", "hooks", "claudeslim.sh"),
        ],
        "rules": [
            os.path.join(HOME, ".claude", "claudeslim.md"),
        ],
        "rule_markers": ["claudeslim"],
    },
    "codegraph": {
        "name": "CodeGraph",
        "class": "DropInBetter",
        "description": "TokenSafe GraphEngine provides God Nodes and architecture topology in < 250 tokens without MCP lag.",
        "binary": "codegraph",
        "mcp_keys": ["codegraph"],
    },
    "tokensave": {
        "name": "TokenSave",
        "class": "DropInBetter",
        "description": "Legacy token saver tool; safe to replace.",
        "binary": "tokensave",
    },
}


class CompetitorAuditor:
    """Audits and cleans up conflicting token optimization tools and orphan hooks."""

    @classmethod
    def audit(cls) -> List[Dict[str, Any]]:
        """Audits the environment for presence of competitor tools."""
        detected = []

        for key, spec in COMPETITORS_SPEC.items():
            traces = []

            # 1. Binary on PATH
            bin_name = spec.get("binary")
            if bin_name:
                bin_path = shutil.which(bin_name)
                if bin_path:
                    traces.append({"kind": "binary", "path": bin_path})

            # 2. Hook scripts
            for h_path in spec.get("hooks", []):
                if os.path.isfile(h_path):
                    traces.append({"kind": "hook_script", "path": h_path})

            # 3. Dedicated rules files
            for r_path in spec.get("rules", []):
                if os.path.isfile(r_path):
                    traces.append({"kind": "rule_file", "path": r_path})

            # 4. MCP entries in Claude / Cursor configs
            mcp_keys = spec.get("mcp_keys", [])
            if mcp_keys:
                for cfg_path in (
                    os.path.join(HOME, ".claude.json"),
                    os.path.join(HOME, ".cursor", "mcp.json"),
                    os.path.join(HOME, ".codex", "config.json"),
                ):
                    if os.path.isfile(cfg_path):
                        try:
                            with open(cfg_path, "r", encoding="utf-8") as f:
                                data = json.load(f)
                            servers = data.get("mcpServers") or data.get("mcp") or {}
                            for mk in mcp_keys:
                                if mk in servers:
                                    traces.append({"kind": "mcp_entry", "config": cfg_path, "key": mk})
                        except Exception:
                            pass

            # 5. Shared rules file references (@RTK.md in ~/.claude/CLAUDE.md)
            markers = spec.get("rule_markers", [])
            claude_md = os.path.join(HOME, ".claude", "CLAUDE.md")
            if markers and os.path.isfile(claude_md):
                try:
                    with open(claude_md, "r", encoding="utf-8") as f:
                        txt = f.read()
                    for m in markers:
                        if m in txt:
                            traces.append({"kind": "rule_reference", "file": claude_md, "marker": m})
                except Exception:
                    pass

            if traces:
                detected.append({
                    "id": key,
                    "name": spec["name"],
                    "class": spec["class"],
                    "description": spec["description"],
                    "traces": traces,
                })

        return detected

    @classmethod
    def cleanup(cls, tool_id: str) -> Dict[str, Any]:
        """Cleans up orphan hook scripts and rules for the specified tool."""
        spec = COMPETITORS_SPEC.get(tool_id)
        if not spec:
            return {"success": False, "error": f"Unknown tool id: {tool_id}"}

        removed = []
        # Remove hook scripts
        for h_path in spec.get("hooks", []):
            if os.path.isfile(h_path):
                try:
                    os.remove(h_path)
                    removed.append(h_path)
                except Exception:
                    pass

        # Remove rule files
        for r_path in spec.get("rules", []):
            if os.path.isfile(r_path):
                try:
                    os.remove(r_path)
                    removed.append(r_path)
                except Exception:
                    pass

        # Clean markers from ~/.claude/CLAUDE.md
        markers = spec.get("rule_markers", [])
        claude_md = os.path.join(HOME, ".claude", "CLAUDE.md")
        if markers and os.path.isfile(claude_md):
            try:
                with open(claude_md, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                new_lines = [ln for ln in lines if not any(m in ln for m in markers)]
                if len(new_lines) != len(lines):
                    with open(claude_md, "w", encoding="utf-8") as f:
                        f.writelines(new_lines)
                    removed.append(f"{claude_md} (cleaned marker references)")
            except Exception:
                pass

        return {"success": True, "removed": removed}
