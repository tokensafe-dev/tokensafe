"""Explicit per-session state: workspace root + metrics session id.

TokenSafe historically resolved both from process-global state: the machine-wide
``~/.tokensafe/data/.active_workspace`` marker and the module-level MCP client
name. That is fine for a one-shot hook process, but it breaks as soon as a
single process owns several sessions — the MCP server serving two editor
windows, or the ACP agent server running one prompt per thread. A search that
is demonstrably scoped to repo A gets validated against repo B (or a leftover
temp directory) and rejected as "outside project root".

This module makes the context explicit and cheap:

  * ``SessionContext`` is immutable and carries root + session id + agent.
  * ``session_scope(...)`` binds it to the current thread/task via a
    ContextVar, so it propagates through the call stack without threading a
    parameter through every handler.
  * Consumers call ``current_root()`` / ``current_session_id(default)`` and
    fall back to the legacy global marker only when no scope is active, which
    keeps hook processes behaving exactly as before.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Optional


@dataclass(frozen=True)
class SessionContext:
    """One agent session: where it works and how its metrics are keyed."""

    session_id: str = "ephemeral"
    root: Optional[str] = None
    agent: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.root:
            object.__setattr__(self, "root", os.path.realpath(os.path.abspath(self.root)))

    @property
    def valid_root(self) -> Optional[str]:
        """Root only if it still exists on disk (temp dirs may be gone)."""
        root = self.root
        return root if root and os.path.isdir(root) else None


_CURRENT: ContextVar[Optional[SessionContext]] = ContextVar(
    "tokensafe_session_context", default=None
)


def current() -> Optional[SessionContext]:
    """The active session context, or None when running outside a scope."""
    return _CURRENT.get()


def current_root() -> Optional[str]:
    """Active workspace root, or None when no scope is bound."""
    ctx = _CURRENT.get()
    return ctx.valid_root if ctx else None


def current_session_id(default: str = "") -> str:
    """Active session id, or ``default`` when no scope is bound."""
    ctx = _CURRENT.get()
    if ctx and ctx.session_id:
        return ctx.session_id
    return default


def activate(ctx: SessionContext) -> Token:
    """Bind ``ctx`` to the current context; returns a token for ``deactivate``."""
    return _CURRENT.set(ctx)


def deactivate(token: Token) -> None:
    """Restore the previous context. Always pair with ``activate`` in ``finally``."""
    _CURRENT.reset(token)


@contextmanager
def session_scope(
    root: Optional[str] = None,
    session_id: str = "ephemeral",
    agent: str = "",
    data: Optional[Dict[str, Any]] = None,
) -> Iterator[SessionContext]:
    """Bind a session for the duration of the block.

    Threads do not inherit ContextVars, so a worker must open its own scope
    (the ACP server does this per prompt).
    """
    ctx = SessionContext(
        session_id=session_id or "ephemeral",
        root=root,
        agent=agent,
        data=dict(data or {}),
    )
    token = _CURRENT.set(ctx)
    try:
        yield ctx
    finally:
        _CURRENT.reset(token)


def resolve_path(path: str, root: Optional[str] = None) -> str:
    """Resolve ``path`` against an explicit root, then the active session root.

    Absolute paths pass through unchanged. Unlike ``os.path.abspath`` this never
    silently resolves against the process cwd, which is what the MCP server's
    global-marker fallback used to do.
    """
    if not path or os.path.isabs(path):
        return path
    base = root or current_root()
    if base:
        candidate = os.path.join(base, path)
        if os.path.exists(candidate):
            return os.path.abspath(candidate)
    return os.path.abspath(path)
