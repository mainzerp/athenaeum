"""Activity tracking: in-flight registry + persisted MCP tool-call journal.

Contract: PLAN.md section 1 and step 3.2. The ActivityRegistry exposes the
currently in-flight tool calls to the WebUI (/activity); the
ActivityMiddleware (FastMCP, registered innermost after SeedMiddleware — T7)
mints one RequestTelemetry per tool call into ``_telemetry_var`` so the MCP
handlers and the agent loop share a single trace id (D9), and journals every
authenticated call into the ``activity`` table via its own sqlite connection
(precedent: ``sqlite_token_lookup`` in mcp_server.py). Auth failures raised
in the outer BearerAuthMiddleware are never journaled (accepted, T7).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from athenaeum import db, identity
from athenaeum.librarian.tracing import (
    MAX_ERR,
    RequestTelemetry,
    _telemetry_var,
    mint_request_id,
)

MAX_ARGS = 2000  # journal argument strings are truncated to this length


def _sanitize_arguments(tool: str, arguments: dict) -> dict:
    """Replace bulky argument values with refs before journaling (R20).

    For ``store_knowledge`` each ``images[i].data_base64` is hashed to a
    ``sha256:<hex16> (<n> chars)`` ref — the journal records the reference,
    never the base64 payload. All other tools/arguments pass through
    unchanged.
    """
    if tool != "store_knowledge":
        return arguments
    images = arguments.get("images")
    if not isinstance(images, list):
        return arguments
    sanitized_images = []
    for image in images:
        if isinstance(image, dict) and isinstance(image.get("data_base64"), str):
            data = image["data_base64"]
            digest = hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]
            image = {**image, "data_base64": f"sha256:{digest} ({len(data)} chars)"}
        sanitized_images.append(image)
    return {**arguments, "images": sanitized_images}


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "…[truncated]"


def _activity_keep(conn, user_id: str) -> int:
    row = conn.execute(
        "SELECT activity_keep FROM librarian_configs WHERE user_id = ?", (user_id,)
    ).fetchone()
    return int(row["activity_keep"]) if row is not None else 0


def journal_activity(
    db_path: str | Path,
    *,
    trace_id: str,
    user_id: str,
    token_label: str,
    tool: str,
    arguments: str,
    started_at: str,
    duration_ms: float,
    outcome: str,
    error_text: str | None,
    telemetry: RequestTelemetry,
) -> None:
    """Persist one activity row (A8: shared by the MCP middleware and the
    scheduler); prunes per the user's activity_keep. Synchronous sqlite I/O —
    callers on the event loop must offload via ``asyncio.to_thread``."""
    llm = telemetry.llm or {}
    conn = db.connect(db_path)
    try:
        db.insert_activity(
            conn,
            trace_id=trace_id,
            user_id=user_id,
            token_label=token_label,
            tool=tool,
            arguments=arguments,
            started_at=started_at,
            duration_ms=duration_ms,
            outcome=outcome,
            error=_truncate(error_text, MAX_ERR) if error_text else None,
            iterations=telemetry.iterations if telemetry.llm else None,
            prompt_tokens=llm.get("prompt_tokens"),
            completion_tokens=llm.get("completion_tokens"),
            total_tokens=llm.get("total_tokens"),
        )
        keep = _activity_keep(conn, user_id)
        if keep > 0:
            db.prune_activity(conn, user_id, keep)
    finally:
        conn.close()


class ActivityRegistry:
    """In-flight tool calls keyed by trace_id (threading.Lock-guarded).

    The MCP server runs tool handlers on the event loop and in threadpools
    while the WebUI reads snapshots from request threads, so all access goes
    through the lock and ``snapshot`` iterates a copy.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, dict] = {}

    def add(self, entry: dict) -> None:
        with self._lock:
            self._entries[entry["trace_id"]] = entry

    def remove(self, trace_id: str) -> None:
        with self._lock:
            self._entries.pop(trace_id, None)

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self._entries.values())


class ActivityMiddleware(Middleware):
    """Mint per-request telemetry; journal every authenticated tool call."""

    def __init__(self, db_path: str | Path, registry: ActivityRegistry) -> None:
        self._db_path = Path(db_path)
        self._registry = registry

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        telemetry = RequestTelemetry(trace_id=mint_request_id())
        token = _telemetry_var.set(telemetry)
        current = identity.get_current_identity()
        user_id, label = current if current is not None else (None, None)
        arguments = context.message.arguments or {}
        entry = {
            "trace_id": telemetry.trace_id,
            "user_id": user_id,
            "token_label": label,
            "tool": context.message.name,
            "arguments": _truncate(
                json.dumps(_sanitize_arguments(context.message.name, arguments), default=str),
                MAX_ARGS,
            ),
            "started_at": context.timestamp.isoformat(),
        }
        self._registry.add(entry)
        start = time.perf_counter()
        outcome, error_text = "ok", None
        try:
            return await call_next(context)
        except ToolError as exc:
            outcome, error_text = "error", str(exc)
            raise
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception as exc:
            outcome, error_text = "error", f"{type(exc).__name__}: {exc}"
            raise
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            self._registry.remove(telemetry.trace_id)
            # In-process unauthenticated calls are not journaled (user_id is
            # NOT NULL in the activity schema). The journal write is
            # synchronous sqlite I/O — off the event loop (A1).
            if user_id is not None:
                await asyncio.to_thread(
                    self._journal, entry, duration_ms, outcome, error_text, telemetry
                )
            _telemetry_var.reset(token)

    def _journal(
        self,
        entry: dict,
        duration_ms: float,
        outcome: str,
        error_text: str | None,
        telemetry: RequestTelemetry,
    ) -> None:
        journal_activity(
            self._db_path,
            trace_id=entry["trace_id"],
            user_id=entry["user_id"],
            token_label=entry["token_label"],
            tool=entry["tool"],
            arguments=entry["arguments"],
            started_at=entry["started_at"],
            duration_ms=duration_ms,
            outcome=outcome,
            error_text=error_text,
            telemetry=telemetry,
        )
