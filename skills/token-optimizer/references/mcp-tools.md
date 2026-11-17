# Core MCP Tools Reference (`tokensafe`)

All tools are exposed via Model Context Protocol (stdio JSON-RPC 2.0).

| MCP Tool | Description | Efficiency Impact |
|---|---|---|
| `tokensafe:smart_read_file` | Graph-guided AST reader with `symbol` body retention, `dep_mode` (`signature` default / `full`), and session pointer dedup (`force_full`). OpenAPI/Swagger, Prisma, GraphQL and SQL files are auto-sliced: `symbol` = spec path/model/type/table; empty `symbol` returns a compact overview. Lockfiles (`package-lock.json`, `Cargo.lock`) and tabular data (`.csv`, `.tsv`, `.sql`, `.har`) auto-reduce with reversible hash anchors. | **-90% to -98%** token reduction |
| `tokensafe:expand_ref` | Expands a previously folded or stashed tool/command/file result by its 8-character hex hash anchor (e.g. `'a1b2c3d4'`). Returns the full original text that TokenSafe folded without re-running tools. | **Zero Data Loss** recovery |
| `tokensafe:read_web_page` | Fetches a web page URL and converts it to clean, readable Markdown, stripping scripts, styles, navbars, and ads. | **-80% to -95%** vs raw HTML fetches |
| `tokensafe:smart_bundle` | Multi-symbol context bundle: up to 8 `{path, symbol}` targets in ONE call — per-file skeletons with shared import lines deduplicated and merged 1-hop dependency signatures. Replaces 3+ chained `smart_read_file` calls. | **-85% to -95%** + one roundtrip instead of many |
| `tokensafe:scoped_search` | Safe, token-bounded keyword, symbol, or config search across the codebase. Accelerated by native SQLite FTS5 trigram index with BM25 relevance ranking (<1ms) and seamless regex fallback (supports `path='.'`, `scope='code'`/`'config'`/`'all'`/file extension/subfolder). | **-90% to -95%** vs raw grep/find |
| `tokensafe:list_dir` | Token-efficient directory tree without lockfiles, timestamps, or ignored folders (node_modules, .git, dist, .env). Replaces `ls -la`. | **-90%** vs verbose `ls` |
| `tokensafe:get_architecture_graph` | Computes dependency topology, hub centrality, and identifies top God Nodes for `project_path`. | **~200 tokens** (replaces 50k crawl) |
| `tokensafe:get_api_surface` | Generates a compact markdown map of all exported types, interfaces, and classes. | **~500 tokens** for entire project |
| `tokensafe:sanitize_command` | Per-stage command rewrites: lockfile diffs, unbounded `git log`/`git show`/`git blame`/`rg`, docker/kubectl log caps, quiet npm/pip installs, bounded `cat`/`curl` output, `git push` object-counting strip (`--no-progress`), and `git commit` hook wrapping. Compound commands (`a && b ; c`) are optimized per stage. Test commands (`bun test`, `pytest`, `python -m unittest`, `vitest`, `jest`) are wrapped or forced into run-once mode (`CI=true`, `--watch=false`, `--forceExit`) so passing suites emit a clean 1-line summary and agents never hang on watch mode. Pass optional `cwd` to identify runners behind opaque `yarn/npm test` scripts. | Saves **10k-50k tokens** per run |
| `tokensafe:slice_diff` | Slices unified git diffs, eliminating lockfiles and minimizing hunk context lines. | **-50% to -80%** diff size |
| `tokensafe:compress_prompt` | Compresses verbose user instructions into lean TOCP S-expressions via DeepSeek/Regex. | **-40% to -65%** prompt tokens |
| `tokensafe:compact_conversation` | On-demand context compaction: folds a long JSONL session transcript into a lean Consolidated State Ledger with a 3-line Post-Compaction Orientation Digest (`transcript_path`, optional `invocation_num`). Preserves flow instantly without post-compaction amnesia. | **-80% to -95%** history tokens |
| `tokensafe:context_budget` | Context Budget HUD & Context Quality Score (Grade S/A/B/C/F): estimated window usage and unified health grade (tools/history/prompts breakdown, static tool/skill tax, headroom %). Call before deciding to compact. | Floor estimate, always labeled estimated |
| `tokensafe:guard_inspect` | Pre-flight security & hygiene audit: denies literal secrets/destructive commands in shell lines (secrets, TLS, SQLi, raw grep); advisory for never-terminating streams (`tail -f`, `watch`, `docker attach`) and TLS bypasses. | **Zero-Risk** pre-flight gate |
| `tokensafe:optimize_image` | Downscales oversized screenshots/diagrams (>1024px) before LLM vision ingestion while preserving original disk files and aspect ratios. | **-50% to -75%** vision tokens |
| `tokensafe:prune_superseded` | Prunes obsolete intermediate tool read outputs in long transcripts that were superseded by subsequent edits or re-reads. | **-30% to -40%** context mass |
| `tokensafe:get_mistakes` | Anti-stuck persistent ledger: retrieves past dead-ends, loop guard trips, and git reverts to prevent cycling through failed solutions. | **Anti-loop / Zero Repetition** |
| `tokensafe:get_token_savings_metrics` | Returns cumulative analytics: tokens saved (measured/estimated split), context ratio, ≈USD value (derived), and optimization source breakdown. | Persistent SQLite analytics |

