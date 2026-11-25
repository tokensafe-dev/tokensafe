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
2. **Surgical Code Edits, Batching & Zero In-Place Stream Editing**: Prefer `tokensafe:apply_edits(edits=[...], verify="npm test")` for >=2 hunks/files or edit + verify loops. For single tweaks, use `edit_file` with exact `Target` and `Replacement`. NEVER run terminal stream editors (`sed -i`, `perl -i`, `awk -i`) on source code (hard-blocked by TokenSafe Guard), and never suppress errors (`2>/dev/null`).
3. **Diff-Centric Responses**: Never reprint entire unmodified files or long boilerplate blocks. Reference file lines instead (e.g., `wallet.ts#L42-L50`).
4. **Dense Formatting**: Use compact bullet points and direct technical prose.
5. **Repeat Read Deduplication**: Never re-read unchanged files or symbols in the same session. Reuse the skeleton or pointer reference (`// [TokenSafe: Pointer Hit ...]`) returned earlier; pass `force_full=true` only after compaction or as a fresh subagent.
6. **Compact Test Output Execution**: Run unit tests via `tokensafe tests [cmd]` or `tokensafe wrap '<cmd>'` so passing suites return a 1-line summary (`PASS: ...`), preserving full output only on failure.
7. **Multimodal Image Isolation (Prompt Cache Protection)**: Never load raw `.png`/`.jpg`/`.webp` screenshots into a long (>150k-token) coding session (triggers KV-cache invalidation). Delegate visual inspection to a subagent returning text-only findings.
8. **Startup Snapshot Utilization**: Check `[TokenSafe Workspace Snapshot]` on turn 1 before running exploratory `ls` or `git status`.
9. **Zero-Spill Response Budget & Reversible Stash**: TokenSafe MCP responses are capped per response (not per block) at ~8.8 KB of UTF-8 text, with headroom left for JSON escaping, to prevent Antigravity `output.txt` disk spills. Folded content carries a `[TokenSafe folded: #hash ...]` anchor and is archived in the stash; call `tokensafe:run_helper(action="expand", hash="...")` to restore it. If the stash write fails, the response is still truncated and the marker states that the raw copy is unavailable.

### 3. Architecture & Code Navigation Directives (7 Consolidated MCP Tools)
1. **Search (`scoped_search`)**: Terminal `grep`/`egrep`/`rg`/`find` on source files are BLOCKED. Call `tokensafe:scoped_search(query, path=".", scope="code")` (supports `mode="auto|text|regex|glob|fuzzy|git"`) or pass `queries=[...]` for up to 20 concurrent queries in 1 call (<70ms local).
2. **Symbol, Bundle & External Type Reads (`smart_read_file`)**: Call `tokensafe:smart_read_file(path, symbol)` (`dep_mode="signature"` default, `"full"` when needed, `force_full=true` on pointer miss; OpenAPI/Prisma/GraphQL/SQL auto-slice via `symbol`). Pass `targets=[{path, symbol}]` (2–8 targets) to bundle files with deduplicated imports, or `package_name` + `symbol` for 3rd-party `.d.ts` interfaces.
3. **Architecture, API Surface & Directory Trees (`repo_map`)**: First inspect God Nodes via `tokensafe:repo_map(view="graph", project_path=...)` before crawling files. Use `view="api"` for exported types/interfaces and `view="tree"` (never `ls -la`).
4. **Multi-File Edits (`apply_edits`)**: Batch >=2 hunks/files and optional `verify` commands in a single turn.
5. **Session & Context Hygiene (`session_context`)**: Call `tokensafe:session_context(action="budget", model=...)` before large tasks; when `warning`/`critical` or Grade C/F, call `action="compact"` with `transcript_path`. Use `action="prune"` for superseded reads, `action="mistakes"` for anti-stuck dead-end memory, and `action="metrics"` for savings analytics.
6. **Utilities (`run_helper`)**: Call `tokensafe:run_helper(action=...)` for:
   - `"expand"` (`hash`): restore stashed `#hash` content
   - `"web"` (`url`): fetch clean Markdown docs (-80-95% tokens, L1/L2/L3 cached)
   - `"deps"` (`package_name`, `lockfile_path`): point lockfile lookup in <50 tokens
   - `"sanitize"` (`command`, `cwd`): rewrite noisy shell/test commands
   - `"guard"` (`command`/`code`, `file_path`): pre-flight security/hygiene audit
   - `"diff"` (`diff`): slice unified git diffs
   - `"image"` (`image_path`, `max_edge=1024`): downscale screenshots (-50-75% vision tokens)
   - `"compress"` (`prompt`): TOCP S-expression compression
7. **Universal OUMV Protocol (`oumv`)**: For live Frontend (ports 5173/3000) and Backend (ports 8080/8000) runtimes, call `tokensafe:oumv(action="status"|"observe"|"understand"|"verify"|"mutate"|"graph", ...)` instead of headless browsers or blind shell curls.
8. **Bounded Reads & Styles**: Never call raw `read_file` without `StartLine`/`EndLine` on files > 100 lines. `smart_read_file` preserves CSS/SCSS/Tailwind design tokens (`:root`, `@theme`) in full.

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
