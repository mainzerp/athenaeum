"""Librarian agent: hand-rolled tool-calling loop over LibraryBackend.

Contract: plan sections 3.4/3.4a. The loop is a plain while loop with a
max_iterations cap — no framework, no streaming. On cap exhaustion a final
answer is requested with no tools. Three handlers back the MCP tools:
``handle_request`` / ``handle_store`` / ``handle_update``. The curator
handlers (``handle_maintain`` / ``handle_curate``) live on the separate
``Curator`` agent in ``athenaeum.curator``; the shared loop and plumbing
live in ``athenaeum.librarian.base.BaseAgent``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from typing import Any

# Re-exports (``as`` form): existing call sites and tests import these names
# from athenaeum.librarian.agent; the definitions live in .base.
from athenaeum.librarian.base import (
    FINAL_ANSWER_REQUEST as FINAL_ANSWER_REQUEST,
)
from athenaeum.librarian.base import (
    PARTIAL_SUMMARY,
    RESERVED_NAMES,
    BaseAgent,
    _post_run_note,
    _ProviderRunError,
    _RunResult,
    _Tracker,
)
from athenaeum.librarian.base import (
    TOOL_RESULT_CHAR_LIMIT as TOOL_RESULT_CHAR_LIMIT,
)
from athenaeum.librarian.base import (
    LibrarianConfig as LibrarianConfig,
)
from athenaeum.librarian.base import (
    LibrarianNotConfiguredError as LibrarianNotConfiguredError,
)
from athenaeum.librarian.gate import KIND_LIBRARIAN, AgentRunBusyError
from athenaeum.librarian.tracing import current_trace, mint_request_id, telemetry_or_mint
from athenaeum.library.payloads import PayloadStore
from athenaeum.okf import is_stale, trust_tier

logger = logging.getLogger(__name__)

STORE_RELATED_TOP_K = 5  # tuning parameter for the store/update related-concepts injection


class LibrarianNoWriteError(RuntimeError):
    """Raised when a write task completes without any writes landing."""


NO_WRITE_NUDGE = (
    "\n\nYou finished without writing anything. That is a failed store: apply "
    "your write discipline now — the context you gathered is enough; write the "
    "concept(s) and their back-links."
)


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


class Librarian(BaseAgent):
    """One librarian bound to one library root and one user config."""

    # --- post-processing -------------------------------------------------

    async def _concept_entries(self, tracker: _Tracker) -> list[dict]:
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
                doc = await asyncio.to_thread(self.backend.read_document, path)
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

    # --- handlers (back the MCP tools, plan section 3.1) ------------------

    async def handle_request(
        self,
        query: str,
        context: str | None = None,
        *,
        agent_label: str | None = None,
    ) -> dict:
        session = current_trace()
        if session is not None:
            session.set_request({"query": query, "context": context})
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
            result = await self._run(task, agent_label, answer_guard=True)
            session = current_trace()
            if session is not None:
                # F21 A3: the post-guard answer text lands in the trace.
                session.set_answer(result.text)
            answer: dict[str, Any] = {
                "answer": result.text,
                "concepts": await self._concept_entries(result.tracker),
            }
            follow_ups = _parse_follow_ups(result.text)
            if follow_ups:
                answer["follow_ups"] = follow_ups
            return answer

    async def _run_write_task(
        self,
        task: str,
        agent_label: str | None,
        requested_by: str | None = None,
        via: str | None = None,
    ) -> _RunResult:
        """Run a write-intent task; retry once when nothing was written (F11).

        L1: a run counts as successful ONLY when at least one write landed —
        a non-empty summary alone (e.g. after cap exhaustion) is a failure.
        L2: a mid-loop provider failure after landed writes returns a
        partial-success result (``partial=True``) instead of raising.
        """
        result = await self._run_write_once(task, agent_label, requested_by, via)
        if await self._stored_entries(result.tracker):
            return result
        retry = await self._run_write_once(task + NO_WRITE_NUDGE, agent_label, requested_by, via)
        if await self._stored_entries(retry.tracker):
            return retry
        raise LibrarianNoWriteError("librarian completed the write task without writing anything")

    async def _run_write_once(
        self,
        task: str,
        agent_label: str | None,
        requested_by: str | None = None,
        via: str | None = None,
    ) -> _RunResult:
        try:
            return await self._run(
                task, agent_label, requested_by=requested_by, via=via, write_task=True
            )
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
                doc = await asyncio.to_thread(self.backend.read_document, f"{concept_id}.md")
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
        requested_by: str | None = None,
        via: str | None = None,
    ) -> dict:
        # D3.2: two-phase payload archive. The "received" record is written
        # BEFORE the run-gate acquire so busy rejections are recorded too;
        # the exit rewrite (same request_id, atomic overwrite) carries the
        # final outcome. Best-effort: archive failures never fail the store.
        payloads = PayloadStore(self.root, keep=self.config.payload_keep)
        image_refs = _payload_image_refs(images)
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
                "images": image_refs,
            },
            "stored": [],
        }
        await self._archive_payload(payloads, record)
        session = current_trace()
        if session is not None:
            session.set_request(
                {
                    "content": content,
                    "kind_hint": kind_hint,
                    "relates_to": relates_to,
                    "topic_hint": topic_hint,
                    "images": image_refs,
                }
            )
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
                    "subjects. Caller-suggested related concepts are back-linked by "
                    "the server automatically after the run — do not write or claim "
                    "those links yourself. Caller-suggested related concepts and "
                    "similarity-ranked candidates are NOT placement hints — "
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
                result = await self._run_write_task(task, agent_label, requested_by, via)
                # F22: relates_to back-linking is deterministic server-side;
                # it runs only after the no-write guard succeeded, so a
                # zero-write store still raises LibrarianNoWriteError above.
                backlinked = await self._ensure_relates_to_backlinks(
                    await self._stored_entries(result.tracker),
                    relates_to,
                    agent_label=agent_label,
                )
                response = await self._write_result(
                    result, report_contradictions=True, extra_stored=backlinked
                )
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
        relates_to: list[str] | None = None,
        *,
        agent_label: str | None = None,
        requested_by: str | None = None,
        via: str | None = None,
    ) -> dict:
        session = current_trace()
        if session is not None:
            session.set_request({"instruction": instruction, "relates_to": relates_to})
        async with self._run_gate.acquire(self.config.user_id, KIND_LIBRARIAN, wait=False):
            self._maybe_embed_reconcile()
            related = await self._related_section(instruction)
            task = (
                "UPDATE TASK. An external agent requests a change to existing "
                "knowledge in the library.\n"
                f"Instruction:\n{instruction}\n\n"
                + (
                    f"Related concept IDs suggested by the caller: {', '.join(relates_to)} "
                    "— back-linking these is automatic server-side; do not write or "
                    "claim those links yourself.\n\n"
                    if relates_to
                    else ""
                )
                + f"{related}"
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
            result = await self._run_write_task(task, agent_label, requested_by, via)
            # F22: same deterministic relates_to back-linking as handle_store.
            backlinked = await self._ensure_relates_to_backlinks(
                await self._stored_entries(result.tracker),
                relates_to,
                agent_label=agent_label,
            )
            return await self._write_result(result, extra_stored=backlinked)

    async def _ensure_relates_to_backlinks(
        self,
        stored: list[dict],
        relates_to: list[str] | None,
        *,
        agent_label: str | None,
    ) -> list[dict]:
        """Deterministic relates_to back-linking after a write run (F22).

        For each stored (non-deleted) concept x each relates_to hint the
        server writes the back-link itself — the model's claim is no longer
        trusted to produce the edit. Path normalization, dedupe against
        already-linked targets, and self-link guards live in
        ``LibraryBackend.add_backlink``; a per-target failure (missing or
        ambiguous hint) is logged and skipped, never fails the run. The
        tracker is deliberately bypassed (hygiene-sweep precedent): the edits
        surface here as ``updated`` entries instead.

        Returns the ``{"id", "title", "action": "updated"}`` entries for the
        edited targets, skipping ids already present in ``stored`` (the
        model's own edit on the target already drives embedding sync).
        """
        if not relates_to or not stored:
            return []
        seen_ids = {entry["id"] for entry in stored}
        backlinked: list[dict] = []
        for entry in stored:
            if entry["action"] == "deleted":
                continue
            for raw in relates_to:
                try:
                    edited = await asyncio.to_thread(
                        self.backend.add_backlink,
                        f"{entry['id']}.md",
                        raw,
                        agent_label=agent_label,
                    )
                except Exception as exc:
                    logger.warning(
                        "relates_to backlink %s -> %s failed; skipping (%s)",
                        entry["id"],
                        raw,
                        exc,
                    )
                    continue
                if edited is None or edited["id"] in seen_ids:
                    continue
                seen_ids.add(edited["id"])
                backlinked.append(edited)
        return backlinked

    async def _write_result(
        self,
        result: _RunResult,
        *,
        report_contradictions: bool = False,
        extra_stored: list[dict] | None = None,
    ) -> dict:
        """store/update result dict; a partial run is marked explicitly (L2).

        ``report_contradictions`` (store only, D1.2): LLM-reported resolved
        contradictions land in the result — cross-checked against the tracked
        writes (D1.1), present only when the verified list is non-empty (D1.3).
        ``extra_stored`` (F22): deterministic relates_to back-link edits,
        appended to ``stored`` (embedding sync coverage) and surfaced in the
        additive ``backlinked`` field; the deterministic note lands before the
        links verdict, which therefore scans the post-backlink state.
        """
        response: dict[str, Any] = {
            "stored": await self._stored_entries(result.tracker),
            "summary": result.text,
        }
        if extra_stored:
            response["stored"] += extra_stored
            response["backlinked"] = [
                {"id": entry["id"], "title": entry["title"]} for entry in extra_stored
            ]
        if result.partial:
            response["partial"] = True
            response["summary"] = PARTIAL_SUMMARY
        if extra_stored:
            ids = ", ".join(entry["id"] for entry in extra_stored)
            response["summary"] += _post_run_note(
                True,
                f"relates_to back-links ensured from {len(extra_stored)} existing "
                f"concept(s): {ids}.",
                "",
            )
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
