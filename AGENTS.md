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
Before submitting changes, keep new dependencies minimal: the AST, proxy, MCP, and telemetry engines run strictly on the Python standard library, with `cryptography>=42.0` (see `requirements.txt` / `pyproject.toml`) as the single required runtime dependency for Ed25519 offline license verification and fail-closed RS256 Clerk session verification.

<!-- tokensafe -->
## TOKENSAFE GRAPH-FIRST OPTIMIZATION PROTOCOL
The full 12-point protocol is installed globally in the agent-level instructions (~/.zcode/AGENTS.md, ~/.claude/CLAUDE.md, ~/.codex/AGENTS.md, ~/.kimi-code/AGENTS.md) and applies to this project. Core rules: never raw grep/find or `ls -la` — use `tokensafe:get_architecture_graph(project_path)` first for God Nodes, `tokensafe:scoped_search`, `tokensafe:smart_read_file(path, symbol)`, `tokensafe:smart_bundle` and `tokensafe:list_dir`; sanitize high-output commands via `tokensafe:sanitize_command`; check `tokensafe:context_budget` (Grade S-F) before large tasks and compact via `tokensafe:compact_conversation` (orientation digest active); never hardcode secret fallbacks; append the `⚡ TokenSafe Turn Savings` footer on tool-using turns.
<!-- /tokensafe -->

