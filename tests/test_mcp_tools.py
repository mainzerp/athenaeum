"""Tests for the MCP server: bearer auth, tool routing, scoping, seed injection.

Runs the real FastMCP server in-memory (fastmcp.Client) with a per-user
in-memory LibraryBackend stand-in and scripted LLM providers. The bearer
middleware is additionally unit-tested against synthetic HTTP requests.
"""

import asyncio
import hashlib
import json
import re
import sqlite3

import pytest
from fastmcp import Client
from fastmcp.exceptions import McpError, ToolError
from fastmcp.server.middleware import MiddlewareContext
from starlette.requests import Request

import athenaeum.mcp_server as mcp_server
from athenaeum import db as db_module
from athenaeum.librarian.llm import LLMResponse, ToolCall
from athenaeum.librarian.manager import LibrarianManager
from athenaeum.librarian.tools import dispatch
from athenaeum.librarian.tracing import current_trace
from athenaeum.library import organize as organize_mod
from athenaeum.mcp_server import (
    BearerAuthMiddleware,
    SeedCache,
    create_mcp_server,
    sqlite_token_lookup,
)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class FakeBackend:
    """In-memory LibraryBackend stand-in; seed_marker changes on writes."""

    def __init__(self, healthy: bool = True):
        self.docs: dict[str, dict] = {}
        self.healthy = healthy
        self.seed_marker = 0
        self.calls: list[tuple] = []
        # Real dir the A10 scan pass-throughs delegate to (scheduler tests
        # point it at the user's on-disk library root).
        self.scan_root = None

    def organization_findings(self, *, since=None) -> dict:
        return organize_mod.organization_findings(self.scan_root, since=since)

    def findings_empty(self, report: dict) -> bool:
        return organize_mod.findings_empty(report)

    def list_dir(self, path: str = "/") -> list[dict]:
        self.calls.append(("list_dir", path))
        return [
            {
                "name": path.rsplit("/", 1)[-1],
                "path": path,
                "is_directory": False,
                "title": doc["frontmatter"].get("title"),
                "type": doc["frontmatter"].get("type"),
                "description": doc["frontmatter"].get("description"),
            }
            for path, doc in self.docs.items()
        ]

    def read_document(self, path: str) -> dict:
        self.calls.append(("read_document", path))
        doc = self.docs[path]
        return {"path": path, "frontmatter": doc["frontmatter"], "body": doc["body"]}

    def search_metadata(self, field=None, value=None) -> list[dict]:
        self.calls.append(("search_metadata", field, value))
        return []

    def create_concept(self, path, frontmatter, body, *, agent_label=None) -> dict:
        self.calls.append(("create_concept", path, agent_label))
        self.docs[path] = {"frontmatter": dict(frontmatter), "body": body}
        self.seed_marker += 1  # any write invalidates the seed
        return {"id": path[: -len(".md")], "action": "created"}

    def edit_concept(
        self, path, *, frontmatter_patch=None, remove_keys=None, new_body=None, agent_label=None
    ) -> dict:
        self.calls.append(("edit_concept", path, agent_label))
        self.docs[path]["frontmatter"].update(frontmatter_patch or {})
        if new_body is not None:
            self.docs[path]["body"] = new_body
        self.seed_marker += 1
        return {"id": path[: -len(".md")], "action": "updated"}

    def move_concept(self, old_path, new_path, *, agent_label=None) -> dict:
        self.calls.append(("move_concept", old_path, new_path, agent_label))
        self.docs[new_path] = self.docs.pop(old_path)
        self.seed_marker += 1
        return {"id": new_path[: -len(".md")], "action": "moved", "links_rewritten": 0}

    def deprecate_concept(self, path, *, agent_label=None) -> dict:
        self.calls.append(("deprecate_concept", path, agent_label))
        self.docs[path]["frontmatter"]["status"] = "deprecated"
        self.seed_marker += 1
        return {"id": path[: -len(".md")], "action": "deprecated"}

    def delete_concept(self, path, *, agent_label=None) -> dict:
        self.calls.append(("delete_concept", path, agent_label))
        del self.docs[path]
        self.seed_marker += 1
        return {"id": path[: -len(".md")], "action": "deleted", "inbound_links": []}

    def link_check(self, path=None) -> list[dict]:
        self.calls.append(("link_check", path))
        return []

    def status(self) -> dict:
        self.calls.append(("status",))
        return {
            "stats": {
                "concepts": len(self.docs),
                "directories": 1,
                "versions": 0,
                "last_write": None,
            },
            "health": {"orphans": [], "broken_links": [], "warnings": 0, "errors": 0},
            "healthy": self.healthy,
        }

    def link_health(self, paths: list[str]) -> dict:
        self.calls.append(("link_health", tuple(paths)))
        link_re = re.compile(r"\]\((/[^)\s]+)\)")
        outbound = {p: set(link_re.findall(doc["body"])) for p, doc in self.docs.items()}
        report = {}
        for raw in paths:
            bundle = raw if raw.startswith("/") else "/" + raw
            report[bundle] = {
                "inbound": sum(1 for targets in outbound.values() if bundle in targets),
                "outbound": len(outbound.get(bundle, ())),
            }
        return report