---

## Agent Workflows

### 1. Architecture Exploration Workflow
```json
{
  "ServerName": "tokensafe",
  "ToolName": "get_architecture_graph",
  "Arguments": {
    "project_path": "/Users/.../projects/my-app"
  }
}
```
*Identifies core architectural hubs and dependency weights before inspecting files.*

### 2. Scoped Code & Config Search Workflow
```json
{
  "ServerName": "tokensafe",
  "ToolName": "scoped_search",
  "Arguments": {
    "query": "HARDCODED_VERSION",
    "path": "./src",
    "scope": "code"
  }
}
```
*Locates exact occurrences with file and line references in < 100 tokens. Always pass `path` (defaults to active project root, e.g. ".") and optional `scope` ('code', 'config', 'all', file extension like 'go', or subfolder like 'internal/promo'). Auto-guarded against system roots.*

### 2b. Directory Navigation Workflow (`list_dir`)
```json
{
  "ServerName": "tokensafe",
  "ToolName": "list_dir",
  "Arguments": {
    "path": "/Users/.../projects/my-app/src",
    "depth": 2
  }
}
```
*Renders a clean directory tree without permissions, timestamps, lockfiles, or heavy directories (`node_modules`, `.git`, `dist`).*

### 3. Targeted Symbol Inspection Workflow
```json
{
  "ServerName": "tokensafe",
  "ToolName": "smart_read_file",
  "Arguments": {
    "path": "/Users/.../projects/my-app/src/views/StatsView.tsx",
    "symbol": "LuckyWheelTab",
    "dep_mode": "signature"
  }
}
```
*Extracts the targeted `LuckyWheelTab` implementation plus compact 1-line signatures for 1-hop dependencies with inert code and imports folded. Pass `"dep_mode": "full"` for full dependency bodies, `"include_deps": false` for 0-hop isolation, or `"force_full": true` to bypass a session pointer hit.*

### 4. Public API Map Workflow
```json
{
  "ServerName": "tokensafe",
  "ToolName": "get_api_surface",
  "Arguments": {
    "project_path": "/Users/.../projects/my-app"
  }
}
```
*Returns all exported interfaces and types without reading hundreds of module files.*

### 5. Pre-flight Security & Hygiene Audit
```json
{
  "ServerName": "tokensafe",
  "ToolName": "guard_inspect",
  "Arguments": {
    "command": "git add .env",
    "file_path": "src/auth.ts"
  }
}
```
*Validates commands and code snippets before execution; returns structured pass/block verdict with check_name.*

### 6. Context Budget → Smart Compact Workflow
```json
{
  "ServerName": "tokensafe",
  "ToolName": "context_budget",
  "Arguments": {
    "model": "claude-x"
  }
}
```
*Run before large multi-step tasks. When the response reports `threshold: "warning"` or `"critical"`, immediately chain the compact call:*

```json
{
  "ServerName": "tokensafe",
  "ToolName": "compact_conversation",
  "Arguments": {
    "transcript_path": "/Users/.../.claude/projects/<session>/transcript.jsonl",
    "invocation_num": 12
  }
}
```
*Folds past turns into a Consolidated State Ledger (-80% to -95% history tokens) so long sessions never degrade into context starvation. The transcript path is provided by the host session (hook payload `transcript_path` / session start context).*

### 7. Vision Image Optimizer (`optimize_image`)
```json
{
  "ServerName": "tokensafe",
  "ToolName": "optimize_image",
  "Arguments": {
    "image_path": "/path/to/screenshot.png",
    "max_edge": 1024
  }
}
```
*Downscales oversized 2K/4K screenshots to max 1024px, saving 50-75% vision tokens while keeping original files intact on disk.*

### 8. Superseded Tool Output Pruning (`prune_superseded`)
```json
{
  "ServerName": "tokensafe",
  "ToolName": "prune_superseded",
  "Arguments": {
    "transcript_path": "/path/to/transcript.jsonl"
  }
}
```
*Replaces outdated intermediate file reads with reversible 1-line stashed tombstones (-30-40% context mass in multi-turn sessions).*

### 9. Anti-Stuck Mistakes Memory (`get_mistakes`)
```json
{
  "ServerName": "tokensafe",
  "ToolName": "get_mistakes",
  "Arguments": {
    "project_root": "."
  }
}
```
*Retrieves active dead-ends, failed hypotheses, and git reverts to prevent agents from attempting known failing solutions.*
