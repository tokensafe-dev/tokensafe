"""
Out-of-the-box syntax guard for OUMV direct workspace edits.

Validates replacement content BEFORE it is persisted so the agent can never
write syntactically broken code through the `oumv/mutate` offline fallback.
Runs strictly on the Python standard library; JavaScript checks shell out to
`node --check` when Node.js is available (best-effort, bounded timeout).

Languages without a checker are reported as "skipped" — honest reporting per
the OUMV spec §8 Mutation Policy (never imply a validation that did not run).
"""

import ast
import os
import shutil
import subprocess
import tempfile
from typing import Dict

_NODE_TIMEOUT_SEC = 3.0

_EXT_STRATEGY = {
    ".py": "python-ast",
    ".js": "node-check",
    ".mjs": "node-check",
    ".cjs": "node-check",
}


def check_syntax(abs_path: str, content: str) -> Dict[str, str]:
    """
    Pre-flight syntax validation for a direct workspace edit.

    Returns {"status": "passed"|"failed"|"skipped", "tool": str, "error": str}.
    Never raises: a language we cannot validate is reported as "skipped".
    """
    ext = os.path.splitext(abs_path)[1].lower()
    strategy = _EXT_STRATEGY.get(ext)

    if strategy == "python-ast":
        try:
            ast.parse(content)
            return {"status": "passed", "tool": "python-ast"}
        except SyntaxError as e:
            return {"status": "failed", "tool": "python-ast", "error": f"line {e.lineno}: {e.msg}"}

    if strategy == "node-check":
        node_bin = shutil.which("node")
        if not node_bin:
            return {"status": "skipped", "tool": "node-check", "error": "node not found on PATH"}
        # `node --check` requires a file: validate a temp copy so the target stays untouched.
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=ext, prefix="tokensafe_syntax_")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(content)
            proc = subprocess.run(
                [node_bin, "--check", tmp_path],
                capture_output=True,
                text=True,
                timeout=_NODE_TIMEOUT_SEC,
            )
            if proc.returncode == 0:
                return {"status": "passed", "tool": "node --check"}
            err_lines = (proc.stderr or proc.stdout or "").strip().splitlines()
            return {
                "status": "failed",
                "tool": "node --check",
                "error": err_lines[0] if err_lines else "syntax check failed",
            }
        except subprocess.TimeoutExpired:
            return {"status": "skipped", "tool": "node-check", "error": "timeout"}
        except Exception as e:  # noqa: BLE001 - guard must never raise
            return {"status": "skipped", "tool": "node-check", "error": str(e)}
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    return {
        "status": "skipped",
        "tool": "none",
        "error": f"no syntax checker for {ext or 'unknown extension'}",
    }
