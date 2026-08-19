"""Tests for the librarian agent loop (plan stream-B checklist).

Uses a scripted fake LLMProvider and an in-memory LibraryBackend stand-in
conforming to plan section 3.2. The "real backend" section at the bottom
drives the handlers and the lazy startup reconcile against a real
LibraryBackend on tmp_path (plan section 5 item 7, plan Step 4).
"""

import hashlib
import json
import logging
import re
import tempfile

import pytest

from athenaeum.librarian.agent import (
    FINAL_ANSWER_REQUEST,
    KIND_LIBRARIAN,
    TOOL_RESULT_CHAR_LIMIT,
    Librarian,
    LibrarianConfig,
    LibrarianNotConfiguredError,
    LibrarianNoWriteError,
    _Tracker,
    is_stale,
    trust_tier,
)
from athenaeum.librarian.gate import AgentRunBusyError
from athenaeum.librarian.llm import LLMConfig, LLMResponse, ToolCall
from athenaeum.librarian.prompts import DEFAULT_SYSTEM_PROMPT
from athenaeum.librarian.tracing import (
    RequestTelemetry,
    TraceSession,
    _telemetry_var,
    _trace_var,
)
from athenaeum.library import escape_guard as escape_guard_mod
from athenaeum.library import organize as organize_mod
from athenaeum.library.backend import LibraryBackend
from athenaeum.library.payloads import PayloadStore


class FakeBackend:
    """In-memory LibraryBackend conforming to plan section 3.2 signatures."""

    def __init__(self, docs: dict | None = None, healthy: bool = True):
        # docs: path -> {"frontmatter": dict, "body": str}
        self.docs = docs or {}
        self.healthy = healthy
        self.calls: list[tuple] = []
        # Real dir the A10 scan pass-throughs delegate to; tests set it when
        # curate scans matter (the in-memory docs above back tool dispatch).
        self.scan_root = None

    def organization_findings(self, *, since=None) -> dict:
        return organize_mod.organization_findings(self.scan_root, since=since)

    def escape_artifact_scan(self):
        return escape_guard_mod.scan_escape_artifacts(self.scan_root)

    def code_span_escape_candidates(self):
        return escape_guard_mod.scan_code_span_escape_candidates(self.scan_root)

    def findings_empty(self, report: dict) -> bool:
        return organize_mod.findings_empty(report)

    def list_dir(self, path: str = "/") -> list[dict]:
        self.calls.append(("list_dir", path))
        return [{"name": "concepts", "path": "/concepts", "is_directory": True}]

    def read_document(self, path: str) -> dict:
        self.calls.append(("read_document", path))
        doc = self.docs[path]
        return {"path": path, "frontmatter": doc["frontmatter"], "body": doc["body"]}

    def search_metadata(self, field=None, value=None) -> list[dict]:
        self.calls.append(("search_metadata", field, value))
        return [
            {
                "id": path[: -len(".md")],
                "path": path,
                "title": doc["frontmatter"].get("title", ""),
                "type": doc["frontmatter"].get("type", ""),
                "description": doc["frontmatter"].get("description", ""),
            }
            for path, doc in self.docs.items()
        ]

    async def search_semantic(self, query, limit=8) -> list[dict]:
        self.calls.append(("search_semantic", query, limit))
        return [
            {
                "id": "/alpha",
                "path": "/alpha.md",
                "title": "Alpha",
                "type": "Note",
                "description": "about alpha",
                "score": 0.9,
            }
        ]

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
        self.calls.append(("create_concept", path, agent_label))
        self.docs[path] = {"frontmatter": dict(frontmatter), "body": body}
        return {"id": path[: -len(".md")], "action": "created"}

    def edit_concept(
        self,
        path,
        *,
        frontmatter_patch=None,
        remove_keys=None,
        new_body=None,
        agent_label=None,
        requested_by=None,
        via=None,
        allow_literal_escapes=False,
    ) -> dict:
        self.calls.append(("edit_concept", path, agent_label))
        doc = self.docs[path]
        doc["frontmatter"].update(frontmatter_patch or {})
        for key in remove_keys or []:
            doc["frontmatter"].pop(key, None)
        if new_body is not None:
            doc["body"] = new_body
        return {"id": path[: -len(".md")], "action": "updated"}

    def move_concept(self, old_path, new_path, *, agent_label=None) -> dict:
        self.calls.append(("move_concept", old_path, new_path, agent_label))
        self.docs[new_path] = self.docs.pop(old_path)
        return {"id": new_path[: -len(".md")], "action": "moved", "links_rewritten": 0}

    def deprecate_concept(self, path, *, agent_label=None, requested_by=None, via=None) -> dict:
        self.calls.append(("deprecate_concept", path, agent_label))
        self.docs[path]["frontmatter"]["status"] = "deprecated"
        return {"id": path[: -len(".md")], "action": "deprecated"}

    def delete_concept(self, path, *, agent_label=None) -> dict:
        self.calls.append(("delete_concept", path, agent_label))
        del self.docs[path]
        return {"id": path[: -len(".md")], "action": "deleted", "inbound_links": []}

    def verify_concept(self, path, *, by, at=None, agent_label=None) -> dict:
        self.calls.append(("verify_concept", path, by, agent_label))
        entries = self.docs[path]["frontmatter"].setdefault("verified", [])
        entries.append({"by": by, "at": at or "2026-08-04T00:00:00+00:00"})
        return {"id": path[: -len(".md")], "action": "verified"}

    def link_check(self, path=None) -> list[dict]:
        self.calls.append(("link_check", path))
        return []

    def status(self) -> dict:
        self.calls.append(("status",))
        health = {"orphans": [], "broken_links": [], "warnings": 0, "errors": 0}
        if not self.healthy:
            health["orphans"] = [{"id": "/orphan", "title": "Orphan"}]
            health["warnings"] = 1
        return {
            "stats": {
                "concepts": len(self.docs),
                "directories": 1,
                "versions": 0,
                "last_write": None,
            },
            "health": health,
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
    """Returns a fixed queue of LLMResponses; records every complete() call."""

    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.calls: list[tuple[list[dict], list[dict], LLMConfig]] = []

    async def complete(self, messages, tools, config) -> LLMResponse:
        self.calls.append((list(messages), list(tools), config))
        if not self.responses:
            return LLMResponse(text="(script exhausted)")
        return self.responses.pop(0)


def make_librarian(
    backend, provider, max_iterations=3, *, root=None, computation_runner=None
) -> Librarian:
    config = LibrarianConfig(
        user_id="user-1",
        llm=LLMConfig(provider="openai", model="m", api_key="k", max_iterations=max_iterations),
    )
    # The root defaults to a fresh temp dir: handle_store's payload archive
    # (0.20.0) writes under <root>/.athenaeum/payloads — never a fixed path.
    return Librarian(
        root or tempfile.mkdtemp(prefix="athenaeum-loop-"),
        config,
        backend=backend,
        provider=provider,
        computation_runner=computation_runner,
    )


def tc(call_id: str, name: str, arguments: dict | None = None) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments or {})


DOC = {"frontmatter": {"title": "Alpha", "type": "Note"}, "body": "about alpha"}


async def test_multi_round_tool_dispatch_and_answer():
    backend = FakeBackend(docs={"/alpha.md": dict(DOC, body="about alpha")})
    provider = ScriptedProvider(
        [
            LLMResponse(tool_calls=[tc("c1", "list_dir", {"path": "/"})]),
            LLMResponse(tool_calls=[tc("c2", "read_document", {"path": "/alpha.md"})]),
            LLMResponse(text="Alpha is a note [/alpha.md]."),
        ]
    )
    librarian = make_librarian(backend, provider)

    result = await librarian.handle_request("what is alpha?", agent_label="agent-x")

    assert result["answer"] == "Alpha is a note [/alpha.md]."
    assert ("list_dir", "/") in backend.calls
    assert ("read_document", "/alpha.md") in backend.calls
    assert result["concepts"] == [
        {
            "id": "/alpha",
            "title": "Alpha",
            "type": "Note",
            "trust_tier": "unverified",
            "stale": False,
        }
    ]
    # second provider call saw the first tool result in the message history
    second_call_messages = provider.calls[1][0]
    tool_messages = [m for m in second_call_messages if m["role"] == "tool"]
    assert tool_messages[0]["tool_call_id"] == "c1"
    assert "concepts" in tool_messages[0]["content"]


