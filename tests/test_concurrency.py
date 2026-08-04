"""Concurrency tests: the per-root write lock shared by LibraryBackend instances.

Two backends on one library root (the WebUI builds fresh backends per
request) run threaded write bursts; the shared RLock must serialize the
compound write (snapshot -> concept file -> index.md -> log.md) so no log
entry is lost, snapshot numbers stay unique, and the index tree stays
consistent.
"""

import asyncio
import threading
import time
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from athenaeum import db as db_module
from athenaeum import security
from athenaeum.librarian.gate import AgentRunBusyError, RunGate
from athenaeum.librarian.llm import LLMResponse
from athenaeum.librarian.tools import dispatch
from athenaeum.library.backend import LibraryBackend
from test_mcp_tools import make_stack as make_mcp_stack
from test_mcp_tools import set_identity


def _entry_lines(root: Path, kind: str) -> list[str]:
    return [
        line
        for line in (root / "log.md").read_text(encoding="utf-8").split("\n")
        if line.startswith(f"* **{kind}**")
    ]


def test_threaded_write_bursts_preserve_log_snapshots_index(tmp_path):
    root = tmp_path / "library"
    backend_a = LibraryBackend(root, actor="agent-a")
    backend_b = LibraryBackend(root, actor="agent-b")
    backend_a.init_bundle()
    backend_a.create_concept("/shared/topic.md", {"type": "Note", "title": "t0"}, "body\n")
    barrier = threading.Barrier(4)
    errors = []

    def creator(backend: LibraryBackend, prefix: str) -> None:
        try:
            barrier.wait(timeout=10)
            for i in range(10):
                backend.create_concept(
                    f"/{prefix}/c{i}.md",
                    {"type": "Note", "title": f"{prefix} {i}"},
                    f"body {prefix} {i}\n",
                )
        except Exception as exc:
            errors.append(exc)

    def editor(backend: LibraryBackend) -> None:
        try:
            barrier.wait(timeout=10)
            for i in range(10):
                backend.edit_concept("/shared/topic.md", frontmatter_patch={"title": f"t{i}"})
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=creator, args=(backend_a, "alpha")),
        threading.Thread(target=creator, args=(backend_b, "beta")),
        threading.Thread(target=editor, args=(backend_a,)),
        threading.Thread(target=editor, args=(backend_b,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []

    # every compound write appended its log entry (no lost update)
    assert len(_entry_lines(root, "Initialization")) == 1
    assert len(_entry_lines(root, "Creation")) == 21
    assert len(_entry_lines(root, "Update")) == 20

    # one snapshot per mutating call, all uniquely numbered
    snaps = [d.name for d in (root / ".athenaeum" / "versions").iterdir() if d.is_dir()]
    assert len(snaps) == 41
    assert len(set(snaps)) == 41

    # no tmp residue from atomic writes anywhere in the bundle
    assert list(root.rglob("*.tmp")) == []

    # all concept files landed; the index tree is a consistent function of it
    for prefix in ("alpha", "beta"):
        for i in range(10):
            assert (root / prefix / f"c{i}.md").is_file()
        index_text = (root / prefix / "index.md").read_text(encoding="utf-8")
        for i in range(10):
            assert f"c{i}.md" in index_text
    report = backend_a.validate()
    assert report["errors"] == []


def test_move_concept_nested_lock_no_deadlock(tmp_path):
    """move_concept nests regenerate_index under the write lock (RLock)."""
    root = tmp_path / "library"
    backend_a = LibraryBackend(root, actor="agent-a")
    backend_b = LibraryBackend(root, actor="agent-b")
    backend_a.create_concept("/x/a.md", {"type": "Note", "title": "A"}, "body a\n")
    backend_b.create_concept("/x/b.md", {"type": "Note", "title": "B"}, "body b\n")
    barrier = threading.Barrier(2)
    errors = []

    def mover(backend: LibraryBackend, first: str, second: str) -> None:
        try:
            barrier.wait(timeout=10)
            for _ in range(10):
                backend.move_concept(first, second)
                backend.move_concept(second, first)
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=mover, args=(backend_a, "/x/a.md", "/y/a.md")),
        threading.Thread(target=mover, args=(backend_b, "/x/b.md", "/y/b.md")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive(), "move_concept deadlocked"
    assert errors == []
    assert (root / "x" / "a.md").is_file()
    assert (root / "x" / "b.md").is_file()
    assert len(_entry_lines(root, "Move")) == 40
    report = backend_a.validate()
    assert report["errors"] == []


def test_rollback_and_reconcile_serialized_with_writes(tmp_path):
    root = tmp_path / "library"
    backend_a = LibraryBackend(root, actor="agent-a")
    backend_b = LibraryBackend(root, actor="agent-b")
    backend_a.create_concept("/a.md", {"type": "Note", "title": "A"}, "v1\n")
    backend_a.edit_concept("/a.md", new_body="v2\n")  # snapshot 2 pre-images /a.md
    barrier = threading.Barrier(2)
    errors = []

    def writer() -> None:
        try:
            barrier.wait(timeout=10)
            for i in range(10):
                backend_a.edit_concept("/a.md", new_body=f"v{i}\n")
        except Exception as exc:
            errors.append(exc)

    def maintainer() -> None:
        try:
            barrier.wait(timeout=10)
            for _ in range(5):
                backend_b.reconcile()
                # L12: rollback is constrained to the latest snapshot; the
                # writer can outpace the lookup, so retry on that race.
                for _attempt in range(20):
                    latest = backend_b.list_versions()[0]["n"]
                    try:
                        backend_b.rollback(latest)
                        break
                    except ValueError:
                        continue
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer), threading.Thread(target=maintainer)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []
    assert (root / "a.md").is_file()
    report = backend_a.validate()
    assert report["errors"] == []
    assert list(root.rglob("*.tmp")) == []


# --- first-run setup race (CS-13) -----------------------------------------------


def test_concurrent_first_run_setup_creates_one_admin(tmp_path):
    """Two racing create_first_admin calls: exactly one wins, the loser gets None."""
    db_path = tmp_path / "app.db"
    db_module.init_db(db_path)
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def racer(username: str) -> None:
        conn = db_module.connect(db_path)
        try:
            barrier.wait(timeout=10)
            results.append(
                db_module.create_first_admin(
                    conn, username, security.hash_password(username + "-password")
                )
            )
        except Exception as exc:
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=racer, args=(name,)) for name in ("owner-a", "owner-b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    winners = [row for row in results if row is not None]
    assert len(winners) == 1

    conn = db_module.connect(db_path)
    try:
        users = db_module.list_users(conn)
        assert len(users) == 1
        assert users[0]["is_admin"] == 1
        assert db_module.get_config(conn, users[0]["id"]) is not None
    finally:
        conn.close()


# --- run gate (librarian/gate.py) -------------------------------------------------


async def test_run_gate_same_kind_rejects_overlap():
    gate = RunGate()
    async with gate.acquire("user-1", "librarian", wait=False):
        assert gate.locked("user-1", "librarian")
        with pytest.raises(AgentRunBusyError, match="another librarian run is in progress"):
            async with gate.acquire("user-1", "librarian", wait=False):
                pass
    assert not gate.locked("user-1", "librarian")


async def test_run_gate_cross_kind_and_cross_user_run_parallel():
    gate = RunGate()
    async with gate.acquire("user-1", "librarian", wait=False):
        async with gate.acquire("user-1", "curator", wait=False):
            pass
        async with gate.acquire("user-2", "librarian", wait=False):
            pass


async def test_run_gate_released_on_exception():
    gate = RunGate()
    with pytest.raises(RuntimeError, match="boom"):
        async with gate.acquire("user-1", "curator", wait=False):
            raise RuntimeError("boom")
    assert not gate.locked("user-1", "curator")
    async with gate.acquire("user-1", "curator", wait=False):  # re-acquirable
        pass


async def test_run_gate_wait_true_blocks_until_release():
    gate = RunGate()
    entered = False

    async def waiter() -> None:
        nonlocal entered
        async with gate.acquire("user-1", "librarian", wait=True):
            entered = True

    async with gate.acquire("user-1", "librarian"):
        task = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        assert not entered  # parked on the held lock
    await asyncio.wait_for(task, timeout=5)
    assert entered


# --- run gate at the MCP layer (plan step 7) ---------------------------------------


class _BlockingProvider:
    """LLM provider that parks inside complete() until released."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, messages, tools, config):
        self.started.set()
        await self.release.wait()
        return LLMResponse(text="slow answer")


async def test_mcp_same_kind_overlap_rejected(tmp_path, monkeypatch):
    server, manager, *_ = make_mcp_stack(tmp_path)
    librarian = manager.get("user-a")
    provider = _BlockingProvider()
    librarian._provider = provider
    set_identity(monkeypatch, "user-a", "agent-a")
    tool = await server.get_tool("request_knowledge")
    first = asyncio.create_task(tool.fn(query="q1"))
    await asyncio.wait_for(provider.started.wait(), timeout=5)
    with pytest.raises(ToolError, match="another librarian run is in progress; retry shortly"):
        await tool.fn(query="q2")
    provider.release.set()
    result = await first
    assert result["answer"] == "slow answer"
    # gate released with the first run: the next call goes through
    result = await tool.fn(query="q3")
    assert result["answer"] == "slow answer"


async def test_mcp_cross_kind_runs_in_parallel(tmp_path, monkeypatch):
    server, manager, *_ = make_mcp_stack(tmp_path)
    librarian = manager.get("user-a")
    provider = _BlockingProvider()
    librarian._provider = provider
    set_identity(monkeypatch, "user-a", "agent-a")
    request_tool = await server.get_tool("request_knowledge")
    maintain_tool = await server.get_tool("library_maintain")
    first = asyncio.create_task(request_tool.fn(query="q1"))
    await asyncio.wait_for(provider.started.wait(), timeout=5)
    # the curator kind is a different gate slot: it runs while the
    # librarian-kind run is still parked
    result = await asyncio.wait_for(maintain_tool.fn(), timeout=5)
    assert result["healthy"] is True
    provider.release.set()
    await first


# --- event-loop offload (A1/A3, Phase 2 step 2.4) -----------------------------------


async def test_dispatch_offloads_blocking_backend_calls():
    """A blocking backend call runs on a worker thread and does not stall the
    event loop while it is parked (A1/A3)."""
    loop_thread = threading.get_ident()
    call_threads: list[int] = []
    entered = threading.Event()
    release = threading.Event()

    class SlowBackend:
        def create_concept(self, path, frontmatter, body, *, agent_label=None, **kwargs):
            call_threads.append(threading.get_ident())
            entered.set()
            assert release.wait(timeout=10)
            return {"id": path[: -len(".md")], "action": "created"}

    task = asyncio.create_task(
        dispatch(
            "write_concept",
            {"path": "/x.md", "frontmatter": {"type": "note"}, "body": "b"},
            SlowBackend(),
        )
    )
    for _ in range(200):
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert entered.is_set(), "backend call never started"
    # the loop stays responsive while the blocking call is parked
    start = time.perf_counter()
    for _ in range(5):
        await asyncio.sleep(0.02)
    assert time.perf_counter() - start < 0.5, "event loop stalled behind a backend call"
    release.set()
    result = await asyncio.wait_for(task, timeout=5)
    assert result == {"id": "/x", "action": "created"}
    assert call_threads and call_threads[0] != loop_thread
