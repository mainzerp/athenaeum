"""Library browsing: document tree, document view, versions/diffs, log viewer.

Read-only views over the plan §3.2 LibraryBackend surface. All paths are
bundle-relative and scoped to the logged-in user's own library root, so
cross-user access is structurally impossible (the path simply does not
exist in another user's bundle and surfaces as 404).
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from athenaeum.config import Settings
from athenaeum.isolation import PathEscapeError
from athenaeum.webui import deps

router = APIRouter(dependencies=[Depends(deps.csrf_protect)])


def _backend(
    request: Request,
    conn: sqlite3.Connection,
    settings: Settings,
) -> tuple[sqlite3.Row, object] | None:
    user = deps.current_user(request, conn)
    if user is None:
        return None
    return user, deps.get_library_backend(settings, user, conn)


def _read_document(backend: object, path: str) -> dict:
    try:
        return backend.read_document(path)
    except (FileNotFoundError, ValueError) as exc:
        # Missing document or rejected path: indistinguishable, both 404.
        raise HTTPException(status_code=404, detail="Document not found") from exc


@router.get("/")
def home(request: Request, conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)]):
    if deps.current_user(request, conn) is None:
        return deps.login_redirect(conn)
    return deps.redirect("/library/tree")


@router.get("/library/tree")
def tree_page(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
):
    ctx = _backend(request, conn, settings)
    if ctx is None:
        return deps.login_redirect(conn)
    user, backend = ctx
    entries = backend.list_dir("/")
    return deps.templates.TemplateResponse(
        request, "tree.html", {"user": user, "entries": entries, "root": "/"}
    )


@router.get("/library/tree/children")
def tree_children(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
    path: str = "/",
):
    """htmx lazy-expansion endpoint: one directory's entries as a <ul> partial."""
    ctx = _backend(request, conn, settings)
    if ctx is None:
        return deps.login_redirect(conn)
    user, backend = ctx
    try:
        entries = backend.list_dir(path)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Directory not found") from exc
    return deps.templates.TemplateResponse(
        request, "tree_children.html", {"user": user, "entries": entries}
    )


@router.get("/library/document")
def document_page(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
    path: str,
):
    ctx = _backend(request, conn, settings)
    if ctx is None:
        return deps.login_redirect(conn)
    user, backend = ctx
    doc = _read_document(backend, path)
    fm = doc.get("frontmatter") or {}
    return deps.templates.TemplateResponse(
        request,
        "document.html",
        {
            "user": user,
            "doc": doc,
            "fm": fm,
            "tags": fm.get("tags") or [],
            "trust": deps.trust_tier(fm),
            "stale": deps.is_stale(fm),
        },
    )


@router.get("/library/versions")
def versions_page(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
):
    ctx = _backend(request, conn, settings)
    if ctx is None:
        return deps.login_redirect(conn)
    user, backend = ctx
    return deps.templates.TemplateResponse(
        request,
        "versions.html",
        {"user": user, "versions": backend.list_versions()},
    )


@router.get("/library/versions/diff")
def version_diff_page(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
    n: int,
    path: str,
):
    ctx = _backend(request, conn, settings)
    if ctx is None:
        return deps.login_redirect(conn)
    user, backend = ctx
    try:
        diff = backend.diff_version(n, path)
    except PathEscapeError as exc:
        raise HTTPException(status_code=400, detail="Invalid path") from exc
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Version not found") from exc
    return deps.templates.TemplateResponse(
        request, "diff.html", {"user": user, "n": n, "path": path, "diff": diff}
    )


@router.post("/library/versions/{n}/rollback")
def version_rollback(
    request: Request,
    n: int,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
):
    """Restore the pre-image of a snapshot. Latest-only (CS-12): any older n
    is a 400; the template only offers the button on the latest row."""
    ctx = _backend(request, conn, settings)
    if ctx is None:
        return deps.login_redirect(conn)
    user, backend = ctx
    try:
        backend.rollback(n)
    except PathEscapeError as exc:
        raise HTTPException(status_code=400, detail="Invalid path") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Version not found") from exc
    return deps.redirect("/library/versions")


@router.get("/library/log")
def log_page(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
):
    ctx = _backend(request, conn, settings)
    if ctx is None:
        return deps.login_redirect(conn)
    user, backend = ctx
    return deps.templates.TemplateResponse(request, "log.html", {"user": user})


@router.get("/library/log/content")
def log_content(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
):
    """htmx polling target: current log.md body, read-only."""
    ctx = _backend(request, conn, settings)
    if ctx is None:
        return deps.login_redirect(conn)
    user, backend = ctx
    try:
        body = backend.read_document("/log.md").get("body", "")
    except (FileNotFoundError, ValueError):
        body = ""
    return deps.templates.TemplateResponse(
        request, "log_content.html", {"user": user, "body": body}
    )
