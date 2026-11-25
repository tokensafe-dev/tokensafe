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
import hashlib
import shutil
from typing import Dict, List, Any, Optional, Tuple

from src.tokenizer import count_tokens

EST_TOKENS_PER_TOOL = 25
EST_TOKENS_PER_SKILL = 180
CRITICAL_TOOL_COUNT_THRESHOLD = 40

# Cross-IDE rule files at or below this size that mention AGENTS.md are pointer
# stubs ("rules live in AGENTS.md") — the canonical dedup pattern written by the
# installer and repo hygiene, not ghost mass.
POINTER_RULE_MAX_TOKENS = 250

# rules.md contract limits
RULE_FILE_LIMIT_BYTES = 24000   # per rule file, after @[label](path) expansion
RULES_BUDGET_TOKENS = 20000     # shared always-on/global rules budget

_RULE_INCLUDE_RE = re.compile(r"@\[[^\]]*\]\(([^)]+)\)")
_SUBTABLE_RE = re.compile(r'^\s*\[mcp_servers\.(?:"([^"]+)"|\'([^\']+)\'|([A-Za-z0-9_-]+))(?:\.|\])')


def _parse_codex_toml_servers(raw_bytes: bytes) -> Tuple[Dict[str, Any], Optional[str]]:
    """Parses [mcp_servers.*] top-level server entries from Codex config.toml.
    Returns (servers_dict, parse_error). Never treats nested subtables like
    .env or .http_headers as separate MCP servers."""
    text = raw_bytes.decode("utf-8", errors="replace")
    parse_err: Optional[str] = None
    try:
        import tomllib
        try:
            loaded_toml = tomllib.loads(text)
            mcp_tbl = loaded_toml.get("mcp_servers")
            return (dict(mcp_tbl) if isinstance(mcp_tbl, dict) else {}), None
        except Exception as exc:
            parse_err = str(exc)
    except ImportError:
        pass

    servers: Dict[str, Any] = {}
    for line in text.splitlines():
        m = _SUBTABLE_RE.match(line)
        if m:
            s_name = next((g for g in m.groups() if g), "").strip()
            if s_name:
                servers.setdefault(s_name, {})
    return servers, parse_err


