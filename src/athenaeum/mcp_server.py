"""Athenaeum MCP server (FastMCP 3.x, Streamable HTTP).

Contract: plan sections 3.1 (six external tools), 3.1a (seed injection),
and Decision 4 (bearer-token auth seam; OAuth 2.1 + PKCE deferred).

- Auth: ``Authorization: Bearer <token>`` -> SHA-256 hex lookup in
  ``mcp_tokens`` (reject missing/unknown/revoked) -> ``user_id`` + token
  ``label`` into request context. ``user_id`` is never accepted from request
  parameters. The seam is ``BearerAuthMiddleware.resolve_user_id``.
- Seed: static base instructions on the server; the per-user seed ships in
  the descriptions of ``store_knowledge`` / ``request_knowledge`` in
  tools/list responses, composed per request by the SeedMiddleware from the
  base description plus the caller's current seed. The shared tool registry
  keeps the base descriptions forever, so no user's seed can leak into
  another user's tools/list response. Verified against fastmcp 3.4.5:
  middleware cannot modify the initialize result per request, so Contract
  2's fallback (descriptions only) applies. After mutating calls the user's
  seed cache entry is invalidated and a tools/list_changed notification is
  sent (best-effort).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import inspect
import logging
import posixpath
import re
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import mcp.types as mt
from fastmcp import FastMCP
from fastmcp.exceptions import McpError, ToolError
from fastmcp.server.context import Context
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from starlette.requests import Request

from athenaeum import db
from athenaeum.activity import ActivityMiddleware, ActivityRegistry
from athenaeum.identity import _identity_var, get_current_identity
from athenaeum.librarian.agent import LibrarianNoWriteError
from athenaeum.librarian.gate import AgentRunBusyError
from athenaeum.librarian.manager import LibrarianManager
from athenaeum.librarian.tools import Backend
from athenaeum.librarian.tracing import TraceSession, _trace_var, telemetry_or_mint

logger = logging.getLogger(__name__)

BASE_INSTRUCTIONS = (
    "Athenaeum is a self-hosted, LLM-maintained personal knowledge base. "
    "A librarian agent curates an OKF v0.2 bundle of markdown concept "
    "documents on your behalf. Ask for knowledge with request_knowledge, "
    "persist new knowledge with store_knowledge (the librarian decides "
    "placement, frontmatter, and linking), change or correct existing "
    "knowledge with update_knowledge, inspect library health with "
    "library_status, drive graph repair with library_maintain, and tidy, "
    "reorganize, and consolidate the library with library_curate. "
    "Trust tiers (unverified / machine-confirmed / human-reviewed) and "
    "staleness flags in responses tell you how much to rely on each concept."
)

SEEDED_TOOL_NAMES = ("request_knowledge", "store_knowledge")

# (user_id, token_label) for the in-flight request lives in athenaeum.identity
# (set by BearerAuthMiddleware below; get_current_identity is re-exported here
# so existing mcp_server.get_current_identity references keep working).

# token_hash (SHA-256 hex) -> (user_id, label) or None
TokenLookup = Callable[[str], tuple[str, str] | None]
SeedGenerator = Callable[[Backend], str]


# A18: last_used_at is orientation metadata, not a per-message audit — write
# it at most once per token per interval instead of on every JSON-RPC message
# (pings, tools/list, notifications all hit the contended WAL database).
LAST_USED_WRITE_INTERVAL = 300.0  # seconds


def sqlite_token_lookup(
    db_path: str | Path,
    *,
    write_interval: float = LAST_USED_WRITE_INTERVAL,
    clock: Callable[[], float] = time.monotonic,
) -> TokenLookup:
    """Token lookup against the mcp_tokens table (schema: plan section 3.5).

    ``last_used_at`` writes are coalesced per token: a token re-authenticating
    within ``write_interval`` seconds skips the UPDATE (A18). The in-memory
    stamp is process-local, so a restart simply re-writes on first use.
    """
    db_path = Path(db_path)
    last_written: dict[str, float] = {}
    write_lock = threading.Lock()

    def lookup(token_hash: str) -> tuple[str, str] | None:
        with db.connect(db_path) as conn:
            row = conn.execute(
                "SELECT user_id, label FROM mcp_tokens WHERE token_hash = ? AND revoked_at IS NULL",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            now = clock()
            with write_lock:
                due = now - last_written.get(token_hash, float("-inf")) >= write_interval
                if due:
                    last_written[token_hash] = now
            if due:
                conn.execute(
                    "UPDATE mcp_tokens SET last_used_at = ? WHERE token_hash = ?",
                    (datetime.now(UTC).isoformat(), token_hash),
                )
        return row[0], row[1]

    return lookup


def _default_seed_generator(backend: Backend) -> str:
    # Lazy import: library/seed.py is Stream A's module.
    from athenaeum.library.seed import generate_seed

    return generate_seed(backend)


class SeedCache:
    """Per-user seed access with a last-known-good fallback.

    The built-in generator caches ``(log.md mtime, seed)`` on the backend
    and self-validates by stat on every call (plan decision (c)), so
    ``get`` simply delegates. ``invalidate`` drops the fallback entry so a
    post-write generator failure cannot resurrect a pre-write seed. Serving
    the fallback is logged as possibly stale (CS-11).
    """

    def __init__(self, seed_generator: SeedGenerator | None = None) -> None:
        self._seed_generator = seed_generator
        self._last_good: dict[str, str] = {}

    def get(self, user_id: str, backend: Backend) -> str:
        generator = self._seed_generator or _default_seed_generator
        try:
            seed = generator(backend)
        except Exception:
            # The seed is best-effort orientation; never break a request.
            logger.exception("seed generation failed for user %s", user_id)
            fallback = self._last_good.get(user_id)
            if fallback is None:
                return ""
            # CS-11: the fallback may be stale — say so instead of silently
            # serving an outdated seed.
            logger.warning("serving last-known-good seed for user %s; it may be stale", user_id)
            return fallback
        self._last_good[user_id] = seed
        return seed

    def invalidate(self, user_id: str) -> None:
        self._last_good.pop(user_id, None)


def _auth_error(message: str) -> McpError:
    return McpError(mt.ErrorData(code=mt.INVALID_REQUEST, message=message))


def _unexpected_tool_error(tool: str, exc: Exception) -> ToolError:
    """Sanitized client-facing error for unexpected failures (CS-5).

    The full exception is logged server-side and recorded in the trace; the
    MCP client gets a generic message (no provider payloads, paths, URLs).
    """
    logger.exception("unexpected error in MCP tool %s", tool)
    return ToolError(f"{tool} failed with an internal error; details were logged server-side.")


NO_WRITE_TOOL_MESSAGE = (
    "The librarian completed without writing anything; retry the request or rephrase it."
)


async def _configured_librarian(manager: LibrarianManager, user_id: str):
    # manager.get can be a cold build (config load + backend reconcile,
    # all synchronous filesystem/sqlite I/O) — keep it off the loop (A1).
    librarian = await asyncio.to_thread(manager.get, user_id)
    if not librarian.configured:
        raise ToolError(
            "Librarian is not configured for this user: set an LLM provider, "
            "model, and API key in the librarian settings."
        )
    return librarian


@asynccontextmanager
async def _agent_run(
    manager: LibrarianManager, user_id: str, label: str, tool: str, *, for_client: bool
) -> AsyncIterator[Any]:
    """Shared trace/exception wiring for one agent-backed run (A8).

    Opens the librarian and a TraceSession, finishes the session by exception
    type (ok / error / cancelled), and closes it. With ``for_client`` (the
    MCP tools) failures translate to client-facing ToolErrors — sanitized for
    unexpected exceptions (CS-5); without it (the scheduler) the original
    exception propagates so the caller can journal the raw error text.
    """
    librarian = await _configured_librarian(manager, user_id)
    session = TraceSession(
        manager.library_root(user_id),
        telemetry_or_mint().trace_id,
        tool=tool,
        agent_label=label,
        keep=librarian.config.trace_keep,
    )
    trace_token = _trace_var.set(session)
    try:
        yield librarian
        session.finish(outcome="ok")
    except LibrarianNoWriteError as exc:
        session.finish(outcome="error", error=str(exc))
        if for_client:
            raise ToolError(NO_WRITE_TOOL_MESSAGE) from exc
        raise
    except AgentRunBusyError as exc:
        session.finish(outcome="error", error=str(exc))
        if for_client:
            raise ToolError(str(exc)) from exc
        raise
    except asyncio.CancelledError:
        session.finish(outcome="cancelled")
        raise
    except Exception as exc:
        session.finish(outcome="error", error=f"{type(exc).__name__}: {exc}")
        if for_client:
            raise _unexpected_tool_error(tool, exc) from exc
        raise
    finally:
        _trace_var.reset(trace_token)
        session.close()


class BearerAuthMiddleware(Middleware):
    """Resolve the bearer token to (user_id, label) for every MCP message.

    Over HTTP the Authorization header is mandatory. When there is no HTTP
    request (in-process transports), the message passes through with whatever
    identity is already in context — the server is only ever exposed over
    Streamable HTTP, so no unauthenticated channel exists in production.
    """

    def __init__(self, token_lookup: TokenLookup) -> None:
        self._token_lookup = token_lookup

    def resolve_user_id(self, request: Request) -> tuple[str, str]:
        """Auth seam (Decision 4): bearer token -> (user_id, token label).

        Raises McpError for missing, unknown, or revoked tokens. user_id is
        never accepted from request parameters.
        """
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise _auth_error("Missing bearer token")
        token_hash = hashlib.sha256(token.strip().encode("utf-8")).hexdigest()
        identity = self._token_lookup(token_hash)
        if identity is None:
            raise _auth_error("Invalid or revoked bearer token")
        return identity

    async def on_message(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        request: Request | None = None
        with suppress(RuntimeError):
            request = get_http_request()
        if request is None:
            return await call_next(context)
        # The sqlite lookup does short-lived synchronous I/O; keep it off the
        # event loop (A1).
        identity = await asyncio.to_thread(self.resolve_user_id, request)
        token = _identity_var.set(identity)
        try:
            return await call_next(context)
        finally:
            _identity_var.reset(token)


class SeedMiddleware(Middleware):
    """Per-user seed injection (plan section 3.1a).

    Verified against fastmcp 3.4.5: middleware CANNOT modify the initialize
    result per request — ``on_initialize`` sees the InitializeResult but the
    mutated copy never reaches the client (the low-level server sends the
    result built by its own handler). Per Contract 2's fallback, the static
    base instructions remain as-is and the seed ships via tool descriptions:
    ``on_list_tools`` composes base description + the caller's current seed
    into per-request copies. The shared registry is never mutated, so a
    caller with an empty library sees base descriptions only.
    """

    def __init__(
        self,
        seed_cache: SeedCache,
        manager: LibrarianManager,
        base_descriptions: dict[str, str],
    ) -> None:
        self._seed_cache = seed_cache
        self._manager = manager
        self._base_descriptions = base_descriptions

    def _seed_for(self, user_id: str) -> str:
        librarian = self._manager.get(user_id)
        return self._seed_cache.get(user_id, librarian.backend)

    def _current_seed(self) -> str:
        identity = get_current_identity()
        if identity is None:
            return ""
        return self._seed_for(identity[0])

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Any],
    ) -> Any:
        tools = await call_next(context)
        # The seed read can regenerate (reads every concept file) and a cold
        # manager.get runs a full reconcile; both stay off the loop (A1).
        seed = await asyncio.to_thread(self._current_seed)
        if not seed:
            return tools
        rewritten = []
        for tool in tools:
            if tool.name in SEEDED_TOOL_NAMES:
                base = self._base_descriptions.get(tool.name) or tool.description or ""
                tool = tool.model_copy(
                    update={"description": f"{base}\n\nCurrent library seed:\n{seed}"}
                )
            rewritten.append(tool)
        return rewritten


MAX_IMAGES_PER_STORE = 5  # attached images accepted per store_knowledge call
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # decoded size cap per image
IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
_IMAGE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize_image_filename(filename: str) -> str:
    """Bare-name, safe-character filename for the asset store."""
    base = posixpath.basename(filename.replace("\\", "/"))
    cleaned = _IMAGE_NAME_RE.sub("-", base)
    return cleaned if cleaned not in ("", ".", "..") else "image"


def _validate_images(images: list[dict] | None) -> list[dict] | None:
    """Validate attached images (ToolError on violation); return them decoded.

    Each accepted image becomes ``{"filename", "media_type", "data"}`` with
    sanitized filename and raw bytes — base64 never travels further in.
    """
    if images is None:
        return None
    if len(images) > MAX_IMAGES_PER_STORE:
        raise ToolError(f"at most {MAX_IMAGES_PER_STORE} images per store_knowledge call.")
    validated = []
    for image in images:
        if not isinstance(image, dict):
            raise ToolError("each image must be an object with filename/media_type/data_base64.")
        media_type = image.get("media_type")
        if media_type not in IMAGE_MEDIA_TYPES:
            raise ToolError(
                f"unsupported image media_type {media_type!r}; "
                f"allowed: {', '.join(sorted(IMAGE_MEDIA_TYPES))}."
            )
        data_base64 = image.get("data_base64")
        if not isinstance(data_base64, str):
            raise ToolError("image data_base64 must be a base64 string.")
        try:
            data = base64.b64decode(data_base64, validate=True)
        except (binascii.Error, ValueError):
            raise ToolError("image data_base64 is not valid base64.") from None
        if len(data) > MAX_IMAGE_BYTES:
            raise ToolError(f"image exceeds the {MAX_IMAGE_BYTES}-byte limit.")
        filename = image.get("filename")
        if not isinstance(filename, str) or not filename:
            raise ToolError("image filename must be a non-empty string.")
        validated.append(
            {
                "filename": _sanitize_image_filename(filename),
                "media_type": media_type,
                "data": data,
            }
        )
    return validated


def create_mcp_server(
    manager: LibrarianManager,
    *,
    token_lookup: TokenLookup,
    seed_cache: SeedCache | None = None,
    name: str = "athenaeum",
    activity_db_path: Path | None = None,
    activity_registry: ActivityRegistry | None = None,
) -> FastMCP:
    """Build the FastMCP instance: auth + seed middleware, 6 external tools.

    When both ``activity_db_path`` and ``activity_registry`` are given, an
    ActivityMiddleware is registered after SeedMiddleware (innermost; T7) so
    tool calls are journaled and tracked in flight.
    """
    seeds = seed_cache or SeedCache()
    mcp = FastMCP(name, instructions=BASE_INSTRUCTIONS)

    def _identity() -> tuple[str, str]:
        identity = get_current_identity()
        if identity is None:
            raise ToolError("Authentication required: no identity resolved.")
        return identity

    @mcp.tool
    async def request_knowledge(query: str, context: str | None = None) -> dict:
        """Ask the librarian for knowledge by intent."""
        user_id, label = _identity()
        async with _agent_run(
            manager, user_id, label, "request_knowledge", for_client=True
        ) as librarian:
            return await librarian.handle_request(query, context=context, agent_label=label)

    @mcp.tool
    async def store_knowledge(
        content: str,
        kind_hint: str | None = None,
        relates_to: list[str] | None = None,
        topic_hint: str | None = None,
        images: list[dict] | None = None,
        ctx: Context = None,
    ) -> dict:
        """Persist new knowledge; the librarian decides placement/frontmatter/links \
