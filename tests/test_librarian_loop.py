"""Tests for the librarian agent loop (plan stream-B checklist).

Uses a scripted fake LLMProvider and an in-memory LibraryBackend stand-in
conforming to plan section 3.2. The "real backend" section at the bottom
drives the handlers and the lazy startup reconcile against a real
LibraryBackend on tmp_path (plan section 5 item 7, plan Step 4).
"""

import dataclasses
import hashlib
import json
import logging
import re
import tempfile

import pytest

from athenaeum.librarian.agent import (
    CURATOR_VERIFIER,
    FINAL_ANSWER_REQUEST,
    KIND_LIBRARIAN,
    MAX_PAYLOAD_EXCERPT,
    MAX_STORE_PAYLOAD_REVIEWS,
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


async def test_maintain_noop_when_healthy_without_llm_call():
    backend = FakeBackend(healthy=True)
    provider = ScriptedProvider([LLMResponse(text="should not be used")])
    librarian = make_librarian(backend, provider)

    result = await librarian.handle_maintain()

    assert result == {
        "actions": [],
        "summary": "Library is healthy; no maintenance needed.",
        "healthy": True,
        "verified": [],
    }
    assert provider.calls == []  # no LLM call on the no-op path


async def test_maintain_runs_loop_on_orphaned_bundle():
    docs = {
        "/orphan.md": {"frontmatter": {"title": "Orphan", "type": "Note"}, "body": "o"},
        "/hub.md": {"frontmatter": {"title": "Hub", "type": "Note"}, "body": "h"},
    }
    backend = FakeBackend(docs=docs, healthy=False)
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc(
                        "c1",
                        "edit_concept",
                        {"path": "/hub.md", "new_body": "h\n\nSee [/orphan.md]."},
                    )
                ]
            ),
            LLMResponse(text="Wired the orphan into the hub."),
        ]
    )
    librarian = make_librarian(backend, provider)

    result = await librarian.handle_maintain("fix orphans", agent_label="agent-y")

    assert provider.calls, "maintenance loop must run on an unhealthy bundle"
    assert result["summary"] == (
        "Wired the orphan into the hub."
        "\n\nPost-run check: the library still has open health issues."
        "\n\nPost-run verification: machine-confirmed 1 repaired concept(s)."
    )
    assert result["actions"] == [{"id": "/hub", "title": "Hub", "action": "updated"}]
    # the deterministic post-step machine-confirmed exactly the repaired concept
    assert result["verified"] == [{"id": "/hub", "by": CURATOR_VERIFIER}]
    assert ("verify_concept", "/hub.md", CURATOR_VERIFIER, "agent-y") in backend.calls
    assert result["healthy"] is False  # fake backend stays unhealthy
    assert ("edit_concept", "/hub.md", "agent-y") in backend.calls
    # preamble carries the health report + caller instructions
    task_prompt = provider.calls[0][0][1]["content"]
    assert "/orphan" in task_prompt and "fix orphans" in task_prompt


async def test_maintain_verifies_only_updated_writes():
    """Creations, moves, deprecations, deletes are NOT machine-confirmed."""
    docs = {
        "/orphan.md": {"frontmatter": {"title": "Orphan", "type": "Note"}, "body": "o"},
        "/old.md": {"frontmatter": {"title": "Old", "type": "Note"}, "body": "o"},
    }
    backend = FakeBackend(docs=docs, healthy=False)
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
                            "body": "n",
                        },
                    ),
                    tc("c2", "deprecate_concept", {"path": "/old.md"}),
                    tc("c3", "move_concept", {"old_path": "/orphan.md", "new_path": "/moved.md"}),
                ]
            ),
            LLMResponse(text="done"),
        ]
    )
    librarian = make_librarian(backend, provider)

    result = await librarian.handle_maintain()

    assert result["verified"] == []
    assert "Post-run verification" not in result["summary"]
    assert not any(call[0] == "verify_concept" for call in backend.calls)


