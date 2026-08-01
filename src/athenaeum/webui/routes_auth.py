"""Auth routes: first-run setup, login, logout.

Decision 2 (plan §1): first-run bootstrap + admin-created accounts. While the
``users`` table is empty the WebUI shows a one-time setup page that creates
the owner (admin); afterwards admins create further users. No self-registration.

``SessionMiddleware`` itself is wired in ``app.py`` (integration step); the
session keys used across the WebUI are defined here.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from athenaeum import db, security
from athenaeum.config import Settings
from athenaeum.library.backend import provision_library
from athenaeum.webui import deps

router = APIRouter(dependencies=[Depends(deps.csrf_protect)])

# Session keys (signed-cookie session provided by SessionMiddleware).
SESSION_USER_ID = "user_id"


def _render_setup(request: Request, error: str | None = None) -> HTMLResponse:
    return deps.templates.TemplateResponse(request, "setup.html", {"error": error, "user": None})


def _render_login(request: Request, error: str | None = None) -> HTMLResponse:
    return deps.templates.TemplateResponse(request, "login.html", {"error": error, "user": None})


def bootstrap_admin_if_configured(settings: Settings) -> bool:
    """Pre-seed the owner account from ATHENAEUM_BOOTSTRAP_ADMIN_* env vars.

    Only consumed when the users table is empty; ignored once any user
    exists (plan §3.6). Startup hook called by ``app.py`` (integration).
    Returns True when an account was created.
    """
    username = settings.bootstrap_admin_username
    password = settings.bootstrap_admin_password
    if not username or not password:
        return False
    db_path = deps.db_path_for(settings)
    deps.ensure_db(db_path)
    conn = db.connect(db_path)
    try:
        if not db.users_empty(conn):
            return False
        user = db.create_user(
            conn,
            username,
            security.hash_password(password),
            is_admin=True,
        )
        provision_library(settings.data_root, user["id"])
        return True
    finally:
        conn.close()


@router.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request, conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)]):
    if not db.users_empty(conn):
        return deps.redirect("/login")
    return _render_setup(request)


@router.post("/setup", response_class=HTMLResponse)
def setup_submit(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
    username: str = Form(""),
    password: str = Form(""),
    confirm: str = Form(""),
):
    if not db.users_empty(conn):
        return deps.redirect("/login")
    username = username.strip()
    if not username:
        return _render_setup(request, "Username is required.")
    if not password:
        return _render_setup(request, "Password is required.")
    if len(password) < security.MIN_PASSWORD_LENGTH:
        return _render_setup(
            request, f"Password must be at least {security.MIN_PASSWORD_LENGTH} characters."
        )
    if password != confirm:
        return _render_setup(request, "Passwords do not match.")
    user = db.create_first_admin(conn, username, security.hash_password(password))
    if user is None:
        # Lost the first-run race: setup was completed by a concurrent POST.
        return deps.redirect("/login")
    provision_library(settings.data_root, user["id"])
    request.session[SESSION_USER_ID] = user["id"]
    return deps.redirect("/")


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)]):
    if db.users_empty(conn):
        return deps.redirect("/setup")
    return _render_login(request)


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    username: str = Form(""),
    password: str = Form(""),
):
    if db.users_empty(conn):
        return deps.redirect("/setup")
    username = username.strip()
    client_ip = request.client.host if request.client is not None else "unknown"
    throttle_keys = (f"user:{username}", f"ip:{client_ip}")
    remaining = max(db.login_lockout_seconds(conn, key) for key in throttle_keys)
    if remaining:
        return _render_login(
            request, f"Too many failed attempts. Try again in {remaining} seconds."
        )
    user = db.get_user_by_username(conn, username)
    if user is None or not security.verify_password(user["password_hash"], password):
        for key in throttle_keys:
            db.record_login_failure(conn, key)
        return _render_login(request, "Invalid username or password.")
    for key in throttle_keys:
        db.reset_login_failures(conn, key)
    request.session.clear()
    request.session[SESSION_USER_ID] = user["id"]
    return deps.redirect("/")


@router.post("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return deps.redirect("/login")
