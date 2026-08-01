"""Semantic duplicate candidates: embedding-similarity concept pairs (F2).

Pure over cached vectors: no embedding calls at scan time — the caller
(``Librarian.handle_curate``) passes the embedding store's ``load()`` output,
so the zero-cost converged-night property survives. Gates mirror
``organize.organization_findings``' near-duplicate pass (same-type,
changed-set, series guard) plus a Jaccard dedup so a pair already reported by
the structural pass is not double-reported under two kinds. Per-model default
thresholds live in ``SEMANTIC_DUPLICATE_THRESHOLDS`` and are resolved by the
caller (Librarian), keeping this pass pure.
"""

from __future__ import annotations

from pathlib import Path

from athenaeum.embeddings import cosine

from . import frontmatter as fm_mod
from . import links as links_mod
from .organize import (
    NEAR_DUPLICATE_JACCARD,
    _is_series_pair,
    _jaccard,
    _norm_ts,
    _raw_ts,
    _tokens,
)

# TUNABLE — fallback for models not in SEMANTIC_DUPLICATE_THRESHOLDS
SEMANTIC_DUPLICATE_COSINE = 0.85
# NOTE (L9): these per-model values were calibrated against vectors that had
# `query:`/`passage:` prefixes applied to ALL local models. Since the prefix
# fix, only the E5 family embeds prefixed text — BGE/MiniLM vectors changed
# basis, so these thresholds may need empirical re-tuning.
SEMANTIC_DUPLICATE_THRESHOLDS: dict[str, float] = {
    "BAAI/bge-small-en-v1.5": 0.85,
    "BAAI/bge-base-en-v1.5": 0.85,
    "sentence-transformers/all-MiniLM-L6-v2": 0.80,
    "intfloat/multilingual-e5-small": 0.82,
}
SEMANTIC_DUPLICATE_MAX_PAIRS = 20  # mirrors NEAR_DUPLICATE_MAX_PAIRS


def semantic_threshold_for_model(model: str | None) -> float:
    """Effective semantic-duplicate threshold for an embedding model.

    Per-model defaults are initial calibrations (TUNABLE); unknown models get
    the conservative 0.85 fallback. A per-user override (librarian_configs.
    semantic_threshold) wins over this default — resolved by the caller.
    """
    if model is None:
        return SEMANTIC_DUPLICATE_COSINE
    return SEMANTIC_DUPLICATE_THRESHOLDS.get(model, SEMANTIC_DUPLICATE_COSINE)


# Row-block size for the vectorized similarity matrix: bounds memory to
# BLOCK x group-size float64 dots regardless of library scale.
_MATRIX_BLOCK = 1024


def _similarity_pairs(group: list[dict], threshold: float) -> list[tuple[int, int, float]]:
    """(i, j, cosine) triples for pairs in ``group`` at or above ``threshold``.

    A2: numpy-vectorized matrix cosine when numpy is importable (the Docker
    image and the ``local`` extra ship it) — the pure-Python pairwise zip/sum
    is O(n^2 * dims) and was the curate scaling trap. Without numpy the
    previous pairwise stdlib scan runs instead (minimal pip installs keep the
    old behavior). Both paths are exact over the whole group: no pair at or
    above the threshold is ever dropped, so unaddressed findings cannot be
    re-hidden.
    """
    try:
        import numpy as np
    except ImportError:
        np = None
    if np is None:
        return _similarity_pairs_stdlib(group, threshold)
    vectors = np.asarray([c["vector"] for c in group], dtype=np.float64)
    norms = np.sqrt((vectors * vectors).sum(axis=1))
    n = len(group)
    pairs: list[tuple[int, int, float]] = []
    for start in range(0, n, _MATRIX_BLOCK):
        block = vectors[start : start + _MATRIX_BLOCK]
        dots = block @ vectors.T
        denom = norms[start : start + _MATRIX_BLOCK, None] * norms[None, :]
        with np.errstate(divide="ignore", invalid="ignore"):
            sims = dots / denom  # zero-norm rows/cols yield nan, never >= threshold
        rows, cols = np.nonzero(sims >= threshold)
        for row, col in zip(rows.tolist(), cols.tolist(), strict=True):
            i = start + row
            if col <= i:
                continue  # upper triangle only
            pairs.append((i, col, float(sims[row, col])))
    return pairs


def _similarity_pairs_stdlib(group: list[dict], threshold: float) -> list[tuple[int, int, float]]:
    pairs: list[tuple[int, int, float]] = []
    for i, first in enumerate(group):
        for j in range(i + 1, len(group)):
            similarity = cosine(first["vector"], group[j]["vector"])
            if similarity >= threshold:
                pairs.append((i, j, similarity))
    return pairs


def semantic_duplicate_candidates(
    root: str | Path,
    vectors: dict[str, list[float]],
    *,
    since: str | None = None,
    threshold: float | None = None,
) -> list[dict]:
    """Embedding-similarity duplicate pairs over the cached vector store.

    ``vectors`` maps concept paths (with ``.md``) to stored vectors; a leading
    ``/`` on either side is tolerated. Only concepts present in ``vectors``
    participate (unembedded concepts are invisible to this pass; the reconcile
    keeps coverage complete). Entries are score-only (``ids`` + ``similarity``,
    no ``shared`` key). ``threshold`` overrides the module default; None =
    ``SEMANTIC_DUPLICATE_COSINE``.
    """
    threshold = SEMANTIC_DUPLICATE_COSINE if threshold is None else threshold
    root = Path(root)
    normed = {key.lstrip("/"): vector for key, vector in vectors.items()}
    concepts: list[dict] = []
    for bundle_path, abs_path in links_mod.iter_concept_files(root):
        vector = normed.get(bundle_path.lstrip("/"))
        if vector is None:
            continue
        try:
            fm, _ = fm_mod.split_document(abs_path.read_text(encoding="utf-8"))
        except (fm_mod.FrontmatterError, OSError, UnicodeDecodeError):
            continue
        generated = fm.get("generated")
        generated_at = _raw_ts(generated.get("at")) if isinstance(generated, dict) else None
        changed = since is None or generated_at is None or _norm_ts(generated_at) >= _norm_ts(since)
        concepts.append(
            {
                "id": bundle_path[: -len(".md")],
                "type": fm.get("type"),
                "title": str(fm.get("title") or ""),
                "changed": changed,
                "vector": vector,
            }
        )

    token_sets = {c["id"]: _tokens(c["title"]) for c in concepts}
    # Same-type AND same-dims groups (mixed dims = mixed-model transition
    # state; such pairs are skipped). Grouping by dims up front lets the
    # similarity pass run one matrix per group.
    by_type: dict[tuple[str, int], list[dict]] = {}
    for c in concepts:
        if c["type"]:
            by_type.setdefault((str(c["type"]), len(c["vector"])), []).append(c)
    candidates = []
    for group in by_type.values():
        for i, j, similarity in _similarity_pairs(group, threshold):
            first, second = group[i], group[j]
            if not (first["changed"] or second["changed"]):
                continue
            a, b = token_sets[first["id"]], token_sets[second["id"]]
            if _is_series_pair(a, b):
                continue
            if a and b and _jaccard(a, b) >= NEAR_DUPLICATE_JACCARD:
                continue  # already reported by the structural pass
            candidates.append(
                {
                    "ids": sorted([first["id"], second["id"]]),
                    "similarity": round(similarity, 2),
                }
            )
    candidates.sort(key=lambda item: (-item["similarity"], item["ids"]))
    return candidates[:SEMANTIC_DUPLICATE_MAX_PAIRS]