async def test_maintain_verification_failure_never_fails_run(monkeypatch):
    """A verify_concept failure is logged and skipped; the run still succeeds."""
    docs = {"/hub.md": {"frontmatter": {"title": "Hub", "type": "Note"}, "body": "h"}}
    backend = FakeBackend(docs=docs, healthy=False)

    def boom(path, *, by, at=None, agent_label=None):
        raise RuntimeError("verify exploded")

    monkeypatch.setattr(backend, "verify_concept", boom)
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[tc("c1", "edit_concept", {"path": "/hub.md", "new_body": "h2"})]
            ),
            LLMResponse(text="repaired"),
        ]
    )
    librarian = make_librarian(backend, provider)

    result = await librarian.handle_maintain()

    assert result["verified"] == []
    assert result["actions"] == [{"id": "/hub", "title": "Hub", "action": "updated"}]
    assert "Post-run verification" not in result["summary"]


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


async def test_curate_noop_when_well_organized_without_llm_call(tmp_path):
    backend = FakeBackend()
    provider = ScriptedProvider([LLMResponse(text="should not be used")])
    librarian = make_librarian(backend, provider)
    backend.scan_root = tmp_path  # empty library root: no findings

    result = await librarian.handle_curate()

    assert result["actions"] == []
    assert result["summary"] == "Library is well-organized; nothing to curate."
    assert result["organized"] is True
    assert result["verified"] == []
    assert result["findings"]["concepts_scanned"] == 0
    assert result["health_after"] == {"healthy": True, "orphans": 0, "broken_links": 0}
    assert provider.calls == []  # no LLM call on the no-op path


async def test_curate_runs_loop_on_findings(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    (root / "thin.md").write_text("---\ntype: Note\ntitle: Thin\n---\nstub\n", encoding="utf-8")
    docs = {"/thin.md": {"frontmatter": {"title": "Thin", "type": "Note"}, "body": "stub"}}
    backend = FakeBackend(docs=docs)
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[tc("c1", "edit_concept", {"path": "/thin.md", "new_body": "enriched"})]
            ),
            LLMResponse(text="Enriched the thin concept."),
        ]
    )
    librarian = make_librarian(backend, provider)
    backend.scan_root = root

    result = await librarian.handle_curate("tidy up", agent_label="agent-c")

    assert provider.calls, "curate loop must run when findings exist"
    assert result["summary"] == (
        "Enriched the thin concept."
        "\n\nPost-run check: open findings remain (see 'findings'); unaddressed "
        "findings are re-reported on the next run until fixed."
        "\n\nPost-run verification: machine-confirmed 1 repaired concept(s)."
    )
    assert result["actions"] == [{"id": "/thin", "title": "Thin", "action": "updated"}]
    # the same deterministic post-step backs curate runs (happy path)
    assert result["verified"] == [{"id": "/thin", "by": CURATOR_VERIFIER}]
    assert result["organized"] is False  # scanned files unchanged by the fake backend
    # L15: findings are the POST-run report (same epoch as 'organized')
    assert result["findings"]["thin_concepts"] == [
        {"id": "/thin", "title": "Thin", "body_chars": 4}
    ]
    assert result["health_after"] == {"healthy": True, "orphans": 0, "broken_links": 0}
    assert ("edit_concept", "/thin.md", "agent-c") in backend.calls
    # preamble carries the findings report + caller instructions
    task_prompt = provider.calls[0][0][1]["content"]
    assert "CURATION TASK" in task_prompt
    assert "/thin" in task_prompt and "tidy up" in task_prompt


