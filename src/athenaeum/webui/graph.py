"""Relations graph: /api/graph/universe feeds the Sunburst view.

One flat payload ``{metric, clusters, nodes, edges}``: every concept with
its top-level ``cluster``, a character-count ``size``, the document link
``edges``, and the selected ``?metric=`` (``recency`` or ``link_density``,
default ``link_density``) pre-normalized to a 0..1 ``radius``. Only absolute
bundle-relative links (``/path/to/concept.md``) become edges; broken links
are tolerated (skipped), per OKF §6. The trace replay page renders the same
payload with its hop overlay.
"""

from __future__ import annotations

import math
import re
import sqlite3
from datetime import UTC, date, datetime
from typing import Annotated

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request

from athenaeum.config import Settings
from athenaeum.library import frontmatter as fm_mod
from athenaeum.webui import deps

router = APIRouter()

# Markdown links whose target is an absolute bundle-relative .md path.
_LINK_RE = re.compile(r"\[[^\]]*\]\((/[^)\s]+\.md)\)")

_RESERVED = {"index.md", "log.md"}

# CS-17: bound on folder nesting accepted by the graph walk. A tree deeper
# than this is pathological and rejected with a clear 400 instead of failing
# with RecursionError -> 500.
MAX_WALK_DEPTH = 100


def _walk(backend: object, path: str = "/") -> tuple[list[dict], list[dict]]:
    """Iteratively collect concept and folder entries via the §3.2 list_dir surface.

    Explicit stack instead of recursion (CS-17), bounded by MAX_WALK_DEPTH;
    the stack order mirrors the original depth-first traversal exactly.
    """
    concepts: list[dict] = []
    folders: list[dict] = []
    # Work items: ("walk", path, depth) lists a directory; ("folder", entry)
    # appends the folder row before its subtree is walked.
    stack: list[tuple[str, object, int]] = [("walk", path, 0)]
    while stack:
        action, value, depth = stack.pop()
        if action == "folder":
            folders.append(value)
            continue
        subdirs: list[dict] = []
        for entry in backend.list_dir(value):
            if entry["is_directory"]:
                subdirs.append(entry)
            elif entry["name"].endswith(".md") and entry["name"] not in _RESERVED:
                concepts.append(entry)
        if subdirs and depth + 1 > MAX_WALK_DEPTH:
            raise HTTPException(
                status_code=400,
                detail=f"Library tree exceeds the maximum folder depth ({MAX_WALK_DEPTH})",
            )
        for entry in reversed(subdirs):
            stack.append(("walk", entry["path"], depth + 1))
            stack.append(("folder", entry, depth + 1))
    return concepts, folders


def _folder_of(path: str) -> tuple[str, int]:
    """Parent folder path of a bundle path and its segment depth (0 = root)."""
    folder = path.rsplit("/", 1)[0] or "/"
    depth = len([seg for seg in folder.split("/") if seg])
    return folder, depth


# --- flat universe payload (particle/sunburst views) -----------------------------

# Allowed values for the universe endpoint's ``?metric=`` selector.
UNIVERSE_METRICS = ("recency", "link_density")


def _metric_timestamp(fm: dict) -> float | None:
    """Frontmatter ``generated.at`` as epoch seconds; missing/malformed -> None.

    YAML may hand back date objects, so accept those alongside strings (same
    raw-value idiom as ``library/organize.py``).
    """
    generated = fm.get("generated")
    raw = generated.get("at") if isinstance(generated, dict) else None
    if raw is None:
        return None
    if isinstance(raw, datetime):
        parsed = raw
    elif isinstance(raw, date):
        parsed = datetime(raw.year, raw.month, raw.day)
    else:
        text = str(raw).strip()
        if len(text) <= 10:  # date-only -> start of day
            text += "T00:00:00"
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _metric_value_raw(fm: dict) -> str | None:
    """Raw ``generated.at`` as an ISO string for tooltips (organize.py idiom)."""
    generated = fm.get("generated")
    raw = generated.get("at") if isinstance(generated, dict) else None
    if raw is None:
        return None
    if isinstance(raw, (date, datetime)):
        return raw.isoformat()
    return str(raw)


