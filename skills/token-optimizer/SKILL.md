---
name: token-optimizer
description: >-
  MANDATORY code inspection, architecture navigation, and live runtime verification skill.
  Activate whenever inspecting files, searching code, locating components/functions/classes,
  reading implementations, analyzing dependencies, navigating architecture, or interacting with live systems.
  Replaces wasteful terminal grep/find with zero-loss AST smart slicing (-90% tokens) and enables
  OUMV (Observe, Understand, Mutate, Verify) runtime verification across frontend and backend dev-servers.
---

# TokenSafe Optimization, Architecture & OUMV Runtime Skill

TokenSafe is the zero-loss token economy, context governor, security engine, and runtime verification layer for autonomous AI coding agents.
It replaces brute-force terminal crawls (`grep`, `find`, `cat`) with deterministic AST symbol extraction,
folds multi-turn conversation history, guards against secret leaks, and standardizes live runtime interaction via the **Universal OUMV Protocol (UARP)**.

---

## MANDATORY AGENT DIRECTIVES

1. **ZERO RAW TERMINAL GREP / FIND (HARD-BLOCKED & REPLACED BY `scoped_search`)**:
   - Strictly NEVER run `grep`, `egrep`, `ripgrep`, or `find` in `run_command` on source code files (`.ts`, `.tsx`, `.py`, `.js`, `src/`, `views/`, `components/`).
   - **Mandatory Search Tool (`scoped_search`)**: Call `tokensafe:scoped_search(query, path=".", scope="code")` for single searches (`mode="auto|text|regex|glob|fuzzy|git"`), or pass `queries=[{pattern, type, path, glob}]` for up to 20 concurrent queries in <70ms.

2. **SYMBOL, BUNDLE & EXTERNAL TYPE EXTRACTION (`smart_read_file`)**:
   - Call `tokensafe:smart_read_file` with `path` and `symbol` (`dep_mode="signature"` default, `"full"` for dependency bodies, `"include_deps": false` for 0-hop isolation, `"force_full": true` only after compaction or as a fresh subagent).
   - **Multi-Symbol Bundles**: Pass `targets=[{path, symbol}]` (up to 8 targets) to merge skeletons and deduplicate shared imports in ONE call.
   - **External `.d.ts` Types**: Pass `package_name` + `symbol` to slice 3rd-party TypeScript definitions from `node_modules` (-95% tokens).
   - **Schema Auto-Slicing**: OpenAPI/Prisma/GraphQL/SQL files auto-slice via `symbol` (empty `symbol` returns an overview).

3. **ARCHITECTURE GRAPH, API SURFACE & DIRECTORY TREE (`repo_map`)**:
   - First call `tokensafe:repo_map(view="graph", project_path=...)` to inspect God Nodes and dependency hubs (<250 tokens) before reading files.
   - Use `view="api"` for exported classes/types/interfaces, and `view="tree"` (depth=2) instead of `ls -la`.

4. **SURGICAL FILE EDITS (`apply_edits` / `edit_file`) & STREAM GUARD (`sed -i` BLOCKED)**:
   - Use `tokensafe:apply_edits(edits=[...], verify="npm test")` for >=2 hunks/files or edit + verify loops.
   - On existing files with >= 40 lines, wholesale rewrites via `write_file` are blocked by **Output Guard**. Use `edit_file` or `apply_edits`.
   - NEVER run `sed -i`, `perl -i`, `awk -i` on source code or suppress errors (`2>/dev/null`).

5. **SECRET LEAK PREVENTION (ZERO HARDCODED FALLBACKS)**:
   - Never hardcode real private keys, API secrets, or credentials as fallback values in `os.environ.get()` or `process.env`.

6. **SESSION & CONTEXT HYGIENE (`session_context`)**:
   - `action="budget"` (`model=...`, `transcript_path=...`): check window usage and Context Quality Score (Grade S–F).
   - `action="compact"` (`transcript_path=...`): fold past turns into a Consolidated State Ledger with a 3-line Post-Compaction Orientation Digest.
   - `action="prune"` (`transcript_path=...`): drop superseded intermediate file reads.
   - `action="mistakes"` (`project_root=...`): query anti-stuck memory of prior dead-ends and reverts.
   - `action="metrics"`: cumulative savings analytics.

7. **UTILITIES (`run_helper`)**:
   - `action="expand"` (`hash`): restore full original text of a folded `#hash` block.
   - `action="web"` (`url`): fetch docs/pages as clean Markdown (L1/L2/L3 cached).
   - `action="deps"` (`package_name`, `lockfile_path`): point lockfile lookup in <50 tokens.
   - `action="sanitize"` (`command`, `cwd`): rewrite noisy shell/test commands.
   - `action="guard"` (`command`/`code`, `file_path`): pre-flight security/hygiene audit.
   - `action="diff"` (`diff`): slice unified diffs.
   - `action="image"` (`image_path`, `max_edge=1024`): downscale oversized screenshots (-50-75% vision tokens).
   - `action="compress"` (`prompt`): TOCP S-expression prompt compression.

