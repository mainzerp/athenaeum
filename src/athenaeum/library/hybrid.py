"""Hybrid retrieval: FTS5 query sanitizing, RRF fusion, cross-encoder rerank.

Pure helpers (``sanitize_match_query``, ``rrf_merge``) plus the
``CrossEncoderReranker`` seam used by ``LibraryBackend.search_semantic``.
Stdlib-only at import time; fastembed is imported lazily inside the reranker
(the ``local`` extra is optional, and construction downloads ONNX weights on
first use).

Score contract: when the reranker ran, hit scores are cross-encoder logits
(higher = better, may be negative); otherwise they are reciprocal-rank-fusion
scores. Both are relative ranking aids, never trust signals.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# TUNABLEs
HYBRID_RRF_K = 60  # RRF damping constant (qmd/RRF convention)
HYBRID_RERANK_CANDIDATES = 30  # fused candidates fed to the reranker
# fastembed 0.8.0 registry; 0.08 GB, apache-2.0.
HYBRID_RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
HYBRID_QUERY_TOKEN_CAP = 16  # max MATCH tokens; OR-joined, so recall stays high
HYBRID_FTS_TABLE = "concepts_fts"

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def sanitize_match_query(query: str) -> str | None:
    """Build a safe FTS5 MATCH expression from free-text user input.

    Tokens are ``\\w+`` runs, deduped preserving order, capped at
    ``HYBRID_QUERY_TOKEN_CAP``, and double-quoted — quoting every token
    neutralizes FTS5 operators (``AND OR NOT NEAR ( ) : ^ * "``) in the raw
    input, so a user query can never break or reprogram the MATCH. Tokens are
    OR-joined: the lexical leg optimizes recall, BM25 does the ranking.
    Returns None when the input yields no tokens (caller returns no hits).
    """
    tokens = list(dict.fromkeys(_TOKEN_RE.findall(query)))
    if not tokens:
        return None
    return " OR ".join(f'"{token}"' for token in tokens[:HYBRID_QUERY_TOKEN_CAP])


def rrf_merge(ranked_lists: list[list[str]], *, k: int = HYBRID_RRF_K) -> list[tuple[str, float]]:
    """Reciprocal rank fusion over pre-ranked key lists.

    Inputs are key lists in rank order (each leg's own scores are irrelevant;
    RRF is rank-based, so the bm25 lower-is-better direction is already
    handled by the FTS query's ORDER BY). Per list, rank is 1-based and
    contributes ``1 / (k + rank)``; contributions sum across lists. Sorted by
    ``(-score, path)`` so ties break deterministically.
    """
    scores: dict[str, float] = {}
    for keys in ranked_lists:
        for rank, key in enumerate(keys, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


class CrossEncoderReranker:
    """Local fastembed cross-encoder (ONNX) over fused candidates.

    Construction stores config only — no model load, no fastembed import.
    The model is memoized on first use under a lock (pattern:
    ``librarian/embed/local.py``); construction and inference both run via
    ``asyncio.to_thread`` so ONNX stays off the event loop (A6). ``rerank``
    returns None on any failure (fastembed missing, download error, ONNX
    crash): the caller then keeps the RRF order — the reranker is a quality
    layer, never a hard dependency.
    """

    def __init__(self, model_name: str = HYBRID_RERANK_MODEL, cache_dir: Path | None = None):
        self._model_name = model_name
        self._cache_dir = cache_dir
        self._model = None
        self._model_lock = threading.Lock()
        self._unavailable = False  # ImportError is terminal for this instance

    def _model_or_none(self):
        with self._model_lock:
            if self._model is None and not self._unavailable:
                try:
                    # fastembed 0.8.0: TextCrossEncoder is not a top-level export.
                    from fastembed.rerank.cross_encoder import TextCrossEncoder
                except ImportError:
                    logger.info("fastembed reranker unavailable; hybrid search stays RRF-only")
                    self._unavailable = True
                    return None
                kwargs = {"model_name": self._model_name}
                if self._cache_dir is not None:
                    kwargs["cache_dir"] = str(self._cache_dir)
                self._model = TextCrossEncoder(**kwargs)
            return self._model

    async def rerank(self, query: str, texts: list[str]) -> list[float] | None:
        """Cross-encoder logits aligned with ``texts``; None on any failure."""
        if not texts:
            return []
        try:
            model = await asyncio.to_thread(self._model_or_none)
            if model is None:
                return None
            return await asyncio.to_thread(
                lambda: [float(score) for score in model.rerank(query, texts)]
            )
        except Exception:
            logger.warning("cross-encoder rerank failed; keeping RRF order", exc_info=True)
            return None
