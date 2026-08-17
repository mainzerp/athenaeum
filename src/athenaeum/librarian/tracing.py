"""Per-request trace sessions, request telemetry, and the on-disk trace store.

Contract: PLAN.md section 0 (T1-T3, T5) and section 1. One JSON file per
request under ``<library_root>/.traces/<trace_id>.json``, written atomically.
A ``TraceSession`` is opened per MCP tool call, set into ``_trace_var``, and
fed by ``Librarian._dispatch_tracked``; a ``RequestTelemetry`` is minted per
request, set into ``_telemetry_var``, and fed by ``Librarian._run``. Both
ContextVars mirror the ``_identity_var`` mechanism in athenaeum.identity.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from athenaeum.librarian.tools import WRITE_ACTIONS
from athenaeum.library.frontmatter import write_text_atomic

logger = logging.getLogger(__name__)

TRACE_DIR = ".traces"

MAX_STR = 500  # recorded arg strings are truncated to this length
MAX_ITEMS = 50  # cap for shaped result lists (hits/entries/broken)
MAX_ERR = 500  # recorded error strings are truncated to this length

_TRACE_ID_RE = re.compile(r"^[0-9A-Za-z-]+$")

_SUMMARY_KEYS = (
    "trace_id",
    "tool",
    "agent_label",
    "started_at",
    "ended_at",
    "duration_ms",
    "outcome",
    "error",
)

_WRITE_TOOLS = frozenset(WRITE_ACTIONS)


def mint_request_id() -> str:
    """Filename-safe, sortable request id: ``<UTC timestamp>-<uuid8>``."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]


@dataclass
class RequestTelemetry:
    """LLM metadata for one in-flight request, mutated by the agent loop."""

    trace_id: str
    llm: dict | None = None
    llm_calls: list[dict] = field(default_factory=list)
    iterations: int = 0


class TraceStore:
    """JSON trace files for one library root (``.traces/<trace_id>.json``)."""

    def __init__(self, root: str | Path, keep: int = 0) -> None:
        self.root = Path(root)
        self.store = self.root / TRACE_DIR
        self.keep = keep

    def create(self, trace: dict) -> str:
        """Persist ``trace`` and return its trace_id. Prunes when keep > 0."""
        trace_id = str(trace.get("trace_id", ""))
        if not _TRACE_ID_RE.fullmatch(trace_id):
            raise ValueError(f"invalid trace id {trace_id!r}")
        write_text_atomic(
            self.store / f"{trace_id}.json",
            json.dumps(trace, indent=2, default=str) + "\n",
        )
        if self.keep > 0:
            self.prune(self.keep)
        return trace_id

    def list(self, limit: int = 100) -> list[dict]:
        """Trace summaries (no events), newest first."""
        out: list[dict] = []
        if self.store.is_dir():
            for path in sorted(self.store.glob("*.json"), reverse=True):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                out.append({key: data.get(key) for key in _SUMMARY_KEYS})
                if len(out) >= limit:
                    break
        return out

    def read(self, trace_id: str) -> dict:
        """Full trace JSON. Raises ValueError on bad ids (traversal guard)."""
        if not _TRACE_ID_RE.fullmatch(trace_id):
            raise ValueError(f"invalid trace id {trace_id!r}")
        path = self.store / f"{trace_id}.json"
        if not path.is_file():
            raise FileNotFoundError(f"no such trace: {trace_id!r}")
        return json.loads(path.read_text(encoding="utf-8"))

    def exists(self, trace_id: str) -> bool:
        """True when a trace file exists for ``trace_id`` (bad ids: False)."""
        if not _TRACE_ID_RE.fullmatch(trace_id):
            return False
        return (self.store / f"{trace_id}.json").is_file()

    def prune(self, keep_last: int) -> int:
        """Delete all but the newest ``keep_last`` traces; returns deletions.

        A per-file unlink failure (e.g. a locked file) is logged and skipped —
        pruning must never fail the trace write that triggered it.
        """
        if not self.store.is_dir():
            return 0
        files = sorted(f for f in self.store.glob("*.json") if _TRACE_ID_RE.fullmatch(f.stem))
        excess = files[: max(0, len(files) - keep_last)]
        deleted = 0
        for path in excess:
            try:
                path.unlink()
            except OSError:
                logger.warning("trace prune: could not delete %s; skipping", path, exc_info=True)
                continue
            deleted += 1
        return deleted


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "…[truncated]"


def _summarize_value(value: Any) -> Any:
    """Recursively truncate strings (T3); the structure is otherwise kept."""
    if isinstance(value, str):
        return _truncate(value, MAX_STR)
    if isinstance(value, dict):
        return {key: _summarize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_summarize_value(item) for item in value]
    return value