8. **COMPACT TEST EXECUTION & MULTIMODAL ISOLATION**:
   - Run unit tests via `tokensafe tests [cmd]` or `tokensafe wrap '<cmd>'` (1-line summary on pass, full traceback on failure).
   - Never load raw screenshots into long (>150k) sessions; delegate visual inspection to a subagent returning text-only findings.

---

## Core MCP Tools (`tokensafe` — 7 Consolidated Tools)

- [`references/mcp-tools.md`](references/mcp-tools.md) — Core MCP Tools table + JSON workflow examples
- [`references/guard-checks.md`](references/guard-checks.md) — Security & Hygiene Guard check matrix
- [`references/cli-reference.md`](references/cli-reference.md) — `tokensafe` CLI command reference

Quick reference:
- `tokensafe:smart_read_file(path, symbol)` / `(targets=[...])` / `(package_name, symbol)`
- `tokensafe:scoped_search(query, path=".", scope="code")` / `(queries=[...])`
- `tokensafe:apply_edits(edits=[...], verify="...")`
- `tokensafe:repo_map(view="graph"|"api"|"tree", project_path=".")`
- `tokensafe:session_context(action="budget"|"compact"|"prune"|"mistakes"|"metrics", ...)`
- `tokensafe:oumv(action="status"|"observe"|"understand"|"verify"|"mutate"|"graph", ...)`
- `tokensafe:run_helper(action="sanitize"|"guard"|"diff"|"deps"|"web"|"expand"|"image"|"compress", ...)`

---

## Context Governor & History Compactor

For long-running autonomous sessions, TokenSafe actively manages context bloat:

1. **Multi-Turn History Compaction & Post-Compaction Orientation Digest**:
   - At conversation turn >= 3, automatically compiles past turns into a lean **Consolidated State Ledger** paired with a deterministic 3-line **Post-Compaction Orientation Digest** (`1. Active Goal: ...`, `2. Last State: ...`, `3. Next Focus: ...`).
   - Preserves active file paths, modified symbols, and pending tasks while discarding transient tool output noise and preventing post-compaction amnesia.
   - **Manual trigger**: call `tokensafe:compact_conversation` with the session's `transcript_path` any time you need to shed context between subtasks — it returns the ledger JSON, orientation digest, and exact token counts.
2. **Subagent Deduplication Firewall**:
   - Caches file reads across subagents and sessions using SHA-256 content hashes. Interactive MCP clients receive self-healing pointer hits on unchanged repeat reads inside the same session; fresh subagents or post-compaction callers recover automatically with `force_full=true`.
3. **Loop Guard**:
   - Detects repeated identical tool calls and triggers `force_ask` to prevent runaway infinite agent loops.
4. **Output Token Guard**:
   - Intercepts wholesale `write_file` calls on existing files with >= 40 lines to prevent expensive output token burn.

---

## LLM Prompt Cache Optimization

TokenSafe actively maximizes provider KV-cache hit rates alongside token compression:

1. **Append-Only Epoch Architecture**: In proxy mode, past messages (`messages[0..N-2]`) are never retroactively mutated within an epoch, preserving prefix cache hits. When cumulative context reaches **80% of the model's window**, an atomic Epoch Rollover consolidates deep history into a State Ledger.
2. **Multi-Breakpoint Cache Control**: `PromptCacheNormalizer` sorts `tools` deterministically and places Anthropic `cache_control: {"type": "ephemeral"}` across tools, static system instructions, and the penultimate completed turn; OpenAI requests receive deterministic `prompt_cache_key`.
3. **Deterministic Prefix Normalization**: Ephemeral timestamps, hit counters, and session IDs are stripped from static prefixes, and `AgentFirewall` headers remain byte-deterministic across hits.
4. **Parametric Prompt Template Caching**: `DistillationCache` extracts file paths and symbols into positional slots (`<FILE_0>`, `<SYM_0>`) so structurally identical prompts hit L1/L2/L3 caches with 0 LLM roundtrips.

---

## Universal OUMV Protocol (Runtime Introspection & Verification)

TokenSafe integrates the **Universal Agent-Runtime Protocol (UARP / OUMV)** to inspect, reason about, edit, and verify running Frontend (`tokensafe-ui`, React 18/19 with Zustand/Redux/Context/useReducer, Vue 3) and Backend (FastAPI, Go, Node.js) services in real time without heavy headless browsers or unconstrained shell curls:

1. **LIVE RUNTIME HEALTH (`oumv(action="status")`)**:
   - Call `tokensafe:oumv(action="status")` to inspect connection status and capabilities of running DevServers (Frontend ports 5173/3000, Backend ports 8080/8000).