async def test_request_loop_runs_computation(tmp_path):
    """The internal run_computation tool: the loop gets the receipt, answers
    from it, and the tracker records nothing (read-only action)."""
    import sqlite3

    from athenaeum import db as db_module
    from athenaeum.computation import ComputationRunner

    target = tmp_path / "target.db"
    conn = sqlite3.connect(target)
    conn.execute("CREATE TABLE items (name TEXT)")
    conn.executemany("INSERT INTO items VALUES (?)", [("a",), ("b",)])
    conn.commit()
    conn.close()
    db_path = tmp_path / "app.db"
    db_module.init_db(db_path)
    with db_module.connect(db_path) as conn:
        row = db_module.create_runtime_connection(
            conn, label="L", runtime="sqlite", dbname=str(target)
        )
        db_module.set_app_setting(conn, "computation_execution_enabled", "1")
    backend = FakeBackend(
        docs={
            "/q.md": {
                "frontmatter": {"title": "Q", "type": "Attested Computation", "runtime": "sqlite"},
                "body": "# Computation\n\n```sql\nSELECT count(*) AS n FROM items\n```\n",
            }
        }
    )
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc("c1", "run_computation", {"path": "/q.md", "connection_id": row["id"]})
                ]
            ),
            LLMResponse(text="There are 2 items [/q.md]."),
        ]
    )
    librarian = make_librarian(backend, provider, computation_runner=ComputationRunner(db_path))

    result = await librarian.handle_request("how many items?")

    assert result["answer"] == "There are 2 items [/q.md]."
    # the receipt reached the model as the tool result
    second_call_messages = provider.calls[1][0]
    tool_message = [m for m in second_call_messages if m["role"] == "tool"][0]
    assert '"row_count": 1' in tool_message["content"]
    # read-only: no read_document tracking, no write tracking
    assert result["concepts"] == []


async def test_cap_enforcement_final_answer_without_tools():
    backend = FakeBackend()
    provider = ScriptedProvider([LLMResponse(tool_calls=[tc("c1", "list_dir")]) for _ in range(3)])
    librarian = make_librarian(backend, provider, max_iterations=2)

    result = await librarian.handle_request("never satisfied")

    # initial call + 2 tool rounds + final-answer request = 4 calls
    assert len(provider.calls) == 4
    final_messages, final_tools, _ = provider.calls[-1]
    assert final_tools == []  # cap exhausted: final answer requested with no tools
    assert final_messages[-1]["role"] == "user"
    assert FINAL_ANSWER_REQUEST in final_messages[-1]["content"]
    assert result["answer"] == "(script exhausted)"


async def test_final_answer_extraction_with_follow_ups():
    backend = FakeBackend()
    provider = ScriptedProvider(
        [LLMResponse(text="The answer.\n\n## Follow-ups\n- q one\n- q two\n")]
    )
    librarian = make_librarian(backend, provider)

    result = await librarian.handle_request("q")

    assert result["answer"].startswith("The answer.")
    assert result["concepts"] == []
    assert result["follow_ups"] == ["q one", "q two"]


async def test_handle_store_tracks_writes_and_injects_agent_label():
    docs = {
        "/existing.md": {
            "frontmatter": {"title": "Existing", "type": "Note"},
            "body": "old body",
        }
    }
    backend = FakeBackend(docs=docs)
    provider = ScriptedProvider(
        [
            LLMResponse(tool_calls=[tc("c1", "search_metadata", {"field": "title"})]),
            LLMResponse(
                tool_calls=[
                    tc(
                        "c2",
                        "write_concept",
                        {
                            "path": "/new.md",
                            "frontmatter": {"title": "New Thing", "type": "Note"},
                            "body": "fresh knowledge",
                        },
                    )
                ]
            ),
            LLMResponse(
                tool_calls=[
                    tc(
                        "c3",
                        "edit_concept",
                        {"path": "/existing.md", "new_body": "old body\n\nSee [/new.md]."},
                    )
                ]
            ),
            LLMResponse(text="Stored as /new.md and linked from /existing.md."),
        ]
    )
    librarian = make_librarian(backend, provider)

    result = await librarian.handle_store("fresh knowledge", agent_label="agent-z")

    assert result["summary"] == (
        "Stored as /new.md and linked from /existing.md."
        "\n\nPost-run check: 2 written concept(s) received no inbound link: /new, /existing."
    )
    assert result["stored"] == [
        {"id": "/new", "title": "New Thing", "action": "created"},
        {"id": "/existing", "title": "Existing", "action": "updated"},
    ]
    # agent_label injected by the loop, never by the LLM
    assert ("create_concept", "/new.md", "agent-z") in backend.calls
    assert ("edit_concept", "/existing.md", "agent-z") in backend.calls


async def test_handle_update_tracks_writes_and_injects_agent_label():
    docs = {
        "/existing.md": {
            "frontmatter": {"title": "Existing", "type": "Note"},
            "body": "old body",
        }
    }
    backend = FakeBackend(docs=docs)
    provider = ScriptedProvider(
        [
            LLMResponse(tool_calls=[tc("c1", "search_metadata", {"field": "title"})]),
            LLMResponse(
                tool_calls=[
                    tc("c2", "edit_concept", {"path": "/existing.md", "new_body": "fixed body"})
                ]
            ),
            LLMResponse(text="Corrected /existing.md."),
        ]
    )
    librarian = make_librarian(backend, provider)

    result = await librarian.handle_update("fix the existing note", agent_label="agent-u")

    assert result["summary"] == (
        "Corrected /existing.md."
        "\n\nPost-run check: 1 written concept(s) received no inbound link: /existing."
    )
    assert result["stored"] == [{"id": "/existing", "title": "Existing", "action": "updated"}]
    assert ("edit_concept", "/existing.md", "agent-u") in backend.calls
    task_prompt = provider.calls[0][0][1]["content"]
    assert "UPDATE TASK" in task_prompt
    assert "fix the existing note" in task_prompt


async def test_update_task_pins_deprecate_over_delete():
    """Deletion via update_knowledge ALWAYS routes to deprecate_concept;
    delete_concept is reserved for curator cleanup (prompt-level pin)."""
    backend = FakeBackend(
        docs={"/old.md": {"frontmatter": {"title": "Old", "type": "Note"}, "body": "x"}}
    )
    provider = ScriptedProvider(
        [
            LLMResponse(tool_calls=[tc("c1", "deprecate_concept", {"path": "/old.md"})]),
            LLMResponse(text="Deprecated the old note."),
        ]
    )
    librarian = make_librarian(backend, provider)
    await librarian.handle_update("delete the old note")
    task_prompt = provider.calls[0][0][1]["content"]
    assert "ALWAYS go through deprecate_concept" in task_prompt
    assert "NEVER use delete_concept" in task_prompt
    assert "reserved for curator cleanup" in task_prompt


async def test_store_retries_once_on_silent_no_write():
    backend = FakeBackend()
    provider = ScriptedProvider(
        [
            LLMResponse(text=""),  # first run: silent no-op (F11 signature)
            LLMResponse(
                tool_calls=[
                    tc(
                        "c1",
                        "write_concept",
                        {
                            "path": "/n.md",
                            "frontmatter": {"title": "N", "type": "Note"},
                            "body": "b",
                        },
                    )
                ]
            ),
            LLMResponse(text="Stored."),
        ]
    )
    librarian = make_librarian(backend, provider)

    result = await librarian.handle_store("knowledge")

    assert result["stored"] == [{"id": "/n", "title": "N", "action": "created"}]
    assert result["summary"] == (
        "Stored.\n\nPost-run check: 1 written concept(s) received no inbound link: /n."
    )
    assert "failed store" in provider.calls[1][0][1]["content"]  # nudge in the retry task


async def test_store_raises_after_two_silent_no_writes():
    backend = FakeBackend()
    provider = ScriptedProvider([LLMResponse(text=""), LLMResponse(text="")])
    librarian = make_librarian(backend, provider)

    with pytest.raises(LibrarianNoWriteError):
        await librarian.handle_store("knowledge")


async def test_store_without_writes_fails_despite_summary():
    """L1 (F13): a non-empty summary alone does NOT make a write task ok."""
    backend = FakeBackend()
    provider = ScriptedProvider(
        [
            LLMResponse(text="Already covered by /existing.md."),
            LLMResponse(text="Already covered by /existing.md."),  # nudge retry
        ]
    )
    librarian = make_librarian(backend, provider)

    with pytest.raises(LibrarianNoWriteError):
        await librarian.handle_store("duplicate knowledge")

    assert len(provider.calls) == 2  # no writes -> nudge retry -> still no writes


