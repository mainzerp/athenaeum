"""Trace replay views over the per-user ``.traces/`` store.

Owner-scoped by construction: the TraceStore is rooted at the logged-in
user's own library root, so another user's trace ids simply do not exist and
surface as 404 (same pattern as document access in routes_library.py).
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from athenaeum.config import Settings
from athenaeum.librarian.tracing import TraceStore
from athenaeum.webui import deps, markdown_render

router = APIRouter()


def _store(
    request: Request,
    conn: sqlite3.Connection,
    settings: Settings,
) -> tuple[sqlite3.Row, TraceStore] | None:
    user = deps.current_user(request, conn)
    if user is None:
        return None
    return user, TraceStore(deps.library_root_for(settings, user["id"]))


def _read_trace(store: TraceStore, trace_id: str) -> dict:
    try:
        return store.read(trace_id)
    except (FileNotFoundError, ValueError) as exc:
        # Missing trace or rejected id: indistinguishable, both 404.
        raise HTTPException(status_code=404, detail="Trace not found") from exc


@router.get("/library/traces/{trace_id}")
def trace_page(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
    trace_id: str,
):
    ctx = _store(request, conn, settings)
    if ctx is None:
        return deps.login_redirect(conn)
    user, store = ctx
    trace = _read_trace(store, trace_id)
    answer_html = markdown_render.render_markdown(trace["answer"]) if trace.get("answer") else None
    return deps.templates.TemplateResponse(
        request, "trace.html", {"user": user, "trace": trace, "answer_html": answer_html}
    )


@router.get("/api/traces/{trace_id}")
def trace_data(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
    trace_id: str,
):
    """Trace JSON for the replay script (trace.html)."""
    ctx = _store(request, conn, settings)
    if ctx is None:
        return deps.login_redirect(conn)
    _, store = ctx
    return _read_trace(store, trace_id)
