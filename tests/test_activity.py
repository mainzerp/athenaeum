"""Tests for the activity journal: ActivityRegistry + ActivityMiddleware.

Runs the real FastMCP server in-memory with the middleware registered
(both create_mcp_server activity params given), identity injected via
monkeypatch on athenaeum.identity.get_current_identity (the in-process
transport has no HTTP request for the bearer middleware to resolve).
Self-contained: local fakes mirror the test_mcp_tools.py stand-ins.
"""

import asyncio
import base64
import json
import sqlite3
from datetime import UTC, datetime

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import MiddlewareContext

import athenaeum.identity as identity_module
import athenaeum.mcp_server as mcp_server
from athenaeum import db as db_module
from athenaeum.activity import MAX_ARGS, ActivityMiddleware, ActivityRegistry, _sanitize_arguments
from athenaeum.librarian.llm import LLMResponse, ToolCall
from athenaeum.librarian.manager import LibrarianManager
from athenaeum.mcp_server import SeedCache, create_mcp_server, sqlite_token_lookup


class FakeBackend:
    def __init__(self, healthy: bool = True):
        self.docs: dict[str, dict] = {}
        self.healthy = healthy

    def list_dir(self, path: str = "/") -> list[dict]:
        return []

    def read_document(self, path: str) -> dict:
        doc = self.docs[path]
        return {"path": path, "frontmatter": doc["frontmatter"], "body": doc["body"]}

    def search_metadata(self, field=None, value=None) -> list[dict]:
        return []

    def create_concept(
        self,
        path,
        frontmatter,
        body,
        *,
        agent_label=None,
        requested_by=None,
        via=None,
        allow_literal_escapes=False,
    ) -> dict:
        self.docs[path] = {"frontmatter": dict(frontmatter), "body": body}
        return {"id": path[: -len(".md")], "action": "created"}

    def link_check(self, path=None) -> list[dict]:
        return []

    def write_asset(self, filename: str, data: bytes) -> str:
        return f"/.athenaeum/assets/fake-{filename}"

    def status(self) -> dict:
        return {
            "stats": {"concepts": len(self.docs), "directories": 1},
            "health": {"orphans": [], "broken_links": [], "warnings": 0, "errors": 0},
            "healthy": self.healthy,
        }

    def link_health(self, paths: list[str]) -> dict:
        report = {}
        for raw in paths:
            bundle = raw if raw.startswith("/") else "/" + raw
            report[bundle] = {"inbound": 0, "outbound": 0}
        return report


class ScriptedProvider:
    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)

    async def complete(self, messages, tools, config) -> LLMResponse:
        if not self.responses:
            return LLMResponse(text="(script exhausted)")
        return self.responses.pop(0)


def make_db(tmp_path, *, activity_keep: int = 0):
    """Real canonical schema + users (user-a configured, user-c without LLM)."""
    db_path = tmp_path / "app.db"
    db_module.init_db(db_path)
    conn = db_module.connect(db_path)
    try:
        for user_id, username in (("user-a", "alice"), ("user-c", "cid")):
            conn.execute(
                "INSERT INTO users (id, username, password_hash, created_at) "
                "VALUES (?, ?, 'hash', ?)",
                (user_id, username, db_module.utcnow()),
            )
        conn.execute(
            "INSERT INTO librarian_configs "
            "(user_id, llm_model, activity_keep) VALUES ('user-a', 'm', ?)",
            (activity_keep,),
        )
        conn.execute(
            "INSERT INTO provider_configs "
            "(id, user_id, label, provider, api_key_enc, is_default, created_at) "
            "VALUES ('conn-a', 'user-a', 'Default', 'openai', 'k', 1, ?)",
            (db_module.utcnow(),),
        )
        conn.execute("INSERT INTO librarian_configs (user_id) VALUES ('user-c')")
        conn.commit()
    finally:
        conn.close()
    return db_path


def make_stack(tmp_path, scripts=None, *, activity_keep: int = 0):
    db_path = make_db(tmp_path, activity_keep=activity_keep)
    backends = {"user-a": FakeBackend(), "user-c": FakeBackend()}
    providers = {"user-a": ScriptedProvider(scripts or [LLMResponse(text="ok")])}
    manager = LibrarianManager(
        db_path,
        tmp_path / "data",
        backend_factory=lambda user_id, root, config: backends[user_id],
        provider_factory=lambda user_id, llm: providers[user_id],
    )
    registry = ActivityRegistry()
    server = create_mcp_server(
        manager,
        token_lookup=sqlite_token_lookup(db_path),
        seed_cache=SeedCache(seed_generator=lambda backend: "SEED"),
        activity_db_path=db_path,
        activity_registry=registry,
    )
    return server, manager, backends, providers, registry, db_path


def journal_rows(db_path, user_id: str = "user-a"):
    conn = db_module.connect(db_path)
    try:
        return db_module.list_activity(conn, user_id, limit=50)
    finally:
        conn.close()


