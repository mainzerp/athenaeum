"""Library browsing, git history, and whole-bundle transfer: tree (with the
library-wide history section), document (with the per-document timeline and
restore), per-commit diff, log viewer, export (zip download), and import
(replace-restore). The standalone Time-Machine page is gone; its legacy
``/library/time-machine`` URLs 301-redirect here.

Read-only views over the plan §3.2 LibraryBackend surface. All paths are
bundle-relative and scoped to the logged-in user's own library root, so
cross-user access is structurally impossible (the path simply does not
exist in another user's bundle and surfaces as 404). Export/import operate
on the user's own bundle only; import is a writer class honoring the run
gates and the per-root write lock (architecture §7). History mutations
(restore/revert/reset/pull) journal via ``journal_activity``, mirroring
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
    msg: str | None = None,
    error: str | None = None,
):
    ctx = _backend(request, conn, settings)
    if ctx is None:
        return deps.login_redirect(conn)
    user, backend = ctx
    entries = backend.list_dir("/")
    history_available = backend.history_available
    commits, head = [], None
    if history_available:
        try:
            commits = backend.list_commits()
            head = backend.git_head()
        except GitError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    cfg = db.get_config(conn, user["id"])
    return deps.templates.TemplateResponse(
        request,
        "tree.html",
        {
            "user": user,
            "entries": entries,
            "root": "/",
            "commits": commits,
            "head": head,
            # Slider targets: every commit except HEAD, oldest first.
            "reset_points": [
                {
                    "sha": c["sha"],
                    "short": c["short"],
                    "timestamp": c["timestamp"],
                    "subject": c["subject"],
                }
                for c in reversed(commits[1:])
            ],
            "history_configured": backend.history_configured,
            "history_available": history_available,
            "remote_configured": bool(cfg["git_remote_url"]),
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
def document_page(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
    path: str,
    sha: str | None = None,
    msg: str | None = None,
    error: str | None = None,
):
    ctx = _backend(request, conn, settings)
    if ctx is None:
        return deps.login_redirect(conn)
    user, backend = ctx
    history_available = backend.history_available
    file_commits, viewed, diff_html = [], None, None
    head = None
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
            diff_html = markdown_render.render_diff_html(backend.file_diff_at(sha, path))
    else:
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
            "body_html": markdown_render.render_markdown(doc["body"]),
            "timeline": timeline,
            "viewed": viewed,
            "viewed_index": viewed_index,
            "diff_html": diff_html,
            "history_configured": backend.history_configured,
            "history_available": history_available,
            "msg": msg,
            "error": error,
        },
    )


@router.get("/library/document/diff")
def document_diff(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
    path: str,
    sha: str,
):
    """JSON preview diff of one document: ``sha`` vs current HEAD (slider)."""
    ctx = _backend(request, conn, settings)
    if ctx is None:
        return deps.login_redirect(conn)
    user, backend = ctx
    if not backend.history_available:
        raise HTTPException(status_code=404, detail="History unavailable")
    head = backend.git_head()
    if head and (head == sha or head.startswith(sha)):
        return {"diff_html": ""}
    try:
        patch = backend.file_diff_to_head(sha, path)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Document not found") from exc
    except GitError as exc:
        raise HTTPException(status_code=404, detail="Commit not found") from exc
    return {"diff_html": markdown_render.render_diff_html(patch)}


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


# --- git history (timeline restore, library-wide mutations, diff) --------------

_TREE_PAGE = "/library/tree"


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
        result = deps.redirect(f"/library/document?{urlencode({'path': path, 'error': str(exc)})}")
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
            f"/library/document?"
            f"{urlencode({'path': path, 'msg': f'Restored to commit {sha[:7]}.'})}"
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


@router.get("/library/diff")
def history_diff_page(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
    commit: str,
):
    ctx = _backend(request, conn, settings)
    if ctx is None:
        return deps.login_redirect(conn)
    user, backend = ctx
    try:
        diff = backend.commit_diff(commit)
    except GitError as exc:
        raise HTTPException(status_code=404, detail="Commit not found") from exc
    subject, short = "", commit[:7]
    try:
        for entry in backend.list_commits():
            if entry["sha"] == commit or entry["sha"].startswith(commit):
                subject, short = entry["subject"], entry["short"]
                break
    except GitError:
        pass  # the diff above succeeded; the header falls back to the sha
    return deps.templates.TemplateResponse(
        request,
        "diff.html",
        {"user": user, "diff": diff, "short": short, "subject": subject},
    )


@router.post("/library/history/{sha}/revert")
def history_revert(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
    sha: str,
):
    """Undo one commit (reverse-apply + one log-faithful commit)."""
    ctx = _backend(request, conn, settings)
    if ctx is None:
        return deps.login_redirect(conn)
    user, backend = ctx
    started_at, start = db.utcnow(), time.perf_counter()
    outcome, error_text = "ok", None
    try:
        backend.revert_commit(sha)
    except GitError as exc:
        outcome, error_text = "error", str(exc)
        result = _error_redirect(str(exc))
    except Exception as exc:
        outcome, error_text = "error", f"{type(exc).__name__}: {exc}"
        raise
    else:
        result = deps.redirect(f"{_TREE_PAGE}?msg=Commit+reverted.")
    finally:
        _journal(
            settings,
            user["id"],
            "time_machine_revert",
            json.dumps({"sha": sha}),
            started_at,
            start,
            outcome,
            error_text,
        )
    return result


@router.post("/library/history/reset")
def history_reset(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
    sha: str = Form(...),
):
    """Reset the library to an earlier commit (append-only, undoable)."""
    ctx = _backend(request, conn, settings)
    if ctx is None:
        return deps.login_redirect(conn)
    user, backend = ctx
    started_at, start = db.utcnow(), time.perf_counter()
    outcome, error_text = "ok", None
    try:
        backend.reset_to_commit(sha)
    except GitError as exc:
        outcome, error_text = "error", str(exc)
        result = _error_redirect(str(exc))
    except Exception as exc:
        outcome, error_text = "error", f"{type(exc).__name__}: {exc}"
        raise
    else:
        result = deps.redirect(f"{_TREE_PAGE}?msg=Library+reset.+Use+the+slider+again+to+undo.")
    finally:
        _journal(
            settings,
            user["id"],
            "time_machine_reset",
            json.dumps({"sha": sha}),
            started_at,
            start,
            outcome,
            error_text,
        )
    return result


@router.post("/library/history/pull")
def history_pull(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
    manager: Annotated[LibrarianManager | None, Depends(deps.manager_dep)],
):
    """Fast-forward pull from the configured remote (button only rendered
    when one is configured; divergence surfaces as an error flash)."""
    ctx = _backend(request, conn, settings)
    if ctx is None:
        return deps.login_redirect(conn)
    user, backend = ctx
    cfg = db.get_config(conn, user["id"])
    if not cfg["git_remote_url"]:
        return _error_redirect("No remote configured.")
    started_at, start = db.utcnow(), time.perf_counter()
    outcome, error_text = "ok", None
    try:
        backend.git_pull()
    except GitError as exc:
        outcome, error_text = "error", str(exc)
        result = _error_redirect(str(exc))
    except Exception as exc:
        outcome, error_text = "error", f"{type(exc).__name__}: {exc}"
        raise
    else:
        # The WebUI route is the sole pull trigger: evict the cached
        # librarian so the next agent run reconciles embeddings/FTS, and
        # invalidate the per-request seed cache (belt and braces — the
        # log.md mtime self-heal covers it anyway).
        if manager is not None:
            manager.evict(user["id"])
        seed_cache = getattr(request.app.state, "seed_cache", None)
        if seed_cache is not None:
            seed_cache.invalidate(user["id"])
        result = deps.redirect(f"{_TREE_PAGE}?msg=Pulled.")
    finally:
        _journal(
            settings,
            user["id"],
            "time_machine_pull",
            "{}",
            started_at,
            start,
            outcome,
            error_text,
        )
    return result


# --- legacy Time-Machine URLs (folded into tree/document, 0.22.0) --------------
# No login gate, no backend: plain 301s keep old bookmarks alive. A 301 on a
# POST is re-issued as GET by browsers (method not preserved) — only reachable
# from pre-upgrade stale pages; the router-level csrf_protect still runs first.


@router.get("/library/time-machine")
def time_machine_page_redirect():
    return RedirectResponse(_TREE_PAGE, status_code=301)


@router.get("/library/time-machine/diff")
def time_machine_diff_redirect(commit: str):
    return RedirectResponse(f"/library/diff?{urlencode({'commit': commit})}", status_code=301)


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
