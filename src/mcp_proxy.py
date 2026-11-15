"""
TokenSafe MCP Response Proxy (v3.3.0).

Transparent stdio JSON-RPC 2.0 proxy for third-party MCP servers.
Spawns the target MCP server as a subprocess, relays messages bidirectionally,
and intercepts oversized tool responses (e.g. GitHub, Linear, Slack, Context7).
When a tool response exceeds MAX_UNFOLDED_CHARS (1500 chars / ~400 tokens), it
folds the payload, saves the raw original into TokenSafe Reversible Stash, and
attaches a hash anchor:
    [TokenSafe folded: #a1b2c3d4 — call tokensafe:expand_ref('a1b2c3d4') to restore raw content]
"""

import sys
import os
import json
import subprocess
import threading
from typing import Dict, Any, Optional, List, Tuple

from src.stash import stash_block

MAX_UNFOLDED_CHARS = 1500
MAX_UNFOLDED_LINES = 35
HEAD_LINES = 15
TAIL_LINES = 10


def fold_tool_text(text: str, tool_name: str = "mcp_tool") -> Tuple[str, bool]:
    """Folds oversized text and returns (folded_text, was_folded)."""
    if not text:
        return text, False
    from src.format_parsers.ansi import strip_ansi
    text = strip_ansi(text)
    if len(text) <= MAX_UNFOLDED_CHARS:
        return text, False

    lines = text.splitlines()
    if len(lines) <= MAX_UNFOLDED_LINES:
        return text, False

    hash8, marker = stash_block(text, source=f"mcp:{tool_name}")
    head = lines[:HEAD_LINES]
    tail = lines[-TAIL_LINES:]
    omitted = len(lines) - (HEAD_LINES + TAIL_LINES)

    folded_lines = head + [
        f"... [{omitted} lines folded by TokenSafe MCP Proxy] ...",
    ] + tail + [marker]

    return "\n".join(folded_lines), True


def compact_mcp_response(resp: Dict[str, Any]) -> Dict[str, Any]:
    """Inspects an MCP JSON-RPC response and compacts tools/call content blocks."""
    if not isinstance(resp, dict):
        return resp

    result = resp.get("result")
    if not isinstance(result, dict):
        return resp

    content = result.get("content")
    if not isinstance(content, list):
        return resp

    modified = False
    new_content = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            raw_text = block.get("text", "")
            folded_text, was_folded = fold_tool_text(raw_text)
            if was_folded:
                new_block = dict(block)
                new_block["text"] = folded_text
                new_content.append(new_block)
                modified = True
            else:
                new_content.append(block)
        else:
            new_content.append(block)

    if modified:
        new_resp = dict(resp)
        new_result = dict(result)
        new_result["content"] = new_content
        new_resp["result"] = new_result
        return new_resp

    return resp


def run_proxy(target_cmd: List[str]):
    """Runs the stdio proxy bridging sys.stdin/stdout with target_cmd."""
    if not target_cmd:
        sys.stderr.write("TokenSafe MCP Proxy Error: No target command specified.\n")
        sys.exit(1)

    try:
        proc = subprocess.Popen(
            target_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            bufsize=0,
        )
    except Exception as e:
        sys.stderr.write(f"TokenSafe MCP Proxy Error: Failed to spawn {' '.join(target_cmd)}: {e}\n")
        sys.exit(1)

    def _forward_stdin():
        try:
            while True:
                line = sys.stdin.readline()
                if not line:
                    break
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.write(line.encode("utf-8"))
                    proc.stdin.flush()
        except Exception:
            pass
        finally:
            if proc.stdin and not proc.stdin.closed:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

    t_in = threading.Thread(target=_forward_stdin, daemon=True)
    t_in.start()

    try:
        while True:
            line_bytes = proc.stdout.readline()
            if not line_bytes:
                break
            line_str = line_bytes.decode("utf-8", errors="replace").strip()
            if not line_str:
                continue

            # Parse JSON-RPC line
            try:
                msg = json.loads(line_str)
                compacted = compact_mcp_response(msg)
                out_line = json.dumps(compacted, ensure_ascii=False) + "\n"
            except Exception:
                out_line = line_str + "\n"

            sys.stdout.write(out_line)
            sys.stdout.flush()
    except Exception:
        pass
    finally:
        proc.wait()
        sys.exit(proc.returncode or 0)
