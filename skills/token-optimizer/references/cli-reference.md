# CLI Reference (`tokensafe`)

Run standalone in the terminal or invoke via agent:

```bash
tokensafe --version           # Print version and exit (no metrics side effects)

# Telemetry & Economics
tokensafe stats               # View terminal report with tokens saved & USD ROI
tokensafe html                # Launch interactive Apple-style HTML dashboard
tokensafe benchmark           # Run the zero-context-loss benchmark suite (simulated SWE-bench harness; see BENCHMARK_REPORT.md)
tokensafe tests [pytest]      # Run the unit test suite (compact failure report; --traceback for full stacks)

# Code Intelligence & Architecture
tokensafe graph [path]        # Inspect God Nodes and centrality ranking
tokensafe map [path]          # Generate tokensafe-out/API_SURFACE.md
tokensafe skeleton <file>     # View AST skeleton for a specific file
tokensafe search "<query>"    # Sub-millisecond SQLite FTS5 BM25-ranked code search
tokensafe index [path]        # Build/update incremental FTS5 trigram full-text index

# Multi-Agent Ecosystem Management
tokensafe install             # Interactive agent installer (Cursor, Windsurf, Claude, etc.)
tokensafe install --all       # Automatically configure all 9 detected agent environments
tokensafe update              # One-click self-update & multi-agent re-synchronization

# System Health & Hygiene
tokensafe guard "<command>"   # Pre-flight check command safety (rm, secrets, grep)
tokensafe guard audit [path]  # Recursively audit project files for security violations
tokensafe doctor [--cleanup]  # Audit MCP tool overhead, token tax & clean competitor conflicts
tokensafe expand-ref <hash>   # Restore original raw content of any folded block by its 8-char hash
tokensafe wrap "<cmd>"        # Run noisy command with compact output folding (e.g. tests, tsc, builds)
tokensafe web <url>           # Fetch web page/docs and print clean compact Markdown
tokensafe optimize-image <img># Downscale oversized screenshot to max 1024px (-50-75% vision tokens)
tokensafe prune-superseded <t># Prune outdated intermediate reads from conversation transcript
tokensafe mistakes [list]     # Inspect persistent dead-ends, git reverts & anti-stuck prompt guard
tokensafe mcp wrap [--all]    # Wrap third-party MCP servers behind tokensafe mcp-proxy
tokensafe mcp unwrap [--all]  # Restore third-party MCP servers to direct execution
tokensafe prune               # Compact metrics database and prune old events
tokensafe vacuum              # Reclaim SQLite database disk space
tokensafe test "<prompt>"     # Test TOCP S-expression prompt compaction
```
