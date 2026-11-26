"""Go sandbox target for the OUMV benchmark.

The workstation copy is a throwaway module containing only what is needed to build
and evaluate the AUB sandbox. Two arms edit the *same* file:

* baseline — the agent gets file/command tools only;
* oumv     — the same tools plus observe/getSource/verify/impact, served by a
             helper binary that mounts the OUMV runtime over the same handler.

The 12-assertion oracle (`go test ./benchmark -run TestGroundTruthOracleVanilla`)
is the single source of truth for correctness; it runs after every agent step.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[3]
GO_ROOT = REPO_ROOT / "tokensafe-backend" / "go"
COPY_ITEMS = ("go.mod", "go.sum", "tokensafe.go", "pkg", "benchmark", "cmd")
SANDBOX_FILE = "benchmark/sandboxes/vanilla_gin/app.go"
ALLOWED_WRITE_PREFIX = "benchmark/sandboxes/"
MAX_TOOL_OUTPUT = 4000

TASKS: Dict[str, Dict[str, Any]] = {
    "product-create-status": {
        "symptom": "creating a product returns HTTP 200 instead of 201, so the contract test fails.",
        "breaks": [(f"{SANDBOX_FILE}", "c.JSON(http.StatusCreated, p)", "c.JSON(http.StatusOK, p)")],
    },
    "order-conflict-status": {
        "symptom": "a duplicate idempotency key returns HTTP 400 instead of 409, so clients retry blindly.",
        "breaks": [(
            SANDBOX_FILE,
            'c.JSON(http.StatusConflict, gin.H{"error": errMsg})',
            'c.JSON(http.StatusBadRequest, gin.H{"error": errMsg})',
        )],
    },
    "product-validation-status": {
        "symptom": "an invalid product payload is accepted with HTTP 200 instead of being rejected with 422.",
        "breaks": [(
            SANDBOX_FILE,
            # the CreateProduct validation branch (the binding branch alone is not
            # exercised by TC03, which sends a well-formed but invalid payload)
            'p, err := db.CreateProduct(req)\n\t\tif err != nil {\n\t\t\tc.JSON(http.StatusUnprocessableEntity, gin.H{"error": err.Error()})',
            'p, err := db.CreateProduct(req)\n\t\tif err != nil {\n\t\t\tc.JSON(http.StatusOK, gin.H{"error": err.Error()})',
        )],
    },
}


def _run(cmd: List[str], cwd: Path, timeout: float = 240.0, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=env)


class GoWorkspace:
    def __init__(self, task_id: str, helper: Path):
        if task_id not in TASKS:
            raise ValueError(f"unknown task {task_id}; valid: {', '.join(TASKS)}")
        self.task_id = task_id
        self.helper = helper
        self.dir = Path(tempfile.mkdtemp(prefix=f"oumv-bench-{task_id}-")) / "go"
        self.dir.mkdir(parents=True)
        for item in COPY_ITEMS:
            src = GO_ROOT / item
            dst = self.dir / item
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        self.break_build()

    # ---- task setup -------------------------------------------------------
    def break_build(self) -> None:
        for rel, old, new in TASKS[self.task_id]["breaks"]:
            path = self.dir / rel
            text = path.read_text(encoding="utf-8")
            if old not in text:
                raise RuntimeError(f"break target not found in {rel}: {old[:60]}")
            path.write_text(text.replace(old, new, 1), encoding="utf-8")

    # ---- oracle -----------------------------------------------------------
    def oracle(self) -> Tuple[int, List[str], str]:
        proc = _run(
            ["go", "test", "./benchmark", "-run", "TestGroundTruthOracleVanilla", "-count=1", "-v"],
            cwd=self.dir,
            env={**os.environ, "APP_ENV": "development"},
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        failed = sorted(set(re.findall(r"Assertion (TC\d+_\w+) failed", output)))
        if proc.returncode == 0:
            return 12, [], "PASS"
        if not failed:
            return 0, [], f"BUILD/TEST ERROR: {output.strip().splitlines()[-1][:200] if output.strip() else 'no output'}"
        return 12 - len(failed), failed, "FAIL"

    # ---- tools ------------------------------------------------------------
    def _resolve(self, rel: str) -> Path:
        candidate = (self.dir / rel).resolve()
        root = self.dir.resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"path escapes the workspace: {rel}")
        return candidate

    def list_files(self, path: str = ".", depth: int = 2) -> Any:
        base = self._resolve(path)
        if not base.exists():
            return {"error": f"no such path: {path}"}
        depth = max(0, min(int(depth), 4))
        lines: List[str] = []
        for entry in sorted(base.rglob("*")):
            rel = entry.relative_to(base)
            if len(rel.parts) > depth:
                continue
            if any(part in {".git", "node_modules"} for part in rel.parts):
                continue
            suffix = "/" if entry.is_dir() else ""
            lines.append(f"{rel}{suffix}")
            if len(lines) >= 120:
                lines.append("… truncated")
                break
        return {"path": path, "entries": lines}

    def read_file(self, path: str, start: int = 1, end: int = 160) -> Any:
        target = self._resolve(path)
        if not target.is_file():
            return {"error": f"not a file: {path}"}
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(start))
        end = min(len(lines), max(start, int(end)))
        return {
            "path": path,
            "total_lines": len(lines),
            "start": start,
            "end": end,
            "content": "\n".join(f"{i:>4}: {lines[i - 1]}" for i in range(start, end + 1)),
        }

    def write_file(self, path: str, content: str) -> Any:
        if not path.startswith(ALLOWED_WRITE_PREFIX) or not path.endswith(".go"):
            return {"error": f"writes are limited to {ALLOWED_WRITE_PREFIX}**/*.go"}
        if path.endswith("_test.go"):
            return {"error": "the oracle tests are read-only"}
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"ok": True, "path": path, "bytes": len(content.encode())}

    def run_command(self, cmd: str) -> Any:
        allowed_prefixes = ("go test ./benchmark", "go build ./", "go vet ./benchmark", "go test ./benchmark/")
        if not cmd.strip().startswith(allowed_prefixes):
            return {"error": "only `go test ./benchmark…`, `go build ./…` and `go vet ./benchmark…` are allowed"}
        proc = _run(cmd.split(), cwd=self.dir, env={**os.environ, "APP_ENV": "development"})
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return {"exit_code": proc.returncode, "output": out[-MAX_TOOL_OUTPUT:]}

    # ---- OUMV -------------------------------------------------------------
    def oumv(self, method: str, params: Dict[str, Any]) -> Any:
        request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        proc = subprocess.run(
            [str(self.helper)],
            input=json.dumps(request) + "\n",
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(self.dir),
            env={**os.environ, "APP_ENV": "development"},
        )
        line = (proc.stdout or "").strip().splitlines()
        if not line:
            return {"error": f"oumv helper produced no output: {(proc.stderr or '')[:200]}"}
        try:
            payload = json.loads(line[-1])
        except json.JSONDecodeError:
            return {"error": f"invalid JSON from helper: {line[-1][:200]}"}
        if payload.get("error"):
            return {"error": payload["error"]}
        return payload.get("result")

    def cleanup(self) -> None:
        shutil.rmtree(self.dir.parent, ignore_errors=True)


def build_helper() -> Path:
    """Builds `cmd/benchoumv` once from the pristine module."""
    out_dir = Path(tempfile.mkdtemp(prefix="oumv-bench-helper-"))
    helper = out_dir / "benchoumv"
    proc = _run(["go", "build", "-o", str(helper), "./cmd/benchoumv"], cwd=GO_ROOT)
    if proc.returncode != 0:
        raise RuntimeError(f"failed to build benchoumv: {(proc.stderr or proc.stdout)[-800:]}")
    return helper


# ---- tool schemas ---------------------------------------------------------

BASE_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files under a workspace-relative path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative directory, default '.'"},
                    "depth": {"type": "integer", "description": "Max depth 0-4"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a line range of a workspace file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start": {"type": "integer"},
                    "end": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Replace a sandbox Go file (benchmark/sandboxes/**/*.go only).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run an allowlisted Go command (go test ./benchmark…, go build ./…, go vet ./benchmark…).",
            "parameters": {
                "type": "object",
                "properties": {"cmd": {"type": "string"}},
                "required": ["cmd"],
            },
        },
    },
]

OUMV_TOOLS: List[Dict[str, Any]] = BASE_TOOLS + [
    {
        "type": "function",
        "function": {
            "name": "oumv_observe",
            "description": "OUMV observe: L0 pulse, L1 endpoint surface, L2 endpoint contract, L3 source, L4 traces.",
            "parameters": {
                "type": "object",
                "properties": {
                    "level": {"type": "string", "enum": ["L0", "L1", "L2", "L3", "L4"]},
                    "target": {"type": "string", "description": "e.g. 'GET /api/products' or 'path#symbol'"},
                },
                "required": ["level"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "oumv_getSource",
            "description": "OUMV source read with the sha256 OCC version token.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "symbol": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "oumv_verify",
            "description": "OUMV verify: dispatch a request (optionally with a JSON body) in-process and see status/body without starting a server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "e.g. 'GET /api/products' or 'POST /api/orders'"},
                    "payload": {"type": "object", "description": "JSON body for POST/PUT targets"},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "oumv_understand",
            "description": "OUMV understand: impact analysis for an endpoint target.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "enum": ["impact", "causality"]},
                    "target": {"type": "string"},
                },
                "required": ["query", "target"],
            },
        },
    },
]


def registry(ws: GoWorkspace, arm: str) -> Dict[str, Any]:
    tools: Dict[str, Any] = {
        "list_files": ws.list_files,
        "read_file": ws.read_file,
        "write_file": ws.write_file,
        "run_command": ws.run_command,
    }
    if arm == "oumv":
        tools.update({
            "oumv_observe": lambda level, target="": ws.oumv("oumv/observe", {"level": level, "target": target}),
            "oumv_getSource": lambda path, symbol="": ws.oumv("oumv/getSource", {"path": path, "symbol": symbol}),
            "oumv_verify": lambda target, payload=None: ws.oumv(
                "oumv/verify", {"target": target, "action": "probe", "payload": payload}
            ),
            "oumv_understand": lambda query, target: ws.oumv("oumv/understand", {"query": query, "target": target}),
        })
    return tools


SYSTEM_BASELINE = (
    "You are a senior Go engineer working in a small Go module. Use list_files/read_file to inspect code, "
    "write_file to fix it, and run_command to verify. Tests and the oracle are read-only. "
    "Make the smallest correct change; stop as soon as the oracle passes."
)
SYSTEM_OUMV = (
    "You are a senior Go engineer working in a small Go module that exposes the OUMV runtime. "
    "Prefer the OUMV tools before reading whole files: oumv_observe L1 lists endpoints, L2 gives an endpoint "
    "contract, oumv_getSource returns source with an OCC version token, oumv_verify dispatches a request "
    "in-process, oumv_understand gives impact analysis. Fix the code with write_file, confirm with "
    "oumv_verify or run_command, and stop as soon as the oracle passes. Tests are read-only."
)


def task_prompt(task_id: str) -> str:
    symptom = TASKS[task_id]["symptom"]
    return (
        f"The order API has a regression: {symptom}\n"
        "Reproduce it, find the cause, and fix the handler code. "
        "Verification: `go test ./benchmark -run TestGroundTruthOracleVanilla -count=1` must pass all 12 assertions. "
        "Do not modify tests, the oracle, or the database package."
    )
