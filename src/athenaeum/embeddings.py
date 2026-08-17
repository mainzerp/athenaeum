"""Embedding store and service: app.db CRUD over the ``embeddings`` table.

One ``EmbeddingService`` per user bundles store access + provider. Vectors are
stored as float32 BLOBs keyed by ``(user_id, concept_path)`` with the source
text's SHA-256 for drift detection. The service is the only writer; consumers
(F1 search, F2 duplicates, F3 related-injection) read through it or through
``load()``.

Local-provider inference offloads internally; API providers are async httpx,
so the async high-level methods never block the event loop beyond short-lived
sqlite I/O (same posture as db.py).

Keys are canonical: ``concept_path`` values are always stored without a
leading slash (``x.md``), regardless of whether callers pass backend-style
ids (``/x``) or bundle paths (``/x.md``). Normalization happens once at the
service boundary (``upsert``/``delete``/``load``), so the table never holds
mixed key shapes.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import socket
import struct
import time
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from athenaeum import db
from athenaeum.librarian.embed import (
    KIND_DOCUMENT,
    KIND_QUERY,
    EmbeddingConfig,
    EmbeddingProvider,
)
from athenaeum.library import escape_guard as escape_guard_mod
from athenaeum.library.links import LINK_RE

if TYPE_CHECKING:
    from athenaeum.library.backend import LibraryBackend

logger = logging.getLogger(__name__)

RECONCILE_BATCH = 32

# A reconcile holding its DB claim longer than this is presumed crashed and
# the slot becomes reclaimable; generous because batches are provider-paced.
RECONCILE_CLAIM_TTL = 3600.0  # seconds


def strip_link_targets(body: str) -> str:
    """Reduce inline markdown links to their anchor text for indexing.

    ``[text](url)`` (including the optional ``"title"``) becomes ``text``, so
    link URLs do not pollute embedding vectors or the FTS index. Fence- and
    code-span aware via ``escape_guard``: links inside fenced code blocks or
    inline code spans pass through byte-identical. Images (``![alt](src)``)
    are untouched — ``LINK_RE``'s ``(?<!!)`` lookbehind excludes them, shared
    with graph extraction. A link-free body round-trips byte-identical (early
    out). Out of scope by design (untouched): reference-style links
    (``[t][ref]``), autolinks (``<https://...>``), and bare URLs — ``LINK_RE``
    is the shared inline-link contract, and a second regex would drift.
    """
    if not LINK_RE.search(body):
        return body
    parts: list[str] = []
    for is_fenced, text in escape_guard_mod._split_fence_segments(body):
        if is_fenced:
            parts.append(text)
            continue
        pos = 0
        for start, end in escape_guard_mod._iter_code_spans(text):
            parts.append(LINK_RE.sub(lambda m: m.group(1), text[pos:start]))
            parts.append(text[start:end])
            pos = end
        parts.append(LINK_RE.sub(lambda m: m.group(1), text[pos:]))
    return "".join(parts)


def concept_text(frontmatter: dict, body: str) -> str:
    """Canonical embedded text: title + description + body (empty-tolerant).

    The body is link-stripped (``strip_link_targets``): inline markdown links
    contribute their anchor text only, outside code fences/spans.
    """
    title = frontmatter.get("title") or ""
    description = frontmatter.get("description") or ""
    return f"{title}\n{description}\n\n{strip_link_targets(body)}"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def canonical_path(concept_path: str) -> str:
    """Canonical embeddings-table key: relative path, no leading slash."""
    return concept_path.lstrip("/")


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def _unpack(blob: bytes) -> list[float]:
    dims = len(blob) // 4
    return list(struct.unpack(f"{dims}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    """Stdlib cosine similarity: the fallback for ``top_k`` and
    ``library/semantic.py`` when numpy is unavailable (a few ms at <1000
    concepts)."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class EmbedStatusRegistry:
    """In-memory per-user reconcile status (single-worker deployment)."""

    def __init__(self) -> None:
        self._status: dict[str, dict] = {}

    def get(self, user_id: str) -> dict | None:
        return self._status.get(user_id)

    def begin(self, user_id: str, total: int, model: str) -> None:
        self._status[user_id] = {
            "state": "running",
            "total": total,
            "done": 0,
            "model": model,
            "error": None,
            "started_at": db.utcnow(),
        }

    def progress(self, user_id: str, done: int) -> None:
        status = self._status.get(user_id)
        if status is not None:
            status["done"] = done

    def finish(self, user_id: str, error: str | None = None) -> None:
        status = self._status.get(user_id)
        if status is None:
            return
        status["state"] = "failed" if error else "idle"
        status["error"] = error
        status["finished_at"] = db.utcnow()


class EmbeddingService:
    """Per-user embedding store + provider bundle."""

    def __init__(
        self,
        db_path: str | Path,
        user_id: str,
        config: EmbeddingConfig,
        provider: EmbeddingProvider,
        *,
        status: EmbedStatusRegistry | None = None,
        fts=None,
    ) -> None:
        self.db_path = Path(db_path)
        self.user_id = user_id
        self.config = config
        self.provider = provider
        self._status = status
        # Duck-typed FtsIndex collaborator (hybrid search lexical leg); the
        # service drives it, it never drives back (fts.py -> embeddings.py is
        # a one-way edge).
        self.fts = fts
        # Cache-aside mirror of the embeddings table; the service is the only
        # writer (module docstring), so own writes invalidate/update it.
        # Single-worker deployment.
        self._vector_cache: dict[str, dict] | None = None

    # --- CRUD (sync sqlite3, short-lived connections) -------------------

    def upsert(self, concept_path: str, model: str, vector: list[float], hash_: str) -> None:
        concept_path = canonical_path(concept_path)
        with closing(db.connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO embeddings"
                    " (user_id, concept_path, model, dims, vector, content_hash, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (user_id, concept_path) DO UPDATE SET"
                    " model = excluded.model, dims = excluded.dims,"
                    " vector = excluded.vector, content_hash = excluded.content_hash,"
                    " updated_at = excluded.updated_at",
                    (
                        self.user_id,
                        concept_path,
                        model,
                        len(vector),
                        _pack(vector),
                        hash_,
                        db.utcnow(),
                    ),
                )
        if self._vector_cache is not None:
            self._vector_cache[concept_path] = {
                "model": model,
                "dims": len(vector),
                "vector": vector,
                "content_hash": hash_,
            }

    def delete(self, concept_path: str) -> None:
        concept_path = canonical_path(concept_path)
        with closing(db.connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    "DELETE FROM embeddings WHERE user_id = ? AND concept_path = ?",
                    (self.user_id, concept_path),
                )
        if self._vector_cache is not None:
            self._vector_cache.pop(concept_path, None)

    def load(self) -> dict[str, dict]:
        """All stored rows: concept_path -> {model, dims, vector, content_hash}.

        The result is a snapshot; repeated calls are served from memory
        (``_vector_cache``) until the next write. A shallow copy is returned
        so callers iterating from a ``to_thread`` worker are insulated from
        concurrent ``upsert``/``delete`` updates.
        """
        if self._vector_cache is None:
            with closing(db.connect(self.db_path)) as conn:
                rows = conn.execute(
                    "SELECT concept_path, model, dims, vector, content_hash"
                    " FROM embeddings WHERE user_id = ?",
                    (self.user_id,),
                ).fetchall()
            self._vector_cache = {
                canonical_path(row["concept_path"]): {
                    "model": row["model"],
                    "dims": row["dims"],
                    "vector": _unpack(row["vector"]),
                    "content_hash": row["content_hash"],
                }
                for row in rows
            }
        return dict(self._vector_cache)

    def stats(self) -> dict:
        """Row count + stored model(s)/dims for the WebUI status card."""
        with closing(db.connect(self.db_path)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM embeddings WHERE user_id = ?", (self.user_id,)
            ).fetchone()["n"]
            models = [
                row["model"]
                for row in conn.execute(
                    "SELECT DISTINCT model FROM embeddings WHERE user_id = ?", (self.user_id,)
                ).fetchall()
            ]
            dims = [
                row["dims"]
                for row in conn.execute(
                    "SELECT DISTINCT dims FROM embeddings WHERE user_id = ?", (self.user_id,)
                ).fetchall()
            ]
        return {"rows": count, "models": models, "dims": dims}

    # --- math ------------------------------------------------------------

    def top_k(self, query_vector: list[float], k: int) -> list[tuple[str, float]]:
        """(concept_path, score) pairs over stored vectors, sorted desc.

        Rows with mismatched dims (model transition, reconcile pending) are
        skipped rather than crashing the whole ranking. numpy-vectorized when
        numpy is importable (same optional-import posture as
        ``library/semantic.py``); the stdlib ``cosine`` loop runs otherwise.
        """
        rows = [
            (concept_path, row["vector"])
            for concept_path, row in self.load().items()
            if row["dims"] == len(query_vector)
        ]
        try:
            import numpy as np
        except ImportError:
            np = None
        if np is None or not rows:
            scored = [(path, cosine(query_vector, vector)) for path, vector in rows]
            scored.sort(key=lambda pair: pair[1], reverse=True)
            return scored[:k]
        matrix = np.asarray([vector for _, vector in rows], dtype=np.float64)
        q = np.asarray(query_vector, dtype=np.float64)
        norms = np.sqrt((matrix * matrix).sum(axis=1))
        q_norm = float(np.sqrt((q * q).sum()))
        with np.errstate(divide="ignore", invalid="ignore"):
            scores = (matrix @ q) / (norms * q_norm)
        scores = np.nan_to_num(scores)  # zero-norm rows score 0.0, like cosine
        order = np.argsort(-scores, kind="stable")[:k]
        return [(rows[i][0], float(scores[i])) for i in order]

    # --- async high-level --------------------------------------------------

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self.provider.embed([text], self.config, kind=KIND_QUERY)
        return vectors[0]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self.provider.embed(texts, self.config, kind=KIND_DOCUMENT)

    async def search_ids(self, query: str, limit: int) -> list[tuple[str, float]]:
        """(concept_id, score) pairs; concept ids are paths minus ``.md``.

        Raises on any embedding-pipeline failure (callers own fallback policy).
        """
        t0 = time.perf_counter()
        query_vector = await self.embed_query(query)
        t1 = time.perf_counter()
        ranked = await asyncio.to_thread(self.top_k, query_vector, limit)
        t2 = time.perf_counter()
        logger.debug("search_ids: query embedding %.1f ms", (t1 - t0) * 1000)
        logger.debug("search_ids: vector scan %.1f ms", (t2 - t1) * 1000)
        return [
            (path[: -len(".md")] if path.endswith(".md") else path, score) for path, score in ranked
        ]

    async def related(self, text: str, k: int) -> list[tuple[str, float]]:
        """Same ranking as ``search_ids`` for raw text (F3 injection)."""
        return await self.search_ids(text, k)

    async def sync_writes(self, backend: LibraryBackend, writes: list[dict]) -> None:
        """Best-effort write-through index update; NEVER raises.

        Actions are collapsed per concept path (last one wins) before any I/O:
        ``deleted`` and ``deprecated`` remove the row (deprecated concepts are
        hidden pending cleanup), a ``moved`` write's ``from_id`` removes
        the OLD path's row, and everything else re-reads the concept through
        the backend (A10) and re-embeds (L8 — a create-then-delete in one run
        must not resurrect a row, and a move must not leak the stale old-path
        row). All surviving writes embed in ONE batched call.
        """
        # backend result ids are "/x"-shaped; the store key is "x.md".
        if self.fts is not None:
            # FTS rows land FIRST: even when the embed call below fails, the
            # lexical leg (the no-provider degradation leg) stays current.
            self.fts.sync_writes(backend, writes)
        by_path: dict[str, dict] = {}
        moved_from: list[str] = []
        for write in writes:
            by_path[canonical_path(f"{write['id']}.md")] = write
            if write.get("action") == "moved" and write.get("from_id"):
                moved_from.append(canonical_path(f"{write['from_id']}.md"))
        for concept_path in moved_from:
            # Delete the OLD path's row so it cannot leak into top_k ranking
            # (L8); a same-run re-create of that path is upserted below. A
            # failure here (e.g. sqlite) is logged and skipped — it cannot
            # escape the NEVER-raise contract (fts.py precedent).
            try:
                self.delete(concept_path)
            except Exception as exc:
                logger.warning("embedding sync skipped %s: %s", concept_path, exc)
        pending: list[tuple[str, str, str]] = []  # (concept_path, text, hash)
        for concept_path, write in by_path.items():
            try:
                if write.get("action") in ("deleted", "deprecated"):
                    self.delete(concept_path)
                    continue
                doc = backend.read_document(concept_path)
                assembled = concept_text(doc["frontmatter"], doc["body"])
                pending.append((concept_path, assembled, content_hash(assembled)))
            except Exception as exc:
                logger.warning("embedding sync skipped %s: %s", concept_path, exc)
        if not pending:
            return
        try:
            vectors = await self.embed_documents([text for _, text, _ in pending])
            for (concept_path, _, hash_), vector in zip(pending, vectors, strict=True):
                self.upsert(concept_path, self.config.model, vector, hash_)
        except Exception as exc:
            logger.warning("embedding sync failed: %s", exc)

    async def reconcile(self, backend: LibraryBackend) -> None:
        """Full drift pass: embed missing/changed concepts, drop vanished rows.

        Model mismatch forces a full re-embed (locked decision g). Guarded
        against concurrent runs by a DB claim row (cross-instance safe, TTL
        covers crashed owners); the status registry only reports progress.
        The tree scan runs through the backend (A10).
        """
        owner = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex}"
        with closing(db.connect(self.db_path)) as conn:
            claimed = db.try_claim_embed_reconcile(conn, self.user_id, owner, RECONCILE_CLAIM_TTL)
        if not claimed:
            return
        try:
            await self._reconcile_inner(backend)
        except Exception as exc:
            if self._status is not None:
                self._status.finish(self.user_id, error=str(exc))
                raise
            logger.warning("embedding reconcile failed: %s", exc)
        finally:
            with closing(db.connect(self.db_path)) as conn:
                db.release_embed_reclaim(conn, self.user_id, owner)

    async def _reconcile_inner(self, backend: LibraryBackend) -> None:
        stored = self.load()
        on_disk: dict[str, tuple[str, str]] = {}  # concept_path -> (text, hash)
        for bundle_path, _abs_path in backend.iter_concept_files():
            concept_path = canonical_path(bundle_path)
            try:
                doc = backend.read_document(bundle_path)
            except Exception as exc:
                logger.warning("embedding reconcile skipped %s: %s", concept_path, exc)
                continue
            if doc["frontmatter"].get("status") == "deprecated":
                # Hidden pending cleanup: excluded from on_disk, so a stale
                # stored row drops out in the deletion pass below.
                continue
            text = concept_text(doc["frontmatter"], doc["body"])
            on_disk[concept_path] = (text, content_hash(text))
        stale = [
            concept_path
            for concept_path, (_, hash_) in on_disk.items()
            if concept_path not in stored
            or stored[concept_path]["content_hash"] != hash_
            or stored[concept_path]["model"] != self.config.model
        ]
        for concept_path in stored:
            if concept_path not in on_disk:
                self.delete(concept_path)
        if self._status is not None:
            self._status.begin(self.user_id, len(stale), self.config.model)
        done = 0
        for offset in range(0, len(stale), RECONCILE_BATCH):
            batch = stale[offset : offset + RECONCILE_BATCH]
            vectors = await self.embed_documents([on_disk[path][0] for path in batch])
            for concept_path, vector in zip(batch, vectors, strict=True):
                self.upsert(concept_path, self.config.model, vector, on_disk[concept_path][1])
            done += len(batch)
            if self._status is not None:
                self._status.progress(self.user_id, done)
        if self._status is not None:
            self._status.finish(self.user_id)
        if self.fts is not None:
            # Inside the same claim; FtsIndex.reconcile contains its own
            # exceptions, so an FTS failure cannot fail the embed reconcile.
            self.fts.reconcile(backend)

    def fts_search(self, query: str, limit: int) -> list[tuple[str, float]]:
        """Lexical leg of hybrid search: (concept_path, bm25) best-first.

        Empty without an FTS collaborator — the backend reaches the FTS index
        only through the service (it owns neither db_path nor user_id).
        """
        if self.fts is None:
            return []
        return self.fts.search(query, limit)
