"""
Environment Auditor & Context Bloat Guard for Antigravity Token Optimizer.
Detects heavy MCP servers, redundant tool schemas, and excessive skills that bloat
the system prompt on every API turn. Provides one-click enable/disable toggles.
Also audits always-on rule files against the Antigravity limits (rules.md):
24 KB per file after @[label](path) expansion, 20,000-token shared budget.
"""

import os
import re
import json
from typing import Dict, List, Any, Optional

from src.tokenizer import count_tokens

EST_TOKENS_PER_TOOL = 25
EST_TOKENS_PER_SKILL = 180
CRITICAL_TOOL_COUNT_THRESHOLD = 40

# rules.md contract limits
RULE_FILE_LIMIT_BYTES = 24000   # per rule file, after @[label](path) expansion
RULES_BUDGET_TOKENS = 20000     # shared always-on/global rules budget

_RULE_INCLUDE_RE = re.compile(r"@\[[^\]]*\]\(([^)]+)\)")


class EnvironmentAuditor:
    """Scans and audits static context overhead (MCP servers, skills, rules)."""

    @classmethod
    def get_config_paths(cls) -> List[str]:
        home = os.path.expanduser("~")
        return [
            os.path.join(home, ".gemini", "config", "mcp_config.json"),
            os.path.join(home, ".gemini", "antigravity", "mcp_config.json")
        ]

    @classmethod
    def get_mcp_schemas_dir(cls) -> str:
        return os.path.join(os.path.expanduser("~"), ".gemini", "antigravity", "mcp")

    @classmethod
    def get_skills_dirs(cls, workspace_root: Optional[str] = None) -> List[str]:
        """Global skill roots plus the workspace `.agents/` customization dir
        (json_configs.md: one level deep, entries scanned for SKILL.md)."""
        home = os.path.expanduser("~")
        dirs = [
            os.path.join(home, ".gemini", "config", "skills"),
            os.path.join(home, ".gemini", "config", "plugins"),
            os.path.join(home, ".gemini", "antigravity", "builtin", "skills"),
            os.path.join(workspace_root or os.getcwd(), ".agents"),
        ]
        return [d for d in dirs if os.path.isdir(d)]

    @classmethod
    def get_plugins_dir(cls) -> str:
        return os.path.join(os.path.expanduser("~"), ".gemini", "config", "plugins")

    @classmethod
    def get_enable_state_path(cls) -> str:
        return os.path.join(os.path.expanduser("~"), ".gemini", "config", "config.json")

    @classmethod
    def _get_disabled_plugin_names(cls) -> set:
        """Plugin directory names that are switched off. plugins.md: the
        enable-state lives in config.json under a `plugins` map keyed by the
        plugin's directory name; a `plugin.json` `"disabled": true` also ships
        a plugin switched off."""
        disabled = set()
        state_path = cls.get_enable_state_path()
        if os.path.isfile(state_path):
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    plugins_state = json.load(f).get("plugins", {})
                for name, state in plugins_state.items():
                    if isinstance(state, dict) and state.get("enabled") is False:
                        disabled.add(name)
            except Exception:
                pass
        plugins_dir = cls.get_plugins_dir()
        if os.path.isdir(plugins_dir):
            for entry in os.listdir(plugins_dir):
                manifest = os.path.join(plugins_dir, entry, "plugin.json")
                if os.path.isfile(manifest):
                    try:
                        with open(manifest, "r", encoding="utf-8") as f:
                            if json.load(f).get("disabled") is True:
                                disabled.add(entry)
                    except Exception:
                        pass
        return disabled

    @classmethod
    def get_configured_mcp_servers(cls) -> Dict[str, Any]:
        configured: Dict[str, Any] = {}
        # Merge EVERY present config file: both ~/.gemini/config and the
        # antigravity dir can legitimately carry servers (they are merged by
        # Antigravity too), so breaking on the first one undercounts.
        for p in cls.get_config_paths():
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict) and isinstance(loaded.get("mcpServers"), dict):
                        configured.update(loaded["mcpServers"])
                except Exception:
                    continue
        # Plugin-declared servers (plugins/<name>/mcp_config.json) are loaded
        # by Antigravity together with the plugin — audit them too, unless the
        # plugin itself is disabled.
        disabled = cls._get_disabled_plugin_names()
        plugins_dir = cls.get_plugins_dir()
        if os.path.isdir(plugins_dir):
            for entry in sorted(os.listdir(plugins_dir)):
                if entry in disabled or entry.endswith(".disabled"):
                    continue
                plugin_cfg = os.path.join(plugins_dir, entry, "mcp_config.json")
                if not os.path.isfile(plugin_cfg):
                    continue
                try:
                    with open(plugin_cfg, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict) and isinstance(loaded.get("mcpServers"), dict):
                        configured.update(loaded["mcpServers"])
                except Exception:
                    continue
        return configured

    @classmethod
    def _load_rule_text(cls, path: str, depth: int = 1) -> str:
        """Reads a rule file with one level of @[label](path) include expansion."""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception:
            return ""

        if depth <= 0:
            return text

        def _sub(match: "re.Match[str]") -> str:
            inc = match.group(1)
            if inc.startswith(("http://", "https://")):
                return match.group(0)
            if not os.path.isabs(inc):
                inc = os.path.join(os.path.dirname(path), inc)
            try:
                with open(inc, "r", encoding="utf-8", errors="replace") as f:
                    return f.read()
            except Exception:
                return match.group(0)

        return _RULE_INCLUDE_RE.sub(_sub, text)

    @classmethod
    def audit_rules(cls, workspace_root: Optional[str] = None) -> Dict[str, Any]:
        """Audits workspace rule files against the rules.md limits.

        Scans AGENTS.md / GEMINI.md at the workspace root and .agents/rules/*.md.
        Per-file: effective byte size after include expansion vs the 24 KB cap.
        Aggregate: token count vs the 20,000-token always-on rules budget
        (exceeding it demotes rules to file-path pointers)."""
        root = workspace_root or os.getcwd()
        candidates = [
            os.path.join(root, "AGENTS.md"),
            os.path.join(root, "GEMINI.md"),
        ]
        rules_dir = os.path.join(root, ".agents", "rules")
        if os.path.isdir(rules_dir):
            for entry in sorted(os.listdir(rules_dir)):
                if entry.endswith(".md"):
                    candidates.append(os.path.join(rules_dir, entry))

        violations: List[Dict[str, Any]] = []
        total_tokens = 0
        files_count = 0
        for path in candidates:
            if not os.path.isfile(path):
                continue
            files_count += 1
            text = cls._load_rule_text(path)
            effective_bytes = len(text.encode("utf-8", errors="replace"))
            total_tokens += count_tokens(text)
            if effective_bytes > RULE_FILE_LIMIT_BYTES:
                violations.append({
                    "path": os.path.relpath(path, root),
                    "effective_bytes": effective_bytes,
                    "limit_bytes": RULE_FILE_LIMIT_BYTES,
                })

        return {
            "files": files_count,
            "total_tokens": total_tokens,
            "budget_exceeded": total_tokens > RULES_BUDGET_TOKENS,
            "violations": violations,
        }

    @classmethod
    def audit(cls, workspace_root: Optional[str] = None) -> Dict[str, Any]:
        """Audits all active MCP servers, skills and rules for context footprint."""
        configured = cls.get_configured_mcp_servers()
        schemas_dir = cls.get_mcp_schemas_dir()

        servers = {}
        # 1. Discover all servers from schema directories
        if os.path.isdir(schemas_dir):
            for entry in os.listdir(schemas_dir):
                entry_path = os.path.join(schemas_dir, entry)
                if not os.path.isdir(entry_path):
                    continue

                is_disabled = entry.endswith(".disabled")
                srv_name = entry[:-9] if is_disabled else entry

                # Count tool schemas
                tool_files = [f for f in os.listdir(entry_path) if f.endswith(".json")]
                tool_count = len(tool_files)

                is_active = (not is_disabled) and (srv_name in configured)
                est_tokens = tool_count * EST_TOKENS_PER_TOOL

                servers[srv_name] = {
                    "name": srv_name,
                    "active": is_active,
                    "disabled": is_disabled,
                    "tool_count": tool_count,
                    "est_tokens_per_turn": est_tokens,
                    "is_bloat": is_active and (tool_count >= CRITICAL_TOOL_COUNT_THRESHOLD)
                }

        # Include configured servers that might not have a schema dir cached yet
        for srv_name in configured:
            if srv_name not in servers:
                servers[srv_name] = {
                    "name": srv_name,
                    "active": True,
                    "disabled": False,
                    "tool_count": 5,  # default estimate
                    "est_tokens_per_turn": 5 * EST_TOKENS_PER_TOOL,
                    "is_bloat": False
                }

        # 2. Count skills (follow symlinked plugins/skills with cycle protection; skip disabled plugins)
        skills_count = 0
        disabled_plugins = cls._get_disabled_plugin_names()
        plugins_dir = os.path.abspath(cls.get_plugins_dir())
        seen_skill_dirs = set()
        for sdir in cls.get_skills_dirs(workspace_root):
            is_plugins_root = os.path.abspath(sdir) == plugins_dir
            for root, dirs, files in os.walk(sdir, followlinks=True):
                real_root = os.path.realpath(root)
                if real_root in seen_skill_dirs:
                    dirs[:] = []
                    continue
                seen_skill_dirs.add(real_root)
                if is_plugins_root and os.path.abspath(root) == plugins_dir:
                    dirs[:] = [d for d in dirs if d not in disabled_plugins and not d.endswith(".disabled")]
                else:
                    dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", "__pycache__", ".venv", "venv") and not d.endswith(".disabled")]
                if "SKILL.md" in files:
                    skills_count += 1

        # 3. Rule files (rules.md limits)
        rules = cls.audit_rules(workspace_root)

        # 4. Ghost Tokens audit
        ghost_tokens = cls.audit_ghost_tokens(workspace_root)

        total_mcp_tools = sum(s["tool_count"] for s in servers.values() if s["active"])
        total_mcp_tokens = sum(s["est_tokens_per_turn"] for s in servers.values() if s["active"])
        total_skills_tokens = skills_count * EST_TOKENS_PER_SKILL
        total_static_tokens = total_mcp_tokens + total_skills_tokens

        bloated_servers = [s for s in servers.values() if s["is_bloat"]]

        treesitter_info: Dict[str, Any] = {"available": False, "active": 0, "total": 0, "missing": []}
        try:
            from src.treesitter_backend import TreeSitterBackend
            ts_status = TreeSitterBackend.check_all_languages()
            ok_cnt = sum(1 for v in ts_status.values() if v)
            tot_cnt = len(ts_status)
            missing = [k for k, v in ts_status.items() if not v]
            treesitter_info = {
                "available": TreeSitterBackend.is_available(),
                "active": ok_cnt,
                "total": tot_cnt,
                "all_ok": ok_cnt == tot_cnt and tot_cnt > 0,
                "missing": missing,
            }
        except Exception:
            pass

        return {
            "servers": servers,
            "total_active_servers": len([s for s in servers.values() if s["active"]]),
            "total_tools": total_mcp_tools,
            "total_skills": skills_count,
            "mcp_tokens_per_turn": total_mcp_tokens,
            "skills_tokens_per_turn": total_skills_tokens,
            "total_static_tokens_per_turn": total_static_tokens,
            "bloated_servers": bloated_servers,
            "has_critical_bloat": len(bloated_servers) > 0,
            "rules": rules,
            "ghost_tokens": ghost_tokens,
            "treesitter": treesitter_info,
        }

    @classmethod
    def audit_ghost_tokens(cls, workspace_root: Optional[str] = None) -> Dict[str, Any]:
        """
        Audits the workspace and configuration for 'Ghost Tokens':
        1. Multi-IDE duplicate rule files (e.g. both CLAUDE.md and .cursorrules / AGENTS.md repeating rules).
        2. Commented-out dead code blocks in rule files (HTML comments, hash comments).
        3. Stale or orphaned agent instruction files.
        4. Oversized prompt config files (>2,500 tokens).
        """
        root = workspace_root or os.getcwd()

        rule_candidates = [
            ("AGENTS.md", os.path.join(root, "AGENTS.md"), "Universal"),
            ("GEMINI.md", os.path.join(root, "GEMINI.md"), "Gemini CLI"),
            ("CLAUDE.md", os.path.join(root, "CLAUDE.md"), "Claude Code"),
            (".claude/CLAUDE.md", os.path.join(root, ".claude", "CLAUDE.md"), "Claude Code"),
            (".cursorrules", os.path.join(root, ".cursorrules"), "Cursor"),
            (".windsurfrules", os.path.join(root, ".windsurfrules"), "Windsurf"),
            (".clinerules", os.path.join(root, ".clinerules"), "Cline"),
            (".github/copilot-instructions.md", os.path.join(root, ".github", "copilot-instructions.md"), "Copilot"),
        ]

        found_rules = []
        for name, path, agent_type in rule_candidates:
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    toks = count_tokens(content)
                    found_rules.append({
                        "name": name,
                        "path": path,
                        "rel_path": os.path.relpath(path, root),
                        "agent": agent_type,
                        "tokens": toks,
                        "content": content
                    })
                except Exception:
                    pass

        ghost_files = []
        ghost_tokens = 0

        # Check for multi-IDE redundant rule files
        if len(found_rules) >= 2:
            canonical = next((r for r in found_rules if r["name"] in ("AGENTS.md", "CLAUDE.md")), found_rules[0])
            for r in found_rules:
                if r != canonical:
                    ghost_files.append({
                        "file": r["rel_path"],
                        "type": "DUPLICATE_CROSS_IDE_RULE",
                        "tokens": r["tokens"],
                        "description": f"Redundant across IDEs ({r['agent']} vs {canonical['name']})"
                    })
                    ghost_tokens += r["tokens"]

        # Check for dead commented-out rule blocks
        comment_re = re.compile(r"<!--[\s\S]*?-->|^(?:#\s*.*\n){4,}", re.MULTILINE)
        for r in found_rules:
            dead_matches = comment_re.findall(r["content"])
            dead_chars = sum(len(m) for m in dead_matches)
            if dead_chars > 300:
                dead_toks = count_tokens("".join(dead_matches))
                ghost_files.append({
                    "file": r["rel_path"],
                    "type": "DEAD_COMMENT_BLOCKS",
                    "tokens": dead_toks,
                    "description": f"Commented-out inactive rule text in {r['rel_path']} (~{dead_toks} tokens)"
                })
                ghost_tokens += dead_toks

        # Check for oversized config files (>2,500 tokens)
        for cfg in ["package.json", "pyproject.toml", "Cargo.toml", "tsconfig.json"]:
            cfg_path = os.path.join(root, cfg)
            if os.path.isfile(cfg_path):
                try:
                    with open(cfg_path, "r", encoding="utf-8", errors="replace") as f:
                        cfg_content = f.read()
                    cfg_tokens = count_tokens(cfg_content)
                    if cfg_tokens > 2500:
                        ghost_files.append({
                            "file": cfg,
                            "type": "OVERSIZED_CONFIG",
                            "tokens": cfg_tokens,
                            "description": f"Heavy config file ({cfg_tokens:,} tokens) loaded repeatedly"
                        })
                except Exception:
                    pass

        return {
            "ghost_tokens_per_turn": ghost_tokens,
            "ghost_files": ghost_files,
            "total_rule_files": len(found_rules),
            "has_ghost_tokens": len(ghost_files) > 0
        }

    @classmethod
    def get_bloat_warning(cls, workspace_root: Optional[str] = None) -> Optional[str]:
        """Returns a formatted warning string if critical context bloat or
        rule-file overruns are detected."""
        report = cls.audit(workspace_root)
        parts: List[str] = []

        if report["has_critical_bloat"]:
            bloated = report["bloated_servers"]
            names = ", ".join([f"`{b['name']}` ({b['tool_count']} tools, ~{b['est_tokens_per_turn']:,} tokens/turn)" for b in bloated])
            parts.append(
                f"Heavy MCP server detected: {names}. "
                f"Static context overhead per turn: **~{report['total_static_tokens_per_turn']:,} tokens**. "
                f"Disable unused servers via `tokensafe mcp disable <name>`."
            )

        rules = report.get("rules") or {}
        if rules.get("budget_exceeded"):
            parts.append(
                f"Always-on rule files total ~{rules['total_tokens']:,} tokens — over the "
                f"{RULES_BUDGET_TOKENS:,}-token rules budget; over-budget rules degrade to file-path pointers. "
                f"Trim AGENTS.md / .agents/rules/*.md."
            )
        for v in rules.get("violations", []):
            parts.append(
                f"Rule file `{v['path']}` is {v['effective_bytes']:,} bytes after @[label](path) expansion — "
                f"over the {RULE_FILE_LIMIT_BYTES:,}-byte per-file cap; it will be truncated."
            )

        if not parts:
            return None
        return "⚠️ **TokenSafe Bloat Alert**: " + " ".join(parts)

    @classmethod
    def disable_mcp(cls, server_name: str) -> Dict[str, Any]:
        """Disables an MCP server in mcp_config.json and renames its schema directory."""
        server_name = server_name.strip()
        schemas_dir = cls.get_mcp_schemas_dir()

        # 1. Update config files
        modified = False
        server_config = None
        for p in cls.get_config_paths():
            real_path = os.path.realpath(p)
            if os.path.isfile(real_path):
                try:
                    with open(real_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if not isinstance(data, dict):
                        continue
                    mcp_servers = data.get("mcpServers")
                    if isinstance(mcp_servers, dict) and server_name in mcp_servers:
                        server_config = mcp_servers.pop(server_name)
                        if not isinstance(data.get("disabledMcpServers"), dict):
                            data["disabledMcpServers"] = {}
                        data["disabledMcpServers"][server_name] = server_config
                        with open(real_path, "w", encoding="utf-8") as f:
                            json.dump(data, f, indent=2)
                        modified = True
                except Exception:
                    pass

        # 2. Rename schema directory to .disabled
        active_dir = os.path.join(schemas_dir, server_name)
        disabled_dir = os.path.join(schemas_dir, f"{server_name}.disabled")
        renamed = False
        if os.path.isdir(active_dir):
            try:
                os.rename(active_dir, disabled_dir)
                renamed = True
            except Exception:
                pass

        return {
            "success": modified or renamed,
            "server_name": server_name,
            "config_updated": modified,
            "directory_renamed": renamed
        }

    @classmethod
    def enable_mcp(cls, server_name: str) -> Dict[str, Any]:
        """Re-enables a previously disabled MCP server."""
        server_name = server_name.strip()
        schemas_dir = cls.get_mcp_schemas_dir()

        # 1. Update config files
        modified = False
        for p in cls.get_config_paths():
            real_path = os.path.realpath(p)
            if os.path.isfile(real_path):
                try:
                    with open(real_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if not isinstance(data, dict):
                        continue
                    disabled_servers = data.get("disabledMcpServers")
                    if isinstance(disabled_servers, dict) and server_name in disabled_servers:
                        srv_cfg = disabled_servers.pop(server_name)
                        if not isinstance(data.get("mcpServers"), dict):
                            data["mcpServers"] = {}
                        data["mcpServers"][server_name] = srv_cfg
                        with open(real_path, "w", encoding="utf-8") as f:
                            json.dump(data, f, indent=2)
                        modified = True
                except Exception:
                    pass

        # 2. Rename schema directory from .disabled
        disabled_dir = os.path.join(schemas_dir, f"{server_name}.disabled")
        active_dir = os.path.join(schemas_dir, server_name)
        renamed = False
        if os.path.isdir(disabled_dir):
            try:
                os.rename(disabled_dir, active_dir)
                renamed = True
            except Exception:
                pass

        return {
            "success": modified or renamed,
            "server_name": server_name,
            "config_updated": modified,
            "directory_renamed": renamed
        }

    @classmethod
    def format_cli_report(cls, workspace_root: Optional[str] = None) -> str:
        """Formats visual CLI report for tokensafe doctor."""
        report = cls.audit(workspace_root)
        score_line = ""
        try:
            from src.quality_score import ContextQualityScore
            from src.governor import ContextBudget
            from src.metrics import MetricsTracker
            budget = ContextBudget.estimate(MetricsTracker(), session_id="mcp_session")
            q_score = ContextQualityScore.calculate(budget=budget, audit=report)
            color = "\033[32m" if q_score["grade"] in ("S", "A") else ("\033[33m" if q_score["grade"] == "B" else "\033[31m")
            score_line = f"  \033[1mContext Quality Score:\033[0m {color}\033[1mGrade {q_score['grade']} ({q_score['label']})\033[0m — {q_score['headroom_pct']:.0f}% headroom, {q_score['score']}/100 pts\n"
        except Exception:
            score_line = ""

        lines = [
            "\n\033[1m\033[36m⚡ TOKENSAFE ENVIRONMENT AUDIT (CONTEXT TAX DOCTOR) ⚡\033[0m\n",
        ]
        if score_line:
            lines.append(score_line)
        lines.extend([
            f"  \033[1mTotal Base Context Tax:\033[0m ~{report['total_static_tokens_per_turn']:,} tokens on EVERY API turn\n",
            "  \033[1mActive MCP Servers:\033[0m"
        ])

        for name, s in sorted(report["servers"].items()):
            if s["active"]:
                if s["is_bloat"]:
                    status = f"\033[31m\033[1m✖ {name:<18} ({s['tool_count']:>3} tools) [CRITICAL BLOAT: ~{s['est_tokens_per_turn']:,} tok/turn]\033[0m"
                else:
                    status = f"\033[32m✔ {name:<18} ({s['tool_count']:>3} tools) [~{s['est_tokens_per_turn']:,} tok/turn]\033[0m"
            else:
                status = f"\033[2m○ {name:<18} ({s['tool_count']:>3} tools) [Disabled]\033[0m"
            lines.append(f"    {status}")

        lines.append(f"\n  \033[1mInstalled Skills:\033[0m {report['total_skills']} skills (~{report['skills_tokens_per_turn']:,} tokens/turn)")

        rules = report.get("rules") or {}
        if rules.get("files"):
            budget_flag = "\033[33mover budget\033[0m" if rules.get("budget_exceeded") else "\033[32mwithin budget\033[0m"
            lines.append(f"  \033[1mRule Files:\033[0m {rules['files']} files, ~{rules['total_tokens']:,}/20,000 tokens [{budget_flag}]")
            for v in rules.get("violations", []):
                lines.append(f"    \033[31m✖ {v['path']}: {v['effective_bytes']:,} bytes > {RULE_FILE_LIMIT_BYTES:,} cap\033[0m")

        ts = report.get("treesitter") or {}
        if ts.get("total", 0) > 0:
            if ts.get("all_ok"):
                lines.append(f"  \033[1mAST Engines (Tree-sitter):\033[0m \033[32m✔ {ts['active']}/{ts['total']} languages active\033[0m")
            else:
                missing_str = ", ".join(ts.get("missing", []))
                lines.append(f"  \033[1mAST Engines (Tree-sitter):\033[0m \033[33m⚠ {ts['active']}/{ts['total']} active (missing: {missing_str})\033[0m")

        ghost = report.get("ghost_tokens") or {}
        if ghost.get("has_ghost_tokens"):
            lines.append(f"\n  \033[33m\033[1m👻 Ghost Tokens Detected:\033[0m ~{ghost['ghost_tokens_per_turn']:,} wasted tokens per turn")
            for gf in ghost.get("ghost_files", [])[:5]:
                lines.append(f"    • \033[1m{gf['file']}\033[0m (~{gf['tokens']:,} tokens): {gf['description']}")
            lines.append("    \033[36m💡 Tip: Consolidate redundant rule files into a single AGENTS.md\033[0m")
        else:
            lines.append("\n  \033[32m✔ No Ghost Tokens detected: Rule files and configs are lean.\033[0m")

        if report["has_critical_bloat"]:
            lines.append("\n  \033[33m\033[1mDiagnostic Recommendation:\033[0m")
            for b in report["bloated_servers"]:
                lines.append(f"    ➜ Run: \033[36mtokensafe mcp disable {b['name']}\033[0m to save ~{b['est_tokens_per_turn']:,} tokens per turn!")
        else:
            lines.append("\n  \033[32m✔ Environment healthy: No critical MCP bloat detected.\033[0m")

        lines.append("")
        return "\n".join(lines)
