"""Library browsing and whole-bundle transfer: the document view (tree page
takeover — the library tree and a sunburst minimap flank the selected
document with its per-document timeline, inline diff, edit, delete, and
restore), the log viewer, export (zip download), and import
(replace-restore). The standalone Time-Machine page and the library-wide
history section are gone; the legacy ``/library/time-machine`` and
``/library/document`` URLs 301-redirect here.

Read-only views over the plan §3.2 LibraryBackend surface. All paths are
bundle-relative and scoped to the logged-in user's own library root, so
cross-user access is structurally impossible (the path simply does not
exist in another user's bundle and surfaces as 404). Export/import operate
on the user's own bundle only; import is a writer class honoring the run
gates and the per-root write lock (architecture §7). Document mutations
(restore/edit/delete) journal via ``journal_activity``, mirroring
the import route's pattern.
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
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from starlette.background import BackgroundTask

from athenaeum import db
from athenaeum.activity import journal_activity
from athenaeum.config import Settings
from athenaeum.librarian.agent import KIND_CURATOR, KIND_LIBRARIAN
from athenaeum.librarian.gate import AgentRunBusyError
from athenaeum.librarian.manager import LibrarianManager
from athenaeum.librarian.tracing import RequestTelemetry, mint_request_id
from athenaeum.library import transfer
from athenaeum.library.gittool import GitError
from athenaeum.webui import deps, markdown_render

router = APIRouter(dependencies=[Depends(deps.csrf_protect)])

MAX_UPLOAD_BYTES = 512 * 1024 * 1024  # 512 MiB upload cap (read at call time)
_UPLOAD_CHUNK = 1024 * 1024

_TREE_PAGE = "/library/tree"

# Unified-context line count for inline (in-flow) diffs: large enough to
# always cover the whole file, so the rendered view shows the full document
# with the accumulated changes vs HEAD marked inline — not just the hunks.
_INLINE_DIFF_CONTEXT = 1_000_000


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
    path: str | None = None,
    sha: str | None = None,
    msg: str | None = None,
    error: str | None = None,
):
    """Document view: tree + minimap flank the selected document (``path``);
    without one the center pane shows an empty state."""
    ctx = _backend(request, conn, settings)
    if ctx is None:
        return deps.login_redirect(conn)
    user, backend = ctx
    entries = backend.list_dir("/")
    history_available = backend.history_available
    doc, fm = None, None
    tags: list = []
    trust, stale = None, False
    body_html, diff_html = "", None
    timeline, viewed = [], None
    viewed_index, head = 0, None
    if path is not None:
        file_commits = []
        if history_available:
            try:
                file_commits = backend.file_history(path)
            except (ValueError, GitError):
                # Reserved paths (index.md/log.md) get no timeline; the never-
                # raise contract of file_history makes this cheap insurance.
                file_commits = []
            head = backend.git_head()
        # Slider model: oldest-LEFT / newest-RIGHT; the rightmost stop doubles
        # as the live view (the newest file commit IS the live content).
        timeline = file_commits[::-1]
        viewed_index = len(timeline) - 1
        if sha and history_available:
            if head and (head == sha or head.startswith(sha)):
                # The viewed commit IS the live state: plain live view, no banner.
                doc = _read_document(backend, path)
            else:
                try:
                    doc = backend.read_document_at(path, sha)
                except (FileNotFoundError, ValueError) as exc:
                    raise HTTPException(status_code=404, detail="Document not found") from exc
                except GitError as exc:
                    raise HTTPException(status_code=404, detail="Commit not found") from exc
                for index, entry in enumerate(file_commits):
                    if entry["sha"] == sha or entry["sha"].startswith(sha):
                        viewed = entry
                        viewed_index = len(file_commits) - 1 - index
                        break
                if viewed is None:
                    # Commit outside the file's own log (hand-crafted URL); the
                    # template guards the empty timestamp.
                    viewed = {"sha": sha, "short": sha[:7], "timestamp": "", "subject": ""}
                # Historical stop: the inline vs-HEAD diff replaces the body.
                diff_html = markdown_render.render_inline_diff_html(
                    backend.file_diff_to_head(sha, path, context=_INLINE_DIFF_CONTEXT)
                )
        else:
            doc = _read_document(backend, path)
        fm = doc.get("frontmatter") or {}
        tags = fm.get("tags") or []
        trust = deps.trust_tier(fm)
        stale = deps.is_stale(fm)
        body_html = markdown_render.render_markdown(doc["body"])
    bootstrap = {
        "path": path,
        "timeline": timeline,
        "viewedIndex": viewed_index,
        "landedLive": viewed is None,
        "head": head,
        "historyAvailable": history_available,
        # Forms rebuilt client-side after an in-page selection reuse the
        # session token (already rendered into the server-side forms).
        "csrf": deps.csrf_token(request),
    }
    return deps.templates.TemplateResponse(
        request,
        "document_view.html",
        {
            "user": user,
            "entries": entries,
            "root": "/",
            "doc": doc,
            "fm": fm,
            "tags": tags,
            "trust": trust,
            "stale": stale,
            "body_html": body_html,
            "timeline": timeline,
            "viewed": viewed,
            "viewed_index": viewed_index,
            "diff_html": diff_html,
            "history_configured": backend.history_configured,
            "history_available": history_available,
            "bootstrap": bootstrap,
            "msg": msg,
            "error": error,
        },
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
def document_page_redirect(path: str, sha: str | None = None):
    """Legacy document URL: the document view lives at /library/tree now
    (keeps graph-page navigation and external bookmarks working)."""
    params = {"path": path}
    if sha:
        params["sha"] = sha
    return RedirectResponse(f"{_TREE_PAGE}?{urlencode(params)}", status_code=301)


@router.get("/library/document/data")
def document_data(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
    path: str,
):
    """JSON payload for in-page center-pane swaps (document_view.js)."""
    ctx = _backend(request, conn, settings)
    if ctx is None:
        return deps.login_redirect(conn)
    user, backend = ctx
    doc = _read_document(backend, path)
    fm = doc.get("frontmatter") or {}
    history_available = backend.history_available
    timeline = []
    head = None
    if history_available:
        try:
            # Reserved paths (index.md/log.md) get no timeline.
            timeline = backend.file_history(path)[::-1]
        except (ValueError, GitError):
            timeline = []
        head = backend.git_head()
    return {
        "path": path,
        "title": fm.get("title") or path,
        "description": fm.get("description") or "",
        "type": fm.get("type") or "unknown",
        "status": fm.get("status") or "stable",
        "tags": fm.get("tags") or [],
        "trust": deps.trust_tier(fm),
        "stale": deps.is_stale(fm),
        "body": doc["body"],
        "body_html": markdown_render.render_markdown(doc["body"]),
        "timeline": timeline,
        "viewed_index": len(timeline) - 1,
        "head": head,
        "history_available": history_available,
        "history_configured": backend.history_configured,
    }


@router.get("/library/document/diff")
def document_diff(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
    path: str,
    sha: str,
    mode: str = "transcript",
):
    """JSON preview diff of one document: ``sha`` vs current HEAD (slider).

    ``mode="transcript"`` (default) returns the per-line patch transcript;
    ``mode="inline"`` returns the in-flow document diff.
    """
    ctx = _backend(request, conn, settings)
    if ctx is None:
        return deps.login_redirect(conn)
    user, backend = ctx
    if mode not in ("transcript", "inline"):
        raise HTTPException(status_code=400, detail="Unknown diff mode")
    if not backend.history_available:
        raise HTTPException(status_code=404, detail="History unavailable")
    head = backend.git_head()
    if head and (head == sha or head.startswith(sha)):
        return {"diff_html": ""}
    try:
        if mode == "inline":
            patch = backend.file_diff_to_head(sha, path, context=_INLINE_DIFF_CONTEXT)
            return {"diff_html": markdown_render.render_inline_diff_html(patch)}
        patch = backend.file_diff_to_head(sha, path)
        return {"diff_html": markdown_render.render_diff_html(patch)}
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Document not found") from exc
    except GitError as exc:
        raise HTTPException(status_code=404, detail="Commit not found") from exc


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


# --- document history mutations (timeline restore) ----------------------------


def _error_redirect(message: str):
    return deps.redirect(f"{_TREE_PAGE}?{urlencode({'error': message})}")


def _journal(
    settings: Settings,
    user_id: str,
    tool: str,
    arguments: str,
    started_at: str,
    start: float,
    outcome: str,
    error_text: str | None,
) -> None:
    """One activity-journal row for a history mutation (sync routes run
    in the threadpool, so the synchronous journal write is legal here)."""
    telemetry = RequestTelemetry(trace_id=mint_request_id())
    journal_activity(
        deps.db_path_for(settings),
        trace_id=telemetry.trace_id,
        user_id=user_id,
        token_label="webui",
        tool=tool,
        arguments=arguments,
        started_at=started_at,
        duration_ms=(time.perf_counter() - start) * 1000,
        outcome=outcome,
        error_text=error_text,
        telemetry=telemetry,
    )


@router.post("/library/document/restore")
def document_restore(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
    manager: Annotated[LibrarianManager | None, Depends(deps.manager_dep)],
    path: str = Form(...),
    sha: str = Form(...),
):
    """Restore ONE document to its state at a commit (one new commit)."""
    ctx = _backend(request, conn, settings)
    if ctx is None:
        return deps.login_redirect(conn)
    user, backend = ctx
    started_at, start = db.utcnow(), time.perf_counter()
    outcome, error_text = "ok", None
    try:
        backend.restore_file_from_commit(path, sha)
    except (GitError, ValueError) as exc:
        outcome, error_text = "error", str(exc)
        result = deps.redirect(f"{_TREE_PAGE}?{urlencode({'path': path, 'error': str(exc)})}")
    except Exception as exc:
        outcome, error_text = "error", f"{type(exc).__name__}: {exc}"
        raise
    else:
        # Embedding/FTS reconcile on the next agent entry (import-route
        # precedent); the seed cache self-heals via log.md mtime anyway.
        if manager is not None:
            manager.evict(user["id"])
        seed_cache = getattr(request.app.state, "seed_cache", None)
        if seed_cache is not None:
            seed_cache.invalidate(user["id"])
        result = deps.redirect(
            f"{_TREE_PAGE}?{urlencode({'path': path, 'msg': f'Restored to commit {sha[:7]}.'})}"
        )
    finally:
        _journal(
            settings,
            user["id"],
            "document_restore",
            json.dumps({"path": path, "sha": sha}),
            started_at,
            start,
            outcome,
            error_text,
        )
    return result


@router.post("/library/document/edit")
def document_edit(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
    manager: Annotated[LibrarianManager | None, Depends(deps.manager_dep)],
    path: str = Form(...),
    body: str = Form(...),
):
    """Edit ONE document's body (frontmatter untouched; one new commit)."""
    ctx = _backend(request, conn, settings)
    if ctx is None:
        return deps.login_redirect(conn)
    user, backend = ctx
    started_at, start = db.utcnow(), time.perf_counter()
    outcome, error_text = "ok", None
    try:
        backend.edit_concept(path, new_body=body, agent_label="webui")
    except (FileNotFoundError, ValueError) as exc:
        outcome, error_text = "error", str(exc)
        result = deps.redirect(f"{_TREE_PAGE}?{urlencode({'path': path, 'error': str(exc)})}")
    except Exception as exc:
        outcome, error_text = "error", f"{type(exc).__name__}: {exc}"
        raise
    else:
        # Same post-write hygiene as document_restore: evict the cached
        # librarian and invalidate the per-request seed cache.
        if manager is not None:
            manager.evict(user["id"])
        seed_cache = getattr(request.app.state, "seed_cache", None)
        if seed_cache is not None:
            seed_cache.invalidate(user["id"])
        result = deps.redirect(f"{_TREE_PAGE}?{urlencode({'path': path, 'msg': 'Saved.'})}")
    finally:
        _journal(
            settings,
            user["id"],
            "document_edit",
            json.dumps({"path": path}),
            started_at,
            start,
            outcome,
            error_text,
        )
    return result