class ScriptedProvider:
    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.calls: list[tuple] = []

    async def complete(self, messages, tools, config) -> LLMResponse:
        self.calls.append((list(messages), list(tools), config))
        if not self.responses:
            return LLMResponse(text="(script exhausted)")
        return self.responses.pop(0)


def make_db(tmp_path) -> str:
    db_path = tmp_path / "app.db"
    db_module.init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        for user_id, username in [("user-a", "alice"), ("user-b", "bob"), ("user-c", "cid")]:
            conn.execute(
                "INSERT INTO users (id, username, password_hash, created_at) "
                "VALUES (?, ?, 'hash', '2026-01-01T00:00:00Z')",
                (user_id, username),
            )
        # user-a and user-b configured; user-c has no config row (unconfigured)
        for user_id in ("user-a", "user-b"):
            conn.execute(
                "INSERT INTO librarian_configs (user_id, llm_model) VALUES (?, 'm')",
                (user_id,),
            )
            conn.execute(
                "INSERT INTO provider_configs "
                "(id, user_id, label, provider, api_key_enc, is_default, created_at) "
                "VALUES (?, ?, 'Default', 'openai', 'k', 1, '2026-01-01T00:00:00Z')",
                (f"conn-{user_id}", user_id),
            )
        tokens = [
            ("tok-1", "user-a", "agent-a", "token-a", None),
            ("tok-2", "user-b", "agent-b", "token-b", None),
            ("tok-3", "user-a", "agent-old", "token-revoked", "2026-06-01T00:00:00Z"),
        ]
        for token_id, user_id, label, token, revoked in tokens:
            conn.execute(
                "INSERT INTO mcp_tokens "
                "(id, user_id, label, token_hash, created_at, revoked_at) "
                "VALUES (?, ?, ?, ?, '2026-01-01T00:00:00Z', ?)",
                (token_id, user_id, label, _hash(token), revoked),
            )
    return str(db_path)


def make_stack(
    tmp_path,
    provider_scripts: dict[str, list[LLMResponse]] | None = None,
    *,
    seed_generator=None,
):
    """Assemble db + manager + server with per-user fakes."""
    db_path = make_db(tmp_path)
    backends = {"user-a": FakeBackend(), "user-b": FakeBackend(), "user-c": FakeBackend()}
    providers = {
        user_id: ScriptedProvider(scripts) for user_id, scripts in (provider_scripts or {}).items()
    }

    def backend_factory(user_id, root, config):
        backend = backends[user_id]
        # A10: the curate scans run through the backend; delegate them to the
        # user's real on-disk library root (mirrors the old self.root scan).
        backend.scan_root = root
        return backend

    manager = LibrarianManager(
        db_path,
        tmp_path / "data",
        backend_factory=backend_factory,
        provider_factory=lambda user_id, llm: providers.setdefault(user_id, ScriptedProvider([])),
    )
    generator = seed_generator or (lambda backend: f"SEED<{backend.seed_marker}>")
    seeds = SeedCache(seed_generator=generator)
    server = create_mcp_server(manager, token_lookup=sqlite_token_lookup(db_path), seed_cache=seeds)
    return server, manager, backends, providers, seeds


def set_identity(monkeypatch, user_id: str, label: str):
    monkeypatch.setattr(mcp_server, "get_current_identity", lambda: (user_id, label))


def make_http_request(token: str | None) -> Request:
    headers = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode("latin-1")))
    return Request({"type": "http", "method": "POST", "headers": headers})


# --- bearer auth (unit level) ----------------------------------------------


