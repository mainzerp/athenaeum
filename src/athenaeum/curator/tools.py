"""Curator tool surface (D4): the full librarian toolset minus run_computation.

Kept (10): list_dir, read_document, search_metadata, search_semantic,
write_concept, edit_concept, move_concept, deprecate_concept,
delete_concept, link_check. Dropped: run_computation (a librarian-retrieval
tool; nothing in the maintain/curate flows uses it). ``write_concept`` is
KEPT: the "create no new concepts during curation" rule stays prompt-level
in CURATE_TASK_TEMPLATE. ``tools.dispatch`` needs no change: a hallucinated
run_computation call errors back to the model as "Unknown tool".
"""

from __future__ import annotations

from athenaeum.librarian.tools import TOOL_SCHEMAS

CURATOR_TOOL_SCHEMAS: list[dict] = [t for t in TOOL_SCHEMAS if t["name"] != "run_computation"]
