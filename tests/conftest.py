"""Shared fixtures for integration/E2E tests (plan Step 4).

Stream test files keep their own local fixtures; these serve the cross-stream
end-to-end tests that exercise the real app assembly (``athenaeum.app``).
"""

from __future__ import annotations

import base64
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from athenaeum import db as db_module
from athenaeum import security
from athenaeum.app import create_app
from athenaeum.config import Settings
from athenaeum.library.backend import provision_library

# >= 32 chars: the config.py secret_key validator (SERVER-03) parses this too.
TEST_SECRET_KEY = "athenaeum-test-secret-key-0123456789"


class CsrfTestClient(TestClient):
    """TestClient that attaches the session CSRF token to every POST (CS-8).

    The token lives in the signed session cookie (starlette SessionMiddleware
    payload: ``base64(json).timestamp.signature``), so it is readable without
    the secret key. ``post(..., csrf=False)`` sends a raw, tokenless POST for
    the CSRF rejection tests.
    """

    def csrf_token(self) -> str | None:
        raw = self.cookies.get("session")
        if not raw:
            return None
        payload = raw.split(".")[0]
        payload += "=" * (-len(payload) % 4)
        session = json.loads(base64.urlsafe_b64decode(payload))
        return session.get("csrf_token")

    def post(self, url, *, data=None, csrf=True, **kwargs):
        if csrf:
            if self.csrf_token() is None:
                self.get("/")  # any WebUI GET mints the session CSRF token
            token = self.csrf_token()
            if token is not None:
                data = {**(data or {}), "csrf_token": token}
        return super().post(url, data=data, **kwargs)


@pytest.fixture
def test_settings(tmp_path, monkeypatch) -> Settings:
    """Settings on a tmp data root with a test secret key (env mirrors it)."""
    monkeypatch.setenv("ATHENAEUM_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("ATHENAEUM_SECRET_KEY", TEST_SECRET_KEY)
    return Settings()


@pytest.fixture
def app(test_settings):
    """The real assembled app (SessionMiddleware, routers, mounted /mcp)."""
    return create_app(test_settings)


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    """TestClient with lifespan running (bootstrap + MCP session manager)."""
    with CsrfTestClient(app, follow_redirects=False) as client:
        yield client


@pytest.fixture
def admin_user(test_settings):
    """A provisioned admin user (real DB row + OKF bundle on disk)."""
    db_path = Path(test_settings.data_root) / "app.db"
    db_module.init_db(db_path)
    conn = db_module.connect(db_path)
    try:
        user = db_module.create_user(
            conn,
            "owner",
            security.hash_password("owner-pw"),
            is_admin=True,
        )
        provision_library(test_settings.data_root, user["id"])
        return user
    finally:
        conn.close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def running_app(app) -> Iterator[str]:
    """Serve the assembled app under real uvicorn; yields the base URL.

    Used by the MCP round-trip tests so Streamable HTTP runs over a real
    socket (session ids, SSE framing, lifespan) instead of an ASGI shortcut.
    """
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        raise RuntimeError("uvicorn failed to start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
