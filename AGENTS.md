# TokenSafe Project Instructions

Welcome to the **TokenSafe** project repository.

## Project Overview
This repository implements the TokenSafe local engine for AI coding agents.
It intercepts user prompts via lifecycle hooks, compiles natural language into compact intent expressions,
slices bulky tool calls (such as full-file reads), guards secrets and unsafe commands, and provides real-time SQLite metrics tracking.

## STRICT REPOSITORY PRIVACY BOUNDARY (CONFIDENTIALITY DIRECTIVE)
- This repository (`tokensafe`) is **STRICTLY PUBLIC OPEN-SOURCE**.
- **NEVER** save, commit, or create internal architectural plans, marketing strategies, repositioning docs, roadmaps, design documents (`docs/plans/`), or internal notes in this repository!
- All internal documents, plans, strategy files, roadmaps, and business design docs MUST live exclusively in the private repository (`tokensafe-api`, specifically `tokensafe-api/docs/plans/` or `tokensafe-site/docs/`).
- If you find any internal planning or strategy document here, delete it immediately and move it to the private repository.

## Architecture Guidelines
- `src/tokenizer.py`: Zero-dependency, calibrated BPE token counter.
- `src/compactor.py`: Semantic intent extractor and S-expression serializer.
- `src/slicer.py`: Tool arguments optimizer (AST & line windowing).
- `src/type_slicer.py`: 3rd-party TypeScript definition slicer (`@types`, `.d.ts`) with L1/L2/L3 caching.
- `src/schema_slicer.py`: OpenAPI & multi-file Prisma schema contract optimizer with composite fingerprints.
- `src/web_reader.py`: Sanitized documentation fetcher with 3-tier L1/L2/L3 caching.
- `src/format_parsers/lockfiles.py`: Lockfile point queries (`query_dependency`) and index summarizer.
- `src/mistakes.py`: Anti-stuck dead-end ledger with 2-tier hybrid escalation and team broadcast.
- `src/metrics.py`: SQLite metrics persistence engine (`data/metrics.db`).
- `src/dashboard.py`: Visual terminal & Markdown analytics visualizer.
- `hooks/`: Lifecycle hook implementations (`PreInvocation`, `PreToolUse`, `PostToolUse`).
- `bin/tokensafe`: Standalone executable CLI for developers.

## Testing & Quality
Run unit tests with:
```bash
python3 -m unittest discover tests
```
Before submitting changes, keep new dependencies minimal: the AST, proxy, MCP, and telemetry engines run strictly on the Python standard library, with `cryptography>=42.0` (see `requirements.txt` / `pyproject.toml`) as the single *required* runtime dependency for Ed25519 offline license verification and fail-closed RS256 Clerk session verification. `tree-sitter` grammars and `Pillow` are optional accelerators and must keep their stdlib fallbacks (`src/treesitter_backend.py`, `src/image_downscale.py`) working when absent. Installed interpreters come from `bin/tokensafe.js`: system CPython, then `~/.tokensafe/venv`, then the pinned managed runtime in `bin/python-runtime.json` (SHA-256 verified, downloaded only when no CPython 3.10+ exists).

<!-- tokensafe -->
## TOKENSAFE GRAPH-FIRST OPTIMIZATION PROTOCOL
The full protocol is installed globally in the agent-level instructions (~/.zcode/AGENTS.md, ~/.claude/CLAUDE.md, ~/.codex/AGENTS.md, ~/.kimi-code/AGENTS.md) and applies to this project. Core tools: `tokensafe:repo_map(view='graph'|'api'|'tree')` for architecture & directory trees, `tokensafe:scoped_search` (single or `queries` batch, never raw grep/find), `tokensafe:smart_read_file` (symbol, `targets` bundle, or `.d.ts` `package_name`), `tokensafe:apply_edits` for multi-file edits, `tokensafe:oumv` (MANDATORY first on OUMV-supported React/Vue/Go/FastAPI stacks — live or static offline; check `status` in mixed monorepos), `tokensafe:session_context(action='budget'|'compact'|'prune'|'mistakes')` for context & anti-stuck hygiene, and `tokensafe:run_helper(action='sanitize'|'guard'|'diff'|'deps'|'web'|'expand'|'image'|'compress')`; never hardcode secret fallbacks; append the `⚡ TokenSafe Turn Savings` footer on tool-using turns.
<!-- /tokensafe -->

