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
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.bench import tokensafe_mcp

REPO_ROOT = Path(__file__).resolve().parents[3]
GO_ROOT = REPO_ROOT / "tokensafe-backend" / "go"
COPY_ITEMS = ("go.mod", "go.sum", "tokensafe.go", "pkg", "benchmark", "cmd")
SANDBOX_FILE = "benchmark/sandboxes/vanilla_gin/app.go"
ALLOWED_WRITE_PREFIX = "benchmark/sandboxes/"
MAX_TOOL_OUTPUT = 4000

TASKS: Dict[str, Dict[str, Any]] = {
    "product-create-status": {
        "symptom": "creating a product returns HTTP 200 instead of 201, so the contract test fails.",
        "repro": (
            "POST /api/products with {\"title\":\"USB-C Hub\",\"price\":39.99,\"stock\":15,\"category\":\"electronics\"} "
            "must answer 201; it answers 200. Stateless: one request is enough."
        ),
        "breaks": [(f"{SANDBOX_FILE}", "c.JSON(http.StatusCreated, p)", "c.JSON(http.StatusOK, p)")],
    },
    "order-conflict-status": {
        "symptom": "a duplicate idempotency key returns HTTP 400 instead of 409, so clients retry blindly.",
        "repro": (
            "POST /api/orders with {\"idempotencyKey\":\"order_key_001\",\"customerId\":\"cust_100\","
            "\"items\":[{\"productId\":2,\"quantity\":1}]} twice in the same process: the second call must answer 409 "
            "and answers 400. Stateful: the first call creates the key, so the second call is the one that fails."
        ),
        "breaks": [(
            SANDBOX_FILE,
            'c.JSON(http.StatusConflict, gin.H{"error": errMsg})',
            'c.JSON(http.StatusBadRequest, gin.H{"error": errMsg})',
        )],
    },
    "product-validation-status": {
        "symptom": "an invalid product payload is accepted with HTTP 200 instead of being rejected with 422.",
        "repro": (
            "POST /api/products with {\"title\":\"Bad Product\",\"price\":-10,\"stock\":5} must answer 422 (or 400); "
            "it answers 200. Stateless: one request is enough."
        ),
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
    def __init__(self, task_id: str):
        if task_id not in TASKS:
            raise ValueError(f"unknown task {task_id}; valid: {', '.join(TASKS)}")
        self.task_id = task_id
        self.dir = Path(tempfile.mkdtemp(prefix=f"oumv-bench-{task_id}-")) / "go"
        self.dir.mkdir(parents=True)
        for item in COPY_ITEMS:
            src = GO_ROOT / item
            dst = self.dir / item
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        # The runtime must serve the *workspace* code: build the helper inside the
        # copied module and rebuild it when an edit changes the sources, otherwise
        # oumv/verify would keep reporting the pristine app's behaviour.
        self.helper = self.dir.parent / "benchoumv"
        self._helper_built_at = 0.0
        self._mcp = None
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

    def search_files(self, query: str, path: str = ".", max_hits: int = 40) -> Any:
        """Case-insensitive substring search — the grep a normal agent would have."""
        base = self._resolve(path)
        needle = query.strip().lower()
        if not needle:
            return {"error": "query is required"}
        max_hits = max(1, min(int(max_hits), 100))
        hits: List[str] = []
        for entry in sorted(base.rglob("*")):
            if not entry.is_file() or entry.suffix not in {".go", ".mod", ".sum"}:
                continue
            if any(part in {".git", "vendor"} for part in entry.relative_to(base).parts):
                continue
            try:
                text = entry.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if needle in line.lower():
                    hits.append(f"{entry.relative_to(base)}:{number}: {line.strip()[:160]}")
                    if len(hits) >= max_hits:
                        return {"query": query, "hits": hits, "truncated": True}
        return {"query": query, "hits": hits, "truncated": False}

    # ---- OUMV -------------------------------------------------------------
    def _newest_source_mtime(self) -> float:
        newest = 0.0
        for path in self.dir.rglob("*.go"):
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                continue
        return newest

    def _ensure_helper(self, rebuild: bool) -> Optional[Dict[str, Any]]:
        """Builds the workspace helper; rebuilds only when live code is requested."""
        if self.helper.exists() and (not rebuild or self._helper_built_at >= self._newest_source_mtime()):
            return None
        proc = _run(["go", "build", "-o", str(self.helper), "./cmd/benchoumv"], cwd=self.dir)
        if proc.returncode != 0:
            lines = ((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()
            return {"error": {"code": -32002, "message": "BUILD_ERROR: " + (lines[-1] if lines else "build failed")[:300]}}
        self._helper_built_at = time.time()
        return None

    def oumv(self, method: str, params: Dict[str, Any]) -> Any:
        # verify/observe/understand must see the edited sources; getSource and L3
        # read files directly, so they can reuse the last good binary.
        live = method in ("oumv/verify", "oumv/understand") or (
            method == "oumv/observe" and str(params.get("level") or "") != "L3"
        )
        failure = self._ensure_helper(rebuild=live)
        if failure:
            return failure
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

    def tokensafe(self, name: str, arguments: Dict[str, Any]) -> Any:
        if self._mcp is None:
            self._mcp = tokensafe_mcp.open_client(self.dir)
        if self._mcp is None:
            return {"error": "TokenSafe MCP is unavailable"}
        return self._mcp.call(name, arguments)

    def cleanup(self) -> None:
        if self._mcp is not None:
            self._mcp.close()
            self._mcp = None
        shutil.rmtree(self.dir.parent, ignore_errors=True)


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
            "name": "search_files",
            "description": "Case-insensitive substring search across workspace .go files, returns path:line hits.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string", "description": "Relative directory, default '.'"},
                    "max_hits": {"type": "integer", "description": "Default 40, max 100"},
                },
                "required": ["query"],
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

TOKENSAFE_TOOLS: List[Dict[str, Any]] = BASE_TOOLS + [
    {
        "type": "function",
        "function": {
            "name": "ts_smart_read_file",
            "description": "TokenSafe AST-skeleton reader: a compact symbol view (~90% fewer tokens than the file).",
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
            "name": "ts_scoped_search",
            "description": "TokenSafe scoped search returning file:line hits inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string", "description": "Relative directory, default '.'"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ts_apply_edits",
            "description": (
                "TokenSafe surgical editor: replaces exact snippets (hunks of {target, replacement}) "
                "and runs `verify` after the edit, returning a one-line pass/fail."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "target": {"type": "string", "description": "Exact snippet to replace"},
                                "replacement": {"type": "string", "description": "Replacement snippet"},
                            },
                            "required": ["target", "replacement"],
                        },
                    },
                    "verify": {"type": "string", "description": "Shell command to run after the edit"},
                },
                "required": ["path", "edits"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ts_repo_map",
            "description": "TokenSafe architecture map: god nodes / high-dependency files, or an API surface.",
            "parameters": {
                "type": "object",
                "properties": {"view": {"type": "string", "enum": ["graph", "api", "tree"]}},
                "required": ["view"],
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
            "name": "oumv_applyEdit",
            "description": "OUMV guarded edit: replaces the file only if `expectedVersion` from oumv_getSource matches the file on disk.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "replacement": {"type": "string"},
                    "expectedVersion": {"type": "string", "description": "sha256 version from oumv_getSource"},
                },
                "required": ["path", "replacement", "expectedVersion"],
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
        "search_files": ws.search_files,
        "run_command": ws.run_command,
    }
    if arm == "tokensafe":
        tools.update({
            "ts_smart_read_file": lambda path, symbol="": ws.tokensafe(
                "smart_read_file", {"path": path, **({"symbol": symbol} if symbol else {})}
            ),
            "ts_scoped_search": lambda query, path=".": ws.tokensafe(
                "scoped_search", {"query": query, "path": path, "max_results": 20}
            ),
            "ts_apply_edits": lambda path, edits, verify="": ws.tokensafe(
                "apply_edits",
                {
                    "edits": [{"path": path, "hunks": edits}],
                    **({"verify": verify} if verify else {}),
                },
            ),
            "ts_repo_map": lambda view="graph": ws.tokensafe("repo_map", {"view": view, "project_path": "."}),
        })
    if arm == "oumv":
        tools.update({
            "oumv_observe": lambda level, target="": ws.oumv("oumv/observe", {"level": level, "target": target}),
            "oumv_getSource": lambda path, symbol="": ws.oumv("oumv/getSource", {"path": path, "symbol": symbol}),
            "oumv_applyEdit": lambda path, replacement, expectedVersion: ws.oumv(
                "oumv/applyEdit", {"path": path, "replacement": replacement, "expectedVersion": expectedVersion}
            ),
            "oumv_verify": lambda target, payload=None: ws.oumv(
                "oumv/verify", {"target": target, "action": "probe", "payload": payload}
            ),
            "oumv_understand": lambda query, target: ws.oumv("oumv/understand", {"query": query, "target": target}),
        })
    return tools


SYSTEM_BASELINE = (
    "You are a senior Go engineer working in a small Go module. Use search_files to locate code, "
    "read_file to inspect it, write_file to fix it, and run_command to verify. Tests and the oracle are "
    "read-only. Make the smallest correct change; stop as soon as the oracle passes."
)
SYSTEM_TOKENSAFE = (
    "You are a senior Go engineer working in a small Go module. The workspace has TokenSafe tools. "
    "The bug report includes a repro; use this loop and stop as soon as the oracle passes: "
    "(1) ts_scoped_search for the status code or error text named in the repro — it returns file:line hits; "
    "(2) ts_smart_read_file with that path and the identifier from the hit as `symbol` — it returns the "
    "enclosing handler body (or a window around the hit) with line numbers, which is what you edit; "
    "(3) ts_apply_edits with one hunk {target: the exact snippet, replacement: the fix} plus "
    'verify="auto" (the project\'s own test command) — it edits and verifies in one call; '
    "(4) if verify fails, read the reported line and repeat (2)-(3). "
    "ts_repo_map is for an unfamiliar repository; skip it when the repro already names the endpoint. "
    "Prefer these tools over list_files/read_file. Tests and the oracle are read-only. "
    "Do not rewrite whole files; stop as soon as the oracle passes."
)

SYSTEM_OUMV = (
    "You are a senior Go engineer working in a small Go module that exposes the OUMV runtime. "
    "The bug report includes a repro; use it to go straight to the right handler instead of guessing payloads. "
    "Use this loop and stop as soon as the oracle passes: (1) oumv_observe L3 with the failing endpoint target "
    "(e.g. \"POST /api/products\") — it returns the handler body, its workspace path and the sha256 OCC version; "
    "(2) oumv_getSource for that path once to get the full file bytes; (3) oumv_applyEdit with the fixed file and "
    "the version from step 1 or 2 (it rejects a stale version); (4) confirm with oumv_verify when the repro is a "
    "single request. When the failure needs state created by earlier requests, run the oracle with run_command "
    "instead — oumv_verify intentionally starts a fresh app per call. Do not call list_files/read_file unless an "
    "OUMV tool returns an error. Tests are read-only."
)


def task_prompt(task_id: str) -> str:
    task = TASKS[task_id]
    return (
        f"The order API has a regression: {task['symptom']}\n"
        f"Repro: {task['repro']}\n"
        "Find the cause and fix the handler code. "
        "Verification: `go test ./benchmark -run TestGroundTruthOracleVanilla -count=1` must pass all 12 assertions. "
        "Do not modify tests, the oracle, or the database package."
    )
