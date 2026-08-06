"""Nightly scheduled library maintenance (0.11.0).

One in-process asyncio task (started in the app lifespan) wakes once per
minute and, for every user whose ``curate_schedule_enabled`` flag is set and
whose UTC ``curate_schedule_time`` fell inside the window since the previous
tick, runs ``library_maintain`` followed by ``library_curate`` through the
same librarian handlers the MCP tools use.

Design notes (SCHEDULED_CURATION plan D5-D12, D16, D19):

- Missed runs are never caught up: the first tick only records the baseline
  minute, and a user fires at most once per day even after a multi-day stall.
- One activity row per tool (``token_label``/``agent_label`` = "scheduler"),
  one ``RequestTelemetry`` + one ``TraceSession`` per tool through the same
  ``mcp_server._agent_run`` / ``activity.journal_activity`` wiring the MCP
  tools use (A8); no-op runs journal a row but write no trace file. A busy
  gate after the pre-check journals an error row, matching the MCP tools.
- A completed curate run (success or no-op) re-baselines
  ``curate_last_run_at``; failed runs do not. A maintain failure never blocks
  the curate run.
- When either tool reported actions, the user's cached seed is invalidated so
  the next tools/list response carries a fresh seed.
- Manual WebUI "Run now" triggers reuse the same ``_run_tool`` wiring with a
  caller-supplied label ("webui") instead of "scheduler".

Limitation (A20): due users run sequentially inside the tick (single-worker
model), so one slow user delays the rest of the tick. Each user's run is
bounded by ``USER_RUN_TIMEOUT`` (default 45 min — above the ~40 min worst
case of two tools x max_iterations x 120 s provider timeout, so only a
genuinely stuck run is cut); a timed-out run is cancelled like a kill (the
in-flight tool journals no row) and the tick proceeds to the next user.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from athenaeum import db
from athenaeum.activity import journal_activity
from athenaeum.librarian.agent import KIND_CURATOR
from athenaeum.librarian.gate import AgentRunBusyError
from athenaeum.librarian.tracing import (
    RequestTelemetry,
    _telemetry_var,
    mint_request_id,
)
from athenaeum.mcp_server import _agent_run

if TYPE_CHECKING:
    from athenaeum.activity import ActivityRegistry
    from athenaeum.librarian.agent import Librarian
    from athenaeum.librarian.manager import LibrarianManager
    from athenaeum.mcp_server import SeedCache

logger = logging.getLogger(__name__)

SCHEDULER_LABEL = "scheduler"
TICK_INTERVAL = 60.0  # seconds
# A20: per-user run bound — comfortably above the worst-case legitimate run
# (two tools x max_iterations(10) x 120 s provider timeout ~= 40 min) so only
# genuinely stuck runs are cancelled.
USER_RUN_TIMEOUT = 45 * 60.0  # seconds


def _abs_minute(moment: datetime) -> int:
    """Absolute minute number (days since epoch * 1440 + minute of day)."""
    return moment.toordinal() * 1440 + moment.hour * 60 + moment.minute


class CurateScheduler:
    """Per-user nightly maintain+curate runner (single in-process task)."""

    def __init__(
        self,
        manager: LibrarianManager,
        db_path: str | Path,
        *,
        registry: ActivityRegistry | None = None,
        seed_cache: SeedCache | None = None,
        interval: float = TICK_INTERVAL,
        run_timeout: float = USER_RUN_TIMEOUT,
    ) -> None:
        self._manager = manager
        self._db_path = Path(db_path)
        self._registry = registry
        self._seed_cache = seed_cache
        self._interval = interval
        self._run_timeout = run_timeout
        # In-memory on purpose: one scheduler task per process, so dedup state
        # needs no persistence; cross-entry-point overlap is the run gate's job
        # (manager.run_gate).
        self._last_abs_minute: int | None = None  # None until the first tick
        self._last_fired_day: dict[str, int] = {}  # user_id -> toordinal()
        # Strong refs for fire-and-forget "Run now" tasks (an unreferenced
        # task could be garbage-collected mid-run).
        self._background: set[asyncio.Task] = set()

    async def run_forever(self) -> None:
        """Tick every ``interval`` seconds; a failed tick never kills the loop."""
        while True:
            try:
                await self.tick(datetime.now(UTC))
            except Exception:
                logger.exception("scheduler tick failed")
            await asyncio.sleep(self._interval)

    async def tick(self, now: datetime) -> None:
        """Run every due user (sequential, each bounded by ``run_timeout``;
        skips users whose curator gate is held)."""
        now_abs = _abs_minute(now)
        last = self._last_abs_minute
        self._last_abs_minute = now_abs
        if last is None:
            return  # baseline tick: no catch-up on startup
        conn = db.connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT user_id, curate_schedule_time FROM librarian_configs"
                " WHERE curate_schedule_enabled = 1"
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            user_id = row["user_id"]
            time_hhmm = row["curate_schedule_time"]
            if not time_hhmm or not db.HHMM_RE.fullmatch(time_hhmm):
                logger.warning(
                    "scheduler: skipping user %s with invalid time %r", user_id, time_hhmm
                )
                continue
            if not self._due(user_id, time_hhmm, last, now_abs, now.toordinal()):
                continue
            self._last_fired_day[user_id] = now.toordinal()
            try:
                # A20: bound each user's run; users stay sequential (single-
                # worker model) but a stuck run can no longer stall the tick.
                await asyncio.wait_for(self._run_user(user_id), timeout=self._run_timeout)
            except TimeoutError:
                logger.warning(
                    "scheduler: run timed out for user %s after %.0f s; continuing",
                    user_id,
                    self._run_timeout,
                )
            except Exception:
                logger.exception("scheduler: run failed for user %s", user_id)

    def _due(self, user_id: str, time_hhmm: str, last: int, now_abs: int, now_ordinal: int) -> bool:
        """True when the scheduled minute falls in ``(last, now_abs]`` for the
        current or previous day and the user has not fired today."""
        if self._last_fired_day.get(user_id) == now_ordinal:
            return False
        scheduled = int(time_hhmm[:2]) * 60 + int(time_hhmm[3:])
        today = now_ordinal * 1440 + scheduled
        return any(last < minute <= now_abs for minute in (today, today - 1440))

    async def _run_user(self, user_id: str) -> None:
        if self._manager.run_gate.locked(user_id, KIND_CURATOR):
            # An MCP-initiated curator run holds the gate: this scheduled run
            # is redundant — skip it (no journal rows, no re-baseline).
            logger.info("scheduler: skipping user %s (curator run in progress)", user_id)
            return
        librarian = await asyncio.to_thread(self._manager.get, user_id)  # cold build off-loop
        if not librarian.configured:
            logger.info("scheduler: skipping unconfigured user %s", user_id)
            return
        mutated = False
        dirty: list[dict] = []
        for tool, handler in (
            ("library_maintain", librarian.handle_maintain),
            ("library_curate", librarian.handle_curate),
        ):
            result = await self._run_tool(user_id, librarian, tool, handler)
            if result is not None:
                mutated = mutated or bool(result.get("actions"))
                dirty += result.get("actions") or []
                if tool == "library_curate":
                    # Run-end timestamp on completed runs only (no re-baseline
                    # after a failure).
                    await asyncio.to_thread(self._manager.set_curate_last_run, user_id, db.utcnow())
        try:
            # A failed embedding run must never block curate (maintain precedent).
            await librarian.sync_embeddings(dirty)
        except Exception:
            logger.exception("scheduler: embedding sync failed for user %s", user_id)
        if mutated and self._seed_cache is not None:
            self._seed_cache.invalidate(user_id)

    def curator_busy(self, user_id: str) -> bool:
        """True while a curator run (any entry point) holds this user's gate."""
        return self._manager.run_gate.locked(user_id, KIND_CURATOR)

    def start_run_now(self, user_id: str, *, token_label: str = "webui") -> None:
        """Spawn a manual curate run as a background task (WebUI 'Run now')."""
        task = asyncio.create_task(self.run_now(user_id, token_label=token_label))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def run_now(self, user_id: str, *, token_label: str = "webui") -> None:
        """Manual curate run: same gate/telemetry/trace/journal wiring as a scheduled
        run, library_curate only (the F25 content-hygiene sweep runs at the top of
        handle_curate). Completed runs re-baseline curate_last_run_at."""
        librarian = await asyncio.to_thread(self._manager.get, user_id)
        if not librarian.configured:
            logger.info("run_now: skipping unconfigured user %s", user_id)
            return
        try:
            result = await asyncio.wait_for(
                self._run_tool(
                    user_id, librarian, "library_curate", librarian.handle_curate, label=token_label
                ),
                timeout=self._run_timeout,
            )
        except TimeoutError:
            logger.warning("run_now: curate run timed out for user %s", user_id)
            return
        except Exception:
            logger.exception("run_now: curate run failed for user %s", user_id)
            return
        if result is None:
            return  # failure already journaled by _run_tool
        dirty = result.get("actions") or []
        await asyncio.to_thread(self._manager.set_curate_last_run, user_id, db.utcnow())
        try:
            # A failed embedding run must never fail the curate run (maintain precedent).
            await librarian.sync_embeddings(dirty)
        except Exception:
            logger.exception("run_now: embedding sync failed for user %s", user_id)
        if dirty and self._seed_cache is not None:
            self._seed_cache.invalidate(user_id)

    async def _run_tool(
        self,
        user_id: str,
        librarian: Librarian,
        tool: str,
        handler,
        *,
        label: str = SCHEDULER_LABEL,
    ) -> dict | None:
        """Run one tool with MCP-equivalent telemetry/trace/journal wiring.

        Returns the tool result, or None when the run failed (the failure is
        journaled and traced here; the caller continues with the next tool).
        """
        telemetry = RequestTelemetry(trace_id=mint_request_id())
        telemetry_token = _telemetry_var.set(telemetry)
        started_at = db.utcnow()
        if self._registry is not None:
            self._registry.add(
                {
                    "trace_id": telemetry.trace_id,
                    "user_id": user_id,
                    "token_label": label,
                    "tool": tool,
                    "arguments": "{}",
                    "started_at": started_at,
                }
            )
        start = time.perf_counter()
        outcome, error_text, result = "ok", None, None
        journal = True
        try:
            async with _agent_run(self._manager, user_id, label, tool, for_client=False):
                result = await handler(None, agent_label=label)
        except AgentRunBusyError as exc:
            # Gate contention after the pre-check: journaled as an error row,
            # matching the MCP wiring (A8).
            outcome, error_text = "error", str(exc)
        except asyncio.CancelledError:
            journal = False  # mid-run shutdown: no journal row, same as a kill
            raise
        except Exception as exc:
            outcome, error_text = "error", f"{type(exc).__name__}: {exc}"
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            if self._registry is not None:
                self._registry.remove(telemetry.trace_id)
            if journal:
                await asyncio.to_thread(
                    journal_activity,
                    self._db_path,
                    trace_id=telemetry.trace_id,
                    user_id=user_id,
                    token_label=label,
                    tool=tool,
                    arguments="{}",
                    started_at=started_at,
                    duration_ms=duration_ms,
                    outcome=outcome,
                    error_text=error_text,
                    telemetry=telemetry,
                )
            # Telemetry resets last: TraceSession.close and the journal read it.
            _telemetry_var.reset(telemetry_token)
        return result
