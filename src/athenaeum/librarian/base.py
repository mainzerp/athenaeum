"""BaseAgent: shared agent plumbing for the Librarian and the Curator.

Holds the hand-rolled tool-calling loop (``_run``), tracked dispatch with the
duplicate-call guard and tracing (``_dispatch_tracked``), the tracker/result
dataclasses, the LLM provider lifecycle, and the embedding-reconcile /
shutdown machinery. ``Librarian`` (librarian/agent.py) and ``Curator``
(curator/agent.py) are thin subclasses owning their handlers; ``tool_schemas``
is the per-agent tool surface (the full TOOL_SCHEMAS for the librarian).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athenaeum import __version__
from athenaeum.librarian.embed import EmbeddingConfig
from athenaeum.librarian.gate import RunGate
from athenaeum.librarian.llm import LLMConfig, LLMProvider, LLMResponse, create_provider
from athenaeum.librarian.prompts import build_system_prompt
from athenaeum.librarian.tools import TOOL_SCHEMAS, WRITE_ACTIONS, dispatch
from athenaeum.librarian.tracing import _telemetry_var, _trace_var

if TYPE_CHECKING:
    from athenaeum.embeddings import EmbeddingService
    from athenaeum.librarian.tools import Backend

logger = logging.getLogger(__name__)

FINAL_ANSWER_REQUEST = (
    "You have reached the tool-use limit for this request. Provide your final "
    "answer now from what you have already gathered; do not call more tools. "
    "Answer with results and citations only: never narrate your process, the "
    "tool-use limit, or what you could not read — name unread items as plain "
    "coverage gaps."
)

RESERVED_NAMES = {"index.md", "log.md"}

PARTIAL_SUMMARY = (
    "The run was interrupted by a provider error after the listed writes landed. "
    "Those writes were kept (embeddings still synced); retry the request to "
    "finish the task."
)

WRITE_NUDGE_REMAINING = 2  # mid-loop write nudge fires when this many iterations remain

WRITE_NUDGE = (
    f"WRITE NOW: only {WRITE_NUDGE_REMAINING} iterations remain and nothing has "
    "been written yet. Stop searching and reading — call write_concept (or "
    "edit_concept) immediately with the content you have already prepared. A "
    "store that ends without a write is a failed store."
)


class LibrarianNotConfiguredError(RuntimeError):
    """Raised when an agent-backed handler runs without an LLM config."""


class _ProviderRunError(Exception):
    """Mid-loop provider failure AFTER writes landed; carries the tracker (L2).

    Raised by ``_run`` in place of the original provider exception so the
    write-task path can recover the landed writes as a partial-success
    result instead of discarding them (F16).
    """

    def __init__(self, message: str, *, tracker: _Tracker, iterations: int) -> None:
        super().__init__(message)
        self.tracker = tracker
        self.iterations = iterations


@dataclass
class LibrarianConfig:
    """Loaded per-user librarian configuration (manager-resolved connection
    configs from ``provider_configs`` + the per-agent binding columns)."""

    user_id: str
    llm: LLMConfig | None = None  # None = unconfigured librarian
    curate_llm: LLMConfig | None = None  # resolved curator config; None = use llm
    prompt_addendum: str | None = None  # None = built-in default prompt only
    git_enabled: bool = True  # commit every library write to git history
    git_remote_url: str | None = None  # None = no remote configured
    git_auto_push: bool = False  # push to the remote after each commit
    trace_keep: int = 0  # 0 = keep all
    activity_keep: int = 0  # 0 = keep all
    payload_keep: int = 0  # 0 = keep all
    library_name: str | None = None
    library_description: str | None = None
    curate_prompt_addendum: str | None = None  # None = no addendum
    curate_last_run_at: str | None = None  # None = never curated
    embedding: EmbeddingConfig | None = None  # None = embeddings unconfigured
    semantic_threshold: float | None = None  # None = per-model default
    hybrid_search: bool = True  # fuse semantic with FTS5 BM25 (RRF)
    hybrid_rerank: bool = False  # optional CPU-heavy cross-encoder pass (0.23.0: default off)


@dataclass
class _Tracker:
    """Backend interactions observed during one agent run.

    Also carries the per-run dedupe state (``seen_calls``/``seen_queries``)
    for the duplicate-call guard in ``_dispatch_tracked``; a fresh tracker is
    minted per ``_run``, so the state resets on the F11 no-write nudge retry.
    """

    read_paths: list[str] = field(default_factory=list)
    writes: list[dict] = field(default_factory=list)  # {"id", "action"}
    seen_calls: set[tuple[str, str]] = field(default_factory=set)  # exact dedupe keys
    seen_queries: list[frozenset[str]] = field(default_factory=list)  # search_semantic tokens


@dataclass
class _RunResult:
    text: str
    tracker: _Tracker
    iterations: int = 0
    usage: dict[str, int] | None = None  # summed tokens; None when never reported
    partial: bool = False  # True: provider failed mid-loop after writes landed (L2)


TOOL_RESULT_CHAR_LIMIT = 12_000  # per-tool-result cap fed back to the model (A11)

# TUNABLE: token-set Jaccard similarity at which a search_semantic query counts
# as a near-duplicate of an earlier successful query in the same run.
SEMANTIC_QUERY_SIMILARITY_THRESHOLD = 0.8

# Retrieval tools the duplicate-call guard applies to. run_computation (fresh
# verified figures are its contract), link_check (a whole-library re-check
# after a repair is legitimate), and all WRITE_ACTIONS are never deduped.
_DEDUPE_TOOLS = frozenset({"list_dir", "read_document", "search_metadata", "search_semantic"})


def _dedupe_key(name: str, args: dict) -> tuple[str, str]:
    """Exact dedupe key for a tool call: name + canonical JSON of its args."""
    return (name, json.dumps(args, sort_keys=True, default=str))


def _query_tokens(query: str) -> frozenset[str]:
    """Token set of a semantic query for fuzzy duplicate detection."""
    return frozenset(re.findall(r"[a-z0-9]+", query.lower()))


def _duplicate_result(name: str, *, near: bool) -> dict:
    """Synthetic result for a suppressed duplicate tool call.

    Returned (not raised) so the loop's success path serializes it into the
    tool message like any other result; the "error" key matches the
    recoverable-error envelope the model already knows.
    """
    earlier = "a near-identical search_semantic call" if near else "an identical call"
    return {
        "deduplicated": True,
        "error": (
            f"duplicate {name} call suppressed: {earlier} already ran "
            "successfully in this run — work from the earlier result instead of re-calling"
        ),
    }


def _truncate_tool_result(content: str) -> str:
    """Cap one tool result before it enters the message history (A11).

    Only the model-facing copy is bounded — the server-side trace record
    (with its own shaping) is written by _dispatch_tracked before this runs.
    Without this cap a few large read_document calls or one unfiltered
    search could blow the context window mid-run.
    """
    if len(content) <= TOOL_RESULT_CHAR_LIMIT:
        return content
    omitted = len(content) - TOOL_RESULT_CHAR_LIMIT
    return content[:TOOL_RESULT_CHAR_LIMIT] + f"... [truncated: {omitted} characters omitted]"


def _post_run_note(converged: bool, done: str, remaining: str) -> str:
    """Deterministic post-run verdict appended to an LLM summary (L15)."""
    return f"\n\nPost-run check: {done if converged else remaining}"


class BaseAgent:
    """One agent bound to one library root and one user config."""

    # Per-agent tool surface: the librarian offers the full schema list; the
    # Curator narrows it (curator/tools.py).
    tool_schemas: list[dict] = TOOL_SCHEMAS

    def __init__(
        self,
        root: str | Path,
        config: LibrarianConfig,
        *,
        backend: Backend | None = None,
        provider: LLMProvider | None = None,
        embedding_service: EmbeddingService | None = None,
        run_gate: RunGate | None = None,
        reranker=None,
        computation_runner=None,
    ) -> None:
        self.root = Path(root)
        self.config = config
        self._embed = embedding_service
        self._reranker = reranker
        # Shared ComputationRunner (manager-built); None in standalone/tests:
        # the internal run_computation tool then reports "not available".
        self._computation_runner = computation_runner
        self._embed_reconcile_pending = False
        # A5: strong reference to the in-flight reconcile task (GC could
        # otherwise destroy a fire-and-forget task mid-run) plus its owning
        # loop so shutdown() can cancel it from any thread.
        self._embed_reconcile_task: asyncio.Task | None = None
        self._embed_reconcile_loop: asyncio.AbstractEventLoop | None = None
        # Standalone instances (tests) get a private gate; the manager injects
        # its shared one so all of a user's librarians serialize together.
        self._run_gate = run_gate if run_gate is not None else RunGate()
        if backend is None:
            backend = self._build_backend()
        self.backend = backend
        self._provider = provider

    def _build_backend(self) -> Backend:
        # Lazy import: library/ is Stream A's module; only needed when no
        # backend is injected (integration path).
        from athenaeum.library.backend import LibraryBackend

        created = not self.root.exists()
        self.root.mkdir(parents=True, exist_ok=True)
        backend = LibraryBackend(
            self.root,
            actor=f"athenaeum-librarian/{__version__}",
            git_enabled=self.config.git_enabled,
            git_remote_url=self.config.git_remote_url,
            git_auto_push=self.config.git_auto_push,
            embedding_service=self._embed,
            hybrid_search=self.config.hybrid_search,
            hybrid_rerank=self.config.hybrid_rerank,
            reranker=self._reranker,
        )
        if created:
            backend.init_bundle()
        else:
            try:
                backend.reconcile()  # lazy crash-safety pass (plan Step 4)
            except Exception as exc:
                logger.warning("reconcile failed for %s: %s", self.root, exc)
        # Flag only: this method is sync and must stay async-safe (the
        # reconcile fires via _maybe_embed_reconcile on the first async entry).
        self._embed_reconcile_pending = self._embed is not None
        return backend

    @property
    def configured(self) -> bool:
        return self.config.llm is not None

    @property
    def system_prompt(self) -> str:
        return build_system_prompt(self.config.prompt_addendum)

    @property
    def provider(self) -> LLMProvider:
        if not self.configured:
            raise LibrarianNotConfiguredError(
                "Librarian is not configured: set an LLM provider, model, and "
                "API key in the librarian settings."
            )
        if self._provider is None:
            self._provider = create_provider(self.config.llm)  # type: ignore[union-attr]
        return self._provider

    # --- agent loop -----------------------------------------------------

    async def _dispatch_tracked(
        self,
        name: str,
        args: dict,
        agent_label: str | None,
        tracker: _Tracker,
        requested_by: str | None = None,
        via: str | None = None,
    ) -> Any:
        if name in _DEDUPE_TOOLS:
            key = _dedupe_key(name, args)
            duplicate = key in tracker.seen_calls
            near = False
            if not duplicate and name == "search_semantic":
                tokens = _query_tokens(str(args.get("query", "")))
                near = any(
                    tokens
                    and len(tokens & seen) / len(tokens | seen)
                    >= SEMANTIC_QUERY_SIMILARITY_THRESHOLD
                    for seen in tracker.seen_queries
                )
            if duplicate or near:
                result = _duplicate_result(name, near=near)
                session = _trace_var.get()
                if session is not None:
                    # Record the suppressed duplicate so traces stay faithful
                    # to what the model actually asked for.
                    session.record(name, args, result, None, 0.0)
                return result
        session = _trace_var.get()
        if session is None:
            result = await dispatch(
                name,
                args,
                self.backend,
                agent_label,
                requested_by=requested_by,
                via=via,
                computation_runner=self._computation_runner,
            )
        else:
            start = time.perf_counter()
            try:
                result = await dispatch(
                    name,
                    args,
                    self.backend,
                    agent_label,
                    requested_by=requested_by,
                    via=via,
                    computation_runner=self._computation_runner,
                )
            except Exception as exc:
                # Record the failed hop, then re-raise so the loop's catch
                # still feeds the error back to the model.
                session.record(name, args, None, exc, (time.perf_counter() - start) * 1000)
                raise
            session.record(name, args, result, None, (time.perf_counter() - start) * 1000)
        if name in _DEDUPE_TOOLS:
            # Only successful dispatches are recorded, so a failed call may be
            # retried. Writes do NOT invalidate dedupe state: the model
            # authored the change itself, so re-reading a path it just wrote
            # carries no new information and stays suppressed.
            tracker.seen_calls.add(_dedupe_key(name, args))
            if name == "search_semantic":
                tracker.seen_queries.append(_query_tokens(str(args.get("query", ""))))
        if name == "read_document":
            tracker.read_paths.append(args["path"])
        if name in WRITE_ACTIONS and isinstance(result, dict) and "id" in result:
            write = {"id": result["id"], "action": result.get("action", WRITE_ACTIONS[name])}
            if name == "move_concept":
                # L8: carry the OLD id (bundle-shaped like the result id) so
                # the embedding sync can delete the stale old-path row.
                old = str(args.get("old_path", "")).replace("\\", "/")
                if old.endswith(".md"):
                    old = old[: -len(".md")]
                if old and not old.startswith("/"):
                    old = "/" + old
                if old:
                    write["from_id"] = old
            tracker.writes.append(write)
        return result

    async def _run(
        self,
        task_prompt: str,
        agent_label: str | None,
        *,
        llm_config: LLMConfig | None = None,
        provider: LLMProvider | None = None,
        requested_by: str | None = None,
        via: str | None = None,
        write_task: bool = False,
    ) -> _RunResult:
        """Hand-rolled tool-calling loop (plan section 3.4)."""
        llm_config = llm_config or self.config.llm
        if llm_config is None:
            raise LibrarianNotConfiguredError(
                "Librarian is not configured: set an LLM provider, model, and "
                "API key in the librarian settings."
            )
        provider = provider or self.provider
        tracker = _Tracker()
        telemetry = _telemetry_var.get()
        llm_calls: list[dict] = []
        totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        usage_seen = False

        def track_usage(usage: dict[str, int] | None) -> None:
            nonlocal usage_seen
            llm_calls.append({"model": llm_config.model, "usage": usage})
            if usage:
                usage_seen = True
                for key in totals:
                    totals[key] += int(usage.get(key) or 0)

        llm_ms_total = 0.0

        async def complete_timed(messages: list[dict], tools: list[dict]) -> LLMResponse:
            """provider.complete with wall-time accounting: per-step attribution
            via the trace session's pending slot + a run total for telemetry."""
            nonlocal llm_ms_total
            start = time.perf_counter()
            response = await provider.complete(messages, tools, llm_config)
            elapsed_ms = (time.perf_counter() - start) * 1000
            llm_ms_total += elapsed_ms
            session = _trace_var.get()
            if session is not None:
                session.set_pending_llm_ms(elapsed_ms)
            track_usage(response.usage)
            return response

        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": task_prompt},
        ]
        response = await complete_timed(messages, self.tool_schemas)
        iterations = 0
        write_nudge_sent = False
        while response.has_tool_calls and iterations < llm_config.max_iterations:
            iterations += 1
            # Per-hop budget marker prefixed to every tool message (never a
            # suffix: _truncate_tool_result cannot cut a prefix). Wording says
            # "iterations" — one iteration = one assistant response.
            budget = f"[budget: {llm_config.max_iterations - iterations} iterations remaining] "
            messages.append(
                {
                    "role": "assistant",
                    "content": response.text,
                    "tool_calls": [asdict(tc) for tc in response.tool_calls],
                }
            )
            for call in response.tool_calls:
                try:
                    result = await self._dispatch_tracked(
                        call.name,
                        call.arguments,
                        agent_label,
                        tracker,
                        requested_by=requested_by,
                        via=via,
                    )
                    # A11: bound the model-facing copy; the server-side trace
                    # record was already written by _dispatch_tracked.
                    content = _truncate_tool_result(json.dumps(result, default=str))
                except Exception as exc:  # tool errors go back to the model
                    logger.warning("tool %s failed: %s", call.name, exc)
                    content = json.dumps({"error": f"{type(exc).__name__}: {exc}"})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": budget + content,
                    }
                )
            if (
                write_task
                and not write_nudge_sent
                and not tracker.writes
                and llm_config.max_iterations - iterations == WRITE_NUDGE_REMAINING
            ):
                # Store-watchdog: the model spent the budget on retrieval and has not
                # written; one direct instruction lands better than another nudge later
                # (F11/F13 history). Once per run; the F11 retry gets a fresh flag.
                messages.append({"role": "user", "content": WRITE_NUDGE})
                write_nudge_sent = True
            try:
                response = await complete_timed(messages, self.tool_schemas)
            except Exception as exc:
                raise self._provider_failure(exc, tracker, iterations) from exc
        if response.has_tool_calls:
            # Cap exhausted: final-answer request with no tools.
            messages.append({"role": "user", "content": FINAL_ANSWER_REQUEST})
            try:
                response = await complete_timed(messages, [])
            except Exception as exc:
                raise self._provider_failure(exc, tracker, iterations) from exc
        if telemetry is not None:
            # Accumulate, never overwrite: the F11 nudge retry runs _run twice
            # in one request, and the retry's numbers must ADD to the first
            # run's (llm_calls already extends), otherwise the journal/trace
            # summary under-accounts the tokens the first run burned.
            telemetry.iterations += iterations
            telemetry.llm_calls.extend(llm_calls)
            merged = dict(telemetry.llm) if telemetry.llm else {}
            merged["provider"] = llm_config.provider
            merged["model"] = llm_config.model
            merged["iterations"] = merged.get("iterations", 0) + iterations
            for key, value in totals.items():
                merged[key] = merged.get(key, 0) + value
            merged["llm_ms_total"] = merged.get("llm_ms_total", 0.0) + llm_ms_total
            telemetry.llm = merged
        return _RunResult(
            text=response.text or "",
            tracker=tracker,
            iterations=iterations,
            usage=dict(totals) if usage_seen else None,
        )

    @staticmethod
    def _provider_failure(exc: Exception, tracker: _Tracker, iterations: int) -> Exception:
        """Mid-loop provider failure: keep landed writes recoverable (L2).

        With writes in the tracker the failure becomes a _ProviderRunError so
        the write-task path can surface a partial-success result (and still
        sync embeddings); without writes the original exception propagates.
        """
        if tracker.writes:
            return _ProviderRunError(str(exc), tracker=tracker, iterations=iterations)
        return exc

    # --- post-processing -------------------------------------------------

    async def _stored_entries(self, tracker: _Tracker) -> list[dict]:
        """Map tracked writes onto the store_knowledge output vocabulary.

        Moves surface as ``moved`` with a ``from_id`` (the old concept id)
        so the embedding sync deletes the stale old-path row (L8).
        """
        stored: list[dict] = []
        for write in tracker.writes:
            action = write["action"]
            title = write["id"]
            if action != "deleted":
                try:
                    doc = await asyncio.to_thread(self.backend.read_document, f"{write['id']}.md")
                    title = (doc.get("frontmatter") or {}).get("title", title)
                except Exception as exc:
                    # Title falls back to the concept id; the entry is kept.
                    logger.warning(
                        "stored entry: title lookup failed for %s; using the id (%s)",
                        write["id"],
                        exc,
                    )
            entry = {"id": write["id"], "title": title, "action": action}
            if write.get("from_id"):
                entry["from_id"] = write["from_id"]
            stored.append(entry)
        return stored

    # --- lifecycle / embeddings ---------------------------------------------

    def _maybe_embed_reconcile(self) -> None:
        """Fire the deferred embedding reconcile on the first async entry.

        ``_build_backend`` (sync, no event loop) only sets the flag; the
        service's DB claim makes double-scheduling harmless. The task is
        retained on the instance (A5): a fire-and-forget task can be
        garbage-collected mid-run, and eviction/shutdown must be able to
        cancel it.
        """
        if not self._embed_reconcile_pending or self._embed is None:
            return
        self._embed_reconcile_pending = False
        if self._embed_reconcile_task is not None and not self._embed_reconcile_task.done():
            return  # one reconcile in flight per agent
        self._embed_reconcile_loop = asyncio.get_running_loop()
        task = asyncio.create_task(self._embed_reconcile_logged())
        self._embed_reconcile_task = task
        task.add_done_callback(self._clear_reconcile_task)

    def _clear_reconcile_task(self, task: asyncio.Task) -> None:
        if self._embed_reconcile_task is task:
            self._embed_reconcile_task = None

    def shutdown(self) -> None:
        """Cancel a pending embedding reconcile (eviction/shutdown hook, A5).

        Safe from any thread: cancellation is delivered through the owning
        loop. The service releases its ``embed_reconcile_claims`` row in a
        ``finally``, so a cancelled reconcile never blocks the next one for
        the claim TTL.
        """
        task = self._embed_reconcile_task
        self._embed_reconcile_task = None
        if task is None or task.done():
            return
        loop = self._embed_reconcile_loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(task.cancel)
        else:
            task.cancel()

    async def _embed_reconcile_logged(self) -> None:
        try:
            await self._embed.reconcile(self.backend)
        except Exception:
            logger.exception("embedding reconcile failed for user %s", self.config.user_id)

    async def sync_embeddings(self, writes: list[dict]) -> None:
        """Write-through embedding sync; no-op when unconfigured or no writes."""
        if self._embed is None or not writes:
            return
        await self._embed.sync_writes(self.backend, writes)
