"""LLM-facing internal toolset: JSON schemas + dispatch onto LibraryBackend.

Contract: plan section 3.3. Exactly 11 tools, dispatched 1:1 onto the
LibraryBackend (section 3.2) — except ``run_computation``, which routes to
the shared ComputationRunner (read-only, never a write action).
``agent_label`` is injected by the agent loop
from request context, never by the LLM. index.md/log.md maintenance,
init/reconcile/validate, and the git history surface are NOT LLM-callable.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol


class Backend(Protocol):
    """Structural subset of LibraryBackend (plan section 3.2) used by dispatch."""

    def list_dir(self, path: str = "/") -> list[dict]: ...
    def read_document(self, path: str) -> dict: ...
    def search_metadata(self, field: str | None = None, value: str | None = None) -> list[dict]: ...
    async def search_semantic(self, query: str, limit: int = 8) -> list[dict]: ...
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
    ) -> dict: ...
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
    ) -> dict: ...
    def move_concept(
        self, old_path: str, new_path: str, *, agent_label: str | None = None
    ) -> dict: ...
    def deprecate_concept(
        self,
        path: str,
        *,
        agent_label: str | None = None,
        requested_by: str | None = None,
        via: str | None = None,
    ) -> dict: ...
    def delete_concept(self, path: str, *, agent_label: str | None = None) -> dict: ...
    def link_check(self, path: str | None = None) -> list[dict]: ...
    def status(self) -> dict: ...
    # Scoped link-graph check for written concepts (F19/F20); keys are the
    # bundle paths passed in.
    def link_health(self, paths: list[str]) -> dict: ...
    # A10: whole-tree scans also go through the backend (the only OKF
    # boundary); the librarian handlers call these, never library modules.
    def organization_findings(self, *, since: str | None = None) -> dict: ...
    def findings_empty(self, report: dict) -> bool: ...
    def semantic_duplicate_candidates(
        self,
        vectors: dict[str, list[float]],
        *,
        since: str | None = None,
        threshold: float | None = None,
        model: str | None = None,
    ) -> list[dict]: ...


def _params(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


_PATH = {
    "type": "string",
    "description": "Absolute bundle-relative path, e.g. /tables/customers.md",
}

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "list_dir",
        "description": (
            "List a directory of the library, mirroring index.md semantics. "
            "Start at the root ('/') and descend progressively."
        ),
        "parameters": _params({"path": {**_PATH, "default": "/"}}),
    },
    {
        "name": "read_document",
        "description": (
            "Read a concept document (frontmatter + body) or an index.md/log.md "
            "file by bundle-relative path."
        ),
        "parameters": _params({"path": _PATH}, ["path"]),
    },
    {
        "name": "search_metadata",
        "description": (
            "Scan frontmatter of all concepts. Filter by a single field/value "
            "pair (substring, case-insensitive) or list all concepts when both "
            "are omitted."
        ),
        "parameters": _params(
            {
                "field": {
                    "type": "string",
                    "description": "Frontmatter field to match, e.g. title, type, tags",
                },
                "value": {"type": "string", "description": "Value to match"},
            }
        ),
    },
    {
        "name": "search_semantic",
        "description": (
            "Hybrid search over concept titles, descriptions, and bodies — semantic "
            "similarity fused with full-text (BM25) matching via reciprocal rank "
            "fusion, with an optional local rerank pass. Use it to find concepts by "
            "intent, meaning, or exact terms. "
            "Returns ranked hits (score is a relative ranking aid — a reranker or "
            "fused-rank score, not a trust signal). "
            "On embedding-pipeline failure the result degrades to title/description "
            "metadata matches marked fallback: true (empty when nothing matches). "
            "Requires embeddings to be configured; otherwise it "
            "returns an error — use search_metadata then."
        ),
        "parameters": _params(
            {
                "query": {"type": "string", "description": "Natural-language lookup text."},
                "limit": {
                    "type": "integer",
                    "description": "Max hits (default 8).",
                },
            },
            required=["query"],
        ),
    },
    {
        "name": "write_concept",
        "description": (
            "Create a NEW concept document. Fails if the path exists — search "
            "first and prefer edit_concept to enrich an existing concept. "
            "generated:{by,at}, index.md, and log.md are maintained automatically."
        ),
        "parameters": _params(
            {
                "path": _PATH,
                "frontmatter": {
                    "type": "object",
                    "description": (
                        "OKF frontmatter; must include non-empty 'type', should "
                        "include title and description."
                    ),
                },
                "body": {
                    "type": "string",
                    "description": "Markdown body with absolute bundle-relative links.",
                },
                "allow_literal_escapes": {
                    "type": "boolean",
                    "description": (
                        "Confirm intentional literal \\uXXXX escapes inside code "
                        "spans/fenced blocks; suppresses the code-span escape "
                        "warning. Never set it to keep artifact escapes."
                    ),
                },
            },
            ["path", "frontmatter", "body"],
        ),
    },
    {
        "name": "edit_concept",
        "description": (
            "Patch an existing concept: key-level frontmatter patch (unknown "
            "keys preserved), key removal, and/or full body replacement. "
            "Use this to enrich concepts in place and to add back-links."
        ),
        "parameters": _params(
            {
                "path": _PATH,
                "frontmatter_patch": {
                    "type": "object",
                    "description": "Keys merged into the existing frontmatter",
                },
                "remove_keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Frontmatter keys to remove",
                },
                "new_body": {
                    "type": "string",
                    "description": "Replacement markdown body (replaces, not appends)",
                },
                "allow_literal_escapes": {
                    "type": "boolean",
                    "description": (
                        "Confirm intentional literal \\uXXXX escapes inside code "
                        "spans/fenced blocks; suppresses the code-span escape "
                        "warning. Never set it to keep artifact escapes."
                    ),
                },
            },
            ["path"],
        ),
    },
    {
        "name": "move_concept",
        "description": (
            "Move a concept to a new bundle-relative path; inbound absolute "
            "links are rewritten bundle-wide automatically."
        ),
        "parameters": _params(
            {"old_path": _PATH, "new_path": _PATH},
            ["old_path", "new_path"],
        ),
    },
    {
        "name": "deprecate_concept",
        "description": (
            "Mark a concept as status: deprecated. Preferred over delete for "
            "concepts referenced by others."
        ),
        "parameters": _params({"path": _PATH}, ["path"]),
    },
    {
        "name": "delete_concept",
        "description": (
            "Delete a concept file. Returns the inbound links that pointed at "
            "it so they can be repaired."
        ),
        "parameters": _params({"path": _PATH}, ["path"]),
    },
    {
        "name": "link_check",
        "description": (
            "Report broken bundle links (warning-level). Omit path to check the whole library."
        ),
        "parameters": _params({"path": _PATH}),
    },
    {
        "name": "run_computation",
        "description": (
            "Execute an Attested Computation concept's SQL (its body "
            "# Computation fence) read-only against an admin-configured "
            "connection and return the verified receipt (columns, rows, "
            "row_count, truncated). Use it to answer with current verified "
            "figures instead of quoting stored results. Requires the admin "
            "execution toggle; a disabled or unavailable execution is a "
            "coverage gap, not an error to narrate."
        ),
        "parameters": _params(
            {
                "path": _PATH,
                "connection_id": {
                    "type": "string",
                    "description": "Admin-configured runtime connection id.",
                },
                "parameters": {
                    "type": "object",
                    "description": "Values for the concept's declared parameters.",
                },
            },
            ["path", "connection_id"],
        ),
    },
]

_SCHEMA_BY_NAME = {t["name"]: t for t in TOOL_SCHEMAS}

# Write ops, mapped to the store/maintain action vocabulary of plan section 3.1.
# run_computation is deliberately NOT here: it is a READ action — no tracker
# entry, no no-write detection interference, no embedding-sync input.
WRITE_ACTIONS = {
    "write_concept": "created",
    "edit_concept": "updated",
    "move_concept": "updated",
    "deprecate_concept": "deprecated",
    "delete_concept": "deleted",
}


def _require_args(name: str, args: dict[str, Any]) -> None:
    """Reject calls missing schema-required args with a model-recoverable message.

    L6: an explicit JSON ``null`` is not "missing" — the key was supplied,
    just with a value the schema forbids — so it gets its own message.
    """
    schema = next((t for t in TOOL_SCHEMAS if t["name"] == name), None)
    required = (schema or {}).get("parameters", {}).get("required", [])
    missing = [key for key in required if key not in args]
    if missing:
        raise ValueError(f"{name} missing required argument(s): {', '.join(missing)}")
    null = [key for key in required if args[key] is None]
    if null:
        raise ValueError(f"{name} required argument(s) given as null: {', '.join(null)}")


async def dispatch(
    name: str,
    args: dict[str, Any],
    backend: Backend,
    agent_label: str | None = None,
    requested_by: str | None = None,
    via: str | None = None,
    computation_runner=None,
) -> Any:
    """Dispatch one LLM tool call onto the backend (1:1 mapping, plan section 3.3).

    Backend work is synchronous filesystem I/O (compound writes under the
    per-root RLock); every call runs via ``asyncio.to_thread`` so the agent
    loop never blocks the event loop (A1/A3). The lock then serializes
    worker threads, never the loop thread.
    """
    args = args or {}
    _require_args(name, args)
    if name == "list_dir":
        # L6: explicit null on an optional arg coerces to the schema default.
        path = args.get("path")
        return await asyncio.to_thread(backend.list_dir, path=path if path is not None else "/")
    if name == "read_document":
        return await asyncio.to_thread(backend.read_document, args["path"])
    if name == "search_metadata":
        return await asyncio.to_thread(
            backend.search_metadata, field=args.get("field"), value=args.get("value")
        )
    if name == "write_concept":
        return await asyncio.to_thread(
            backend.create_concept,
            args["path"],
            args["frontmatter"],
            args["body"],
            agent_label=agent_label,
            requested_by=requested_by,
            via=via,
            # L6 convention: absent or explicit null coerces to False.
            allow_literal_escapes=bool(args.get("allow_literal_escapes")),
        )
    if name == "edit_concept":
        return await asyncio.to_thread(
            backend.edit_concept,
            args["path"],
            frontmatter_patch=args.get("frontmatter_patch"),
            remove_keys=args.get("remove_keys"),
            new_body=args.get("new_body"),
            agent_label=agent_label,
            requested_by=requested_by,
            via=via,
            allow_literal_escapes=bool(args.get("allow_literal_escapes")),
        )
    if name == "move_concept":
        return await asyncio.to_thread(
            backend.move_concept, args["old_path"], args["new_path"], agent_label=agent_label
        )
    if name == "deprecate_concept":
        return await asyncio.to_thread(
            backend.deprecate_concept,
            args["path"],
            agent_label=agent_label,
            requested_by=requested_by,
            via=via,
        )
    if name == "delete_concept":
        return await asyncio.to_thread(
            backend.delete_concept, args["path"], agent_label=agent_label
        )
    if name == "link_check":
        return await asyncio.to_thread(backend.link_check, path=args.get("path"))
    if name == "search_semantic":
        # L6: `is not None` — an explicit limit of 0 stays 0; missing or
        # explicit null coerces to the schema default (8).
        limit = args.get("limit")
        return await backend.search_semantic(
            query=args["query"], limit=limit if limit is not None else 8
        )
    if name == "run_computation":
        # READ action (never in WRITE_ACTIONS): the receipt dict has no "id"
        # key, so the tracker guard cannot fire either. The backend is the
        # concrete LibraryBackend — the runner only needs read_document.
        if computation_runner is None:
            raise ValueError("computation execution is not available")
        return await asyncio.to_thread(
            computation_runner.run,
            backend,
            args["path"],
            args["connection_id"],
            args.get("parameters"),
        )
    raise ValueError(f"Unknown tool {name!r}")