def set_identity(monkeypatch, identity):
    # The middleware resolves identity via athenaeum.identity; the MCP tool
    # handlers via their own module-global re-export in mcp_server.
    monkeypatch.setattr(identity_module, "get_current_identity", lambda: identity)
    monkeypatch.setattr(mcp_server, "get_current_identity", lambda: identity)


# --- registry unit behavior ----------------------------------------------------


def test_registry_add_remove_snapshot():
    registry = ActivityRegistry()
    entry = {
        "trace_id": "t-1",
        "user_id": "user-a",
        "token_label": "agent-a",
        "tool": "request_knowledge",
        "arguments": "{}",
        "started_at": datetime.now(UTC).isoformat(),
    }
    registry.add(entry)
    snapshot = registry.snapshot()
    assert snapshot == [entry]
    snapshot.append({"trace_id": "mutated"})  # snapshot iterates a copy
    assert len(registry.snapshot()) == 1
    registry.remove("t-1")
    registry.remove("t-1")  # idempotent
    assert registry.snapshot() == []


# --- middleware journaling ------------------------------------------------------


async def test_run_computation_journal_has_no_credentials(tmp_path, monkeypatch):
    """Threat-model pin: journaled arguments are references (concept id,
    connection id, parameters) — never the connection's stored credential."""
    server, manager, backends, providers, registry, db_path = make_stack(tmp_path)
    target = tmp_path / "target.db"
    conn = sqlite3.connect(target)
    conn.execute("CREATE TABLE items (name TEXT)")
    conn.execute("INSERT INTO items VALUES ('a')")
    conn.commit()
    conn.close()
    with db_module.connect(db_path) as conn:
        row = db_module.create_runtime_connection(
            conn, label="L", runtime="sqlite", dbname=str(target), password_enc="supersecret-pw"
        )
        db_module.set_app_setting(conn, "computation_execution_enabled", "1")
    backends["user-a"].docs["/q.md"] = {
        "frontmatter": {"title": "Q", "type": "Attested Computation", "runtime": "sqlite"},
        "body": "# Computation\n\n```sql\nSELECT name FROM items\n```\n",
    }
    set_identity(monkeypatch, ("user-a", "agent-a"))
    async with Client(server) as client:
        await client.call_tool("run_computation", {"concept_id": "/q", "connection_id": row["id"]})
    rows = journal_rows(db_path)
    assert len(rows) == 1
    arguments = rows[0]["arguments"]
    assert "/q" in arguments
    assert row["id"] in arguments
    assert "supersecret-pw" not in arguments
    assert rows[0]["outcome"] == "ok"