(topic_hint names the target topic area). Optional images ([{"filename", \
"media_type", "data_base64"}]) are stored server-side as library assets and \
linked into the stored concepts."""
        user_id, label = _identity()
        validated_images = _validate_images(images)
        async with _agent_run(
            manager, user_id, label, "store_knowledge", for_client=True
        ) as librarian:
            result = await librarian.handle_store(
                content,
                kind_hint=kind_hint,
                relates_to=relates_to,
                topic_hint=topic_hint,
                images=validated_images,
                agent_label=label,
            )
        # The trace session closed when the with-block exited (plan step 3.1).
        await _refresh_seed(user_id, ctx)
        try:
            await librarian.sync_embeddings(result.get("stored") or result.get("actions") or [])
        except Exception:
            logger.exception("embedding sync failed for user %s", user_id)
        return result

    @mcp.tool
    async def update_knowledge(instruction: str, ctx: Context = None) -> dict:
        """Change or correct existing knowledge; the librarian locates the target concepts."""
        user_id, label = _identity()
        async with _agent_run(
            manager, user_id, label, "update_knowledge", for_client=True
        ) as librarian:
            result = await librarian.handle_update(instruction, agent_label=label)
        await _refresh_seed(user_id, ctx)
        try:
            await librarian.sync_embeddings(result.get("stored") or result.get("actions") or [])
        except Exception:
            logger.exception("embedding sync failed for user %s", user_id)
        return result

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
    async def library_status() -> dict:
        """Deterministic library health report; no LLM involved."""
        user_id, _ = _identity()
        librarian = await asyncio.to_thread(manager.get, user_id)
        return await asyncio.to_thread(librarian.backend.status)

    @mcp.tool
    async def library_maintain(instructions: str | None = None, ctx: Context = None) -> dict:
        """Drive the librarian to repair graph health; no-op when healthy."""
        user_id, label = _identity()
        async with _agent_run(
            manager, user_id, label, "library_maintain", for_client=True
        ) as librarian:
            result = await librarian.handle_maintain(instructions, agent_label=label)
        if result.get("actions"):
            await _refresh_seed(user_id, ctx)
        try:
            await librarian.sync_embeddings(result.get("stored") or result.get("actions") or [])
        except Exception:
            logger.exception("embedding sync failed for user %s", user_id)
        return result

    @mcp.tool
    async def library_curate(instructions: str | None = None, ctx: Context = None) -> dict:
        """Curate library organization: fix taxonomy, move misplaced concepts, and \