2. **PROGRESSIVE CONTEXT OBSERVATION (`oumv(action="observe")`)**:
   - Never dump raw HTML or giant JSON payloads. Observe progressively across native components, React, Vue, or backend APIs:
     - **L0 (Page Pulse ~20 tokens)**: Status, active route, mounted components/roots, error counts (`level="L0"`).
     - **L1 (Surface ~150 tokens)**: Interactive map of endpoints, models, buttons, and ARIA roles (`level="L1"`).
     - **L2 (Contract ~80 tokens)**: Targeted component state contract, props, Zod schema, or API DTO schemas (`level="L2", target="ModelName"`).
     - **L3 (Source)**: Targeted symbol source with line mapping and OCC version hash token.
     - **L4 (Traces)**: Event stream, signal mutations, store subscriptions (Zustand/Redux/Context updates), and state snapshot diffs.

3. **RUNTIME CAUSALITY & WHY-STATE (`oumv(action="understand")`)**:
   - Never guess state flows or assume dependencies.
   - Call `tokensafe:oumv(action="understand", query="causality", target="POST /api/orders")` to query live dependencies:
     `Route ➔ Handler ➔ ValidatesWith ➔ Model ➔ DB Table` or `Signal ➔ Computed ➔ Effect ➔ DOM Binding`.
   - Call `tokensafe:oumv(action="understand", query="impact", target="cart")` before refactoring to know exactly what breaks. Use `action="graph"` for the cross-stack graph.

4. **SANDBOXED ACTION VERIFICATION (`oumv(action="verify")`)**:
   - Sub-millisecond to low-millisecond local action verification inside transactional sandbox.
   - **For backend endpoints**: call `tokensafe:oumv(action="verify", target="POST /api/products", payload={...})` — executes in-memory with automatic DB rollback (`rolledBack: true`).
   - **For frontend UI**: call `tokensafe:oumv(action="verify", target="cart", verify_action="addItem", payload={...})` — executes synthetic events/store actions in memory without browser automation.

5. **OPTIMISTIC CONCURRENCY MUTATIONS (`oumv(action="mutate")`)**:
   - Submits surgical edits verified against `expectedVersion` with built-in Babel AST TSX/JSX syntax guards and security leak prevention.

---

## Greenfield Ecosystem Architecture & Component Generation

When building greenfield applications with the TokenSafe ecosystem (`tokensafe create` or `tokensafe init`):

### 1. Frontend: `tokensafe-ui`
`tokensafe-ui` is a deterministic, token-efficient frontend framework designed for AI coding agents. It provides zero-bundle DOM parity, reactive state, and automatic OUMV introspection.

**Component Anatomy (`src/App.tsx` / `src/components/`):**
```typescript
import { defineComponent, z, bind, h } from 'tokensafe-ui';

export const UserCard = defineComponent({
  id: 'user-card',
  // Zod-enforced reactive state (introspectable via oumv_observe L2)
  state: {
    username: z.string().default('Alice'),
    active: z.boolean().default(true),
    score: z.number().default(100),
  },
  // Pure actions for deterministic simulation in oumv_verify
  actions: {
    toggleActive: (s) => ({ ...s, active: !s.active }),
    addScore: (s, delta: number) => ({ ...s, score: s.score + delta }),
    setName: (s, username: string) => ({ ...s, username }),
  },
  // HyperScript rendering with required data-agent-id attributes for deterministic targeting
  render: ({ state, emit }) =>
    h('div', { class: 'card', 'data-agent-id': 'user-card:container' },
      h('h2', { 'data-agent-id': 'user-card:title' }, state.username),
      h('p', { 'data-agent-id': 'user-card:status' }, state.active ? 'Online' : 'Offline'),
      h('button', {
        'data-agent-id': 'user-card:btn-toggle',
        onClick: () => emit.toggleActive()
      }, 'Toggle Status'),
      h('input', {
        'data-agent-id': 'user-card:input-name',
        ...bind(emit.setName, state.username)
      })
    )
});
```

### 2. Backend: `tokensafe-python` (FastAPI)
`tokensafe-python` instruments FastAPI services with UARP JSON-RPC 2.0 (`/__oumv/rpc`) and SQLAlchemy transactional sandboxes.

```python
from fastapi import FastAPI
from pydantic import BaseModel, Field
from tokensafe_python.adapters.fastapi import mount_tokensafe

app = FastAPI(title="App API")

class TaskCreate(BaseModel):
    title: str = Field(..., example="Implement feature")
    done: bool = False

@app.post("/api/tasks", status_code=201)
def create_task(task: TaskCreate):
    return {"id": 1, **task.model_dump()}

# 1-line UARP mounting: registers /__oumv/rpc, /__oumv/health, and writes .tokensafe/runtime.json
mount_tokensafe(app, port=8000)
```

---

## Proof-of-Savings Turn Footer (always in English)

At the end of any response where tools were executed, files were inspected, commands were sanitized, or prompts were compacted, append a compact 1-line footer in English:
`⚡ **TokenSafe Turn Savings**: +<tokens> tokens saved on this turn (<breakdown>) | Cumulative Total: <total> tokens (≈$<usd> approx)`

The USD figure is **derived** (tokens × $1.5/M blended rate) — an approximation for planning, never a bill. Cumulative analytics distinguish `measured` (metered I/O delta) from `estimated` (counterfactual baseline) savings — see METRICS.md.
