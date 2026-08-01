"""Deterministic organization findings: structural lints over a bundle root.

Mirrors ``validate.py``'s shape (pure functions, no backend state, no LLM).
Consumed by ``Librarian.handle_curate`` the same way ``status()`` consumes
``validate()``. Kept out of the validator on purpose: ``healthy`` drives the
maintain no-op and is contract-pinned; organization smells are lints, not
OKF conformance issues.

Changed-set scoping (``since``): a concept counts as *changed* when
``generated.at >= since``. The comparison is lexicographic over ISO strings —
same-offset ISO strings order chronologically when compared as text, and
date-only OKF values are padded to start-of-day. A same-second boundary can
shift by one case between seconds- and microseconds-precision timestamps
(harmless for a scoping filter); foreign non-UTC offsets could misorder.
Both caveats are accepted for 0.5.0.

Global-structural findings (type-named folders, oversized folders) describe
folders, not concepts, and are always computed over the whole tree.
Per-concept findings (thin concepts, near-duplicate candidates) are filtered
by the changed-set: a thin concept is reported only when changed, a
duplicate pair only when at least one member changed.

The module stays pure functions / no backend state / no LLM. The fifth
finding key, ``semantic_duplicate_candidates``, is an empty placeholder the
caller (``Librarian.handle_curate``) fills from the embedding store.
"""

from __future__ import annotations

import posixpath
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import frontmatter as fm_mod
from . import links as links_mod

OVERSIZED_FOLDER_THRESHOLD = 12
THIN_CONCEPT_BODY_CHARS = 200
NEAR_DUPLICATE_JACCARD = 0.6
NEAR_DUPLICATE_MAX_PAIRS = 20
GENERIC_TYPE_WORDS = frozenset(
    {
        "note",
        "notes",
        "project",
        "projects",
        "version",
        "versions",
        "server",
        "servers",
        "document",
        "documents",
        "page",
        "pages",
        "concept",
        "concepts",
    }
)

_STOPWORDS = frozenset({"the", "a", "an", "of", "and", "for", "to", "in", "on"})
_TOKEN_RE = re.compile(r"[^a-z0-9]+")
_NUMERIC_RE = re.compile(r"\d+[a-z]*")

FINDING_KEYS = (
    "type_named_folders",
    "oversized_folders",
    "thin_concepts",
    "near_duplicate_candidates",
    "semantic_duplicate_candidates",
)


def findings_empty(report: dict) -> bool:
    """True when all five finding lists are empty (the no-op predicate)."""
    return all(not report[key] for key in FINDING_KEYS)


def _norm_ts(ts: str) -> str:
    """Pad date-only OKF values to start-of-day for lexicographic compare."""
    return ts if len(ts) > 10 else ts + "T00:00:00+00:00"


def _raw_ts(value: Any) -> str | None:
    """Raw ``generated.at`` as an ISO string (YAML may hand back date objects)."""
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _tokens(title: str) -> set[str]:
    return {t for t in _TOKEN_RE.split(title.lower()) if t and t not in _STOPWORDS}


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b)


def _is_series_pair(a: set[str], b: set[str]) -> bool:
    """True when the only title-token difference is a number-style token.

    Number-style tokens are digits with an optional letter suffix (``1``,
    ``4c``, ``10``) — pure numbers and version/phase identifiers alike.
    """
    strip_a = {t for t in a if not _NUMERIC_RE.fullmatch(t)}
    strip_b = {t for t in b if not _NUMERIC_RE.fullmatch(t)}
    return a != b and strip_a == strip_b


