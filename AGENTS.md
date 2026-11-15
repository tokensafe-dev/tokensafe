# TokenSafe Project Instructions

Welcome to the **TokenSafe** project repository.

## Project Overview
This repository implements the TokenSafe local engine for AI coding agents.
It intercepts user prompts via lifecycle hooks, compiles natural language into compact intent expressions,
slices bulky tool calls (such as full-file reads), guards secrets and unsafe commands, and provides real-time SQLite metrics tracking.

## Architecture Guidelines
- `src/tokenizer.py`: Zero-dependency, calibrated BPE token counter.
- `src/compactor.py`: Semantic intent extractor and S-expression serializer.
- `src/slicer.py`: Tool arguments optimizer (AST & line windowing).
- `src/metrics.py`: SQLite metrics persistence engine (`data/metrics.db`).
- `src/dashboard.py`: Visual terminal & Markdown analytics visualizer.
- `hooks/`: Lifecycle hook implementations (`PreInvocation`, `PreToolUse`, `PostToolUse`).
- `bin/tokensafe`: Standalone executable CLI for developers.

## Testing & Quality
Run unit tests with:
```bash
python3 -m unittest discover tests
```
Before submitting changes, keep new dependencies minimal: the AST, proxy, MCP, and telemetry engines run strictly on the Python standard library, with `cryptography>=42.0` (see `requirements.txt` / `pyproject.toml`) as the single required runtime dependency for Ed25519 offline license verification and fail-closed RS256 Clerk session verification.

<!-- tokensafe -->
## TOKENSAFE GRAPH-FIRST OPTIMIZATION PROTOCOL
Always use `tokensafe` MCP tools over default raw file reads or high-output terminal executions to prevent context window exhaustion.

