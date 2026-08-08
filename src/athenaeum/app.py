"""FastAPI application assembly (plan Step 4 / section 8).

One process serves both surfaces:

- the WebUI (Jinja2 + htmx routers from ``athenaeum.webui.ROUTERS``) behind
  signed-cookie sessions (``SessionMiddleware``, key from
  ``ATHENAEUM_SECRET_KEY``), and
- the MCP server (FastMCP Streamable HTTP) at ``/mcp`` with per-user bearer
  token auth.

The FastMCP ASGI app's lifespan is chained into the parent app's lifespan so
the Streamable HTTP session manager runs (do NOT use startup/shutdown
decorators for it). Startup also runs the optional first-run admin bootstrap
(env pre-seed hook, only consumed while the users table is empty).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from athenaeum import __version__, db, security
from athenaeum.activity import ActivityRegistry
from athenaeum.config import Settings, get_settings
from athenaeum.librarian.manager import LibrarianManager
from athenaeum.mcp_server import SeedCache, create_mcp_server, sqlite_token_lookup
from athenaeum.scheduler import CurateScheduler
from athenaeum.webui import ROUTERS, deps
from athenaeum.webui.routes_auth import bootstrap_admin_if_configured

logger = logging.getLogger(__name__)


async def _warm_local_embedding_models(db_path, data_root) -> None:
    """Preload local ONNX embedding models into the process-wide cache (0.23.0).

    Background task: never blocks startup (healthz answers immediately) and
    never raises — a failed preload just means the first real embed call
    pays the load as before.
    """
    try:
        with db.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT embedding_model FROM librarian_configs"
                " WHERE embedding_source = 'local' AND embedding_model IS NOT NULL"
            ).fetchall()
        names = [row["embedding_model"] for row in rows]
        if not names:
            return
        from athenaeum.librarian.embed.local import preload_local_models

        loaded = await asyncio.to_thread(
            preload_local_models, names, Path(data_root) / "embedding-models"
        )
        if loaded:
            logger.info("preloaded local embedding models: %s", ", ".join(loaded))
        failed = [name for name in names if name not in loaded]
        if failed:
            logger.warning("local embedding model preload failed for: %s", ", ".join(failed))
    except Exception:
        logger.warning("local embedding model warm-up failed", exc_info=True)


class _MCPCatchAll:
    """ASGI guard bounding the root catch-all mount to the MCP endpoint (A17).

    The FastMCP app answers only /mcp; without this guard every unmatched
    WebUI path fell through to it and got a JSON-RPC error instead of a
    plain 404, and router/mount ordering stayed load-bearing. WebUI routes
    are registered before the mount, so this wrapper only sees paths no
    WebUI route claimed.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # SERVER-12: exact /mcp or a child path only — a bare startswith would
        # also route WebUI typos like /mcpfoo into the FastMCP app.
        path = scope.get("path", "")
        if scope["type"] == "lifespan" or path == "/mcp" or path.startswith("/mcp/"):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        response = JSONResponse({"detail": "Not Found"}, status_code=404)
        await response(scope, receive, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Assemble the FastAPI app from server settings (env by default)."""
    settings = settings or get_settings()
    db_path = deps.db_path_for(settings)
    deps.ensure_db(db_path)

    manager = LibrarianManager(
        db_path,
        settings.data_root,
        key_decryptor=lambda ciphertext: security.decrypt_secret(ciphertext, settings.secret_key),
    )
    activity_registry = ActivityRegistry()
    seed_cache = SeedCache()
    mcp_server = create_mcp_server(
        manager,
        token_lookup=sqlite_token_lookup(db_path),
        seed_cache=seed_cache,
        activity_db_path=db_path,
        activity_registry=activity_registry,
    )
    # Inner route at /mcp; the app is mounted at the root below so the MCP
    # endpoint stays exactly /mcp. stateless_http is read once at startup;
    # changing it (Admin > Server) takes effect on restart.
    conn = db.connect(db_path)
    try:
        stateless_http = db.get_app_setting(conn, "mcp_stateless_http", "0") == "1"
    finally:
        conn.close()
    mcp_app = mcp_server.http_app(path="/mcp", stateless_http=stateless_http)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if bootstrap_admin_if_configured(settings):
            logger.info("bootstrapped admin account from ATHENAEUM_BOOTSTRAP_ADMIN_*")
        # FastMCP's Streamable HTTP session manager lives in the sub-app's
        # lifespan; chain it into the parent lifespan.
        async with mcp_app.lifespan(app):
            # Nightly scheduled curation runs alongside the session manager;
            # cancelled cleanly on shutdown.
            scheduler = CurateScheduler(
                manager, db_path, registry=activity_registry, seed_cache=seed_cache
            )
            app.state.curate_scheduler = scheduler
            task = asyncio.create_task(scheduler.run_forever())
            # Preload local ONNX embedding models in the background (0.23.0):
            # first searches stay fast instead of paying the model load.
            warmup = asyncio.create_task(_warm_local_embedding_models(db_path, settings.data_root))
            try:
                yield
            finally:
                task.cancel()
                warmup.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                with suppress(asyncio.CancelledError):
                    await warmup
                # Cancel pending embed reconciles; their claim rows are
                # released in the service's finally (A5).
                manager.close()

    app = FastAPI(title="athenaeum", version=__version__, lifespan=lifespan)
    # One Settings instance per app (A15): settings_dep serves this cached
    # object instead of re-parsing the environment on every WebUI request.
    app.state.settings = settings
    app.state.librarian_manager = manager
    app.state.activity_registry = activity_registry
    app.state.seed_cache = seed_cache
    # CS-8: pin the cookie flags explicitly. same_site="lax" is the CSRF
    # baseline; https_only stays False because the documented deployment is
    # plain-HTTP self-hosting (behind a TLS proxy, set it at the proxy).
    app.add_middleware(
        SessionMiddleware, secret_key=settings.secret_key, same_site="lax", https_only=False
    )

    @app.get("/healthz")
    def healthz() -> dict:
        """Unauthenticated liveness probe (Docker HEALTHCHECK); opens the DB."""
        conn = db.connect(db_path)
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()
        return {"status": "ok"}

    for router in ROUTERS:
        app.include_router(router)

    app.mount("/static", StaticFiles(directory=str(deps.STATIC_DIR)), name="static")
    # Catch-all mount, registered last: the guard bounds it to /mcp, so paths
    # no WebUI route claimed get a plain 404, never the FastMCP app (A17).
    app.mount("/", _MCPCatchAll(mcp_app), name="mcp")
    return app