async def test_store_cap_exit_with_zero_writes_fails():
    """L1 (F13): cap-exhausted run with a summary but no writes is a failure."""
    backend = FakeBackend()
    provider = ScriptedProvider(
        [
            LLMResponse(tool_calls=[tc("c1", "list_dir", {"path": "/"})]),  # burns the cap
            LLMResponse(text="I only read."),  # cap-exit final answer
            LLMResponse(tool_calls=[tc("c2", "list_dir", {"path": "/"})]),  # retry: same
            LLMResponse(text="Still only reading."),
        ]
    )
    librarian = make_librarian(backend, provider, max_iterations=1)

    with pytest.raises(LibrarianNoWriteError):
        await librarian.handle_store("knowledge")


async def test_write_nudge_fires_with_zero_writes_at_threshold():
    """Store-watchdog: nothing written and WRITE_NUDGE_REMAINING iterations left
    -> one direct WRITE NOW user message before the next complete()."""
    backend = FakeBackend()
    provider = ScriptedProvider(
        [
            LLMResponse(tool_calls=[tc("c1", "list_dir", {"path": "/"})]),
            LLMResponse(tool_calls=[tc("c2", "list_dir", {"path": "/a"})]),
            LLMResponse(
                tool_calls=[
                    tc(
                        "c3",
                        "write_concept",
                        {
                            "path": "/n.md",
                            "frontmatter": {"title": "N", "type": "Note"},
                            "body": "b",
                        },
                    )
                ]
            ),
            LLMResponse(text="Stored."),
        ]
    )
    librarian = make_librarian(backend, provider, max_iterations=4)

    result = await librarian.handle_store("knowledge")

    # third complete() (after pass 2, remaining == 2) saw the nudge
    third_call_messages = provider.calls[2][0]
    nudges = [m for m in third_call_messages if m["role"] == "user" and "WRITE NOW" in m["content"]]
    assert len(nudges) == 1
    # the nudge did not break the run: the write still landed
    assert result["stored"] == [{"id": "/n", "title": "N", "action": "created"}]


async def test_write_nudge_not_sent_after_successful_write():
    """Once a write landed, the watchdog stays silent even past the threshold."""
    backend = FakeBackend()
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc(
                        "c1",
                        "write_concept",
                        {
                            "path": "/n.md",
                            "frontmatter": {"title": "N", "type": "Note"},
                            "body": "b",
                        },
                    )
                ]
            ),
            LLMResponse(tool_calls=[tc("c2", "list_dir", {"path": "/"})]),
            LLMResponse(tool_calls=[tc("c3", "list_dir", {"path": "/a"})]),
            LLMResponse(text="Stored."),
        ]
    )
    librarian = make_librarian(backend, provider, max_iterations=4)

    result = await librarian.handle_store("knowledge")

    assert result["stored"]
    for messages, _, _ in provider.calls:
        assert all("WRITE NOW" not in (m.get("content") or "") for m in messages)


async def test_write_nudge_never_fires_on_request_task():
    """Retrieval runs (and the curator runs sharing the unflagged _run call)
    never see the write nudge, even with zero writes past the threshold."""
    backend = FakeBackend()
    provider = ScriptedProvider(
        [
            LLMResponse(tool_calls=[tc("c1", "list_dir", {"path": "/"})]),
            LLMResponse(tool_calls=[tc("c2", "list_dir", {"path": "/a"})]),
            LLMResponse(tool_calls=[tc("c3", "list_dir", {"path": "/b"})]),
            LLMResponse(text="Found nothing relevant."),
        ]
    )
    librarian = make_librarian(backend, provider, max_iterations=4)

    result = await librarian.handle_request("anything?")

    assert result["answer"] == "Found nothing relevant."
    for messages, _, _ in provider.calls:
        assert all("WRITE NOW" not in (m.get("content") or "") for m in messages)


async def test_write_nudge_fires_once_and_final_answer_flow_intact():
    """The nudge fires at most once per run and the cap-exit FINAL_ANSWER
    request still goes out with tools == [] as its last message."""
    backend = FakeBackend()
    provider = ScriptedProvider(
        [
            # first run: retrieval only, never writes (distinct paths — exact
            # duplicate calls would be suppressed by the dedupe guard); the
            # response after the last allowed pass still has tool calls, so
            # the cap-exit final-answer request fires
            LLMResponse(tool_calls=[tc("c1", "list_dir", {"path": "/"})]),
            LLMResponse(tool_calls=[tc("c2", "list_dir", {"path": "/a"})]),
            LLMResponse(tool_calls=[tc("c3", "list_dir", {"path": "/b"})]),
            LLMResponse(tool_calls=[tc("c4", "list_dir", {"path": "/c"})]),
            LLMResponse(tool_calls=[tc("c5", "list_dir", {"path": "/d"})]),
            LLMResponse(text="I only read."),  # cap-exit final answer
            LLMResponse(text="Still only reading."),  # F11 retry ends at once
        ]
    )
    librarian = make_librarian(backend, provider, max_iterations=4)

    with pytest.raises(LibrarianNoWriteError):
        await librarian.handle_store("knowledge")

    # first run: initial call + 4 tool rounds + cap-exit call = 6 calls
    first_run_calls = provider.calls[:6]
    final_call_messages = first_run_calls[-1][0]
    nudges = [m for m in final_call_messages if m["role"] == "user" and "WRITE NOW" in m["content"]]
    assert len(nudges) == 1  # fired once, never re-fired on passes 3/4
    cap_exit_tools = first_run_calls[-1][1]
    assert cap_exit_tools == []
    assert final_call_messages[-1]["role"] == "user"
    assert FINAL_ANSWER_REQUEST in final_call_messages[-1]["content"]


class FailAfterProvider:
    """ScriptedProvider variant that raises once the script is exhausted."""

    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.calls: list[tuple[list[dict], list[dict], LLMConfig]] = []

    async def complete(self, messages, tools, config) -> LLMResponse:
        self.calls.append((list(messages), list(tools), config))
        if not self.responses:
            raise RuntimeError("provider exploded")
        return self.responses.pop(0)


async def test_store_provider_error_after_write_returns_partial_success():
    """L2 (F16): a mid-loop provider failure keeps the landed writes."""
    backend = FakeBackend()
    provider = FailAfterProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc(
                        "c1",
                        "write_concept",
                        {
                            "path": "/n.md",
                            "frontmatter": {"title": "N", "type": "Note"},
                            "body": "b",
                        },
                    )
                ]
            ),
            # second complete() raises RuntimeError -> partial result
        ]
    )
    librarian = make_librarian(backend, provider)

    result = await librarian.handle_store("knowledge")

    assert result["stored"] == [{"id": "/n", "title": "N", "action": "created"}]
    assert result["partial"] is True
    assert "interrupted" in result["summary"]


async def test_store_provider_error_without_writes_raises():
    """L2: no landed writes -> the provider failure surfaces unchanged."""
    backend = FakeBackend()
    provider = FailAfterProvider(
        [
            LLMResponse(tool_calls=[tc("c1", "list_dir", {"path": "/"})]),
        ]
    )
    librarian = make_librarian(backend, provider)

    with pytest.raises(RuntimeError, match="provider exploded"):
        await librarian.handle_store("knowledge")


async def test_store_result_reports_links_after_backlinked():
    """F19/F20: a run that back-linked its new concept reports healthy."""
    backend = FakeBackend(
        docs={
            "/home.md": {
                "frontmatter": {"title": "Home", "type": "Note"},
                "body": "[A](/a.md)\n",
            },
            "/a.md": {"frontmatter": {"title": "A", "type": "Note"}, "body": "about a\n"},
        }
    )
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc(
                        "c1",
                        "write_concept",
                        {
                            "path": "/new.md",
                            "frontmatter": {"title": "New", "type": "Note"},
                            "body": "new knowledge\n",
                        },
                    )
                ]
            ),
            LLMResponse(
                tool_calls=[
                    tc(
                        "c2",
                        "edit_concept",
                        {"path": "/a.md", "new_body": "about a\n\nSee [New](/new.md).\n"},
                    )
                ]
            ),
            LLMResponse(text="Stored /new.md, back-linked from /a.md."),
        ]
    )
    librarian = make_librarian(backend, provider)

    result = await librarian.handle_store("knowledge")

    assert result["links_after"] == {
        "checked": ["/new", "/a"],
        "unbacklinked": [],
        "orphans": [],
        "healthy": True,
    }
    assert result["summary"].endswith("Post-run check: all written concepts have inbound links.")


