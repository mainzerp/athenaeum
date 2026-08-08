"""Tests for the tracing core (plan stream 1.3).

Covers the TraceStore file layout under .traces/, newest-first listing,
read round-trip, the traversal guard, prune keep-last-N (template:
test_snapshots.py), TraceSession event recording with the T3 per-tool
result shaping, and the mint/telemetry helpers.
"""

import json
import logging
import re
from pathlib import Path

import pytest

from athenaeum.librarian.tracing import (
    MAX_ERR,
    MAX_ITEMS,
    MAX_STR,
    RequestTelemetry,
    TraceSession,
    TraceStore,
    _telemetry_var,
    mint_request_id,
)


def make_trace(trace_id: str, tool: str = "request_knowledge") -> dict:
    return {
        "trace_id": trace_id,
        "tool": tool,
        "agent_label": "agent-x",
        "started_at": "2026-07-28T18:00:00+00:00",
        "ended_at": "2026-07-28T18:00:01+00:00",
        "duration_ms": 1234.5,
        "outcome": "ok",
        "error": None,
        "llm": None,
        "events": [],
    }


def read_trace(tmp_path, trace_id: str) -> dict:
    return json.loads((tmp_path / ".traces" / f"{trace_id}.json").read_text(encoding="utf-8"))


# --- TraceStore -----------------------------------------------------------


def test_create_writes_under_traces_dir(tmp_path):
    store = TraceStore(tmp_path)
    trace_id = store.create(make_trace("20260728T180000Z-aaaabbbb"))
    path = tmp_path / ".traces" / f"{trace_id}.json"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert json.loads(text)["trace_id"] == trace_id


def test_create_rejects_invalid_id(tmp_path):
    store = TraceStore(tmp_path)
    with pytest.raises(ValueError):
        store.create(make_trace("../evil"))


def test_list_newest_first(tmp_path):
    store = TraceStore(tmp_path)
    for trace_id in (
        "20260728T180001Z-aaaaaaaa",
        "20260728T180003Z-cccccccc",
        "20260728T180002Z-bbbbbbbb",
    ):
        store.create(make_trace(trace_id))
    listed = store.list()
    assert [t["trace_id"] for t in listed] == [
        "20260728T180003Z-cccccccc",
        "20260728T180002Z-bbbbbbbb",
        "20260728T180001Z-aaaaaaaa",
    ]
    assert "events" not in listed[0]  # summaries only
    assert listed[0]["tool"] == "request_knowledge"


def test_list_limit(tmp_path):
    store = TraceStore(tmp_path)
    for i in range(5):
        store.create(make_trace(f"20260728T18000{i}Z-aaaaaaaa"))
    assert len(store.list(limit=2)) == 2


def test_read_round_trip(tmp_path):
    store = TraceStore(tmp_path)
    trace = make_trace("20260728T180000Z-ddddeeee")
    trace["events"] = [{"seq": 1, "tool": "list_dir"}]
    store.create(trace)
    assert store.read(trace["trace_id"]) == trace


def test_read_rejects_traversal_id(tmp_path):
    store = TraceStore(tmp_path)
    with pytest.raises(ValueError):
        store.read("../../etc/passwd")
    with pytest.raises(ValueError):
        store.read("a/b")


def test_read_missing_raises(tmp_path):
    store = TraceStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.read("20260728T180000Z-missing1")


def test_prune_keeps_newest_n(tmp_path):
    store = TraceStore(tmp_path)
    for i in range(3):
        store.create(make_trace(f"20260728T18000{i}Z-aaaaaaaa"))
    deleted = store.prune(2)
    assert deleted == 1
    remaining = sorted(p.name for p in (tmp_path / ".traces").iterdir())
    assert remaining == [
        "20260728T180001Z-aaaaaaaa.json",
        "20260728T180002Z-aaaaaaaa.json",
    ]


def test_keep_prunes_on_create(tmp_path):
    store = TraceStore(tmp_path, keep=1)
    store.create(make_trace("20260728T180000Z-aaaaaaaa"))
    store.create(make_trace("20260728T180001Z-bbbbbbbb"))
    remaining = [p.name for p in (tmp_path / ".traces").iterdir()]
    assert remaining == ["20260728T180001Z-bbbbbbbb.json"]


