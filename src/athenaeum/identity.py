"""Request identity context: (user_id, token_label) of the in-flight MCP call.

Shared home for identity propagation (A7): the transport
(``mcp_server.BearerAuthMiddleware``) sets the context var, the activity
middleware reads it for journaling — previously ``activity.py`` imported
``mcp_server`` for this, an import cycle hidden by a function-local import.
"""

from __future__ import annotations

from contextvars import ContextVar

# (user_id, token_label) for the in-flight request, set by BearerAuthMiddleware.
_identity_var: ContextVar[tuple[str, str] | None] = ContextVar("athenaeum_identity", default=None)


def get_current_identity() -> tuple[str, str] | None:
    """Identity of the in-flight MCP request, or None when unauthenticated."""
    return _identity_var.get()