async def test_curate_preamble_includes_addendum(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    (root / "thin.md").write_text("---\ntype: Note\ntitle: Thin\n---\nstub\n", encoding="utf-8")
    backend = FakeBackend()
    provider = ScriptedProvider([LLMResponse(text="done")])
    config = LibrarianConfig(
        user_id="user-1",
        llm=LLMConfig(provider="openai", model="m", api_key="k"),
        curate_prompt_addendum="never create concepts",
    )
    librarian = Librarian(root, config, backend=backend, provider=provider)
    backend.scan_root = root

    await librarian.handle_curate()

    task_prompt = provider.calls[0][0][1]["content"]
    assert "Standing curation rules from the library owner:" in task_prompt
    assert "never create concepts" in task_prompt


async def test_curate_uses_default_llm_without_override(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    (root / "thin.md").write_text("---\ntype: Note\ntitle: Thin\n---\nstub\n", encoding="utf-8")
    backend = FakeBackend()
    provider = ScriptedProvider([LLMResponse(text="done")])
    librarian = make_librarian(backend, provider)
    backend.scan_root = root

    await librarian.handle_curate()

    # no override: the provider sees the base LLM config, byte-identical to today
    assert provider.calls[0][2] is librarian.config.llm
    assert librarian._curate_provider is None


async def test_curate_model_override_effective_config(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    (root / "thin.md").write_text("---\ntype: Note\ntitle: Thin\n---\nstub\n", encoding="utf-8")
    backend = FakeBackend()
    base = LLMConfig(provider="openai", model="m", api_key="k")
    curate_llm = LLMConfig(provider="anthropic", model="big-model", api_key="k2")
    config = LibrarianConfig(user_id="user-1", llm=base, curate_llm=curate_llm)
    default_provider = ScriptedProvider([LLMResponse(text="should not be used")])
    curate_provider = ScriptedProvider([LLMResponse(text="curated")])
    librarian = Librarian(root, config, backend=backend, provider=default_provider)
    librarian._curate_provider = curate_provider
    backend.scan_root = root

    result = await librarian.handle_curate()

    assert result["summary"].startswith("curated")
    assert "Post-run check:" in result["summary"]
    assert default_provider.calls == []
    effective = curate_provider.calls[0][2]
    assert effective.provider == "anthropic"
    assert effective.model == "big-model"
    assert effective.api_key == "k2"  # the curator's connection has its own credentials


def test_curate_llm_partial_override_and_default():
    base = LLMConfig(provider="openai", model="m", api_key="k")
    curate_llm = LLMConfig(provider="anthropic", model="big-model", api_key="k2")
    librarian = Librarian(
        "/unused-root",
        LibrarianConfig(user_id="user-1", llm=base, curate_llm=curate_llm),
        backend=FakeBackend(),
    )
    assert librarian._curate_llm() is curate_llm

    plain = Librarian(
        "/unused-root", LibrarianConfig(user_id="user-1", llm=base), backend=FakeBackend()
    )
    assert plain._curate_llm() is base


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
    assert telemetry.llm == {
        "provider": "openai",
        "model": "m",
        "iterations": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
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


async def test_maintain_against_real_backend(tmp_path):
    """handle_maintain against a real LibraryBackend (regression: D1 crash).

    Before the status() orphans-shape fix this crashed with
    AttributeError: 'str' object has no attribute 'get'.
    """
    backend = make_real_backend(tmp_path)
    backend.create_concept("/orphan.md", {"type": "Note", "title": "Orphan"}, "o\n")
    backend.create_concept("/hub.md", {"type": "Note", "title": "Hub"}, "h\n")
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc(
                        "c1",
                        "edit_concept",
                        {"path": "/hub.md", "new_body": "h\n\nSee [Orphan](/orphan.md).\n"},
                    )
                ]
            ),
            LLMResponse(text="Wired the orphan into the hub."),
        ]
    )
    librarian = make_librarian(backend, provider)

    result = await librarian.handle_maintain("fix orphans")

    assert result["summary"] == (
        "Wired the orphan into the hub.\n\nPost-run check: the library is now healthy."
        "\n\nPost-run verification: machine-confirmed 1 repaired concept(s)."
    )
    assert result["actions"] == [{"id": "/hub", "title": "Hub", "action": "updated"}]
    # real backend: the repaired concept's frontmatter gained the verified entry
    assert result["verified"] == [{"id": "/hub", "by": CURATOR_VERIFIER}]
    verified = backend.read_document("/hub.md")["frontmatter"]["verified"]
    assert [entry["by"] for entry in verified] == [CURATOR_VERIFIER]
    # real backend re-validates after the edit: /orphan gained an inbound
    # link, /hub an outbound one, and no broken links remain
    assert result["healthy"] is True
    # the dict-shaped orphan entry rendered into the preamble
    preamble = provider.calls[0][0][1]["content"]
    assert "/orphan" in preamble and "Orphan" in preamble