async def test_store_result_flags_missing_backlink():
    """F19: a claimed back-link that never happened is flagged deterministically."""
    backend = FakeBackend()
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc(
                        "c1",
                        "write_concept",
                        {
                            "path": "/new.md",
                            "frontmatter": {"title": "New", "type": "Note"},
                            "body": "new knowledge\n",
                        },
                    )
                ]
            ),
            LLMResponse(text="Stored /new.md, back-linked from /a.md and /b.md."),
        ]
    )
    librarian = make_librarian(backend, provider)

    result = await librarian.handle_store("knowledge")

    assert result["links_after"]["healthy"] is False
    assert result["links_after"]["unbacklinked"] == ["/new"]
    assert result["links_after"]["orphans"] == ["/new"]
    assert (
        "Post-run check: 1 written concept(s) received no inbound link: /new." in result["summary"]
    )


async def test_store_result_links_after_links_out_without_backlink():
    """Inbound is the F19 signal: linking OUT without a back-link is no orphan."""
    backend = FakeBackend(
        docs={"/a.md": {"frontmatter": {"title": "A", "type": "Note"}, "body": "about a\n"}}
    )
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc(
                        "c1",
                        "write_concept",
                        {
                            "path": "/new.md",
                            "frontmatter": {"title": "New", "type": "Note"},
                            "body": "new knowledge; see [A](/a.md).\n",
                        },
                    )
                ]
            ),
            LLMResponse(text="Stored /new.md."),
        ]
    )
    librarian = make_librarian(backend, provider)

    result = await librarian.handle_store("knowledge")

    assert result["links_after"]["unbacklinked"] == ["/new"]
    assert result["links_after"]["orphans"] == []
    assert result["links_after"]["healthy"] is False


async def test_partial_store_result_includes_links_after():
    """L2 interaction: the link check runs on partial results too."""
    backend = FakeBackend()
    provider = FailAfterProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc(
                        "c1",
                        "write_concept",
                        {
                            "path": "/n.md",
                            "frontmatter": {"title": "N", "type": "Note"},
                            "body": "b",
                        },
                    )
                ]
            ),
            # second complete() raises RuntimeError -> partial result
        ]
    )
    librarian = make_librarian(backend, provider)

    result = await librarian.handle_store("knowledge")

    assert result["partial"] is True
    assert "links_after" in result
    assert "interrupted" in result["summary"]
    assert result["summary"].index("interrupted") < result["summary"].index("Post-run check:")


async def test_links_after_skips_deleted_concepts():
    """Deleted concepts are excluded from the link check (file no longer exists)."""
    backend = FakeBackend(
        docs={"/d.md": {"frontmatter": {"title": "D", "type": "Note"}, "body": "d\n"}}
    )
    provider = ScriptedProvider(
        [
            LLMResponse(tool_calls=[tc("c1", "delete_concept", {"path": "/d.md"})]),
            LLMResponse(text="Deleted /d.md."),
        ]
    )
    librarian = make_librarian(backend, provider)

    result = await librarian.handle_update("remove the d note")

    assert result["links_after"] == {
        "checked": [],
        "unbacklinked": [],
        "orphans": [],
        "healthy": True,
    }


# --- payload archive (D3.2, 0.20.0) --------------------------------------------
#
# handle_store writes a two-phase record under <root>/.athenaeum/payloads:
# "received" on entry (busy rejections included), final outcome on exit.


def read_payloads(root) -> list[dict]:
    store = PayloadStore(root)
    return [store.read(summary["request_id"]) for summary in store.list()]


async def test_store_archives_payload_two_phase(tmp_path):
    """The exit rewrite carries the final outcome + stored entries; the
    record holds params, identity, and the minted trace_id."""
    backend = FakeBackend()
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc(
                        "c1",
                        "write_concept",
                        {
                            "path": "/new.md",
                            "frontmatter": {"title": "New", "type": "Note"},
                            "body": "fresh knowledge",
                        },
                    )
                ]
            ),
            LLMResponse(text="Stored /new.md."),
        ]
    )
    librarian = make_librarian(backend, provider, root=tmp_path / "lib")

    result = await librarian.handle_store(
        "fresh knowledge", kind_hint="note", topic_hint="alpha", agent_label="agent-p"
    )

    assert result["stored"] == [{"id": "/new", "title": "New", "action": "created"}]
    (record,) = read_payloads(tmp_path / "lib")
    assert record["outcome"] == "ok"
    assert record["error"] is None
    assert record["tool"] == "store_knowledge"
    assert record["user_id"] == "user-1"
    assert record["agent_label"] == "agent-p"
    assert record["trace_id"]  # minted (no middleware in this harness)
    assert record["received_at"] and record["finished_at"]
    assert record["params"] == {
        "content": "fresh knowledge",
        "kind_hint": "note",
        "relates_to": None,
        "topic_hint": "alpha",
        "images": [],
    }
    assert record["stored"] == [{"id": "/new", "title": "New", "action": "created"}]


async def test_store_payload_records_error_on_no_write(tmp_path):
    """A LibrarianNoWriteError store is archived (exception name only) and re-raised."""
    backend = FakeBackend()
    provider = ScriptedProvider([LLMResponse(text="no tools")])
    librarian = make_librarian(backend, provider, root=tmp_path / "lib")

    with pytest.raises(LibrarianNoWriteError):
        await librarian.handle_store("nothing happens")

    (record,) = read_payloads(tmp_path / "lib")
    assert record["outcome"] == "error"
    assert record["error"] == "LibrarianNoWriteError"
    assert record["stored"] == []


async def test_store_payload_records_partial_outcome(tmp_path):
    """A mid-loop provider failure after landed writes archives outcome partial."""
    backend = FakeBackend()
    provider = FailAfterProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc(
                        "c1",
                        "write_concept",
                        {
                            "path": "/n.md",
                            "frontmatter": {"title": "N", "type": "Note"},
                            "body": "b",
                        },
                    )
                ]
            ),
        ]
    )
    librarian = make_librarian(backend, provider, root=tmp_path / "lib")

    result = await librarian.handle_store("knowledge")

    assert result["partial"] is True
    (record,) = read_payloads(tmp_path / "lib")
    assert record["outcome"] == "partial"
    assert record["stored"] == [{"id": "/n", "title": "N", "action": "created"}]


async def test_store_payload_records_busy_rejection(tmp_path):
    """The received record is written BEFORE the gate acquire, so a busy
    rejection is archived with outcome busy."""
    backend = FakeBackend()
    provider = ScriptedProvider([LLMResponse(text="unused")])
    librarian = make_librarian(backend, provider, root=tmp_path / "lib")

    async with librarian._run_gate.acquire("user-1", KIND_LIBRARIAN, wait=False):
        with pytest.raises(AgentRunBusyError):
            await librarian.handle_store("while busy")

    (record,) = read_payloads(tmp_path / "lib")
    assert record["outcome"] == "busy"
    assert record["error"] == "AgentRunBusyError"
    assert provider.calls == []


async def test_store_payload_archive_failure_never_fails_store(tmp_path, monkeypatch):
    """Best-effort archive (D3.2): a failing PayloadStore is logged; the
    store result is unaffected."""
    backend = FakeBackend()
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc(
                        "c1",
                        "write_concept",
                        {
                            "path": "/new.md",
                            "frontmatter": {"title": "New", "type": "Note"},
                            "body": "x",
                        },
                    )
                ]
            ),
            LLMResponse(text="Stored."),
        ]
    )
    librarian = make_librarian(backend, provider, root=tmp_path / "lib")

    def broken_create(payload):
        raise OSError("disk full")

    monkeypatch.setattr(PayloadStore, "create", broken_create)

    result = await librarian.handle_store("knowledge")
    assert result["stored"] == [{"id": "/new", "title": "New", "action": "created"}]


