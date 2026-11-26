"""Agent Client Protocol adapter: JSON-RPC 2.0 over stdio for editors.

The server module is imported lazily: importing it here would execute the
module twice under ``python -m src.acp.server`` and confuse the stdio loop.
"""

__all__ = ["AcpServer", "run_acp_server"]


def __getattr__(name):
    if name in __all__:
        from src.acp import server as _server

        return getattr(_server, name)
    raise AttributeError(name)