async def test_curate_against_real_backend(tmp_path):
    """handle_curate against a real LibraryBackend: preamble + post-run organized."""
    backend = make_real_backend(tmp_path)
    backend.create_concept("/stub.md", {"type": "Note", "title": "Stub"}, "s\n")
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[tc("c1", "edit_concept", {"path": "/stub.md", "new_body": "x" * 250})]
            ),
            LLMResponse(text="Enriched the stub."),
        ]
    )
    librarian = make_librarian(backend, provider)

    result = await librarian.handle_curate()

    assert result["summary"] == (
        "Enriched the stub."
        "\n\nPost-run check: no open findings remain; the library is well-organized."
        "\n\nPost-run verification: machine-confirmed 1 repaired concept(s)."
    )
    assert result["actions"] == [{"id": "/stub", "title": "Stub", "action": "updated"}]
    assert result["verified"] == [{"id": "/stub", "by": CURATOR_VERIFIER}]
    # the enrichment cleared the only finding: converged on the post-run scan
    assert result["organized"] is True
    # L15: findings are the POST-run report — the fixed thin concept is gone
    assert result["findings"]["thin_concepts"] == []
    # the enriched /stub has no bundle links, so the real validator reports it
    # as an orphan (validate.py) — post-curate observability via health_after
    assert result["health_after"] == {"healthy": False, "orphans": 1, "broken_links": 0}
    preamble = provider.calls[0][0][1]["content"]
    assert "CURATION TASK" in preamble
    assert "/stub" in preamble and "Stub" in preamble


# --- curate content-hygiene sweep (F25 stock repair) ---------------------------


async def test_curate_hygiene_repairs_dirty_concept_without_llm(tmp_path):
    """The deterministic sweep repairs a dirty on-disk body before the
    findings scan: no LLM call, one 'updated' action, no verify receipts."""
    backend = make_real_backend(tmp_path)
    dirty_body = "prose with escape \\u2011 here. " * 10 + "\n"
    (tmp_path / "lib" / "a.md").write_text(
        "---\ntype: Note\ntitle: Alpha\n---\n" + dirty_body,
        encoding="utf-8",
    )
    provider = ScriptedProvider([LLMResponse(text="should not be used")])
    librarian = make_librarian(backend, provider)

    result = await librarian.handle_curate()

    body = backend.read_document("/a.md")["body"]
    assert "\\u" not in body
    assert "‑" in body
    assert result["actions"] == [{"id": "/a", "title": "Alpha", "action": "updated"}]
    assert provider.calls == []  # D6: hygiene repair alone never wakes the LLM
    assert result["verified"] == []  # no receipts for deterministic repairs
    assert result["summary"] == (
        "Library is well-organized; nothing to curate."
        "\n\nContent hygiene: decoded literal unicode escape artifacts in "
        "1 existing concept(s) (F25 stock repair)."
    )
    assert "hygiene_repairs" not in result  # repairs merge into 'actions'
    assert result["organized"] is True


async def test_curate_hygiene_prefilter_leaves_fence_only_file_untouched(tmp_path):
    """Escapes confined to a fenced block skip the deterministic sweep but
    become code-span escape candidates: the D6 gate opens and the curator LLM
    IS called. A curator judging "intentional" (text-only response, no tool
    calls) leaves the file byte-identical; the structural finding is
    re-reported post-run (L14)."""
    backend = make_real_backend(tmp_path)
    body = "```text\n" + "DP\\u20111 " * 30 + "\n```\n"
    target = tmp_path / "lib" / "a.md"
    target.write_text("---\ntype: Note\ntitle: Alpha\n---\n" + body, encoding="utf-8")
    before = target.read_bytes()
    provider = ScriptedProvider(
        [LLMResponse(text="Intentional documentation of the escape format; left unchanged.")]
    )
    librarian = make_librarian(backend, provider)

    result = await librarian.handle_curate()

    assert provider.calls  # a code-span candidate wakes the curator (D6 gate)
    preamble = provider.calls[0][0][1]["content"]
    assert "code-span escape candidates" in preamble
    assert "/a.md" in preamble
    assert result["actions"] == []  # no sweep repair, no LLM write
    assert target.read_bytes() == before  # no rewrite, no commit
    # L14 re-report: the confirmed-intentional literals stay on the findings
    candidates = result["findings"]["code_span_escape_candidates"]
    assert [c["path"] for c in candidates] == ["/a.md"]
    assert result["organized"] is False


