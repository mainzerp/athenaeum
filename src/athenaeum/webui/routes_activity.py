"""Activity view: in-flight MCP calls + the persisted tool-call journal.

In-flight rows come from the app-level ActivityRegistry (None-tolerant: the
self-contained WebUI test app has no registry on app.state); the journal is
read from the activity table, owner-scoped to the logged-in user.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from athenaeum import db
from athenaeum.config import Settings
from athenaeum.librarian.tracing import TraceStore
from athenaeum.webui import deps

router = APIRouter()

# Only the agent-backed tools produce traces (D3); journal rows for other
# tools get no replay link.
TRACED_TOOLS = frozenset(
    {
        "request_knowledge",
        "store_knowledge",
        "update_knowledge",
        "library_maintain",
        "library_curate",
    }
)


@router.get("/activity")
def activity_page(request: Request, conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)]):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    return deps.templates.TemplateResponse(request, "activity.html", {"user": user})


@router.get("/activity/rows")
def activity_rows(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
):
    """htmx polling target: in-flight calls + newest journal rows."""
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    registry = getattr(request.app.state, "activity_registry", None)
    if registry is None:
        in_flight = []
    else:
        in_flight = [e for e in registry.snapshot() if e.get("user_id") == user["id"]]
    journal = db.list_activity(conn, user["id"], limit=50)
    # Replay links only for rows whose trace file exists: no-op agent runs
    # journal a row but write no trace (locked decision 5).
    store = TraceStore(deps.library_root_for(settings, user["id"]))
    existing_traces = {
        row["trace_id"]
        for row in journal
        if row["tool"] in TRACED_TOOLS and store.exists(row["trace_id"])
    }
    return deps.templates.TemplateResponse(
        request,
        "activity_rows.html",
        {
            "user": user,
            "in_flight": in_flight,
            "journal": journal,
            "traced_tools": TRACED_TOOLS,
            "existing_traces": existing_traces,
        },
    )