def test_prune_tolerates_unlink_oserror(tmp_path, monkeypatch, caplog):
    """A locked trace file is logged and skipped; prune must not raise."""
    store = TraceStore(tmp_path)
    for i in range(3):
        store.create(make_trace(f"20260728T18000{i}Z-aaaaaaaa"))
    real_unlink = Path.unlink

    def flaky_unlink(self, *args, **kwargs):
        if self.name.startswith("20260728T180000Z"):
            raise OSError("file locked")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    with caplog.at_level(logging.WARNING, logger="athenaeum.librarian.tracing"):
        deleted = store.prune(1)

    assert deleted == 1  # only the file that actually unlinked counts
    remaining = sorted(p.name for p in (tmp_path / ".traces").iterdir())
    assert remaining == [
        "20260728T180000Z-aaaaaaaa.json",  # survived: unlink failed
        "20260728T180002Z-aaaaaaaa.json",
    ]
    assert "trace prune: could not delete" in caplog.text


def test_mint_request_id_format():
    request_id = mint_request_id()
    assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{8}", request_id)


# --- TraceSession ----------------------------------------------------------


def test_session_writes_trace_file(tmp_path):
    session = TraceSession(tmp_path, "20260728T180000Z-aaaabbbb", "request_knowledge", "agent-x")
    session.record(
        "list_dir",
        {"path": "/"},
        [{"name": "concepts", "path": "/concepts", "is_directory": True}],
        None,
        0.4,
    )
    session.finish("ok")
    trace_id = session.close()

    assert trace_id == "20260728T180000Z-aaaabbbb"
    data = read_trace(tmp_path, trace_id)
    assert data["trace_id"] == trace_id
    assert data["tool"] == "request_knowledge"
    assert data["agent_label"] == "agent-x"
    assert data["outcome"] == "ok"
    assert data["error"] is None
    assert data["llm"] is None
    assert data["duration_ms"] >= 0
    assert data["started_at"] and data["ended_at"]
    event = data["events"][0]
    assert event["seq"] == 1
    assert event["tool"] == "list_dir"
    assert event["args"] == {"path": "/"}
    assert event["result"] == {"path": "/", "entries": ["concepts"], "count": 1}
    assert event["error"] is None
    assert event["duration_ms"] == 0.4


def test_session_no_events_no_llm_writes_nothing(tmp_path):
    session = TraceSession(tmp_path, "20260728T180000Z-aaaabbbb", "library_maintain", None)
    session.finish("ok")
    assert session.close() is None
    assert not (tmp_path / ".traces").exists()


def test_session_close_persistence_failure_returns_none(tmp_path, monkeypatch, caplog):
    """Containment: a TraceStore.create failure is logged and swallowed —
    trace persistence must never fail the observed run."""
    session = TraceSession(tmp_path, "20260728T180000Z-aaaabbbb", "request_knowledge", None)
    session.record("list_dir", {"path": "/"}, [], None, 0.4)
    session.finish("ok")

    def boom(self, trace):
        raise OSError("disk full")

    monkeypatch.setattr(TraceStore, "create", boom)
    with caplog.at_level(logging.WARNING, logger="athenaeum.librarian.tracing"):
        assert session.close() is None
    assert "trace persistence failed for 20260728T180000Z-aaaabbbb" in caplog.text
    assert not (tmp_path / ".traces").exists()


def test_session_llm_only_writes_file(tmp_path):
    telemetry = RequestTelemetry(trace_id="20260728T180000Z-aaaabbbb")
    telemetry.llm = {
        "provider": "openai",
        "model": "m",
        "iterations": 0,
        "prompt_tokens": 1,
        "completion_tokens": 2,
        "total_tokens": 3,
    }
    token = _telemetry_var.set(telemetry)
    try:
        session = TraceSession(tmp_path, "20260728T180000Z-aaaabbbb", "request_knowledge", None)
        session.finish("ok")
        trace_id = session.close()
    finally:
        _telemetry_var.reset(token)

    data = read_trace(tmp_path, trace_id)
    assert data["llm"]["total_tokens"] == 3
    assert data["events"] == []