def test_resolve_user_id_valid_token(tmp_path):
    middleware = BearerAuthMiddleware(sqlite_token_lookup(make_db(tmp_path)))
    assert middleware.resolve_user_id(make_http_request("token-a")) == (
        "user-a",
        "agent-a",
    )


def test_resolve_user_id_missing_token(tmp_path):
    middleware = BearerAuthMiddleware(sqlite_token_lookup(make_db(tmp_path)))
    with pytest.raises(McpError, match="Missing bearer token"):
        middleware.resolve_user_id(make_http_request(None))


def test_resolve_user_id_unknown_token(tmp_path):
    middleware = BearerAuthMiddleware(sqlite_token_lookup(make_db(tmp_path)))
    with pytest.raises(McpError, match="Invalid or revoked"):
        middleware.resolve_user_id(make_http_request("no-such-token"))


def test_resolve_user_id_revoked_token(tmp_path):
    middleware = BearerAuthMiddleware(sqlite_token_lookup(make_db(tmp_path)))
    with pytest.raises(McpError, match="Invalid or revoked"):
        middleware.resolve_user_id(make_http_request("token-revoked"))


def test_valid_token_updates_last_used(tmp_path):
    db_path = make_db(tmp_path)
    middleware = BearerAuthMiddleware(sqlite_token_lookup(db_path))
    middleware.resolve_user_id(make_http_request("token-b"))
    with sqlite3.connect(db_path) as conn:
        last_used = conn.execute(
            "SELECT last_used_at FROM mcp_tokens WHERE id = 'tok-2'"
        ).fetchone()[0]
    assert last_used is not None


def test_last_used_write_coalesced_per_token(tmp_path):
    """A18: repeated lookups within the interval skip the UPDATE; a lookup
    past the interval writes again."""
    db_path = make_db(tmp_path)
    now = [1000.0]
    lookup = sqlite_token_lookup(db_path, write_interval=300.0, clock=lambda: now[0])
    token_hash = _hash("token-b")

    def last_used():
        with sqlite3.connect(db_path) as conn:
            return conn.execute(
                "SELECT last_used_at FROM mcp_tokens WHERE id = 'tok-2'"
            ).fetchone()[0]

    assert lookup(token_hash) == ("user-b", "agent-b")
    assert last_used() is not None
    # sentinel proves whether a later lookup actually wrote
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE mcp_tokens SET last_used_at = 'SENTINEL' WHERE id = 'tok-2'")
    # immediate repeat lookups (pings, tools/list, notifications) do not write
    lookup(token_hash)
    lookup(token_hash)
    assert last_used() == "SENTINEL"
    # past the interval the timestamp is refreshed
    now[0] += 301.0
    lookup(token_hash)
    refreshed = last_used()
    assert refreshed is not None and refreshed != "SENTINEL"


async def test_auth_middleware_sets_and_resets_identity(tmp_path, monkeypatch):
    middleware = BearerAuthMiddleware(sqlite_token_lookup(make_db(tmp_path)))
    monkeypatch.setattr(mcp_server, "get_http_request", lambda: make_http_request("token-a"))
    seen = {}

    async def call_next(context):
        seen["identity"] = mcp_server.get_current_identity()
        return "ok"

    result = await middleware.on_message(MiddlewareContext(message={}), call_next)
    assert result == "ok"
    assert seen["identity"] == ("user-a", "agent-a")
    assert mcp_server.get_current_identity() is None  # reset after the request


# --- tool routing / scoping (in-memory client) ------------------------------


async def test_unauthenticated_call_rejected(tmp_path):
    server, *_ = make_stack(tmp_path)
    async with Client(server) as client:
        with pytest.raises(ToolError, match="Authentication required"):
            await client.call_tool("request_knowledge", {"query": "hi"})


async def test_valid_identity_routes_to_correct_librarian(tmp_path, monkeypatch):
    server, manager, backends, providers, _ = make_stack(
        tmp_path, {"user-a": [LLMResponse(text="answer for A")]}
    )
    providers["user-b"] = ScriptedProvider([])
    set_identity(monkeypatch, "user-a", "agent-a")
    async with Client(server) as client:
        result = await client.call_tool("request_knowledge", {"query": "hi"})
    assert result.data["answer"] == "answer for A"
    assert providers["user-a"].calls
    assert not providers["user-b"].calls


