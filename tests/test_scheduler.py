"""Tests for the nightly scheduled curation runner (scheduler.py).

Real SQLite DB on tmp_path, LibrarianManager with per-user fakes (FakeBackend
with a ``healthy`` flag, ScriptedProvider), ticks driven directly with
constructed UTC datetimes (fake clock) — ``run_forever`` is only exercised in
one cancellation smoke test.
"""

import asyncio
import logging
import sqlite3
from contextlib import suppress
from datetime import UTC, datetime

import pytest

from athenaeum import db as db_module
from athenaeum.curator.agent import CURATOR_VERIFIER
from athenaeum.librarian.gate import KIND_CURATOR, AgentRunBusyError
from athenaeum.librarian.llm import LLMResponse, ToolCall
from athenaeum.librarian.manager import LibrarianManager
from athenaeum.scheduler import SCHEDULER_LABEL, CurateScheduler
from test_mcp_tools import FakeBackend, ScriptedProvider

DAY1 = datetime(2026, 7, 27, tzinfo=UTC)  # Monday
DAY2 = datetime(2026, 7, 28, tzinfo=UTC)
DAY4 = datetime(2026, 7, 29, tzinfo=UTC)  # Wednesday


def at(day: datetime, hhmm: str) -> datetime:
    return day.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]))


class RaisingProvider:
    async def complete(self, messages, tools, config):
        raise RuntimeError("provider down")


class RecordingSeedCache:
    def __init__(self):
        self.invalidated: list[str] = []

    def invalidate(self, user_id: str) -> None:
        self.invalidated.append(user_id)


class RecordingRegistry:
    def __init__(self):
        self.added: list[dict] = []
        self.removed: list[str] = []

    def add(self, entry: dict) -> None:
        self.added.append(dict(entry))

    def remove(self, trace_id: str) -> None:
        self.removed.append(trace_id)


def make_db(
    tmp_path,
    *,
    enabled: int = 1,
    schedule_time: str | None = "03:00",
    configured: bool = True,
) -> str:
    db_path = tmp_path / "app.db"
    db_module.init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO users (id, username, password_hash, created_at) "
            "VALUES ('user-a', 'alice', 'hash', '2026-01-01T00:00:00Z')"
        )
        if schedule_time is None:
            conn.execute(
                "INSERT INTO librarian_configs"
                " (user_id, llm_model, curate_schedule_enabled, curate_schedule_time)"
                " VALUES ('user-a', ?, ?, NULL)",
                ("m" if configured else None, enabled),
            )
        else:
            conn.execute(
                "INSERT INTO librarian_configs"
                " (user_id, llm_model, curate_schedule_enabled, curate_schedule_time)"
                " VALUES ('user-a', ?, ?, ?)",
                ("m" if configured else None, enabled, schedule_time),
            )
        if configured:
            conn.execute(
                "INSERT INTO provider_configs "
                "(id, user_id, label, provider, api_key_enc, is_default, created_at) "
                "VALUES ('conn-user-a', 'user-a', 'Default', 'openai', 'k', 1, "
                "'2026-01-01T00:00:00Z')"
            )
    return str(db_path)


def make_stack(
    tmp_path,
    *,
    healthy: bool = True,
    provider_scripts: list[LLMResponse] | None = None,
    raise_provider: bool = False,
    registry=None,
    seed_cache=None,
    **db_kwargs,
):
    """Assemble db + manager + scheduler with per-user fakes (no MCP server)."""
    db_path = make_db(tmp_path, **db_kwargs)
    backend = FakeBackend(healthy=healthy)
    # A10: curate scans run through the backend; delegate to the real on-disk
    # library root (write_thin_concept writes there).
    backend.scan_root = tmp_path / "data" / "users" / "user-a" / "library"
    providers: list = []

    def provider_factory(user_id, llm):
        provider = RaisingProvider() if raise_provider else ScriptedProvider(provider_scripts or [])
        providers.append(provider)
        return provider

    manager = LibrarianManager(
        db_path,
        tmp_path / "data",
        backend_factory=lambda user_id, root, config: backend,
        provider_factory=provider_factory,
    )
    scheduler = CurateScheduler(manager, db_path, registry=registry, seed_cache=seed_cache)
    return scheduler, manager, backend, providers, db_path