consolidate duplicates; no-op when well-organized."""
        user_id, label = _identity()
        async with _agent_run(
            manager, user_id, label, "library_curate", for_client=True
        ) as librarian:
            result = await librarian.handle_curate(instructions, agent_label=label)
        # Run-end timestamp on every completed run (no-op and mutating); the
        # exception path above skips it, so failed runs don't re-baseline.
        await asyncio.to_thread(manager.set_curate_last_run, user_id, db.utcnow())
        if result.get("actions"):
            await _refresh_seed(user_id, ctx)
        try:
            await librarian.sync_embeddings(result.get("stored") or result.get("actions") or [])
        except Exception:
            logger.exception("embedding sync failed for user %s", user_id)
        return result

    # @mcp.tool returns the original function; docstrings are the base
    # descriptions the seed middleware composes per request.
    tool_functions = {
        "request_knowledge": request_knowledge,
        "store_knowledge": store_knowledge,
    }
    base_descriptions = {
        tool_name: (inspect.getdoc(fn) or "") for tool_name, fn in tool_functions.items()
    }

    async def _refresh_seed(user_id: str, ctx: Context | None) -> None:
        """Invalidate the user's seed and notify the caller (best-effort).

        Runs inside the mutating tool's request context (plan section 3.1a).
        The registry keeps the base descriptions; the SeedMiddleware composes
        a fresh seed into the next tools/list response per request.
        """
        seeds.invalidate(user_id)
        if ctx is not None:
            try:
                await ctx.send_notification(mt.ToolListChangedNotification())
            except Exception:
                logger.exception("seed refresh notification failed for user %s", user_id)

    mcp.add_middleware(BearerAuthMiddleware(token_lookup))
    mcp.add_middleware(SeedMiddleware(seeds, manager, base_descriptions))
    if activity_db_path is not None and activity_registry is not None:
        mcp.add_middleware(ActivityMiddleware(activity_db_path, activity_registry))
    return mcp