1. **Zero Raw Grep/Find (Blocked & Replaced by `scoped_search`)**: Strictly NEVER execute terminal `grep`, `egrep`, `ripgrep`, or `find` on source code files or directories (`src/`, `views/`, `components/`, `.ts`, `.tsx`, `.py`, `.js`). These commands are hard-blocked by TokenSafe Guard. To search for text, symbols, or functions across unknown files, ALWAYS call `tokensafe:scoped_search(query, path='.', scope='code')`. The `path` parameter accepts the project root or subfolder (`'.'`), and `scope` accepts categories (`'code'`, `'config'`, `'all'`), extensions (`'go'`, `'ts'`), or subfolders (`'internal/promo'`).
2. **Mandatory Symbol & Component Inspection**: Whenever locating or reading a function, class, component, or method, ALWAYS call `tokensafe:smart_read_file` with `path` (relative e.g. `'src/service.ts'` or absolute) and `symbol`. By default (`dep_mode='signature'`), it preserves the target body and compact 1-line 1-hop dependency signatures (-95%+ tokens); pass `dep_mode='full'` only when dependency bodies are required, or `force_full=true` if a cache pointer references content missing from your context. When you need 3+ related symbols across several files, call `tokensafe:smart_bundle(targets=[{path, symbol}])` ONCE instead of repeated `smart_read_file` calls — it merges the skeletons and deduplicates shared imports/types in a single roundtrip. For OpenAPI/Prisma/GraphQL/SQL schema files `smart_read_file` auto-slices: pass the spec path/model/type/table as `symbol` (empty `symbol` returns a compact overview). Lockfiles (`package-lock.json`, `Cargo.lock`) and tabular files (`.csv`, `.tsv`, `.sql`, `.har`) auto-reduce to compact schemas with reversible hash anchors.
3. **Reversible Hash Stashing (`expand_ref`)**: Whenever TokenSafe folds a long command output, test log, file, or MCP payload, it tags it with an 8-character hash `[TokenSafe folded: #a1b2c3d4 ...]`. If full raw content is ever needed, ALWAYS call `tokensafe:expand_ref(hash='a1b2c3d4')` or run `tokensafe expand-ref a1b2c3d4` to retrieve the exact original bytes without re-running.
4. **Web & Documentation Reading (`read_web_page`)**: To read web pages, documentation, or API references, ALWAYS call `tokensafe:read_web_page(url)` or run `tokensafe web <url>` instead of raw HTML fetches — it strips scripts, styles, navbars, and cookie banners (-80-95% tokens).
5. **Repeat Read Deduplication**: Never repeatedly re-read unchanged files or symbols within the same session. Reuse the earlier skeleton or pointer hit (`// [TokenSafe: Pointer Hit ...]`); pass `force_full=true` only if context was compacted or you are a fresh subagent.
6. **Zero-Fluff Egress & Surgical Diffs**: Omit conversational filler, courtesy preambles, and sign-offs. Provide concise bullet prose. Never reprint full unmodified files or lengthy boilerplate; link to file line ranges (`file.ts#L10-L25`) and output surgical diffs.
7. **Architecture Discovery**: Always call `tokensafe:get_architecture_graph(project_path='.')` before exploratory multi-file reading (`project_path` defaults to active workspace). Returns project God Nodes and key dependency hubs in < 250 tokens without file crawling.
8. **API Surface Map**: Use `tokensafe:get_api_surface(project_path='.')` to get all exported classes, types, and interfaces in a single markdown map without crawling files.
9. **No Unconstrained Shell Grep/Find & No Verbose ls -la**: Strictly avoid running raw recursive grep/find across projects. Never run verbose `ls -la` or `ls -l`; use `tokensafe:list_dir(path='.', depth=2)` to inspect directory trees in clean markdown without permissions, timestamps, or lockfiles (-90% tokens). Supports relative subfolders (`path='src'`).
10. **Command Sanitization & Compact Tests**: Use `sanitize_command` on high-output terminal commands (`git diff/log/show/blame`, `rg`, `find`, `grep`, `docker`/`kubectl logs`, `npm`/`pip install`, `cat` of lockfiles, `curl`) — compound commands (`a && b ; c`) are rewritten per stage. Run noisy commands and test suites via `tokensafe wrap '<command>'` or `tokensafe tests [cmd]` (compact passing summary `✓ PASS: ...`, full traceback only on failure) instead of raw verbose test crawls; never leave `vitest`/`jest` in watch mode.
11. **Styles & Design Token Preservation**: When inspecting CSS, SCSS, LESS, or Tailwind, `smart_read_file` automatically preserves all design tokens (`:root`, `@theme`, CSS variables) in full while folding component selectors to protect project design integrity.
12. **Context Budget & Smart Compaction**: Before large multi-step tasks, call `tokensafe:context_budget(model=...)` to inspect window usage; when it reports `warning` or `critical`, call `tokensafe:compact_conversation(transcript_path=...)` before continuing.
13. **Vision Image Optimization (`optimize_image`)**: When handling screenshots or visual diagrams, call `tokensafe:optimize_image` or run `tokensafe optimize-image <file>` to downscale images exceeding 1024px (-50-75% vision tokens) while preserving original disk files.
14. **Superseded Output Pruning (`prune_superseded`)**: In long multi-turn sessions, prune obsolete intermediate file reads via `tokensafe:prune_superseded(transcript_path)` to free up context without losing history.
15. **Anti-Stuck Revert Memory (`get_mistakes`)**: Consult `tokensafe:get_mistakes` to retrieve persistent records of prior dead-ends, loop guard trips, and git reverts to avoid attempting previously failed solutions.
16. **Secret Leak Prevention Directive (Zero Hardcoded Fallbacks)**:
Never hardcode real private keys, API secrets, access tokens, or webhook secrets as default fallback arguments in `os.environ.get()`, `os.getenv()`, or `process.env`. If an environment variable is absent, raise an explicit configuration error or use an empty string / test mock. Whenever a secret fallback attempt is blocked, state:
`🛡️ **TokenSafe Security Guard**: Prevented secret / private key leak into git (<target_file>: <details>)`
17. **Proof-of-Savings Turn Footer (always in English)**:
At the end of your response for any turn where tools were executed, files were inspected, commands were sanitized, or prompts were compacted, append a compact 1-line footer in English:
`⚡ **TokenSafe Turn Savings**: +<tokens> tokens saved on this turn (<breakdown>) | Cumulative Total: <total> tokens (≈$<usd> approx)`
<!-- /tokensafe -->