async def test_cross_user_scoping(tmp_path, monkeypatch):
    server, manager, backends, providers, _ = make_stack(tmp_path)
    set_identity(monkeypatch, "user-b", "agent-b")
    async with Client(server) as client:
        result = await client.call_tool("library_status", {})
    assert result.data["healthy"] is True
    assert ("status",) in backends["user-b"].calls
    assert backends["user-a"].calls == []  # user A's backend never touched


async def test_library_status_works_without_llm_config(tmp_path, monkeypatch):
    server, manager, backends, providers, _ = make_stack(tmp_path)
    set_identity(monkeypatch, "user-c", "agent-c")  # user-c: no config row
    async with Client(server) as client:
        result = await client.call_tool("library_status", {})
    data = result.data
    assert set(data["stats"]) == {"concepts", "directories", "versions", "last_write"}
    assert set(data["health"]) == {"orphans", "broken_links", "warnings", "errors"}
    assert data["healthy"] is True


async def test_agent_tools_error_clearly_when_unconfigured(tmp_path, monkeypatch):
    server, *_ = make_stack(tmp_path)
    set_identity(monkeypatch, "user-c", "agent-c")
    async with Client(server) as client:
        for tool, args in [
            ("request_knowledge", {"query": "q"}),
            ("store_knowledge", {"content": "c"}),
            ("update_knowledge", {"instruction": "i"}),
            ("library_maintain", {}),
            ("library_curate", {}),
        ]:
            with pytest.raises(ToolError, match="not configured"):
                await client.call_tool(tool, args)


async def test_library_maintain_noop_when_healthy(tmp_path, monkeypatch):
    server, manager, backends, providers, _ = make_stack(
        tmp_path, {"user-b": [LLMResponse(text="unused")]}
    )
    set_identity(monkeypatch, "user-b", "agent-b")
    async with Client(server) as client:
        result = await client.call_tool("library_maintain", {})
    assert result.data == {
        "actions": [],
        "summary": "Library is healthy; no maintenance needed.",
        "healthy": True,
    }
    assert providers["user-b"].calls == []  # no LLM call on the no-op path


async def test_library_curate_noop_when_well_organized(tmp_path, monkeypatch):
    server, manager, backends, providers, _ = make_stack(
        tmp_path, {"user-b": [LLMResponse(text="unused")]}
    )
    set_identity(monkeypatch, "user-b", "agent-b")
    async with Client(server) as client:
        result = await client.call_tool("library_curate", {})
    assert result.data["actions"] == []
    assert result.data["summary"] == "Library is well-organized; nothing to curate."
    assert result.data["organized"] is True
    assert result.data["findings"]["concepts_scanned"] == 0
    assert result.data["health_after"] == {"healthy": True, "orphans": 0}
    assert providers["user-b"].calls == []  # no LLM call on the no-op path
    # run-end timestamp persisted: no-op runs still update the bookkeeping
    assert manager.curate_last_run_at("user-b") is not None


async def test_update_knowledge_roundtrip(tmp_path, monkeypatch):
    server, manager, backends, providers, _ = make_stack(
        tmp_path,
        {
            "user-a": [
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="edit_concept",
                            arguments={"path": "/a.md", "new_body": "fixed"},
                        )
                    ]
                ),
                LLMResponse(text="updated"),
            ]
        },
    )
    backends["user-a"].docs["/a.md"] = {
        "frontmatter": {"title": "A", "type": "Note"},
        "body": "old",
    }
    set_identity(monkeypatch, "user-a", "agent-a")
    async with Client(server) as client:
        result = await client.call_tool("update_knowledge", {"instruction": "fix a"})
    assert result.data == {
        "stored": [{"id": "/a", "title": "A", "action": "updated"}],
        "summary": (
            "updated\n\nPost-run check: 1 written concept(s) received no inbound link: /a."
        ),
        "links_after": {
            "checked": ["/a"],
            "unbacklinked": ["/a"],
            "orphans": ["/a"],
            "healthy": False,
        },
    }


# --- trace sessions (step 3.1) -----------------------------------------------


def trace_files(manager, user_id: str) -> list:
    store = manager.library_root(user_id) / ".traces"
    return sorted(store.glob("*.json")) if store.is_dir() else []