def organization_findings(root: str | Path, *, since: str | None = None) -> dict:
    """Scan the bundle under ``root`` and return the organization report."""
    root = Path(root)
    concepts: list[dict] = []
    for bundle_path, abs_path in links_mod.iter_concept_files(root):
        try:
            fm, body = fm_mod.split_document(abs_path.read_text(encoding="utf-8"))
        except (fm_mod.FrontmatterError, OSError, UnicodeDecodeError):
            continue
        generated = fm.get("generated")
        generated_at = _raw_ts(generated.get("at")) if isinstance(generated, dict) else None
        concept_id = bundle_path[: -len(".md")]
        parts = [p for p in concept_id.split("/") if p]
        parent = posixpath.dirname(concept_id)
        changed = since is None or generated_at is None or _norm_ts(generated_at) >= _norm_ts(since)
        concepts.append(
            {
                "id": concept_id,
                "type": fm.get("type"),
                "title": str(fm.get("title") or ""),
                "status": fm.get("status"),
                "body_chars": len(body.strip()),
                "parent": parent,
                "depth1": parts[0] if len(parts) > 1 else None,
                "changed": changed,
            }
        )

    # 1. type-named folders (top-level only; nested versions/ is allowed)
    types_in_use = {str(c["type"]).lower() for c in concepts if c["type"]}
    subtree_counts: dict[str, int] = {}
    for c in concepts:
        if c["depth1"] is not None:
            subtree_counts[c["depth1"]] = subtree_counts.get(c["depth1"], 0) + 1
    type_named_folders = []
    for folder in sorted(subtree_counts):
        name = folder.lower()
        is_type_named = name in GENERIC_TYPE_WORDS or any(
            name == t or name == t + "s" or name + "s" == t for t in types_in_use
        )
        if is_type_named:
            type_named_folders.append({"path": folder, "concepts": subtree_counts[folder]})

    # 2. oversized folders (direct-child concept count over threshold)
    direct_counts: dict[str, int] = {}
    for c in concepts:
        direct_counts[c["parent"]] = direct_counts.get(c["parent"], 0) + 1
    oversized_folders = [
        {
            "path": parent.lstrip("/") or "/",
            "concepts": count,
            "threshold": OVERSIZED_FOLDER_THRESHOLD,
        }
        for parent, count in sorted(direct_counts.items())
        if count > OVERSIZED_FOLDER_THRESHOLD
    ]

    # 3. thin concepts (changed-set only; deprecated excluded)
    thin_concepts = [
        {"id": c["id"], "title": c["title"], "body_chars": c["body_chars"]}
        for c in sorted(concepts, key=lambda c: c["id"])
        if c["changed"]
        and c["body_chars"] < THIN_CONCEPT_BODY_CHARS
        and c["status"] != "deprecated"
    ]

    # 4. near-duplicate candidates (same type; numbered title series excluded;
    #    pair reported when a member changed)
    token_sets = {c["id"]: _tokens(c["title"]) for c in concepts}
    by_type: dict[str, list[dict]] = {}
    for c in concepts:
        if c["type"]:
            by_type.setdefault(str(c["type"]), []).append(c)
    near_duplicate_candidates = []
    for group in by_type.values():
        # A2: candidate generation instead of the all-pairs loop. Both
        # filters are exact (no true pair is ever dropped): a Jaccard >= t
        # pair must share at least one token (inverted index), and
        # Jaccard <= min(|a|,|b|)/max(|a|,|b|) (size bound). Only surviving
        # candidate pairs pay the set intersection.
        sizes = [len(token_sets[c["id"]]) for c in group]
        inverted: dict[str, list[int]] = {}
        for idx, c in enumerate(group):
            for token in token_sets[c["id"]]:
                inverted.setdefault(token, []).append(idx)
        candidate_pairs: set[tuple[int, int]] = set()
        for members in inverted.values():
            for x in range(len(members)):
                for y in range(x + 1, len(members)):
                    i, j = members[x], members[y]
                    small, large = sorted((sizes[i], sizes[j]))
                    if small < NEAR_DUPLICATE_JACCARD * large:
                        continue  # size bound: cannot reach the threshold
                    candidate_pairs.add((i, j))
        for i, j in candidate_pairs:
            first, second = group[i], group[j]
            if not (first["changed"] or second["changed"]):
                continue
            a, b = token_sets[first["id"]], token_sets[second["id"]]
            if _is_series_pair(a, b):
                continue
            similarity = _jaccard(a, b)
            if similarity >= NEAR_DUPLICATE_JACCARD:
                near_duplicate_candidates.append(
                    {
                        "ids": sorted([first["id"], second["id"]]),
                        "similarity": round(similarity, 2),
                        "shared": sorted(a & b),
                    }
                )
    near_duplicate_candidates.sort(key=lambda item: (-item["similarity"], item["ids"]))
    near_duplicate_candidates = near_duplicate_candidates[:NEAR_DUPLICATE_MAX_PAIRS]

    return {
        "type_named_folders": type_named_folders,
        "oversized_folders": oversized_folders,
        "thin_concepts": thin_concepts,
        "near_duplicate_candidates": near_duplicate_candidates,
        # Populated by the caller (handle_curate); always empty here.
        "semantic_duplicate_candidates": [],
        "concepts_scanned": len(concepts),
        "since": since,
    }