@router.post("/library/document/delete")
def document_delete(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
    manager: Annotated[LibrarianManager | None, Depends(deps.manager_dep)],
    path: str = Form(...),
):
    """Delete ONE document (one new commit; the log entry keeps the record)."""
    ctx = _backend(request, conn, settings)
    if ctx is None:
        return deps.login_redirect(conn)
    user, backend = ctx
    started_at, start = db.utcnow(), time.perf_counter()
    outcome, error_text = "ok", None
    try:
        backend.delete_concept(path, agent_label="webui")
    except (FileNotFoundError, ValueError) as exc:
        outcome, error_text = "error", str(exc)
        result = _error_redirect(str(exc))
    except Exception as exc:
        outcome, error_text = "error", f"{type(exc).__name__}: {exc}"
        raise
    else:
        if manager is not None:
            manager.evict(user["id"])
        seed_cache = getattr(request.app.state, "seed_cache", None)
        if seed_cache is not None:
            seed_cache.invalidate(user["id"])
        name = path.rstrip("/").rsplit("/", 1)[-1]
        # No path on the redirect: the document is gone.
        result = deps.redirect(f"{_TREE_PAGE}?{urlencode({'msg': f'Deleted {name}.'})}")
    finally:
        _journal(
            settings,
            user["id"],
            "document_delete",
            json.dumps({"path": path}),
            started_at,
            start,
            outcome,
            error_text,
        )
    return result


# --- legacy Time-Machine URLs (folded into the tree page, 0.22.0) ----------------
# No login gate, no backend: plain 301s keep old bookmarks alive. A 301 on a
# POST is re-issued as GET by browsers (method not preserved) — only reachable
# from pre-upgrade stale pages; the router-level csrf_protect still runs first.


@router.get("/library/time-machine")
def time_machine_page_redirect():
    return RedirectResponse(_TREE_PAGE, status_code=301)


@router.get("/library/time-machine/diff")
def time_machine_diff_redirect(commit: str):
    # The per-commit diff page is gone with the library-wide history section.
    return RedirectResponse(_TREE_PAGE, status_code=301)


@router.post("/library/time-machine/{sha}/revert")
def time_machine_revert_redirect(sha: str):
    return RedirectResponse(_TREE_PAGE, status_code=301)


@router.post("/library/time-machine/reset")
def time_machine_reset_redirect():
    return RedirectResponse(_TREE_PAGE, status_code=301)


@router.post("/library/time-machine/pull")
def time_machine_pull_redirect():
    return RedirectResponse(_TREE_PAGE, status_code=301)


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