async def test_curate_repairs_code_span_escape_candidate(tmp_path):
    """A curator judging "artifact" repairs the candidate via edit_concept
    with the real characters: the post-run rescan (L15) finds nothing left
    and the repair is machine-confirmed."""
    backend = make_real_backend(tmp_path)
    body = "```text\n" + "DP\\u20111 " * 30 + "\n```\n"
    (tmp_path / "lib" / "a.md").write_text(
        "---\ntype: Note\ntitle: Alpha\n---\n" + body, encoding="utf-8"
    )
    fixed_body = "```text\n" + "DP\u20111 " * 60 + "\n```\n"  # decoded, stays >200 chars
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[tc("c1", "edit_concept", {"path": "/a.md", "new_body": fixed_body})]
            ),
            LLMResponse(text="Replaced the escape artifacts with real characters."),
        ]
    )
    librarian = make_librarian(backend, provider)

    result = await librarian.handle_curate()

    assert provider.calls
    assert backend.read_document("/a.md")["body"] == fixed_body
    assert result["findings"]["code_span_escape_candidates"] == []
    assert result["organized"] is True
    assert {"id": "/a", "by": CURATOR_VERIFIER} in result["verified"]


async def test_curate_hygiene_multiple_dirty_files(tmp_path):
    """Every dirty concept is repaired in one run (N files = N commits)."""
    backend = make_real_backend(tmp_path)
    dirty_body = "prose with escape \\u2011 here. " * 10 + "\n"
    (tmp_path / "lib" / "a.md").write_text(
        "---\ntype: Note\ntitle: Alpha\n---\n" + dirty_body,
        encoding="utf-8",
    )
    (tmp_path / "lib" / "b.md").write_text(
        "---\ntype: Note\ntitle: Beta\n---\n" + dirty_body,
        encoding="utf-8",
    )
    provider = ScriptedProvider([LLMResponse(text="should not be used")])
    librarian = make_librarian(backend, provider)

    result = await librarian.handle_curate()

    for path in ("/a.md", "/b.md"):
        body = backend.read_document(path)["body"]
        assert "\\u" not in body
        assert "‑" in body
    assert result["actions"] == [
        {"id": "/a", "title": "Alpha", "action": "updated"},
        {"id": "/b", "title": "Beta", "action": "updated"},
    ]
    assert provider.calls == []


async def test_curate_deprecated_cleanup_finding_reaches_curator(tmp_path):
    """A deprecated concept with no live inbound links is a cleanup finding:
    it wakes the curator (scripted delete_concept) and converges post-run."""
    backend = make_real_backend(tmp_path)
    backend.create_concept("/old.md", {"type": "Note", "title": "Old"}, "stale\n")
    backend.deprecate_concept("/old.md")
    provider = ScriptedProvider(
        [
            LLMResponse(tool_calls=[tc("c1", "delete_concept", {"path": "/old.md"})]),
            LLMResponse(text="Deleted the deprecated concept."),
        ]
    )
    librarian = make_librarian(backend, provider)

    result = await librarian.handle_curate()

    assert provider.calls, "a cleanup finding must wake the curator"
    preamble = provider.calls[0][0][1]["content"]
    assert "deprecated concepts pending cleanup" in preamble
    assert "- /old (Old)" in preamble
    assert [a["id"] for a in result["actions"]] == ["/old"]
    assert result["actions"][0]["action"] == "deleted"
    # post-run scan: the deprecated concept is gone, nothing left to clean
    assert result["organized"] is True
    assert result["findings"]["deprecated_cleanup"] == []


# --- curate store-payload digest (D3.6, 0.20.0) ---------------------------------


def payload_record(
    request_id: str,
    *,
    outcome: str = "error",
    received_at: str = "2026-08-01T00:00:00+00:00",
    content: str = "c",
) -> dict:
    return {
        "request_id": request_id,
        "tool": "store_knowledge",
        "user_id": "user-1",
        "agent_label": "agent-a",
        "trace_id": "20260801T000000Z-deadbeef",
        "received_at": received_at,
        "outcome": outcome,
        "error": "LibrarianNoWriteError" if outcome == "error" else None,
        "params": {"content": content},
        "stored": [],
    }


