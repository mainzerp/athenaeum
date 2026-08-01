"""MCP access token management: list / create / revoke.

Plan §3.1 + decision 7: per-user bearer tokens, stored SHA-256-hashed; the
plaintext is shown exactly once at creation; ``label`` enables per-agent
attribution.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from athenaeum import db, security
from athenaeum.webui import deps

router = APIRouter(prefix="/tokens", dependencies=[Depends(deps.csrf_protect)])


def _render_tokens(
    request: Request,
    conn: sqlite3.Connection,
    user: sqlite3.Row,
    new_token: str | None = None,
    new_label: str | None = None,
):
    return deps.templates.TemplateResponse(
        request,
        "tokens.html",
        {
            "user": user,
            "tokens": db.list_tokens(conn, user["id"]),
            "new_token": new_token,
            "new_label": new_label,
        },
    )


@router.get("")
def tokens_page(request: Request, conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)]):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    return _render_tokens(request, conn, user)


@router.post("")
def create_token_submit(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    label: str = Form(""),
):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    label = label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="Label is required")
    plaintext, token_hash = security.generate_token()
    db.create_token(conn, user["id"], label, token_hash)
    # Plaintext is rendered exactly once, in this response only.
    return _render_tokens(request, conn, user, new_token=plaintext, new_label=label)


@router.post("/{token_id}/revoke")
def revoke_token_submit(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    token_id: str,
):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    if not db.revoke_token(conn, user["id"], token_id):
        raise HTTPException(status_code=404, detail="Token not found")
    return deps.redirect("/tokens")
