<!--
Rule: Token-Optimized Communication Protocol (TOCP)
Trigger: always_on
-->

# Token-Optimized Communication Protocol (TOCP)

When interacting within this environment, you MUST follow strict token economy guidelines:

### 1. Ingress Intent Decoding
When a user input or system message is prefixed with `[TOCP Compressed Intent]`, it follows the lean S-Expression syntax:
`(ACTION [target_file] [scope_symbol] [[traits...]] ["residual_intent"])`

- `ACTION`: `REFACTOR`, `DEBUG`, `CREATE`, `OPTIMIZE`, `EXPLAIN`, `AUDIT`, `TEST`, `RUN`.
- `target_file`: Primary file path to inspect or edit.
- `scope_symbol`: Targeted function, method, class, or identifier.
- `traits`: Constraints such as `[async redis cache zero_alloc typesafe unit_test]`.
- `residual_intent`: Minimal context delta.

Execute the specified action directly based on these extracted parameters.

### 2. Zero-Fluff Output Directives (Egress Optimization)
1. **No Conversational Filler**: Never include courtesy greetings or sign-offs (e.g., "Certainly!", "Sure, I can help", "Let me know if you need anything else").
2. **Surgical Code Edits & Zero In-Place Stream Editing**: Prefer `edit_file` with concise `Target` and `Replacement` over `write_file` on large existing files. NEVER run terminal stream editors (`sed -i`, `perl -i`, `awk -i`) to edit or delete source code — blind numeric ranges cause line-drift and silent code loss (hard-blocked by TokenSafe Guard). NEVER suppress errors (`2>/dev/null`) on file modifications.
3. **Diff-Centric Responses**: Never reprint entire unmodified files or long boilerplate blocks. Reference file lines instead (e.g., `wallet.ts#L42-L50`).
4. **Dense Formatting**: Use compact bullet points and direct technical prose.
5. **Repeat Read Deduplication**: Never re-read unchanged files or symbols in the same session. Reuse the skeleton or pointer reference (`// [TokenSafe: Pointer Hit ...]`) returned earlier; only pass `force_full=true` if your context was compacted or you are a fresh subagent.
6. **Compact Test Output Execution**: Run unit tests with concise flags or via `tokensafe tests [cmd]` so passing test runs return a 1-line summary (`PASS: ...`) rather than flooding `Messages` history with hundreds of verbose log lines. Full output is preserved only on failure.

