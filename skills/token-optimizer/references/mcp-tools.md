# Core MCP Tools Reference (`tokensafe`)

Exposed via Model Context Protocol (stdio JSON-RPC 2.0) as **7 consolidated tools** (all 28 legacy tool names remain supported as aliases).

| MCP Tool | Modes / Actions | Efficiency Impact |
|---|---|---|
| `tokensafe:smart_read_file` | Single `path+symbol` (`dep_mode`: `signature`\|`full`, `force_full`); `targets=[{path,symbol}]` bundle (2–8 symbols); `package_name+symbol` (`.d.ts`). Auto-slices OpenAPI/Prisma/GraphQL/SQL/lockfiles. | **-90% to -98%** token reduction |
| `tokensafe:scoped_search` | Single `query+scope` (`mode`: `auto`\|`text`\|`regex`\|`glob`\|`fuzzy`\|`git`); or `queries=[...]` batch (up to 20 concurrent queries in <70ms). | **-90% to -95%** vs grep/find |
| `tokensafe:apply_edits` | `edits=[...]` (up to 50 files, `best_effort`, `dry_run`) + optional `verify` command in 1 turn. | Replaces serial edit/test loops |
| `tokensafe:repo_map` | `view="graph"` (God Nodes & deps), `"api"` (exported types/classes), `"tree"` (clean dir tree). | **~200–500 tokens** vs 50k crawl |
| `tokensafe:session_context` | `action="budget"` (Grade S–F), `"compact"` (State Ledger), `"prune"` (superseded reads), `"mistakes"` (anti-stuck), `"metrics"`. | **-80% to -95%** history mass |
| `tokensafe:oumv` | `action="status"`\|`"observe"` (`L0–L4`)\|`"understand"`\|`"verify"` (sandbox)\|`"mutate"` (OCC)\|`"graph"`. | Sub-ms live runtime verification |
| `tokensafe:run_helper` | `action="sanitize"`\|`"guard"`\|`"diff"`\|`"deps"`\|`"web"`\|`"expand"`\|`"image"`\|`"compress"`. | Unified utility surface |

---

## Agent Workflows

### 1. Architecture, API Map & Directory Tree (`repo_map`)
```json
{"tool": "repo_map", "arguments": {"view": "graph", "project_path": "/path/to/repo"}}
```
*Use `"view": "api"` for exported interfaces/types and `"view": "tree"` (`depth=2`) instead of `ls -la`.*

### 2. Single & Batch Code Search (`scoped_search`)
```json
{"tool": "scoped_search", "arguments": {"query": "HARDCODED_VERSION", "path": "./src", "scope": "code"}}
```
```json
{"tool": "scoped_search", "arguments": {"queries": [{"pattern": "auth", "type": "text"}, {"pattern": "status --short", "type": "git"}]}}
```

### 3. Symbol, Bundle & External `.d.ts` Reads (`smart_read_file`)
```json
{"tool": "smart_read_file", "arguments": {"path": "src/views/StatsView.tsx", "symbol": "LuckyWheelTab", "dep_mode": "signature"}}
```
```json
{"tool": "smart_read_file", "arguments": {"targets": [{"path": "src/a.ts", "symbol": "fnA"}, {"path": "src/b.ts", "symbol": "fnB"}]}}
```
```json
{"tool": "smart_read_file", "arguments": {"package_name": "zod", "symbol": "ZodSchema"}}
```

### 4. Session & Context Hygiene (`session_context`)
```json
{"tool": "session_context", "arguments": {"action": "budget", "model": "claude-sonnet-4", "transcript_path": "/path/to/transcript.jsonl"}}
```
```json
{"tool": "session_context", "arguments": {"action": "compact", "transcript_path": "/path/to/transcript.jsonl", "invocation_num": 12}}
```

### 5. Rare Utilities (`run_helper`)
```json
{"tool": "run_helper", "arguments": {"action": "web", "url": "https://docs.example.com/api"}}
```
```json
{"tool": "run_helper", "arguments": {"action": "expand", "hash": "a1b2c3d4"}}
```
```json
{"tool": "run_helper", "arguments": {"action": "deps", "package_name": "zod"}}
```
```json
{"tool": "run_helper", "arguments": {"action": "guard", "command": "git add .env"}}
```
```json
{"tool": "run_helper", "arguments": {"action": "image", "image_path": "/path/to/screenshot.png", "max_edge": 1024}}
```