async def test_curate_wakes_on_failed_store_payload_once(tmp_path):
    """D3.6: a failed store payload is a one-shot curate finding — it wakes
    a paid run and is consumed by it (post-run key empty, organized True)."""
    backend = FakeBackend()
    store_provider = ScriptedProvider([LLMResponse(text="no tools")])
    storer = make_librarian(backend, store_provider, root=tmp_path / "lib")
    with pytest.raises(LibrarianNoWriteError):
        await storer.handle_store("unstored knowledge about zeta")

    curator_provider = ScriptedProvider([LLMResponse(text="Reviewed the failed payload.")])
    curator = make_librarian(backend, curator_provider, root=tmp_path / "lib")
    # the payload archive lives under a dot-dir: structural scans stay empty
    backend.scan_root = tmp_path / "lib"

    result = await curator.handle_curate()

    assert curator_provider.calls, "a payload-only finding must wake a curate run"
    preamble = curator_provider.calls[0][0][1]["content"]
    assert "store payloads pending review" in preamble
    assert "unstored knowledge about zeta" in preamble
    assert result["organized"] is True
    # one-shot: the post-run report never re-lists the consumed payload
    assert result["findings"]["store_payload_reviews"] == []


async def test_curate_skips_payloads_older_than_last_run(tmp_path):
    """The time filter is payload-store-local (D3.6/R22): payloads received
    before curate_last_run_at are not reported — each payload is reported
    exactly once because curate_last_run_at advances."""
    backend = FakeBackend()
    provider = ScriptedProvider([LLMResponse(text="unused")])
    config = LibrarianConfig(
        user_id="user-1",
        llm=LLMConfig(provider="openai", model="m", api_key="k"),
        curate_last_run_at="2026-08-02T00:00:00+00:00",
    )
    librarian = Librarian(tmp_path / "lib", config, backend=backend, provider=provider)
    backend.scan_root = tmp_path / "lib"
    PayloadStore(tmp_path / "lib").create(
        payload_record(
            "20260801T000000Z-aaaa1111",
            received_at="2026-08-01T00:00:00+00:00",  # before the baseline
            content="old failed store",
        )
    )

    result = await librarian.handle_curate()

    assert provider.calls == []  # nothing wakes the curator
    assert result["organized"] is True
    assert result["summary"] == "Library is well-organized; nothing to curate."


def test_store_payload_reviews_digest_bounds(tmp_path):
    """The digest keeps the MAX_STORE_PAYLOAD_REVIEWS newest error/partial
    records and bounds excerpts at MAX_PAYLOAD_EXCERPT chars."""
    config = LibrarianConfig(
        user_id="user-1",
        llm=LLMConfig(provider="openai", model="m", api_key="k"),
    )
    librarian = Librarian(
        tmp_path / "lib", config, backend=FakeBackend(), provider=ScriptedProvider([])
    )
    store = PayloadStore(tmp_path / "lib")
    for day in range(1, 8):
        store.create(
            payload_record(
                f"2026080{day}T000000Z-aaaa111{day}",
                received_at=f"2026-08-0{day}T00:00:00+00:00",
                content="x" * 200,
            )
        )
    store.create(payload_record("20260808T000000Z-aaaa1118", outcome="ok"))
    store.create(payload_record("20260809T000000Z-aaaa1119", outcome="busy"))

    reviews = librarian._store_payload_reviews()

    # ok/busy outcomes are excluded; newest first, capped at 5
    assert [r["request_id"] for r in reviews] == [
        "20260807T000000Z-aaaa1117",
        "20260806T000000Z-aaaa1116",
        "20260805T000000Z-aaaa1115",
        "20260804T000000Z-aaaa1114",
        "20260803T000000Z-aaaa1113",
    ]
    assert len(reviews) == MAX_STORE_PAYLOAD_REVIEWS
    assert all(r["outcome"] == "error" for r in reviews)
    assert all(len(r["excerpt"]) == MAX_PAYLOAD_EXCERPT for r in reviews)


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


