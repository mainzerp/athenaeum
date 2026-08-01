"""Run gate: per-(user, agent kind) asyncio lock registry.

One gate per LibrarianManager serializes same-kind agent runs for one user
across entry points (MCP tools, scheduler). The contention policy is reject,
never block: MCP callers get a retryable AgentRunBusyError; the scheduler
peeks with ``locked`` and skips its run. Different kinds on the same user
stay parallel.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class AgentRunBusyError(RuntimeError):
    """Another run of the same agent kind is in progress for this user."""


class RunGate:
    """Per-(user_id, kind) asyncio locks with a reject-on-contention acquire."""

    def __init__(self) -> None:
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def _lock_for(self, user_id: str, kind: str) -> asyncio.Lock:
        async with self._guard:
            key = (user_id, kind)
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            return self._locks[key]

    @asynccontextmanager
    async def acquire(self, user_id: str, kind: str, *, wait: bool = False) -> AsyncIterator[None]:
        lock = await self._lock_for(user_id, kind)
        # Check-then-acquire is atomic: acquiring a free asyncio.Lock never
        # suspends, so no task can interleave between the two.
        if not wait and lock.locked():
            raise AgentRunBusyError(f"another {kind} run is in progress; retry shortly")
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()

    def locked(self, user_id: str, kind: str) -> bool:
        """Peek: True while a (user_id, kind) run holds the gate."""
        lock = self._locks.get((user_id, kind))
        return lock is not None and lock.locked()

    async def drop(self, user_id: str) -> None:
        """Forget all IDLE locks for ``user_id`` (A12: no per-user leak).

        Locks held by an in-flight run are kept so its serialization
        survives; they are dropped by the next call once idle.
        """
        async with self._guard:
            idle = [key for key, lock in self._locks.items() if key[0] == user_id]
            for key in idle:
                if not self._locks[key].locked():
                    del self._locks[key]
