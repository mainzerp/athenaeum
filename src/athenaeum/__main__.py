"""``python -m athenaeum`` entrypoint: uvicorn, single worker (plan section 8.1).

Multi-worker is incorrect for phase 1: librarian instances, the seed cache,
and agent-loop state are per-user in-process objects (LibrarianManager), and
concurrent SQLite writes across workers invite lock contention.
"""

from __future__ import annotations

import logging

import uvicorn

from athenaeum.config import get_settings


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())

    from athenaeum.app import create_app

    # Trusted-proxy story (SERVER-02): only when forwarded_allow_ips is set do
    # we let uvicorn honor X-Forwarded-* from those peers; otherwise the
    # uvicorn default (loopback only, headers effectively untrusted for remote
    # clients) keeps request.client.host honest for the login throttle.
    uvicorn_kwargs: dict = {}
    if settings.forwarded_allow_ips:
        uvicorn_kwargs["proxy_headers"] = True
        uvicorn_kwargs["forwarded_allow_ips"] = settings.forwarded_allow_ips
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        workers=1,
        log_level=settings.log_level.lower(),
        **uvicorn_kwargs,
    )


if __name__ == "__main__":
    main()