async def test_request_knowledge_writes_trace(tmp_path, monkeypatch):
    server, manager, backends, providers, _ = make_stack(
        tmp_path, {"user-a": [LLMResponse(text="answer for A")]}
    )
    set_identity(monkeypatch, "user-a", "agent-a")
    async with Client(server) as client:
        await client.call_tool("request_knowledge", {"query": "hi"})

    files = trace_files(manager, "user-a")
    assert len(files) == 1
    trace = json.loads(files[0].read_text(encoding="utf-8"))
    assert trace["tool"] == "request_knowledge"
    assert trace["agent_label"] == "agent-a"
    assert trace["outcome"] == "ok"
    assert trace["error"] is None
    assert trace["llm"]["provider"] == "openai"
    assert trace["llm"]["iterations"] == 0  # direct answer: no tool-call rounds
    assert trace["events"] == []  # no tool hops on the direct-answer path
    # per-user scoping: no trace store under the other users' roots
    assert trace_files(manager, "user-b") == []


@pytest.mark.parametrize(
    "tool_name,handler_name,call_kwargs",
    [
        ("request_knowledge", "handle_request", {"query": "hi"}),
        ("store_knowledge", "handle_store", {"content": "c"}),
        ("update_knowledge", "handle_update", {"instruction": "i"}),
        ("library_maintain", "handle_maintain", {}),
        ("library_curate", "handle_curate", {}),
    ],
)
async def test_cancelled_handler_traces_outcome_cancelled(
    tmp_path, monkeypatch, tool_name, handler_name, call_kwargs
):
    """A handler raising CancelledError traces outcome 'cancelled', never 'ok'."""
    server, manager, *_ = make_stack(tmp_path)
    librarian = manager.get("user-a")

    async def cancelled(*args, **kwargs):
        session = current_trace()
        assert session is not None
        session.record("list_dir", {"path": "/"}, [], None, 1.0)
        raise asyncio.CancelledError

    monkeypatch.setattr(librarian, handler_name, cancelled)
    set_identity(monkeypatch, "user-a", "agent-a")
    tool = await server.get_tool(tool_name)
    with pytest.raises(asyncio.CancelledError):
        await tool.fn(**call_kwargs)

    files = trace_files(manager, "user-a")
    assert len(files) == 1
    trace = json.loads(files[0].read_text(encoding="utf-8"))
    assert trace["tool"] == tool_name
    assert trace["outcome"] == "cancelled"


async def test_trace_records_tool_events(tmp_path, monkeypatch):
    server, manager, backends, providers, _ = make_stack(
        tmp_path,
        {
            "user-a": [
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
        },
    )
    set_identity(monkeypatch, "user-a", "agent-a")
    async with Client(server) as client:
        await client.call_tool("store_knowledge", {"content": "fresh"})

    files = trace_files(manager, "user-a")
    assert len(files) == 1
    trace = json.loads(files[0].read_text(encoding="utf-8"))
    assert trace["tool"] == "store_knowledge"
    assert [event["tool"] for event in trace["events"]] == ["write_concept"]
    event = trace["events"][0]
    assert event["result"] == {"id": "/new", "action": "created"}
    assert event["error"] is None
    assert event["duration_ms"] >= 0


async def test_failed_call_records_error_outcome(tmp_path, monkeypatch):
    class FlakyProvider:
        def __init__(self):
            self.calls = 0

        async def complete(self, messages, tools, config):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    tool_calls=[
                        ToolCall(id="c1", name="read_document", arguments={"path": "/a.md"})
                    ]
                )
            raise RuntimeError("backend down")

    db_path = make_db(tmp_path)
    backends = {"user-a": FakeBackend()}
    backends["user-a"].docs["/a.md"] = {
        "frontmatter": {"title": "A", "type": "Note"},
        "body": "old",
    }
    manager = LibrarianManager(
        db_path,
        tmp_path / "data",
        backend_factory=lambda user_id, root, config: backends[user_id],
        provider_factory=lambda user_id, llm: FlakyProvider(),
    )
    server = create_mcp_server(manager, token_lookup=sqlite_token_lookup(db_path))
    set_identity(monkeypatch, "user-a", "agent-a")
    async with Client(server) as client:
        with pytest.raises(ToolError):
            await client.call_tool("request_knowledge", {"query": "hi"})

    # the read_document hop was recorded before the LLM failure, so the
    # trace file exists and carries the error outcome
    files = trace_files(manager, "user-a")
    assert len(files) == 1
    trace = json.loads(files[0].read_text(encoding="utf-8"))
    assert trace["outcome"] == "error"
    assert "backend down" in trace["error"]
    assert [event["tool"] for event in trace["events"]] == ["read_document"]


