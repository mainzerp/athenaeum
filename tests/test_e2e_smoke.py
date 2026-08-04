"""End-to-end smoke tests against the real app assembly (plan Step 4).

Exercises the full stack: FastAPI app with SessionMiddleware, WebUI routers,
mounted FastMCP Streamable HTTP endpoint, real sqlite DB and real OKF bundle
on a tmp data root. MCP tests run against a real uvicorn server (running_app
fixture) so auth headers, session ids, and SSE framing are exercised for real.
No LLM HTTP is needed: agent-backed tools run against an in-process scripted
provider injected via monkeypatch on ``athenaeum.librarian.manager.create_provider``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import McpError

from athenaeum import db as db_module
from athenaeum import security
from athenaeum.app import create_app
from athenaeum.config import get_settings
from athenaeum.librarian.llm import LLMResponse, ToolCall
from athenaeum.library.backend import LibraryBackend
from conftest import CsrfTestClient

ALL_TOOLS = {
    "request_knowledge",
    "store_knowledge",
    "update_knowledge",
    "library_status",
    "library_maintain",
    "library_curate",
    "run_computation",
}


def _mcp_client(base_url: str, token: str | None = None) -> Client:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return Client(StreamableHttpTransport(f"{base_url}/mcp", headers=headers))


def _create_token(data_root: str, user_id: str, label: str = "e2e-agent") -> str:
    """Persist a real MCP token row; returns the plaintext bearer token."""
    plaintext, token_hash = security.generate_token()
    conn = db_module.connect(Path(data_root) / "app.db")
    try:
        db_module.create_token(conn, user_id, label, token_hash)
    finally:
        conn.close()
    return plaintext


class ScriptedProvider:
    """Returns a fixed queue of LLMResponses; records every complete() call."""

    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.calls: list[tuple] = []

    async def complete(self, messages, tools, config) -> LLMResponse:
        self.calls.append((list(messages), list(tools), config))
        if not self.responses:
            return LLMResponse(text="(script exhausted)")
        return self.responses.pop(0)


# --- health ------------------------------------------------------------------


def test_import_hygiene_no_cycles():
    """A7: the mcp_server <-> activity import cycle and the db -> library
    inversion are gone — checked in fresh interpreters (import order)."""
    activity_check = (
        "import sys, athenaeum.activity;sys.exit('athenaeum.mcp_server' in sys.modules)"
    )
    db_check = (
        "import sys, athenaeum.db;"
        "sys.exit(any(m.startswith('athenaeum.library') for m in sys.modules))"
    )
    for code in (activity_check, db_check):
        result = subprocess.run([sys.executable, "-c", code], capture_output=True)
        assert result.returncode == 0, result.stderr.decode()


def test_healthz_unauthenticated(client):
    """/healthz answers 200 with no session (Docker HEALTHCHECK contract)."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_unknown_webui_path_is_plain_404_not_jsonrpc(client):
    """A17: unmatched WebUI paths get a plain 404; the catch-all mount is
    bounded to /mcp, so WebUI typos never reach the FastMCP app."""
    response = client.get("/no/such/page")
    assert response.status_code == 404
    assert "jsonrpc" not in response.text.lower()
    response = client.post("/no/such/page")
    assert response.status_code == 404
    assert "jsonrpc" not in response.text.lower()


# --- first-run bootstrap ------------------------------------------------------


def test_first_run_setup_page(client):
    """With an empty users table the setup page creates the owner account."""
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"

    response = client.post(
        "/setup",
        data={"username": "owner", "password": "owner-password-1", "confirm": "owner-password-1"},
    )
    assert response.status_code == 303
    # the new session is logged in; the real bundle is browsable
    assert client.get("/library/tree").status_code == 200