async def test_store_payload_archives_image_refs_only(tmp_path):
    """D3.4: archived image params are content-addressed refs — the bytes
    live once in the asset store, never as base64 in the payload JSON."""
    backend = make_real_backend(tmp_path)
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc(
                        "c1",
                        "write_concept",
                        {
                            "path": "/new.md",
                            "frontmatter": {"title": "New", "type": "Note"},
                            "body": "x",
                        },
                    )
                ]
            ),
            LLMResponse(text="Stored."),
        ]
    )
    librarian = make_librarian(backend, provider, root=tmp_path / "lib")

    await librarian.handle_store(
        "knowledge",
        images=[{"filename": "diagram.png", "media_type": "image/png", "data": b"\x89PNG fake"}],
    )

    (record,) = read_payloads(tmp_path / "lib")
    (ref,) = record["params"]["images"]
    assert ref["filename"] == "diagram.png"
    assert ref["media_type"] == "image/png"
    assert ref["bytes"] == len(b"\x89PNG fake")
    assert ref["sha256"] == hashlib.sha256(b"\x89PNG fake").hexdigest()
    # the predicted asset path matches the content-addressed write_asset name
    assert ref["asset"] == f"/.athenaeum/assets/{ref['sha256'][:12]}-diagram.png"
    assert "data" not in ref
    assert "data_base64" not in ref


# --- contradiction warnings (Feature 1, D1.x, 0.20.0) ---------------------------


async def test_store_reports_contradictions_cross_checked():
    """D1.1: LLM-reported contradictions land in the result ONLY when the
    tracker confirms an updated/deprecated write on that concept. Runs in
    degraded mode (no embedding service): the channel is embeddings-
    independent (D1.5)."""
    docs = {
        "/existing.md": {
            "frontmatter": {"title": "Existing", "type": "Note"},
            "body": "old body",
        }
    }
    backend = FakeBackend(docs=docs)
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc("c1", "edit_concept", {"path": "/existing.md", "new_body": "new body"})
                ]
            ),
            LLMResponse(
                text="Updated /existing.md with the corrected version.\n\n"
                "## Contradictions\n"
                "- /existing: replaced the outdated version claim\n"
                "- /hallucinated: no such write happened"
            ),
        ]
    )
    librarian = make_librarian(backend, provider)
    assert librarian._embed is None  # degraded mode (D1.5): no pre-ranked candidates

    result = await librarian.handle_store("the version is now 2.0")

    # the LLM-reported id without a tracked write is dropped
    assert result["contradictions"] == [
        {"id": "/existing", "note": "replaced the outdated version claim"}
    ]
    # D1.4: the contradiction verdict comes AFTER the links verdict
    assert result["summary"] == (
        "Updated /existing.md with the corrected version.\n\n"
        "## Contradictions\n"
        "- /existing: replaced the outdated version claim\n"
        "- /hallucinated: no such write happened"
        "\n\nPost-run check: 1 written concept(s) received no inbound link: /existing."
        "\n\nPost-run check: 1 contradiction(s) resolved in place: /existing."
    )


async def test_store_contradiction_via_deprecate_counts():
    """A deprecate_concept write also verifies a reported contradiction."""
    backend = FakeBackend(
        docs={"/old.md": {"frontmatter": {"title": "Old", "type": "Note"}, "body": "x"}}
    )
    provider = ScriptedProvider(
        [
            LLMResponse(tool_calls=[tc("c1", "deprecate_concept", {"path": "/old.md"})]),
            LLMResponse(
                text="Deprecated /old.md.\n\n## Contradictions\n- /old: superseded entirely"
            ),
        ]
    )
    librarian = make_librarian(backend, provider)

    result = await librarian.handle_store("the old note is obsolete")

    assert result["contradictions"] == [{"id": "/old", "note": "superseded entirely"}]


async def test_store_contradictions_absent_without_section_or_writes():
    """D1.3 absent-when-empty: '- none' and a missing section both yield no field."""
    backend = FakeBackend()
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc(
                        "c1",
                        "write_concept",
                        {
                            "path": "/new.md",
                            "frontmatter": {"title": "New", "type": "Note"},
                            "body": "x",
                        },
                    )
                ]
            ),
            LLMResponse(text="Stored.\n\n## Contradictions\n- none"),
        ]
    )
    librarian = make_librarian(backend, provider)
    result = await librarian.handle_store("knowledge")
    assert "contradictions" not in result
    assert result["summary"].count("Post-run check:") == 1  # links verdict only

    backend = FakeBackend()
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc(
                        "c1",
                        "write_concept",
                        {
                            "path": "/new.md",
                            "frontmatter": {"title": "New", "type": "Note"},
                            "body": "x",
                        },
                    )
                ]
            ),
            LLMResponse(text="Stored without any section."),
        ]
    )
    librarian = make_librarian(backend, provider)
    result = await librarian.handle_store("knowledge")
    assert "contradictions" not in result


async def test_update_never_reports_contradictions():
    """D1.2 store-only scoping: the update contract is untouched even when
    the summary carries a Contradictions section."""
    docs = {
        "/existing.md": {
            "frontmatter": {"title": "Existing", "type": "Note"},
            "body": "old body",
        }
    }
    backend = FakeBackend(docs=docs)
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc("c1", "edit_concept", {"path": "/existing.md", "new_body": "new body"})
                ]
            ),
            LLMResponse(text="Fixed.\n\n## Contradictions\n- /existing: corrected the claim"),
        ]
    )
    librarian = make_librarian(backend, provider)

    result = await librarian.handle_update("fix the existing note")

    assert "contradictions" not in result


async def test_store_task_carries_contradictions_report_instruction():
    """D1.1 prompt side: the STORE task pins the report format."""
    backend = FakeBackend()
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc(
                        "c1",
                        "write_concept",
                        {
                            "path": "/new.md",
                            "frontmatter": {"title": "New", "type": "Note"},
                            "body": "x",
                        },
                    )
                ]
            ),
            LLMResponse(text="Stored."),
        ]
    )
    librarian = make_librarian(backend, provider)

    await librarian.handle_store("knowledge")

    task_prompt = provider.calls[0][0][1]["content"]
    assert "## Contradictions" in task_prompt
    assert "- <concept id>: <note>" in task_prompt


async def test_missing_required_arg_is_recoverable_tool_error():
    backend = FakeBackend()
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc(
                        "c1",
                        "write_concept",
                        {"path": "/n.md", "frontmatter": {"title": "N", "type": "Note"}},
                    )
                ]
            ),
            LLMResponse(
                tool_calls=[
                    tc(
                        "c2",
                        "write_concept",
                        {
                            "path": "/n.md",
                            "frontmatter": {"title": "N", "type": "Note"},
                            "body": "recovered",
                        },
                    )
                ]
            ),
            LLMResponse(text="Stored."),
        ]
    )
    librarian = make_librarian(backend, provider)

    result = await librarian.handle_store("knowledge")

    assert result["stored"] == [{"id": "/n", "title": "N", "action": "created"}]
    tool_results = [
        m for m in provider.calls[1][0] if m["role"] == "tool" and "error" in m["content"]
    ]
    assert tool_results
    assert "missing required argument(s): body" in tool_results[0]["content"]


async def test_exact_duplicate_search_never_reaches_backend():
    backend = FakeBackend()
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc("c1", "search_metadata", {"field": "type", "value": "note"}),
                    tc("c2", "search_metadata", {"field": "type", "value": "note"}),
                ]
            ),
            LLMResponse(text="done"),
        ]
    )
    librarian = make_librarian(backend, provider)

    await librarian.handle_request("q")

    assert backend.calls.count(("search_metadata", "type", "note")) == 1
    tool_messages = [m for m in provider.calls[1][0] if m["role"] == "tool"]
    assert len(tool_messages) == 2  # every tool_call gets a tool message
    assert "deduplicated" in tool_messages[1]["content"]
    assert "work from the earlier result" in tool_messages[1]["content"]


async def test_fuzzy_near_duplicate_semantic_blocked():
    backend = FakeBackend()
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc(
                        "c1",
                        "search_semantic",
                        {"query": "how does the librarian store knowledge"},
                    ),
                    tc(
                        "c2",
                        "search_semantic",
                        {"query": "how does the librarian store new knowledge"},
                    ),
                ]
            ),
            LLMResponse(
                tool_calls=[
                    tc("c3", "search_semantic", {"query": "curator verification schedule"}),
                ]
            ),
            LLMResponse(text="done"),
        ]
    )
    librarian = make_librarian(backend, provider)

    await librarian.handle_request("q")

    # token Jaccard 6/7 >= 0.8 blocks the second query; the genuinely
    # different control query still reaches the backend
    semantic_calls = [c for c in backend.calls if c[0] == "search_semantic"]
    assert semantic_calls == [
        ("search_semantic", "how does the librarian store knowledge", 8),
        ("search_semantic", "curator verification schedule", 8),
    ]
    near_notice = [m for m in provider.calls[1][0] if m["role"] == "tool"][1]
    assert "deduplicated" in near_notice["content"]
    assert "near-identical" in near_notice["content"]


