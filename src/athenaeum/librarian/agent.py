"""Librarian agent: hand-rolled tool-calling loop over LibraryBackend.

Contract: plan sections 3.4/3.4a. The loop is a plain while loop with a
max_iterations cap — no framework, no streaming. On cap exhaustion a final
answer is requested with no tools. Four handlers back the MCP tools:
``handle_request`` / ``handle_store`` / ``handle_update`` / ``handle_maintain``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athenaeum import __version__
from athenaeum.librarian.embed import EmbeddingConfig
from athenaeum.librarian.gate import AgentRunBusyError, RunGate
from athenaeum.librarian.llm import LLMConfig, LLMProvider, create_provider
from athenaeum.librarian.prompts import (
    build_curate_preamble,
    build_maintain_preamble,
    build_system_prompt,
)
from athenaeum.librarian.tools import TOOL_SCHEMAS, WRITE_ACTIONS, dispatch
from athenaeum.librarian.tracing import (
    _telemetry_var,
    _trace_var,
    mint_request_id,
    telemetry_or_mint,
)
from athenaeum.library.payloads import PayloadStore
from athenaeum.okf import is_stale, trust_tier

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

STORE_RELATED_TOP_K = 5  # tuning parameter for the store/update related-concepts injection

MAX_STORE_PAYLOAD_REVIEWS = 5  # curate digest cap for failed/partial store payloads
MAX_PAYLOAD_EXCERPT = 120  # per-entry content excerpt cap in that digest


class LibrarianNotConfiguredError(RuntimeError):
    """Raised when an agent-backed handler runs without an LLM config."""


class LibrarianNoWriteError(RuntimeError):
    """Raised when a write task completes without any writes landing."""


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


KIND_LIBRARIAN = "librarian"  # handle_request/handle_store/handle_update
KIND_CURATOR = "curator"  # handle_maintain/handle_curate


NO_WRITE_NUDGE = (
    "\n\nYou finished without writing anything. That is a failed store: apply "
    "your write discipline now — the context you gathered is enough; write the "
    "concept(s) and their back-links."
)

PARTIAL_SUMMARY = (
    "The run was interrupted by a provider error after the listed writes landed. "
    "Those writes were kept (embeddings still synced); retry the request to "
    "finish the task."
)


@dataclass
class LibrarianConfig:
    """Loaded per-user librarian configuration (manager-resolved connection
    configs from ``provider_configs`` + the per-agent binding columns)."""

    user_id: str
    llm: LLMConfig | None = None  # None = unconfigured librarian
    curate_llm: LLMConfig | None = None  # resolved curator config; None = use llm
    prompt_addendum: str | None = None  # None = built-in default prompt only
    versioning: bool = True
    snapshot_keep: int = 0  # 0 = keep all
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
    hybrid_rerank: bool = True  # local cross-encoder pass over fused candidates


@dataclass
class _Tracker:
    """Backend interactions observed during one agent run."""

    read_paths: list[str] = field(default_factory=list)
    writes: list[dict] = field(default_factory=list)  # {"id", "action"}


@dataclass
class _RunResult:
    text: str
    tracker: _Tracker
    iterations: int = 0
    usage: dict[str, int] | None = None  # summed tokens; None when never reported
    partial: bool = False  # True: provider failed mid-loop after writes landed (L2)


TOOL_RESULT_CHAR_LIMIT = 12_000  # per-tool-result cap fed back to the model (A11)


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


def _parse_follow_ups(text: str) -> list[str]:
    """Extract bullets under a '## Follow-ups' heading, if the model emitted one."""
    follow_ups: list[str] = []
    in_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            in_section = stripped.lstrip("#").strip().lower() == "follow-ups"
            continue
        if in_section and stripped.startswith("- "):
            item = stripped[2:].strip()
            if item:
                follow_ups.append(item)
    return follow_ups


def _parse_contradictions(text: str) -> list[dict]:
    """Extract '- <concept id>: <note>' bullets under a '## Contradictions' heading.

    Mirrors _parse_follow_ups (same heading convention); pure string work,
    never raises. '- none' is the explicit empty marker, not an entry.
    """
    contradictions: list[dict] = []
    in_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            in_section = stripped.lstrip("#").strip().lower() == "contradictions"
            continue
        if in_section and stripped.startswith("- "):
            item = stripped[2:].strip()
            if not item or item.lower() == "none":
                continue
            concept_id, sep, note = item.partition(":")
            contradictions.append({"id": concept_id.strip(), "note": note.strip() if sep else ""})
    return contradictions


def _payload_image_refs(images: list[dict] | None) -> list[dict]:
    """Archive image params as refs only (D3.4): the bytes live once in the
    content-addressed asset store, never duplicated as base64 into JSON. The
    predicted asset path matches ``LibraryBackend.write_asset`` naming."""
    refs: list[dict] = []
    for image in images or []:
        data = image.get("data") or b""
        digest = hashlib.sha256(data).hexdigest()
        refs.append(
            {
                "filename": image.get("filename"),
                "media_type": image.get("media_type"),
                "bytes": len(data),
                "sha256": digest,
                "asset": f"/.athenaeum/assets/{digest[:12]}-{image.get('filename')}",
            }
        )
    return refs


class Librarian:
    """One librarian bound to one library root and one user config."""

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
    ) -> None:
        self.root = Path(root)
        self.config = config
        self._embed = embedding_service
        self._reranker = reranker
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
        self._curate_provider: LLMProvider | None = None

    def _build_backend(self) -> Backend:
        # Lazy import: library/ is Stream A's module; only needed when no
        # backend is injected (integration path).
        from athenaeum.library.backend import LibraryBackend

        created = not self.root.exists()
        self.root.mkdir(parents=True, exist_ok=True)
        backend = LibraryBackend(
            self.root,
            actor=f"athenaeum-librarian/{__version__}",
            versioning=self.config.versioning,
            snapshot_keep=self.config.snapshot_keep,
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
        self, name: str, args: dict, agent_label: str | None, tracker: _Tracker
    ) -> Any:
        session = _trace_var.get()
        if session is None:
            result = await dispatch(name, args, self.backend, agent_label)
        else:
            start = time.perf_counter()
            try:
                result = await dispatch(name, args, self.backend, agent_label)
            except Exception as exc:
                # Record the failed hop, then re-raise so the loop's catch
                # still feeds the error back to the model.
                session.record(name, args, None, exc, (time.perf_counter() - start) * 1000)
                raise
            session.record(name, args, result, None, (time.perf_counter() - start) * 1000)
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

        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": task_prompt},
        ]
        response = await provider.complete(messages, TOOL_SCHEMAS, llm_config)
        track_usage(response.usage)
        iterations = 0
        while response.has_tool_calls and iterations < llm_config.max_iterations:
            iterations += 1
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
                        call.name, call.arguments, agent_label, tracker
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
                        "content": content,
                    }
                )
            try:
                response = await provider.complete(messages, TOOL_SCHEMAS, llm_config)
            except Exception as exc:
                raise self._provider_failure(exc, tracker, iterations) from exc
            track_usage(response.usage)
        if response.has_tool_calls:
            # Cap exhausted: final-answer request with no tools.
            messages.append({"role": "user", "content": FINAL_ANSWER_REQUEST})
            try:
                response = await provider.complete(messages, [], llm_config)
            except Exception as exc:
                raise self._provider_failure(exc, tracker, iterations) from exc
            track_usage(response.usage)
        if telemetry is not None:
            telemetry.iterations = iterations
            telemetry.llm_calls.extend(llm_calls)
            telemetry.llm = {
                "provider": llm_config.provider,
                "model": llm_config.model,
                "iterations": iterations,
                **totals,
            }
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

    def _concept_entries(self, tracker: _Tracker) -> list[dict]:
        """Deterministic concept metadata for every concept the run touched."""
        candidates: list[str] = []
        for path in tracker.read_paths:
            name = path.rsplit("/", 1)[-1]
            if path.endswith(".md") and name not in RESERVED_NAMES:
                candidates.append(path)
        for write in tracker.writes:
            candidates.append(f"{write['id']}.md")
        entries: list[dict] = []
        seen: set[str] = set()
        for path in candidates:
            concept_id = path[: -len(".md")]
            if concept_id in seen:
                continue
            seen.add(concept_id)
            try:
                doc = self.backend.read_document(path)
            except Exception as exc:
                # e.g. deleted during the run — the entry is skipped, not fatal
                logger.warning("concept entry: read failed for %s; skipping (%s)", path, exc)
                continue
            frontmatter = doc.get("frontmatter") or {}
            entries.append(
                {
                    "id": concept_id,
                    "title": frontmatter.get("title", concept_id),
                    "type": frontmatter.get("type", ""),
                    "trust_tier": trust_tier(frontmatter),
                    "stale": is_stale(frontmatter),
                }
            )
        return entries

    def _stored_entries(self, tracker: _Tracker) -> list[dict]:
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
                    doc = self.backend.read_document(f"{write['id']}.md")
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

    # --- handlers (back the MCP tools, plan section 3.1) ------------------

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
            return  # one reconcile in flight per librarian
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

    async def handle_request(
        self,
        query: str,
        context: str | None = None,
        *,
        agent_label: str | None = None,
    ) -> dict:
        async with self._run_gate.acquire(self.config.user_id, KIND_LIBRARIAN, wait=False):
            self._maybe_embed_reconcile()
            task = (
                "REQUEST TASK. An external agent asks for knowledge from the library.\n"
                f"Query: {query}\n"
                f"Context from the caller: {context or 'none provided'}\n\n"
                "Retrieve what you need per your retrieval discipline, then answer. "
                "Cite concept IDs and surface trust/staleness for everything you cite. "
                "If useful, end with a '## Follow-ups' section of up to 3 suggested "
                "follow-up questions as markdown bullets."
            )
            result = await self._run(task, agent_label)
            answer: dict[str, Any] = {
                "answer": result.text,
                "concepts": self._concept_entries(result.tracker),
            }
            follow_ups = _parse_follow_ups(result.text)
            if follow_ups:
                answer["follow_ups"] = follow_ups
            return answer

    async def _run_write_task(self, task: str, agent_label: str | None) -> _RunResult:
        """Run a write-intent task; retry once when nothing was written (F11).

        L1: a run counts as successful ONLY when at least one write landed —
        a non-empty summary alone (e.g. after cap exhaustion) is a failure.
        L2: a mid-loop provider failure after landed writes returns a
        partial-success result (``partial=True``) instead of raising.
        """
        result = await self._run_write_once(task, agent_label)
        if self._stored_entries(result.tracker):
            return result
        retry = await self._run_write_once(task + NO_WRITE_NUDGE, agent_label)
        if self._stored_entries(retry.tracker):
            return retry
        raise LibrarianNoWriteError("librarian completed the write task without writing anything")

    async def _run_write_once(self, task: str, agent_label: str | None) -> _RunResult:
        try:
            return await self._run(task, agent_label)
        except _ProviderRunError as exc:
            logger.warning(
                "provider failed mid-run; keeping %d landed write(s)", len(exc.tracker.writes)
            )
            return _RunResult(text="", tracker=exc.tracker, iterations=exc.iterations, partial=True)

    async def _related_section(self, text: str) -> str:
        """Related-concepts preamble section; "" when unconfigured/failed/empty.

        One embed call + one store query OUTSIDE the loop — zero loop-iteration
        cost. A failure never blocks the write task.
        """
        if self._embed is None:
            return ""
        try:
            ranked = await self._embed.related(text, STORE_RELATED_TOP_K)
        except Exception:
            logger.warning("related-concepts lookup failed; continuing without it", exc_info=True)
            return ""
        lines = []
        for concept_id, score in ranked:
            try:
                doc = self.backend.read_document(f"{concept_id}.md")
            except Exception as exc:
                logger.warning(
                    "related-concepts: read failed for %s; skipping (%s)", concept_id, exc
                )
                continue
            title = (doc.get("frontmatter") or {}).get("title") or concept_id
            lines.append(f"- {concept_id} ({title}) — similarity {score:.2f}")
        if not lines:
            return ""
        return (
            "Possibly related existing concepts (semantic similarity — scores are "
            "relative ranking aids, not trust; verify with read_document before "
            "linking or enriching):\n" + "\n".join(lines) + "\n\n"
        )

    async def _store_image_assets(self, images: list[dict]) -> str:
        """Write attached images to the asset store; return the task section.

        Server-side only: the bytes go to the content-addressed asset store
        (off the event loop, A1) and the prompt sees absolute markdown image
        links — base64 never enters the prompt.
        """
        if not images:
            return ""
        lines = []
        for image in images:
            asset = await asyncio.to_thread(
                self.backend.write_asset, image["filename"], image["data"]
            )
            lines.append(f"![{image['filename']}]({asset})")
        return (
            "Attached images (already stored in the library's asset store; "
            "place these markdown image links into the stored concept(s) "
            "where they belong):\n" + "\n".join(lines) + "\n\n"
        )

    async def _archive_payload(self, payloads: PayloadStore, record: dict) -> None:
        """Best-effort payload archive write (D3.2): logged, never raised."""
        try:
            await asyncio.to_thread(payloads.create, record)
        except Exception:
            logger.warning("payload archive write failed; continuing", exc_info=True)

    async def handle_store(
        self,
        content: str,
        kind_hint: str | None = None,
        relates_to: list[str] | None = None,
        topic_hint: str | None = None,
        images: list[dict] | None = None,
        *,
        agent_label: str | None = None,
    ) -> dict:
        # D3.2: two-phase payload archive. The "received" record is written
        # BEFORE the run-gate acquire so busy rejections are recorded too;
        # the exit rewrite (same request_id, atomic overwrite) carries the
        # final outcome. Best-effort: archive failures never fail the store.
        payloads = PayloadStore(self.root, keep=self.config.payload_keep)
        record: dict[str, Any] = {
            "request_id": mint_request_id(),
            "tool": "store_knowledge",
            "user_id": self.config.user_id,
            "agent_label": agent_label,
            "trace_id": telemetry_or_mint().trace_id,
            "received_at": datetime.now(UTC).isoformat(),
            "outcome": "received",
            "error": None,
            "params": {
                "content": content,
                "kind_hint": kind_hint,
                "relates_to": relates_to,
                "topic_hint": topic_hint,
                "images": _payload_image_refs(images),
            },
            "stored": [],
        }
        await self._archive_payload(payloads, record)
        try:
            async with self._run_gate.acquire(self.config.user_id, KIND_LIBRARIAN, wait=False):
                self._maybe_embed_reconcile()
                related = await self._related_section(content)
                attached = await self._store_image_assets(images or [])
                task = (
                    "STORE TASK. An external agent wants to persist knowledge in the library.\n"
                    f"Content:\n{content}\n\n"
                    f"Kind hint from the caller: {kind_hint or 'none provided'}\n"
                    f"Topic hint from the caller: {topic_hint or 'none provided'}\n"
                    f"Related concept IDs suggested by the caller: "
                    f"{', '.join(relates_to) if relates_to else 'none provided'}\n\n"
                    f"{related}"
                    f"{attached}"
                    "This is NEW knowledge to add. Apply your write discipline: search "
                    "first, enrich in place when the knowledge is an attribute or "
                    "detail of an existing concept, create only for genuinely new "
                    "subjects, and back-link every new concept from a related one. "
                    "Caller-suggested related concepts and similarity-ranked "
                    "candidates are back-link candidates, NOT placement hints — "
                    "placement follows the subject, per your taxonomy rules. "
                    "State the subject of this knowledge and its target topic area "
                    "before your first write call. "
                    "Corrections and deprecations of existing knowledge are NOT this "
                    "task's job — if the content is a change request rather than new "
                    "knowledge, say so in your summary instead of forcing it in. "
                    "When you supersede a contradiction in an existing concept "
                    "(write discipline rule 3), your summary MUST end with a "
                    "'## Contradictions' section — one '- <concept id>: <note>' "
                    "bullet per resolved contradiction, or '- none'. "
                    "When done, summarize what you stored and where."
                )
                result = await self._run_write_task(task, agent_label)
                response = await self._write_result(result, report_contradictions=True)
                record["outcome"] = "partial" if response.get("partial") else "ok"
                record["stored"] = response["stored"]
                record["finished_at"] = datetime.now(UTC).isoformat()
                await self._archive_payload(payloads, record)
                return response
        except AgentRunBusyError as exc:
            record["outcome"] = "busy"
            record["error"] = type(exc).__name__
            record["finished_at"] = datetime.now(UTC).isoformat()
            await self._archive_payload(payloads, record)
            raise
        except Exception as exc:
            record["outcome"] = "error"
            record["error"] = type(exc).__name__
            record["finished_at"] = datetime.now(UTC).isoformat()
            await self._archive_payload(payloads, record)
            raise

    async def handle_update(
        self,
        instruction: str,
        *,
        agent_label: str | None = None,
    ) -> dict:
        async with self._run_gate.acquire(self.config.user_id, KIND_LIBRARIAN, wait=False):
            self._maybe_embed_reconcile()
            related = await self._related_section(instruction)
            task = (
                "UPDATE TASK. An external agent requests a change to existing "
                "knowledge in the library.\n"
                f"Instruction:\n{instruction}\n\n"
                f"{related}"
                "Locate the target concept(s) yourself: use search_semantic, "
                "search_metadata, list_dir, and read_document to find what the "
                "instruction refers "
                "to. Apply the change with targeted edits: edit_concept for "
                "corrections and restructuring, move_concept for relocation, "
                "deprecate_concept when a whole concept is obsolete. Delete "
                "requests ALWAYS go through deprecate_concept — NEVER use "
                "delete_concept; it is reserved for curator cleanup. Supersede "
                "contradictions in place — never leave old and new versions "
                "standing side by side. Index and log maintenance is automatic. "
                "When done, summarize what you changed and where."
            )
            result = await self._run_write_task(task, agent_label)
            return await self._write_result(result)

    async def _write_result(
        self, result: _RunResult, *, report_contradictions: bool = False
    ) -> dict:
        """store/update result dict; a partial run is marked explicitly (L2).

        ``report_contradictions`` (store only, D1.2): LLM-reported resolved
        contradictions land in the result — cross-checked against the tracked
        writes (D1.1), present only when the verified list is non-empty (D1.3).
        """
        response: dict[str, Any] = {
            "stored": self._stored_entries(result.tracker),
            "summary": result.text,
        }
        if result.partial:
            response["partial"] = True
            response["summary"] = PARTIAL_SUMMARY
        links_after = await self._links_after(response["stored"])
        if links_after is not None:
            response["links_after"] = links_after
            response["summary"] += _post_run_note(
                links_after["healthy"],
                "all written concepts have inbound links.",
                f"{len(links_after['unbacklinked'])} written concept(s) received "
                f"no inbound link: {', '.join(links_after['unbacklinked'])}.",
            )
        if report_contradictions:
            # D1.1: keep a reported id ONLY when the tracker holds an
            # updated/deprecated write on it (LLM-judged, tracker-verified);
            # tracker writes the LLM did not report are NOT added.
            contradictions = [
                entry
                for entry in _parse_contradictions(result.text)
                if any(
                    write["id"] == entry["id"] and write["action"] in ("updated", "deprecated")
                    for write in result.tracker.writes
                )
            ]
            if contradictions:
                response["contradictions"] = contradictions
                ids = ", ".join(entry["id"] for entry in contradictions)
                # D1.4: the verdict suffix comes AFTER the links verdict.
                response["summary"] += _post_run_note(
                    True,
                    f"{len(contradictions)} contradiction(s) resolved in place: {ids}.",
                    "",
                )
        return response

    async def _links_after(self, stored: list[dict]) -> dict | None:
        """Post-run link-graph verdict for written concepts (F19/F20); None on scan failure."""
        checked: list[str] = []
        for entry in stored:
            if entry["action"] == "deleted" or entry["id"] in checked:
                continue
            checked.append(entry["id"])
        try:
            report = await asyncio.to_thread(
                self.backend.link_health, [f"{cid}.md" for cid in checked]
            )
        except Exception as exc:
            logger.warning("links_after scan failed; omitting the field (%s)", exc)
            return None
        unbacklinked = [cid for cid in checked if report[f"{cid}.md"]["inbound"] == 0]
        orphans = [cid for cid in unbacklinked if report[f"{cid}.md"]["outbound"] == 0]
        return {
            "checked": checked,
            "unbacklinked": unbacklinked,
            "orphans": orphans,
            "healthy": not unbacklinked,
        }

    def _curate_llm(self) -> LLMConfig:
        """Effective LLM config for a curate run (manager-resolved, 0.8.0)."""
        return self.config.curate_llm or self.config.llm  # type: ignore[return-value]

    def _curate_provider_or_default(self) -> LLMProvider:
        """Provider for the effective curate config.

        ``curate_llm is None`` is the explicit inheritance marker (A21): the
        curator uses the librarian's config, so it reuses the librarian's
        provider. A resolved curator config gets its own cached provider.
        Dispatching on the None marker (not object identity) keeps
        inheritance intact across config copies/derived mutations.
        """
        if self.config.curate_llm is None:
            return self.provider
        if self._curate_provider is None:
            self._curate_provider = create_provider(self.config.curate_llm)
        return self._curate_provider

    async def handle_maintain(
        self,
        instructions: str | None = None,
        *,
        agent_label: str | None = None,
    ) -> dict:
        async with self._run_gate.acquire(self.config.user_id, KIND_CURATOR, wait=False):
            self._maybe_embed_reconcile()
            status = await asyncio.to_thread(self.backend.status)
            if status.get("healthy"):
                # No-op without an LLM call (plan section 3.4).
                # L16 trade-off: `healthy` requires zero orphans AND zero
                # broken links, so a single stray unlinked concept flips the
                # library unhealthy and the next maintain run pays for a full
                # LLM run. Deliberate: placing an orphan needs librarian
                # judgment; a deterministic threshold would silently defer
                # real repair work.
                return {
                    "actions": [],
                    "summary": "Library is healthy; no maintenance needed.",
                    "healthy": True,
                }
            task = build_maintain_preamble(status, instructions)
            result = await self._run(task, agent_label)
            final_status = await asyncio.to_thread(self.backend.status)
            healthy = bool(final_status.get("healthy"))
            return {
                "actions": self._stored_entries(result.tracker),
                # L15: the LLM narrates from pre-run status; append the
                # deterministic post-run verdict so epochs don't mix.
                "summary": result.text
                + _post_run_note(
                    healthy,
                    "the library is now healthy.",
                    "the library still has open health issues.",
                ),
                "healthy": healthy,
            }

    def _semantic_duplicates(self, since: str | None) -> list[dict]:
        """Embedding-similarity duplicate pass over cached vectors; never raises.

        Reads cached vectors only — no embedding calls at scan time. Empty
        when embeddings are unconfigured or the pass fails. The scan runs
        through the backend (A10); threshold resolution order: user override
        → per-model default → 0.85 fallback.
        """
        if self._embed is None:
            return []
        try:
            return self.backend.semantic_duplicate_candidates(
                {p: r["vector"] for p, r in self._embed.load().items()},
                since=since,
                threshold=self.config.semantic_threshold,
                model=self.config.embedding.model if self.config.embedding else None,
            )
        except Exception:
            logger.warning("semantic duplicate pass failed; continuing without it", exc_info=True)
            return []

    def _store_payload_reviews(self) -> list[dict]:
        """Failed/partial store payloads since the last curate run; never raises.

        Deterministic digest construction (D3.6), not an LLM-chosen call:
        records with outcome error/partial and ``received_at >=
        curate_last_run_at`` (None baseline = all retained payloads),
        newest first, capped, content reduced to a bounded excerpt. The
        PayloadStore is constructed on demand (TraceSession.close
        precedent), keeping the Librarian signature untouched.
        """
        try:
            records = PayloadStore(self.root, keep=self.config.payload_keep).since(
                self.config.curate_last_run_at
            )
        except Exception:
            logger.warning("payload review scan failed; continuing without it", exc_info=True)
            return []
        reviews: list[dict] = []
        for record in records:
            if record.get("outcome") not in ("error", "partial"):
                continue
            content = str((record.get("params") or {}).get("content") or "")
            reviews.append(
                {
                    "request_id": record.get("request_id"),
                    "received_at": record.get("received_at"),
                    "outcome": record.get("outcome"),
                    "error": record.get("error"),
                    "excerpt": content[:MAX_PAYLOAD_EXCERPT],
                }
            )
            if len(reviews) >= MAX_STORE_PAYLOAD_REVIEWS:
                break
        return reviews

    async def handle_curate(
        self,
        instructions: str | None = None,
        *,
        agent_label: str | None = None,
    ) -> dict:
        async with self._run_gate.acquire(self.config.user_id, KIND_CURATOR, wait=False):
            self._maybe_embed_reconcile()
            # L14: findings are recomputed over the whole library every run —
            # an unaddressed finding is re-reported until it is actually fixed
            # (no changed-set amnesia from the curate_last_run_at baseline).
            # The full-tree scans run through the backend (A10), off the
            # event loop (A1/A2).
            report = await asyncio.to_thread(self.backend.organization_findings)
            report["semantic_duplicate_candidates"] = await asyncio.to_thread(
                self._semantic_duplicates, None
            )
            report["store_payload_reviews"] = await asyncio.to_thread(self._store_payload_reviews)
            if self.backend.findings_empty(report):
                # No-op without an LLM call (maintain precedent, D6).
                status = await asyncio.to_thread(self.backend.status)
                return {
                    "actions": [],
                    "summary": "Library is well-organized; nothing to curate.",
                    "organized": True,
                    "findings": report,
                    "health_after": {
                        "healthy": bool(status.get("healthy")),
                        "orphans": len(status["health"]["orphans"]),
                    },
                }
            task = build_curate_preamble(
                report, instructions, addendum=self.config.curate_prompt_addendum
            )
            result = await self._run(
                task,
                agent_label,
                llm_config=self._curate_llm(),
                provider=self._curate_provider_or_default(),
            )
            # L15: re-scan post-run so the response reports ONE epoch — what
            # remains after the run, not the pre-run state the LLM narrated.
            final = await asyncio.to_thread(self.backend.organization_findings)
            final["semantic_duplicate_candidates"] = await asyncio.to_thread(
                self._semantic_duplicates, None
            )
            # One-shot events, not structural state: payload reviews are
            # consumed by the run that reported them and never re-reported
            # (keeps the post-run 'organized' verdict exact).
            final["store_payload_reviews"] = []
            final_status = await asyncio.to_thread(self.backend.status)
            organized = self.backend.findings_empty(final)
            return {
                "actions": self._stored_entries(result.tracker),
                "summary": result.text
                + _post_run_note(
                    organized,
                    "no open findings remain; the library is well-organized.",
                    "open findings remain (see 'findings'); unaddressed findings are "
                    "re-reported on the next run until fixed.",
                ),
                "organized": organized,
                "findings": final,
                "health_after": {
                    "healthy": bool(final_status.get("healthy")),
                    "orphans": len(final_status["health"]["orphans"]),
                },
            }
