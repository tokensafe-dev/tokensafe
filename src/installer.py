"""
Universal Interactive Agent Installer for TokenSafe.
Scans and automatically configures MCP integration for:
- Google Antigravity
- Claude Desktop
- Cursor
- Windsurf
- Cline / Roo Code (VS Code)
- OpenAI Codex
- Z.ai Code
"""

import os
import sys
import json
import hashlib
import shutil
import re
import shlex
import signal
import subprocess
import time
from typing import Dict, List, Any, Optional, Tuple

RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[32m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"

HOME = os.path.expanduser("~")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
MCP_SERVER_PATH = os.path.join(PROJECT_ROOT, "src", "mcp_server.py")
PYTHON_BIN = sys.executable

def _app_data_dir(*parts: str) -> str:
    if sys.platform == "darwin":
        base = os.path.join(HOME, "Library", "Application Support")
    elif sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.join(HOME, "AppData", "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")
    return os.path.join(base, *parts)

UNIFIED_TOKENSAFE_RULE = (
    "## TOKENSAFE GRAPH-FIRST OPTIMIZATION PROTOCOL\n"
    "Always use `tokensafe` MCP tools (`smart_read_file`, `scoped_search`, `apply_edits`, `repo_map`, `session_context`, `oumv`, `run_helper`) over default raw file reads or high-output terminal executions.\n\n"
    "1. **Zero Raw Grep/Find (Blocked & Replaced by `scoped_search`)**: Strictly NEVER execute terminal `grep`, `egrep`, `ripgrep`, or `find` on source code files or directories (`src/`, `views/`, `components/`, `.ts`, `.tsx`, `.py`, `.js`). Use `tokensafe:scoped_search(query, path='.', scope='code')` for single searches (`mode='auto'|'text'|'regex'|'glob'|'fuzzy'|'git'`) or `queries=[...]` for up to 20 concurrent queries in one call.\n"
    "2. **Mandatory Symbol, Bundle & External Type Inspection (`smart_read_file`)**: Always call `tokensafe:smart_read_file(path, symbol)` (default `dep_mode='signature'` folds inert code -95%+ tokens; pass `dep_mode='full'` only when needed, or `force_full=true` if a pointer hit references missing context). Pass `targets=[{path, symbol}]` to bundle 2-8 symbols across files in one call, or `package_name`+`symbol` for 3rd-party `.d.ts` interfaces. OpenAPI/Prisma/GraphQL/SQL schemas auto-slice via `symbol`.\n"
    "3. **Repeat Read Deduplication**: Never re-read unchanged files or symbols within the same session. Reuse the earlier skeleton or pointer hit (`// [TokenSafe: Pointer Hit ...]`).\n"
    "4. **Zero-Fluff Egress & Surgical Diffs**: Omit conversational filler. Reference line ranges (`file.ts#L10-L25`) and use `tokensafe:apply_edits` for multi-hunk/multi-file edits with optional inline `verify`.\n"
    "5. **Architecture Graph, API Surface & Directory Tree (`repo_map`)**: For any codebase inquiry or refactor, FIRST call `tokensafe:repo_map(view='graph', project_path=...)` for God Nodes (<250 tokens). Use `view='api'` for exported types/interfaces and `view='tree'` instead of `ls -la`.\n"
    "6. **Utilities (`run_helper`)**: Use `tokensafe:run_helper(action=...)` for rare/auxiliary operations: `'sanitize'` (rewrite shell/test commands), `'guard'` (pre-flight secret/safety audit), `'diff'` (slice diff), `'deps'` (lockfile point lookup), `'web'` (fetch clean Markdown), `'expand'` (restore `#hash`), `'image'` (downscale >1024px), and `'compress'` (TOCP prompt).\n"
    "7. **Compact Tests & Build Output**: Run noisy commands, builds, and test suites via `tokensafe wrap '<cmd>'` or `tokensafe tests [cmd]` (clean 1-line summary on pass, full trace on failure; never leave `vitest`/`jest`/`bun` in watch mode).\n"
    "8. **Styles & Design Tokens**: When inspecting CSS/SCSS/LESS/Tailwind, `smart_read_file` preserves design tokens (`:root`, `@theme`, CSS variables) in full.\n"
    "9. **Session & Context Hygiene (`session_context`)**: Call `tokensafe:session_context(action='budget', model=...)` before large tasks. On `warning`/`critical` or Grade C/F, call `action='compact'` (with `transcript_path`). Use `action='prune'` for superseded reads, `action='mistakes'` for anti-stuck memory, and `action='metrics'` for savings analytics.\n"
    "10. **Secret Leak Prevention (Zero Hardcoded Fallbacks)**: Never hardcode real private keys, API secrets, or webhook secrets as fallback arguments in `os.environ.get()`, `os.getenv()`, or `process.env`. Whenever blocked, state:\n"
    "`🛡️ **TokenSafe Security Guard**: Prevented secret / private key leak into git (<target_file>: <details>)`\n"
    "11. **Proof-of-Savings Turn Footer (strictly English)**: Append on tool-using turns:\n"
    "`⚡ **TokenSafe Turn Savings**: +<tokens> tokens saved on this turn (<breakdown in English>) | Cumulative Total: <total> tokens (≈$<usd> approx)`\n"
    "12. **Universal OUMV Protocol (`oumv`)**: When a workspace profile is OUMV-supported (React+Vite, Vue, Solid, Go, FastAPI), "
    "ALWAYS prefer `tokensafe:oumv(action='status'|'observe'|'understand'|'verify'|'mutate'|'graph', ...)` BEFORE full file crawls, raw curls, or browser checks — "
    "static OUMV works offline on read/edit tasks too, and `tokensafe dev` starts zero-footprint live Vite+React RPC without touching repo files. "
    "In mixed monorepos (e.g. React + Node.js), use `oumv` on supported profiles and `repo_map`/`smart_read_file`/`scoped_search` on unsupported stacks (e.g. `backend/node`).\n"
)

# Short pointer written into PROJECT-level rule files when the full protocol is
# already installed at the agent-global level (~/.zcode/AGENTS.md etc.).
# Keeps every session's meta-context lean: ~100 tokens instead of ~700.
POINTER_RULE = (
    "## TOKENSAFE GRAPH-FIRST OPTIMIZATION PROTOCOL\n"
    "The full protocol is installed globally in the agent-level instructions "
    "(~/.zcode/AGENTS.md, ~/.claude/CLAUDE.md, ~/.codex/AGENTS.md, ~/.kimi-code/AGENTS.md) and applies to this project. "
    "Core tools: `tokensafe:repo_map(view='graph'|'api'|'tree')` for architecture & directory trees, "
    "`tokensafe:scoped_search` (single or `queries` batch, never raw grep/find), `tokensafe:smart_read_file` (symbol, `targets` bundle, or `.d.ts` `package_name`), "
    "`tokensafe:apply_edits` for multi-file edits, `tokensafe:oumv` (MANDATORY first on OUMV-supported React/Vue/Go/FastAPI stacks — live or static offline; check `status` in mixed monorepos), "
    "`tokensafe:session_context(action='budget'|'compact'|'prune'|'mistakes')` for context & anti-stuck hygiene, and "
    "`tokensafe:run_helper(action='sanitize'|'guard'|'diff'|'deps'|'web'|'expand'|'image'|'compress')`; "
    "never hardcode secret fallbacks; append the `⚡ TokenSafe Turn Savings` footer on tool-using turns.\n"
)

AGENTS_REGISTRY = {
    "antigravity": {
        "name": "Google Antigravity",
        "type": "plugin",
        "target_path": os.path.join(HOME, ".gemini", "config", "plugins", "tokensafe"),
        "legacy_path": os.path.join(HOME, ".gemini", "config", "plugins", "agy-token-optimizer"),
        "detect_path": os.path.join(HOME, ".gemini")
    },
    "claude_desktop": {
        "name": "Claude Desktop",
        "type": "mcp_json",
        "config_path": _app_data_dir("Claude", "claude_desktop_config.json"),
        "detect_path": _app_data_dir("Claude")
    },
    "cursor": {
        "name": "Cursor",
        "type": "cursor_env",
        "config_path": os.path.join(HOME, ".cursor", "mcp.json"),
        "skills_path": os.path.join(HOME, ".cursor", "skills", "token-optimizer"),
        "rules_path": os.path.join(HOME, ".cursor", "rules", "tokensafe.mdc"),
        "extension_path": os.path.join(HOME, ".cursor", "extensions", "tokensafe-1.0.0"),
        "detect_path": os.path.join(HOME, ".cursor")
    },
    "windsurf": {
        "name": "Windsurf",
        "type": "mcp_json",
        "config_path": os.path.join(HOME, ".codeium", "windsurf", "mcp_config.json"),
        "rules_path": os.path.join(HOME, ".codeium", "windsurf", "memories", "global_rules.md"),
        "detect_path": os.path.join(HOME, ".codeium", "windsurf")
    },
    "cline": {
        "name": "Cline / Roo Code (VS Code)",
        "type": "mcp_json",
        "config_path": _app_data_dir("Code", "User", "globalStorage", "saoudrizwan.claude-dev", "settings", "cline_mcp_settings.json"),
        "rules_path": _app_data_dir("Code", "User", "globalStorage", "saoudrizwan.claude-dev", "settings", "Rule_TokenSafe.md"),
        "detect_path": _app_data_dir("Code", "User", "globalStorage", "saoudrizwan.claude-dev")
    },
    "claude_code": {
        "name": "Claude Code CLI",
        "type": "claude_md",
        "config_path": os.path.join(HOME, ".claude.json"),
        "agents_md_path": os.path.join(HOME, ".claude", "CLAUDE.md"),
        "skills_path": os.path.join(HOME, ".claude", "skills", "token-optimizer"),
        "detect_path": os.path.join(HOME, ".claude")
    },
    "codex": {
        "name": "OpenAI Codex",
        "type": "codex_toml",
        "config_path": os.path.join(HOME, ".codex", "config.toml"),
        "skills_path": os.path.join(HOME, ".codex", "skills", "token-optimizer"),
        "agents_md_path": os.path.join(HOME, ".codex", "AGENTS.md"),
        "plugin_path": os.path.join(HOME, ".codex", "plugins", "tokensafe"),
        "detect_path": os.path.join(HOME, ".codex")
    },
    "copilot": {
        "name": "GitHub Copilot",
        "type": "copilot_json",
        "config_path": os.path.join(HOME, ".copilot", "mcp-config.json"),
        "detect_path": os.path.join(HOME, ".copilot")
    },
    "zcode": {
        "name": "Z.ai Code",
        "type": "zcode_json",
        "config_path": os.path.join(HOME, ".zcode", "cli", "config.json"),
        "skills_path": os.path.join(HOME, ".zcode", "skills", "token-optimizer"),
        "plugin_path": os.path.join(HOME, ".zcode", "cli", "plugins", "tokensafe"),
        "agents_md_path": os.path.join(HOME, ".zcode", "AGENTS.md"),
        "detect_path": os.path.join(HOME, ".zcode")
    },
    "kimi": {
        "name": "Kimi Code",
        "type": "kimi_plugin",
        "config_path": os.path.join(HOME, ".kimi-code", "mcp.json"),
        "installed_json_path": os.path.join(HOME, ".kimi-code", "plugins", "installed.json"),
        "managed_plugin_path": os.path.join(HOME, ".kimi-code", "plugins", "managed", "tokensafe"),
        "skills_path": os.path.join(HOME, ".kimi-code", "skills", "token-optimizer"),
        "agents_md_path": os.path.join(HOME, ".kimi-code", "AGENTS.md"),
        "detect_path": os.path.join(HOME, ".kimi-code")
    }
}

_DEFAULT_DETECT_PATHS = {k: v["detect_path"] for k, v in AGENTS_REGISTRY.items()}


def _has_vscode_extension(prefixes: Tuple[str, ...]) -> bool:
    ext_root = os.path.join(HOME, ".vscode", "extensions")
    if os.path.isdir(ext_root):
        try:
            for entry in os.listdir(ext_root):
                low = entry.lower()
                if any(low.startswith(p) for p in prefixes):
                    return True
        except OSError:
            pass
    return False


class AgentInstaller:
    """Manages multi-agent detection and configuration injection."""

    _CODEX_SECTION_RE = re.compile(
        r'(?ms)^\[mcp_servers\.tokensafe\]\s*\n(?:(?!^\[{1,2}\s*(?!["\']?(?:/|[A-Za-z]:\\))[A-Za-z0-9_."\' -]+\]{1,2}\s*(?:#.*)?$).)*'
    )
    _CODEX_ORPHAN_LINE_RE = re.compile(r'(?m)^\s*\["[^"\n]*(?:mcp_server\.py|tokensafe[^"\n]*)"\]\s*\n?')

    @classmethod
    def _clean_codex_orphans(cls, content: str) -> str:
        """Removes orphaned array lines left by legacy Codex TOML regex bugs."""
        return cls._CODEX_ORPHAN_LINE_RE.sub("", content)

    @classmethod
    def _is_real_agent_installed(cls, key: str, info: Dict[str, Any]) -> bool:
        """Checks genuine IDE/CLI installation artifacts rather than empty/installer-created dirs."""
        detect_path = info.get("detect_path", "")
        if detect_path != _DEFAULT_DETECT_PATHS.get(key):
            return bool(detect_path and os.path.exists(detect_path))

        if key == "antigravity":
            return any((
                os.path.exists("/Applications/Antigravity.app"),
                os.path.exists("/Applications/Antigravity IDE.app"),
                os.path.exists(os.path.join(HOME, "Applications", "Antigravity.app")),
                os.path.exists(os.path.join(HOME, ".gemini", "antigravity")),
                os.path.exists(os.path.join(HOME, ".gemini", "antigravity-ide")),
                shutil.which("agy") is not None,
            ))
        if key == "claude_desktop":
            return any((
                os.path.exists("/Applications/Claude.app"),
                os.path.exists(os.path.join(HOME, "Applications", "Claude.app")),
                os.path.exists(_app_data_dir("Claude", "claude_desktop_config.json")),
                os.path.exists(_app_data_dir("Claude", "IndexedDB")),
                os.path.exists(_app_data_dir("Claude", "Session Storage")),
            ))
        if key == "cursor":
            if sys.platform == "darwin":
                return any((
                    os.path.exists("/Applications/Cursor.app"),
                    os.path.exists(os.path.join(HOME, "Applications", "Cursor.app")),
                    shutil.which("cursor") is not None,
                ))
            return any((
                os.path.exists(_app_data_dir("Cursor")),
                os.path.exists(os.path.join(HOME, ".cursor", "projects")),
                shutil.which("cursor") is not None,
            ))
        if key == "windsurf":
            if sys.platform == "darwin":
                return any((
                    os.path.exists("/Applications/Windsurf.app"),
                    os.path.exists(os.path.join(HOME, "Applications", "Windsurf.app")),
                    shutil.which("windsurf") is not None,
                ))
            return any((
                os.path.exists(_app_data_dir("Windsurf")),
                os.path.exists(os.path.join(HOME, ".codeium", "windsurf", "User")),
                os.path.exists(os.path.join(HOME, ".codeium", "windsurf", "globalStorage")),
                shutil.which("windsurf") is not None,
            ))
        if key == "cline":
            # Require the actual installed extension (not bare VS Code or deleted globalStorage).
            return _has_vscode_extension(("saoudrizwan.claude-dev", "rooveterinaryinc.roo-cline"))
        if key == "claude_code":
            return any((
                shutil.which("claude") is not None,
                os.path.exists(os.path.join(HOME, ".claude", "projects")),
                os.path.exists(os.path.join(HOME, ".claude", "statsig")),
                os.path.exists(os.path.join(HOME, ".claude", ".credentials.json")),
            ))
        if key == "codex":
            codex_dir = os.path.join(HOME, ".codex")
            has_logs = os.path.isdir(codex_dir) and any(
                n.startswith("logs") or n in ("auth.json", "sessions", "history.jsonl")
                for n in os.listdir(codex_dir)
            )
            return any((
                os.path.exists("/Applications/Codex.app"),
                os.path.exists(os.path.join(HOME, "Applications", "Codex.app")),
                shutil.which("codex") is not None,
                has_logs,
            ))
        if key == "copilot":
            return any((
                shutil.which("github-copilot-cli") is not None,
                shutil.which("copilot") is not None,
                os.path.exists(os.path.join(HOME, ".local", "share", "gh", "extensions", "gh-copilot")),
                _has_vscode_extension(("github.copilot",)),
            ))
        if key == "zcode":
            return any((
                os.path.exists("/Applications/ZCode.app"),
                os.path.exists(os.path.join(HOME, "Applications", "ZCode.app")),
                os.path.exists(os.path.join(HOME, ".zcode", "v2")),
                os.path.exists(os.path.join(HOME, ".zcode", "workspace")),
                shutil.which("zcode") is not None,
            ))
        if key == "kimi":
            return any((
                os.path.exists("/Applications/Kimi Code.app"),
                os.path.exists(os.path.join(HOME, "Applications", "Kimi Code.app")),
                os.path.exists(os.path.join(HOME, ".kimi-code", "sessions")),
                os.path.exists(os.path.join(HOME, ".kimi-code", "oauth")),
                shutil.which("kimi") is not None,
            ))
        return bool(detect_path and os.path.exists(detect_path))

    @classmethod
    def _is_agent_configured(cls, key: str, info: Dict[str, Any]) -> bool:
        """True if TokenSafe MCP/plugin is currently wired into this agent."""
        if info.get("type") == "plugin":
            t = info.get("target_path", "")
            return bool(t and os.path.exists(t))
        if key == "codex":
            p = info.get("config_path", "")
            if os.path.isfile(p):
                try:
                    return "[mcp_servers.tokensafe]" in open(p, "r", encoding="utf-8", errors="replace").read()
                except Exception:
                    return False
            return False
        if key == "zcode":
            p = info.get("config_path", "")
            if os.path.isfile(p):
                try:
                    d = json.load(open(p, "r", encoding="utf-8"))
                    return "tokensafe" in ((d.get("mcp") or {}).get("servers") or {})
                except Exception:
                    return False
            return False
        p = info.get("config_path", "")
        if os.path.isfile(p):
            try:
                d = json.load(open(p, "r", encoding="utf-8"))
                return "tokensafe" in (d.get("mcpServers") or {})
            except Exception:
                return False
        return False

    @classmethod
    def refresh_installed_agents(cls) -> Dict[str, Any]:
        """Re-synchronizes MCP, plugins, skills, rules, and hooks for genuinely installed or already-configured agents."""
        agents = cls.detect_available_agents()
        synced_count = 0
        details: Dict[str, Any] = {}
        for key, info in agents.items():
            if info.get("installed") or cls._is_agent_configured(key, info):
                ok, msg = cls.install_mcp_config(key)
                if ok:
                    h_ok, h_msg = cls.install_hooks_config(key)
                    if h_ok:
                        msg = f"{msg}; {h_msg}"
                    synced_count += 1
                details[key] = {"name": info["name"], "success": ok, "message": msg}
        return {"synced_count": synced_count, "agents": details}

    @classmethod
    def detect_available_agents(cls) -> Dict[str, Dict[str, Any]]:
        """Scans filesystem to find which AI agent platforms are genuinely installed."""
        detected = {}
        for key, info in AGENTS_REGISTRY.items():
            detected[key] = {
                **info,
                "installed": cls._is_real_agent_installed(key, info)
            }
        return detected

    @staticmethod
    def _tokensafe_hook_command(script_name: str) -> str:
        """Shell command string for a bundled hook adapter (space-safe)."""
        script = os.path.join(PROJECT_ROOT, "hooks", script_name)
        return f"{shlex.quote(PYTHON_BIN)} {shlex.quote(script)}"

    @staticmethod
    def _load_json_with_backup(config_file: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Reads a JSON config file after creating a .backup copy.
        If parsing fails or root is non-dict, refuses to overwrite existing data."""
        if not os.path.exists(config_file):
            return {}, None
        try:
            shutil.copy2(config_file, f"{config_file}.backup")
        except Exception:
            pass
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return None, f"Existing JSON config '{config_file}' is not an object; left untouched (.backup saved)."
            return data, None
        except Exception as e:
            return None, f"Could not parse existing JSON config '{config_file}' ({e}); left untouched (.backup saved)."

    @staticmethod
    def _atomic_write_json(config_file: str, data: Dict[str, Any]) -> None:
        parent = os.path.dirname(config_file)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = f"{config_file}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, config_file)

    @classmethod
    def install_hooks_config(cls, agent_key: str) -> Tuple[bool, str]:
        """Registers TokenSafe lifecycle-hook adapters with agents that support them.
        Claude Code: SessionStart (one-shot directives) + UserPromptSubmit (TOCP
        injection) + PreToolUse (shell guard) + PostToolUse (stdout compaction).
        Cursor: beforeShellExecution / beforeReadFile guards.
        Codex has no lifecycle hooks (MCP + AGENTS.md remain the integration)."""
        if agent_key == "codex":
            return False, "Codex CLI has no lifecycle hooks — MCP + AGENTS.md stay the integration"
        if agent_key not in ("claude_code", "cursor", "kimi", "zcode"):
            return False, "hooks not applicable for this agent"

        claude_cmd = cls._tokensafe_hook_command("claude_code_hook.py")
        cursor_cmd = cls._tokensafe_hook_command("cursor_hook.py")
        kimi_cmd = cls._tokensafe_hook_command("kimi_hook.py")

        if agent_key == "kimi":
            config_file = os.path.join(HOME, ".kimi-code", "config.toml")
            os.makedirs(os.path.dirname(config_file), exist_ok=True)
            content = ""
            if os.path.exists(config_file):
                try:
                    shutil.copy2(config_file, f"{config_file}.backup")
                except Exception:
                    pass
                try:
                    with open(config_file, "r", encoding="utf-8") as f:
                        content = f.read()
                except Exception:
                    pass

            toml_hook_cmd = json.dumps(kimi_cmd)

            def _kimi_section(event: str, matcher: str = "") -> str:
                m = f'matcher = "{matcher}"\n' if matcher else ""
                return f'[[hooks]]\nevent = "{event}"\n{m}command = {toml_hook_cmd}\ntimeout = 10\n'

            if "kimi_hook.py" in content:
                # Refresh command path/interpreter on upgrade + add missing events.
                migrated = False
                new_content, cmd_replacements = re.subn(
                    r'(?m)^command\s*=\s*".*kimi_hook\.py[^"\n]*"\s*$',
                    f'command = {toml_hook_cmd}',
                    content,
                )
                if new_content != content and cmd_replacements > 0:
                    content = new_content
                    migrated = True
                if 'event = "PostToolUse"' not in content:
                    content = content.rstrip() + "\n\n# TokenSafe PostToolUse stdout compaction (migration)\n" + _kimi_section("PostToolUse")
                    migrated = True
                if 'matcher = "Edit"' not in content:
                    content = content.rstrip() + "\n\n# TokenSafe PreToolUse Edit guard (migration)\n" + _kimi_section("PreToolUse", "Edit")
                    migrated = True
                if 'event = "Stop"' not in content:
                    content = content.rstrip() + "\n\n# TokenSafe Stop telemetry/fold (migration)\n" + _kimi_section("Stop")
                    migrated = True
                if not migrated:
                    return True, "TokenSafe Kimi Code hooks already registered in config.toml"
                with open(config_file, "w", encoding="utf-8") as f:
                    f.write(content)
                return True, "Kimi Code hooks updated in ~/.kimi-code/config.toml (Edit/PostToolUse/Stop)"

            hook_sections = (
                '\n# TokenSafe Lifecycle Hooks\n'
                + _kimi_section("SessionStart") + "\n"
                + _kimi_section("UserPromptSubmit") + "\n"
                + _kimi_section("PreToolUse", "Bash") + "\n"
                + _kimi_section("PreToolUse", "Write") + "\n"
                + _kimi_section("PreToolUse", "Edit") + "\n"
                + _kimi_section("PreToolUse", "Read") + "\n"
                + _kimi_section("PostToolUse") + "\n"
                + _kimi_section("Stop")
            )
            content = (content.rstrip() + "\n" + hook_sections) if content.strip() else hook_sections.lstrip()
            with open(config_file, "w", encoding="utf-8") as f:
                f.write(content)
            return True, "hooks registered in ~/.kimi-code/config.toml (SessionStart + UserPromptSubmit + PreToolUse Bash/Write/Read + PostToolUse compact)"

        if agent_key == "claude_code":
            config_file = os.path.join(HOME, ".claude", "settings.json")
            os.makedirs(os.path.dirname(config_file), exist_ok=True)
            data, err = cls._load_json_with_backup(config_file)
            if err or data is None:
                return False, err or f"Failed to parse {config_file}"
            hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
            prompt_entry = {"hooks": [{"type": "command", "command": claude_cmd, "timeout": 10}]}
            bash_entry = {"matcher": "Bash", "hooks": [{"type": "command", "command": claude_cmd, "timeout": 10}]}
            write_entry = {"matcher": "Write", "hooks": [{"type": "command", "command": claude_cmd, "timeout": 10}]}
            read_entry = {"matcher": "Read", "hooks": [{"type": "command", "command": claude_cmd, "timeout": 10}]}
            session_entry = {"hooks": [{"type": "command", "command": claude_cmd, "timeout": 10}]}
            post_bash_entry = {"matcher": "Bash", "hooks": [{"type": "command", "command": claude_cmd, "timeout": 10}]}
            # Stale-edit guard: PreToolUse asks on externally-changed targets;
            # PostToolUse snapshots the on-disk hash after every read/write.
            stale_edit_entry = {"matcher": "Edit|MultiEdit", "hooks": [{"type": "command", "command": claude_cmd, "timeout": 10}]}
            post_track_entry = {"matcher": "Read|Edit|MultiEdit|Write", "hooks": [{"type": "command", "command": claude_cmd, "timeout": 10}]}
            # Fold-at-rest: Stop hook prunes superseded reads from the idle
            # transcript (opt-in via TOKENSAFE_FOLD_AT_REST=1; the hook itself
            # is a no-op while the flag is off).
            stop_entry = {"hooks": [{"type": "command", "command": claude_cmd, "timeout": 15}]}

            def _sync_claude_event(event_name: str, default_entry: dict) -> bool:
                entries = hooks.get(event_name)
                if not isinstance(entries, list):
                    hooks[event_name] = [default_entry]
                    return True
                default_matcher = str(default_entry.get("matcher") or "")
                seen = False
                dirty = False
                for entry in entries:
                    if isinstance(entry, dict) and isinstance(entry.get("hooks"), list):
                        for h in entry["hooks"]:
                            if isinstance(h, dict) and "claude_code_hook.py" in str(h.get("command", "")):
                                # Matcher-aware sync: a legacy entry with a
                                # DIFFERENT matcher covers only its own tool
                                # set (e.g. PostToolUse "Write|Edit|MultiEdit"
                                # never fires for Read), so the canonical
                                # default entry must still be appended.
                                if str(entry.get("matcher") or "") == default_matcher:
                                    seen = True
                                    if h.get("command") != claude_cmd:
                                        h["command"] = claude_cmd
                                        dirty = True
                if not seen:
                    entries.append(default_entry)
                    dirty = True
                return dirty

            changed = any([
                _sync_claude_event("SessionStart", session_entry),
                _sync_claude_event("UserPromptSubmit", prompt_entry),
                _sync_claude_event("PreToolUse", bash_entry),
                _sync_claude_event("PreToolUse", write_entry),
                _sync_claude_event("PreToolUse", read_entry),
                _sync_claude_event("PreToolUse", stale_edit_entry),
                _sync_claude_event("PostToolUse", post_bash_entry),
                _sync_claude_event("PostToolUse", post_track_entry),
                _sync_claude_event("Stop", stop_entry),
            ])
            if not changed:
                return True, "TokenSafe Claude Code hooks already registered"
            data["hooks"] = hooks
            cls._atomic_write_json(config_file, data)
            return True, "hooks registered (SessionStart + UserPromptSubmit + PreToolUse guard/write/stale + PostToolUse compact/tracking + Stop fold-at-rest)"

        if agent_key == "zcode":
            config_file = os.path.join(HOME, ".zcode", "cli", "config.json")
            data, err = cls._load_json_with_backup(config_file)
            if err or data is None:
                return False, err or f"Failed to parse {config_file}"
            # ZCode configuration-file hooks are disabled by default and only run
            # with enabled=true. Register the dedicated zcode_hook.py adapter
            # (Claude-Code-compatible PreToolUse/PostToolUse/SessionStart contract
            # + live burn alerts) and upgrade any legacy Antigravity script entries.
            hooks_block = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
            hooks_block["enabled"] = True
            events = hooks_block.get("events") if isinstance(hooks_block.get("events"), dict) else {}

            legacy_scripts = ("session_start.py", "pre_invocation.py", "pre_tool_use.py", "post_tool_use.py", "stop_hook.py")
            expected = os.path.join(PROJECT_ROOT, "hooks", "zcode_hook.py")

            def _zcode_entry() -> dict:
                return {"hooks": [{
                    "type": "process",
                    "command": PYTHON_BIN,
                    "args": [expected],
                    "timeoutMs": 10000,
                }]}

            def _sync_zcode_event(event_name: str) -> bool:
                entry = _zcode_entry()
                entries = events.get(event_name)
                if not isinstance(entries, list):
                    events[event_name] = [entry]
                    return True
                seen = False
                dirty = False
                for e in entries:
                    if isinstance(e, dict) and isinstance(e.get("hooks"), list):
                        for h in e["hooks"]:
                            args = h.get("args") if isinstance(h, dict) else None
                            if not isinstance(args, list):
                                continue
                            if any(isinstance(a, str) and (a.endswith("zcode_hook.py") or any(a.endswith(s) for s in legacy_scripts)) for a in args):
                                seen = True
                                if args[-1] != expected or h.get("command") != PYTHON_BIN:
                                    h["command"] = PYTHON_BIN
                                    args[-1] = expected
                                    dirty = True
                if not seen:
                    entries.append(entry)
                    dirty = True
                return dirty

            changed = any([
                _sync_zcode_event("SessionStart"),
                _sync_zcode_event("UserPromptSubmit"),
                _sync_zcode_event("PreToolUse"),
                _sync_zcode_event("PostToolUse"),
                _sync_zcode_event("Stop"),
            ])
            if changed:
                hooks_block["events"] = events
                data["hooks"] = hooks_block
                cls._atomic_write_json(config_file, data)
                return True, "ZCode hooks registered in ~/.zcode/cli/config.json (SessionStart + UserPromptSubmit + PreToolUse guard + PostToolUse compact + Stop) — restart the app to take effect"
            return True, "TokenSafe ZCode hooks already registered"

        # cursor
        config_file = os.path.join(HOME, ".cursor", "hooks.json")
        os.makedirs(os.path.dirname(config_file), exist_ok=True)
        data, err = cls._load_json_with_backup(config_file)
        if err or data is None:
            return False, err or f"Failed to parse {config_file}"
        hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
        shell_entry = {"command": cursor_cmd, "timeout": 10}

        def _sync_cursor_event(event_name: str) -> bool:
            entries = hooks.get(event_name)
            if not isinstance(entries, list):
                hooks[event_name] = [dict(shell_entry)]
                return True
            seen = False
            dirty = False
            for entry in entries:
                if isinstance(entry, dict) and "cursor_hook.py" in str(entry.get("command", "")):
                    seen = True
                    if entry.get("command") != cursor_cmd:
                        entry["command"] = cursor_cmd
                        dirty = True
            if not seen:
                entries.append(dict(shell_entry))
                dirty = True
            return dirty

        changed = any([
            _sync_cursor_event("beforeShellExecution"),
            _sync_cursor_event("beforeReadFile"),
            _sync_cursor_event("beforeEditFile"),
            _sync_cursor_event("beforeSubmitPrompt"),
            _sync_cursor_event("afterFileEdit"),
            _sync_cursor_event("stop"),
        ])
        if not changed:
            return True, "TokenSafe Cursor hooks already registered"
        data["hooks"] = hooks
        cls._atomic_write_json(config_file, data)
        return True, "guards registered (shell + junk-read + prompt pipeline)"

    @classmethod
    def uninstall_hooks_config(cls, agent_key: str) -> Tuple[bool, str]:
        """Removes TokenSafe lifecycle hooks from agents that support them."""
        if agent_key not in ("claude_code", "cursor", "kimi", "zcode"):
            return True, ""

        if agent_key == "kimi":
            config_file = os.path.join(HOME, ".kimi-code", "config.toml")
            if not os.path.exists(config_file):
                return True, "Kimi Code config.toml not found"
            try:
                shutil.copy2(config_file, f"{config_file}.backup")
            except Exception:
                pass
            try:
                with open(config_file, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                return False, f"Could not read {config_file}"

            sections = re.split(r'(?m)^(?=\[\[hooks\]\])', content)
            keep = []
            removed = False
            for s in sections:
                if "kimi_hook.py" in s:
                    removed = True
                    continue
                keep.append(s)

            new_content = "".join(keep)
            new_content = re.sub(r'(?m)^[ \t]*#[ \t]*TokenSafe[^\n]*\n?', '', new_content)
            cleaned = new_content.strip()
            if not cleaned:
                try:
                    os.remove(config_file)
                    return True, "Removed hooks from ~/.kimi-code/config.toml (file removed)"
                except Exception:
                    pass

            with open(config_file, "w", encoding="utf-8") as f:
                f.write(cleaned + "\n" if cleaned else "")
            return True, "Removed TokenSafe hooks from ~/.kimi-code/config.toml" if removed else "No TokenSafe hooks in config.toml"

        if agent_key == "claude_code":
            config_file = os.path.join(HOME, ".claude", "settings.json")
            if not os.path.exists(config_file):
                return True, "Claude Code settings.json not found"
            data, err = cls._load_json_with_backup(config_file)
            if err or data is None:
                return False, err or f"Failed to parse {config_file}"
            hooks = data.get("hooks")
            if not isinstance(hooks, dict):
                return True, "No hooks in Claude Code settings.json"

            changed = False
            for event_name in list(hooks.keys()):
                entries = hooks.get(event_name)
                if not isinstance(entries, list):
                    continue
                new_entries = []
                for entry in entries:
                    if isinstance(entry, dict) and isinstance(entry.get("hooks"), list):
                        remaining = [
                            h for h in entry["hooks"]
                            if not (isinstance(h, dict) and "claude_code_hook.py" in str(h.get("command", "")))
                        ]
                        if len(remaining) != len(entry["hooks"]):
                            changed = True
                        if remaining:
                            entry["hooks"] = remaining
                            new_entries.append(entry)
                    else:
                        new_entries.append(entry)
                if new_entries:
                    hooks[event_name] = new_entries
                else:
                    hooks.pop(event_name, None)
                    changed = True

            if changed:
                if not hooks:
                    data.pop("hooks", None)
                else:
                    data["hooks"] = hooks
                cls._atomic_write_json(config_file, data)
                return True, "Removed TokenSafe hooks from Claude Code settings.json"
            return True, "No TokenSafe hooks found in Claude Code settings.json"

        if agent_key == "zcode":
            config_file = os.path.join(HOME, ".zcode", "cli", "config.json")
            if not os.path.exists(config_file):
                return True, "ZCode config.json not found"
            data, err = cls._load_json_with_backup(config_file)
            if err or data is None:
                return False, err or f"Failed to parse {config_file}"
            hooks_block = data.get("hooks")
            if not isinstance(hooks_block, dict):
                return True, "No hooks in ZCode config.json"
            events = hooks_block.get("events")
            if not isinstance(events, dict):
                return True, "No events in ZCode hooks"

            tokensafe_scripts = ("zcode_hook.py", "session_start.py", "pre_invocation.py", "pre_tool_use.py", "post_tool_use.py", "stop_hook.py")
            changed = False
            for event_name in list(events.keys()):
                entries = events.get(event_name)
                if not isinstance(entries, list):
                    continue
                new_entries = []
                for entry in entries:
                    if isinstance(entry, dict) and isinstance(entry.get("hooks"), list):
                        remaining = []
                        for h in entry["hooks"]:
                            args = h.get("args") if isinstance(h, dict) else []
                            is_ts = False
                            if isinstance(args, list):
                                for a in args:
                                    if isinstance(a, str) and (any(a.endswith(s) for s in tokensafe_scripts) or "tokensafe" in a):
                                        is_ts = True
                                        break
                            if not is_ts:
                                remaining.append(h)
                            else:
                                changed = True
                        if remaining:
                            entry["hooks"] = remaining
                            new_entries.append(entry)
                    else:
                        new_entries.append(entry)
                if new_entries:
                    events[event_name] = new_entries
                else:
                    events.pop(event_name, None)
                    changed = True

            if changed:
                if not events:
                    hooks_block.pop("events", None)
                    if list(hooks_block.keys()) == ["enabled"]:
                        data.pop("hooks", None)
                    else:
                        data["hooks"] = hooks_block
                else:
                    hooks_block["events"] = events
                    data["hooks"] = hooks_block
                cls._atomic_write_json(config_file, data)
                return True, "Removed TokenSafe hooks from ZCode config.json"
            return True, "No TokenSafe hooks found in ZCode config.json"

        # cursor
        config_file = os.path.join(HOME, ".cursor", "hooks.json")
        if not os.path.exists(config_file):
            return True, "Cursor hooks.json not found"
        data, err = cls._load_json_with_backup(config_file)
        if err or data is None:
            return False, err or f"Failed to parse {config_file}"
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            return True, "No hooks in Cursor hooks.json"

        changed = False
        for event_name in list(hooks.keys()):
            entries = hooks.get(event_name)
            if not isinstance(entries, list):
                continue
            new_entries = [
                e for e in entries
                if not (isinstance(e, dict) and "cursor_hook.py" in str(e.get("command", "")))
            ]
            if len(new_entries) != len(entries):
                changed = True
            if new_entries:
                hooks[event_name] = new_entries
            else:
                hooks.pop(event_name, None)
                changed = True

        if changed:
            if not hooks:
                try:
                    os.remove(config_file)
                except Exception:
                    cls._atomic_write_json(config_file, {})
            else:
                data["hooks"] = hooks
                cls._atomic_write_json(config_file, data)
            return True, "Removed TokenSafe guards from Cursor hooks.json"
        return True, "No TokenSafe guards found in Cursor hooks.json"

    # ------------------------------------------------------------------
    # Proxy auto-wiring (v3.0.0): daemon + per-agent base URL override with
    # one-command rollback. Only agents that CAN be wired programmatically
    # are touched (Claude Code via settings.json env); everything else gets
    # printed manual steps instead of silent failure.
    # ------------------------------------------------------------------

    PROXY_PORT = 8765
    PROXY_BASE_URL = f"http://127.0.0.1:{PROXY_PORT}"
    PROXY_ENV_KEY = "ANTHROPIC_BASE_URL"
    _PLIST_LABEL = "dev.tokensafe.proxy"
    _SERVICE_NAME = "tokensafe-proxy"
    _WIRING_BACKUP = "proxy_wiring_backup.json"
    # ZCode desktop provider registry (separate from the CLI's ~/.zcode/cli/config.json
    # which only holds mcp/plugins): model providers live in the v2 config.
    ZCODE_V2_CONFIG = os.path.join(HOME, ".zcode", "v2", "config.json")
    _ZCODE_CLONE_SUFFIX = "-tokensafe"

    @classmethod
    def proxy_health(cls, timeout: float = 1.5) -> bool:
        """True when the local proxy answers /health."""
        try:
            import urllib.request
            with urllib.request.urlopen(f"{cls.PROXY_BASE_URL}/health", timeout=timeout) as resp:
                return resp.status == 200
        except Exception:
            return False

    @classmethod
    def _wiring_backup_path(cls) -> str:
        return os.path.join(HOME, ".tokensafe", "data", cls._WIRING_BACKUP)

    @classmethod
    def wire_proxy_config(cls) -> Tuple[bool, str]:
        """Points Claude Code at the local proxy via settings.json env.
        Backs up the previous value for `tokensafe proxy off`."""
        if not cls.proxy_health():
            return False, "proxy is not running on 127.0.0.1:8765 — start it first (`tokensafe proxy on` installs a daemon)"
        config_file = os.path.join(HOME, ".claude", "settings.json")
        os.makedirs(os.path.dirname(config_file), exist_ok=True)
        data, err = cls._load_json_with_backup(config_file)
        if err or data is None:
            return False, err or f"Failed to parse {config_file}"
        env = data.get("env") if isinstance(data.get("env"), dict) else {}
        previous = env.get(cls.PROXY_ENV_KEY)
        if previous == cls.PROXY_BASE_URL:
            return True, "Claude Code already points at the local proxy"
        # Persist rollback info before mutating user config
        try:
            backup_path = cls._wiring_backup_path()
            os.makedirs(os.path.dirname(backup_path), exist_ok=True)
            backup = {}
            if os.path.exists(backup_path):
                try:
                    with open(backup_path, "r", encoding="utf-8") as f:
                        backup = json.load(f)
                except Exception:
                    backup = {}
            backup.setdefault("claude_code", {})["ANTHROPIC_BASE_URL"] = previous
            with open(backup_path, "w", encoding="utf-8") as f:
                json.dump(backup, f)
        except Exception:
            pass
        env[cls.PROXY_ENV_KEY] = cls.PROXY_BASE_URL
        data["env"] = env
        cls._atomic_write_json(config_file, data)
        return True, "Claude Code wired: ANTHROPIC_BASE_URL=http://127.0.0.1:8765 (rollback: tokensafe proxy off)"

    @classmethod
    def unwire_proxy_config(cls) -> Tuple[bool, str]:
        """Removes the proxy override and restores the pre-wiring value."""
        config_file = os.path.join(HOME, ".claude", "settings.json")
        if not os.path.exists(config_file):
            return True, "nothing to unwire"
        data, err = cls._load_json_with_backup(config_file)
        if err or data is None:
            return False, err or f"Failed to parse {config_file}"
        env = data.get("env") if isinstance(data.get("env"), dict) else {}
        if env.get(cls.PROXY_ENV_KEY) != cls.PROXY_BASE_URL:
            return True, "Claude Code was not wired to the proxy"
        restored = None
        try:
            backup_path = cls._wiring_backup_path()
            if os.path.exists(backup_path):
                with open(backup_path, "r", encoding="utf-8") as f:
                    backup = json.load(f)
                restored = (backup.get("claude_code") or {}).get("ANTHROPIC_BASE_URL")
        except Exception:
            restored = None
        if restored:
            env[cls.PROXY_ENV_KEY] = restored
        else:
            env.pop(cls.PROXY_ENV_KEY, None)
        if env:
            data["env"] = env
        else:
            data.pop("env", None)
        cls._atomic_write_json(config_file, data)
        return True, "Claude Code unwired (previous base URL restored)"

    # ---- Multi-agent proxy wiring (one daemon, per-key upstream routes) ----

    @staticmethod
    def _proxy_routes_path() -> str:
        try:
            from src.paths import resolve_data_dir
            return os.path.join(resolve_data_dir(), "proxy_routes.json")
        except Exception:
            return os.path.join(HOME, ".tokensafe", "data", "proxy_routes.json")

    @classmethod
    def _register_proxy_route(cls, label: str, upstream_url: str, api_key: str) -> bool:
        """Registers a key-fingerprint -> upstream route so the proxy forwards
        this agent's traffic to the provider the key belongs to. Only the key
        FINGERPRINT is stored — the key itself never touches the routes file."""
        if not api_key or not upstream_url:
            return False
        try:
            fp = hashlib.sha256(api_key.strip().encode("utf-8")).hexdigest()[:16]
            path = cls._proxy_routes_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            routes: Dict[str, Any] = {}
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        routes = json.load(f)
                except Exception:
                    routes = {}
            if not isinstance(routes, dict):
                routes = {}
            routes[fp] = {"upstream_url": upstream_url, "label": label, "added_at": int(time.time())}
            # Atomic write: a truncated routes file makes the proxy silently fall
            # back to its default upstream (the live 400-incident class).
            tmp_path = f"{path}.tmp.{os.getpid()}"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(routes, f, indent=2)
            os.replace(tmp_path, path)
            return True
        except Exception as e:
            try:
                sys.stderr.write(f"[TokenSafe] WARN: proxy route registration failed ({label}): {e}\n")
            except Exception:
                pass
            return False

    @classmethod
    def _wire_zcode_provider(cls) -> Tuple[bool, str]:
        """Points the ZCode desktop app at the local proxy by ADDING a clone of the
        currently enabled provider entry (baseURL swapped, apiKey kept). The original
        entry is only disabled, never mutated — rollback removes the clone and
        re-enables the original. Requires an app restart to take effect."""
        if not cls.proxy_health():
            return (False, "proxy is not running on 127.0.0.1:8765 — start it first")
        if not os.path.isfile(cls.ZCODE_V2_CONFIG):
            return (False, f"{cls.ZCODE_V2_CONFIG} not found — ZCode desktop provider registry missing")
        data, err = cls._load_json_with_backup(cls.ZCODE_V2_CONFIG)
        if err or not isinstance(data, dict):
            return (False, err or f"Failed to parse {cls.ZCODE_V2_CONFIG}")
        providers = data.get("provider")
        if not isinstance(providers, dict):
            return (False, "no provider entries in the ZCode v2 config")

        # Fast path: a proxy clone already exists. Re-assert its flags (app updates
        # may re-enable the original entry), but respect a deliberate provider
        # switch (user enabled a different provider while the clone was disabled).
        existing_clone_key = next(
            (k for k, v in providers.items() if isinstance(v, dict) and k.endswith(cls._ZCODE_CLONE_SUFFIX)),
            None,
        )
        if existing_clone_key:
            clone = providers[existing_clone_key]
            base_ok = str((clone.get("options") or {}).get("baseURL", "")).rstrip("/") == cls.PROXY_BASE_URL
            origin = providers.get(existing_clone_key[: -len(cls._ZCODE_CLONE_SUFFIX)])
            other_enabled = any(
                k
                for k, v in providers.items()
                if isinstance(v, dict)
                and v.get("enabled") is True
                and k != existing_clone_key
                and not k.endswith(cls._ZCODE_CLONE_SUFFIX)
            )
            if base_ok and clone.get("enabled") is True and isinstance(origin, dict) and origin.get("enabled") is not True and not other_enabled:
                return (True, "ZCode already wired to the local proxy (provider clone present)")
            # Drift after an app update OR deliberate switch — drop the clone and
            # re-derive the wiring from whatever the user has enabled now.
            del providers[existing_clone_key]

        enabled_key = next(
            (
                k
                for k, v in providers.items()
                if isinstance(v, dict) and v.get("enabled") is True and not k.endswith(cls._ZCODE_CLONE_SUFFIX)
            ),
            None,
        )
        if not enabled_key:
            return (False, "no enabled provider entry found in the ZCode v2 config")
        src = providers[enabled_key]
        opts = src.get("options") if isinstance(src.get("options"), dict) else {}
        api_key = str(opts.get("apiKey") or "")
        base_url = str(opts.get("baseURL") or "").rstrip("/")
        if not api_key or not base_url:
            return (False, f"enabled provider '{enabled_key}' lacks apiKey/baseURL")
        clone_key = f"{enabled_key}{cls._ZCODE_CLONE_SUFFIX}"
        # Anthropic-kind upstreams need the full messages endpoint on the route.
        # Append for a bare host (https://api.anthropic.com) or an /anthropic
        # mirror base (https://api.z.ai/api/anthropic); leave other paths alone.
        upstream = base_url
        if str(src.get("kind") or "").lower() == "anthropic" and not upstream.endswith("/v1/messages"):
            after_host = upstream.split("://", 1)[-1]
            path = "/" + after_host.split("/", 1)[1] if "/" in after_host else ""
            if not path or path.rstrip("/").endswith("/anthropic"):
                upstream = f"{upstream.rstrip('/')}/v1/messages"
        route_ok = cls._register_proxy_route(f"zcode:{enabled_key}", upstream, api_key)
        clone = json.loads(json.dumps(src))
        clone["enabled"] = True
        clone["name"] = f"{src.get('name') or enabled_key} — TokenSafe Proxy"
        if not isinstance(clone.get("options"), dict):
            clone["options"] = {}
        clone["options"]["baseURL"] = cls.PROXY_BASE_URL
        providers[clone_key] = clone
        src["enabled"] = False
        cls._atomic_write_json(cls.ZCODE_V2_CONFIG, data)
        msg = (
            f"ZCode wired: provider clone '{clone_key}' -> {cls.PROXY_BASE_URL} "
            f"(original '{enabled_key}' disabled; rollback: tokensafe proxy off; "
            f"restart the ZCode app to pick up the new provider)"
        )
        if not route_ok:
            msg += " [WARN: upstream route registration failed — proxy will use its default upstream]"
        return (True, msg)

    @classmethod
    def _unwire_zcode_provider(cls) -> Tuple[bool, str]:
        """Removes the TokenSafe provider clone and re-enables the original entry."""
        if not os.path.isfile(cls.ZCODE_V2_CONFIG):
            return (True, "ZCode v2 config not found — nothing to unwire")
        data, err = cls._load_json_with_backup(cls.ZCODE_V2_CONFIG)
        if err or not isinstance(data, dict):
            return (False, err or f"Failed to parse {cls.ZCODE_V2_CONFIG}")
        providers = data.get("provider")
        if not isinstance(providers, dict):
            return (True, "ZCode not wired (no provider entries)")
        clone_keys = [k for k, v in providers.items() if isinstance(v, dict) and k.endswith(cls._ZCODE_CLONE_SUFFIX)]
        if not clone_keys:
            return (True, "ZCode not wired (no provider clone present)")
        for ck in clone_keys:
            origin_key = ck[: -len(cls._ZCODE_CLONE_SUFFIX)]
            del providers[ck]
            origin = providers.get(origin_key)
            # Only re-enable the original when nothing else is enabled — if the
            # user deliberately switched to another provider while wired, we must
            # not end up with two enabled providers after `proxy off`.
            other_enabled = any(
                k
                for k, v in providers.items()
                if isinstance(v, dict) and v.get("enabled") is True and k != origin_key
            )
            if isinstance(origin, dict) and origin.get("enabled") is not True and not other_enabled:
                origin["enabled"] = True
        cls._atomic_write_json(cls.ZCODE_V2_CONFIG, data)
        return (True, "ZCode unwired: clone removed (restart the app)")

    @classmethod
    def install_proxy_daemon(cls) -> Tuple[bool, str]:
        """Keeps `tokensafe proxy` running: launchd (macOS) / systemd user unit (Linux)."""
        python_bin = sys.executable or PYTHON_BIN
        if sys.platform == "darwin":
            launch_dir = os.path.join(HOME, "Library", "LaunchAgents")
            os.makedirs(launch_dir, exist_ok=True)
            plist_path = os.path.join(launch_dir, f"{cls._PLIST_LABEL}.plist")
            log_path = os.path.join(HOME, ".tokensafe", "data", "proxy.log")
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{cls._PLIST_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python_bin}</string>
    <string>-m</string>
    <string>src.proxy</string>
    <string>{cls.PROXY_PORT}</string>
  </array>
  <key>WorkingDirectory</key><string>{PROJECT_ROOT}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log_path}</string>
  <key>StandardErrorPath</key><string>{log_path}</string>
</dict>
</plist>
"""
            with open(plist_path, "w", encoding="utf-8") as f:
                f.write(plist)
            subprocess.run(["launchctl", "unload", plist_path], capture_output=True)
            res = subprocess.run(["launchctl", "load", "-w", plist_path], capture_output=True, text=True)
            if res.returncode != 0:
                return False, f"launchctl load failed: {(res.stderr or '').strip()[:160]}"
            return True, f"launchd daemon installed ({plist_path})"
        if sys.platform.startswith("linux"):
            unit_dir = os.path.join(HOME, ".config", "systemd", "user")
            os.makedirs(unit_dir, exist_ok=True)
            unit_path = os.path.join(unit_dir, f"{cls._SERVICE_NAME}.service")
            unit = f"""[Unit]
Description=TokenSafe Local AI Proxy
After=network.target

[Service]
ExecStart={python_bin} -m src.proxy {cls.PROXY_PORT}
WorkingDirectory={PROJECT_ROOT}
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
"""
            with open(unit_path, "w", encoding="utf-8") as f:
                f.write(unit)
            for cmd in (["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", cls._SERVICE_NAME]):
                res = subprocess.run(cmd, capture_output=True, text=True)
                if res.returncode != 0:
                    return False, f"systemctl failed: {(res.stderr or '').strip()[:160]}"
            return True, f"systemd user service installed ({unit_path})"
        return False, "no daemon support on this OS — run `tokensafe proxy` manually in a terminal"

    @classmethod
    def remove_proxy_daemon(cls) -> Tuple[bool, str]:
        if sys.platform == "darwin":
            plist_path = os.path.join(HOME, "Library", "LaunchAgents", f"{cls._PLIST_LABEL}.plist")
            if not os.path.exists(plist_path):
                return True, "no daemon installed"
            subprocess.run(["launchctl", "unload", plist_path], capture_output=True)
            os.remove(plist_path)
            return True, "launchd daemon removed"
        if sys.platform.startswith("linux"):
            unit_path = os.path.join(HOME, ".config", "systemd", "user", f"{cls._SERVICE_NAME}.service")
            if not os.path.exists(unit_path):
                return True, "no daemon installed"
            subprocess.run(["systemctl", "--user", "disable", "--now", cls._SERVICE_NAME], capture_output=True)
            os.remove(unit_path)
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
            return True, "systemd user service removed"
        return True, "no daemon support on this OS"

    @classmethod
    def proxy_on(cls) -> Dict[str, Any]:
        """One command: daemon + wiring. Returns a structured report."""
        report: Dict[str, Any] = {"daemon": None, "wiring": None, "health": False, "manual_steps": []}
        d_ok, d_msg = cls.install_proxy_daemon()
        report["daemon"] = {"ok": d_ok, "message": d_msg}
        # Give the daemon a moment to boot before wiring agents to it.
        for _ in range(6):
            if cls.proxy_health():
                break
            time.sleep(0.8)
        report["health"] = cls.proxy_health()
        if d_ok and not report["health"]:
            report["wiring"] = {"ok": False, "message": "daemon installed but /health did not answer — check ~/.tokensafe/data/proxy.log; agents NOT rewired"}
            return report
        if report["health"]:
            w_ok, w_msg = cls.wire_proxy_config()
            report["wiring"] = {"ok": w_ok, "message": w_msg}
            z_ok, z_msg = cls._wire_zcode_provider()
            report["wiring_zcode"] = {"ok": z_ok, "message": z_msg}
        else:
            report["wiring"] = {"ok": False, "message": "proxy not reachable; agents NOT rewired"}
        report["manual_steps"] = [
            "Cursor: Settings → Models → add OpenAI-compatible model with base URL http://127.0.0.1:8765/v1",
            "Cline/Aider: set OpenAI-compatible base URL to http://127.0.0.1:8765/v1",
            "Codex: add a model_providers entry in ~/.codex/config.toml with base_url http://127.0.0.1:8765/v1",
        ]
        return report

    @classmethod
    def proxy_off(cls) -> Dict[str, Any]:
        report: Dict[str, Any] = {}
        u_ok, u_msg = cls.unwire_proxy_config()
        report["unwired"] = {"ok": u_ok, "message": u_msg}
        z_ok, z_msg = cls._unwire_zcode_provider()
        report["unwired_zcode"] = {"ok": z_ok, "message": z_msg}
        r_ok, r_msg = cls.remove_proxy_daemon()
        report["daemon"] = {"ok": r_ok, "message": r_msg}
        return report

    @classmethod
    def proxy_status(cls) -> Dict[str, Any]:
        env_present = None
        settings_path = os.path.join(HOME, ".claude", "settings.json")
        try:
            if os.path.exists(settings_path):
                with open(settings_path, "r", encoding="utf-8") as f:
                    env = (json.load(f) or {}).get("env") or {}
                    env_present = env.get(cls.PROXY_ENV_KEY)
        except Exception:
            pass
        heartbeat_age = None
        try:
            from src.paths import resolve_data_dir
            marker = os.path.join(resolve_data_dir(), ".proxy_active")
            if os.path.exists(marker):
                with open(marker, "r", encoding="utf-8") as f:
                    heartbeat_age = int(time.time()) - int(f.read().strip() or 0)
        except Exception:
            pass
        return {
            "health": cls.proxy_health(),
            "claude_code_wired": env_present == cls.PROXY_BASE_URL,
            "claude_code_env_value": env_present,
            "heartbeat_age_seconds": heartbeat_age,
            "daemon": sys.platform,
        }

    @classmethod
    def _ensure_proxy_default(cls, context: str = "install", fresh_bin: Optional[str] = None) -> None:
        """Ultimate default-on proxy assertion for install/update flows: TokenSafe
        is the primary LLM route for every wire-able agent. Daemon is installed and
        agents are wired WITHOUT asking (the old interactive prompt is retired).
        Emergency brake: TOKENSAFE_PROXY_DISABLED=1 leaves everything untouched.
        Fail-open: a proxy problem never breaks install/update itself."""
        try:
            if os.environ.get("TOKENSAFE_PROXY_DISABLED", "").strip().lower() in ("1", "true", "yes"):
                print(f"  {DIM}ℹ TokenSafe proxy left off (TOKENSAFE_PROXY_DISABLED is set). Enable anytime: tokensafe proxy on{RESET}")
                return
            if fresh_bin:
                # Re-assert via the freshly upgraded binary so the daemon plist
                # points at the new engine path, not this (old) process.
                proc = subprocess.run(
                    [fresh_bin, "proxy", "on"],
                    env=cls._npm_subprocess_env(fresh_bin), capture_output=True, text=True, timeout=30,
                )
                # Surface the subreport: swallowed wiring/route WARNs previously
                # made a failed wire look like a success on the update path.
                out = (proc.stdout or "").strip()
                if out:
                    print(out)
                if proc.returncode != 0:
                    err = (proc.stderr or "").strip()[:300]
                    if err:
                        print(f"  {YELLOW}⚠{RESET} proxy on reported a problem: {err}")
            else:
                cls.proxy_on()
            wired_ok = cls.wire_proxy_config()[0]
            zcode_ok = cls._wire_zcode_provider()[0]
            if cls.proxy_health(timeout=3.0):
                if wired_ok and zcode_ok:
                    print(f"  {GREEN}✔{RESET} TokenSafe proxy daemon active — Claude Code / ZCode routed through the local optimizer.")
                else:
                    print(f"  {YELLOW}⚠{RESET} TokenSafe proxy daemon active, but agent wiring is incomplete — run {BOLD}tokensafe proxy on{RESET} for details.")
                print(f"    {DIM}Rollback: tokensafe proxy off{RESET}")
            else:
                print(f"  {YELLOW}⚠{RESET} TokenSafe proxy did not answer — run {BOLD}tokensafe proxy on{RESET} to retry.")
        except Exception:
            pass  # fail-open: a proxy problem never breaks install/update

    @classmethod
    def maybe_prompt_account_login(cls, context: str = "install") -> None:
        """
        Interactive prompt to connect TokenSafe account and unlock 500k free quota + web dashboard.
        Migrates anonymous community@tokensafe.dev and guest installations to verified user accounts.
        Features a resilient /dev/tty fallback for piped 'curl | bash' installations and an
        intelligent one-more-chance nudge on decline.
        Fail-open: skips quietly on non-interactive environments (CI, scripts).
        """
        try:
            from src.license_manager import LicenseManager
            tier, lic = LicenseManager.get_active_license()
            email = lic.get("email", "")

            # If user already has a real personal account (not the legacy anonymous key), nothing to prompt
            if email and email != "community@tokensafe.dev":
                return

            if os.environ.get("TOKENSAFE_NONINTERACTIVE", "").strip() in ("1", "true", "yes"):
                return
            if "unittest" in sys.modules and not os.environ.get("TOKENSAFE_TEST_PROMPT"):
                return

            has_tty = sys.stdin.isatty() or (os.path.exists("/dev/tty") and sys.stdout.isatty())
            if not has_tty:
                print(f"  {CYAN}ℹ{RESET} Run {BOLD}tokensafe login{RESET} anytime to activate your Community license & web dashboard.\n")
                return

            def _read_input(prompt_text: str) -> str:
                sys.stdout.write(prompt_text)
                sys.stdout.flush()
                if sys.stdin.isatty():
                    return sys.stdin.readline().strip()
                if os.path.exists("/dev/tty"):
                    with open("/dev/tty", "r") as tty_in:
                        return tty_in.readline().strip()
                raise EOFError("No interactive TTY")

            if context == "update":
                used_tokens = 0
                try:
                    from src.metrics import MetricsTracker
                    quota = LicenseManager.check_quota(MetricsTracker())
                    used_tokens = int(quota.get("monthly_saved", 0))
                except Exception:
                    used_tokens = 0
                pct = min(100.0, (used_tokens / 50_000) * 100) if 50_000 > 0 else 0.0
                try:
                    from src.version_checker import CURRENT_VERSION
                    ver_label = f"v{CURRENT_VERSION}"
                except Exception:
                    ver_label = ""
                header_text = f"⚡ TOKENSAFE UPDATED {ver_label} ⚡".strip()

                print(f"\n{BOLD}{CYAN}╔════════════════════════════════════════════════════════════════════╗{RESET}")
                print(f"{BOLD}{CYAN}║{header_text.center(68)}║{RESET}")
                print(f"{BOLD}{CYAN}╚════════════════════════════════════════════════════════════════════╝{RESET}\n")
                print(f"  • Current Status:  Local Guest (50,000 tokens/month)  [{GREEN}Active Now{RESET}]")
                print(f"  • Monthly Usage:   {used_tokens:,} / 50,000 tokens used ({pct:.1f}%)\n")
                print("  Connect your free account to unlock:")
                print("  ✔ 500,000 free tokens every month (10x boost)")
                print("  ✔ Cloud Web Dashboard & live analytics (tokensafe.dev/dashboard)")
                print("  ✔ Full code search & AST slicing across all AI agents\n")

                answer = _read_input(f"  {CYAN}→ Activate your free 500k Community license now? [{GREEN}Y{RESET}/n]: {RESET}").lower()
                if answer in ("", "y", "yes"):
                    from src.cli import main as cli_main
                    cli_main(["login"])
                else:
                    # Strict 1-step polite decline (no nagging on update)
                    print(f"\n  {CYAN}ℹ{RESET} Continuing in Guest Mode (50,000 tokens/mo limit).")
                    print(f"    Run {BOLD}tokensafe login{RESET} anytime in your terminal to unlock 500,000 free tokens/mo!\n")
                return

            print(f"\n{BOLD}{CYAN}╔════════════════════════════════════════════════════════════════════╗{RESET}")
            print(f"{BOLD}{CYAN}║                  ⚡ TOKENSAFE TIERS & LIMITS                      ║{RESET}")
            print(f"{BOLD}{CYAN}╚════════════════════════════════════════════════════════════════════╝{RESET}\n")
            print(f"  • Local Guest (No account):    50,000 tokens/month  [{GREEN}Active Now{RESET}]")
            print(f"  • Free Community Account:     500,000 tokens/month  ({CYAN}10x Boost + Dashboard{RESET})\n")
            print("  Authorizing takes 5 seconds (opens your browser) and gives you:")
            print("  ✔ 500,000 free tokens every month")
            print("  ✔ Cloud Web Dashboard & live analytics (tokensafe.dev/dashboard)")
            print("  ✔ Full code search (scoped_search) & AST slicing\n")

            answer = _read_input(f"  {CYAN}→ Activate your free 500k Community license now? [{GREEN}Y{RESET}/n]: {RESET}").lower()
            if answer in ("", "y", "yes"):
                from src.cli import main as cli_main
                cli_main(["login"])
            else:
                # Intelligent One-More-Chance nudge highlighting quick exhaustion of guest quota
                print(f"\n  {YELLOW}⚠️  Are you sure?{RESET} In Guest mode, optimizations pause after only 50,000 tokens (~1-2 days of coding).")
                second_chance = _read_input(f"  {CYAN}→ Connect your free account now for 500,000 tokens/month? [{GREEN}y{RESET}/N]: {RESET}").lower()
                if second_chance in ("y", "yes"):
                    from src.cli import main as cli_main
                    cli_main(["login"])
                else:
                    print(f"\n  {CYAN}ℹ{RESET} Continuing in Guest Mode (50,000 tokens/mo limit).")
                    print(f"    Run {BOLD}tokensafe login{RESET} anytime in your terminal to unlock 500,000 free tokens/mo!\n")
        except (EOFError, KeyboardInterrupt):
            print()
            return
        except Exception:
            return

    @classmethod
    def install_mcp_config(cls, agent_key: str) -> Tuple[bool, str]:
        """Injects TokenSafe MCP config into target agent."""
        info = AGENTS_REGISTRY.get(agent_key)
        if not info:
            return False, f"Unknown agent: {agent_key}"

        if info["type"] == "plugin":
            # Symlink the plugin under its canonical name only. Antigravity keys
            # plugin enable-state by directory name (plugins.md), so a second
            # legacy-named symlink registers the plugin twice and runs every
            # hook twice per turn. Remove only stale legacy SYMLINKS — a real
            # directory there is user data and must not be touched.
            target = info["target_path"]
            cls._symlink_dir(PROJECT_ROOT, target)
            legacy = info.get("legacy_path")
            if legacy:
                try:
                    if os.path.islink(legacy):
                        os.unlink(legacy)
                except Exception:
                    pass
            # Once the plugin declares its own mcp_config.json (loaded by
            # Antigravity together with the plugin), a leftover user-level
            # tokensafe entry would register the MCP server twice. Migrate it
            # away — Unix only, the plugin-root sh shim is not cmd-compatible.
            migrated = False
            if os.name != "nt" and os.path.isfile(os.path.join(PROJECT_ROOT, "mcp_config.json")):
                migrated = cls._remove_user_level_tokensafe_mcp()
            msg = f"Symlinked plugin into {target}"
            if migrated:
                msg += "; migrated user-level MCP entry to the plugin-declared server"
            return True, msg

        elif info["type"] == "cursor_env":
            config_file = info["config_path"]
            os.makedirs(os.path.dirname(config_file), exist_ok=True)

            data, err = cls._load_json_with_backup(config_file)
            if err or data is None:
                return False, err or f"Failed to parse {config_file}"

            if "mcpServers" not in data or not isinstance(data["mcpServers"], dict):
                data["mcpServers"] = {}

            data["mcpServers"]["tokensafe"] = {
                "command": PYTHON_BIN,
                "args": [MCP_SERVER_PATH]
            }
            data["mcpServers"].pop("tokensafe-mcp", None)

            cls._atomic_write_json(config_file, data)

            if "skills_path" in info:
                cls._symlink_skill(info["skills_path"])

            if "extension_path" in info:
                ext_src = os.path.join(PROJECT_ROOT, "extensions", "cursor-tokensafe")
                if os.path.exists(ext_src):
                    cls._symlink_dir(ext_src, info["extension_path"])

            if "rules_path" in info:
                rule_text = (
                    "---\n"
                    "description: TokenSafe token economy, graph-first navigation and context optimization protocol\n"
                    "globs: *\n"
                    "alwaysApply: true\n"
                    "---\n\n"
                    + UNIFIED_TOKENSAFE_RULE
                )
                cls._write_file_safely(info["rules_path"], rule_text)
                cursorrules = os.path.join(HOME, ".cursorrules")
                cls._inject_global_rule(cursorrules, UNIFIED_TOKENSAFE_RULE)

            return True, f"Configured MCP, Extension & Rule for Cursor"

        elif info["type"] == "mcp_json":
            config_file = info["config_path"]
            os.makedirs(os.path.dirname(config_file), exist_ok=True)

            data, err = cls._load_json_with_backup(config_file)
            if err or data is None:
                return False, err or f"Failed to parse {config_file}"

            if "mcpServers" not in data or not isinstance(data["mcpServers"], dict):
                data["mcpServers"] = {}

            data["mcpServers"]["tokensafe"] = {
                "command": PYTHON_BIN,
                "args": [MCP_SERVER_PATH]
            }
            data["mcpServers"].pop("tokensafe-mcp", None)

            cls._atomic_write_json(config_file, data)

            if "skills_path" in info:
                cls._symlink_skill(info["skills_path"])

            if "rules_path" in info:
                cls._inject_global_rule(info["rules_path"], UNIFIED_TOKENSAFE_RULE)

            return True, f"Updated MCP configuration at {config_file}"

        elif info["type"] == "codex_toml":
            config_file = info["config_path"]
            os.makedirs(os.path.dirname(config_file), exist_ok=True)

            content = ""
            if os.path.exists(config_file):
                try:
                    shutil.copy2(config_file, f"{config_file}.backup")
                except Exception:
                    pass
                try:
                    with open(config_file, "r", encoding="utf-8") as f:
                        content = f.read()
                except Exception:
                    pass

            toml_cmd = json.dumps(PYTHON_BIN)
            toml_srv = json.dumps(MCP_SERVER_PATH)
            server_section = (
                f'[mcp_servers.tokensafe]\n'
                f'command = {toml_cmd}\n'
                f'args = [{toml_srv}]\n'
            )

            content = cls._clean_codex_orphans(content)
            if re.search(r'(?m)^\[mcp_servers\.tokensafe\]\s*$', content):
                # Replace the entire section up to the next valid TOML table header or EOF (never stop at array items like ["/path/..."]).
                content = cls._CODEX_SECTION_RE.sub(lambda _: server_section + "\n", content)
            else:
                content = (content.rstrip() + "\n\n" + server_section) if content.strip() else server_section

            with open(config_file, "w", encoding="utf-8") as f:
                f.write(content)

            if "skills_path" in info:
                cls._symlink_skill(info["skills_path"])

            if "plugin_path" in info:
                cls._symlink_dir(PROJECT_ROOT, info["plugin_path"])

            if "agents_md_path" in info:
                cls._inject_global_rule(info["agents_md_path"], UNIFIED_TOKENSAFE_RULE)

            return True, f"Configured MCP, Plugin, Skill & AGENTS.md in {config_file}"

        elif info["type"] == "claude_md":
            if "config_path" in info:
                config_file = info["config_path"]
                data, err = cls._load_json_with_backup(config_file)
                if err or data is None:
                    return False, err or f"Failed to parse {config_file}"
                if "mcpServers" not in data or not isinstance(data["mcpServers"], dict):
                    data["mcpServers"] = {}
                data["mcpServers"]["tokensafe"] = {
                    "command": PYTHON_BIN,
                    "args": [MCP_SERVER_PATH]
                }
                data["mcpServers"].pop("tokensafe-mcp", None)
                cls._atomic_write_json(config_file, data)

            if "agents_md_path" in info:
                cls._inject_global_rule(info["agents_md_path"], UNIFIED_TOKENSAFE_RULE)

            if "skills_path" in info:
                cls._symlink_skill(info["skills_path"])

            return True, f"Configured MCP, Rule & Skill in {info.get('agents_md_path')}"

        elif info["type"] == "copilot_json":
            config_file = info["config_path"]
            os.makedirs(os.path.dirname(config_file), exist_ok=True)
            data, err = cls._load_json_with_backup(config_file)
            if err or data is None:
                return False, err or f"Failed to parse {config_file}"
            if "mcpServers" not in data or not isinstance(data["mcpServers"], dict):
                data["mcpServers"] = {}
            data["mcpServers"]["tokensafe"] = {
                "type": "stdio",
                "command": PYTHON_BIN,
                "args": [MCP_SERVER_PATH],
                "tools": ["*"]
            }
            data["mcpServers"].pop("tokensafe-mcp", None)
            cls._atomic_write_json(config_file, data)
            return True, f"Configured MCP for GitHub Copilot in {config_file}"

        elif info["type"] == "zcode_json":
            config_file = info["config_path"]
            os.makedirs(os.path.dirname(config_file), exist_ok=True)

            data, err = cls._load_json_with_backup(config_file)
            if err or data is None:
                return False, err or f"Failed to parse {config_file}"

            if "mcp" not in data or not isinstance(data["mcp"], dict):
                data["mcp"] = {}
            if "servers" not in data["mcp"] or not isinstance(data["mcp"]["servers"], dict):
                data["mcp"]["servers"] = {}

            data["mcp"]["servers"]["tokensafe"] = {
                "command": PYTHON_BIN,
                "args": [MCP_SERVER_PATH]
            }
            data["mcp"]["servers"].pop("tokensafe-mcp", None)

            cls._atomic_write_json(config_file, data)

            if "skills_path" in info:
                cls._symlink_skill(info["skills_path"])

            if "plugin_path" in info:
                cls._symlink_dir(PROJECT_ROOT, info["plugin_path"])

            if "agents_md_path" in info:
                cls._inject_global_rule(info["agents_md_path"], UNIFIED_TOKENSAFE_RULE)

            return True, f"Configured MCP, Plugin, Skill & AGENTS.md in {config_file}"

        elif info["type"] == "kimi_plugin":
            mcp_file = info.get("config_path")
            if mcp_file:
                os.makedirs(os.path.dirname(mcp_file), exist_ok=True)
                data, err = cls._load_json_with_backup(mcp_file)
                if err or data is None:
                    data = {}
                if "mcpServers" not in data or not isinstance(data["mcpServers"], dict):
                    data["mcpServers"] = {}
                data["mcpServers"]["tokensafe"] = {
                    "command": PYTHON_BIN,
                    "args": [MCP_SERVER_PATH]
                }
                data["mcpServers"].pop("tokensafe-mcp", None)
                cls._atomic_write_json(mcp_file, data)

            managed_plugin_path = info.get("managed_plugin_path")
            if managed_plugin_path:
                cls._symlink_dir(PROJECT_ROOT, managed_plugin_path)

            installed_json = info.get("installed_json_path")
            if installed_json:
                os.makedirs(os.path.dirname(installed_json), exist_ok=True)
                inst_data, err = cls._load_json_with_backup(installed_json)
                if err or inst_data is None:
                    inst_data = {"version": 1, "plugins": []}
                if "plugins" not in inst_data or not isinstance(inst_data["plugins"], list):
                    inst_data["plugins"] = []

                import datetime
                iso_now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                plugin_root = managed_plugin_path or PROJECT_ROOT

                entry_found = False
                for p in inst_data["plugins"]:
                    if isinstance(p, dict) and p.get("id") == "tokensafe":
                        p["root"] = plugin_root
                        p["enabled"] = True
                        p["updatedAt"] = iso_now
                        p["originalSource"] = PROJECT_ROOT
                        p["iconUrl"] = "https://tokensafe.dev/ts-logo-black-256.png"
                        p["logo"] = "https://tokensafe.dev/ts-logo-black-256.png"
                        entry_found = True
                        break
                if not entry_found:
                    inst_data["plugins"].append({
                        "id": "tokensafe",
                        "root": plugin_root,
                        "enabled": True,
                        "installedAt": iso_now,
                        "updatedAt": iso_now,
                        "originalSource": PROJECT_ROOT,
                        "origin": "custom",
                        "iconUrl": "https://tokensafe.dev/ts-logo-black-256.png",
                        "logo": "https://tokensafe.dev/ts-logo-black-256.png"
                    })
                cls._atomic_write_json(installed_json, inst_data)

            if "skills_path" in info:
                cls._symlink_skill(info["skills_path"])

            if "agents_md_path" in info:
                cls._inject_global_rule(info["agents_md_path"], UNIFIED_TOKENSAFE_RULE)

            return True, f"Configured MCP, Plugin, Skill & AGENTS.md for Kimi Code"

        return False, "Unsupported type"

    @staticmethod
    def _remove_path_safely(target_path: Optional[str]) -> bool:
        """Safely removes a file, symlink, or directory. Returns True if removed."""
        if not target_path:
            return False
        if os.path.islink(target_path) or os.path.isfile(target_path):
            try:
                os.unlink(target_path)
                return True
            except Exception:
                return False
        elif os.path.isdir(target_path):
            try:
                shutil.rmtree(target_path)
                return True
            except Exception:
                return False
        return False

    @classmethod
    def uninstall_mcp_config(cls, agent_key: str) -> Tuple[bool, str]:
        """Removes TokenSafe MCP server, skills, rules, and plugins from the target agent."""
        info = AGENTS_REGISTRY.get(agent_key)
        if not info:
            return False, f"Unknown agent: {agent_key}"

        if info["type"] == "plugin":
            target = info["target_path"]
            legacy = info.get("legacy_path")
            r1 = cls._remove_path_safely(target)
            r2 = False
            if legacy and os.path.islink(legacy):
                r2 = cls._remove_path_safely(legacy)
            r3 = cls._remove_user_level_tokensafe_mcp()
            if r1 or r2 or r3:
                return True, f"Removed TokenSafe plugin from Antigravity ({target})"
            return True, "TokenSafe plugin not found in Antigravity"

        elif info["type"] == "cursor_env":
            config_file = info["config_path"]
            if os.path.exists(config_file):
                data, err = cls._load_json_with_backup(config_file)
                if not err and data and isinstance(data.get("mcpServers"), dict):
                    mutated = False
                    if "tokensafe" in data["mcpServers"]:
                        data["mcpServers"].pop("tokensafe")
                        mutated = True
                    if "tokensafe-mcp" in data["mcpServers"]:
                        data["mcpServers"].pop("tokensafe-mcp")
                        mutated = True
                    if mutated:
                        cls._atomic_write_json(config_file, data)

            cls._remove_path_safely(info.get("skills_path"))
            cls._remove_path_safely(info.get("extension_path"))
            cls._remove_path_safely(info.get("rules_path"))
            cls._remove_global_rule(os.path.join(HOME, ".cursorrules"))
            return True, "Removed MCP, Extension & Rules for Cursor"

        elif info["type"] == "mcp_json":
            config_file = info["config_path"]
            if os.path.exists(config_file):
                data, err = cls._load_json_with_backup(config_file)
                if not err and data and isinstance(data.get("mcpServers"), dict):
                    mutated = False
                    if "tokensafe" in data["mcpServers"]:
                        data["mcpServers"].pop("tokensafe")
                        mutated = True
                    if "tokensafe-mcp" in data["mcpServers"]:
                        data["mcpServers"].pop("tokensafe-mcp")
                        mutated = True
                    if mutated:
                        cls._atomic_write_json(config_file, data)

            if "skills_path" in info:
                cls._remove_path_safely(info["skills_path"])

            if "rules_path" in info:
                if info["rules_path"].endswith("Rule_TokenSafe.md"):
                    cls._remove_path_safely(info["rules_path"])
                else:
                    cls._remove_global_rule(info["rules_path"])

            return True, f"Removed MCP & Rules from {config_file}"

        elif info["type"] == "codex_toml":
            config_file = info["config_path"]
            if os.path.exists(config_file):
                try:
                    with open(config_file, "r", encoding="utf-8") as f:
                        content = f.read()
                    if re.search(r'(?m)^\[mcp_servers\.tokensafe\]\s*$', content) or cls._CODEX_ORPHAN_LINE_RE.search(content):
                        shutil.copy2(config_file, f"{config_file}.backup")
                        content = cls._clean_codex_orphans(content)
                        content = cls._CODEX_SECTION_RE.sub("", content)
                        with open(config_file, "w", encoding="utf-8") as f:
                            f.write(content.strip() + "\n" if content.strip() else "")
                except Exception:
                    pass

            if "skills_path" in info:
                cls._remove_path_safely(info["skills_path"])

            if "plugin_path" in info:
                cls._remove_path_safely(info["plugin_path"])

            if "agents_md_path" in info:
                cls._remove_global_rule(info["agents_md_path"])

            return True, f"Removed MCP, Plugin, Skill & AGENTS.md for Codex ({config_file})"

        elif info["type"] == "claude_md":
            if "config_path" in info and os.path.exists(info["config_path"]):
                config_file = info["config_path"]
                data, err = cls._load_json_with_backup(config_file)
                if not err and data and isinstance(data.get("mcpServers"), dict):
                    mutated = False
                    if "tokensafe" in data["mcpServers"]:
                        data["mcpServers"].pop("tokensafe")
                        mutated = True
                    if "tokensafe-mcp" in data["mcpServers"]:
                        data["mcpServers"].pop("tokensafe-mcp")
                        mutated = True
                    if mutated:
                        cls._atomic_write_json(config_file, data)

            if "agents_md_path" in info:
                cls._remove_global_rule(info["agents_md_path"])

            if "skills_path" in info:
                cls._remove_path_safely(info["skills_path"])

            return True, f"Removed MCP, Rule & Skill for Claude Code ({info.get('agents_md_path')})"

        elif info["type"] == "copilot_json":
            config_file = info["config_path"]
            if os.path.exists(config_file):
                data, err = cls._load_json_with_backup(config_file)
                if not err and data and isinstance(data.get("mcpServers"), dict):
                    mutated = False
                    if "tokensafe" in data["mcpServers"]:
                        data["mcpServers"].pop("tokensafe")
                        mutated = True
                    if "tokensafe-mcp" in data["mcpServers"]:
                        data["mcpServers"].pop("tokensafe-mcp")
                        mutated = True
                    if mutated:
                        cls._atomic_write_json(config_file, data)
            return True, f"Removed MCP for GitHub Copilot in {config_file}"

        elif info["type"] == "zcode_json":
            config_file = info["config_path"]
            if os.path.exists(config_file):
                data, err = cls._load_json_with_backup(config_file)
                if not err and data:
                    mcp = data.get("mcp")
                    if isinstance(mcp, dict) and isinstance(mcp.get("servers"), dict):
                        mutated = False
                        if "tokensafe" in mcp["servers"]:
                            mcp["servers"].pop("tokensafe")
                            mutated = True
                        if "tokensafe-mcp" in mcp["servers"]:
                            mcp["servers"].pop("tokensafe-mcp")
                            mutated = True
                        if mutated:
                            cls._atomic_write_json(config_file, data)

            if "skills_path" in info:
                cls._remove_path_safely(info["skills_path"])

            if "plugin_path" in info:
                cls._remove_path_safely(info["plugin_path"])

            if "agents_md_path" in info:
                cls._remove_global_rule(info["agents_md_path"])

            return True, f"Removed MCP, Plugin, Skill & AGENTS.md for Z.ai Code ({config_file})"

        elif info["type"] == "kimi_plugin":
            mcp_file = info.get("config_path")
            if mcp_file and os.path.exists(mcp_file):
                data, err = cls._load_json_with_backup(mcp_file)
                if not err and data and isinstance(data.get("mcpServers"), dict):
                    mutated = False
                    if "tokensafe" in data["mcpServers"]:
                        data["mcpServers"].pop("tokensafe")
                        mutated = True
                    if "tokensafe-mcp" in data["mcpServers"]:
                        data["mcpServers"].pop("tokensafe-mcp")
                        mutated = True
                    if mutated:
                        cls._atomic_write_json(mcp_file, data)

            managed_plugin_path = info.get("managed_plugin_path")
            if managed_plugin_path:
                cls._remove_path_safely(managed_plugin_path)

            installed_json = info.get("installed_json_path")
            if installed_json and os.path.exists(installed_json):
                inst_data, err = cls._load_json_with_backup(installed_json)
                if not err and inst_data and isinstance(inst_data.get("plugins"), list):
                    inst_data["plugins"] = [
                        p for p in inst_data["plugins"]
                        if not (isinstance(p, dict) and p.get("id") == "tokensafe")
                    ]
                    cls._atomic_write_json(installed_json, inst_data)

            if "skills_path" in info:
                cls._remove_path_safely(info["skills_path"])

            if "agents_md_path" in info:
                cls._remove_global_rule(info["agents_md_path"])

            return True, "Removed MCP, Plugin, Skill & AGENTS.md for Kimi Code"

    @staticmethod
    def _remove_user_level_tokensafe_mcp() -> bool:
        """Removes the tokensafe MCP entry from Antigravity's user-level
        mcp_config.json files (backup first). Returns True if any entry was
        removed. Entries under disabledMcpServers are left alone: the user
        explicitly disabled the server there."""
        removed = False
        for config_path in (
            os.path.join(HOME, ".gemini", "config", "mcp_config.json"),
            os.path.join(HOME, ".gemini", "antigravity", "mcp_config.json"),
        ):
            if not os.path.isfile(config_path):
                continue
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                servers = data.get("mcpServers")
                if isinstance(servers, dict) and "tokensafe" in servers:
                    shutil.copy2(config_path, f"{config_path}.backup")
                    servers.pop("tokensafe")
                    with open(config_path, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2)
                    removed = True
            except Exception:
                continue
        return removed

    @classmethod
    def _symlink_dir(cls, source_path: str, target_path: str):
        """Safely creates or replaces a symlink to a directory (falls back to copytree if symlinks fail)."""
        if not os.path.exists(source_path):
            return
        parent = os.path.dirname(target_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # Remove previous link or directory cleanly
        if os.path.islink(target_path) or os.path.exists(target_path):
            try:
                if os.path.islink(target_path) or os.path.isfile(target_path):
                    os.unlink(target_path)
                else:
                    shutil.rmtree(target_path)
            except Exception:
                pass
        try:
            os.symlink(source_path, target_path)
        except Exception:
            try:
                shutil.copytree(source_path, target_path, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules", "*.pyc"))
            except Exception:
                pass

    @classmethod
    def _write_file_safely(cls, file_path: str, content: str):
        """Safely writes content creating parent directories."""
        parent = os.path.dirname(file_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

    @classmethod
    def _inject_global_rule(cls, file_path: str, rule_snippet: str, tag: str = "tokensafe"):
        """Injects or updates a delimited rule block in a markdown instruction file."""
        parent = os.path.dirname(file_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        content = ""
        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                content = ""
        start_tag = f"<!-- {tag} -->"
        end_tag = f"<!-- /{tag} -->"
        pattern = re.compile(rf"{re.escape(start_tag)}.*?{re.escape(end_tag)}", re.DOTALL)
        wrapped = f"{start_tag}\n{rule_snippet.strip()}\n{end_tag}"
        if start_tag in content and end_tag in content:
            new_content = pattern.sub(lambda _: wrapped, content)
        elif start_tag in content:
            new_content = content.replace(start_tag, wrapped, 1)
        else:
            new_content = (content.rstrip() + "\n\n" + wrapped).lstrip()
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_content)

    @classmethod
    def _remove_global_rule(cls, file_path: Optional[str], tag: str = "tokensafe") -> bool:
        """Removes a delimited rule block from a markdown instruction file.
        Returns True if a rule block was found and stripped."""
        if not file_path or not os.path.exists(file_path):
            return False
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            return False

        start_tag = f"<!-- {tag} -->"
        end_tag = f"<!-- /{tag} -->"
        pattern = re.compile(rf"[ \t]*{re.escape(start_tag)}.*?{re.escape(end_tag)}[ \t]*\n?", re.DOTALL)
        if not re.search(pattern, content) and start_tag not in content:
            return False

        new_content = pattern.sub("", content)
        if start_tag in new_content:
            new_content = re.sub(rf"[ \t]*{re.escape(start_tag)}.*", "", new_content)

        cleaned = new_content.strip()
        if not cleaned:
            try:
                os.remove(file_path)
                return True
            except Exception:
                pass

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(cleaned + "\n" if cleaned else "")
            return True
        except Exception:
            return False

    @classmethod
    def _symlink_skill(cls, target_path: str):
        """Symlinks the TokenSafe skill into the target agent's skills directory."""
        skill_src = os.path.join(PROJECT_ROOT, "skills", "token-optimizer")
        cls._symlink_dir(skill_src, target_path)

    @classmethod
    def _global_rule_installed(cls) -> bool:
        """True if any agent-global rules file already carries the tokensafe block,
        meaning project-level files can reference it instead of duplicating it."""
        for info in AGENTS_REGISTRY.values():
            for key in ("agents_md_path", "rules_path"):
                path = info.get(key)
                if not path or not os.path.isfile(path):
                    continue
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        if "<!-- tokensafe -->" in f.read():
                            return True
                except Exception:
                    continue
        return False

    @classmethod
    def install_project(
        cls,
        project_dir: Optional[str] = None,
        agents_plugin_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Injects TokenSafe rules and MCP configs directly into a target project workspace.

        agents_plugin_path (opt-in): declares the tokensafe plugin directory in
        the project's .agents/plugins.json (json_configs.md) so teammates on
        Antigravity load the plugin from the shared repo without running the
        installer themselves."""
        if not project_dir:
            project_dir = os.getcwd()
        project_dir = os.path.abspath(project_dir)

        rule_files = [
            os.path.join(project_dir, "AGENTS.md"),
            os.path.join(project_dir, "CLAUDE.md"),
            os.path.join(project_dir, ".cursorrules"),
            os.path.join(project_dir, ".windsurfrules"),
            os.path.join(project_dir, ".clinerules"),
            os.path.join(project_dir, ".github", "copilot-instructions.md"),
            os.path.join(project_dir, ".cursor", "rules", "tokensafe.mdc"),
        ]

        cursor_mdc_header = (
            "---\n"
            "description: TokenSafe token economy, graph-first navigation and context optimization protocol\n"
            "globs: *\n"
            "alwaysApply: true\n"
            "---\n\n"
        )

        # Rules dedup: when the full protocol is already installed globally,
        # project files get the short pointer instead of the ~850-token block.
        project_rule = POINTER_RULE if cls._global_rule_installed() else UNIFIED_TOKENSAFE_RULE

        created = []
        for rf in rule_files:
            try:
                if rf.endswith(".mdc"):
                    cls._write_file_safely(rf, cursor_mdc_header + project_rule)
                else:
                    cls._inject_global_rule(rf, project_rule)
                created.append(os.path.relpath(rf, project_dir))
            except Exception:
                pass

        # Also inject project-local .cursor/mcp.json
        cursor_mcp = os.path.join(project_dir, ".cursor", "mcp.json")
        try:
            os.makedirs(os.path.dirname(cursor_mcp), exist_ok=True)
            # Idempotency exception: when the project ALREADY carries a
            # tokensafe entry (committed by the repo authors or injected
            # earlier), leave it verbatim — rewriting the machine-specific
            # PYTHON_BIN command churns tracked project files on every
            # `install --all` run (the tokensafe repo itself shipped exactly
            # this dirt: a .cursor/mcp.json diff + .backup after each run).
            already_present = False
            try:
                with open(cursor_mcp, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    existing = (loaded.get("mcpServers") or {}).get("tokensafe")
                    already_present = isinstance(existing, dict) and bool(existing.get("command"))
            except Exception:
                already_present = False
            if not already_present:
                data, err = cls._load_json_with_backup(cursor_mcp)
                if not err and data is not None:
                    if "mcpServers" not in data or not isinstance(data["mcpServers"], dict):
                        data["mcpServers"] = {}
                    data["mcpServers"]["tokensafe"] = {
                        "command": PYTHON_BIN,
                        "args": [MCP_SERVER_PATH]
                    }
                    data["mcpServers"].pop("tokensafe-mcp", None)
                    cls._atomic_write_json(cursor_mcp, data)
                    created.append(".cursor/mcp.json")
        except Exception:
            pass

        if agents_plugin_path:
            cls._write_agents_plugins_config(project_dir, agents_plugin_path)
            created.append(".agents/plugins.json")

        try:
            from src.paths import ensure_tokensafe_gitignore
            if ensure_tokensafe_gitignore(project_dir):
                created.append(".gitignore (.tokensafe/ registered)")
        except Exception:
            pass

        return {
            "success": True,
            "project_dir": project_dir,
            "created_rules": created
        }

    @classmethod
    def _write_agents_plugins_config(cls, project_dir: str, plugin_path: str) -> None:
        """Writes/merges .agents/plugins.json declaring the tokensafe plugin
        directory. Stored relative to the project root when the plugin lives
        inside it (team-shareable), otherwise as an absolute path."""
        config_path = os.path.join(project_dir, ".agents", "plugins.json")
        config, err = cls._load_json_with_backup(config_path)
        if err or config is None:
            return

        abs_path = os.path.abspath(plugin_path if os.path.isabs(plugin_path) else os.path.join(os.getcwd(), plugin_path))
        if abs_path == project_dir or abs_path.startswith(project_dir + os.sep):
            stored = os.path.relpath(abs_path, project_dir).replace(os.sep, "/")
        else:
            stored = abs_path

        entries = config.get("entries")
        if not isinstance(entries, list):
            entries = []
        if not any(isinstance(e, dict) and e.get("path") == stored for e in entries):
            entries.append({"path": stored})
        config["entries"] = entries
        cls._write_file_safely(config_path, json.dumps(config, indent=2))

    @classmethod
    def install_all(cls, force: bool = False) -> Dict[str, Any]:
        """Automatically installs TokenSafe to all detected (or all registered) AI agents."""
        agents = cls.detect_available_agents()
        results = {}
        for key, info in agents.items():
            if info["installed"] or force:
                ok, msg = cls.install_mcp_config(key)
                if ok:
                    h_ok, h_msg = cls.install_hooks_config(key)
                    if h_ok:
                        msg = f"{msg}; {h_msg}"
                results[key] = {"name": info["name"], "success": ok, "message": msg}
        # Also configure project rules and ensure zero-footprint tokensafe-ui companion CLI
        cls._ensure_tokensafe_ui_global(install_if_missing=True)
        proj_res = cls.install_project(os.getcwd())
        return {
            "agents": results,
            "project_rules": proj_res.get("created_rules", [])
        }

    @classmethod
    def run_interactive(cls):
        """Interactive terminal UI asking developer which agents to connect."""
        agents = cls.detect_available_agents()

        print("\n\033[1m\033[36m╔════════════════════════════════════════════════════════════════════╗\033[0m")
        print("\033[1m\033[36m║         ⚡ TOKENSAFE UNIVERSAL MCP & AGENT INSTALLER ⚡          ║\033[0m")
        print("\033[1m\033[36m╚════════════════════════════════════════════════════════════════════╝\033[0m\n")
        print("Detected AI developer environments on your machine:\n")

        keys = list(agents.keys())
        selected = {}

        for idx, key in enumerate(keys, 1):
            info = agents[key]
            tag = "\033[32m[Detected]\033[0m" if info["installed"] else "\033[2m[Not Found]\033[0m"
            # Auto-select detected platforms
            selected[key] = info["installed"]
            mark = "[\033[32m✓\033[0m]" if selected[key] else "[ ]"
            print(f"  {idx}. {mark} {info['name']:<28} {tag}")

        print("\n\033[2mEnter numbers to toggle (e.g., 2,3), 'A' for all, or press Enter to install:\033[0m")
        choice = input("> ").strip().lower()

        if choice == "a":
            for k in selected:
                selected[k] = True
        elif choice:
            for num in choice.replace(",", " ").split():
                if num.isdigit() and 1 <= int(num) <= len(keys):
                    k = keys[int(num) - 1]
                    selected[k] = not selected[k]

        print("\nInstalling TokenSafe configurations...\n")
        installed_count = 0
        for key, is_sel in selected.items():
            if is_sel:
                name = agents[key]["name"]
                ok, msg = cls.install_mcp_config(key)
                if ok:
                    h_ok, h_msg = cls.install_hooks_config(key)
                    if h_ok:
                        msg = f"{msg}; {h_msg}"
                if ok:
                    print(f"  \033[32m✔\033[0m \033[1m{name}\033[0m: {msg}")
                    installed_count += 1
                else:
                    print(f"  \033[31m✖\033[0m \033[1m{name}\033[0m: {msg}")

        print(f"\n\033[32m\033[1m✅ TokenSafe successfully connected to {installed_count} agent(s)!\033[0m")
        ui_sync = cls._ensure_tokensafe_ui_global(install_if_missing=True)
        if ui_sync.get("ok"):
            print(f"   \033[32m✔\033[0m Zero-footprint Vite+React companion tokensafe-ui {ui_sync['action']} globally.")
        print("   Restart your IDE / Agent to start saving tokens automatically.\n")
        cls.maybe_prompt_account_login(context="install")
        cls._ensure_proxy_default("install")

    @classmethod
    def uninstall_project(cls, project_dir: Optional[str] = None) -> Dict[str, Any]:
        """Removes TokenSafe rules, project MCP configs, and .gitignore references from a project workspace."""
        if not project_dir:
            project_dir = os.getcwd()
        project_dir = os.path.abspath(project_dir)

        rule_files = [
            os.path.join(project_dir, "AGENTS.md"),
            os.path.join(project_dir, "CLAUDE.md"),
            os.path.join(project_dir, ".cursorrules"),
            os.path.join(project_dir, ".windsurfrules"),
            os.path.join(project_dir, ".clinerules"),
            os.path.join(project_dir, ".github", "copilot-instructions.md"),
        ]
        cleaned = []
        for rf in rule_files:
            try:
                if cls._remove_global_rule(rf):
                    cleaned.append(os.path.relpath(rf, project_dir))
            except Exception:
                pass

        # Remove .cursor/rules/tokensafe.mdc if present
        cursor_mdc = os.path.join(project_dir, ".cursor", "rules", "tokensafe.mdc")
        if cls._remove_path_safely(cursor_mdc):
            cleaned.append(".cursor/rules/tokensafe.mdc")

        # Clean project-local .cursor/mcp.json
        cursor_mcp = os.path.join(project_dir, ".cursor", "mcp.json")
        if os.path.exists(cursor_mcp):
            try:
                data, err = cls._load_json_with_backup(cursor_mcp)
                if not err and data and isinstance(data.get("mcpServers"), dict):
                    mutated = False
                    if "tokensafe" in data["mcpServers"]:
                        data["mcpServers"].pop("tokensafe")
                        mutated = True
                    if "tokensafe-mcp" in data["mcpServers"]:
                        data["mcpServers"].pop("tokensafe-mcp")
                        mutated = True
                    if mutated:
                        if not data["mcpServers"] and list(data.keys()) == ["mcpServers"]:
                            os.remove(cursor_mcp)
                        else:
                            cls._atomic_write_json(cursor_mcp, data)
                        cleaned.append(".cursor/mcp.json")
            except Exception:
                pass

        # Clean .agents/plugins.json
        agents_plugins = os.path.join(project_dir, ".agents", "plugins.json")
        if os.path.exists(agents_plugins):
            try:
                data, err = cls._load_json_with_backup(agents_plugins)
                if not err and data and isinstance(data.get("entries"), list):
                    original_len = len(data["entries"])
                    data["entries"] = [
                        e for e in data["entries"]
                        if not (isinstance(e, dict) and "tokensafe" in str(e.get("path", "")))
                    ]
                    if len(data["entries"]) != original_len:
                        if not data["entries"] and list(data.keys()) == ["entries"]:
                            os.remove(agents_plugins)
                        else:
                            cls._write_file_safely(agents_plugins, json.dumps(data, indent=2))
                        cleaned.append(".agents/plugins.json")
            except Exception:
                pass

        # Clean .gitignore
        root_gitignore = os.path.join(project_dir, ".gitignore")
        if os.path.isfile(root_gitignore):
            try:
                with open(root_gitignore, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                new_lines = [
                    line for line in lines
                    if not line.strip().startswith(".tokensafe")
                    and not ("TokenSafe" in line and ("runtime" in line or "metric" in line or "cache" in line))
                ]
                if len(new_lines) != len(lines):
                    with open(root_gitignore, "w", encoding="utf-8") as f:
                        f.writelines(new_lines)
                    cleaned.append(".gitignore")
            except Exception:
                pass

        return {
            "success": True,
            "project_dir": project_dir,
            "cleaned_rules": cleaned
        }

    @classmethod
    def uninstall_all(cls, force: bool = False, purge: bool = False) -> Dict[str, Any]:
        """Uninstalls TokenSafe from all detected AI agents, unwires proxy, and optionally purges data."""
        proxy_report = cls.proxy_off()

        agents = cls.detect_available_agents()
        results = {}
        for key, info in agents.items():
            if info["installed"] or force:
                m_ok, m_msg = cls.uninstall_mcp_config(key)
                h_ok, h_msg = cls.uninstall_hooks_config(key)
                status_ok = m_ok or h_ok
                msgs = [m for m in (m_msg, h_msg) if m]
                results[key] = {
                    "name": info["name"],
                    "success": status_ok,
                    "message": "; ".join(msgs) if msgs else "Cleaned"
                }

        cls.restart_mcp_daemons()
        proj_report = cls.uninstall_project(os.getcwd())

        purged = False
        if purge:
            try:
                from src.paths import resolve_data_dir
                d_dir = resolve_data_dir()
                if os.path.isdir(d_dir):
                    shutil.rmtree(d_dir, ignore_errors=True)
                ts_home = os.path.join(HOME, ".tokensafe")
                if os.path.isdir(ts_home):
                    shutil.rmtree(ts_home, ignore_errors=True)
                purged = True
            except Exception:
                pass

        return {
            "agents": results,
            "project_rules": proj_report.get("cleaned_rules", []),
            "proxy": proxy_report,
            "purged": purged
        }

    @classmethod
    def run_interactive_uninstall(cls):
        """Interactive terminal UI asking developer which agents to disconnect and unwire."""
        agents = cls.detect_available_agents()

        print("\n\033[1m\033[36m╔════════════════════════════════════════════════════════════════════╗\033[0m")
        print("\033[1m\033[36m║        ⚡ TOKENSAFE UNIVERSAL MCP & AGENT UNINSTALLER ⚡         ║\033[0m")
        print("\033[1m\033[36m╚════════════════════════════════════════════════════════════════════╝\033[0m\n")
        print("Detected AI developer environments on your machine:\n")

        keys = list(agents.keys())
        selected = {}

        for idx, key in enumerate(keys, 1):
            info = agents[key]
            tag = "\033[32m[Detected]\033[0m" if info["installed"] else "\033[2m[Not Found]\033[0m"
            selected[key] = info["installed"]
            mark = "[\033[32m✓\033[0m]" if selected[key] else "[ ]"
            print(f"  {idx}. {mark} {info['name']:<28} {tag}")

        print("\n\033[2mEnter numbers to toggle (e.g., 2,3), 'A' for all, or press Enter to uninstall selected:\033[0m")
        try:
            choice = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return

        if choice == "a":
            for k in selected:
                selected[k] = True
        elif choice:
            for num in choice.replace(",", " ").split():
                if num.isdigit() and 1 <= int(num) <= len(keys):
                    k = keys[int(num) - 1]
                    selected[k] = not selected[k]

        try:
            purge_ans = input("\nAlso purge local metrics database and cache (~/.tokensafe)? [y/N]: ").strip().lower()
            purge = purge_ans in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            purge = False

        print("\nUninstalling TokenSafe configurations...\n")
        p_rep = cls.proxy_off()
        if p_rep.get("unwired", {}).get("ok"):
            print(f"  \033[32m✔\033[0m Proxy daemon & wiring unwound")

        uninstalled_count = 0
        for key, is_sel in selected.items():
            if is_sel:
                name = agents[key]["name"]
                m_ok, m_msg = cls.uninstall_mcp_config(key)
                h_ok, h_msg = cls.uninstall_hooks_config(key)
                if m_ok or h_ok:
                    msg = f"{m_msg}; {h_msg}" if (m_msg and h_msg) else (m_msg or h_msg)
                    print(f"  \033[32m✔\033[0m \033[1m{name}\033[0m: {msg}")
                    uninstalled_count += 1
                else:
                    print(f"  \033[33m–\033[0m \033[1m{name}\033[0m: Nothing to remove")

        cls.restart_mcp_daemons()
        proj_rep = cls.uninstall_project(os.getcwd())
        if proj_rep.get("cleaned_rules"):
            print(f"\n\033[32m✔ Cleaned project rules from: {', '.join(proj_rep['cleaned_rules'])}\033[0m")

        if purge:
            try:
                from src.paths import resolve_data_dir
                d_dir = resolve_data_dir()
                if os.path.isdir(d_dir):
                    shutil.rmtree(d_dir, ignore_errors=True)
                ts_home = os.path.join(HOME, ".tokensafe")
                if os.path.isdir(ts_home):
                    shutil.rmtree(ts_home, ignore_errors=True)
                print("  \033[32m✔\033[0m Local data directory purged (~/.tokensafe)")
            except Exception:
                pass

        print(f"\n\033[32m\033[1m✅ TokenSafe uninstalled from {uninstalled_count} agent(s)!\033[0m")
        print("   To completely remove the CLI, run:\n     npm uninstall -g @tokensafe/cli   (or pip uninstall tokensafe)\n")

    @staticmethod
    def _is_tokensafe_mcp_command(cmd: str) -> bool:
        """Returns True if a process command line represents a running TokenSafe MCP server daemon."""
        if not cmd or not cmd.strip():
            return False
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            tokens = cmd.split()
        if not tokens:
            return False

        # Ignore shell wrappers or inline python -c inspection commands
        exe_base = os.path.basename(tokens[0]).lower()
        if exe_base in ("sh", "bash", "zsh", "fish", "dash") and "-c" in tokens:
            return False
        if exe_base.startswith("python") and "-c" in tokens:
            return False

        for idx, tok in enumerate(tokens):
            base = os.path.basename(tok).lower()
            if base == "mcp_server.py" and ("tokensafe" in tok.lower() or "agy-token-optimizer" in tok.lower() or "src/mcp_server.py" in tok.replace("\\", "/")):
                return True
            if base in ("tokensafe", "tokensafe.js", "agy-tokens"):
                rest = tokens[idx + 1:]
                if rest and rest[0] == "mcp":
                    subcmd = rest[1] if len(rest) > 1 else "serve"
                    if subcmd not in ("disable", "off", "rm", "enable", "on", "add", "list", "ls", "status"):
                        return True
        return False

    @classmethod
    def restart_mcp_daemons(cls) -> int:
        """
        Touches the MCP reload marker and terminates active TokenSafe MCP server daemons
        so auto-reconnecting IDEs (Antigravity, Cursor, Claude Code) spawn the updated version.
        """
        try:
            # Data-dir resolution is context-independent (canonical
            # ~/.tokensafe/data unless TOKENSAFE_DATA_DIR overrides), so one
            # marker location covers every launch context.
            from src.paths import resolve_data_dir
            data_dir = resolve_data_dir()
            os.makedirs(data_dir, exist_ok=True)
            reload_marker = os.path.join(data_dir, ".mcp_reload")
            with open(reload_marker, "w", encoding="utf-8") as f:
                f.write(str(time.time()))
        except Exception:
            pass

        if os.name == "nt":
            return 0

        my_pid = os.getpid()
        parent_pid = os.getppid()
        target_pids: List[int] = []

        try:
            res = subprocess.run(
                ["ps", "-ax", "-o", "pid=,command="],
                capture_output=True,
                text=True,
                timeout=3
            )
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split(None, 1)
                    if len(parts) < 2:
                        continue
                    try:
                        pid = int(parts[0])
                    except ValueError:
                        continue
                    if pid in (my_pid, parent_pid) or pid <= 1:
                        continue
                    if cls._is_tokensafe_mcp_command(parts[1]):
                        target_pids.append(pid)
        except Exception:
            return 0

        restarted = 0
        for pid in target_pids:
            try:
                os.kill(pid, signal.SIGTERM)
                restarted += 1
            except (ProcessLookupError, PermissionError, OSError):
                continue

        if restarted > 0:
            time.sleep(0.2)
            for pid in target_pids:
                try:
                    os.kill(pid, 0)
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass

        return restarted

    @classmethod
    def _ensure_tokensafe_ui_global(
        cls,
        npm_bins: Optional[List[str]] = None,
        install_if_missing: bool = True,
    ) -> Dict[str, Any]:
        """Installs or upgrades global `tokensafe-ui` for zero-footprint Vite+React `tokensafe dev`."""
        candidates = npm_bins or ([cls._find_npm()] if cls._find_npm() else [])
        for npm_bin in candidates:
            if not npm_bin:
                continue
            npm_env = cls._npm_subprocess_env(npm_bin)
            try:
                res = subprocess.run(
                    [npm_bin, "list", "-g", "--depth=0", "tokensafe-ui"],
                    env=npm_env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                already_installed = "tokensafe-ui@" in (res.stdout or "")
                if not already_installed and not install_if_missing:
                    continue
                up_res = subprocess.run(
                    [npm_bin, "install", "-g", "tokensafe-ui@latest"],
                    env=npm_env,
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
                if up_res.returncode == 0:
                    return {
                        "ok": True,
                        "action": "upgraded" if already_installed else "installed",
                        "npm": npm_bin,
                    }
            except Exception:
                continue
        return {"ok": False, "action": "skipped"}

    @staticmethod
    def _semver_dir_key(name: str) -> Tuple[int, ...]:
        """Parses a version directory name like 'v24.14.0' into a numeric sort key."""
        parts = re.findall(r"\d+", name or "")
        return tuple(int(p) for p in parts[:4]) if parts else (0,)

    @staticmethod
    def _npm_subprocess_env(npm_bin: str) -> Dict[str, str]:
        """Builds an env where `#!/usr/bin/env node` resolves the sibling node binary.

        Under `do shell script` + login shell, nvm's bin dir is missing from PATH
        or shadowed by `/usr/local/bin`. Running `<nvm>/bin/npm` without its own
        bin directory first on PATH causes `env node` to execute `/usr/local/bin/node`,
        making npm look at `/usr/local/lib` instead of the nvm prefix.
        """
        env = os.environ.copy()
        bin_dir = os.path.dirname(os.path.abspath(npm_bin))
        if bin_dir:
            current_path = env.get("PATH", "")
            path_parts = [p for p in current_path.split(os.pathsep) if p]
            if not path_parts or os.path.normpath(path_parts[0]) != os.path.normpath(bin_dir):
                env["PATH"] = f"{bin_dir}{os.pathsep}{current_path}" if current_path else bin_dir
        return env

    @staticmethod
    def _find_npm_candidates(home: Optional[str] = None) -> List[str]:
        """Discovers all executable npm binaries across PATH, project prefix, nvm, fnm, volta, and system dirs."""
        base = home or HOME
        raw_candidates: List[str] = []

        # 1. If running out of an npm node_modules install, include its own bin/npm first.
        if os.path.basename(os.path.dirname(PROJECT_ROOT)) == "node_modules":
            nm_parent = os.path.dirname(os.path.dirname(PROJECT_ROOT))
            if os.path.basename(nm_parent) == "lib":
                prefix = os.path.dirname(nm_parent)
                raw_candidates.append(os.path.join(prefix, "bin", "npm"))
            else:
                raw_candidates.append(os.path.join(nm_parent, "npm.cmd"))
                raw_candidates.append(os.path.join(nm_parent, "bin", "npm"))

        # 2. PATH-resolved npm (note: on macOS login shells this may be /usr/local/bin/npm
        # shadowing nvm, so we must NOT stop after checking only this one).
        for exe in ("npm", "npm.cmd"):
            found = shutil.which(exe)
            if found:
                raw_candidates.append(found)

        # 3. nvm Node installations (newest semver first).
        nvm_dir = os.path.join(base, ".nvm", "versions", "node")
        try:
            if os.path.isdir(nvm_dir):
                for version_dir in sorted(
                    os.listdir(nvm_dir),
                    key=AgentInstaller._semver_dir_key,
                    reverse=True,
                ):
                    raw_candidates.append(os.path.join(nvm_dir, version_dir, "bin", "npm"))
        except OSError:
            pass

        # 4. Other popular Node version managers + system/Homebrew locations.
        raw_candidates.append(os.path.join(base, ".volta", "bin", "npm"))
        raw_candidates.append(os.path.join(base, ".nodenv", "shims", "npm"))
        raw_candidates.append(os.path.join(base, ".fnm", "aliases", "default", "bin", "npm"))
        fnm_versions = os.path.join(base, ".local", "share", "fnm", "node-versions")
        try:
            if os.path.isdir(fnm_versions):
                for version_dir in sorted(
                    os.listdir(fnm_versions),
                    key=AgentInstaller._semver_dir_key,
                    reverse=True,
                ):
                    raw_candidates.append(os.path.join(fnm_versions, version_dir, "installation", "bin", "npm"))
        except OSError:
            pass

        raw_candidates += [
            "/opt/homebrew/bin/npm",
            "/usr/local/bin/npm",
            "/usr/bin/npm",
        ]

        seen = set()
        unique: List[str] = []
        for cand in raw_candidates:
            if not cand:
                continue
            norm = os.path.normpath(os.path.abspath(cand))
            if norm in seen:
                continue
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                seen.add(norm)
                unique.append(cand)
        return unique

    @staticmethod
    def _find_npm(home: Optional[str] = None) -> Optional[str]:
        """Locates an npm executable even in a clean non-interactive environment.

        The updater dialog runs via `do shell script` + login shell, where .zshrc
        (and therefore nvm) is never sourced; PATH-only discovery made the npm
        channel silently skip updates. Falls back to well-known locations."""
        candidates = AgentInstaller._find_npm_candidates(home=home)
        return candidates[0] if candidates else None

    @classmethod
    def self_update(cls):
        """
        One-click self update for TokenSafe:
        1. Detects package installation source (git, npm, pip).
        2. Upgrades TokenSafe engine to latest release.
        3. Re-synchronizes MCP configs and rules for all active agents.
        4. Refreshes local version cache and restarts running MCP daemons.
        """
        print(f"\n{BOLD}{CYAN}╔════════════════════════════════════════════════════════════════════╗{RESET}")
        print(f"{BOLD}{CYAN}║             ⚡ TOKENSAFE ONE-CLICK SELF-UPDATE ⚡                 ║{RESET}")
        print(f"{BOLD}{CYAN}╚════════════════════════════════════════════════════════════════════╝{RESET}\n")

        # 1. Check if git clone installation (e.g. ~/.tokensafe)
        is_git_clone = os.path.isdir(os.path.join(PROJECT_ROOT, ".git"))
        if is_git_clone and PROJECT_ROOT.endswith((".tokensafe", "agy-token-optimizer")):
            print(f"  {CYAN}→{RESET} Detected local repository at {PROJECT_ROOT}. Pulling latest changes...")
            try:
                res = subprocess.run(["git", "pull", "--quiet"], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10)
                if res.returncode == 0:
                    print(f"  {GREEN}✔{RESET} Git repository updated to latest commit.")
            except Exception as e:
                print(f"  {YELLOW}⚠{RESET} Git pull skipped: {e}")

        # 2. Check npm global across all discovered Node/npm prefixes
        # A macOS login shell may have /usr/local/bin/npm on PATH ahead of nvm,
        # and `#!/usr/bin/env node` inside nvm's npm also resolves /usr/local/bin/node
        # unless npm's own bin dir is prepended to PATH.
        primary_npm = cls._find_npm()
        upgraded_npm_bins: List[str] = []
        if primary_npm:
            npm_candidates = cls._find_npm_candidates()
            norm_seen = {os.path.normpath(os.path.abspath(p)) for p in npm_candidates}
            if os.path.normpath(os.path.abspath(primary_npm)) not in norm_seen:
                npm_candidates.insert(0, primary_npm)

            found_npm_install = False
            for npm_bin in npm_candidates:
                npm_env = cls._npm_subprocess_env(npm_bin)
                try:
                    res = subprocess.run(
                        [npm_bin, "list", "-g", "--depth=0", "tokensafe"],
                        env=npm_env,
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    if "tokensafe@" in (res.stdout or ""):
                        found_npm_install = True
                        print(f"  {CYAN}→{RESET} Updating global npm package via {npm_bin} (npm install -g tokensafe@latest)...")
                        up_res = subprocess.run(
                            [npm_bin, "install", "-g", "tokensafe@latest"],
                            env=npm_env,
                            capture_output=True,
                            text=True,
                            timeout=120,
                        )
                        if up_res.returncode == 0:
                            print(f"  {GREEN}✔{RESET} npm package upgraded successfully.")
                            upgraded_npm_bins.append(npm_bin)
                        else:
                            detail = (up_res.stderr or up_res.stdout or "").strip()[:200]
                            print(f"  {YELLOW}⚠{RESET} npm upgrade failed: {detail}")
                except Exception as e:
                    print(f"  {YELLOW}⚠{RESET} npm update check failed ({npm_bin}): {e}")

            if not found_npm_install:
                print(f"  {CYAN}ℹ{RESET} tokensafe not installed via npm — skipping npm channel.")
        else:
            print(f"  {YELLOW}⚠{RESET} npm not found — skipping npm channel.")

        # 3. Check pip (prefer current interpreter's pip, fallback to pip3/pip)
        pip_cmds: List[List[str]] = [[sys.executable, "-m", "pip"]]
        pip_bin = shutil.which("pip3") or shutil.which("pip")
        if pip_bin:
            pip_cmds.append([pip_bin])
        upgraded_pip = False
        seen_pip_bins = set()
        for pip_base in pip_cmds:
            if upgraded_pip:
                break
            try:
                res = subprocess.run([*pip_base, "list"], capture_output=True, text=True, timeout=15)
                if "tokensafe" in (res.stdout or ""):
                    print(f"  {CYAN}→{RESET} Updating Python package ({' '.join(pip_base)} install --upgrade tokensafe)...")
                    up_res = subprocess.run([*pip_base, "install", "--upgrade", "tokensafe"], capture_output=True, text=True, timeout=120)
                    if up_res.returncode == 0:
                        print(f"  {GREEN}✔{RESET} Python package upgraded successfully.")
                        upgraded_pip = True
                    else:
                        detail = (up_res.stderr or up_res.stdout or "").strip()[:200]
                        print(f"  {YELLOW}⚠{RESET} pip upgrade failed: {detail}")
            except Exception as e:
                print(f"  {YELLOW}⚠{RESET} pip update skipped: {e}")
        pip_cmd = pip_bin

        # 3b. Check and upgrade TokenSafe Ecosystem packages (Frontend & Backend)
        print(f"\n  {CYAN}→{RESET} Checking TokenSafe Ecosystem packages (UI & Backend)...")
        target_npm_bins = upgraded_npm_bins or ([primary_npm] if primary_npm else [])
        ui_sync = cls._ensure_tokensafe_ui_global(npm_bins=target_npm_bins, install_if_missing=False)
        if ui_sync.get("ok"):
            print(f"    {GREEN}✔{RESET} tokensafe-ui {ui_sync['action']} globally ({ui_sync['npm']}).")
        for npm_bin in target_npm_bins:
            npm_env = cls._npm_subprocess_env(npm_bin)
            for pkg in ("create-tokensafe-ui",):
                try:
                    res = subprocess.run([npm_bin, "list", "-g", "--depth=0", pkg], env=npm_env, capture_output=True, text=True, timeout=10)
                    if f"{pkg}@" in (res.stdout or ""):
                        print(f"    {CYAN}→{RESET} Updating global {pkg} ({npm_bin} install -g {pkg}@latest)...")
                        up_res = subprocess.run([npm_bin, "install", "-g", f"{pkg}@latest"], env=npm_env, capture_output=True, text=True, timeout=120)
                        if up_res.returncode == 0:
                            print(f"    {GREEN}✔{RESET} {pkg} upgraded successfully.")
                except Exception:
                    pass

        if pip_cmd:
            try:
                res = subprocess.run([pip_cmd, "list"], capture_output=True, text=True, timeout=10)
                if "tokensafe-python" in (res.stdout or ""):
                    print(f"    {CYAN}→{RESET} Updating backend package ({pip_cmd} install --upgrade tokensafe-python)...")
                    up_res = subprocess.run([pip_cmd, "install", "--upgrade", "tokensafe-python"], capture_output=True, text=True, timeout=120)
                    if up_res.returncode == 0:
                        print(f"    {GREEN}✔{RESET} tokensafe-python upgraded successfully.")
            except Exception:
                pass

        # Project-level dependencies update (if inside a TokenSafe project)
        cwd = os.getcwd()
        for cand_pkg in [os.path.join(cwd, "package.json"), os.path.join(cwd, "frontend", "package.json")]:
            if os.path.isfile(cand_pkg):
                try:
                    with open(cand_pkg, "r", encoding="utf-8") as f:
                        txt = f.read()
                    if "tokensafe-ui" in txt:
                        pdir = os.path.dirname(cand_pkg)
                        pm = "pnpm" if shutil.which("pnpm") else ("npm" if shutil.which("npm") else None)
                        if pm:
                            print(f"    {CYAN}→{RESET} Updating local tokensafe-ui in {pdir} ({pm} update tokensafe-ui)...")
                            subprocess.run([pm, "update", "tokensafe-ui"], cwd=pdir, capture_output=True, text=True, timeout=60)
                            print(f"    {GREEN}✔{RESET} Local tokensafe-ui updated.")
                except Exception:
                    pass

        for cand_py in [os.path.join(cwd, "pyproject.toml"), os.path.join(cwd, "backend", "pyproject.toml")]:
            if os.path.isfile(cand_py):
                try:
                    with open(cand_py, "r", encoding="utf-8") as f:
                        txt = f.read()
                    if "tokensafe-python" in txt and pip_cmd:
                        pdir = os.path.dirname(cand_py)
                        print(f"    {CYAN}→{RESET} Updating local tokensafe-python in {pdir}...")
                        subprocess.run([pip_cmd, "install", "--upgrade", "tokensafe-python"], cwd=pdir, capture_output=True, text=True, timeout=60)
                        print(f"    {GREEN}✔{RESET} Local tokensafe-python updated.")
                except Exception:
                    pass

        # 4. Re-synchronize active/configured agents using the FRESHLY upgraded binary when available
        # (so plugins, hooks, skills, and rules come from the new release instead of the old process).
        print(f"\n  {CYAN}→{RESET} Re-synchronizing AI Agent plugins, hooks, MCP configs & rules...")
        fresh_bin = None
        for npm_bin in upgraded_npm_bins:
            cand_bin = os.path.join(os.path.dirname(npm_bin), "tokensafe.cmd" if os.name == "nt" else "tokensafe")
            if os.path.isfile(cand_bin) and os.access(cand_bin, os.X_OK):
                fresh_bin = cand_bin
                break
        if not fresh_bin:
            fresh_bin = shutil.which("tokensafe")

        agents = cls.detect_available_agents()
        synced_count = 0
        if fresh_bin and (upgraded_npm_bins or upgraded_pip) and agents:
            try:
                sync_env = cls._npm_subprocess_env(fresh_bin)
                ref_res = subprocess.run(
                    [fresh_bin, "install", "--refresh"],
                    env=sync_env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if ref_res.returncode == 0:
                    m_cnt = re.search(r"Synchronized\s+(\d+)", ref_res.stdout or "")
                    synced_count = int(m_cnt.group(1)) if m_cnt else sum(1 for _, i in agents.items() if i.get("installed"))
                    print(f"    {GREEN}✔{RESET} Refreshed {synced_count} agent environment(s) via upgraded binary ({fresh_bin}).")
                else:
                    ref_res = None
            except Exception:
                ref_res = None
            if ref_res is None:
                refresh_res = cls.refresh_installed_agents()
                synced_count = refresh_res["synced_count"]
        else:
            for key, info in agents.items():
                if info["installed"] or cls._is_agent_configured(key, info):
                    ok, msg = cls.install_mcp_config(key)
                    if ok:
                        h_ok, h_msg = cls.install_hooks_config(key)
                        if h_ok:
                            msg = f"{msg}; {h_msg}"
                    if ok:
                        print(f"    {GREEN}✔{RESET} {info['name']}: {msg}")
                        synced_count += 1
                    else:
                        print(f"    {YELLOW}⚠{RESET} {info['name']}: {msg}")

        # 5. Refresh local version cache and report the newly installed version in a fresh process
        try:
            from src.paths import resolve_data_dir
            cache_file = os.path.join(resolve_data_dir(), "version_cache.json")
            if os.path.exists(cache_file):
                os.remove(cache_file)
        except Exception:
            pass
        try:
            tokensafe_bin = fresh_bin or shutil.which("tokensafe")
            sync_cmd = [tokensafe_bin, "version", "--check"] if tokensafe_bin else [sys.executable, os.path.join(PROJECT_ROOT, "src", "cli.py"), "version", "--check"]
            sync_env = cls._npm_subprocess_env(tokensafe_bin) if tokensafe_bin else None
            subprocess.run(sync_cmd, env=sync_env, capture_output=True, text=True, timeout=5)
        except Exception:
            pass

        # 6. Restart running MCP server daemons so IDEs load the new version
        restarted_count = cls.restart_mcp_daemons()
        if restarted_count > 0:
            print(f"  {GREEN}✔{RESET} Restarted {restarted_count} running TokenSafe MCP daemon(s).")
            print(f"    {CYAN}ℹ{RESET} Antigravity/Cursor/Claude Code reconnect automatically; reload MCP in Claude Desktop/VS Code if disconnected.")

        # 7. Proxy re-assertion (ultimate, default-on): TokenSafe is the default LLM
        # route for every wire-able agent. Agent/app updates are known to rewrite
        # their own configs and drop third-party routing, so after every update we
        # reinstall the daemon with the FRESH engine binary and re-claim the wiring.
        ensure_bin = fresh_bin
        cls._ensure_proxy_default("update", fresh_bin=ensure_bin)

        print(f"\n{BOLD}{GREEN}✅ Self-update complete!{RESET}")
        print(f"   Synchronized {synced_count} agent environments with zero-loss AST & God Nodes rules.")
        print(f"   Run {BOLD}tokensafe stats{RESET} to view token savings.")
        print(f"   {CYAN}ℹ{RESET} If your terminal still shows the old version, open a new window or run {BOLD}hash -r{RESET}.")
        cls.maybe_prompt_account_login(context="update")

def main():
    args = sys.argv[1:]
    if args and args[0] in ("update", "--update", "upgrade", "--upgrade"):
        AgentInstaller.self_update()
    else:
        AgentInstaller.run_interactive()

if __name__ == "__main__":
    main()