def activity_rows(db_path: str, user_id: str = "user-a") -> list:
    conn = db_module.connect(db_path)
    try:
        return db_module.list_activity(conn, user_id, limit=50)
    finally:
        conn.close()


def write_thin_concept(manager, user_id: str = "user-a") -> None:
    """One thin concept on disk so the curate findings scan reports work."""
    root = manager.library_root(user_id)
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.md").write_text("---\ntype: Note\ntitle: A\n---\nold\n", encoding="utf-8")


def trace_files(manager, user_id: str = "user-a") -> list:
    store = manager.library_root(user_id) / ".traces"
    return sorted(store.glob("*.json")) if store.is_dir() else []


# --- due check / missed runs ---------------------------------------------------


async def test_startup_baseline_skips_missed_run(tmp_path):
    scheduler, _, _, _, db_path = make_stack(tmp_path)
    await scheduler.tick(at(DAY1, "09:00"))  # first tick: baseline only
    assert activity_rows(db_path) == []


async def test_window_crossing_fires_once(tmp_path):
    scheduler, _, _, _, db_path = make_stack(tmp_path)
    await scheduler.tick(at(DAY1, "02:59"))  # baseline
    await scheduler.tick(at(DAY1, "03:00"))
    assert len(activity_rows(db_path)) == 2
    await scheduler.tick(at(DAY1, "03:01"))
    await scheduler.tick(at(DAY1, "03:30"))
    assert len(activity_rows(db_path)) == 2  # no re-fire the same day


async def test_fires_again_next_day(tmp_path):
    scheduler, _, _, _, db_path = make_stack(tmp_path)
    await scheduler.tick(at(DAY1, "02:59"))
    await scheduler.tick(at(DAY1, "03:00"))
    await scheduler.tick(at(DAY2, "03:00"))
    assert len(activity_rows(db_path)) == 4


async def test_midnight_wraparound(tmp_path):
    scheduler, _, _, _, db_path = make_stack(tmp_path, schedule_time="00:00")
    await scheduler.tick(at(DAY1, "23:59"))
    await scheduler.tick(at(DAY2, "00:00"))
    assert len(activity_rows(db_path)) == 2


async def test_multi_day_stall_fires_exactly_once(tmp_path):
    scheduler, _, _, _, db_path = make_stack(tmp_path, schedule_time="04:00")
    await scheduler.tick(at(DAY1, "04:00"))  # baseline Monday
    await scheduler.tick(at(DAY4, "04:00"))  # Wednesday: two days missed
    assert len(activity_rows(db_path)) == 2
    await scheduler.tick(at(DAY4, "04:30"))
    assert len(activity_rows(db_path)) == 2


async def test_disabled_user_skipped(tmp_path):
    scheduler, _, _, _, db_path = make_stack(tmp_path, enabled=0)
    await scheduler.tick(at(DAY1, "02:59"))
    await scheduler.tick(at(DAY1, "03:00"))
    assert activity_rows(db_path) == []


async def test_null_time_skipped(tmp_path):
    scheduler, _, _, _, db_path = make_stack(tmp_path, schedule_time=None)
    await scheduler.tick(at(DAY1, "02:59"))
    await scheduler.tick(at(DAY1, "03:00"))
    assert activity_rows(db_path) == []


async def test_invalid_time_skipped_without_raising(tmp_path):
    scheduler, _, _, _, db_path = make_stack(tmp_path, schedule_time="25:99")
    await scheduler.tick(at(DAY1, "02:59"))
    await scheduler.tick(at(DAY1, "03:00"))  # logs a warning, must not raise
    assert activity_rows(db_path) == []