async def test_unexpected_internal_error_is_sanitized_for_client(tmp_path, monkeypatch):
    """CS-5: the MCP client gets a generic message, never internals."""

    class ExplodingProvider:
        async def complete(self, messages, tools, config):
            raise RuntimeError("provider down at http://internal-host:1234 key sk-xyz")

    db_path = make_db(tmp_path)
    backends = {"user-a": FakeBackend()}
    manager = LibrarianManager(
        db_path,
        tmp_path / "data",
        backend_factory=lambda user_id, root, config: backends[user_id],
        provider_factory=lambda user_id, llm: ExplodingProvider(),
    )
    server = create_mcp_server(manager, token_lookup=sqlite_token_lookup(db_path))
    set_identity(monkeypatch, "user-a", "agent-a")
    async with Client(server) as client:
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool("request_knowledge", {"query": "hi"})
    message = str(excinfo.value)
    assert "internal error" in message
    assert "internal-host" not in message
    assert "sk-xyz" not in message
    assert "provider down" not in message


async def test_partial_store_result_still_syncs_embeddings(tmp_path, monkeypatch):
    """L2 (F16): provider failure after a landed write -> partial result + sync."""

    class WriteThenExplodeProvider:
        def __init__(self):
            self.calls = 0

        async def complete(self, messages, tools, config):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
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
                )
            raise RuntimeError("provider exploded")

    db_path = make_db(tmp_path)
    backends = {"user-a": FakeBackend()}
    manager = LibrarianManager(
        db_path,
        tmp_path / "data",
        backend_factory=lambda user_id, root, config: backends[user_id],
        provider_factory=lambda user_id, llm: WriteThenExplodeProvider(),
    )
    server = create_mcp_server(manager, token_lookup=sqlite_token_lookup(db_path))
    set_identity(monkeypatch, "user-a", "agent-a")
    librarian = manager.get("user-a")
    synced: list[list[dict]] = []

    async def fake_sync(writes):
        synced.append(list(writes))

    monkeypatch.setattr(librarian, "sync_embeddings", fake_sync)
    async with Client(server) as client:
        result = await client.call_tool("store_knowledge", {"content": "knowledge"})
    assert result.data["stored"] == [{"id": "/n", "title": "N", "action": "created"}]
    assert result.data["partial"] is True
    assert "interrupted" in result.data["summary"]
    assert synced == [[{"id": "/n", "title": "N", "action": "created"}]]


