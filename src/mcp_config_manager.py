"""
TokenSafe MCP Config Manager (v3.3.0).

Inspects, wraps, and unwraps third-party MCP servers across AI coding agent configs
(Claude Code, Cursor, Codex, Windsurf) to route them through `tokensafe mcp-proxy`.
Preserves original command & arguments under `_tokensafe_orig` for 100% reversible unwrapping.
"""

import os
import sys
import json
import shutil
from typing import Dict, Any, List, Optional, Tuple

HOME = os.path.expanduser("~")

CONFIG_CANDIDATES = [
    ("Claude Code / Desktop", os.path.join(HOME, ".claude.json")),
    ("Claude Settings", os.path.join(HOME, ".claude", "settings.json")),
    ("Cursor Global", os.path.join(HOME, ".cursor", "mcp.json")),
    ("Codex Global", os.path.join(HOME, ".codex", "config.json")),
    ("Windsurf Global", os.path.join(HOME, ".codeium", "windsurf", "mcp_config.json")),
]


class MCPConfigManager:
    """Manages MCP configurations across local coding agents."""

    @classmethod
    def get_existing_configs(cls) -> List[Tuple[str, str]]:
        """Returns list of (agent_name, file_path) for existing configuration files."""
        found = []
        for name, path in CONFIG_CANDIDATES:
            if os.path.isfile(path):
                found.append((name, path))
        return found

    @classmethod
    def _read_config(cls, path: str) -> Optional[Dict[str, Any]]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    @classmethod
    def _write_config(cls, path: str, data: Dict[str, Any]) -> bool:
        try:
            # Backup
            bak_path = f"{path}.bak.tokensafe"
            if not os.path.exists(bak_path):
                shutil.copy2(path, bak_path)
            tmp_path = f"{path}.tmp.{os.getpid()}"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.replace(tmp_path, path)
            return True
        except Exception:
            return False

    @classmethod
    def list_servers(cls) -> List[Dict[str, Any]]:
        """Lists all registered MCP servers and their wrapping status."""
        results = []
        for agent_name, path in cls.get_existing_configs():
            data = cls._read_config(path)
            if not data:
                continue

            servers = data.get("mcpServers") or data.get("mcp") or {}
            if not isinstance(servers, dict):
                continue

            for s_name, s_def in servers.items():
                if not isinstance(s_def, dict):
                    continue
                cmd = s_def.get("command", "")
                args = s_def.get("args", [])
                is_wrapped = False
                if "_tokensafe_orig" in s_def:
                    is_wrapped = True
                elif isinstance(cmd, str) and ("tokensafe" in cmd and "mcp-proxy" in " ".join(args)):
                    is_wrapped = True

                # Skip TokenSafe's own MCP server
                if s_name in ("tokensafe", "tokensafe-mcp", "tokensafe-cloud"):
                    continue

                results.append({
                    "agent": agent_name,
                    "config_path": path,
                    "server_name": s_name,
                    "command": cmd,
                    "args": args,
                    "is_wrapped": is_wrapped,
                    "orig_def": s_def.get("_tokensafe_orig"),
                })
        return results

    @classmethod
    def wrap_server(cls, config_path: str, server_name: str) -> bool:
        """Wraps a specific server in config_path to route through tokensafe mcp-proxy."""
        data = cls._read_config(config_path)
        if not data:
            return False

        key = "mcpServers" if "mcpServers" in data else ("mcp" if "mcp" in data else "mcpServers")
        servers = data.get(key)
        if not isinstance(servers, dict) or server_name not in servers:
            return False

        s_def = servers[server_name]
        if not isinstance(s_def, dict):
            return False

        if "_tokensafe_orig" in s_def:
            # Already wrapped
            return True

        orig_cmd = s_def.get("command")
        orig_args = s_def.get("args") or []
        if not orig_cmd:
            return False

        s_def["_tokensafe_orig"] = {
            "command": orig_cmd,
            "args": list(orig_args)
        }

        # Resolve tokensafe CLI binary path or python entrypoint
        tokensafe_bin = shutil.which("tokensafe")
        if tokensafe_bin:
            proxy_cmd = tokensafe_bin
            proxy_args = ["mcp-proxy", "--", orig_cmd] + list(orig_args)
        else:
            proxy_cmd = sys.executable
            # Find cli.py path
            script_dir = os.path.dirname(os.path.realpath(__file__))
            cli_path = os.path.join(script_dir, "cli.py")
            proxy_args = [cli_path, "mcp-proxy", "--", orig_cmd] + list(orig_args)

        s_def["command"] = proxy_cmd
        s_def["args"] = proxy_args

        return cls._write_config(config_path, data)

    @classmethod
    def unwrap_server(cls, config_path: str, server_name: str) -> bool:
        """Restores original command and args for a wrapped server."""
        data = cls._read_config(config_path)
        if not data:
            return False

        key = "mcpServers" if "mcpServers" in data else ("mcp" if "mcp" in data else "mcpServers")
        servers = data.get(key)
        if not isinstance(servers, dict) or server_name not in servers:
            return False

        s_def = servers[server_name]
        if not isinstance(s_def, dict):
            return False

        orig = s_def.get("_tokensafe_orig")
        if not orig or not isinstance(orig, dict):
            return False

        s_def["command"] = orig.get("command")
        s_def["args"] = orig.get("args", [])
        del s_def["_tokensafe_orig"]

        return cls._write_config(config_path, data)

    @classmethod
    def wrap_all(cls) -> int:
        """Wraps all detected unwrapped third-party MCP servers. Returns count of wrapped servers."""
        wrapped_count = 0
        servers = cls.list_servers()
        for s in servers:
            if not s["is_wrapped"]:
                ok = cls.wrap_server(s["config_path"], s["server_name"])
                if ok:
                    wrapped_count += 1
        return wrapped_count

    @classmethod
    def unwrap_all(cls) -> int:
        """Unwraps all wrapped third-party MCP servers. Returns count of unwrapped servers."""
        unwrapped_count = 0
        servers = cls.list_servers()
        for s in servers:
            if s["is_wrapped"]:
                ok = cls.unwrap_server(s["config_path"], s["server_name"])
                if ok:
                    unwrapped_count += 1
        return unwrapped_count