async def test_unconfigured_user_skipped(tmp_path):
    scheduler, manager, _, providers, db_path = make_stack(tmp_path, configured=False)
    await scheduler.tick(at(DAY1, "02:59"))
    await scheduler.tick(at(DAY1, "03:00"))
    assert activity_rows(db_path) == []
    assert providers == []  # no LLM provider was ever built
    conn = db_module.connect(db_path)
    try:
        assert db_module.get_config(conn, "user-a")["curate_last_run_at"] is None
    finally:
        conn.close()


async def test_scheduler_skips_while_mcp_curator_run_holds_gate(tmp_path):
    """An in-flight MCP curator run holds the gate: the due scheduler run is
    skipped — no journal rows, no re-baseline (scheduler fold, S8)."""
    scheduler, manager, _, _, db_path = make_stack(tmp_path)
    await scheduler.tick(at(DAY1, "02:59"))
    async with manager.run_gate.acquire("user-a", "curator", wait=False):
        await scheduler.tick(at(DAY1, "03:00"))
        assert activity_rows(db_path) == []
    conn = db_module.connect(db_path)
    try:
        assert db_module.get_config(conn, "user-a")["curate_last_run_at"] is None
    finally:
        conn.close()
    # gate free again: the next day's run fires normally
    await scheduler.tick(at(DAY2, "03:00"))
    assert len(activity_rows(db_path)) == 2


async def test_mcp_curator_run_rejected_while_scheduler_holds_gate(tmp_path):
    """Vice versa: the scheduler's in-flight run holds the curator gate, so a
    concurrent curator handler call is rejected (AgentRunBusyError)."""
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingProvider:
        async def complete(self, messages, tools, config):
            entered.set()
            await release.wait()
            return LLMResponse(text="done")

    scheduler, manager, _, _, db_path = make_stack(tmp_path, healthy=False)
    curator = manager.get_curator("user-a")  # pre-cache: the scheduler uses this instance
    curator._provider = BlockingProvider()
    await scheduler.tick(at(DAY1, "02:59"))
    run = asyncio.create_task(scheduler.tick(at(DAY1, "03:00")))
    await asyncio.wait_for(entered.wait(), timeout=5)
    assert manager.run_gate.locked("user-a", "curator")
    with pytest.raises(AgentRunBusyError, match="another curator run is in progress"):
        await curator.handle_curate()
    release.set()
    await run
    assert len(activity_rows(db_path)) == 2


async def test_busy_race_after_precheck_journals_error_rows(tmp_path, monkeypatch):
    """The gate fills between the locked() pre-check and the handler acquire:
    the busy runs are journaled as error rows, matching the MCP wiring (A8)."""
    scheduler, manager, _, _, db_path = make_stack(tmp_path)
    await scheduler.tick(at(DAY1, "02:59"))
    monkeypatch.setattr(manager.run_gate, "locked", lambda *args: False)
    async with manager.run_gate.acquire("user-a", "curator", wait=False):
        await scheduler.tick(at(DAY1, "03:00"))
    rows = activity_rows(db_path)
    assert len(rows) == 2
    assert {row["tool"] for row in rows} == {"library_maintain", "library_curate"}
    for row in rows:
        assert row["outcome"] == "error"
        assert "another curator run is in progress" in row["error"]
    assert manager.curate_last_run_at("user-a") is None  # no re-baseline on failure


# --- run behavior --------------------------------------------------------------


async def test_noop_run_journals_two_rows_without_traces(tmp_path):
    scheduler, manager, _, providers, db_path = make_stack(tmp_path)
    await scheduler.tick(at(DAY1, "02:59"))
    await scheduler.tick(at(DAY1, "03:00"))
    rows = activity_rows(db_path)
    assert len(rows) == 2
    assert {row["tool"] for row in rows} == {"library_maintain", "library_curate"}
    for row in rows:
        assert row["outcome"] == "ok"
        assert row["token_label"] == SCHEDULER_LABEL
        assert row["arguments"] == "{}"
        assert row["iterations"] is None
        assert row["total_tokens"] is None
    assert all(provider.calls == [] for provider in providers)  # no LLM calls
    assert trace_files(manager) == []  # no-op runs write no trace file


