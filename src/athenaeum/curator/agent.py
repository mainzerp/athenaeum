"""Curator agent: whole-library maintenance and curation over LibraryBackend.

Owns the two curator handlers backing the MCP tools / nightly scheduler:
``handle_maintain`` (graph-health repair, gate ``KIND_CURATOR``, runs on the
librarian LLM) and ``handle_curate`` (organization curation, the ONLY
handler using the separate curator LLM binding — ``_curate_llm`` /
``_curate_provider_or_default``, A21). The agent loop and all shared
plumbing live in ``athenaeum.librarian.base.BaseAgent``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athenaeum import __version__
from athenaeum.curator.prompts import (
    build_curate_preamble,
    build_curator_system_prompt,
    build_maintain_preamble,
)
from athenaeum.curator.tools import CURATOR_TOOL_SCHEMAS
from athenaeum.librarian.base import (
    PARTIAL_SUMMARY,
    BaseAgent,
    LibrarianConfig,
    _post_run_note,
    _ProviderRunError,
    _RunResult,
    _Tracker,
)
from athenaeum.librarian.gate import KIND_CURATOR
from athenaeum.librarian.llm import LLMConfig, LLMProvider, create_provider
from athenaeum.librarian.tracing import current_trace
from athenaeum.library.payloads import PayloadStore

if TYPE_CHECKING:
    from athenaeum.embeddings import EmbeddingService
    from athenaeum.librarian.gate import RunGate
    from athenaeum.librarian.tools import Backend

logger = logging.getLogger(__name__)

# Verifier actor for the deterministic post-curation verification (F18-pattern
# post-step): the SAME string for interactive MCP runs and nightly scheduler
# runs — the curator machine-confirms, humans review outside the loop.
CURATOR_VERIFIER = f"athenaeum-curator/{__version__}"

MAX_STORE_PAYLOAD_REVIEWS = 5  # curate digest cap for failed/partial store payloads
MAX_PAYLOAD_EXCERPT = 120  # per-entry content excerpt cap in that digest


def _verification_note(verified: list[dict]) -> str:
    """Deterministic post-run verification line; "" when nothing was verified."""
    if not verified:
        return ""
    return f"\n\nPost-run verification: machine-confirmed {len(verified)} repaired concept(s)."


def _hygiene_note(actions: list[dict]) -> str:
    """Deterministic content-hygiene line; "" when nothing was repaired."""
    if not actions:
        return ""
    return (
        f"\n\nContent hygiene: decoded literal unicode escape artifacts in "
        f"{len(actions)} existing concept(s) (F25 stock repair)."
    )


class Curator(BaseAgent):
    """One curator bound to one library root and one user config."""

    tool_schemas = CURATOR_TOOL_SCHEMAS

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
        super().__init__(
            root,
            config,
            backend=backend,
            provider=provider,
            embedding_service=embedding_service,
            run_gate=run_gate,
            reranker=reranker,
            computation_runner=computation_runner,
        )
        self._curate_provider: LLMProvider | None = None

    @property
    def system_prompt(self) -> str:
        return build_curator_system_prompt(self.config.prompt_addendum)

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
        session = current_trace()
        if session is not None:
            session.set_request({"instructions": instructions})
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
                    "verified": [],
                }
            task = build_maintain_preamble(status, instructions)
            try:
                result = await self._run(task, agent_label)
            except _ProviderRunError as exc:
                # AGENT-05: a mid-loop provider failure after landed writes is a
                # partial success, not a failed run (L2/_run_write_once pattern).
                logger.warning(
                    "provider failed mid-run; keeping %d landed write(s)",
                    len(exc.tracker.writes),
                )
                result = _RunResult(
                    text="", tracker=exc.tracker, iterations=exc.iterations, partial=True
                )
            # Epoch ordering (L15): verification lands BEFORE the final rescan,
            # so the post-run status and the verified receipts share one epoch.
            verified = await self._verify_repairs(result.tracker, agent_label)
            final_status = await asyncio.to_thread(self.backend.status)
            healthy = bool(final_status.get("healthy"))
            response: dict[str, Any] = {
                "actions": await self._stored_entries(result.tracker),
                # L15: the LLM narrates from pre-run status; append the
                # deterministic post-run verdict so epochs don't mix.
                "summary": (PARTIAL_SUMMARY if result.partial else result.text)
                + _post_run_note(
                    healthy,
                    "the library is now healthy.",
                    "the library still has open health issues.",
                )
                + _verification_note(verified),
                "healthy": healthy,
                "verified": verified,
            }
            if result.partial:
                response["partial"] = True
            return response

    async def _verify_repairs(self, tracker: _Tracker, agent_label: str | None) -> list[dict]:
        """Post-run verification (F18-pattern deterministic post-step); never raises.

        Machine-confirms exactly the concepts the curator run REPAIRED —
        tracker writes with action ``updated`` (content repairs via
        edit_concept on pre-existing concepts; never creations, deprecations,
        deletes, or moves). Calls the backend directly, bypassing
        _dispatch_tracked: no tracker pollution, no embedding-sync input
        (verified is metadata-only; the embedding content hash covers
        title/description/body, never frontmatter trust keys). A per-concept
        failure is logged and skipped — verification must never fail a
        completed curator run (_semantic_duplicates never-raise precedent).
        """
        receipts: list[dict] = []
        seen: set[str] = set()
        for write in tracker.writes:
            if write["action"] != "updated":
                continue
            if write["id"] in seen:
                # One concept edited twice in a run yields exactly one
                # verify_concept call and one receipt.
                continue
            seen.add(write["id"])
            try:
                await asyncio.to_thread(
                    self.backend.verify_concept,
                    write["id"] + ".md",
                    by=CURATOR_VERIFIER,
                    agent_label=agent_label,
                )
            except Exception:
                logger.warning(
                    "post-run verification failed for %s; skipping", write["id"], exc_info=True
                )
                continue
            receipts.append({"id": write["id"], "by": CURATOR_VERIFIER})
        return receipts

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

    def _content_hygiene_sweep(self, agent_label: str | None) -> list[dict]:
        """Deterministic F25 stock repair of literal \\uXXXX body artifacts; never raises.

        Scans the whole library via the backend (A10) for bodies where the
        escape guard's decode would change content, then repairs each through
        ``edit_concept`` with the RAW body — the guard inside ``edit_concept``
        performs the decode (single source of truth). The sweep passes
        ``allow_literal_escapes=True``: it never touches code-span content,
        so the code-span warning would be pure per-run log noise (code-span
        candidates surface via the findings scan in the same run). Bypasses
        the tracker deliberately: there are no LLM writes to track, and the
        sweep's repairs must not feed ``_verify_repairs``. A per-file failure
        is logged and skipped, keeping partial progress (_verify_repairs
        precedent). N dirty files = N commits (accepted; no batching API
        exists, matching all bulk LLM repairs).
        """
        try:
            entries = self.backend.escape_artifact_scan()
        except Exception:
            logger.warning("content hygiene scan failed; continuing without it", exc_info=True)
            return []
        actions: list[dict] = []
        for entry in entries:
            try:
                result = self.backend.edit_concept(
                    entry["path"],
                    new_body=entry["body"],
                    agent_label=agent_label,
                    allow_literal_escapes=True,
                )
            except Exception:
                logger.warning(
                    "content hygiene repair failed for %s; skipping",
                    entry["path"],
                    exc_info=True,
                )
                continue
            actions.append(
                {"id": result["id"], "title": entry["title"] or result["id"], "action": "updated"}
            )
        return actions

    def _store_payload_reviews(self) -> list[dict]:
        """Failed/partial store payloads since the last curate run; never raises.

        Deterministic digest construction (D3.6), not an LLM-chosen call:
        records with outcome error/partial and ``received_at >=
        curate_last_run_at`` (None baseline = all retained payloads),
        newest first, capped, content reduced to a bounded excerpt. The
        PayloadStore is constructed on demand (TraceSession.close
        precedent), keeping the Curator signature untouched.
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

    def _code_span_escape_candidates(self) -> list[dict]:
        """Concept files with literal \\uXXXX inside code spans/fences; never raises.

        Structural state (not one-shot events): reported pre-run and
        re-reported post-run until actually fixed (L14), unlike
        ``_store_payload_reviews``.
        """
        try:
            return self.backend.code_span_escape_candidates()
        except Exception:
            logger.warning("code-span escape scan failed; continuing without it", exc_info=True)
            return []

    async def handle_curate(
        self,
        instructions: str | None = None,
        *,
        agent_label: str | None = None,
    ) -> dict:
        session = current_trace()
        if session is not None:
            session.set_request({"instructions": instructions})
        async with self._run_gate.acquire(self.config.user_id, KIND_CURATOR, wait=False):
            self._maybe_embed_reconcile()
            # F25 stock repair: deterministic content-hygiene sweep BEFORE the findings
            # scan, so the report (and the D6 no-op gate) see the post-repair state.
            hygiene_actions = await asyncio.to_thread(self._content_hygiene_sweep, agent_label)
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
            report["code_span_escape_candidates"] = await asyncio.to_thread(
                self._code_span_escape_candidates
            )
            if self.backend.findings_empty(report):
                # No-op without an LLM call (maintain precedent, D6).
                status = await asyncio.to_thread(self.backend.status)
                return {
                    "actions": hygiene_actions,
                    "summary": "Library is well-organized; nothing to curate."
                    + _hygiene_note(hygiene_actions),
                    "organized": True,
                    "verified": [],
                    "findings": report,
                    "health_after": {
                        "healthy": bool(status.get("healthy")),
                        "orphans": len(status["health"]["orphans"]),
                        "broken_links": len(status["health"]["broken_links"]),
                    },
                }
            task = build_curate_preamble(
                report, instructions, addendum=self.config.curate_prompt_addendum
            )
            try:
                result = await self._run(
                    task,
                    agent_label,
                    llm_config=self._curate_llm(),
                    provider=self._curate_provider_or_default(),
                )
            except _ProviderRunError as exc:
                # AGENT-05: a mid-loop provider failure after landed writes is a
                # partial success, not a failed run (L2/_run_write_once pattern).
                logger.warning(
                    "provider failed mid-run; keeping %d landed write(s)",
                    len(exc.tracker.writes),
                )
                result = _RunResult(
                    text="", tracker=exc.tracker, iterations=exc.iterations, partial=True
                )
            # Epoch ordering (L15): verification lands BEFORE the final rescan.
            verified = await self._verify_repairs(result.tracker, agent_label)
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
            # Structural state: code-span escape candidates MUST be
            # re-reported post-run (L14), unlike payload reviews.
            final["code_span_escape_candidates"] = await asyncio.to_thread(
                self._code_span_escape_candidates
            )
            final_status = await asyncio.to_thread(self.backend.status)
            organized = self.backend.findings_empty(final)
            response: dict[str, Any] = {
                "actions": hygiene_actions + await self._stored_entries(result.tracker),
                "summary": (PARTIAL_SUMMARY if result.partial else result.text)
                + _post_run_note(
                    organized,
                    "no open findings remain; the library is well-organized.",
                    "open findings remain (see 'findings'); unaddressed findings are "
                    "re-reported on the next run until fixed.",
                )
                + _verification_note(verified)
                + _hygiene_note(hygiene_actions),
                "organized": organized,
                "verified": verified,
                "findings": final,
                "health_after": {
                    "healthy": bool(final_status.get("healthy")),
                    "orphans": len(final_status["health"]["orphans"]),
                    "broken_links": len(final_status["health"]["broken_links"]),
                },
            }
            if result.partial:
                response["partial"] = True
            return response
