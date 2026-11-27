"""React sandbox target for the measured benchmark.

Workspace: a throwaway copy of `agent-dom/packages/benchmark` placed *inside* the
repo (`agent-dom/.bench-tmp-*`) so Node resolves react/tsx from the monorepo's
node_modules. The agent edits the real React sandbox sources; correctness is the
12-assertion TaskManagerOracle executed by `src/bin/oracle-runner.ts` after every
step.

The OUMV arm for this target needs the Vite bridge (`tokensafe dev`), so the first
React pilot measures the `baseline` arm only — the Go pilot already compares both.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.bench import tokensafe_mcp

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_DOM = REPO_ROOT / "agent-dom"
PACKAGE = AGENT_DOM / "packages" / "benchmark"
WRITE_PREFIX = "sandboxes/react-task-manager/src/"
STORE = f"{WRITE_PREFIX}store.ts"

TASKS: Dict[str, Dict[str, Any]] = {
    "search-filter": {
        "symptom": "typing in the search box no longer filters the task list (the query is ignored).",
        "repro": "set the search query to 'milk': the list still shows every task instead of only matching titles/tags.",
        "breaks": [(
            STORE,
            "    if (state.searchQuery.trim()) {\n      const q = state.searchQuery.toLowerCase();\n      const inTitle = task.title.toLowerCase().includes(q);\n      const inTags = task.tags?.some((tag) => tag.toLowerCase().includes(q)) ?? false;\n      if (!inTitle && !inTags) return false;\n    }\n",
            "",
        )],
    },
    "optimistic-rollback": {
        "symptom": "when the background sync fails, the toggled task keeps the optimistic status instead of rolling back.",
        "repro": "toggle a task while the mock backend rejects the sync: the task must return to its previous status and show an error.",
        "breaks": [(
            STORE,
            """    } catch (err: any) {
      this.set({
        tasks: this.state.tasks.map((t) => (t.id === id ? { ...t, status: prior.status } : t)),
        errorMessage: err?.message || 'Optimistic sync failed',
      });
      throw err;
    }""",
            """    } catch (err: any) {
      this.set({ errorMessage: err?.message || 'Optimistic sync failed' });
      throw err;
    }""",
        )],
    },
    "saving-indicator": {
        "symptom": "the save button is never disabled while the request is in flight, so users double-submit.",
        "repro": "click Save once: isSaving must become true for the duration of the request; it stays false.",
        "breaks": [(
            STORE,
            "    this.set({ isSaving: true, errorMessage: null, successMessage: null });",
            "    this.set({ errorMessage: null, successMessage: null });",
        )],
    },
}


OUMV_STDIO = AGENT_DOM / "packages" / "vite" / "scripts" / "oumv-stdio.mjs"


class OumvStdio:
    """Newline-delimited JSON-RPC client for the shipped React stdio adapter.

    The adapter serves the same controller `tokensafe dev` uses; in headless mode
    L0-L3 come from the static AST, which is exactly what an agent gets before a
    browser tab connects. One process per workspace, closed with the workspace.
    """

    def __init__(self, root: Path):
        self.proc = subprocess.Popen(
            ["node", str(OUMV_STDIO), "--root", str(root)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(AGENT_DOM),
        )
        self._id = 0

    def request(self, method: str, params: Dict[str, Any]) -> Any:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        try:
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()
            line = self.proc.stdout.readline()
        except (BrokenPipeError, ValueError) as exc:
            return {"error": f"OUMV adapter is not running: {exc}"}
        if not line:
            stderr = (self.proc.stderr.read() or "")[-300:] if self.proc.stderr else ""
            return {"error": f"OUMV adapter produced no output: {stderr}"}
        try:
            body = json.loads(line)
        except json.JSONDecodeError:
            return {"error": f"invalid JSON from adapter: {line[:200]}"}
        if body.get("error"):
            return {"error": body["error"]}
        return body.get("result")

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


class ReactWorkspace:
    def __init__(self, task_id: str):
        if task_id not in TASKS:
            raise ValueError(f"unknown task {task_id}; valid: {', '.join(TASKS)}")
        self.task_id = task_id
        self._rpc: Optional[OumvStdio] = None
        self._mcp = None
        base = AGENT_DOM / f".bench-tmp-{next(tempfile._get_candidate_names())}"
        self.dir = base / "benchmark"
        base.mkdir(parents=True)
        shutil.copytree(
            PACKAGE,
            self.dir,
            ignore=shutil.ignore_patterns("node_modules", "dist", ".turbo", "*.log"),
        )
        # Reuse the package's installed deps (react, happy-dom, tsx) instead of
        # copying them: the temp dir lives inside the repo, so a symlink is enough.
        if (PACKAGE / "node_modules").is_dir():
            os.symlink(PACKAGE / "node_modules", self.dir / "node_modules")
        for rel, old, new in TASKS[task_id]["breaks"]:
            path = self.dir / rel
            text = path.read_text(encoding="utf-8")
            if old not in text:
                raise RuntimeError(f"break target not found in {rel}")
            path.write_text(text.replace(old, new, 1), encoding="utf-8")

    # ---- oracle -----------------------------------------------------------
    def oracle(self) -> Tuple[int, List[str], str]:
        proc = subprocess.run(
            ["npx", "tsx", "src/bin/oracle-runner.ts"],
            cwd=str(self.dir),
            capture_output=True,
            text=True,
            timeout=300,
        )
        out = (proc.stdout or "").strip().splitlines()
        for line in reversed(out):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            return int(payload.get("passed", 0)), list(payload.get("failed", [])), "PASS" if payload.get("passed") == 12 else "FAIL"
        tail = ((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()
        detail = " | ".join(tail[-3:])[:400] if tail else "no output"
        return 0, [], f"RUNNER ERROR: {detail}"

    # ---- tools ------------------------------------------------------------
    def _resolve(self, rel: str) -> Path:
        candidate = (self.dir / rel).resolve()
        root = self.dir.resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"path escapes the workspace: {rel}")
        return candidate

    def list_files(self, path: str = "sandboxes", depth: int = 3) -> Any:
        base = self._resolve(path)
        if not base.exists():
            return {"error": f"no such path: {path}"}
        lines: List[str] = []
        for entry in sorted(base.rglob("*")):
            rel = entry.relative_to(base)
            if len(rel.parts) > max(0, min(int(depth), 4)):
                continue
            lines.append(f"{rel}{'/' if entry.is_dir() else ''}")
            if len(lines) >= 120:
                lines.append("… truncated")
                break
        return {"path": path, "entries": lines}

    def read_file(self, path: str, start: int = 1, end: int = 180) -> Any:
        target = self._resolve(path)
        if not target.is_file():
            return {"error": f"not a file: {path}"}
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(start))
        end = min(len(lines), max(start, int(end)))
        return {
            "path": path,
            "total_lines": len(lines),
            "content": "\n".join(f"{i:>4}: {lines[i-1]}" for i in range(start, end + 1)),
        }

    def write_file(self, path: str, content: str) -> Any:
        if not path.startswith(WRITE_PREFIX) or not path.endswith((".ts", ".tsx")):
            return {"error": f"writes are limited to {WRITE_PREFIX}**/*.ts(x)"}
        if path.endswith("adapter.ts"):
            return {"error": "the adapter is read-only; edit the component/store"}
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"ok": True, "path": path, "bytes": len(content.encode())}

    def run_command(self, cmd: str) -> Any:
        if not cmd.strip().startswith("npx tsx src/bin/oracle-runner.ts"):
            return {"error": "only `npx tsx src/bin/oracle-runner.ts` is allowed"}
        proc = subprocess.run(cmd.split(), cwd=str(self.dir), capture_output=True, text=True, timeout=300)
        return {"exit_code": proc.returncode, "output": ((proc.stdout or "") + (proc.stderr or "")).strip()[-2000:]}

    def search_files(self, query: str, path: str = "sandboxes", max_hits: int = 40) -> Any:
        """Case-insensitive substring search — the grep a normal agent would have."""
        base = self._resolve(path)
        needle = query.strip().lower()
        if not needle:
            return {"error": "query is required"}
        max_hits = max(1, min(int(max_hits), 100))
        hits: List[str] = []
        for entry in sorted(base.rglob("*")):
            if not entry.is_file() or entry.suffix not in {".ts", ".tsx", ".js", ".jsx", ".json"}:
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
    def oumv(self, method: str, params: Dict[str, Any]) -> Any:
        if self._rpc is None:
            self._rpc = OumvStdio(self.dir)
        return self._rpc.request(method, params)

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
        if self._rpc is not None:
            self._rpc.close()
            self._rpc = None
        shutil.rmtree(self.dir.parent, ignore_errors=True)


TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List sandbox files (start from 'sandboxes').",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "depth": {"type": "integer"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a line range of a sandbox file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Replace a sandbox source file (sandboxes/react-task-manager/src/**, not adapter.ts).",
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
            "description": "Case-insensitive substring search across sandbox sources, returns path:line hits.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string", "description": "Relative directory, default 'sandboxes'"},
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
            "description": "Run the 12-assertion oracle (`npx tsx src/bin/oracle-runner.ts`).",
            "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
        },
    },
]


def registry(ws: ReactWorkspace, arm: str = "baseline") -> Dict[str, Any]:
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
            "ts_scoped_search": lambda query, path="sandboxes": ws.tokensafe(
                "scoped_search", {"query": query, "path": path, "max_results": 20}
            ),
            "ts_repo_map": lambda view="graph": ws.tokensafe("repo_map", {"view": view, "project_path": "."}),
            "ts_apply_edits": lambda path, edits, verify="": ws.tokensafe(
                "apply_edits",
                {
                    "edits": [{"path": path, "hunks": edits}],
                    **({"verify": verify} if verify else {}),
                },
            ),
        })
    if arm == "oumv":
        tools.update({
            "oumv_observe": lambda level, target="": ws.oumv("oumv/observe", {"level": level, "target": target}),
            "oumv_getSource": lambda path, symbol="": ws.oumv("oumv/getSource", {"path": path, "symbol": symbol}),
            "oumv_applyEdit": lambda path, replacement, expectedVersion: ws.oumv(
                "oumv/applyEdit", {"path": path, "replacement": replacement, "expectedVersion": expectedVersion}
            ),
            "oumv_understand": lambda query, target: ws.oumv("oumv/understand", {"query": query, "target": target}),
        })
    return tools


TOKENSAFE_TOOLS: List[Dict[str, Any]] = TOOLS + [
    {
        "type": "function",
        "function": {
            "name": "ts_smart_read_file",
            "description": "TokenSafe AST-skeleton reader: a compact symbol view of one file.",
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
            "description": "TokenSafe scoped search returning file:line hits inside the sandbox.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "path": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ts_repo_map",
            "description": "TokenSafe architecture map: high-dependency files and API surface.",
            "parameters": {
                "type": "object",
                "properties": {"view": {"type": "string", "enum": ["graph", "api", "tree"]}},
                "required": ["view"],
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
]

OUMV_TOOLS: List[Dict[str, Any]] = TOOLS + [
    {
        "type": "function",
        "function": {
            "name": "oumv_observe",
            "description": "OUMV observe: L0 pulse, L1 component surface, L2 component contract, L3 source.",
            "parameters": {
                "type": "object",
                "properties": {
                    "level": {"type": "string", "enum": ["L0", "L1", "L2", "L3"]},
                    "target": {"type": "string", "description": "e.g. 'sandboxes/react-task-manager/src/store.ts'"},
                },
                "required": ["level"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "oumv_getSource",
            "description": "OUMV source read with the sha256 OCC version token; pass `symbol` to get just that function, method or class.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "symbol": {"type": "string"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "oumv_applyEdit",
            "description": "OUMV guarded edit: writes the replacement only if `expectedVersion` from getSource matches the file on disk.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "replacement": {"type": "string"},
                    "expectedVersion": {"type": "string", "description": "sha256 version from getSource"},
                },
                "required": ["path", "replacement", "expectedVersion"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "oumv_understand",
            "description": "OUMV understand: impact analysis (importers and components affected by a file or symbol).",
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


SYSTEM = (
    "You are a senior React engineer working in a small sandbox. Use search_files to locate code, "
    "read_file to inspect it, write_file to fix the bug, and run_command to run the 12-assertion oracle. "
    "The oracle output is JSON with `passed` and `failed` assertion ids. Tests and adapter.ts are read-only. "
    "Make the smallest correct change and stop as soon as the oracle reports 12."
)

SYSTEM_TOKENSAFE = (
    "You are a senior React engineer working in a small sandbox. The workspace has TokenSafe tools. "
    "Use this loop and stop as soon as the oracle reports 12: (1) ts_scoped_search for the behaviour named "
    "in the repro to get file:line hits; (2) ts_smart_read_file with that path and the identifier from the hit "
    "as `symbol` — it returns the enclosing component/handler body (or a window around the hit) with line "
    "numbers, and inlines the body when the identifier is declared in another file, so one call is enough; "
    "(3) ts_apply_edits with one hunk {target: the exact snippet from that slice, replacement: the fix} plus "
    'verify="auto" (the declared test command) — it edits and verifies in one call. The slice is exact file '
    "text: do not re-read_file to check it. (4) if verify fails, read the reported line and repeat (2)-(3). "
    "ts_repo_map is for an unfamiliar repository; skip it when the repro already names the component. Prefer "
    "these tools over list_files/read_file. Tests and adapter.ts are read-only. Do not rewrite whole files."
)

SYSTEM_OUMV = (
    "You are a senior React engineer working in a small sandbox that exposes the OUMV runtime. "
    "Use this loop and stop as soon as the oracle reports 12: (1) search_files ONCE for the behaviour named in "
    "the repro; only call oumv_observe L1 if that finds nothing; (2) oumv_getSource ONCE on the file, WITHOUT "
    "`symbol` — it returns the full bytes and the sha256 OCC version (`symbol` is only for inspecting a single "
    "function or method after you already have the file); (3) oumv_applyEdit with the full fixed file and that "
    "version (it rejects a stale version); (4) run_command to run the oracle. Do not re-read a file you already "
    "have in context, and do not use write_file unless an OUMV tool returns an error. Tests and adapter.ts are "
    "read-only."
)


def task_prompt(task_id: str) -> str:
    task = TASKS[task_id]
    return (
        f"The task manager has a regression: {task['symptom']}\n"
        f"Repro: {task['repro']}\n"
        "Find the cause and fix the sandbox source. "
        "Verification: `npx tsx src/bin/oracle-runner.ts` must print {\"passed\":12,\"failed\":[]}. "
        "Do not modify the oracle, adapter.ts, or the test harness."
    )