async def test_nightly_hygiene_repair_without_llm(tmp_path):
    """F25 stock repair on the scheduler path: the curate step's deterministic
    hygiene sweep repairs a dirty on-disk concept via edit_concept (no LLM),
    and the action surfaces as a mutation -> seed invalidation."""
    seed_cache = RecordingSeedCache()
    scheduler, manager, backend, providers, db_path = make_stack(tmp_path, seed_cache=seed_cache)
    root = manager.library_root("user-a")
    root.mkdir(parents=True, exist_ok=True)
    dirty_body = "prose with escape \\u2011 here. " * 10 + "\n"  # >200 chars: no thin finding
    (root / "a.md").write_text(
        "---\ntype: Note\ntitle: Alpha\n---\n" + dirty_body,
        encoding="utf-8",
    )
    # the fake's edit_concept targets the in-memory docs (it does not decode)
    backend.docs["/a.md"] = {
        "frontmatter": {"type": "Note", "title": "Alpha"},
        "body": dirty_body,
    }

    await scheduler.tick(at(DAY1, "02:59"))
    await scheduler.tick(at(DAY1, "03:00"))

    assert ("edit_concept", "/a.md", SCHEDULER_LABEL, None, None) in backend.calls
    assert seed_cache.invalidated == ["user-a"]
    assert all(provider.calls == [] for provider in providers)  # no LLM calls
    rows = activity_rows(db_path)
    assert len(rows) == 2
    assert all(row["outcome"] == "ok" for row in rows)


async def test_nightly_code_span_candidate_wakes_curator(tmp_path):
    """A fence-only escape file is not a sweep repair but a code-span escape
    candidate: the nightly curate wakes the curator LLM (scripted
    keep-literal response), the run completes, and both activity rows are ok."""
    scripts = [LLMResponse(text="Intentional documentation; left unchanged.")]
    scheduler, manager, backend, providers, db_path = make_stack(tmp_path, provider_scripts=scripts)
    root = manager.library_root("user-a")
    root.mkdir(parents=True, exist_ok=True)
    body = "```text\n" + "DP\\u20111 " * 30 + "\n```\n"  # >200 chars: no thin finding
    (root / "a.md").write_text(
        "---\ntype: Note\ntitle: Alpha\n---\n" + body,
        encoding="utf-8",
    )

    await scheduler.tick(at(DAY1, "02:59"))
    await scheduler.tick(at(DAY1, "03:00"))

    assert providers[0].calls, "a code-span candidate must wake the curator LLM"
    assert (root / "a.md").read_text(encoding="utf-8").endswith(body)  # untouched
    rows = activity_rows(db_path)
    assert len(rows) == 2
    assert all(row["outcome"] == "ok" for row in rows)


async def test_noop_curate_rebaselines_last_run(tmp_path):
    scheduler, manager, _, _, db_path = make_stack(tmp_path)
    await scheduler.tick(at(DAY1, "02:59"))
    await scheduler.tick(at(DAY1, "03:00"))
    last_run = manager.curate_last_run_at("user-a")
    assert last_run is not None
    conn = db_module.connect(db_path)
    try:
        assert db_module.get_config(conn, "user-a")["curate_last_run_at"] == last_run
    finally:
        conn.close()


