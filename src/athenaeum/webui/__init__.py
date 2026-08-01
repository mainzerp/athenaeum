"""WebUI: FastAPI routers, Jinja2 templates, static assets.

``app.py`` (integration) includes ``ROUTERS`` and mounts ``deps.STATIC_DIR``
at ``/static``; ``SessionMiddleware`` is wired there as well.
"""

from athenaeum.webui import (
    graph,
    routes_activity,
    routes_admin,
    routes_auth,
    routes_config,
    routes_library,
    routes_tokens,
    routes_traces,
)

ROUTERS = [
    routes_auth.router,
    routes_admin.router,
    routes_config.router,
    routes_tokens.router,
    routes_library.router,
    routes_traces.router,
    routes_activity.router,
    graph.router,
]
