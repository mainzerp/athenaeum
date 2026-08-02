"""FTS5 lexical index over the ``concepts_fts`` virtual table (hybrid search).

Sibling of ``embeddings.py``: one ``FtsIndex`` per user, sync sqlite3
short-lived connections via ``db.connect``, and a NEVER-RAISE contract on the
two lifecycle methods (``sync_writes``/``reconcile``). Keys and text are
identical to the embeddings table — canonical ``concept_path`` (``x.md``, no
leading slash), ``concept_text`` output, SHA-256 content hash — so the two
indexes drift and reconcile in lockstep.

The index rides the embedding service's flows: ``EmbeddingService`` drives
``sync_writes`` after librarian writes and ``reconcile`` inside its existing
DB claim, so the FTS index needs no claim row of its own and exists only
where an embedding service exists. The table is plain-content FTS5 with the
porter tokenizer (better prose recall); changing the tokenizer later means
DROP + reindex (reconcile rebuilds).

bm25() direction: lower is better (scores are negative; verified by probe —
see docs/SubAgent/HYBRID_SEARCH/CHANGES.md Step 0), so searches ORDER BY
score ASC.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

from athenaeum import db
from athenaeum.embeddings import canonical_path, concept_text, content_hash
from athenaeum.library.hybrid import HYBRID_FTS_TABLE, sanitize_match_query

if TYPE_CHECKING:
    from athenaeum.library.backend import LibraryBackend

logger = logging.getLogger(__name__)


class FtsIndex:
    """Per-user FTS5 index over concept text (the lexical leg of hybrid search)."""

    def __init__(self, db_path: str | Path, user_id: str) -> None:
        self.db_path = Path(db_path)
        self.user_id = user_id
        self._available: bool | None = None  # probed once, cached

    @property
    def available(self) -> bool:
        """True when the concepts_fts table exists (FTS5 compiled in, init_db ran)."""
        if self._available is None:
            try:
                with closing(db.connect(self.db_path)) as conn:
                    row = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (HYBRID_FTS_TABLE,),
                    ).fetchone()
                self._available = row is not None
            except sqlite3.OperationalError:
                self._available = False
        return self._available

    # --- CRUD ---------------------------------------------------------------

    def upsert(self, concept_path: str, text: str, hash_: str) -> None:
        """Insert or replace the row (plain-content FTS5 has no UNIQUE —
        delete+insert IS the upsert)."""
        concept_path = canonical_path(concept_path)
        with closing(db.connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    f"DELETE FROM {HYBRID_FTS_TABLE} WHERE user_id = ? AND concept_path = ?",
                    (self.user_id, concept_path),
                )
                conn.execute(
                    f"INSERT INTO {HYBRID_FTS_TABLE} (user_id, concept_path, text, content_hash)"
                    " VALUES (?, ?, ?, ?)",
                    (self.user_id, concept_path, text, hash_),
                )

    def delete(self, concept_path: str) -> None:
        concept_path = canonical_path(concept_path)
        with closing(db.connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    f"DELETE FROM {HYBRID_FTS_TABLE} WHERE user_id = ? AND concept_path = ?",
                    (self.user_id, concept_path),
                )

    def search(self, query: str, limit: int) -> list[tuple[str, float]]:
        """(concept_path, bm25) pairs, best first (bm25 is lower-is-better).

        Empty on an unavailable table, a tokenless query, or any operational
        error — the lexical leg degrades silently; fusion still has the
        semantic leg.
        """
        if not self.available:
            return []
        match = sanitize_match_query(query)
        if match is None:
            return []
        try:
            with closing(db.connect(self.db_path)) as conn:
                rows = conn.execute(
                    f"SELECT concept_path, bm25({HYBRID_FTS_TABLE}) AS score"
                    f" FROM {HYBRID_FTS_TABLE}"
                    f" WHERE {HYBRID_FTS_TABLE} MATCH ? AND user_id = ?"
                    " ORDER BY score ASC LIMIT ?",
                    (match, self.user_id, limit),
                ).fetchall()
        except sqlite3.OperationalError as exc:
            logger.warning("FTS search failed: %s", exc)
            return []
        return [(row["concept_path"], row["score"]) for row in rows]

    def hashes(self) -> dict[str, str]:
        """concept_path -> content_hash for this user (reconcile drift signal)."""
        with closing(db.connect(self.db_path)) as conn:
            rows = conn.execute(
                f"SELECT concept_path, content_hash FROM {HYBRID_FTS_TABLE} WHERE user_id = ?",
                (self.user_id,),
            ).fetchall()
        return {canonical_path(row["concept_path"]): row["content_hash"] for row in rows}

    # --- lifecycle (NEVER raise; driven by EmbeddingService) ----------------

    def sync_writes(self, backend: LibraryBackend, writes: list[dict]) -> None:
        """Best-effort write-through index update; NEVER raises.

        Mirrors ``EmbeddingService.sync_writes`` minus the embedding: actions
        collapse per path (last wins), ``deleted`` removes the row, a
        ``moved`` write's ``from_id`` removes the OLD path's row (L8), and
        survivors are re-read through the backend and re-indexed with the
        current content hash. Per-write failures are logged and skipped.
        """
        if not self.available:
            return
        try:
            # backend result ids are "/x"-shaped; the store key is "x.md".
            by_path: dict[str, dict] = {}
            moved_from: list[str] = []
            for write in writes:
                by_path[canonical_path(f"{write['id']}.md")] = write
                if write.get("action") == "moved" and write.get("from_id"):
                    moved_from.append(canonical_path(f"{write['from_id']}.md"))
            for concept_path in moved_from:
                # Delete the OLD path's row so it cannot leak into search
                # (L8); a same-run re-create of that path is upserted below.
                self.delete(concept_path)
            for concept_path, write in by_path.items():
                try:
                    if write.get("action") == "deleted":
                        self.delete(concept_path)
                        continue
                    doc = backend.read_document(concept_path)
                    assembled = concept_text(doc["frontmatter"], doc["body"])
                    self.upsert(concept_path, assembled, content_hash(assembled))
                except Exception as exc:
                    logger.warning("FTS sync skipped %s: %s", concept_path, exc)
        except Exception as exc:
            logger.warning("FTS sync failed: %s", exc)

    def reconcile(self, backend: LibraryBackend) -> None:
        """Full drift pass: index missing/changed concepts, drop vanished rows.

        The content hash is the only drift signal (no model dimension). Runs
        inside ``EmbeddingService.reconcile``'s existing claim — no claim row
        of its own. NEVER raises.
        """
        if not self.available:
            return
        try:
            stored = self.hashes()
            on_disk: dict[str, tuple[str, str]] = {}  # concept_path -> (text, hash)
            for bundle_path, _abs_path in backend.iter_concept_files():
                concept_path = canonical_path(bundle_path)
                try:
                    doc = backend.read_document(bundle_path)
                except Exception as exc:
                    logger.warning("FTS reconcile skipped %s: %s", concept_path, exc)
                    continue
                text = concept_text(doc["frontmatter"], doc["body"])
                on_disk[concept_path] = (text, content_hash(text))
            for concept_path in stored:
                if concept_path not in on_disk:
                    self.delete(concept_path)
            for concept_path, (text, hash_) in on_disk.items():
                if stored.get(concept_path) != hash_:
                    self.upsert(concept_path, text, hash_)
        except Exception as exc:
            logger.warning("FTS reconcile failed: %s", exc)