### 3. Architecture & Code Navigation Directives (Graph-Guided)
1. **Zero Raw Terminal Grep/Find (Blocked & Replaced by `scoped_search`)**: Strictly NEVER execute terminal `grep`, `egrep`, `ripgrep`, or `find` on source code files or directories (`src/`, `views/`, `components/`, `.ts`, `.tsx`, `.py`, `.js`). These commands are hard-blocked by TokenSafe Guard. To search for text, symbols, or functions across unknown files, ALWAYS call `tokensafe:scoped_search(query, path=".", scope="code")`. The `path` parameter accepts the project root or subfolder (`"."`, `"./src"`), and `scope` accepts file categories (`"code"`, `"config"`, `"all"`), file extensions (`"go"`, `"ts"`), or subfolders (`"internal/promo"`).
2. **Mandatory Symbol & Component Inspection**: Whenever locating or reading a function, class, component, or method, ALWAYS call `tokensafe:smart_read_file` with `path` (relative e.g. `"src/service.ts"` or absolute) and `symbol`. By default (`dep_mode="signature"`), it preserves the full executable target and compact 1-line 1-hop dependency signatures while folding inert code (-95%+ tokens). Pass `dep_mode="full"` only when dependency bodies are needed, or `force_full=true` if a cache pointer references content missing from your context. When you need 3+ related symbols across several files, call `tokensafe:smart_bundle(targets=[{path, symbol}])` ONCE instead of repeated `smart_read_file` calls — it merges the skeletons and deduplicates shared imports/types in a single roundtrip. For OpenAPI/Prisma/GraphQL/SQL schema files `smart_read_file` auto-slices: pass the spec path/model/type/table as `symbol` (empty `symbol` returns a compact overview). Lockfiles (`package-lock.json`, `Cargo.lock`) and tabular data (`.csv`, `.tsv`, `.sql`, `.har`) auto-reduce to compact schemas with reversible hash anchors.
3. **Reversible Hash Stashing (`expand_ref`)**: Whenever TokenSafe folds a long command output, test log, file, or MCP payload, it tags it with an 8-character hash `[TokenSafe folded: #a1b2c3d4 ...]`. If full raw content is ever needed, ALWAYS call `tokensafe:expand_ref(hash="a1b2c3d4")` or run `tokensafe expand-ref a1b2c3d4` to retrieve the exact original bytes without re-running.
4. **Web & Documentation Reading (`read_web_page`)**: To read web pages, documentation, or API references, ALWAYS call `tokensafe:read_web_page(url)` or run `tokensafe web <url>` instead of raw HTML fetches — it strips scripts, styles, navbars, and cookie banners (-80-95% tokens).
5. **Mandatory Architecture Graph & God Nodes First**: For ANY codebase inquiry, feature design, refactoring, or multi-component investigation, your FIRST tool call MUST be `tokensafe:get_architecture_graph(project_path)` to locate God Nodes and dependency topology in < 250 tokens before reading individual files or searching (pass the target project's absolute path when working across multiple workspaces or outside CWD).
6. **API Surface Map**: Use `tokensafe:get_api_surface(project_path)` to get all exported classes, types, and interfaces in a single markdown map without crawling files (pass the target project's absolute path when working across multiple workspaces or outside CWD).
7. **Bounded Reads**: Never call `read_file` without `StartLine`/`EndLine` on files > 100 lines unless inspecting a skeleton.
8. **Directory Navigation**: Never run verbose `ls -la` or `ls -l`; use `tokensafe:list_dir(path=".", depth=2)` to inspect directory trees without permissions, timestamps, or lockfiles (-90% tokens). Supports relative subfolders (`path="src"`).
9. **Style & Design Tokens**: When inspecting CSS/SCSS/LESS/Tailwind, `tokensafe:smart_read_file` automatically preserves all design tokens (`:root`, `@theme`, CSS variables) in full while folding component selectors.
10. **Command Output Wrapping & Test Execution**: Run noisy commands, builds, and test suites (`bun test`, `pytest`, `python -m unittest`, `vitest`, `jest`, `cargo test`, `go test`) via `tokensafe wrap '<command>'` or `tokensafe tests [cmd]`. Sanitization automatically strips `git push` progress animation frames (`--no-progress`), wraps `git commit` when pre-commit/husky hooks exist, and forces test runners into run-once mode. Passing tests return a 1-line summary (`✓ PASS: ...`), and failures return focused failure blocks with full tracebacks preserved in stash.
11. **Context Budget & Smart Compaction**: Before large multi-step tasks, call `tokensafe:context_budget(model=...)` to check window usage and Context Quality Score (Grade S-F). When it reports `warning`, `critical`, or Grade C/F, call `tokensafe:compact_conversation(transcript_path=...)` to fold past turns into a Consolidated State Ledger with a 3-line Post-Compaction Orientation Digest (Active Goal, Last State, Next Focus) before continuing.
12. **Vision Image Optimization (`optimize_image`)**: When handling screenshots or visual diagrams, call `tokensafe:optimize_image` or run `tokensafe optimize-image <file>` to downscale images exceeding 1024px (-50-75% vision tokens) while preserving original disk files.
13. **Superseded Output Pruning (`prune_superseded`)**: In long multi-turn sessions, prune obsolete intermediate file reads via `tokensafe:prune_superseded(transcript_path)` to free up context without losing history.
14. **Anti-Stuck Revert Memory (`get_mistakes`) & Hybrid Escalation**: Consult `tokensafe:get_mistakes` to retrieve persistent records of prior dead-ends, loop guard trips, and git reverts to avoid attempting previously failed solutions. The 2-tier hybrid escalation policy issues a soft advisory warning for 1-2 failures and triggers `force_ask` if an identical action or command fails >2 times within 30 minutes, automatically broadcasting scrubbed failure summaries to the team.
15. **External Types & Lockfile Point Queries**: Use `tokensafe:slice_external_type(package_name, type_name)` to extract 3rd-party TypeScript interfaces from `node_modules` without crawling directories (-95% tokens, L1/L2/L3 cached). Use `tokensafe:query_dependency(lockfile_path, package_name)` for instant lockfile point lookups in <50 tokens; raw `read_file` on lockfiles without bounds is redirected to an index summary.

### 4. Turn Token Savings Footer (Proof-of-Savings — always in English)
At the end of your response for any turn where tools were executed, files were inspected, commands were sanitized, or prompts were compacted, append a compact 1-line footer:
`⚡ **TokenSafe Turn Savings**: +<tokens> tokens saved on this turn (<breakdown>) | Cumulative Total: <total> tokens (≈$<usd> approx)`
Example:
`⚡ **TokenSafe Turn Savings**: +3,520 tokens (~88%) | Cumulative Total: 3.04M tokens (≈$4.56 approx)`

### 5. Secret Leak Prevention Directive (Zero Hardcoded Fallbacks)
1. **Never Hardcode Credential Fallbacks**: Never provide real private keys, API secrets, access tokens, or webhook secrets as default fallback arguments in `os.environ.get()`, `os.getenv()`, or `process.env`. If an environment variable is absent, raise an explicit configuration error or use an empty string / test mock. Real credentials must exist strictly in environment variables.
2. **Leak Prevention Notification (English)**:
Whenever a tool execution, code edit, or command attempts to write or commit a hardcoded secret fallback, block it and include an explicit notification alert in your response:
`🛡️ **TokenSafe Security Guard**: Prevented secret / private key leak into git (<target_file>: <details>)`