class EnvironmentAuditor:
    """Scans and audits static context overhead (MCP servers, skills, rules)."""

    @classmethod
    def get_config_paths(cls) -> List[str]:
        home = os.path.expanduser("~")
        return [
            os.path.join(home, ".gemini", "config", "mcp_config.json"),
            os.path.join(home, ".gemini", "antigravity", "mcp_config.json"),
            os.path.join(home, ".zcode", "cli", "config.json"),
            os.path.join(home, ".claude.json"),
            os.path.join(home, ".cursor", "mcp.json"),
            os.path.join(home, ".kimi-code", "mcp.json"),
            os.path.join(home, ".codex", "config.toml"),
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
            if not os.path.isfile(p):
                continue
            if p.endswith(".toml"):
                try:
                    with open(p, "rb") as f:
                        raw_bytes = f.read()
                    parsed_toml, _ = _parse_codex_toml_servers(raw_bytes)
                    if parsed_toml:
                        configured.update(parsed_toml)
                except Exception:
                    continue
                continue
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
                        for s_name, s_cfg in loaded["mcpServers"].items():
                            configured[s_name] = s_cfg
                            # Antigravity namespaces plugin MCP servers as <plugin_name>_<server_name>
                            configured[f"{entry}_{s_name}"] = s_cfg
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
        # Skip un-namespaced aliases if their namespaced counterpart already matched a schema
        seen_plugin_suffixes = {
            s_name.split("_", 1)[1] for s_name in servers.keys() if "_" in s_name
        }
        for srv_name in configured:
            if srv_name not in servers:
                if srv_name in seen_plugin_suffixes:
                    continue
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
        skills_breakdown: Dict[str, int] = {}
        disabled_plugins = cls._get_disabled_plugin_names()
        plugins_dir = os.path.abspath(cls.get_plugins_dir())
        seen_skill_dirs = set()
        for sdir in cls.get_skills_dirs(workspace_root):
            is_plugins_root = os.path.abspath(sdir) == plugins_dir
            dir_skills = 0
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
                    dir_skills += 1
            if dir_skills > 0:
                skills_breakdown[sdir] = dir_skills

        # Detect config & schema hygiene issues fixable via `tokensafe doctor --fix`
        config_issues: List[Dict[str, str]] = []
        for p in cls.get_config_paths():
            if p.endswith(".toml") and os.path.isfile(p):
                try:
                    with open(p, "rb") as f:
                        _, parse_err = _parse_codex_toml_servers(f.read())
                    if parse_err:
                        config_issues.append({
                            "type": "CORRUPTED_CODEX_TOML",
                            "path": p,
                            "description": f"Invalid TOML syntax in {os.path.basename(p)} ({parse_err[:80]})"
                        })
                except Exception:
                    pass
        if os.path.isdir(schemas_dir):
            if os.path.isdir(os.path.join(schemas_dir, "tokensafe_tokensafe")) and os.path.isdir(os.path.join(schemas_dir, "tokensafe_tokensafe-mcp")):
                config_issues.append({
                    "type": "STALE_MCP_SCHEMA",
                    "path": os.path.join(schemas_dir, "tokensafe_tokensafe-mcp"),
                    "description": "Legacy schema directory tokensafe_tokensafe-mcp duplicates tokensafe_tokensafe"
                })

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
            "skills_breakdown": skills_breakdown,
            "mcp_tokens_per_turn": total_mcp_tokens,
            "skills_tokens_per_turn": total_skills_tokens,
            "total_static_tokens_per_turn": total_static_tokens,
            "bloated_servers": bloated_servers,
            "has_critical_bloat": len(bloated_servers) > 0,
            "config_issues": config_issues,
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
                    if (
                        r["tokens"] <= POINTER_RULE_MAX_TOKENS
                        and "agents.md" in r["content"].lower()
                    ):
                        continue
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
    def auto_fix(cls, workspace_root: Optional[str] = None) -> Dict[str, Any]:
        """Automatically fixes safe, recoverable context/config issues found by `doctor`:
        1. Repairs corrupted [mcp_servers.tokensafe] / orphaned lines in Codex config.toml.
        2. Removes stale duplicate tokensafe_tokensafe-mcp schema directory.
        3. Backs up & consolidates redundant cross-IDE rule files into AGENTS.md pointer stubs
           and strips large inactive comment blocks from rule files.
        4. Cleans conflicting orphan competitor hooks/rules."""
        root = workspace_root or os.getcwd()
        fixed: List[str] = []
        skipped: List[str] = []

        try:
            from src.paths import resolve_data_dir
            backup_base = resolve_data_dir()
        except Exception:
            backup_base = os.path.join(os.path.expanduser("~"), ".tokensafe", "data")
        ws_hash = hashlib.sha256(os.path.abspath(root).encode("utf-8")).hexdigest()[:12]
        backup_dir = os.path.join(backup_base, "backups", ws_hash)

        # 1. Repair Codex config.toml orphaned lines / corrupted [mcp_servers.tokensafe]
        for p in cls.get_config_paths():
            if not (p.endswith(".toml") and os.path.isfile(p)):
                continue
            try:
                with open(p, "rb") as fb:
                    raw_bytes = fb.read()
                _, parse_err = _parse_codex_toml_servers(raw_bytes)
                orig_text = raw_bytes.decode("utf-8", errors="replace")
                from src.installer import AgentInstaller
                if parse_err or AgentInstaller._CODEX_ORPHAN_LINE_RE.search(orig_text):
                    shutil.copy2(p, f"{p}.backup")
                    cleaned = AgentInstaller._clean_codex_orphans(orig_text)
                    try:
                        try:
                            import tomllib
                            tomllib.loads(cleaned)
                        except ImportError:
                            pass
                        with open(p, "w", encoding="utf-8") as f:
                            f.write(cleaned)
                        fixed.append(f"Repaired TOML syntax/orphans in {p} (backup: {p}.backup)")
                    except Exception as e2:
                        skipped.append(f"Could not auto-repair {p} ({e2}); manual edit needed")
            except Exception:
                pass

        # 2. Clean stale legacy tokensafe_tokensafe-mcp schema dir when tokensafe_tokensafe exists
        schemas_dir = cls.get_mcp_schemas_dir()
        if os.path.isdir(schemas_dir):
            legacy_schema = os.path.join(schemas_dir, "tokensafe_tokensafe-mcp")
            canonical_schema = os.path.join(schemas_dir, "tokensafe_tokensafe")
            if os.path.isdir(legacy_schema) and os.path.isdir(canonical_schema):
                try:
                    shutil.rmtree(legacy_schema)
                    fixed.append("Removed stale legacy MCP schema dir tokensafe_tokensafe-mcp")
                except Exception as exc:
                    skipped.append(f"Could not remove {legacy_schema}: {exc}")

        # 3. Consolidate cross-IDE ghost rule duplicates & strip large dead HTML comment blocks
        ghost = cls.audit_ghost_tokens(root)
        agents_md_path = os.path.join(root, "AGENTS.md")
        has_agents_md = os.path.isfile(agents_md_path)
        pointer_stub = (
            "# TokenSafe Project Rules (Pointer)\n\n"
            "The canonical project rules live in AGENTS.md in this repository — "
            "read that file now and follow it for this repo.\n"
        )
        dead_comment_re = re.compile(r"<!--[\s\S]*?-->")
        for gf in ghost.get("ghost_files", []):
            rel = gf.get("file", "")
            gtype = gf.get("type", "")
            abs_p = os.path.join(root, rel)
            if not rel or not os.path.isfile(abs_p):
                continue
            if gtype == "DUPLICATE_CROSS_IDE_RULE" and has_agents_md and rel != "AGENTS.md":
                try:
                    os.makedirs(backup_dir, exist_ok=True)
                    bak_target = os.path.join(backup_dir, rel.replace(os.sep, "__") + ".bak")
                    shutil.copy2(abs_p, bak_target)
                    with open(abs_p, "w", encoding="utf-8") as f:
                        f.write(pointer_stub)
                    fixed.append(f"Consolidated {rel} -> AGENTS.md pointer (-{gf.get('tokens', 0):,} tok/turn, backup: {bak_target})")
                except Exception as exc:
                    skipped.append(f"Failed to consolidate {rel}: {exc}")
            elif gtype == "DEAD_COMMENT_BLOCKS":
                try:
                    with open(abs_p, "r", encoding="utf-8", errors="replace") as f:
                        txt = f.read()
                    # Preserve <!-- tokensafe --> markers; strip large non-tokensafe HTML comment blocks (>300 chars)
                    def _strip_dead_comment(m: "re.Match[str]") -> str:
                        block = m.group(0)
                        if "<!-- tokensafe" in block or "<!-- /tokensafe" in block:
                            return block
                        return "" if len(block) > 300 else block

                    new_txt = dead_comment_re.sub(_strip_dead_comment, txt)
                    new_txt = re.sub(r"\n{3,}", "\n\n", new_txt).strip() + "\n"
                    if new_txt != txt:
                        os.makedirs(backup_dir, exist_ok=True)
                        bak_target = os.path.join(backup_dir, rel.replace(os.sep, "__") + ".bak")
                        shutil.copy2(abs_p, bak_target)
                        with open(abs_p, "w", encoding="utf-8") as f:
                            f.write(new_txt)
                        fixed.append(f"Stripped inactive comment blocks in {rel} (backup: {bak_target})")
                except Exception as exc:
                    skipped.append(f"Failed to strip comments in {rel}: {exc}")

        # 4. Cleanup conflicting competitor hooks/rules
        try:
            from src.competitors import CompetitorAuditor
            for c in CompetitorAuditor.audit():
                res = CompetitorAuditor.cleanup(c["id"])
                if res.get("removed"):
                    fixed.append(f"Cleaned conflicting {c['name']}: {', '.join(res['removed'])}")
        except Exception:
            pass

        return {
            "fixed": fixed,
            "skipped": skipped,
            "backup_dir": backup_dir if fixed else None,
        }

    @classmethod
    def list_skills(cls, workspace_root: Optional[str] = None) -> List[Dict[str, Any]]:
        """Lists active and disabled skills across all skill roots with source & token estimate."""
        skills: List[Dict[str, Any]] = []
        disabled_plugins = cls._get_disabled_plugin_names()
        plugins_dir = os.path.abspath(cls.get_plugins_dir())
        seen_real = set()

        for sdir in cls.get_skills_dirs(workspace_root):
            is_plugins_root = os.path.abspath(sdir) == plugins_dir
            for root, dirs, files in os.walk(sdir, followlinks=True):
                real_root = os.path.realpath(root)
                if real_root in seen_real:
                    dirs[:] = []
                    continue
                seen_real.add(real_root)
                if is_plugins_root and os.path.abspath(root) == plugins_dir:
                    dirs[:] = [d for d in dirs if d not in disabled_plugins and not d.endswith(".disabled")]
                else:
                    dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", "__pycache__", ".venv", "venv")]

                if "SKILL.md" in files:
                    folder_name = os.path.basename(root)
                    is_disabled = folder_name.endswith(".disabled") or ".disabled" + os.sep in root
                    clean_name = folder_name[:-9] if folder_name.endswith(".disabled") else folder_name
                    desc = ""
                    try:
                        with open(os.path.join(root, "SKILL.md"), "r", encoding="utf-8", errors="replace") as f:
                            head = f.read(1200)
                        m_name = re.search(r"(?m)^name:\s*(.+)$", head)
                        if m_name and m_name.group(1).strip():
                            clean_name = m_name.group(1).strip().strip("'\"")
                        m_desc = re.search(r"(?m)^description:\s*(.+)$", head)
                        if m_desc:
                            raw_desc = m_desc.group(1).strip().strip("'\"")
                            if raw_desc in (">", ">-", "|", "|-"):
                                after = head[m_desc.end():]
                                for next_l in after.splitlines():
                                    stripped = next_l.strip()
                                    if stripped and not re.match(r"^[A-Za-z0-9_-]+:\s*", stripped):
                                        desc = stripped[:90]
                                        break
                            else:
                                desc = raw_desc[:90]
                    except Exception:
                        pass
                    skills.append({
                        "name": clean_name,
                        "dir_name": folder_name[:-9] if folder_name.endswith(".disabled") else folder_name,
                        "path": root,
                        "root": sdir,
                        "disabled": is_disabled,
                        "est_tokens": EST_TOKENS_PER_SKILL,
                        "description": desc,
                        "builtin": "builtin" in root.split(os.sep),
                    })
                    dirs[:] = []
        return sorted(skills, key=lambda x: (x["disabled"], x["name"].lower()))

    @classmethod
    def disable_skill(cls, skill_name: str, workspace_root: Optional[str] = None) -> Dict[str, Any]:
        """Disables a user/workspace skill by renaming its directory to `<name>.disabled`."""
        target = skill_name.strip().lower()
        for sk in cls.list_skills(workspace_root):
            if sk["disabled"]:
                continue
            if sk["name"].lower() == target or sk["dir_name"].lower() == target:
                if sk["builtin"]:
                    return {"success": False, "error": f"Skill '{sk['name']}' is a builtin Antigravity skill and cannot be renamed safely."}
                src_path = sk["path"]
                dst_path = f"{src_path}.disabled"
                if os.path.exists(dst_path):
                    return {"success": False, "error": f"Target '{dst_path}' already exists."}
                try:
                    os.rename(src_path, dst_path)
                    return {"success": True, "skill": sk["name"], "path": dst_path}
                except Exception as exc:
                    return {"success": False, "error": str(exc)}
        return {"success": False, "error": f"Active skill '{skill_name}' not found."}

    @classmethod
    def enable_skill(cls, skill_name: str, workspace_root: Optional[str] = None) -> Dict[str, Any]:
        """Re-enables a previously `.disabled` skill directory."""
        target = skill_name.strip().lower()
        for sk in cls.list_skills(workspace_root):
            if not sk["disabled"]:
                continue
            if sk["name"].lower() == target or sk["dir_name"].lower() == target:
                src_path = sk["path"]
                if not src_path.endswith(".disabled"):
                    continue
                dst_path = src_path[:-9]
                if os.path.exists(dst_path):
                    return {"success": False, "error": f"Target '{dst_path}' already exists."}
                try:
                    os.rename(src_path, dst_path)
                    return {"success": True, "skill": sk["name"], "path": dst_path}
                except Exception as exc:
                    return {"success": False, "error": str(exc)}
        return {"success": False, "error": f"Disabled skill '{skill_name}' not found."}

    @classmethod
    def format_skills_report(cls, workspace_root: Optional[str] = None) -> str:
        """Formats a clean CLI list of active & disabled skills grouped by token overhead."""
        skills = cls.list_skills(workspace_root)
        active = [s for s in skills if not s["disabled"]]
        disabled = [s for s in skills if s["disabled"]]
        lines = [
            f"\n\033[1m\033[36m⚡ TOKENSAFE SKILLS INVENTORY ({len(active)} active, {len(disabled)} disabled) ⚡\033[0m\n",
            f"  \033[1mActive Context Overhead:\033[0m ~{len(active) * EST_TOKENS_PER_SKILL:,} tokens/turn (~{EST_TOKENS_PER_SKILL} tok/skill)\n",
        ]
        for s in active:
            tag = "\033[2m[builtin]\033[0m" if s["builtin"] else f"\033[2m[{os.path.basename(s['root'])}]\033[0m"
            desc = f" — {s['description']}" if s["description"] else ""
            lines.append(f"  \033[32m✔\033[0m \033[1m{s['name']:<28}\033[0m {tag}{desc}")
        if disabled:
            lines.append("\n  \033[2mDisabled Skills:\033[0m")
            for s in disabled:
                lines.append(f"  \033[2m○ {s['name']:<28} [{os.path.basename(s['root'])}]\033[0m")
        lines.append("\n  💡 Tip: Run \033[36mtokensafe skills disable <name>\033[0m (or \033[36mtokensafe skills enable <name>\033[0m) to manage overhead.\n")
        return "\n".join(lines)

    @classmethod
    def format_cli_report(cls, workspace_root: Optional[str] = None) -> str:
        """Formats visual CLI report for tokensafe doctor (pure static environment score)."""
        report = cls.audit(workspace_root)
        q_score: Dict[str, Any] = {}
        score_line = ""
        try:
            from src.quality_score import ContextQualityScore
            from src.governor import ContextBudget
            window = ContextBudget.resolve_window()
            static_overhead = (
                int(report.get("total_static_tokens_per_turn", 0) or 0)
                + int((report.get("ghost_tokens") or {}).get("ghost_tokens_per_turn", 0) or 0)
                + int((report.get("rules") or {}).get("total_tokens", 0) or 0)
            )
            used_pct = round(min(100.0, (static_overhead / window) * 100.0), 1) if window > 0 else 0.0
            static_budget = {
                "used_estimated": static_overhead,
                "window_tokens": window,
                "used_pct": used_pct,
            }
            q_score = ContextQualityScore.calculate(budget=static_budget, audit=report)
            color = "\033[32m" if q_score["grade"] in ("S", "A") else ("\033[33m" if q_score["grade"] == "B" else "\033[31m")
            score_line = (
                f"  \033[1mEnvironment Quality Score:\033[0m {color}\033[1mGrade {q_score['grade']} ({q_score['label']})\033[0m "
                f"— {q_score['headroom_pct']:.1f}% fresh-turn headroom, {q_score['score']}/100 pts\n"
            )
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
        if report["total_skills"] >= 15 and report.get("skills_breakdown"):
            for sdir, cnt in sorted(report["skills_breakdown"].items(), key=lambda kv: -kv[1])[:4]:
                lines.append(f"    \033[2m• {sdir}: {cnt} skill(s)\033[0m")
        if report["skills_tokens_per_turn"] >= 4000:
            lines.append("    \033[36m💡 Tip: Run 'tokensafe skills list' and 'tokensafe skills disable <name>' to trim unused skills\033[0m")

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

        config_issues = report.get("config_issues") or []
        if config_issues:
            lines.append("\n  \033[33m\033[1m⚠ Config / Schema Issues Detected:\033[0m")
            for ci in config_issues:
                lines.append(f"    • {ci['description']}")

        ghost = report.get("ghost_tokens") or {}
        if ghost.get("has_ghost_tokens"):
            lines.append(f"\n  \033[33m\033[1m👻 Ghost Tokens Detected:\033[0m ~{ghost['ghost_tokens_per_turn']:,} wasted tokens per turn")
            for gf in ghost.get("ghost_files", [])[:5]:
                lines.append(f"    • \033[1m{gf['file']}\033[0m (~{gf['tokens']:,} tokens): {gf['description']}")
            lines.append("    \033[36m💡 Tip: Consolidate redundant rule files into a single AGENTS.md\033[0m")
        else:
            lines.append("\n  \033[32m✔ No Ghost Tokens detected: Rule files and configs are lean.\033[0m")

        has_auto_fixable = bool(ghost.get("has_ghost_tokens") or config_issues)
        low_grade = q_score.get("grade") in ("C", "F")
        if report["has_critical_bloat"] or has_auto_fixable or rules.get("budget_exceeded") or low_grade:
            lines.append("\n  \033[33m\033[1mDiagnostic Recommendations:\033[0m")
            for b in report["bloated_servers"]:
                lines.append(f"    ➜ Run: \033[36mtokensafe mcp disable {b['name']}\033[0m to save ~{b['est_tokens_per_turn']:,} tokens per turn!")
            if has_auto_fixable:
                lines.append("    ➜ Run: \033[36mtokensafe doctor --fix\033[0m to repair configs & consolidate ghost rule duplicates (with backups)")
            if report["skills_tokens_per_turn"] >= 4000:
                lines.append(f"    ➜ Run: \033[36mtokensafe skills list\033[0m (currently {report['total_skills']} skills ≈ ~{report['skills_tokens_per_turn']:,} tok/turn; disable unused via \033[36mtokensafe skills disable <name>\033[0m)")
            if q_score.get("headroom_pct", 100.0) < 35.0:
                lines.append("    ➜ Run: \033[36mtokensafe budget\033[0m to inspect live session context usage")
        else:
            lines.append("\n  \033[32m✔ Environment healthy: No critical context bloat detected.\033[0m")

        lines.append("")
        return "\n".join(lines)