async def test_reread_blocked():
    backend = FakeBackend(docs={"/alpha.md": dict(DOC, body="about alpha")})
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc("c1", "read_document", {"path": "/alpha.md"}),
                    tc("c2", "read_document", {"path": "/alpha.md"}),
                ]
            ),
            LLMResponse(text="done"),
        ]
    )
    librarian = make_librarian(backend, provider)

    await librarian.handle_request("q")

    # one loop dispatch + the deterministic post-processing re-read in
    # _concept_entries; the duplicate LLM call never reached the backend
    assert backend.calls.count(("read_document", "/alpha.md")) == 2
    tool_messages = [m for m in provider.calls[1][0] if m["role"] == "tool"]
    assert len(tool_messages) == 2
    assert "deduplicated" in tool_messages[1]["content"]


async def test_suppressed_duplicate_visible_in_trace(tmp_path):
    backend = FakeBackend()
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc("c1", "search_metadata", {"field": "type", "value": "note"}),
                    tc("c2", "search_metadata", {"field": "type", "value": "note"}),
                ]
            ),
            LLMResponse(text="done"),
        ]
    )
    librarian = make_librarian(backend, provider)
    session = TraceSession(tmp_path, "20260817T000000Z-dedupe01", "request_knowledge", "agent-x")
    token = _trace_var.set(session)
    try:
        await librarian.handle_request("q", agent_label="agent-x")
    finally:
        _trace_var.reset(token)
    session.finish("ok")
    trace_id = session.close()

    data = read_trace(tmp_path, trace_id)
    events = [e for e in data["events"] if e["tool"] == "search_metadata"]
    assert len(events) == 2  # the suppressed duplicate stays visible in the trace
    assert events[1]["result"]["deduplicated"] is True
    assert "work from the earlier result" in events[1]["result"]["error"]


async def test_budget_marker_in_tool_results():
    backend = FakeBackend()
    provider = ScriptedProvider(
        [
            LLMResponse(tool_calls=[tc("c1", "list_dir", {"path": "/"})]),
            LLMResponse(text="done"),
        ]
    )
    librarian = make_librarian(backend, provider, max_iterations=3)

    await librarian.handle_request("q")

    tool_message = [m for m in provider.calls[1][0] if m["role"] == "tool"][0]
    assert tool_message["content"].startswith("[budget: 2 iterations remaining] ")
    json.loads(tool_message["content"].split("] ", 1)[1])  # JSON payload follows the prefix


async def test_failed_call_retry_not_blocked():
    class FlakySearchBackend(FakeBackend):
        fail_search = True

        def search_metadata(self, field=None, value=None):
            if self.fail_search:
                self.calls.append(("search_metadata", field, value))
                self.fail_search = False
                raise ValueError("backend exploded")
            return super().search_metadata(field=field, value=value)

    backend = FlakySearchBackend()
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[tc("c1", "search_metadata", {"field": "type", "value": "note"})]
            ),
            LLMResponse(
                tool_calls=[tc("c2", "search_metadata", {"field": "type", "value": "note"})]
            ),
            LLMResponse(text="done"),
        ]
    )
    librarian = make_librarian(backend, provider)

    await librarian.handle_request("q")

    # only successful dispatches enter the dedupe state, so the retry after
    # the error reaches the backend and returns a real result
    assert backend.calls.count(("search_metadata", "type", "note")) == 2
    tool_messages = [m for m in provider.calls[2][0] if m["role"] == "tool"]
    assert "deduplicated" not in tool_messages[-1]["content"]


async def test_trust_tiers_and_staleness_in_concept_entries():
    docs = {
        "/plain.md": {"frontmatter": {"title": "P", "type": "Note"}, "body": ""},
        "/machine.md": {
            "frontmatter": {
                "title": "M",
                "type": "Note",
                "verified": [{"by": "athenaeum-librarian/0.1.0", "at": "2026-01-01"}],
            },
            "body": "",
        },
        "/human.md": {
            "frontmatter": {
                "title": "H",
                "type": "Note",
                "verified": [
                    {"by": "athenaeum-librarian/0.1.0", "at": "2026-01-01"},
                    {"by": "human:alice", "at": "2026-01-02"},
                ],
            },
            "body": "",
        },
        "/bare.md": {
            "frontmatter": {
                "title": "B",
                "type": "Note",
                "verified": {"by": "human:bob", "at": "2026-01-02"},  # bare mapping
            },
            "body": "",
        },
        "/stale.md": {
            "frontmatter": {"title": "S", "type": "Note", "stale_after": "2020-01-01"},
            "body": "",
        },
    }
    backend = FakeBackend(docs=docs)
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc(f"c{i}", "read_document", {"path": path}) for i, path in enumerate(docs)
                ]
            ),
            LLMResponse(text="surveyed"),
        ]
    )
    librarian = make_librarian(backend, provider)

    result = await librarian.handle_request("survey trust")

    tiers = {c["id"]: (c["trust_tier"], c["stale"]) for c in result["concepts"]}
    assert tiers["/plain"] == ("unverified", False)
    assert tiers["/machine"] == ("machine-confirmed", False)
    assert tiers["/human"] == ("human-reviewed", False)
    assert tiers["/bare"] == ("human-reviewed", False)  # bare mapping accepted
    assert tiers["/stale"] == ("unverified", True)


def test_is_stale_boundary_stale_on_the_day_itself():
    """CS-7/OKF §5.5: single shared boundary semantics — stale when stale_after <= today."""
    from datetime import date

    today = date(2026, 7, 28)
    assert is_stale({"stale_after": "2026-07-27"}, today=today) is True
    assert is_stale({"stale_after": "2026-07-28"}, today=today) is True
    assert is_stale({"stale_after": "2026-07-29"}, today=today) is False
    assert is_stale({}, today=today) is False
    assert is_stale({"stale_after": "not-a-date"}, today=today) is False


def test_trust_tier_verified_without_dict_entries_is_unverified():
    """L4: a verified list with no parseable verifier mapping is not machine-confirmed."""
    assert trust_tier({"verified": ["alice", None, 42]}) == "unverified"
    assert trust_tier({"verified": []}) == "unverified"
    assert trust_tier({}) == "unverified"
    # mixed lists still honor the parseable entries
    assert trust_tier({"verified": ["junk", {"by": "human:alice"}]}) == "human-reviewed"
    assert trust_tier({"verified": ["junk", {"by": "athenaeum-librarian/0.1.0"}]}) == (
        "machine-confirmed"
    )


async def test_tool_results_truncated_to_context_budget(tmp_path):
    """A11: oversized tool results are capped before entering the history."""
    huge_body = "x" * (TOOL_RESULT_CHAR_LIMIT * 3)
    backend = FakeBackend(
        docs={"/big.md": {"frontmatter": {"title": "Big", "type": "Note"}, "body": huge_body}}
    )
    provider = ScriptedProvider(
        [
            LLMResponse(tool_calls=[tc("c1", "read_document", {"path": "/big.md"})]),
            LLMResponse(text="done"),
        ]
    )
    librarian = make_librarian(backend, provider)
    session = TraceSession(tmp_path, "20260728T000000Z-trace005", "request_knowledge", None)
    token = _trace_var.set(session)
    try:
        result = await librarian.handle_request("read the big one")
    finally:
        _trace_var.reset(token)
    session.finish("ok")
    trace_id = session.close()

    assert result["answer"] == "done"
    tool_message = provider.calls[1][0][-1]
    assert tool_message["role"] == "tool"
    assert "[truncated:" in tool_message["content"]
    assert len(tool_message["content"]) < TOOL_RESULT_CHAR_LIMIT + 100
    assert huge_body not in tool_message["content"]
    # the trace record is written before truncation and stays unaffected
    event = read_trace(tmp_path, trace_id)["events"][0]
    assert event["tool"] == "read_document"
    assert event["result"] == {"path": "/big.md", "title": "Big", "type": "Note"}


async def test_small_tool_results_not_truncated():
    """A11: results under the cap pass through unchanged (no marker)."""
    backend = FakeBackend(docs={"/alpha.md": dict(DOC, body="about alpha")})
    provider = ScriptedProvider(
        [
            LLMResponse(tool_calls=[tc("c1", "read_document", {"path": "/alpha.md"})]),
            LLMResponse(text="done"),
        ]
    )
    librarian = make_librarian(backend, provider)

    await librarian.handle_request("q")

    tool_message = provider.calls[1][0][-1]
    assert "[truncated:" not in tool_message["content"]
    assert "about alpha" in tool_message["content"]