async def test_store_result_includes_links_after(tmp_path, monkeypatch):
    """F19/F20: the links_after field survives the MCP layer unchanged."""
    server, manager, backends, providers, seeds = make_stack(
        tmp_path,
        {
            "user-a": [
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
        },
    )
    set_identity(monkeypatch, "user-a", "agent-a")
    async with Client(server) as client:
        result = await client.call_tool("store_knowledge", {"content": "fresh"})
    assert isinstance(result.data["links_after"]["healthy"], bool)
    assert "Post-run check:" in result.data["summary"]


async def test_library_status_writes_no_trace(tmp_path, monkeypatch):
    server, manager, *_ = make_stack(tmp_path)
    set_identity(monkeypatch, "user-b", "agent-b")
    async with Client(server) as client:
        await client.call_tool("library_status", {})
    assert trace_files(manager, "user-b") == []  # D3: no session opened


async def test_library_maintain_noop_writes_no_trace(tmp_path, monkeypatch):
    server, manager, *_ = make_stack(tmp_path, {"user-b": [LLMResponse(text="unused")]})
    set_identity(monkeypatch, "user-b", "agent-b")
    async with Client(server) as client:
        await client.call_tool("library_maintain", {})
    # D3: the healthy no-op produces no events and no llm data, hence no file
    assert trace_files(manager, "user-b") == []


async def test_library_curate_noop_writes_no_trace(tmp_path, monkeypatch):
    server, manager, *_ = make_stack(tmp_path, {"user-b": [LLMResponse(text="unused")]})
    set_identity(monkeypatch, "user-b", "agent-b")
    async with Client(server) as client:
        await client.call_tool("library_curate", {})
    # D3: the well-organized no-op produces no events and no llm data
    assert trace_files(manager, "user-b") == []


async def test_seed_in_tool_descriptions(tmp_path, monkeypatch):
    server, *_ = make_stack(tmp_path)
    set_identity(monkeypatch, "user-a", "agent-a")
    async with Client(server) as client:
        tools = await client.list_tools()
    by_name = {tool.name: tool for tool in tools}
    assert "SEED<0>" in by_name["store_knowledge"].description
    assert "SEED<0>" in by_name["request_knowledge"].description
    assert "SEED<0>" not in by_name["library_status"].description
    # curate is an operator action, not an orientation surface: seed-free
    assert "SEED<0>" not in by_name["library_curate"].description


async def test_initialize_instructions_are_static_base(tmp_path, monkeypatch):
    # Verified against fastmcp 3.4.5: middleware cannot modify the initialize
    # result per request, so only the static base instructions ship there
    # (Contract 2 fallback: the seed ships via tool descriptions).
    server, *_ = make_stack(tmp_path)
    set_identity(monkeypatch, "user-a", "agent-a")
    async with Client(server) as client:
        instructions = client.initialize_result.instructions
    assert "Athenaeum is a self-hosted" in instructions


async def test_seed_reflects_write_after_cache_invalidation(tmp_path, monkeypatch):
    server, manager, backends, providers, seeds = make_stack(
        tmp_path,
        {
            "user-a": [
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
        },
    )
    set_identity(monkeypatch, "user-a", "agent-a")
    async with Client(server) as client:
        tools_before = {t.name: t for t in await client.list_tools()}
        assert "SEED<0>" in tools_before["store_knowledge"].description

        result = await client.call_tool("store_knowledge", {"content": "fresh"})
        assert result.data["stored"] == [{"id": "/new", "title": "New", "action": "created"}]

        tools_after = {t.name: t for t in await client.list_tools()}
        assert "SEED<1>" in tools_after["store_knowledge"].description
        assert "SEED<1>" in tools_after["request_knowledge"].description


async def test_library_curate_refreshes_seed_after_mutating_run(tmp_path, monkeypatch):
    server, manager, backends, providers, seeds = make_stack(
        tmp_path,
        {
            "user-a": [
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="edit_concept",
                            arguments={"path": "/a.md", "new_body": "enriched body"},
                        )
                    ]
                ),
                LLMResponse(text="curated"),
            ]
        },
    )
    backends["user-a"].docs["/a.md"] = {
        "frontmatter": {"title": "A", "type": "Note"},
        "body": "old",
    }
    # the findings scan reads real files from the user's library root
    root = manager.library_root("user-a")
    root.mkdir(parents=True)
    (root / "a.md").write_text("---\ntype: Note\ntitle: A\n---\nold\n", encoding="utf-8")
    set_identity(monkeypatch, "user-a", "agent-a")
    async with Client(server) as client:
        tools_before = {t.name: t for t in await client.list_tools()}
        assert "SEED<0>" in tools_before["store_knowledge"].description

        result = await client.call_tool("library_curate", {})
        assert result.data["actions"] == [{"id": "/a", "title": "A", "action": "updated"}]
        assert result.data["summary"].startswith("curated")
        assert "Post-run check:" in result.data["summary"]
        assert result.data["health_after"] == {"healthy": True, "orphans": 0}

        tools_after = {t.name: t for t in await client.list_tools()}
        assert "SEED<1>" in tools_after["store_knowledge"].description
        assert "SEED<1>" in tools_after["request_knowledge"].description


