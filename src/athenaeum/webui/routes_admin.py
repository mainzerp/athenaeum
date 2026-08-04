"""Admin routes: user list / create / reset-password (admin only).

Decision 2 (plan §1): after first-run bootstrap, admins create further
accounts in the WebUI; there is no self-registration.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from athenaeum import db, security
from athenaeum.computation import ComputationError, validate_sqlite_dbname
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
            "computation_execution_enabled": db.get_app_setting(
                conn, "computation_execution_enabled", "0"
            )
            == "1",
            "saved": saved,
        },
    )


@router.post("/server")
def server_submit(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    mcp_stateless_http: str | None = Form(None),
    computation_execution_enabled: str | None = Form(None),
):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    db.set_app_setting(conn, "mcp_stateless_http", "1" if mcp_stateless_http is not None else "0")
    # Read live per execution (no restart needed, unlike mcp_stateless_http).
    db.set_app_setting(
        conn,
        "computation_execution_enabled",
        "1" if computation_execution_enabled is not None else "0",
    )
    return deps.redirect("/admin/server?saved=1")


# --- Runtime connections (Attested Computations; admin-managed shared) --------

RUNTIMES = ["postgres", "sqlite"]


def _opt_str(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _connection_form_values(
    label: str,
    runtime: str,
    host: str,
    port: str,
    dbname: str,
    username: str,
) -> dict:
    """Validate the connection form; raises HTTPException(400) on violation."""
    label = label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="Label is required")
    runtime = runtime.strip()
    if runtime not in RUNTIMES:
        raise HTTPException(status_code=400, detail="Unknown runtime")
    port_value = None
    if runtime == "postgres":
        if not port.strip():
            raise HTTPException(status_code=400, detail="Port is required for postgres")
        try:
            port_value = int(port.strip())
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid port") from None
        missing = [
            name
            for name, value in (("host", host), ("dbname", dbname), ("username", username))
            if not value.strip()
        ]
        if missing:
            raise HTTPException(
                status_code=400, detail=f"Required for postgres: {', '.join(missing)}"
            )
    if runtime == "sqlite":
        if not dbname.strip():
            raise HTTPException(status_code=400, detail="Database file path is required for sqlite")
        try:
            validate_sqlite_dbname(dbname.strip())
        except ComputationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
    return {
        "label": label,
        "runtime": runtime,
        "host": _opt_str(host) if runtime == "postgres" else None,
        "port": port_value,
        "dbname": _opt_str(dbname),
        "username": _opt_str(username) if runtime == "postgres" else None,
    }


@router.get("/connections")
def connections_page(
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
        "admin_connections.html",
        {
            "user": user,
            "connections": db.list_runtime_connections(conn),
            "runtimes": RUNTIMES,
            "saved": saved,
        },
    )


@router.post("/connections")
def connection_create(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
    label: str = Form(""),
    runtime: str = Form(""),
    host: str = Form(""),
    port: str = Form(""),
    dbname: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    values = _connection_form_values(label, runtime, host, port, dbname, username)
    # Write-only password field: encrypt and store only when a value was entered.
    password = password.strip()
    values["password_enc"] = (
        security.encrypt_secret(password, settings.secret_key) if password else None
    )
    db.create_runtime_connection(conn, **values)
    return deps.redirect("/admin/connections?saved=1")


@router.get("/connections/{connection_id}/edit")
def connection_edit_form(
    request: Request,
    connection_id: str,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    connection = db.get_runtime_connection(conn, connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    return deps.templates.TemplateResponse(
        request,
        "admin_connection_edit.html",
        {
            "user": user,
            "c": connection,
            "runtimes": RUNTIMES,
            "password_set": bool(connection["password_enc"]),
        },
    )


@router.post("/connections/{connection_id}/edit")
def connection_edit_save(
    request: Request,
    connection_id: str,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
    label: str = Form(""),
    runtime: str = Form(""),
    host: str = Form(""),
    port: str = Form(""),
    dbname: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    if db.get_runtime_connection(conn, connection_id) is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    values = _connection_form_values(label, runtime, host, port, dbname, username)
    # Empty password field means "unchanged" (write-only; never rendered back).
    password = password.strip()
    values["password_enc"] = (
        security.encrypt_secret(password, settings.secret_key) if password else None
    )
    db.update_runtime_connection(conn, connection_id, **values)
    return deps.redirect("/admin/connections?saved=1")


@router.post("/connections/{connection_id}/delete")
def connection_delete(
    request: Request,
    connection_id: str,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    if db.get_runtime_connection(conn, connection_id) is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    db.delete_runtime_connection(conn, connection_id)
    return deps.redirect("/admin/connections?saved=1")


@router.post("/connections/{connection_id}/test")
def connection_test(
    request: Request,
    connection_id: str,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
):
    """One trivial probe against the saved connection (htmx partial).

    sqlite: open the file read-only. postgres: connect and ``SELECT 1``.
    Sync route: FastAPI runs it on the threadpool (A1).
    """
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    connection = db.get_runtime_connection(conn, connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    ok, message = False, ""
    try:
        if connection["runtime"] == "sqlite":
            validate_sqlite_dbname(connection["dbname"])
            probe = sqlite3.connect(f"file:{connection['dbname']}?mode=ro", uri=True)
            probe.close()
            ok, message = True, "Connection OK (file opened read-only)."
        else:
            import psycopg

            password = None
            if connection["password_enc"]:
                password = security.decrypt_secret(connection["password_enc"], settings.secret_key)
            probe = psycopg.connect(
                host=connection["host"],
                port=connection["port"],
                dbname=connection["dbname"],
                user=connection["username"],
                password=password,
                connect_timeout=5,
            )
            try:
                probe.execute("SELECT 1")
            finally:
                probe.close()
            ok, message = True, "Connection OK (SELECT 1)."
    except Exception as exc:  # driver/OS errors surface sanitized (no password)
        message = f"Connection failed: {type(exc).__name__}: {exc}"
        if connection["password_enc"]:
            try:
                secret = security.decrypt_secret(connection["password_enc"], settings.secret_key)
                message = message.replace(secret, "***")
            except Exception:
                pass
    return deps.templates.TemplateResponse(
        request, "test_result.html", {"user": user, "ok": ok, "message": message}
    )


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