async def test_curate_unaddressed_findings_persist_across_runs(tmp_path):
    """L14: an unaddressed finding is re-reported on the next curate run.

    Regression shape: the baseline (curate_last_run_at) is AFTER the finding
    was created — the old changed-set scoping made the finding vanish and run
    N+1 claimed "well-organized".
    """
    root = tmp_path / "lib"
    root.mkdir()
    (root / "thin.md").write_text(
        "---\ntype: Note\ntitle: Thin\ngenerated: {at: '2026-01-01T00:00:00+00:00'}\n---\nstub\n",
        encoding="utf-8",
    )
    backend = FakeBackend()
    provider = ScriptedProvider(
        [
            LLMResponse(text="did nothing"),  # run 1: LLM does not fix it
            LLMResponse(text="did nothing again"),  # run 2
        ]
    )
    librarian = make_librarian(backend, provider)
    backend.scan_root = root
    librarian.config.curate_last_run_at = "2999-01-01T00:00:00+00:00"  # the amnesia condition

    first = await librarian.handle_curate()
    second = await librarian.handle_curate()

    # both runs invoked the LLM (no false "well-organized" no-op)...
    assert len(provider.calls) == 2
    # ...and both report the still-open finding (post-run epoch)
    for result in (first, second):
        assert result["organized"] is False
        assert [c["id"] for c in result["findings"]["thin_concepts"]] == ["/thin"]
        assert "open findings remain" in result["summary"]


async def test_curate_summary_and_findings_share_post_run_epoch(tmp_path):
    """L15 (F18): fixed items are not reported as remaining — one epoch."""
    root = tmp_path / "lib"
    root.mkdir()
    thin = root / "thin.md"
    thin.write_text("---\ntype: Note\ntitle: Thin\n---\nstub\n", encoding="utf-8")

    class FixingProvider:
        """Fixes the finding on disk as part of the scripted tool round."""

        def __init__(self):
            self.calls: list[tuple] = []

        async def complete(self, messages, tools, config) -> LLMResponse:
            self.calls.append((list(messages), list(tools), config))
            if len(self.calls) == 1:
                return LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="edit_concept",
                            arguments={"path": "/thin.md", "new_body": "x" * 250},
                        )
                    ]
                )
            # the edit landed on disk before the final answer
            thin.write_text(
                "---\ntype: Note\ntitle: Thin\n---\n" + "x" * 250 + "\n", encoding="utf-8"
            )
            return LLMResponse(text="Enriched the thin concept.")

    backend = FakeBackend(
        docs={"/thin.md": {"frontmatter": {"title": "Thin", "type": "Note"}, "body": "stub"}}
    )
    librarian = make_librarian(backend, FixingProvider())
    backend.scan_root = root

    result = await librarian.handle_curate()

    assert result["organized"] is True
    assert result["findings"]["thin_concepts"] == []  # fixed finding not "remaining"
    assert result["summary"] == (
        "Enriched the thin concept."
        "\n\nPost-run check: no open findings remain; the library is well-organized."
        "\n\nPost-run verification: machine-confirmed 1 repaired concept(s)."
    )


# --- CS-11: previously silent swallows now log -------------------------------


def test_concept_entries_read_failure_logs_warning(caplog):
    backend = FakeBackend()  # no docs: read_document raises KeyError
    librarian = make_librarian(backend, ScriptedProvider([]))
    tracker = _Tracker(read_paths=["/gone.md"])
    with caplog.at_level(logging.WARNING, logger="athenaeum.librarian.agent"):
        entries = librarian._concept_entries(tracker)
    assert entries == []
    assert "concept entry: read failed for /gone.md" in caplog.text


def test_stored_entries_title_lookup_failure_logs_warning(caplog):
    backend = FakeBackend()
    librarian = make_librarian(backend, ScriptedProvider([]))
    tracker = _Tracker(writes=[{"id": "/gone", "action": "created"}])
    with caplog.at_level(logging.WARNING, logger="athenaeum.librarian.agent"):
        stored = librarian._stored_entries(tracker)
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


# --- A21: curator-config inheritance is an explicit marker -------------------


def test_curate_provider_inheritance_survives_derived_config_copy():
    """Inheritance dispatches on the explicit None marker: deriving a config
    whose llm is a copy cannot silently split off a second provider."""
    base = LLMConfig(provider="openai", model="m", api_key="k")
    provider = ScriptedProvider([LLMResponse(text="x")])
    librarian = Librarian(
        "/unused-root",
        LibrarianConfig(user_id="user-1", llm=base),
        backend=FakeBackend(),
        provider=provider,
    )
    librarian.config = dataclasses.replace(librarian.config, llm=dataclasses.replace(base))
    assert librarian.config.curate_llm is None
    assert librarian._curate_provider_or_default() is provider
    assert librarian._curate_provider is None