async def test_seed_never_leaks_across_users(tmp_path, monkeypatch):
    """A user's seed refresh must never reach another user's tools/list.

    Regression (S9): the seeded tools were re-registered in the shared
    FastMCP registry with the writer's seed baked in, so a caller with an
    empty library (empty seed, middleware early return) saw that seed. The
    registry now keeps base descriptions forever; the middleware composes
    per request.
    """
    server, manager, backends, providers, seeds = make_stack(
        tmp_path,
        {
            "user-a": [
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
        },
        # user-b's library is empty: no seed for them
        seed_generator=lambda backend: f"SEED<{backend.seed_marker}>" if backend.docs else "",
    )
    backends["user-a"].docs["/a.md"] = {
        "frontmatter": {"title": "A", "type": "Note"},
        "body": "old",
    }
    set_identity(monkeypatch, "user-a", "agent-a")
    async with Client(server) as client:
        tools_a = {t.name: t for t in await client.list_tools()}
        assert "SEED<0>" in tools_a["store_knowledge"].description

        result = await client.call_tool("store_knowledge", {"content": "fresh"})
        assert result.data["stored"] == [{"id": "/new", "title": "New", "action": "created"}]

        tools_a = {t.name: t for t in await client.list_tools()}
        assert "SEED<1>" in tools_a["store_knowledge"].description

    set_identity(monkeypatch, "user-b", "agent-b")
    async with Client(server) as client:
        tools_b = {t.name: t for t in await client.list_tools()}
    for name in ("store_knowledge", "request_knowledge"):
        description = tools_b[name].description
        assert "SEED<" not in description
        assert "Current library seed" not in description
    assert tools_b["store_knowledge"].description.startswith("Persist new knowledge")
    assert tools_b["request_knowledge"].description == "Ask the librarian for knowledge by intent."


async def test_update_knowledge_refreshes_seed(tmp_path, monkeypatch):
    server, manager, backends, providers, seeds = make_stack(
        tmp_path,
        {
            "user-a": [
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="edit_concept",
                            arguments={"path": "/a.md", "new_body": "fixed"},
                        )
                    ]
                ),
                LLMResponse(text="updated"),
            ]
        },
    )
    backends["user-a"].docs["/a.md"] = {
        "frontmatter": {"title": "A", "type": "Note"},
        "body": "old",
    }
    set_identity(monkeypatch, "user-a", "agent-a")
    async with Client(server) as client:
        tools_before = {t.name: t for t in await client.list_tools()}
        assert "SEED<0>" in tools_before["store_knowledge"].description

        result = await client.call_tool("update_knowledge", {"instruction": "fix a"})
        assert result.data["stored"] == [{"id": "/a", "title": "A", "action": "updated"}]

        tools_after = {t.name: t for t in await client.list_tools()}
        assert "SEED<1>" in tools_after["store_knowledge"].description
        assert "SEED<1>" in tools_after["request_knowledge"].description


async def test_store_knowledge_passes_topic_hint_to_task(tmp_path, monkeypatch):
    server, manager, backends, providers, seeds = make_stack(
        tmp_path,
        {
            "user-a": [
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="write_concept",
                            arguments={
                                "path": "/new.md",
                                "frontmatter": {"title": "New", "type": "Note"},
                                "body": "fresh",
                            },
                        )
                    ]
                ),
                LLMResponse(text="stored"),
            ]
        },
    )
    set_identity(monkeypatch, "user-a", "agent-a")
    async with Client(server) as client:
        result = await client.call_tool(
            "store_knowledge", {"content": "fresh", "topic_hint": "home-automation"}
        )
    assert result.data["summary"] == (
        "stored\n\nPost-run check: 1 written concept(s) received no inbound link: /new."
    )
    task = providers["user-a"].calls[0][0][1]["content"]
    assert "Topic hint from the caller: home-automation" in task


# --- internal tool dispatch: arg coercion (L6) -------------------------------


class _DispatchStub:
    """Minimal backend recording the args dispatch hands it."""

    def __init__(self):
        self.calls: list[tuple] = []

    def list_dir(self, path: str = "/") -> list:
        self.calls.append(("list_dir", path))
        return []

    async def search_semantic(self, query: str, limit: int = 8) -> list:
        self.calls.append(("search_semantic", query, limit))
        return []


async def test_dispatch_semantic_limit_zero_stays_zero():
    backend = _DispatchStub()
    await dispatch("search_semantic", {"query": "q", "limit": 0}, backend)
    assert backend.calls == [("search_semantic", "q", 0)]


async def test_dispatch_semantic_limit_missing_and_null_coerce_to_default():
    for args in ({"query": "q"}, {"query": "q", "limit": None}):
        backend = _DispatchStub()
        await dispatch("search_semantic", args, backend)
        assert backend.calls == [("search_semantic", "q", 8)]


async def test_dispatch_list_dir_null_path_coerces_to_default():
    backend = _DispatchStub()
    await dispatch("list_dir", {"path": None}, backend)
    assert backend.calls == [("list_dir", "/")]


async def test_dispatch_required_null_is_not_reported_as_missing():
    backend = _DispatchStub()
    with pytest.raises(ValueError, match=r"required argument\(s\) given as null: path"):
        await dispatch("read_document", {"path": None}, backend)
    with pytest.raises(ValueError, match=r"missing required argument\(s\): path"):
        await dispatch("read_document", {}, backend)
