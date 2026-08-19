"""Provider connections, agent (librarian/curator), and library settings screens.

Provider connections are named rows in ``provider_configs`` (0.8.0, D1-D3):
list + create/edit/delete + per-connection test button. The API key field is
write-only (empty password-type input; only overwritten when non-empty; never
rendered back). "Test connection" performs one trivial ``complete()`` via the
provider factory. Models live on the agent tabs (D5).
"""

from __future__ import annotations

import asyncio
import logging
import math
import sqlite3
import time
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from athenaeum import db, security
from athenaeum.config import Settings
from athenaeum.curator.prompts import render_curate_prompt_display
from athenaeum.librarian.embed import EmbeddingConfig
from athenaeum.librarian.embed.local import LOCAL_MODEL_SHORTLIST
from athenaeum.librarian.manager import LibrarianManager
from athenaeum.librarian.prompts import build_system_prompt
from athenaeum.library import semantic as semantic_mod
from athenaeum.scheduler import CurateScheduler
from athenaeum.webui import deps

router = APIRouter(prefix="/config", dependencies=[Depends(deps.csrf_protect)])

logger = logging.getLogger(__name__)

PROVIDERS = ["openai", "anthropic", "gemini", "openrouter", "openai-compatible"]


def _opt_str(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _opt_float(value: str | None) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = float(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid number") from None
    if not math.isfinite(parsed):
        raise HTTPException(status_code=400, detail="Invalid number")
    return parsed


def _opt_int(value: str | None) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid integer") from None


def _opt_keep(value: str | None) -> int:
    """Retention keep value: clamped to >= 0 (a negative keep prunes EVERYTHING)."""
    return max(0, _opt_int(value) or 0)


def _opt_hhmm(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if not db.HHMM_RE.fullmatch(value):
        raise HTTPException(status_code=400, detail="Invalid time (expected HH:MM)")
    return value


def _validated_threshold(value: str | None) -> float | None:
    """Form value -> optional cosine threshold; empty means "model default"."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        threshold = float(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid threshold") from None
    if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        raise HTTPException(status_code=400, detail="Threshold must be between 0.0 and 1.0")
    return threshold


# --- Provider connections ------------------------------------------------------


def _provider_error_redirect(message: str):
    return deps.redirect(f"/config/provider?{urlencode({'error': message})}")


def _evict(manager: LibrarianManager | None, user_id: str) -> None:
    if manager is not None:
        manager.evict(user_id)


_EMBED_RECONCILE_TASKS: set[asyncio.Task] = set()


def _kick_embed_reconcile(manager: LibrarianManager | None, user_id: str) -> None:
    """Fire-and-forget: cold-build the librarian, trigger its deferred reconcile.

    Strong-ref set (scheduler start_run_now idiom); the coroutine swallows and
    logs everything, so the task never raises unobserved. The reconcile's first
    embed batch downloads a not-yet-cached local model off the event loop
    (local.py _shared_model memoizes it process-wide).
    """
    if manager is None or getattr(manager, "get", None) is None:
        return  # WebUI test app's FakeManager has no get (getattr idiom, cf. embedding_form)
    task = asyncio.create_task(_embed_reconcile_after_save(manager, user_id))
    _EMBED_RECONCILE_TASKS.add(task)
    task.add_done_callback(_EMBED_RECONCILE_TASKS.discard)


async def _embed_reconcile_after_save(manager: LibrarianManager, user_id: str) -> None:
    try:
        librarian = await asyncio.to_thread(manager.get, user_id)
        librarian._maybe_embed_reconcile()
    except Exception:
        logger.exception("post-save embedding reconcile kick failed for user %s", user_id)


@router.get("/provider")
def provider_form(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    saved: bool = False,
    error: str | None = None,
):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    connections = db.list_provider_configs(conn, user["id"])
    return deps.templates.TemplateResponse(
        request,
        "config_provider.html",
        {
            "user": user,
            "connections": connections,
            "providers": PROVIDERS,
            "saved": saved,
            "error": error,
        },
    )


@router.post("/provider/new")
def provider_create(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
    manager: Annotated[LibrarianManager | None, Depends(deps.manager_dep)],
    label: str = Form(""),
    provider: str = Form(""),
    base_url: str = Form(""),
    api_key: str = Form(""),
    max_iterations: str = Form("10"),
    temperature: str = Form(""),
    max_tokens: str = Form(""),
):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    label = label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="Label is required")
    provider = provider.strip()
    if provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail="Unknown provider")
    # Write-only key field: encrypt and store only when a value was entered.
    api_key = api_key.strip()
    api_key_enc = security.encrypt_secret(api_key, settings.secret_key) if api_key else None
    db.create_provider_config(
        conn,
        user["id"],
        label=label,
        provider=provider,
        api_key_enc=api_key_enc,
        base_url=_opt_str(base_url),
        max_iterations=_opt_int(max_iterations) or 10,
        temperature=_opt_float(temperature),
        max_tokens=_opt_int(max_tokens),
    )
    _evict(manager, user["id"])
    return deps.redirect("/config/provider?saved=1")


@router.get("/provider/{connection_id}")
def provider_edit_form(
    request: Request,
    connection_id: str,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    connection = db.get_provider_config(conn, user["id"], connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    return deps.templates.TemplateResponse(
        request,
        "config_provider_edit.html",
        {
            "user": user,
            "c": connection,
            "providers": PROVIDERS,
            "key_set": bool(connection["api_key_enc"]),
        },
    )


@router.post("/provider/{connection_id}")
def provider_edit_save(
    request: Request,
    connection_id: str,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
    manager: Annotated[LibrarianManager | None, Depends(deps.manager_dep)],
    label: str = Form(""),
    provider: str = Form(""),
    base_url: str = Form(""),
    api_key: str = Form(""),
    max_iterations: str = Form("10"),
    temperature: str = Form(""),
    max_tokens: str = Form(""),
):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    if db.get_provider_config(conn, user["id"], connection_id) is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    label = label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="Label is required")
    provider = provider.strip()
    if provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail="Unknown provider")
    api_key = api_key.strip()
    api_key_enc = security.encrypt_secret(api_key, settings.secret_key) if api_key else None
    db.update_provider_config(
        conn,
        user["id"],
        connection_id,
        label=label,
        provider=provider,
        base_url=_opt_str(base_url),
        max_iterations=_opt_int(max_iterations) or 10,
        temperature=_opt_float(temperature),
        max_tokens=_opt_int(max_tokens),
        api_key_enc=api_key_enc,
    )
    _evict(manager, user["id"])
    return deps.redirect("/config/provider?saved=1")


@router.post("/provider/{connection_id}/delete")
def provider_delete(
    request: Request,
    connection_id: str,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    manager: Annotated[LibrarianManager | None, Depends(deps.manager_dep)],
):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    connection = db.get_provider_config(conn, user["id"], connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    if db.count_provider_config_bindings(conn, connection_id) > 0:
        return _provider_error_redirect(
            "Connection is still assigned to an agent; rebind the agent first."
        )
    if connection["is_default"] and len(db.list_provider_configs(conn, user["id"])) > 1:
        return _provider_error_redirect("Set another connection as default first.")
    db.delete_provider_config(conn, user["id"], connection_id)
    _evict(manager, user["id"])
    return deps.redirect("/config/provider?saved=1")


@router.post("/provider/{connection_id}/default")
def provider_set_default(
    request: Request,
    connection_id: str,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    manager: Annotated[LibrarianManager | None, Depends(deps.manager_dep)],
):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    if db.get_provider_config(conn, user["id"], connection_id) is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    db.set_default_provider_config(conn, user["id"], connection_id)
    _evict(manager, user["id"])
    return deps.redirect("/config/provider?saved=1")


@router.post("/provider/{connection_id}/test")
async def provider_test(
    request: Request,
    connection_id: str,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
    api_key: str = Form(""),
):
    """One trivial complete() against the saved connection (htmx partial)."""
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    connection = db.get_provider_config(conn, user["id"], connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    cfg = db.get_config(conn, user["id"])
    ok, message = False, ""
    if not cfg["llm_model"]:
        message = "Save a provider and model first."
    else:
        key = api_key.strip()
        if not key and connection["api_key_enc"]:
            key = security.decrypt_secret(connection["api_key_enc"], settings.secret_key)
        if not key:
            message = "No API key configured."
        else:
            try:
                llm_config = deps.build_llm_config(connection, key, model=cfg["llm_model"])
                provider = deps.create_llm_provider(llm_config)
                response = await provider.complete(
                    [{"role": "user", "content": "ping"}], [], llm_config
                )
                ok, message = True, f"Connection OK ({type(response).__name__})."
            except ImportError:
                message = "LLM provider layer is not available in this build."
            except Exception as exc:  # provider/network errors surface verbatim
                message = f"Connection failed: {exc}"
    return deps.templates.TemplateResponse(
        request, "test_result.html", {"user": user, "ok": ok, "message": message}
    )


# --- Agents (librarian / curator tabs) ----------------------------------------


def _validated_connection_id(
    conn: sqlite3.Connection, user_id: str, connection_id: str
) -> str | None:
    """Form value -> connection id; empty means "default connection" (None)."""
    connection_id = connection_id.strip()
    if not connection_id:
        return None
    if db.get_provider_config(conn, user_id, connection_id) is None:
        raise HTTPException(status_code=400, detail="Unknown connection")
    return connection_id


@router.get("/agents")
def agents_index():
    return deps.redirect("/config/agents/librarian")


@router.get("/agents/librarian")
def librarian_form(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    saved: bool = False,
):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    cfg = db.get_config(conn, user["id"])
    addendum = cfg["prompt_addendum"]
    return deps.templates.TemplateResponse(
        request,
        "config_agents.html",
        {
            "user": user,
            "cfg": cfg,
            "tab": "librarian",
            "connections": db.list_provider_configs(conn, user["id"]),
            "prompt": build_system_prompt(addendum),
            "addendum_set": bool(addendum),
            "saved": saved,
        },
    )


@router.post("/agents/librarian")
def librarian_save(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    manager: Annotated[LibrarianManager | None, Depends(deps.manager_dep)],
    connection_id: str = Form(""),
    model: str = Form(""),
    prompt_addendum: str = Form(""),
):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    db.update_librarian_config(
        conn,
        user["id"],
        connection_id=_validated_connection_id(conn, user["id"], connection_id),
        model=_opt_str(model),
        prompt_addendum=prompt_addendum.strip() or None,
    )
    _evict(manager, user["id"])
    return deps.redirect("/config/agents/librarian?saved=1")


@router.get("/agents/curator")
def curator_form(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    saved: bool = False,
    msg: str | None = None,
    error: str | None = None,
):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    cfg = db.get_config(conn, user["id"])
    addendum = cfg["curate_prompt_addendum"]
    return deps.templates.TemplateResponse(
        request,
        "config_agents.html",
        {
            "user": user,
            "cfg": cfg,
            "tab": "curator",
            "connections": db.list_provider_configs(conn, user["id"]),
            "curator_prompt": render_curate_prompt_display(addendum),
            "addendum_set": bool(addendum),
            "saved": saved,
            "msg": msg,
            "error": error,
        },
    )


@router.post("/agents/curator")
def curator_save(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    manager: Annotated[LibrarianManager | None, Depends(deps.manager_dep)],
    connection_id: str = Form(""),
    curator_model: str = Form(""),
    curate_prompt_addendum: str = Form(""),
):
    """Optional curator binding; empty fields mean "default connection"."""
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    db.update_curate_config(
        conn,
        user["id"],
        connection_id=_validated_connection_id(conn, user["id"], connection_id),
        curator_model=_opt_str(curator_model),
        curate_prompt_addendum=_opt_str(curate_prompt_addendum),
    )
    _evict(manager, user["id"])
    return deps.redirect("/config/agents/curator?saved=1")


@router.post("/agents/curator/schedule")
def curator_schedule_save(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    manager: Annotated[LibrarianManager | None, Depends(deps.manager_dep)],
    curate_schedule_enabled: str | None = Form(None),
    curate_schedule_time: str = Form(""),
):
    """Nightly maintain+curate schedule; the time is stored as UTC HH:MM."""
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    db.update_curate_schedule(
        conn,
        user["id"],
        enabled=curate_schedule_enabled is not None,
        time_hhmm=_opt_hhmm(curate_schedule_time) or db.DEFAULT_SCHEDULE_TIME,
    )
    _evict(manager, user["id"])
    return deps.redirect("/config/agents/curator?saved=1")


@router.post("/agents/curator/run")
async def curator_run_now(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    scheduler: Annotated[CurateScheduler | None, Depends(deps.scheduler_dep)],
):
    """Manual 'Run now': background library_curate via the scheduler's wiring."""
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    base = "/config/agents/curator?"
    if scheduler is None:
        return deps.redirect(base + urlencode({"error": "Manual runs are unavailable."}))
    if scheduler.curator_busy(user["id"]):
        return deps.redirect(base + urlencode({"error": "A curator run is already in progress."}))
    scheduler.start_run_now(user["id"])
    msg = "Curation run started; progress and result appear on the Activity page."
    return deps.redirect(base + urlencode({"msg": msg}))


# --- Agents (embeddings tab) ---------------------------------------------------


def _resolve_embedding_config(
    conn: sqlite3.Connection, user_id: str, settings: Settings
) -> EmbeddingConfig | None:
    """Saved embedding row -> EmbeddingConfig; None = unconfigured/unusable.

    Mirrors ``LibrarianManager._load_config``'s embedding resolution: an empty
    binding, a dangling connection, or an anthropic connection (no embeddings
    endpoint) all mean "unconfigured".
    """
    cfg = db.get_config(conn, user_id)
    source, model = cfg["embedding_source"], cfg["embedding_model"]
    if not source or not model:
        return None
    if source == "local":
        return EmbeddingConfig(source="local", model=model)
    if source != "api":
        return None
    connection_id = cfg["embedding_connection_id"]
    if connection_id:
        conn_row = db.get_provider_config(conn, user_id, connection_id)
    else:
        conn_row = next(
            (c for c in db.list_provider_configs(conn, user_id) if c["is_default"]), None
        )
    if conn_row is None or conn_row["provider"] == "anthropic":
        return None
    api_key = ""
    if conn_row["api_key_enc"]:
        api_key = security.decrypt_secret(conn_row["api_key_enc"], settings.secret_key)
    return EmbeddingConfig(
        source="api",
        model=model,
        provider=conn_row["provider"],
        api_key=api_key,
        base_url=conn_row["base_url"] or None,
    )


@router.get("/agents/embeddings")
def embedding_form(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    saved: bool = False,
):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    cfg = db.get_config(conn, user["id"])
    # anthropic connections have no embeddings endpoint: never offered here.
    connections = [
        c for c in db.list_provider_configs(conn, user["id"]) if c["provider"] != "anthropic"
    ]
    manager = getattr(request.app.state, "librarian_manager", None)
    status_fn = getattr(manager, "embed_status_for", None)
    embed_status = status_fn(user["id"]) if status_fn is not None else None
    return deps.templates.TemplateResponse(
        request,
        "config_agents.html",
        {
            "user": user,
            "cfg": cfg,
            "tab": "embeddings",
            "connections": connections,
            "local_models": LOCAL_MODEL_SHORTLIST,
            "embed_status": embed_status,
            "embed_stats": db.embedding_stats(conn, user["id"]),
            "threshold_default": semantic_mod.semantic_threshold_for_model(cfg["embedding_model"]),
            "saved": saved,
        },
    )


@router.post("/agents/embeddings")
async def embedding_save(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    manager: Annotated[LibrarianManager | None, Depends(deps.manager_dep)],
    source: str = Form(""),
    local_model: str = Form(""),
    api_model: str = Form(""),
    connection_id: str = Form(""),
    semantic_threshold: str = Form(""),
    hybrid_search: str | None = Form(None),
    hybrid_rerank: str | None = Form(None),
):
    """Embedding binding; the empty source disables embeddings. Saving evicts
    the cached librarian so the next build picks up the new config. A CHANGED
    (source, model) binding additionally kicks the background full re-embed
    (incl. the local model download) immediately; plain re-saves only evict."""
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    old_cfg = db.get_config(conn, user["id"])
    old_binding = (old_cfg["embedding_source"], old_cfg["embedding_model"])
    threshold = _validated_threshold(semantic_threshold)
    hybrid_on = hybrid_search is not None
    rerank_on = hybrid_rerank is not None
    source = source.strip()
    if source not in ("", "local", "api"):
        raise HTTPException(status_code=400, detail="Unknown embedding source")
    if not source:
        new_binding: tuple[str | None, str | None] = (None, None)
        db.update_embedding_config(
            conn,
            user["id"],
            source=None,
            model=None,
            connection_id=None,
            semantic_threshold=threshold,
            hybrid_search=hybrid_on,
            hybrid_rerank=rerank_on,
        )
    elif source == "local":
        model = local_model.strip()
        if model not in {name for name, _ in LOCAL_MODEL_SHORTLIST}:
            raise HTTPException(status_code=400, detail="Unknown local model")
        new_binding = ("local", model)
        db.update_embedding_config(
            conn,
            user["id"],
            source="local",
            model=model,
            connection_id=None,
            semantic_threshold=threshold,
            hybrid_search=hybrid_on,
            hybrid_rerank=rerank_on,
        )
    else:
        model = api_model.strip()
        if not model:
            raise HTTPException(status_code=400, detail="API model is required")
        validated = _validated_connection_id(conn, user["id"], connection_id)
        if validated is not None:
            conn_row = db.get_provider_config(conn, user["id"], validated)
        else:
            conn_row = next(
                (c for c in db.list_provider_configs(conn, user["id"]) if c["is_default"]),
                None,
            )
        if conn_row is None:
            raise HTTPException(status_code=400, detail="No provider connection available")
        if conn_row["provider"] == "anthropic":
            raise HTTPException(
                status_code=400,
                detail="anthropic connections have no embeddings endpoint",
            )
        new_binding = ("api", model)
        db.update_embedding_config(
            conn,
            user["id"],
            source="api",
            model=model,
            connection_id=validated,
            semantic_threshold=threshold,
            hybrid_search=hybrid_on,
            hybrid_rerank=rerank_on,
        )
    _evict(manager, user["id"])
    if new_binding[0] is not None and new_binding != old_binding:
        _kick_embed_reconcile(manager, user["id"])
    return deps.redirect("/config/agents/embeddings?saved=1")


@router.post("/agents/embeddings/test")
async def embedding_test(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
):
    """One trivial embed call against the SAVED embedding config (htmx partial)."""
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    emb_config = _resolve_embedding_config(conn, user["id"], settings)
    ok, message = False, ""
    if emb_config is None:
        message = "Save an embedding source and model first."
    else:
        try:
            provider = deps.create_embedding_provider(
                emb_config, cache_dir=Path(settings.data_root) / "embedding-models"
            )
            started = time.perf_counter()
            vectors = await provider.embed(["athenaeum embedding test"], emb_config)
            elapsed_ms = (time.perf_counter() - started) * 1000
            ok = True
            message = f"Embedding OK ({len(vectors[0])} dims in {elapsed_ms:.0f} ms)."
        except Exception as exc:  # provider/network errors surface verbatim
            message = f"Embedding failed: {exc}"
    return deps.templates.TemplateResponse(
        request, "test_result.html", {"user": user, "ok": ok, "message": message}
    )


# --- Library settings + maintenance ------------------------------------------


@router.get("/library")
def library_form(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
    saved: bool = False,
    msg: str | None = None,
    error: str | None = None,
):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    cfg = db.get_config(conn, user["id"])
    return deps.templates.TemplateResponse(
        request,
        "config_library.html",
        {
            "user": user,
            "cfg": cfg,
            "saved": saved,
            "msg": msg,
            "library_path": str(deps.library_root_for(settings, user["id"])),
            "validation": None,
            "error": error,
        },
    )


@router.post("/library")
def library_save(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    manager: Annotated[LibrarianManager | None, Depends(deps.manager_dep)],
    name: str = Form(""),
    description: str = Form(""),
    git_enabled: str | None = Form(None),
    git_remote_url: str = Form(""),
    git_auto_push: str | None = Form(None),
    trace_keep: str = Form("0"),
    activity_keep: str = Form("0"),
    payload_keep: str = Form("0"),
):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    db.update_library_settings(
        conn,
        user["id"],
        name=name.strip() or None,
        description=description.strip() or None,
        git_enabled=git_enabled is not None,
        git_remote_url=git_remote_url.strip() or None,
        git_auto_push=git_auto_push is not None,
        trace_keep=_opt_keep(trace_keep),
        activity_keep=_opt_keep(activity_keep),
        payload_keep=_opt_keep(payload_keep),
    )
    if manager is not None:
        manager.evict(user["id"])
    return deps.redirect("/config/library?saved=1")


@router.post("/library/reconcile")
def library_reconcile(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
):
    """Maintenance action: regenerate all index.md files (crash-safety pass)."""
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    backend = deps.get_library_backend(settings, user, conn)
    backend.reconcile()
    return deps.redirect("/config/library?msg=Index+files+regenerated.")


@router.post("/library/validate")
def library_validate(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
):
    """Maintenance action: run the OKF validator over the whole bundle."""
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    backend = deps.get_library_backend(settings, user, conn)
    validation = backend.validate()
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
            "validation": validation,
        },
    )


# --- Legacy route redirects (GET only) -----------------------------------------


@router.get("/llm")
def llm_redirect():
    return deps.redirect("/config/provider")


@router.get("/behavior")
def behavior_redirect():
    return deps.redirect("/config/agents/librarian")


@router.get("/prompt")
def prompt_redirect():
    return deps.redirect("/config/agents/librarian")