def test_bootstrap_admin_via_env(tmp_path, monkeypatch):
    """ATHENAEUM_BOOTSTRAP_ADMIN_* pre-seeds the owner at startup (plan §3.6)."""
    monkeypatch.setenv("ATHENAEUM_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("ATHENAEUM_SECRET_KEY", "bootstrap-secret")
    monkeypatch.setenv("ATHENAEUM_BOOTSTRAP_ADMIN_USERNAME", "owner")
    monkeypatch.setenv("ATHENAEUM_BOOTSTRAP_ADMIN_PASSWORD", "owner-pw")

    app = create_app(get_settings())
    with CsrfTestClient(app, follow_redirects=False) as client:
        # a user already exists: setup is closed, login works with the env creds
        response = client.get("/setup")
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        response = client.post("/login", data={"username": "owner", "password": "owner-pw"})
        assert response.status_code == 303
        assert client.get("/library/tree").status_code == 200


# --- MCP endpoint -------------------------------------------------------------


async def test_mcp_auth_reject_missing_token(running_app):
    with pytest.raises(McpError, match="Missing bearer token"):
        async with _mcp_client(running_app):
            pass


async def test_mcp_auth_reject_unknown_token(running_app, admin_user):
    with pytest.raises(McpError, match="Invalid or revoked bearer token"):
        async with _mcp_client(running_app, token="not-a-real-token"):
            pass


async def test_mcp_auth_reject_revoked_token(running_app, admin_user, test_settings):
    plaintext = _create_token(test_settings.data_root, admin_user["id"])
    conn = db_module.connect(Path(test_settings.data_root) / "app.db")
    try:
        token_row = db_module.lookup_token(conn, security.hash_token(plaintext))
        assert db_module.revoke_token(conn, admin_user["id"], token_row["id"])
    finally:
        conn.close()
    with pytest.raises(McpError, match="Invalid or revoked bearer token"):
        async with _mcp_client(running_app, token=plaintext):
            pass


async def test_mcp_roundtrip_with_real_token(running_app, admin_user, test_settings):
    """Bearer token -> real user; all 7 tools; status works unconfigured."""
    plaintext = _create_token(test_settings.data_root, admin_user["id"])

    async with _mcp_client(running_app, token=plaintext) as client:
        tools = await client.list_tools()
        assert {tool.name for tool in tools} == ALL_TOOLS

        # library_status runs without any LLM configuration (read-only)
        result = await client.call_tool("library_status", {})
        status = result.data
        assert status["healthy"] is True
        assert status["stats"]["concepts"] == 0

        # the token's last_used_at was touched by the auth middleware
    conn = db_module.connect(Path(test_settings.data_root) / "app.db")
    try:
        row = db_module.lookup_token(conn, security.hash_token(plaintext))
        assert row["last_used_at"] is not None
    finally:
        conn.close()


async def test_mcp_activity_journal_row(running_app, admin_user, test_settings):
    """A real call_tool over HTTP lands in the persisted activity journal."""
    plaintext = _create_token(test_settings.data_root, admin_user["id"], label="journal-agent")

    async with _mcp_client(running_app, token=plaintext) as client:
        await client.call_tool("library_status", {})

    conn = db_module.connect(Path(test_settings.data_root) / "app.db")
    try:
        rows = db_module.list_activity(conn, admin_user["id"])
    finally:
        conn.close()
    assert len(rows) == 1
    row = rows[0]
    assert row["tool"] == "library_status"
    assert row["token_label"] == "journal-agent"
    assert row["outcome"] == "ok"
    assert row["trace_id"]
    assert row["duration_ms"] is not None


async def test_mcp_seed_in_tool_descriptions(running_app, admin_user, test_settings):
    """Per-user seed ships in the seeded tools' descriptions (plan §3.1a)."""
    plaintext = _create_token(test_settings.data_root, admin_user["id"])

    async with _mcp_client(running_app, token=plaintext) as client:
        tools = await client.list_tools()
    by_name = {tool.name: tool for tool in tools}
    for name in ("request_knowledge", "store_knowledge"):
        description = by_name[name].description or ""
        assert "Current library seed:" in description
    # non-seeded tools keep their plain descriptions
    assert "Current library seed:" not in (by_name["library_status"].description or "")


async def test_mcp_agent_tools_error_when_unconfigured(running_app, admin_user, test_settings):
    """Agent-backed tools give a clear error while no LLM is configured."""
    plaintext = _create_token(test_settings.data_root, admin_user["id"])

    async with _mcp_client(running_app, token=plaintext) as client:
        result = await client.call_tool(
            "request_knowledge", {"query": "anything"}, raise_on_error=False
        )
        assert result.is_error
        assert "not configured" in result.content[0].text


async def test_e2e_store_status_rollback_chain(running_app, admin_user, test_settings, monkeypatch):
    """Full plan Step 4 chain: stub provider -> disk artifacts -> status -> rollback."""
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="write_concept",
                        arguments={
                            "path": "/new.md",
                            "frontmatter": {"title": "New", "type": "Note"},
                            "body": "b",
                        },
                    )
                ]
            ),
            LLMResponse(text="stored"),
        ]
    )
    # route the real stack's provider resolution to the scripted provider
    monkeypatch.setattr("athenaeum.librarian.manager.create_provider", lambda llm: provider)
    conn = db_module.connect(Path(test_settings.data_root) / "app.db")
    try:
        db_module.create_provider_config(
            conn,
            admin_user["id"],
            label="Default",
            provider="openai",
            api_key_enc=security.encrypt_secret("stub-key", test_settings.secret_key),
            max_iterations=5,
        )
        db_module.update_librarian_config(
            conn, admin_user["id"], connection_id=None, model="stub", prompt_addendum=None
        )
    finally:
        conn.close()

    token = _create_token(test_settings.data_root, admin_user["id"])
    async with _mcp_client(running_app, token=token) as client:
        result = await client.call_tool("store_knowledge", {"content": "fresh"})
        assert result.data["stored"] == [{"id": "/new", "title": "New", "action": "created"}]

        # disk artifacts: concept + index + log + snapshot
        root = Path(test_settings.data_root) / "users" / admin_user["id"] / "library"
        assert (root / "new.md").is_file()
        assert "new.md" in (root / "index.md").read_text(encoding="utf-8")
        assert "**Creation**" in (root / "log.md").read_text(encoding="utf-8")
        assert (root / ".athenaeum" / "versions" / "000001" / "meta.json").exists()

        # status delta over the same MCP client; the new concept has no
        # inbound/outbound links, so it is an orphan (D1 contract, end to end)
        status = (await client.call_tool("library_status", {})).data
        assert status["stats"]["concepts"] == 1
        assert status["healthy"] is False
        assert status["health"]["orphans"] == [{"id": "/new", "title": "New"}]

    # rollback the creation version: the concept file disappears again
    backend = LibraryBackend(root, actor="e2e-rollback")
    backend.rollback(1)
    assert not (root / "new.md").exists()
    assert "Rolled back to version 000001" in (root / "log.md").read_text(encoding="utf-8")


def test_library_export_import_round_trip(client, admin_user, test_settings):
    """Export -> destroy -> import restores the whole bundle end to end."""
    response = client.post("/login", data={"username": "owner", "password": "owner-pw"})
    assert response.status_code == 303
    root = Path(test_settings.data_root) / "users" / admin_user["id"] / "library"
    backend = LibraryBackend(root, actor="e2e-roundtrip")
    backend.create_concept("/roundtrip.md", {"type": "Note", "title": "RT"}, "body\n")

    response = client.get("/library/export")
    assert response.status_code == 200
    archive = response.content

    (root / "roundtrip.md").unlink()
    response = client.post(
        "/library/import",
        files={"file": ("lib.zip", archive, "application/zip")},
    )
    assert response.status_code == 303
    assert (root / "roundtrip.md").is_file()
    assert (root.parent / "import-backup.zip").is_file()

    page = client.get("/config/library")
    assert page.status_code == 200
    assert "/library/export" in page.text  # the Backup card renders