async def test_unconfigured_librarian_raises(tmp_path):
    backend = FakeBackend()
    librarian = Librarian(tmp_path / "lib", LibrarianConfig(user_id="user-1"), backend=backend)
    assert not librarian.configured
    with pytest.raises(LibrarianNotConfiguredError):
        await librarian.handle_request("q")
    with pytest.raises(LibrarianNotConfiguredError):
        await librarian.handle_store("content")
    with pytest.raises(LibrarianNotConfiguredError):
        await librarian.handle_update("i")


# --- tracing / telemetry section ---------------------------------------------
#
# The loop records tool events into a TraceSession set via _trace_var and LLM
# usage/iterations into a RequestTelemetry set via _telemetry_var (plan 1.4).


def read_trace(tmp_path, trace_id: str) -> dict:
    return json.loads((tmp_path / ".traces" / f"{trace_id}.json").read_text(encoding="utf-8"))


async def test_first_message_is_effective_system_prompt():
    backend = FakeBackend()
    provider = ScriptedProvider([LLMResponse(text="ok")])
    librarian = make_librarian(backend, provider)

    await librarian.handle_request("q")

    # no override configured: the built-in default is the effective prompt
    assert librarian.system_prompt == DEFAULT_SYSTEM_PROMPT
    assert provider.calls[0][0][0] == {"role": "system", "content": DEFAULT_SYSTEM_PROMPT}


async def test_first_message_uses_system_prompt_with_addendum():
    backend = FakeBackend()
    provider = ScriptedProvider([LLMResponse(text="ok")])
    config = LibrarianConfig(
        user_id="user-1",
        llm=LLMConfig(provider="openai", model="m", api_key="k"),
        prompt_addendum="Custom prompt.",
    )
    librarian = Librarian("/unused-root", config, backend=backend, provider=provider)

    await librarian.handle_request("q")

    content = provider.calls[0][0][0]["content"]
    assert content.startswith(DEFAULT_SYSTEM_PROMPT)
    assert "Standing rules from the library owner:" in content
    assert content.rstrip().endswith("Custom prompt.")


async def test_trace_session_records_dispatched_events(tmp_path):
    backend = FakeBackend(docs={"/alpha.md": dict(DOC, body="about alpha")})
    provider = ScriptedProvider(
        [
            LLMResponse(tool_calls=[tc("c1", "list_dir", {"path": "/"})]),
            LLMResponse(tool_calls=[tc("c2", "read_document", {"path": "/alpha.md"})]),
            LLMResponse(text="done"),
        ]
    )
    librarian = make_librarian(backend, provider)
    session = TraceSession(tmp_path, "20260728T000000Z-trace001", "request_knowledge", "agent-x")
    token = _trace_var.set(session)
    try:
        await librarian.handle_request("what is alpha?", agent_label="agent-x")
    finally:
        _trace_var.reset(token)
    session.finish("ok")
    trace_id = session.close()

    data = read_trace(tmp_path, trace_id)
    # events mirror the LLM's dispatched tool calls in order (the extra
    # read_document in backend.calls is the post-processing re-read in
    # _concept_entries, which is not an LLM tool call and stays untraced)
    assert [event["tool"] for event in data["events"]] == ["list_dir", "read_document"]
    assert [call[0] for call in backend.calls[:2]] == ["list_dir", "read_document"]
    first, second = data["events"]
    assert (first["seq"], second["seq"]) == (1, 2)
    assert first["tool"] == "list_dir"
    assert first["args"] == {"path": "/"}
    assert first["result"] == {"path": "/", "entries": ["concepts"], "count": 1}
    assert first["error"] is None
    assert first["duration_ms"] >= 0
    assert second["tool"] == "read_document"
    assert second["result"] == {"path": "/alpha.md", "title": "Alpha", "type": "Note"}
    assert data["outcome"] == "ok"
    assert data["llm"] is None  # no telemetry set for this request


async def test_trace_records_failed_tool_call(tmp_path):
    backend = FakeBackend()
    provider = ScriptedProvider(
        [
            LLMResponse(tool_calls=[tc("c1", "read_document", {"path": "/missing.md"})]),
            LLMResponse(text="could not read it"),
        ]
    )
    librarian = make_librarian(backend, provider)
    session = TraceSession(tmp_path, "20260728T000000Z-trace002", "request_knowledge", None)
    token = _trace_var.set(session)
    try:
        result = await librarian.handle_request("q")
    finally:
        _trace_var.reset(token)
    session.finish("ok")
    trace_id = session.close()

    # the failed hop is recorded, then re-raised so the loop feeds it back
    assert result["answer"] == "could not read it"
    event = read_trace(tmp_path, trace_id)["events"][0]
    assert event["tool"] == "read_document"
    assert event["result"] is None
    assert event["error"].startswith("KeyError: ")
    tool_message = provider.calls[1][0][-1]
    assert tool_message["role"] == "tool"
    assert "KeyError" in tool_message["content"]


async def test_trace_records_per_step_llm_ms(tmp_path):
    """Each tool event that follows a completion carries that completion's
    wall time; the telemetry total also covers the final-answer completion."""
    backend = FakeBackend(docs={"/alpha.md": dict(DOC, body="about alpha")})
    provider = ScriptedProvider(
        [
            LLMResponse(tool_calls=[tc("c1", "list_dir", {"path": "/"})]),
            LLMResponse(tool_calls=[tc("c2", "read_document", {"path": "/alpha.md"})]),
            LLMResponse(text="done"),
        ]
    )
    librarian = make_librarian(backend, provider)
    session = TraceSession(tmp_path, "20260728T000000Z-trace006", "request_knowledge", None)
    telemetry = RequestTelemetry(trace_id="20260728T000000Z-trace006")
    trace_token = _trace_var.set(session)
    telemetry_token = _telemetry_var.set(telemetry)
    try:
        await librarian.handle_request("what is alpha?")
    finally:
        _trace_var.reset(trace_token)
        _telemetry_var.reset(telemetry_token)
    session.finish("ok")
    trace_id = session.close()

    first, second = read_trace(tmp_path, trace_id)["events"]
    assert first["llm_ms"] >= 0
    assert second["llm_ms"] >= 0
    # the total also covers the final-answer completion, which has no
    # following tool event (ScriptedProvider timing is real wall time)
    assert telemetry.llm["llm_ms_total"] >= first["llm_ms"] + second["llm_ms"]


async def test_trace_llm_ms_attaches_to_first_event_of_batch_only(tmp_path):
    """One LLM response can carry multiple tool calls: its wall time lands on
    the FIRST resulting event only, so sums over events never double-count."""
    backend = FakeBackend(docs={"/alpha.md": dict(DOC, body="about alpha")})
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc("c1", "list_dir", {"path": "/"}),
                    tc("c2", "read_document", {"path": "/alpha.md"}),
                ]
            ),
            LLMResponse(text="done"),
        ]
    )
    librarian = make_librarian(backend, provider)
    session = TraceSession(tmp_path, "20260728T000000Z-trace007", "request_knowledge", None)
    token = _trace_var.set(session)
    try:
        await librarian.handle_request("what is alpha?")
    finally:
        _trace_var.reset(token)
    session.finish("ok")
    trace_id = session.close()

    first, second = read_trace(tmp_path, trace_id)["events"]
    assert first["llm_ms"] >= 0
    assert "llm_ms" not in second


async def test_telemetry_collects_usage_across_completions_including_cap_path():
    def usage(prompt, completion):
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        }

    backend = FakeBackend()
    provider = ScriptedProvider(
        [
            LLMResponse(tool_calls=[tc("c1", "list_dir")], usage=usage(10, 2)),
            LLMResponse(tool_calls=[tc("c2", "list_dir")], usage=usage(20, 3)),
            LLMResponse(text="final", usage=usage(30, 4)),
        ]
    )
    librarian = make_librarian(backend, provider, max_iterations=1)
    telemetry = RequestTelemetry(trace_id="20260728T000000Z-trace003")
    token = _telemetry_var.set(telemetry)
    try:
        await librarian.handle_request("q")
    finally:
        _telemetry_var.reset(token)

    # initial + 1 tool round + cap-exhaustion final call = 3 completions
    assert len(provider.calls) == 3
    assert telemetry.iterations == 1
    assert len(telemetry.llm_calls) == 3
    llm_ms_total = telemetry.llm.pop("llm_ms_total")
    assert llm_ms_total >= 0
    assert telemetry.llm == {
        "provider": "openai",
        "model": "m",
        "iterations": 1,
        "prompt_tokens": 60,
        "completion_tokens": 9,
        "total_tokens": 69,
    }


