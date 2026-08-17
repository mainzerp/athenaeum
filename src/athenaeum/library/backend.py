"""LibraryBackend: the OKF boundary — all bundle reads/writes go through here.

All paths are bundle-relative (``/tables/customers.md`` form); resolution
delegates to ``isolation.resolve_under`` (rejects ``..``, OS-absolute paths,
escapes). Concept IDs are paths minus ``.md``. Every mutating method performs
the compound write in fixed crash-safe order: (1) concept file via
write-then-rename, (2) regenerate affected index.md files, (3) append the
root log.md entry, (4) best-effort git auto-commit (one commit per compound
write; the full history — diff, revert, reset, per-file history and
per-file restore — lives in the bundle's own git repository, and a git
failure never breaks a write). Index.md is a
deterministic pure function of the tree, so ``reconcile()`` can always
regenerate it after a crash — but that is ALL reconcile repairs (L11): a
log.md entry lost between index regeneration and the log append is not
recoverable, and a crash mid-``rewrite_links`` (move) can leave a mixed
link state; both are outside reconcile's scope. Undo goes through git
history (revert / append-only reset), which is always state-consistent.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import itertools
import logging
import posixpath
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from athenaeum import __version__

from ..isolation import resolve_under
from . import escape_guard as escape_guard_mod
from . import frontmatter as fm_mod
from . import gittool as gittool_mod
from . import hybrid as hybrid_mod
from . import index as index_mod
from . import links as links_mod
from . import log as log_mod
from . import organize as organize_mod
from . import semantic as semantic_mod
from . import validate as validate_mod
from .frontmatter import write_bytes_atomic, write_text_atomic
from .links import RESERVED_NAMES

logger = logging.getLogger(__name__)

_SUGGEST_CANDIDATE_LIMIT = 2000  # cap the candidate walk for typo suggestions

# Per-root write locks shared by every LibraryBackend instance in this
# process: several backends can target one library root (WebUI builds fresh
# backends per request), so the compound write serializes on the resolved
# root path. RLock: _regenerate_chain -> regenerate_index nests.
_WRITE_LOCKS: dict[str, threading.RLock] = {}
_WRITE_LOCKS_GUARD = threading.Lock()


def write_lock_for(root: str | Path) -> threading.RLock:
    """The process-wide write lock for a library root (A4): every writer of
    the bundle — compound writes, reconcile, and the import replace — must
    hold it. Keyed by the resolved root path, shared by all backends."""
    key = str(Path(root).resolve())
    with _WRITE_LOCKS_GUARD:
        lock = _WRITE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _WRITE_LOCKS[key] = lock
        return lock


def drop_write_lock(root: str | Path) -> None:
    """Forget the write lock for ``root`` (A12: no per-user registry leak).

    Call only after the root's librarian has been evicted (its writes are
    done); dropping the entry while a write holds the lock would let a new
    backend for the same root start a second, unserialized compound write.
    """
    key = str(Path(root).resolve())
    with _WRITE_LOCKS_GUARD:
        _WRITE_LOCKS.pop(key, None)


def provision_library(data_root: str | Path, user_id: str) -> Path:
    """Create the user's library directory and initialize a fresh OKF bundle.

    Filesystem provisioning lives in the library layer (A7): db.py owns the
    user/config rows, this owns the on-disk bundle layout.
    """
    library_root = Path(data_root) / "users" / user_id / "library"
    library_root.mkdir(parents=True, exist_ok=True)
    LibraryBackend(library_root, actor=f"athenaeum-librarian/{__version__}").init_bundle()
    return library_root


class LibraryBackend:
    """Internal Python API over one OKF bundle (one per user)."""

    def __init__(
        self,
        root: str | Path,
        *,
        actor: str,
        git_enabled: bool = True,
        git_remote_url: str | None = None,
        git_auto_push: bool = False,
        embedding_service=None,
        hybrid_search: bool = True,
        hybrid_rerank: bool = False,
        reranker=None,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.actor = actor
        self._git = (
            gittool_mod.GitRepo(self.root, remote_url=git_remote_url, auto_push=git_auto_push)
            if git_enabled
            else None
        )
        if git_enabled and not gittool_mod.git_available():
            logger.warning("git binary not found; library commit history disabled")
        # (log.md mtime_ns, seed); None forces regeneration (own writes below).
        self.seed_cache: tuple[int | None, str] | None = None
        self._embedding_service = embedding_service
        # Hybrid retrieval knobs (defaults preserve pre-0.19 behavior: the
        # hybrid branch additionally requires an FTS collaborator, which only
        # manager-built embedding services carry).
        self._hybrid_search = hybrid_search
        self._hybrid_rerank = hybrid_rerank
        self._reranker = reranker

    @property
    def history_configured(self) -> bool:
        """Git history is switched on for this backend (UI enabled state)."""
        return self._git is not None

    @property
    def history_available(self) -> bool:
        """Git history works end to end: enabled AND the git binary exists."""
        return self._git is not None and gittool_mod.git_available()

    # ------------------------------------------------------------------ reads

    def list_dir(self, path: str = "/") -> list[dict]:
        dir_path = self._resolve(path)
        if not dir_path.is_dir():
            raise FileNotFoundError(
                f"not a directory: {path!r}{self._did_you_mean(path, directories=True)}"
            )
        entries = []
        for child in sorted(dir_path.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
            if child.name.startswith("."):
                continue
            rel = "/" + child.relative_to(self.root).as_posix()
            if child.is_dir():
                entries.append({"name": child.name, "path": rel, "is_directory": True})
            elif child.suffix == ".md" and child.name not in RESERVED_NAMES:
                entry: dict = {"name": child.name, "path": rel, "is_directory": False}
                try:
                    fm, _ = fm_mod.split_document(child.read_text(encoding="utf-8"))
                except (fm_mod.FrontmatterError, OSError, UnicodeDecodeError):
                    fm = {}
                for key in ("title", "type", "description"):
                    if fm.get(key):
                        entry[key] = fm[key]
                entries.append(entry)
        return entries

    def read_document(self, path: str) -> dict:
        abs_path = self._resolve(path)
        if not abs_path.is_file():
            raise FileNotFoundError(
                f"no such document: {path!r}{self._did_you_mean(path, directories=False)}"
            )
        text = abs_path.read_text(encoding="utf-8")
        bundle = self._bundle_path(path)
        if posixpath.basename(bundle) in RESERVED_NAMES:
            return {"path": bundle, "frontmatter": {}, "body": text}
        fm, body = fm_mod.split_document(text)
        return {"path": bundle, "frontmatter": fm, "body": body}

    def search_metadata(self, field: str | None = None, value: str | None = None) -> list[dict]:
        results = []
        for bundle_path, abs_path in links_mod.iter_concept_files(self.root):
            try:
                fm, _ = fm_mod.split_document(abs_path.read_text(encoding="utf-8"))
            except (fm_mod.FrontmatterError, OSError, UnicodeDecodeError):
                continue
            if field is not None:
                if field not in fm:
                    continue
                if value is not None and value.lower() not in str(fm[field]).lower():
                    continue
            results.append(
                {
                    "id": bundle_path[:-3],
                    "path": bundle_path,
                    "title": fm.get("title"),
                    "type": fm.get("type"),
                    "description": fm.get("description"),
                }
            )
        return results

    async def search_semantic(self, query: str, limit: int = 8) -> list[dict]:
        """Hybrid retrieval over concept text; falls back to search_metadata.

        Two legs fuse via reciprocal rank fusion (``library.hybrid``): the
        semantic leg (embedding cosine) and the lexical leg (FTS5 BM25). When
        a reranker is configured and available, the fused candidates get a
        local cross-encoder pass and hits score by its logits (higher =
        better, may be negative); otherwise hits keep RRF order and score.
        The hybrid branch needs an FTS collaborator on the embedding service
        (duck-typed — services without one keep the legacy pure-semantic path
        with cosine scores). An unavailable FTS5 table likewise degrades to
        legacy. An embedding-pipeline failure degrades to title/description
        metadata matches (``fallback: true``); when the fallback matches
        nothing either, a RuntimeError is raised so the failure is not
        indistinguishable from "no hits".
        """
        if self._embedding_service is None:
            raise RuntimeError(
                "semantic search is not configured: set an embedding source in the "
                "WebUI (Agents > Embeddings); use search_metadata instead"
            )
        fts = getattr(self._embedding_service, "fts", None)
        use_hybrid = self._hybrid_search and fts is not None and fts.available
        if not use_hybrid:
            return await self._search_semantic_legacy(query, limit)
        try:
            leg_k = max(limit, hybrid_mod.HYBRID_RERANK_CANDIDATES)
            t0 = time.perf_counter()
            sem = await self._embedding_service.search_ids(query, leg_k)
            t1 = time.perf_counter()
            lex = self._embedding_service.fts_search(query, leg_k)
            t2 = time.perf_counter()
            logger.debug("search_semantic: semantic leg %.1f ms", (t1 - t0) * 1000)
            logger.debug("search_semantic: fts leg %.1f ms", (t2 - t1) * 1000)
        except Exception as exc:
            logger.warning("semantic search failed; falling back to search_metadata", exc_info=True)
            return self._metadata_fallback(query, limit, exc)
        # Normalize keys to the canonical store shape (x.md) before fusion:
        # semantic ids are "/x"-shaped without .md; FTS keys are already x.md.
        sem_keys = [f"{concept_id.lstrip('/')}.md" for concept_id, _ in sem]
        lex_keys = [path for path, _ in lex]
        fused = hybrid_mod.rrf_merge([sem_keys, lex_keys])[:leg_k]
        t0 = time.perf_counter()
        candidates = await asyncio.to_thread(self._hydrate_candidates, fused)
        logger.debug(
            "search_semantic: hydration %.1f ms over %d candidates",
            (time.perf_counter() - t0) * 1000,
            len(candidates),
        )
        rerank_scores = None
        if self._hybrid_rerank and self._reranker is not None and candidates:
            from athenaeum.embeddings import concept_text

            texts = [
                concept_text(doc["frontmatter"], doc["body"])[: hybrid_mod.HYBRID_RERANK_TEXT_CHARS]
                for _, _, doc in candidates
            ]
            try:
                t0 = time.perf_counter()
                rerank_scores = await self._reranker.rerank(query, texts)
                logger.debug(
                    "search_semantic: rerank %.1f ms over %d texts",
                    (time.perf_counter() - t0) * 1000,
                    len(texts),
                )
            except Exception:
                logger.warning("reranker failed; keeping RRF order", exc_info=True)
                rerank_scores = None
        if rerank_scores is not None and len(rerank_scores) == len(candidates):
            # Cross-encoder logits decide the order and become the hit score.
            ranked = [
                (path, logit, doc)
                for (path, _, doc), logit in sorted(
                    zip(candidates, rerank_scores, strict=True),
                    key=lambda pair: pair[1],
                    reverse=True,
                )
            ]
        else:
            ranked = candidates  # RRF order, RRF scores
        hits = []
        for path, score, doc in ranked[:limit]:
            fm = doc.get("frontmatter") or {}
            hits.append(
                {
                    "id": f"/{path[:-3]}",
                    "path": f"/{path}",
                    "title": fm.get("title"),
                    "type": fm.get("type"),
                    "description": fm.get("description"),
                    "score": round(score, 2),
                }
            )
        return hits

    def _hydrate_candidates(self, fused: list[tuple[str, float]]) -> list[tuple[str, float, dict]]:
        """(path, rrf score, doc) triples for the fused candidates.

        Sync worker (runs via ``asyncio.to_thread``): one ``read_document``
        per candidate; unreadable candidates are skipped, not fatal.
        """
        candidates: list[tuple[str, float, dict]] = []
        for path, fused_score in fused:
            try:
                doc = self.read_document(path)
            except Exception:
                continue  # unreadable candidates are skipped, not fatal
            candidates.append((path, fused_score, doc))
        return candidates

    async def _search_semantic_legacy(self, query: str, limit: int) -> list[dict]:
        """Pre-hybrid pure-semantic path (cosine scores); the contract every
        deployment without an FTS index keeps."""
        try:
            ranked = await self._embedding_service.search_ids(query, limit)
        except Exception as exc:
            logger.warning("semantic search failed; falling back to search_metadata", exc_info=True)
            return self._metadata_fallback(query, limit, exc)
        hits = []
        for concept_id, score in ranked:
            try:
                doc = self.read_document(f"{concept_id}.md")
            except Exception:
                continue
            fm = doc.get("frontmatter") or {}
            hits.append(
                {
                    "id": concept_id,
                    "path": f"{concept_id}.md",
                    "title": fm.get("title"),
                    "type": fm.get("type"),
                    "description": fm.get("description"),
                    "score": round(score, 2),
                }
            )
        return hits

    def _metadata_fallback(
        self, query: str, limit: int, cause: Exception | None = None
    ) -> list[dict]:
        """Degraded semantic search: substring match on title and description.

        Never the unfiltered library (L7): when nothing matches the query the
        fallback cannot stand in for semantic search, so it raises a
        RuntimeError (chaining the embedding-pipeline cause) instead of
        returning an empty list indistinguishable from "no hits". Hits are
        flagged ``fallback: true`` so callers can tell degraded results from
        ranked ones.
        """
        seen: dict[str, dict] = {}
        for field in ("title", "description"):
            for hit in self.search_metadata(field=field, value=query):
                seen.setdefault(hit["path"], hit)
        hits = [{**hit, "fallback": True} for hit in list(seen.values())[:limit]]
        if not hits:
            raise RuntimeError(
                f"semantic search unavailable (embedding pipeline failed: {cause}); "
                "no title/description matches either — use search_metadata with synonyms"
            ) from cause
        return hits

    def link_check(self, path: str | None = None) -> list[dict]:
        return links_mod.broken_links(
            self.root, self._bundle_path(path) if path is not None else None
        )

    def link_health(self, paths: list[str]) -> dict:
        """Scoped link check: inbound/outbound counts for the given bundle paths."""
        graph = links_mod.link_graph(self.root)
        inbound = {p: 0 for p in graph}
        for targets in graph.values():
            for target in targets:
                if target in inbound:
                    inbound[target] += 1
        report = {}
        for raw in paths:
            bundle = self._bundle_path(raw)
            report[bundle] = {
                "inbound": inbound.get(bundle, 0),
                "outbound": len(graph.get(bundle, ())),
            }
        return report

    def status(self) -> dict:
        report = self.validate()
        errors = report["errors"]
        warnings = report["warnings"]
        orphans = []
        for w in warnings:
            if w["code"] != "orphan":
                continue
            path = w["path"]
            title = ""
            try:
                orphan_fm, _ = fm_mod.split_document(
                    self._resolve(path).read_text(encoding="utf-8")
                )
                title = orphan_fm.get("title") or ""
            except (fm_mod.FrontmatterError, OSError, UnicodeDecodeError):
                pass
            orphans.append({"id": path[:-3], "title": title})
        broken = [
            {"source": w["path"], "target": w.get("target")}
            for w in warnings
            if w["code"] == "broken-link"
        ]
        concepts = list(links_mod.iter_concept_files(self.root))
        directories = [
            d
            for d in self.root.rglob("*")
            if d.is_dir()
            and not any(part.startswith(".") for part in d.relative_to(self.root).parts)
        ]
        mtimes = [p.stat().st_mtime for _, p in links_mod.iter_concept_files(self.root)]
        last_write = (
            datetime.fromtimestamp(max(mtimes), tz=UTC).isoformat(timespec="seconds")
            if mtimes
            else None
        )
        return {
            "stats": {
                "concepts": len(concepts),
                "directories": len(directories),
                # Key name kept for MCP contract stability; the value is the
                # git commit count now (0 when git history is disabled).
                "versions": self._git.count_commits() if self._git is not None else 0,
                "last_write": last_write,
            },
            "health": {
                "orphans": orphans,
                "broken_links": broken,
                "warnings": len(warnings),
                "errors": len(errors),
            },
            "healthy": not errors and not orphans and not broken,
        }

    # --------------------------------------------- scans (A10: OKF boundary)

    def iter_concept_files(self) -> Iterator[tuple[str, Path]]:
        """All concept files as (bundle_path, abs_path) pairs (read-only scan)."""
        return links_mod.iter_concept_files(self.root)

    def organization_findings(self, *, since: str | None = None) -> dict:
        """Structural organization findings over this bundle."""
        return organize_mod.organization_findings(self.root, since=since)

    def escape_artifact_scan(self) -> list[dict]:
        """Concept files with repairable literal \\uXXXX body artifacts (F25 stock scan)."""
        return escape_guard_mod.scan_escape_artifacts(self.root)

    def code_span_escape_candidates(self) -> list[dict]:
        """Concept files with literal \\uXXXX inside code spans/fences (LLM-judged)."""
        return escape_guard_mod.scan_code_span_escape_candidates(self.root)

    @staticmethod
    def findings_empty(report: dict) -> bool:
        return organize_mod.findings_empty(report)

    def semantic_duplicate_candidates(
        self,
        vectors: dict[str, list[float]],
        *,
        since: str | None = None,
        threshold: float | None = None,
        model: str | None = None,
    ) -> list[dict]:
        """Embedding-similarity duplicate pairs over this bundle.

        ``threshold`` None resolves to the per-model default for ``model``
        (then the module fallback), matching the librarian's resolution order.
        """
        if threshold is None:
            threshold = semantic_mod.semantic_threshold_for_model(model)
        return semantic_mod.semantic_duplicate_candidates(
            self.root, vectors, since=since, threshold=threshold
        )

    # ----------------------------------------------------------------- writes

    def create_concept(
        self,
        path: str,
        frontmatter: dict,
        body: str,
        *,
        agent_label: str | None = None,
        requested_by: str | None = None,
        via: str | None = None,
        allow_literal_escapes: bool = False,
    ) -> dict:
        with self._write_lock():
            abs_path = self._guard_concept_path(path)
            if abs_path.exists():
                raise FileExistsError(f"concept already exists: {path!r}")
            fm = dict(frontmatter)
            # Concepts are born unverified (R9); verify_concept is the sole
            # writer of 'verified', so a caller-supplied key is silently dropped.
            fm.pop("verified", None)
            self._require_type(fm)
            # F25: decode literal \uXXXX artifacts outside code spans/fences
            # before they hit disk; clean bodies pass through byte-identical.
            body, escape_warning = escape_guard_mod.decode_unicode_escapes(body)
            if escape_warning:
                logger.warning("create_concept %s: %s", path, escape_warning)
            # Warn-always on literal escapes inside code spans/fences (left
            # untouched): the scan runs on the post-decode body (code-span
            # content is decode-invariant) and is skipped entirely when the
            # caller confirmed intentional literals.
            code_span_warning = None
            if not allow_literal_escapes:
                code_span_warning = escape_guard_mod.code_span_escape_warning(body)
                if code_span_warning:
                    logger.warning("create_concept %s: %s", path, code_span_warning)
            # Caller-supplied 'generated' (incl. forged sub-keys) is replaced
            # wholesale; only the trusted parameters add provenance sub-keys.
            self._inject_generated(fm, requested_by=requested_by, via=via, preserve=False)
            bundle = self._bundle_path(path)
            write_text_atomic(abs_path, fm_mod.dump_document(fm, body))
            self._regenerate_chain(bundle)
            text = f"Created [{fm.get('title') or bundle[:-3]}]({bundle})."
            log_mod.append_entry(self.root, "Creation", text, agent_label=agent_label)
            self._git_commit("Creation", text, agent_label)
            self.seed_cache = None
            result = {"id": bundle[:-3], "action": "created"}
            warnings = [w for w in (escape_warning, code_span_warning) if w]
            if warnings:
                result["warnings"] = warnings
            return result

    def edit_concept(
        self,
        path: str,
        *,
        frontmatter_patch: dict | None = None,
        remove_keys: list[str] | None = None,
        new_body: str | None = None,
        agent_label: str | None = None,
        requested_by: str | None = None,
        via: str | None = None,
        allow_literal_escapes: bool = False,
    ) -> dict:
        with self._write_lock():
            abs_path = self._guard_concept_path(path)
            if not abs_path.is_file():
                raise FileNotFoundError(f"no such concept: {path!r}")
            if frontmatter_patch and "verified" in frontmatter_patch:
                raise ValueError("'verified' is never modified by edits")
            if remove_keys and "verified" in remove_keys:
                raise ValueError("'verified' is never modified by edits")
            # 'generated' is provenance: _inject_generated stays the sole
            # writer, so edits can neither patch nor remove it (same guard
            # shape as 'verified' above).
            if frontmatter_patch and "generated" in frontmatter_patch:
                raise ValueError("'generated' is never modified by edits")
            if remove_keys and "generated" in remove_keys:
                raise ValueError("'generated' is never modified by edits")
            fm, body = fm_mod.split_document(abs_path.read_text(encoding="utf-8"))
            escape_warning = None
            code_span_warning = None
            if new_body is not None:
                # F25: decode literal \uXXXX artifacts in the NEW body only;
                # a body-less edit never rescans the existing on-disk body.
                new_body, escape_warning = escape_guard_mod.decode_unicode_escapes(new_body)
                if escape_warning:
                    logger.warning("edit_concept %s: %s", path, escape_warning)
                if not allow_literal_escapes:
                    code_span_warning = escape_guard_mod.code_span_escape_warning(new_body)
                    if code_span_warning:
                        logger.warning("edit_concept %s: %s", path, code_span_warning)
            for key, value in (frontmatter_patch or {}).items():
                fm[key] = value
            for key in remove_keys or []:
                fm.pop(key, None)
            self._require_type(fm)
            # preserve=True: an edit without a new requester keeps existing
            # requested_by/via provenance; a new requester overwrites them.
            self._inject_generated(fm, requested_by=requested_by, via=via, preserve=True)
            bundle = self._bundle_path(path)
            write_text_atomic(
                abs_path, fm_mod.dump_document(fm, new_body if new_body is not None else body)
            )
            self._regenerate_chain(bundle)
            text = f"Updated [{fm.get('title') or bundle[:-3]}]({bundle})."
            log_mod.append_entry(self.root, "Update", text, agent_label=agent_label)
            self._git_commit("Update", text, agent_label)
            self.seed_cache = None
            result = {"id": bundle[:-3], "action": "updated"}
            warnings = [w for w in (escape_warning, code_span_warning) if w]
            if warnings:
                result["warnings"] = warnings
            return result

    def verify_concept(
        self, path: str, *, by: str, at: str | None = None, agent_label: str | None = None
    ) -> dict:
        """Append one ``{by, at}`` entry to the concept's ``verified`` list.

        The sole writer of ``verified`` (create_concept strips a supplied key;
        edit_concept refuses it). Append-only (reference.md: a bare mapping is
        normalized to a one-element list on read, then merged). ``generated``
        is deliberately NOT touched: verification is metadata, not a meaningful
        content change, so ``generated.at`` keeps marking the last content
        change. Accepted side effect: the write re-dumps the whole frontmatter
        mapping — a bare ``verified`` migrates to list form (its
        ``verified-bare-mapping`` warning disappears) and flow style/quoting
        normalize; unknown keys are never dropped.
        """
        with self._write_lock():
            abs_path = self._guard_concept_path(path)
            if not abs_path.is_file():
                raise FileNotFoundError(f"no such concept: {path!r}")
            if not isinstance(by, str) or not by:
                raise ValueError("verifier 'by' must be a non-empty string")
            fm, body = fm_mod.split_document(abs_path.read_text(encoding="utf-8"))
            entries = fm.get("verified")
            if not isinstance(entries, list):
                entries = []  # junk scalar: replaced, not merged
            entries.append({"by": by, "at": at or self._now()})
            fm["verified"] = entries
            bundle = self._bundle_path(path)
            write_text_atomic(abs_path, fm_mod.dump_document(fm, body))
            # No _regenerate_chain: index.md entries carry only title and
            # description, so a verified-only change alters no index.
            text = f"Verified [{fm.get('title') or bundle[:-3]}]({bundle}) (verifier: {by})."
            log_mod.append_entry(self.root, "Verification", text, agent_label=agent_label)
            self._git_commit("Verification", text, agent_label)
            self.seed_cache = None
            return {"id": bundle[:-3], "action": "verified"}

    def move_concept(self, old_path: str, new_path: str, *, agent_label: str | None = None) -> dict:
        with self._write_lock():
            old_abs = self._guard_concept_path(old_path)
            new_abs = self._guard_concept_path(new_path)
            if not old_abs.is_file():
                raise FileNotFoundError(f"no such concept: {old_path!r}")
            if new_abs.exists():
                raise FileExistsError(f"concept already exists: {new_path!r}")
            old_bundle = self._bundle_path(old_path)
            new_bundle = self._bundle_path(new_path)
            new_abs.parent.mkdir(parents=True, exist_ok=True)
            old_abs.rename(new_abs)
            rewritten = links_mod.rewrite_links(self.root, old_bundle, new_bundle)
            self._prune_empty_dirs(old_abs.parent)
            dirs = set(self._ancestor_dirs(old_bundle)) | set(self._ancestor_dirs(new_bundle))
            for directory in sorted(dirs):
                if self._resolve(directory).is_dir():
                    self.regenerate_index(directory)
            text = f"Moved {old_bundle} to [{new_bundle[:-3]}]({new_bundle})."
            log_mod.append_entry(self.root, "Move", text, agent_label=agent_label)
            self._git_commit("Move", text, agent_label)
            self.seed_cache = None
            return {"id": new_bundle[:-3], "action": "moved", "links_rewritten": rewritten}

    def deprecate_concept(
        self,
        path: str,
        *,
        agent_label: str | None = None,
        requested_by: str | None = None,
        via: str | None = None,
    ) -> dict:
        with self._write_lock():
            abs_path = self._guard_concept_path(path)
            if not abs_path.is_file():
                raise FileNotFoundError(f"no such concept: {path!r}")
            fm, body = fm_mod.split_document(abs_path.read_text(encoding="utf-8"))
            fm["status"] = "deprecated"
            self._inject_generated(fm, requested_by=requested_by, via=via, preserve=True)
            bundle = self._bundle_path(path)
            write_text_atomic(abs_path, fm_mod.dump_document(fm, body))
            self._regenerate_chain(bundle)
            text = f"Deprecated [{fm.get('title') or bundle[:-3]}]({bundle})."
            log_mod.append_entry(self.root, "Deprecation", text, agent_label=agent_label)
            self._git_commit("Deprecation", text, agent_label)
            self.seed_cache = None
            return {"id": bundle[:-3], "action": "deprecated"}

    def delete_concept(self, path: str, *, agent_label: str | None = None) -> dict:
        with self._write_lock():
            abs_path = self._guard_concept_path(path)
            if not abs_path.is_file():
                raise FileNotFoundError(f"no such concept: {path!r}")
            bundle = self._bundle_path(path)
            inbound = links_mod.inbound_links(self.root, bundle)
            abs_path.unlink()
            self._prune_empty_dirs(abs_path.parent)
            self._regenerate_chain(bundle)
            text = f"Deleted {bundle}."
            log_mod.append_entry(self.root, "Deletion", text, agent_label=agent_label)
            self._git_commit("Deletion", text, agent_label)
            self.seed_cache = None
            return {"id": bundle[:-3], "action": "deleted", "inbound_links": inbound}

    def write_asset(self, filename: str, data: bytes) -> str:
        """Write an immutable asset blob; return its bundle path.

        The asset name is content-addressed (``<sha256[:12]>-<filename>``),
        so re-stores of the same bytes are idempotent. Deliberately OUTSIDE
        the compound write — no index regeneration or log entry — but
        committed to git history (assets are linked from concepts): assets
        are content-addressed immutable blobs under a dot-dir, invisible to
        every OKF ``.md`` traversal (same posture as `.traces`).
        """
        if not filename or filename in (".", "..") or "/" in filename or "\\" in filename:
            raise ValueError(f"asset filename must be a bare name: {filename!r}")
        name = f"{hashlib.sha256(data).hexdigest()[:12]}-{filename}"
        with self._write_lock():
            write_bytes_atomic(self._resolve(f".athenaeum/assets/{name}"), data)
            self._git_commit("Asset", f"Stored asset {name}.")
        return f"/.athenaeum/assets/{name}"

    # ------------------------------------------------------------ maintenance

    def init_bundle(self) -> None:
        """Create the root index.md (okf_version "0.2") and root log.md."""
        with self._write_lock():
            self.root.mkdir(parents=True, exist_ok=True)
            index_path = self.root / "index.md"
            if not index_path.exists():
                write_text_atomic(index_path, index_mod.generate_index(self.root, "/"))
            if not (self.root / "log.md").exists():
                log_mod.append_entry(self.root, "Initialization", "Initialized the library bundle.")
            # Fresh libraries get their git repository here (ensure() inside
            # commit_all, covering provision_library too); no-op when nothing
            # changed.
            self._git_commit("Initialization", "Initialized the library bundle.")

    def regenerate_index(self, directory: str = "/") -> None:
        with self._write_lock():
            dir_path = self._resolve(directory)
            if not dir_path.is_dir():
                raise FileNotFoundError(f"not a directory: {directory!r}")
            rel = "/" if dir_path == self.root else "/" + dir_path.relative_to(self.root).as_posix()
            write_text_atomic(dir_path / "index.md", index_mod.generate_index(self.root, rel))

    def validate(self, scope: str | None = None) -> dict:
        return validate_mod.validate_bundle(self.root, scope)

    def reconcile(self) -> None:
        """Regenerate every index.md (startup crash-safety pass)."""
        with self._write_lock():
            dirs = [self.root] + [
                d
                for d in self.root.rglob("*")
                if d.is_dir()
                and not d.is_symlink()
                and not any(part.startswith(".") for part in d.relative_to(self.root).parts)
            ]
            for directory in dirs:
                rel = (
                    "/"
                    if directory == self.root
                    else "/" + directory.relative_to(self.root).as_posix()
                )
                self.regenerate_index(rel)

    # ---------------------------------------------------------------- history

    def list_commits(self, limit: int = 200) -> list[dict]:
        """Git history, newest first ({sha, short, timestamp, subject, is_root})."""
        return self._require_git().list_commits(limit)

    def commit_diff(self, sha: str) -> str:
        """Unified patch of one commit."""
        return self._require_git().commit_diff(sha)

    def file_history(self, path: str, limit: int = 100) -> list[dict]:
        """Per-file git history for one concept path (bundle form ``/x.md``).

        Same dict shape as ``list_commits`` plus ``"path"`` (the path valid
        at that commit, rename-tracked). ``ValueError`` on a reserved or
        non-concept path; never raises for git-side failures (``[]``).
        """
        self._guard_concept_path(path)
        rel = self._bundle_path(path).lstrip("/")
        return self._require_git().file_commits(rel, limit)

    def read_document_at(self, path: str, sha: str) -> dict:
        """``{path, frontmatter, body}`` parsed from the bytes at ``sha``.

        ``FileNotFoundError`` when the document is absent at that commit
        (the routes map it to 404 like a missing live document);
        ``GitError`` on a malformed or unknown sha.
        """
        self._guard_concept_path(path)
        git = self._require_git()
        bundle = self._bundle_path(path)
        rel = bundle.lstrip("/")
        text = git.file_at_commit(sha, self._path_at_commit(git, sha, rel))
        if text is None:
            raise FileNotFoundError(f"no such document at commit {sha[:7]}: {path!r}")
        fm, body = fm_mod.split_document(text)
        return {"path": bundle, "frontmatter": fm, "body": body}

    def file_diff_at(self, sha: str, path: str) -> str:
        """Unified patch of ``sha`` limited to one concept path."""
        self._guard_concept_path(path)
        git = self._require_git()
        rel = self._bundle_path(path).lstrip("/")
        return git.file_diff(sha, self._path_at_commit(git, sha, rel))

    def file_diff_to_head(self, sha: str, path: str, context: int | None = None) -> str:
        """Unified patch of one concept path between ``sha`` and current HEAD
        (rename-aware: covers the path valid at ``sha`` and the current path).
        ``context`` overrides the unified-context line count (whole-file diff
        for inline rendering)."""
        self._guard_concept_path(path)
        git = self._require_git()
        rel = self._bundle_path(path).lstrip("/")
        old = self._path_at_commit(git, sha, rel)
        paths = (rel,) if old == rel else (rel, old)
        return git.diff_to_head(sha, *paths, context=context)

    def restore_file_from_commit(self, path: str, sha: str) -> None:
        """Restore ONE document to its state at ``sha`` as one new commit.

        Compound write under the per-root write lock (historical bytes are
        written to the CURRENT path — rename-aware by construction).
        ``GitError`` when the document did not exist at ``sha`` (never a
        silent deletion) or when the current bytes already match (no-op
        refused, like ``reset_staged``'s "already at this commit").
        """
        with self._write_lock():
            abs_path = self._guard_concept_path(path)
            git = self._require_git()
            bundle = self._bundle_path(path)
            rel = bundle.lstrip("/")
            text = git.file_at_commit(sha, self._path_at_commit(git, sha, rel))
            if text is None:
                raise gittool_mod.GitError(f"the document did not exist at commit {sha[:7]}")
            if abs_path.is_file() and abs_path.read_text(encoding="utf-8") == text:
                raise gittool_mod.GitError("already at this state")
            try:
                write_text_atomic(abs_path, text)
                self._regenerate_chain(bundle)
                # A byte-exact restore must not fail on malformed historical
                # frontmatter — the title is only needed for the messages.
                try:
                    fm, _ = fm_mod.split_document(text)
                    title = fm.get("title") or bundle[:-3]
                except (fm_mod.FrontmatterError, ValueError):
                    title = bundle[:-3]
                text_msg = f"Restored [{title}]({bundle}) from commit {sha[:7]}."
                log_mod.append_entry(self.root, "Update", text_msg)
                git.commit_staged(f"Update: {text_msg}")
            except gittool_mod.GitError:
                git.abort_staged()
                raise
            self.seed_cache = None

    def _path_at_commit(self, git: gittool_mod.GitRepo, sha: str, rel: str) -> str:
        """The repo-relative path valid at ``sha`` (rename-aware).

        Resolved from the file's own log; falls back to the current path
        when ``sha`` is not in it (defensive — hand-crafted URLs).
        """
        for entry in git.file_commits(rel):
            if entry["sha"] == sha or entry["sha"].startswith(sha):
                return entry["path"]
        return rel

    def git_head(self) -> str | None:
        """Full sha of HEAD; None on an unborn branch."""
        return self._require_git().head_sha()

    def revert_commit(self, sha: str) -> None:
        """Undo one commit: reverse-apply it, log the revert, commit once.

        ``GitError`` on an unknown sha, the root commit, or a revert
        conflict; the staged state is aborted before re-raising so the
        worktree is left clean.
        """
        with self._write_lock():
            git = self._require_git()
            try:
                git.revert_staged(sha)
                log_mod.append_entry(self.root, "Update", f"Reverted commit {sha[:7]}.")
                git.commit_staged(f"Update: Reverted commit {sha[:7]}.")
            except gittool_mod.GitError:
                git.abort_staged()
                raise
            self.seed_cache = None

    def reset_to_commit(self, sha: str) -> None:
        """Reset the library to an earlier commit (append-only, undoable).

        Index+worktree become exactly ``sha``; ONE new commit records the
        reset, so the pre-reset state stays reachable as its parent (undo is
        another reset through the same UI). ``GitError`` on an unknown sha
        or when already at that commit.
        """
        with self._write_lock():
            git = self._require_git()
            try:
                short = git.reset_staged(sha)
                log_mod.append_entry(self.root, "Update", f"Reset library to {short}.")
                git.commit_staged(f"Update: Reset library to {short}.")
            except gittool_mod.GitError:
                git.abort_staged()
                raise
            self.seed_cache = None

    def git_pull(self) -> None:
        """Fast-forward pull from the configured remote, then reconcile.

        The imported tree may drift the indexes, so a reconcile pass runs
        before the (no-op when unchanged) follow-up commit. ``GitError`` on
        divergence or any other pull failure.
        """
        with self._write_lock():
            git = self._require_git()
            # Wires origin when the remote was configured after the last
            # auto-commit; never raises (degrades to an honest pull error).
            git.ensure()
            git.pull_ff_only()
            self.reconcile()
            self._git_commit("Update", "Pulled from remote.")

    # --------------------------------------------------------------- internal

    def _write_lock(self) -> threading.RLock:
        """The process-wide write lock for this backend's library root."""
        return write_lock_for(self.root)

    def _resolve(self, path: str) -> Path:
        return resolve_under(self.root, path)

    @staticmethod
    def _bundle_path(path: str) -> str:
        normalized = str(path).replace("\\", "/")
        return normalized if normalized.startswith("/") else "/" + normalized

    def _did_you_mean(self, path: str, *, directories: bool) -> str:
        """Close-match suffix for a missing-path FileNotFoundError; "" when no match.

        Candidates come from iter_concept_files (symlink-screened, root-relative),
        so a suggestion can never point outside the library root.
        """
        candidates: set[str] = set()
        walk = links_mod.iter_concept_files(self.root)
        for bundle_path, _ in itertools.islice(walk, _SUGGEST_CANDIDATE_LIMIT):
            if directories:
                candidates.update(self._ancestor_dirs(bundle_path))
            else:
                candidates.add(bundle_path)
        matches = difflib.get_close_matches(
            self._bundle_path(path), sorted(candidates), n=3, cutoff=0.6
        )
        if not matches:
            return ""
        return f". Did you mean: {', '.join(repr(m) for m in matches)}?"

    def _guard_concept_path(self, path: str) -> Path:
        bundle = self._bundle_path(path)
        name = posixpath.basename(bundle)
        if not name.endswith(".md"):
            raise ValueError(f"concept path must end with .md: {path!r}")
        # Reserved names are refused in ANY path component, not just the
        # basename: a mid-path index.md/log.md would shadow a directory's
        # generated index or the root log.
        for part in (p for p in bundle.split("/") if p):
            if part in RESERVED_NAMES:
                raise ValueError(f"reserved filename cannot be a concept: {part!r}")
        if any(part.startswith(".") for part in bundle.split("/") if part):
            raise ValueError(f"hidden path components not allowed: {path!r}")
        return self._resolve(bundle)

    @staticmethod
    def _require_type(fm: dict) -> None:
        type_ = fm.get("type")
        if not isinstance(type_, str) or not type_.strip():
            raise ValueError("frontmatter requires a non-empty 'type'")

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")

    def _inject_generated(
        self, fm: dict, *, requested_by: str | None, via: str | None, preserve: bool
    ) -> None:
        """Write the ``generated`` mapping for a compound write.

        ``generated.by`` stays ``self.actor`` in every path. ``preserve=True``
        (edit/deprecate) keeps existing provenance sub-keys when no new
        requester is supplied; ``preserve=False`` (create) replaces a
        caller-supplied ``generated`` wholesale.
        """
        generated = dict(fm.get("generated") or {}) if preserve else {}
        generated["by"] = self.actor
        generated["at"] = self._now()
        if requested_by is not None:
            generated["requested_by"] = requested_by
        if via is not None:
            generated["via"] = via
        fm["generated"] = generated

    @staticmethod
    def _ancestor_dirs(bundle_path: str) -> list[str]:
        dirs = []
        current = posixpath.dirname(bundle_path)
        while current and current != "/":
            dirs.append(current)
            current = posixpath.dirname(current)
        dirs.append("/")
        return dirs

    def _regenerate_chain(self, bundle_path: str) -> None:
        for directory in self._ancestor_dirs(bundle_path):
            dir_path = self._resolve(directory)
            if dir_path.is_dir():
                self.regenerate_index(directory)

    def _prune_empty_dirs(self, start: Path) -> None:
        """Remove ancestor dirs left empty (besides their index.md), up to the root."""
        current = start
        while current != self.root and current.is_dir():
            children = list(current.iterdir())
            if any(c.name != "index.md" for c in children):
                break
            for child in children:
                if child.is_dir():
                    continue  # defensive: directories are never unlink()ed
                child.unlink()
            current.rmdir()
            current = current.parent

    def _require_git(self) -> gittool_mod.GitRepo:
        """The history repo; GitError when git history is disabled."""
        if self._git is None:
            raise gittool_mod.GitError("git history is disabled for this backend")
        return self._git

    def _git_commit(self, kind: str, text: str, agent_label: str | None = None) -> None:
        """Best-effort auto-commit after a compound write (never raises).

        The commit message mirrors the log.md entry text, including the
        ``(requested by agent:<label>)`` attribution suffix.
        """
        if self._git is None:
            return
        if agent_label:
            text = f"{text} (requested by agent:{agent_label})"
        self._git.commit_all(f"{kind}: {text}")