async def test_nightly_run_verifies_repaired_concepts(tmp_path):
    """The nightly curator machine-confirms the concepts it repaired — with
    the SAME athenaeum-curator/<version> actor as interactive MCP runs."""
    scripts = [
        LLMResponse(
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="edit_concept",
                    arguments={"path": "/a.md", "new_body": "fixed"},
                )
            ]
        ),
        LLMResponse(text="repaired"),
    ]
    scheduler, manager, backend, providers, db_path = make_stack(
        tmp_path, healthy=False, provider_scripts=scripts
    )
    backend.docs["/a.md"] = {"frontmatter": {"title": "A", "type": "Note"}, "body": "old"}
    await scheduler.tick(at(DAY1, "02:59"))
    await scheduler.tick(at(DAY1, "03:00"))
    verified = backend.docs["/a.md"]["frontmatter"]["verified"]
    assert [entry["by"] for entry in verified] == [CURATOR_VERIFIER]
    assert ("verify_concept", "/a.md", CURATOR_VERIFIER, SCHEDULER_LABEL) in backend.calls


async def test_error_path_journals_errors_and_continues(tmp_path):
    scheduler, manager, backend, _, db_path = make_stack(
        tmp_path, healthy=False, raise_provider=True
    )
    write_thin_concept(manager)  # curate findings non-empty: it calls the LLM too
    await scheduler.tick(at(DAY1, "02:59"))
    await scheduler.tick(at(DAY1, "03:00"))  # must return normally
    rows = activity_rows(db_path)
    assert len(rows) == 2  # maintain failure does not block curate
    assert {row["tool"] for row in rows} == {"library_maintain", "library_curate"}
    for row in rows:
        assert row["outcome"] == "error"
        assert "provider down" in row["error"]
    assert manager.curate_last_run_at("user-a") is None  # no re-baseline
    await scheduler.tick(at(DAY1, "03:01"))  # the next tick still works
    assert len(activity_rows(db_path)) == 2


async def test_seed_invalidated_only_after_mutating_run(tmp_path):
    seed_cache = RecordingSeedCache()
    scheduler, manager, backend, _, _ = make_stack(
        tmp_path,
        provider_scripts=[
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
        ],
        seed_cache=seed_cache,
    )
    backend.docs["/a.md"] = {"frontmatter": {"title": "A", "type": "Note"}, "body": "old"}
    write_thin_concept(manager)
    await scheduler.tick(at(DAY1, "02:59"))
    await scheduler.tick(at(DAY1, "03:00"))
    assert seed_cache.invalidated == ["user-a"]


async def test_seed_not_invalidated_after_noop_run(tmp_path):
    seed_cache = RecordingSeedCache()
    scheduler, _, _, _, _ = make_stack(tmp_path, seed_cache=seed_cache)
    await scheduler.tick(at(DAY1, "02:59"))
    await scheduler.tick(at(DAY1, "03:00"))
    assert seed_cache.invalidated == []


async def test_in_flight_registry_entries_added_and_removed(tmp_path):
    registry = RecordingRegistry()
    scheduler, _, _, _, _ = make_stack(tmp_path, registry=registry)
    await scheduler.tick(at(DAY1, "02:59"))
    await scheduler.tick(at(DAY1, "03:00"))
    assert [entry["tool"] for entry in registry.added] == ["library_maintain", "library_curate"]
    for entry in registry.added:
        assert entry["token_label"] == SCHEDULER_LABEL
        assert entry["user_id"] == "user-a"
        assert entry["arguments"] == "{}"
    assert registry.removed == [entry["trace_id"] for entry in registry.added]


async def test_run_forever_cancellation_smoke(tmp_path):
    scheduler, _, _, _, _ = make_stack(tmp_path)
    scheduler._interval = 0.01
    task = asyncio.create_task(scheduler.run_forever())
    await asyncio.sleep(0.05)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    assert task.done()


