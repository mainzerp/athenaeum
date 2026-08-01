"""Admin routes: user list / create / reset-password (admin only).

Decision 2 (plan §1): after first-run bootstrap, admins create further
accounts in the WebUI; there is no self-registration.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from athenaeum import db, security
from athenaeum.config import Settings
from athenaeum.library.backend import provision_library
from athenaeum.webui import deps

router = APIRouter(prefix="/admin", dependencies=[Depends(deps.csrf_protect)])


@router.get("/users")
def users_page(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    error: str | None = None,
):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    return deps.templates.TemplateResponse(
        request,
        "admin_users.html",
        {"user": user, "users": db.list_users(conn), "error": error},
    )


@router.post("/users")
def create_user_submit(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
    username: str = Form(""),
    password: str = Form(""),
    is_admin: str | None = Form(None),
):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    username = username.strip()
    error = None
    if not username:
        error = "Username is required."
    elif not password:
        error = "Password is required."
    elif len(password) < security.MIN_PASSWORD_LENGTH:
        error = f"Password must be at least {security.MIN_PASSWORD_LENGTH} characters."
    elif db.get_user_by_username(conn, username) is not None:
        error = "Username already exists."
    if error is None:
        user = db.create_user(
            conn,
            username,
            security.hash_password(password),
            is_admin=is_admin is not None,
        )
        provision_library(settings.data_root, user["id"])
        return deps.redirect("/admin/users")
    return deps.templates.TemplateResponse(
        request,
        "admin_users.html",
        {"user": user, "users": db.list_users(conn), "error": error},
        status_code=400,
    )


@router.get("/server")
def server_page(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    saved: bool = False,
):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    return deps.templates.TemplateResponse(
        request,
        "admin_server.html",
        {
            "user": user,
            "mcp_stateless_http": db.get_app_setting(conn, "mcp_stateless_http", "0") == "1",
            "saved": saved,
        },
    )


@router.post("/server")
def server_submit(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    mcp_stateless_http: str | None = Form(None),
):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    db.set_app_setting(conn, "mcp_stateless_http", "1" if mcp_stateless_http is not None else "0")
    return deps.redirect("/admin/server?saved=1")


@router.post("/users/{user_id}/reset-password")
def reset_password_submit(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    user_id: str,
    new_password: str = Form(""),
):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    if db.get_user_by_id(conn, user_id) is None:
        raise HTTPException(status_code=404, detail="User not found")
    if new_password:
        if len(new_password) < security.MIN_PASSWORD_LENGTH:
            raise HTTPException(
                status_code=400,
                detail=f"Password must be at least {security.MIN_PASSWORD_LENGTH} characters",
            )
        db.set_password(conn, user_id, security.hash_password(new_password))
    return deps.redirect("/admin/users")