def test_session_error_outcome(tmp_path):
    session = TraceSession(tmp_path, "20260728T180000Z-aaaabbbb", "store_knowledge", "agent-y")
    session.record("search_metadata", {"field": "title"}, [], None, 1.0)
    session.finish("error", error="RuntimeError: boom")
    data = read_trace(tmp_path, session.close())
    assert data["outcome"] == "error"
    assert data["error"] == "RuntimeError: boom"


def test_record_truncates_long_arg_strings(tmp_path):
    session = TraceSession(tmp_path, "20260728T180000Z-aaaabbbb", "store_knowledge", None)
    long_body = "x" * (MAX_STR + 100)
    session.record(
        "write_concept",
        {"path": "/a.md", "frontmatter": {"note": long_body}, "body": long_body},
        {"id": "/a", "action": "created"},
        None,
        1.0,
    )
    session.finish("ok")
    data = read_trace(tmp_path, session.close())

    event = data["events"][0]
    assert event["args"]["body"] == "x" * MAX_STR + "…[truncated]"
    # nested strings are walked recursively
    assert event["args"]["frontmatter"]["note"].endswith("…[truncated]")
    # write-tool results are kept verbatim (already small)
    assert event["result"] == {"id": "/a", "action": "created"}


def test_record_shapes_read_document_and_drops_body(tmp_path):
    session = TraceSession(tmp_path, "20260728T180000Z-aaaabbbb", "request_knowledge", None)
    session.record(
        "read_document",
        {"path": "/alpha.md"},
        {
            "path": "/alpha.md",
            "frontmatter": {"title": "Alpha", "type": "Note", "extra": 1},
            "body": "x" * 5000,
        },
        None,
        1.0,
    )
    session.finish("ok")
    data = read_trace(tmp_path, session.close())

    assert data["events"][0]["result"] == {"path": "/alpha.md", "title": "Alpha", "type": "Note"}


def test_record_shapes_search_hits_paths_intact_and_capped(tmp_path):
    session = TraceSession(tmp_path, "20260728T180000Z-aaaabbbb", "request_knowledge", None)
    hits = [
        {"id": f"/c{i}", "path": f"/c{i}.md", "title": f"C{i}", "type": "Note"}
        for i in range(MAX_ITEMS + 10)
    ]
    session.record("search_metadata", {"field": "type", "value": "Note"}, hits, None, 1.0)
    session.finish("ok")
    data = read_trace(tmp_path, session.close())

    result = data["events"][0]["result"]
    assert result["count"] == MAX_ITEMS + 10
    assert len(result["hits"]) == MAX_ITEMS
    assert result["hits"][0] == "/c0.md"  # hit paths survive intact


def test_record_shapes_link_check(tmp_path):
    session = TraceSession(tmp_path, "20260728T180000Z-aaaabbbb", "library_maintain", None)
    broken = [{"source": f"/s{i}.md", "target": f"/t{i}.md"} for i in range(3)]
    session.record("link_check", {}, broken, None, 1.0)
    session.finish("ok")
    data = read_trace(tmp_path, session.close())

    assert data["events"][0]["result"] == {"broken": broken, "count": 3}


def test_record_error_formatting_and_cap(tmp_path):
    session = TraceSession(tmp_path, "20260728T180000Z-aaaabbbb", "request_knowledge", None)
    try:
        raise FileNotFoundError("y" * (MAX_ERR + 100))
    except FileNotFoundError as exc:
        session.record("read_document", {"path": "/x.md"}, None, exc, 1.0)
    session.finish("ok")
    data = read_trace(tmp_path, session.close())

    event = data["events"][0]
    assert event["result"] is None
    assert event["error"].startswith("FileNotFoundError: ")
    assert event["error"].endswith("…[truncated]")
    assert len(event["error"]) == MAX_ERR + len("…[truncated]")


def test_record_event_seq_increments(tmp_path):
    session = TraceSession(tmp_path, "20260728T180000Z-aaaabbbb", "request_knowledge", None)
    session.record("list_dir", {}, [], None, 0.1)
    session.record("link_check", {}, [], None, 0.2)
    session.finish("ok")
    data = read_trace(tmp_path, session.close())

    assert [event["seq"] for event in data["events"]] == [1, 2]
