"""Library browsing + whole-bundle transfer: tree, document, versions/diffs,
log viewer, export (zip download), and import (replace-restore).

Read-only views over the plan §3.2 LibraryBackend surface. All paths are
bundle-relative and scoped to the logged-in user's own library root, so
cross-user access is structurally impossible (the path simply does not
exist in another user's bundle and surfaces as 404). Export/import operate
on the user's own bundle only; import is a writer class honoring the run
gates and the per-root write lock (architecture §7).
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from athenaeum import db
from athenaeum.activity import journal_activity
from athenaeum.config import Settings
from athenaeum.isolation import PathEscapeError
from athenaeum.librarian.agent import KIND_CURATOR, KIND_LIBRARIAN
from athenaeum.librarian.gate import AgentRunBusyError
from athenaeum.librarian.manager import LibrarianManager
from athenaeum.librarian.tracing import RequestTelemetry, mint_request_id
from athenaeum.library import transfer
from athenaeum.webui import deps

router = APIRouter(dependencies=[Depends(deps.csrf_protect)])

MAX_UPLOAD_BYTES = 512 * 1024 * 1024  # 512 MiB upload cap (read at call time)
_UPLOAD_CHUNK = 1024 * 1024


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


# --- whole-bundle export / import ----------------------------------------------


def _library_page_error(
    request: Request,
    conn: sqlite3.Connection,
    settings: Settings,
    user: sqlite3.Row,
    message: str,
    status_code: int,
):
    """Re-render the Library settings page with an error flash and the real
    status code (success paths stay 303 redirects)."""
    cfg = db.get_config(conn, user["id"])
    return deps.templates.TemplateResponse(
        request,
        "config_library.html",
        {
            "user": user,
            "cfg": cfg,
            "saved": False,
            "msg": None,
            "library_path": str(deps.library_root_for(settings, user["id"])),
            "validation": None,
            "error": message,
        },
        status_code=status_code,
    )


@router.get("/library/export")
def library_export(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
):
    """Download the whole library bundle as one zip archive (backup)."""
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    root = deps.library_root_for(settings, user["id"])
    tmp_zip = root.parent / f"library.export-{os.getpid()}.tmp.zip"
    telemetry = RequestTelemetry(trace_id=mint_request_id())
    started_at = db.utcnow()
    start = time.perf_counter()
    outcome, error_text = "ok", None
    try:
        transfer.export_bundle(root, tmp_zip)
    except Exception as exc:
        outcome, error_text = "error", f"{type(exc).__name__}: {exc}"
        tmp_zip.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Export failed") from exc
    finally:
        # Sync route (threadpool): the synchronous journal write is legal here.
        journal_activity(
            deps.db_path_for(settings),
            trace_id=telemetry.trace_id,
            user_id=user["id"],
            token_label="webui",
            tool="library_export",
            arguments="{}",
            started_at=started_at,
            duration_ms=(time.perf_counter() - start) * 1000,
            outcome=outcome,
            error_text=error_text,
            telemetry=telemetry,
        )
    return FileResponse(
        tmp_zip,
        media_type="application/zip",
        filename=f"athenaeum-library-{datetime.now(UTC):%Y-%m-%d}.zip",
        background=BackgroundTask(tmp_zip.unlink, missing_ok=True),
    )


async def _replace_and_refresh(
    request: Request,
    conn: sqlite3.Connection,
    settings: Settings,
    manager: LibrarianManager | None,
    user: sqlite3.Row,
    root: Path,
    staging: Path,
) -> None:
    """Evict the cached librarian, swap the staged bundle in, then refresh.

    Runs with both run gates held (the caller); the per-root write lock
    itself is taken inside ``replace_staged_bundle``.
    """
    uid = user["id"]
    if manager is not None:
        # Also cancels the pending embed reconcile so it cannot scan a
        # half-restored tree (A5).
        manager.evict(uid)
    await asyncio.to_thread(
        transfer.replace_staged_bundle, root, staging, root.parent / transfer.BACKUP_ZIP_NAME
    )
    seed_cache = getattr(request.app.state, "seed_cache", None)
    if seed_cache is not None:
        seed_cache.invalidate(uid)
    backend = deps.get_library_backend(settings, user, conn)
    await asyncio.to_thread(backend.reconcile)


async def _spool_upload(file: UploadFile, dest_path: Path) -> int:
    """Stream the upload to ``dest_path`` in chunks; returns the byte count.

    The async read side drives the loop; the disk writes are offloaded so
    the event loop never blocks on filesystem I/O (A1).
    """
    total = 0
    dest = await asyncio.to_thread(open, dest_path, "wb")
    try:
        while chunk := await file.read(_UPLOAD_CHUNK):
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                break
            await asyncio.to_thread(dest.write, chunk)
    finally:
        await asyncio.to_thread(dest.close)
    await file.close()
    return total


@router.post("/library/import")
async def library_import(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
    manager: Annotated[LibrarianManager | None, Depends(deps.manager_dep)],
    file: Annotated[UploadFile, File()],
):
    """Replace the whole library with an uploaded archive (restore).

    Order of operations: validate + stage BEFORE the run gates (fail fast
    without blocking agent runs), then gates -> evict -> backup -> swap ->
    seed-cache invalidate -> reconcile; journaled either way.
    """
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    uid = user["id"]
    root = deps.library_root_for(settings, uid)
    upload_tmp = root.parent / "library.import-upload.tmp.zip"
    telemetry = RequestTelemetry(trace_id=mint_request_id())
    started_at = db.utcnow()
    start = time.perf_counter()
    outcome, error_text = "ok", None
    try:
        total = await _spool_upload(file, upload_tmp)
        if total > MAX_UPLOAD_BYTES:
            outcome, error_text = "error", "archive exceeds the 512 MB upload limit"
            return _library_page_error(
                request, conn, settings, user, "Archive exceeds the 512 MB upload limit.", 413
            )
        try:
            staging = await asyncio.to_thread(transfer.stage_import, root, upload_tmp)
        except transfer.TransferError as exc:
            outcome, error_text = "error", str(exc)
            return _library_page_error(request, conn, settings, user, str(exc), 400)
        try:
            if manager is not None:
                async with manager.run_gate.acquire(uid, KIND_LIBRARIAN, wait=False):
                    async with manager.run_gate.acquire(uid, KIND_CURATOR, wait=False):
                        await _replace_and_refresh(
                            request, conn, settings, manager, user, root, staging
                        )
            else:
                await _replace_and_refresh(request, conn, settings, manager, user, root, staging)
        except AgentRunBusyError as exc:
            outcome, error_text = "error", str(exc)
            return _library_page_error(request, conn, settings, user, str(exc), 409)
        except transfer.TransferError as exc:
            outcome, error_text = "error", str(exc)
            return _library_page_error(request, conn, settings, user, str(exc), 400)
        return deps.redirect(
            "/config/library?msg=Library+imported.+The+previous+library+was+"
            "backed+up+to+import-backup.zip."
        )
    except Exception as exc:
        outcome, error_text = "error", f"{type(exc).__name__}: {exc}"
        raise
    finally:
        upload_tmp.unlink(missing_ok=True)
        await asyncio.to_thread(
            journal_activity,
            deps.db_path_for(settings),
            trace_id=telemetry.trace_id,
            user_id=uid,
            token_label="webui",
            tool="library_import",
            arguments=json.dumps({"filename": file.filename or ""}),
            started_at=started_at,
            duration_ms=(time.perf_counter() - start) * 1000,
            outcome=outcome,
            error_text=error_text,
            telemetry=telemetry,
        )