async def test_telemetry_without_reported_usage():
    backend = FakeBackend()
    provider = ScriptedProvider([LLMResponse(text="ok")])
    librarian = make_librarian(backend, provider)
    telemetry = RequestTelemetry(trace_id="20260728T000000Z-trace004")
    token = _telemetry_var.set(telemetry)
    try:
        await librarian.handle_request("q")
    finally:
        _telemetry_var.reset(token)

    assert telemetry.iterations == 0
    assert len(telemetry.llm_calls) == 1
    llm_ms_total = telemetry.llm.pop("llm_ms_total")
    assert llm_ms_total >= 0
    assert telemetry.llm == {
        "provider": "openai",
        "model": "m",
        "iterations": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


async def test_f11_nudge_retry_telemetry_accumulates_across_runs():
    """AGENT-11: the F11 nudge retry's iterations/tokens ADD to the first
    run's instead of replacing them in the journal/trace summary."""

    def usage(prompt, completion):
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        }

    backend = FakeBackend()
    provider = ScriptedProvider(
        [
            # first run: one tool round, no writes
            LLMResponse(tool_calls=[tc("c1", "list_dir")], usage=usage(10, 2)),
            LLMResponse(text="nothing to write", usage=usage(20, 3)),
            # nudge retry: one write round
            LLMResponse(
                tool_calls=[
                    tc(
                        "c2",
                        "write_concept",
                        {
                            "path": "/n.md",
                            "frontmatter": {"title": "N", "type": "Note"},
                            "body": "b",
                        },
                    )
                ],
                usage=usage(30, 4),
            ),
            LLMResponse(text="wrote it", usage=usage(40, 5)),
        ]
    )
    librarian = make_librarian(backend, provider)
    telemetry = RequestTelemetry(trace_id="20260728T000000Z-trace005")
    token = _telemetry_var.set(telemetry)
    try:
        result = await librarian.handle_store("knowledge")
    finally:
        _telemetry_var.reset(token)

    assert result["stored"] == [{"id": "/n", "title": "N", "action": "created"}]
    assert len(provider.calls) == 4  # two completions per run
    assert telemetry.iterations == 2  # 1 + 1, summed across both runs
    assert len(telemetry.llm_calls) == 4
    llm_ms_total = telemetry.llm.pop("llm_ms_total")
    assert llm_ms_total >= 0  # accumulated across both runs, never overwritten
    assert telemetry.llm == {
        "provider": "openai",
        "model": "m",
        "iterations": 2,
        "prompt_tokens": 100,
        "completion_tokens": 14,
        "total_tokens": 114,
    }


# --- real backend section ------------------------------------------------------
#
# Drives handlers and lazy startup reconcile against a real LibraryBackend on
# tmp_path (plan section 5 item 7, plan Step 4).

ACTOR = "athenaeum-librarian/0.1.0"


def make_real_backend(tmp_path) -> LibraryBackend:
    backend = LibraryBackend(tmp_path / "lib", actor=ACTOR)
    backend.init_bundle()
    return backend


async def test_store_links_after_against_real_backend(tmp_path):
    """links_after against a real LibraryBackend: the real link graph agrees."""
    backend = make_real_backend(tmp_path)
    backend.create_concept("/home.md", {"type": "Note", "title": "Home"}, "[A](/a.md)\n")
    backend.create_concept("/a.md", {"type": "Note", "title": "A"}, "a\n")
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc(
                        "c1",
                        "write_concept",
                        {
                            "path": "/new.md",
                            "frontmatter": {"title": "New", "type": "Note"},
                            "body": "new knowledge\n",
                        },
                    )
                ]
            ),
            LLMResponse(
                tool_calls=[
                    tc(
                        "c2",
                        "edit_concept",
                        {"path": "/a.md", "new_body": "a\n\nSee [New](/new.md).\n"},
                    )
                ]
            ),
            LLMResponse(text="Stored /new.md, back-linked from /a.md."),
        ]
    )
    librarian = make_librarian(backend, provider)

    result = await librarian.handle_store("knowledge")

    assert result["links_after"]["healthy"] is True
    assert "/new" in result["links_after"]["checked"]


async def test_store_with_images_attaches_asset_links(tmp_path):
    """handle_store writes image assets server-side (real backend); the task
    text carries the absolute markdown image links, never the payload."""
    backend = make_real_backend(tmp_path)
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc(
                        "c1",
                        "write_concept",
                        {
                            "path": "/new.md",
                            "frontmatter": {"title": "New", "type": "Note"},
                            "body": "new knowledge\n",
                        },
                    )
                ]
            ),
            LLMResponse(text="Stored /new.md with the diagram."),
        ]
    )
    librarian = make_librarian(backend, provider)

    result = await librarian.handle_store(
        "knowledge",
        images=[{"filename": "diagram.png", "media_type": "image/png", "data": b"\x89PNG fake"}],
    )

    assets = list((tmp_path / "lib" / ".athenaeum" / "assets").glob("*.png"))
    assert len(assets) == 1
    assert assets[0].read_bytes() == b"\x89PNG fake"
    assert assets[0].name.endswith("-diagram.png")
    task = provider.calls[0][0][1]["content"]
    assert "Attached images" in task
    assert f"![diagram.png](/.athenaeum/assets/{assets[0].name})" in task
    assert result["stored"] == [{"id": "/new", "title": "New", "action": "created"}]


def test_lazy_reconcile_repairs_drifted_index(tmp_path):
    """_build_backend reconciles an existing root (plan Step 4 lazy pass)."""
    backend = make_real_backend(tmp_path)
    backend.create_concept("/a.md", {"type": "Concept", "title": "A"}, "x\n")
    index_path = tmp_path / "lib" / "index.md"
    index_path.write_text("# Documents\n* [Stale](stale.md)\n", encoding="utf-8")
    assert any(w["code"] == "index-drift" for w in backend.validate()["warnings"])

    # no backend= injection: forces _build_backend on an existing root
    librarian = Librarian(str(tmp_path / "lib"), LibrarianConfig(user_id="user-1"))

    warnings = librarian.backend.validate()["warnings"]
    assert not any(w["code"] == "index-drift" for w in warnings)
    assert "a.md" in index_path.read_text(encoding="utf-8")


def test_reconcile_failure_does_not_break_creation(tmp_path, monkeypatch):
    """A reconcile failure is logged and swallowed; creation still succeeds."""
    make_real_backend(tmp_path)

    def boom(self):
        raise RuntimeError("reconcile exploded")

    monkeypatch.setattr(LibraryBackend, "reconcile", boom)
    librarian = Librarian(str(tmp_path / "lib"), LibrarianConfig(user_id="user-1"))

    assert librarian.backend.read_document("/index.md")


# --- CS-11: previously silent swallows now log -------------------------------


async def test_concept_entries_read_failure_logs_warning(caplog):
    backend = FakeBackend()  # no docs: read_document raises KeyError
    librarian = make_librarian(backend, ScriptedProvider([]))
    tracker = _Tracker(read_paths=["/gone.md"])
    with caplog.at_level(logging.WARNING, logger="athenaeum.librarian.agent"):
        entries = await librarian._concept_entries(tracker)
    assert entries == []
    assert "concept entry: read failed for /gone.md" in caplog.text


async def test_stored_entries_title_lookup_failure_logs_warning(caplog):
    backend = FakeBackend()
    librarian = make_librarian(backend, ScriptedProvider([]))
    tracker = _Tracker(writes=[{"id": "/gone", "action": "created"}])
    with caplog.at_level(logging.WARNING, logger="athenaeum.librarian.agent"):
        stored = await librarian._stored_entries(tracker)
    assert stored == [{"id": "/gone", "title": "/gone", "action": "created"}]
    assert "title lookup failed for /gone" in caplog.text


async def test_related_section_read_failure_logs_warning(caplog):
    class FakeEmbed:
        async def related(self, text, top_k):
            return [("/gone", 0.9)]

    backend = FakeBackend()
    librarian = make_librarian(backend, ScriptedProvider([]))
    librarian._embed = FakeEmbed()
    with caplog.at_level(logging.WARNING, logger="athenaeum.librarian.agent"):
        section = await librarian._related_section("text")
    assert section == ""
    assert "related-concepts: read failed for /gone" in caplog.text
