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

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_DOM = REPO_ROOT / "agent-dom"
PACKAGE = AGENT_DOM / "packages" / "benchmark"
WRITE_PREFIX = "sandboxes/react-task-manager/src/"
STORE = f"{WRITE_PREFIX}store.ts"

TASKS: Dict[str, Dict[str, Any]] = {
    "search-filter": {
        "symptom": "typing in the search box no longer filters the task list (the query is ignored).",
        "breaks": [(
            STORE,
            "    if (state.searchQuery.trim()) {\n      const q = state.searchQuery.toLowerCase();\n      const inTitle = task.title.toLowerCase().includes(q);\n      const inTags = task.tags?.some((tag) => tag.toLowerCase().includes(q)) ?? false;\n      if (!inTitle && !inTags) return false;\n    }\n",
            "",
        )],
    },
    "optimistic-rollback": {
        "symptom": "when the background sync fails, the toggled task keeps the optimistic status instead of rolling back.",
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
        "breaks": [(
            STORE,
            "    this.set({ isSaving: true, errorMessage: null, successMessage: null });",
            "    this.set({ errorMessage: null, successMessage: null });",
        )],
    },
}


class ReactWorkspace:
    def __init__(self, task_id: str):
        if task_id not in TASKS:
            raise ValueError(f"unknown task {task_id}; valid: {', '.join(TASKS)}")
        self.task_id = task_id
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

    def cleanup(self) -> None:
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
            "name": "run_command",
            "description": "Run the 12-assertion oracle (`npx tsx src/bin/oracle-runner.ts`).",
            "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
        },
    },
]


def registry(ws: ReactWorkspace) -> Dict[str, Any]:
    return {
        "list_files": ws.list_files,
        "read_file": ws.read_file,
        "write_file": ws.write_file,
        "run_command": ws.run_command,
    }


SYSTEM = (
    "You are a senior React engineer working in a small sandbox. Use list_files/read_file to inspect the "
    "component, store and adapters; write_file to fix the bug; run_command to run the 12-assertion oracle. "
    "The oracle output is JSON with `passed` and `failed` assertion ids. Tests and adapter.ts are read-only. "
    "Make the smallest correct change and stop as soon as the oracle reports 12."
)


def task_prompt(task_id: str) -> str:
    return (
        f"The task manager has a regression: {TASKS[task_id]['symptom']}\n"
        "Find the cause and fix the sandbox source. "
        "Verification: `npx tsx src/bin/oracle-runner.ts` must print {\"passed\":12,\"failed\":[]}. "
        "Do not modify the oracle, adapter.ts, or the test harness."
    )
