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


_FILLER_VERBS = ("List", "Create", "Pay", "Ship", "Cancel", "Get", "Update", "Delete")
_FILLER_NOUNS = ("Products", "Orders", "Carts", "Invoices", "Users", "Coupons")


def _generate_filler(root: Path, count: int) -> None:
    """`count` generated service files so discovery competes with a real tree.

    The oracle and the broken handler are untouched: only the amount of code a
    workspace search or a read has to get through grows, which is the variable
    matrix v4 could not measure on 69 files. Generated at copy time, never
    committed — the product repo stays clean and the target is reproducible from
    `--files N` alone.
    """
    pkg = root / "benchmark" / "sandboxes" / "gen"
    pkg.mkdir(parents=True, exist_ok=True)
    for idx in range(count):
        noun = _FILLER_NOUNS[idx % len(_FILLER_NOUNS)]
        verb = _FILLER_VERBS[(idx // len(_FILLER_NOUNS)) % len(_FILLER_VERBS)]
        name = f"Svc{idx:03d}"
        route = f"/api/{noun.lower()}"
        (pkg / f"svc{idx:03d}.go").write_text(
            f'''package gen

// Generated benchmark fixture — an unrelated service that mirrors the
// {noun.lower()} API surface ({verb}{noun} -> {route}) and mentions
// StatusCreated / StatusConflict / CONFLICT: so a status-code search has to
// distinguish a placeholder from the real handler.

import "net/http"

// {name} is not mounted anywhere; it exists so discovery has real competition.
type {name} struct{{}}

// {verb}{noun} is a placeholder for {route}.
func (h {name}) {verb}{noun}(w http.ResponseWriter, r *http.Request) {{
\tw.WriteHeader(http.StatusOK)
}}
''',
            encoding="utf-8",
        )


def choose_scoped_hunk(
    hunks: Optional[List[Dict[str, Any]]],
    error: Dict[str, Any],
    window: Optional[Tuple[int, int]],
) -> Optional[Tuple[List[Dict[str, Any]], Dict[str, int]]]:
    """Pick a scoped retry for `-32011 target_not_unique`, or None.

    The retry is deterministic: it is chosen only when exactly one reported
    occurrence falls inside the line window the agent last read. Zero or two
    candidates mean the tool refuses to guess and hands the error back.
    """
    if not isinstance(error, dict) or error.get("reason") != "target_not_unique":
        return None
    if not window or not isinstance(hunks, list) or not hunks:
        return None
    try:
        start, end = int(window[0]), int(window[1])
    except (TypeError, ValueError):
        return None
    if end < start:
        return None
    lines = []
    for value in error.get("lines") or []:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            lines.append(int(value))
    inside = [line for line in lines if start <= line <= end]
    if len(inside) != 1:
        return None
    index = error.get("hunk")
    index = int(index) if isinstance(index, (int, float)) and not isinstance(index, bool) else 0
    if index < 0 or index >= len(hunks):
        return None
    line = inside[0]
    target = str(hunks[index].get("target") or "")
    scope_start = max(start, line - 2)
    scope_end = min(end, line + target.count("\n") + 2)
    scoped = [dict(hunk) for hunk in hunks]
    scoped[index]["startLine"] = scope_start
    scoped[index]["endLine"] = scope_end
    return scoped, {"hunk": index, "startLine": scope_start, "endLine": scope_end}


class GoWorkspace:
    def __init__(self, task_id: str, files: int = 0):
        if task_id not in TASKS:
            raise ValueError(f"unknown task {task_id}; valid: {', '.join(TASKS)}")
        self.task_id = task_id
        self.files = int(files or 0)
        self.dir = Path(tempfile.mkdtemp(prefix=f"oumv-bench-{task_id}-")) / "go"
        self.dir.mkdir(parents=True)
        for item in COPY_ITEMS:
            src = GO_ROOT / item
            dst = self.dir / item
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        if self.files:
            _generate_filler(self.dir, self.files)
        # The runtime must serve the *workspace* code: build the helper inside the
        # copied module and rebuild it when an edit changes the sources, otherwise
        # oumv/verify would keep reporting the pristine app's behaviour.
        self.helper = self.dir.parent / "benchoumv"
        self._helper_built_at = 0.0
        self._mcp = None
        # Last line window the agent actually read per file (observe L3 / getSource).
        # A hunk that repeats can be scoped to it without guessing: the caller just
        # read those lines, so an occurrence outside them is not the one it means.
        self._read_windows: Dict[str, Tuple[int, int]] = {}
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
        result = payload.get("result")
        if isinstance(result, dict):
            self._remember_read_window(method, result)
        return result

    # ---- read windows & scoped retries ------------------------------------
    def _window_key(self, path: str) -> str:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.dir / candidate
        return os.path.normpath(str(candidate))

    def _remember_read_window(self, method: str, result: Dict[str, Any]) -> None:
        if method == "oumv/observe" and isinstance(result.get("data"), dict):
            data = result["data"]
            source = "data.source" in data
        elif method == "oumv/getSource" and "source" in result:
            data = result
            source = True
        else:
            return
        if not source:
            return
        path = data.get("path")
        start, end = data.get("startLine"), data.get("endLine")
        if not path or not isinstance(start, int) or not isinstance(end, int) or end < start:
            return
        self._read_windows[self._window_key(str(path))] = (start, end)

    def apply_edit_scoped(
        self,
        path: str,
        expected_version: str,
        mode: str = "",
        replacement: str = "",
        symbol: str = "",
        hunks: Optional[List[Dict[str, Any]]] = None,
    ) -> Any:
        """applyEdit with one deterministic recovery from -32011 target_not_unique.

        If the model omitted startLine/endLine and the target repeats, the runtime
        answers with the occurrence lines. When exactly one of them lies inside the
        window this agent just read, retry with that window. Two candidates are
        never resolved by guessing: the error goes back to the model instead.
        """
        params: Dict[str, Any] = {
            "path": path,
            "expectedVersion": expected_version,
            **({"mode": mode} if mode else {}),
            **({"replacement": replacement} if replacement else {}),
            **({"symbol": symbol} if symbol else {}),
            **({"hunks": hunks} if hunks else {}),
        }
        result = self.oumv("oumv/applyEdit", params)
        error = result.get("error") if isinstance(result, dict) else None
        if mode == "patch" and isinstance(error, dict) and error.get("code") == -32011:
            data = dict(error.get("data") or {})
            if not data.get("reason") and "not_unique" in str(error.get("message", "")):
                data["reason"] = "target_not_unique"
            window = self._read_windows.get(self._window_key(path))
            chosen = choose_scoped_hunk(hunks, data, window)
            if chosen is not None:
                scoped_hunks, scope = chosen
                retry = self.oumv("oumv/applyEdit", {**params, "hunks": scoped_hunks})
                if isinstance(retry, dict) and not retry.get("error"):
                    annotated = dict(retry)
                    annotated["autoScoped"] = {
                        **scope,
                        "source": "unique occurrence inside the last observe L3 window",
                    }
                    return annotated
        return result

    def tokensafe_tool_schemas(self) -> Optional[List[Dict[str, Any]]]:
        """The product's own `tools/list`, renamed to the arm's stable aliases.

        `oumv` is excluded: it is a different product surface and would let this arm
        call the protocol runtime the third arm is measuring.
        """
        if self._mcp is None:
            self._mcp = tokensafe_mcp.open_client(self.dir)
        if self._mcp is None:
            return None
        live = self._mcp.tool_schemas(exclude={"oumv"})
        if not live:
            return None
        renamed = []
        for tool in live:
            clone = json.loads(json.dumps(tool))
            real = clone["function"]["name"]
            clone["function"]["name"] = TOKENSAFE_ALIASES.get(real, real)
            renamed.append(clone)
        return renamed

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
            "description": (
                "OUMV guarded edit: applies only if `expectedVersion` matches the file on disk. "
                "mode='patch' splices hunks (cheapest — the file is never re-sent); mode='symbol' "
                "replaces one declaration; mode='replace' rewrites the whole file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "expectedVersion": {"type": "string", "description": "version from oumv_observe L3 or oumv_getSource"},
                    "mode": {"type": "string", "enum": ["patch", "symbol", "replace"]},
                    "hunks": {
                        "type": "array",
                        "description": (
                            "mode='patch': exact snippets to splice, applied in order. Copy startLine/endLine "
                            "from the L3 slice: without them a snippet that occurs more than once is rejected "
                            "with -32011 (the error carries the occurrence lines and a scoped retry)."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "target": {"type": "string", "description": "Exact snippet from the source"},
                                "replacement": {"type": "string", "description": "Replacement snippet"},
                                "startLine": {
                                    "type": "integer",
                                    "description": "1-based first line to search, from observe L3; makes a repeated target unique",
                                },
                                "endLine": {"type": "integer", "description": "1-based last line to search"},
                            },
                            "required": ["target", "replacement"],
                        },
                    },
                    "replacement": {"type": "string", "description": "New content, or the new declaration body for mode='symbol'"},
                    "symbol": {"type": "string", "description": "Declaration to replace when mode='symbol'"},
                },
                "required": ["path", "expectedVersion"],
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


# The product's tool names -> the arm's stable aliases (the prompts use these).
TOKENSAFE_ALIASES = {
    "smart_read_file": "ts_smart_read_file",
    "scoped_search": "ts_scoped_search",
    "apply_edits": "ts_apply_edits",
    "repo_map": "ts_repo_map",
    "session_context": "ts_session_context",
    "run_helper": "ts_run_helper",
}


def build_arm(ws: GoWorkspace, arm: str) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
    """What one arm sees: prompt, tool schemas and dispatcher, decided in one place.

    For the `tokensafe` arm the schemas come from the server's own `tools/list` (falling
    back to the hand-written aliases only when the CLI is missing), so the measured arm
    pays the schema cost a real session pays.
    """
    if arm == "tokensafe":
        live = ws.tokensafe_tool_schemas()
        tools = BASE_TOOLS + (live if live else TOKENSAFE_TOOLS[len(BASE_TOOLS):])
        dispatcher: Dict[str, Any] = {
            "list_files": ws.list_files,
            "read_file": ws.read_file,
            "write_file": ws.write_file,
            "search_files": ws.search_files,
            "run_command": ws.run_command,
        }
        reverse = {alias: real for real, alias in TOKENSAFE_ALIASES.items()}
        for tool in tools[len(BASE_TOOLS):]:
            alias = tool["function"]["name"]
            real = reverse.get(alias, alias)
            dispatcher[alias] = (lambda r: lambda **kwargs: ws.tokensafe(r, kwargs))(real)
        return SYSTEM_TOKENSAFE, tools, dispatcher
    if arm == "oumv":
        return SYSTEM_OUMV, OUMV_TOOLS, registry(ws, arm)
    return SYSTEM_BASELINE, BASE_TOOLS, registry(ws, arm)


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
            "oumv_applyEdit": lambda path, expectedVersion, mode="", replacement="", symbol="", hunks=None: ws.apply_edit_scoped(
                path,
                expectedVersion,
                mode=mode,
                replacement=replacement,
                symbol=symbol,
                hunks=hunks,
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
    "You are a senior Go engineer working in a small Go module. The bug report has a repro; "
    "stop as soon as the oracle passes. Loop: (1) ts_smart_read_file(endpoint=<\"METHOD /path\" from the "
    "repro>) → the handler body, its file, its line range and the whole-file version in ONE call; when the "
    "route is registered in several apps the response lists each and shows all of them. "
    "(2) if the defect is in a callee named on the response's `calls:` line, read it with "
    "ts_smart_read_file(path=<that path>, symbol=<name>) — a symbol declared in another file is inlined in "
    "the same response, so one call is enough. Every slice is exact file text: use it verbatim as the hunk "
    "target, without re-reading to check. "
    "(3) ts_apply_edits with edits=[{path, hunks:[{target: exact snippet, replacement: fix, "
    'start_line: the L from the slice}]}] plus verify="auto" → edit and verdict in one call; '
    "(4) if verify fails, fix the reported line and repeat (1)-(3). "
    "Use ts_scoped_search only when the repro names no route. Skip ts_repo_map. Prefer these over "
    "list_files/read_file; never rewrite a whole file."
)

SYSTEM_OUMV = (
    "You are a senior Go engineer working in a small Go module that exposes the OUMV runtime. "
    "The bug report includes a repro; use it to go straight to the right handler instead of guessing payloads. "
    "Use this loop and stop as soon as the oracle passes: (1) oumv_observe L3 with the failing endpoint target "
    "(e.g. \"POST /api/products\") — ONE call returns the handler source, its workspace path, startLine/endLine and "
    "the sha256 OCC version; (2) oumv_applyEdit with that path + expectedVersion and mode=\"patch\", "
    "hunks=[{target: the exact line(s) you change, replacement: the fix, startLine: the line number from step 1, "
    "endLine: the same line}] — scoping the hunk keeps it unique when the snippet appears elsewhere in the file; "
    "the file is never re-sent and the edit is rejected on a "
    "stale version; (3) if the result is an error, read the reported reason and repeat (2); (4) run_command to run "
    "the oracle (oumv_verify starts a fresh app per call, so use it only for a single stateless request). "
    "Do not fetch whole files with getSource and do not use write_file: the patch IS the edit. Do not call "
    "list_files/read_file unless an OUMV tool returns an error. Tests are read-only."
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