async def test_hung_run_times_out_and_next_user_proceeds(tmp_path, caplog):
    """A20: a stuck run is cancelled after run_timeout (journaled like a
    kill: no row for the in-flight tool) and the tick proceeds to the next
    due user."""
    db_path = tmp_path / "app.db"
    db_module.init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        for user_id in ("user-a", "user-b"):
            conn.execute(
                "INSERT INTO users (id, username, password_hash, created_at) "
                f"VALUES ('{user_id}', '{user_id}', 'hash', '2026-01-01T00:00:00Z')"
            )
            conn.execute(
                "INSERT INTO librarian_configs"
                " (user_id, llm_model, curate_schedule_enabled, curate_schedule_time)"
                f" VALUES ('{user_id}', 'm', 1, '03:00')"
            )
            conn.execute(
                "INSERT INTO provider_configs "
                "(id, user_id, label, provider, api_key_enc, is_default, created_at) "
                f"VALUES ('conn-{user_id}', '{user_id}', 'Default', 'openai', 'k', 1,"
                " '2026-01-01T00:00:00Z')"
            )

    class HungProvider:
        async def complete(self, messages, tools, config):
            await asyncio.sleep(600)  # never returns within the test
            raise AssertionError("hung provider was not cancelled")  # pragma: no cover

    backends = {}
    for user_id in ("user-a", "user-b"):
        # user-a is unhealthy: maintain calls the (hung) LLM. user-b no-ops.
        backend = FakeBackend(healthy=(user_id == "user-b"))
        backend.scan_root = tmp_path / "data" / "users" / user_id / "library"
        backends[user_id] = backend
    providers = {"user-a": HungProvider(), "user-b": ScriptedProvider([])}
    manager = LibrarianManager(
        str(db_path),
        tmp_path / "data",
        backend_factory=lambda user_id, root, config: backends[user_id],
        provider_factory=lambda user_id, llm: providers[user_id],
    )
    scheduler = CurateScheduler(manager, str(db_path), run_timeout=0.1)
    await scheduler.tick(at(DAY1, "02:59"))  # baseline tick
    with caplog.at_level(logging.WARNING, logger="athenaeum.scheduler"):
        await scheduler.tick(at(DAY1, "03:00"))
    assert "run timed out for user user-a" in caplog.text
    # the timed-out user's in-flight tool journals no row (killed mid-run)
    assert activity_rows(str(db_path), "user-a") == []
    # the next due user still ran both tools to completion
    rows_b = activity_rows(str(db_path), "user-b")
    assert {row["tool"] for row in rows_b} == {"library_maintain", "library_curate"}
    assert all(row["outcome"] == "ok" for row in rows_b)


# --- manual "Run now" (WebUI trigger) --------------------------------------------


async def test_run_now_journals_webui_row_and_rebaselines(tmp_path):
    scheduler, manager, _, _, db_path = make_stack(tmp_path)
    assert manager.curate_last_run_at("user-a") is None
    await scheduler.run_now("user-a")
    rows = activity_rows(db_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["tool"] == "library_curate"
    assert row["token_label"] == "webui"
    assert row["outcome"] == "ok"
    last_run = manager.curate_last_run_at("user-a")
    assert last_run is not None
    conn = db_module.connect(db_path)
    try:
        assert db_module.get_config(conn, "user-a")["curate_last_run_at"] == last_run
    finally:
        conn.close()


async def test_run_now_skips_unconfigured_user(tmp_path):
    scheduler, _, _, providers, db_path = make_stack(tmp_path, configured=False)
    await scheduler.run_now("user-a")
    assert activity_rows(db_path) == []
    assert providers == []  # no LLM provider was ever built


async def test_run_now_gate_contention_journals_error_row(tmp_path):
    """A curator gate held by another entry point: the manual run is rejected
    by the gate inside the handler and journaled as an error row (A8)."""
    scheduler, manager, _, _, db_path = make_stack(tmp_path)
    async with manager.run_gate.acquire("user-a", KIND_CURATOR, wait=False):
        await scheduler.run_now("user-a")
    rows = activity_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["tool"] == "library_curate"
    assert rows[0]["outcome"] == "error"
    assert "in progress" in rows[0]["error"]
    assert manager.curate_last_run_at("user-a") is None  # no re-baseline on failure
