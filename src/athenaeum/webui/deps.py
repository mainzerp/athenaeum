"""Shared WebUI dependencies: settings, DB, current user, templates, htmx.

The FastAPI ``SessionMiddleware`` wiring lives in ``app.py`` (integration
step); routers here only assume ``request.session`` exists. Routers are
self-contained: settings come from the app-level cached instance (A15) and
the app DB lives at ``<ATHENAEUM_DATA_ROOT>/app.db``.
"""

from __future__ import annotations

import hmac
import secrets
import sqlite3
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from athenaeum import __version__, db, isolation
from athenaeum.config import Settings, get_settings
from athenaeum.librarian.manager import LibrarianManager

# Re-exports (single source of truth is athenaeum.okf, CS-7); kept importable
# here because the WebUI routes and graph module reference them as deps.*.
from athenaeum.okf import TRUST_HUMAN, TRUST_MACHINE, TRUST_UNVERIFIED, is_stale, trust_tier

__all__ = [
    "TRUST_HUMAN",
    "TRUST_MACHINE",
    "TRUST_UNVERIFIED",
    "is_stale",
    "trust_tier",
]

PACKAGE_DIR = Path(__file__).parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# --- CSRF protection (CS-8) ----------------------------------------------------
#
# One token per signed-cookie session, issued lazily and rendered into every
# mutating form as a hidden input. Router-level dependency ``csrf_protect``
# (wired on every WebUI router) validates it on POST. The MCP endpoint is
# bearer-token authenticated (no cookies), so it is exempt by construction.

CSRF_SESSION_KEY = "csrf_token"
CSRF_FORM_FIELD = "csrf_token"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def csrf_token(request: Request) -> str:
    """The session's CSRF token, minting one on first use."""
    token = request.session.get(CSRF_SESSION_KEY)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


async def csrf_protect(request: Request) -> None:
    """Router-level dependency: mint the session token; validate it on POST."""
    expected = csrf_token(request)
    if request.method in _SAFE_METHODS:
        return
    form = await request.form()
    sent = form.get(CSRF_FORM_FIELD)
    if not isinstance(sent, str) or not hmac.compare_digest(sent, expected):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")


def _csrf_input(request: Request) -> Markup:
    """Jinja global: hidden form input carrying the session CSRF token."""
    return Markup(f'<input type="hidden" name="{CSRF_FORM_FIELD}" value="{csrf_token(request)}">')


templates.env.globals["csrf_token"] = csrf_token
templates.env.globals["csrf_input"] = _csrf_input

# DB paths whose schema has already been ensured in this process.
_initialized_dbs: set[str] = set()


def settings_dep(request: Request) -> Settings:
    """FastAPI dependency: the app's one cached Settings instance (A15).

    create_app stores it on ``app.state.settings`` at startup; apps assembled
    without create_app (the self-contained WebUI test app) fall back to
    parsing the environment.
    """
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        settings = get_settings()
    return settings


def manager_dep(request: Request) -> LibrarianManager | None:
    """FastAPI dependency: the app-level LibrarianManager, or None.

    None when the app was assembled without create_app() (e.g. the
    self-contained WebUI test app), which has no manager on app.state.
    """
    return getattr(request.app.state, "librarian_manager", None)


def db_path_for(settings: Settings) -> Path:
    return Path(settings.data_root) / "app.db"


def ensure_db(db_path: Path) -> None:
    """Ensure the schema exists (idempotent, once per path per process)."""
    key = str(db_path.resolve())
    if key not in _initialized_dbs:
        db.init_db(db_path)
        _initialized_dbs.add(key)


def db_dep(settings: Annotated[Settings, Depends(settings_dep)]) -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: one open app-DB connection per request."""
    path = db_path_for(settings)
    ensure_db(path)
    conn = db.connect(path)
    try:
        yield conn
    finally:
        conn.close()


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def login_redirect(conn: sqlite3.Connection) -> RedirectResponse:
    """Unauthenticated users go to first-run setup until an account exists."""
    return redirect("/setup" if db.users_empty(conn) else "/login")


def current_user(request: Request, conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The logged-in user row, or None. Session key is defined in routes_auth."""
    from athenaeum.webui.routes_auth import SESSION_USER_ID

    user_id = request.session.get(SESSION_USER_ID)
    if not user_id:
        return None
    return db.get_user_by_id(conn, user_id)


def require_user(request: Request, conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Alias for current_user; callers redirect/raise when it returns None."""
    return current_user(request, conn)


def library_root_for(settings: Settings, user_id: str) -> Path:
    """Per-user OKF bundle root (plan §3.4: opaque UUID in the path)."""
    isolation.validate_user_id(user_id)
    return Path(settings.data_root) / "users" / user_id / "library"


def get_library_backend(settings: Settings, user: sqlite3.Row, conn: sqlite3.Connection) -> object:
    """Construct a LibraryBackend for the user's bundle (read/history surface).

    Lazy import: ``athenaeum.library`` is owned by stream A. Tests substitute
    this factory with a fake implementing the plan §3.2 read-only surface.
    """
    from athenaeum.library.backend import LibraryBackend

    cfg = db.get_config(conn, user["id"])
    return LibraryBackend(
        library_root_for(settings, user["id"]),
        actor=f"athenaeum-webui/{__version__}",
        git_enabled=bool(cfg["git_enabled"]),
        git_remote_url=cfg["git_remote_url"],
        git_auto_push=bool(cfg["git_auto_push"]),
    )


def build_llm_config(conn_row: sqlite3.Row, api_key: str | None, *, model: str) -> object:
    """Build an LLMConfig (plan §3.4) from a provider_configs row.

    The model is a parameter: it lives on the agent binding, not on the
    connection (0.8.0, D5). Lazy import: ``athenaeum.librarian.llm`` is
    owned by stream B.
    """
    from athenaeum.librarian.llm import LLMConfig

    return LLMConfig(
        provider=conn_row["provider"],
        model=model,
        api_key=api_key,
        base_url=conn_row["base_url"] or None,
        max_iterations=int(conn_row["max_iterations"]),
        temperature=conn_row["temperature"],
        max_tokens=conn_row["max_tokens"],
    )


def create_llm_provider(llm_config: object) -> object:
    """Provider factory (plan §3.4); lazy import of the stream-B module."""
    from athenaeum.librarian.llm import create_provider

    return create_provider(llm_config)


def create_embedding_provider(embedding_config: object, *, cache_dir=None) -> object:
    """Embedding provider factory; lazy import of the embed layer."""
    from athenaeum.librarian.embed import create_embedding_provider as factory

    return factory(embedding_config, cache_dir=cache_dir)


# --- presentation helpers (frontmatter badges, graph colors) ----------------
#
# Trust tier / staleness derivations live in athenaeum.okf (single source of
# truth shared with the librarian, CS-7); the TRUST_* constants and the
# is_stale/trust_tier functions stay importable from this module.


def format_datetime(value: str | None) -> str:
    """Render an ISO 8601 UTC timestamp as ``YYYY-MM-DD HH:MM UTC``.

    None/empty input returns "" (callers apply their own placeholder);
    unparseable input is returned unchanged.
    """
    if not value:
        return ""
    try:
        return datetime.fromisoformat(str(value)).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return str(value)


templates.env.filters["dt"] = format_datetime