def build_universe(backend: object, metric: str = "link_density") -> dict:
    """Flat, metric-tagged view of all concepts for the sunburst view.

    Iterative bundle walk via :func:`_walk` (bounded by MAX_WALK_DEPTH).
    ``radius`` is the selected metric min-max-normalized
    to 0..1 (degenerate range -> 0.5 for every node); the raw input rides along
    as ``metric_value`` so the frontend stays metric-agnostic. ``size`` is a
    character count because ``list_dir`` exposes no byte size or mtime.
    ``edges`` carries the document-to-document links (``_LINK_RE``
    extraction, broken targets skipped per OKF §6) so the view can draw the
    link network; every edge endpoint is a node id.

    link_density is sqrt-scaled BEFORE normalization: raw degrees are heavily
    skewed (most nodes near 0, a few hubs far out), so min-max on raw degrees
    would bunch almost every node at radius ~0. sqrt compresses the hub end
    and spreads the low end for a much more even radial fill.
    """
    concepts, _folders = _walk(backend)
    # Read every document up front: broken frontmatter would otherwise kill the
    # whole payload; skip such nodes, mirroring the list_dir tolerance
    # (backend.py). ``known`` counts only readable concepts so every edge
    # endpoint stays a node id.
    kept: list[dict] = []
    docs: list[dict] = []
    for concept in concepts:
        try:
            doc = backend.read_document(concept["path"])
        except (fm_mod.FrontmatterError, ValueError):
            continue
        kept.append(concept)
        docs.append(doc)
    known = {c["path"] for c in kept}
    nodes = []
    edges_out: list[set[str]] = []
    timestamps: list[float | None] = []
    raw_generated: list[str | None] = []
    for concept, doc in zip(kept, docs, strict=True):
        fm = doc.get("frontmatter") or {}
        body = doc.get("body") or ""
        concept_id = concept["path"][: -len(".md")]
        folder, _depth = _folder_of(concept["path"])
        segments = [seg for seg in concept_id.split("/") if seg]
        timestamps.append(_metric_timestamp(fm))
        raw_generated.append(_metric_value_raw(fm))
        edges_out.append({t for t in _LINK_RE.findall(body) if t in known})
        nodes.append(
            {
                "id": concept_id,
                "label": str(fm.get("title") or concept["name"][: -len(".md")]),
                "cluster": segments[0] if len(segments) > 1 else "root",
                "parent_folder": folder,
                "trust_tier": deps.trust_tier(fm),
                "stale": deps.is_stale(fm),
                "size": len(body) + (len(yaml.safe_dump(fm)) if fm else 0),
                "metric_value": None,  # filled in below, per metric
                "radius": 0.0,  # filled in below, normalized 0..1
            }
        )

    # Link density: out-degree from the payload edges, in-degree by
    # inversion (broken targets are skipped, OKF §6).
    in_degree: dict[str, int] = {}
    for targets in edges_out:
        for target in targets:
            in_degree[target] = in_degree.get(target, 0) + 1
    degrees = [
        len(targets) + in_degree.get(concept["path"], 0)
        for targets, concept in zip(edges_out, kept, strict=True)
    ]

    if metric == "recency":
        values: list[float | None] = timestamps
        for node, raw in zip(nodes, raw_generated, strict=True):
            node["metric_value"] = raw
    else:
        # sqrt BEFORE min-max normalization (see docstring): compresses the
        # hub end of the skewed degree distribution for an even radial fill.
        values = [math.sqrt(d) for d in degrees]
        for node, degree in zip(nodes, degrees, strict=True):
            node["metric_value"] = degree

    present = [v for v in values if v is not None]
    vmin = min(present) if present else 0.0
    vmax = max(present) if present else 0.0
    span = vmax - vmin
    for node, value in zip(nodes, values, strict=True):
        if value is None:
            value = vmin  # no timestamp -> treated as oldest
        node["radius"] = 0.5 if span == 0 else (value - vmin) / span

    counts: dict[str, int] = {}
    for node in nodes:
        counts[node["cluster"]] = counts.get(node["cluster"], 0) + 1
    clusters = [{"id": cid, "count": count} for cid, count in sorted(counts.items())]
    # edges_out holds one set of known targets per concept, so (source,
    # target) pairs are already unique; sort for a deterministic payload.
    edges = sorted(
        (
            {"source": concept["path"][: -len(".md")], "target": target[: -len(".md")]}
            for targets, concept in zip(edges_out, kept, strict=True)
            for target in targets
        ),
        key=lambda e: (e["source"], e["target"]),
    )
    return {"metric": metric, "clusters": clusters, "nodes": nodes, "edges": edges}


@router.get("/api/graph/universe")
def graph_universe(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
    metric: str = "link_density",
):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    if metric not in UNIVERSE_METRICS:
        allowed = ", ".join(UNIVERSE_METRICS)
        raise HTTPException(
            status_code=400,
            detail=f"Unknown graph universe metric {metric!r} (allowed: {allowed})",
        )
    backend = deps.get_library_backend(settings, user, conn)
    return build_universe(backend, metric)


@router.get("/library/graph")
def graph_page(request: Request, conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)]):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    return deps.templates.TemplateResponse(request, "graph.html", {"user": user})