async def test_middleware_journals_successful_call(tmp_path, monkeypatch):
    server, *_rest, db_path = make_stack(tmp_path)
    set_identity(monkeypatch, ("user-a", "agent-a"))
    async with Client(server) as client:
        await client.call_tool("request_knowledge", {"query": "hi"})

    rows = journal_rows(db_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["tool"] == "request_knowledge"
    assert row["user_id"] == "user-a"
    assert row["token_label"] == "agent-a"
    assert row["outcome"] == "ok"
    assert row["error"] is None
    assert row["duration_ms"] is not None and row["duration_ms"] >= 0
    assert row["trace_id"]
    assert row["iterations"] == 0
    assert '"hi"' in row["arguments"]


async def test_middleware_journals_error_call(tmp_path, monkeypatch):
    server, *_rest, db_path = make_stack(tmp_path)
    set_identity(monkeypatch, ("user-c", "agent-c"))  # no LLM configured
    async with Client(server) as client:
        with pytest.raises(ToolError, match="not configured"):
            await client.call_tool("request_knowledge", {"query": "q"})

    rows = journal_rows(db_path, "user-c")
    assert len(rows) == 1
    assert rows[0]["tool"] == "request_knowledge"
    assert rows[0]["outcome"] == "error"
    assert "not configured" in rows[0]["error"]


async def test_middleware_journals_cancelled_call(tmp_path, monkeypatch):
    """A CancelledError out of the handler journals outcome 'cancelled', never 'ok'."""
    db_path = make_db(tmp_path)
    registry = ActivityRegistry()
    middleware = ActivityMiddleware(db_path, registry)
    set_identity(monkeypatch, ("user-a", "agent-a"))
    context = MiddlewareContext(
        message=_FakeCallToolParams("request_knowledge", {"query": "q"}),
        timestamp=datetime.now(UTC),
    )

    async def call_next(ctx):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await middleware.on_call_tool(context, call_next)

    rows = journal_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "cancelled"
    assert registry.snapshot() == []  # removed after the call


async def test_middleware_skips_journal_without_identity(tmp_path, monkeypatch):
    server, *_rest, db_path = make_stack(tmp_path)
    set_identity(monkeypatch, None)  # in-process unauthenticated
    async with Client(server) as client:
        with pytest.raises(ToolError, match="Authentication required"):
            await client.call_tool("request_knowledge", {"query": "q"})

    conn = db_module.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM activity").fetchone()["n"]
    finally:
        conn.close()
    assert count == 0


async def test_middleware_prunes_to_activity_keep(tmp_path, monkeypatch):
    server, *_rest, db_path = make_stack(tmp_path, activity_keep=2)
    set_identity(monkeypatch, ("user-a", "agent-a"))
    async with Client(server) as client:
        for _ in range(3):
            await client.call_tool("library_status", {})

    rows = journal_rows(db_path)
    assert len(rows) == 2
    assert {row["tool"] for row in rows} == {"library_status"}


async def test_in_flight_visible_during_call_and_gone_after(tmp_path, monkeypatch):
    server, manager, backends, providers, registry, db_path = make_stack(tmp_path)
    seen: list[list[dict]] = []

    class SpyProvider:
        async def complete(self, messages, tools, config):
            seen.append(registry.snapshot())
            return LLMResponse(text="ok")

    providers["user-a"] = SpyProvider()  # factory lambda reads the dict lazily
    set_identity(monkeypatch, ("user-a", "agent-a"))
    async with Client(server) as client:
        await client.call_tool("request_knowledge", {"query": "hi"})

    assert len(seen) == 1 and len(seen[0]) == 1  # visible in flight
    assert seen[0][0]["tool"] == "request_knowledge"
    assert seen[0][0]["user_id"] == "user-a"
    assert registry.snapshot() == []  # removed after the call


async def test_library_status_also_journaled(tmp_path, monkeypatch):
    server, *_rest, db_path = make_stack(tmp_path)
    set_identity(monkeypatch, ("user-a", "agent-a"))
    async with Client(server) as client:
        await client.call_tool("library_status", {})
    assert [row["tool"] for row in journal_rows(db_path)] == ["library_status"]


async def test_registry_free_server_has_no_middleware(tmp_path, monkeypatch):
    """create_mcp_server without both activity params works unchanged."""
    db_path = make_db(tmp_path)
    backends = {"user-a": FakeBackend()}
    manager = LibrarianManager(
        db_path,
        tmp_path / "data",
        backend_factory=lambda user_id, root, config: backends[user_id],
        provider_factory=lambda user_id, llm: ScriptedProvider([LLMResponse(text="ok")]),
    )
    server = create_mcp_server(manager, token_lookup=sqlite_token_lookup(db_path))
    set_identity(monkeypatch, ("user-a", "agent-a"))
    async with Client(server) as client:
        result = await client.call_tool("request_knowledge", {"query": "hi"})
    assert result.data["answer"] == "ok"


# --- middleware hook unit behavior ----------------------------------------------


class _FakeCallToolParams:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


async def test_middleware_truncates_long_arguments():
    registry = ActivityRegistry()
    middleware = ActivityMiddleware("unused.db", registry)
    context = MiddlewareContext(
        message=_FakeCallToolParams("request_knowledge", {"query": "x" * (MAX_ARGS * 2)}),
        timestamp=datetime.now(UTC),
    )
    seen: list[dict] = []

    async def call_next(ctx):
        seen.extend(registry.snapshot())
        return "ok"

    assert await middleware.on_call_tool(context, call_next) == "ok"
    assert len(seen) == 1
    arguments = seen[0]["arguments"]
    assert len(arguments) <= MAX_ARGS + len("…[truncated]")
    assert arguments.endswith("…[truncated]")
    assert registry.snapshot() == []  # removed after the call


# --- argument sanitization (R20: journal carries refs, never base64) -----------


def test_sanitize_arguments_replaces_image_base64():
    args = {
        "content": "c",
        "images": [
            {"filename": "a.png", "media_type": "image/png", "data_base64": "QUJD"},
            {"filename": "b.png"},  # tolerated: no data_base64
        ],
    }
    out = _sanitize_arguments("store_knowledge", args)
    ref = out["images"][0]["data_base64"]
    assert ref.startswith("sha256:")
    assert ref.endswith("(4 chars)")
    assert "QUJD" not in json.dumps(out)
    assert out["images"][1] == {"filename": "b.png"}
    # other tools and image-less arguments pass through untouched
    assert _sanitize_arguments("request_knowledge", args) is args
    assert _sanitize_arguments("store_knowledge", {"content": "c"}) == {"content": "c"}


async def test_middleware_journals_image_refs_not_base64(tmp_path, monkeypatch):
    scripts = [
        LLMResponse(
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="write_concept",
                    arguments={
                        "path": "/n.md",
                        "frontmatter": {"title": "N", "type": "Note"},
                        "body": "b",
                    },
                )
            ]
        ),
        LLMResponse(text="stored"),
    ]
    server, *_rest, db_path = make_stack(tmp_path, scripts)
    set_identity(monkeypatch, ("user-a", "agent-a"))
    data_b64 = base64.b64encode(b"\x89PNG fake").decode("ascii")
    async with Client(server) as client:
        await client.call_tool(
            "store_knowledge",
            {
                "content": "with image",
                "images": [
                    {"filename": "d.png", "media_type": "image/png", "data_base64": data_b64}
                ],
            },
        )
    rows = journal_rows(db_path)
    assert len(rows) == 1
    arguments = rows[0]["arguments"]
    assert data_b64 not in arguments
    assert "sha256:" in arguments
    assert f"({len(data_b64)} chars)" in arguments
