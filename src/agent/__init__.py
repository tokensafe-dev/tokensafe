"""In-process agent kernel.

TokenSafe started as a hook-based add-on: another harness owned the turn and
TokenSafe reacted to it. This package owns the turn instead — provider router,
streaming, tool dispatch and context governance in one loop — so the same
kernel can be driven by the ACP server, the CLI, or a test harness.

Import cost matters here: nothing in this package imports ``src.mcp_server`` at
module level, because the MCP server is a large dependency graph and the
provider layer must stay usable (and testable) without it.
"""
