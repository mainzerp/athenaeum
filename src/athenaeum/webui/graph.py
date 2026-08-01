"""Relations graph: /api/graph feeds the 3D universe (vendored 3d-force-graph).

Phase 5: one endpoint returning ``{nodes, folders, edges}`` — folders of
every depth are emitted (depth 1 = star, depth >= 2 = planet), concepts
become moons at any depth. Concept ``color``
comes from trust/staleness, ``group`` from frontmatter ``type``, tooltip
(``title``) from tags; ``trust_tier`` and ``stale`` are emitted for every
node. Only absolute bundle-relative links (``/path/to/concept.md``) become
edges; broken links are tolerated (skipped), per OKF §6. Containment edges
are synthesized client-side from the ``folder``/``parent`` fields, and node
positions are computed client-side (deterministic orbit layout).
"""

from __future__ import annotations

import re
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from athenaeum.config import Settings
from athenaeum.webui import deps

router = APIRouter()

# Markdown links whose target is an absolute bundle-relative .md path.
_LINK_RE = re.compile(r"\[[^\]]*\]\((/[^)\s]+\.md)\)")

# Node colors: staleness overrides trust tier (plan: color <- trust/stale).
COLOR_STALE = "#e67e22"  # orange
COLOR_TRUST = {
    deps.TRUST_HUMAN: "#27ae60",  # green
    deps.TRUST_MACHINE: "#2980b9",  # blue
    deps.TRUST_UNVERIFIED: "#95a5a6",  # grey
}

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


def build_graph(backend: object) -> dict:
    """Scan all concepts and return the 3D-universe ``{nodes, folders, edges}`` data."""
    concepts, folders = _walk(backend)
    known = {c["path"] for c in concepts}
    nodes, edges, seen_edges = [], [], set()
    for concept in concepts:
        doc = backend.read_document(concept["path"])
        fm = doc.get("frontmatter") or {}
        concept_id = concept["path"][: -len(".md")]
        tags = fm.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        stale = deps.is_stale(fm)
        tier = deps.trust_tier(fm)
        color = COLOR_STALE if stale else COLOR_TRUST[tier]
        folder, depth = _folder_of(concept["path"])
        nodes.append(
            {
                "id": concept_id,
                "label": str(fm.get("title") or concept["name"][: -len(".md")]),
                "group": str(fm.get("type") or "unknown"),
                "color": color,
                "title": ", ".join(str(t) for t in tags),  # tooltip <- tags
                "folder": folder,
                "depth": depth,
                "kind": "moon",
                "trust_tier": tier,
                "stale": stale,
            }
        )
        for target in set(_LINK_RE.findall(doc.get("body") or "")):
            if target not in known:
                continue  # consumers must tolerate broken links (OKF §6)
            edge_key = (concept["path"], target)
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            edges.append({"from": concept_id, "to": target[: -len(".md")]})
    folder_nodes = []
    for entry in folders:
        depth = len([seg for seg in entry["path"].split("/") if seg])
        parent, _ = _folder_of(entry["path"])
        folder_nodes.append(
            {
                "id": entry["path"],
                "name": entry["name"],
                "parent": parent,
                "depth": depth,
                "kind": "star" if depth == 1 else "planet",
            }
        )
    return {"nodes": nodes, "folders": folder_nodes, "edges": edges}


@router.get("/api/graph")
def graph_data(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)],
    settings: Annotated[Settings, Depends(deps.settings_dep)],
):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    backend = deps.get_library_backend(settings, user, conn)
    return build_graph(backend)


@router.get("/library/graph")
def graph_page(request: Request, conn: Annotated[sqlite3.Connection, Depends(deps.db_dep)]):
    user = deps.current_user(request, conn)
    if user is None:
        return deps.login_redirect(conn)
    return deps.templates.TemplateResponse(request, "graph.html", {"user": user})