def _shape_result(name: str, args: dict, result: Any) -> Any:
    """Per-tool result shaping (T3): bodies dropped, hit paths kept intact."""
    if name == "read_document" and isinstance(result, dict):
        frontmatter = result.get("frontmatter") or {}
        return {
            "path": result.get("path"),
            "title": frontmatter.get("title"),
            "type": frontmatter.get("type"),
        }
    if name == "search_metadata" and isinstance(result, list):
        hits = [
            str(hit.get("path") or hit.get("id") or "") for hit in result if isinstance(hit, dict)
        ]
        return {"hits": hits[:MAX_ITEMS], "count": len(result)}
    if name == "search_semantic" and isinstance(result, list):
        hits = [
            {"path": hit.get("path"), "score": hit.get("score")}
            for hit in result[:MAX_ITEMS]
            if isinstance(hit, dict)
        ]
        return {
            "hits": hits,
            "count": len(result),
            "fallback": any(isinstance(hit, dict) and hit.get("fallback") for hit in result),
        }
    if name == "list_dir" and isinstance(result, list):
        entries = [
            str(entry.get("name", "")) if isinstance(entry, dict) else str(entry)
            for entry in result
        ]
        return {
            "path": args.get("path", "/"),
            "entries": entries[:MAX_ITEMS],
            "count": len(result),
        }
    if name == "link_check" and isinstance(result, list):
        broken = [
            {"source": item.get("source"), "target": item.get("target")}
            for item in result
            if isinstance(item, dict)
        ]
        return {"broken": broken[:MAX_ITEMS], "count": len(result)}
    if name in _WRITE_TOOLS and isinstance(result, dict):
        return dict(result)
    return _summarize_value(result)


class TraceSession:
    """Records one request's tool events; ``close`` writes the trace file.

    The file is written only when at least one event was recorded or LLM
    telemetry exists (D3: no file for a healthy maintain no-op, and
    ``library_status`` never opens a session).
    """

    def __init__(
        self,
        root: str | Path,
        trace_id: str,
        tool: str,
        agent_label: str | None,
        keep: int = 0,
    ) -> None:
        self.root = Path(root)
        self.trace_id = trace_id
        self.tool = tool
        self.agent_label = agent_label
        self.keep = keep
        self.started_at = datetime.now(UTC)
        self._clock_start = time.perf_counter()
        self._events: list[dict] = []
        self._outcome: str | None = None
        self._error: str | None = None
        self._ended_at: datetime | None = None
        self._closed = False
        self._pending_llm_ms: float | None = None

    def set_pending_llm_ms(self, llm_ms: float) -> None:
        """Queue the wall time of the LLM call whose response triggered the
        next tool event(s); ``record`` attaches it to the FIRST event only,
        so per-trace sums never double-count a multi-tool-call response."""
        self._pending_llm_ms = llm_ms

    def record(
        self,
        name: str,
        args: dict,
        result: Any,
        error: BaseException | str | None,
        duration_ms: float,
    ) -> None:
        """Append one tool event (T3 shaping applied to args/result/error)."""
        args = args or {}
        if error is None:
            error_text = None
        elif isinstance(error, BaseException):
            error_text = _truncate(f"{type(error).__name__}: {error}", MAX_ERR)
        else:
            error_text = _truncate(str(error), MAX_ERR)
        event = {
            "seq": len(self._events) + 1,
            "ts": datetime.now(UTC).isoformat(),
            "tool": name,
            "args": _summarize_value(args),
            "duration_ms": duration_ms,
            "result": None if error is not None else _shape_result(name, args, result),
            "error": error_text,
        }
        if self._pending_llm_ms is not None:
            event["llm_ms"] = self._pending_llm_ms
            self._pending_llm_ms = None
        self._events.append(event)

    def finish(self, outcome: str, error: str | None = None) -> None:
        """Mark the request's outcome; called once by the MCP handler layer."""
        self._outcome = outcome
        self._error = error
        self._ended_at = datetime.now(UTC)

    def close(self) -> str | None:
        """Write the trace file (when events or llm data exist); returns trace_id.

        Containment: a persistence failure is logged and swallowed (returns
        None) — trace writing must never fail the observed run.
        """
        if self._closed:
            return None
        self._closed = True
        telemetry = _telemetry_var.get()
        llm = dict(telemetry.llm) if telemetry is not None and telemetry.llm else None
        if not self._events and llm is None:
            return None
        ended_at = self._ended_at or datetime.now(UTC)
        trace = {
            "trace_id": self.trace_id,
            "tool": self.tool,
            "agent_label": self.agent_label,
            "started_at": self.started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "duration_ms": (time.perf_counter() - self._clock_start) * 1000,
            "outcome": self._outcome or "ok",
            "error": self._error,
            "llm": llm,
            "events": list(self._events),
        }
        try:
            return TraceStore(self.root, keep=self.keep).create(trace)
        except Exception:
            logger.warning(
                "trace persistence failed for %s; the run is unaffected",
                self.trace_id,
                exc_info=True,
            )
            return None


_trace_var: ContextVar[TraceSession | None] = ContextVar("athenaeum_trace", default=None)
_telemetry_var: ContextVar[RequestTelemetry | None] = ContextVar(
    "athenaeum_telemetry", default=None
)


def current_trace() -> TraceSession | None:
    """Trace session of the in-flight request, or None."""
    return _trace_var.get()


def telemetry_or_mint() -> RequestTelemetry:
    """Telemetry of the in-flight request; mint one when absent (no middleware)."""
    telemetry = _telemetry_var.get()
    if telemetry is None:
        telemetry = RequestTelemetry(trace_id=mint_request_id())
        _telemetry_var.set(telemetry)
    return telemetry
