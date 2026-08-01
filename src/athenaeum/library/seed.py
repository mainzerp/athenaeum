"""Deterministic per-user library seed (plan section 3.1a).

A compact orientation document for external agents: the directory tree, every
concept with ``type`` + one-line ``description``, and the ~10 most recent
log.md entries. Capped at ~2 KB; oldest content (oldest log entries first,
then trailing concept/tree lines) is truncated and the truncation is noted
inline. Cached on the backend instance as ``(log.md mtime, seed)`` and
revalidated by stat on every call: every compound write appends log.md, so
its mtime is an exact, process-agnostic version counter (plan decision (c)).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from . import frontmatter as fm_mod
from . import log as log_mod
from .links import RESERVED_NAMES, iter_concept_files

if TYPE_CHECKING:
    from .backend import LibraryBackend

MAX_SEED_BYTES = 2048
MAX_LOG_ENTRIES = 10
_TRUNCATION_NOTE = "(truncated to fit 2 KB seed cap)"


def generate_seed(backend: LibraryBackend) -> str:
    """Return the cached seed for ``backend``'s bundle, regenerating after writes."""
    try:
        mtime: int | None = (backend.root / "log.md").stat().st_mtime_ns
    except OSError:
        mtime = None
    cached = backend.seed_cache
    if cached is not None and cached[0] == mtime:
        return cached[1]
    seed = _build_seed(backend.root)
    backend.seed_cache = (mtime, seed)
    return seed


def _build_seed(root: Path) -> str:
    tree_lines = _tree(root)
    concept_lines = _concepts(root)
    log_entries = log_mod.recent_entries(root, MAX_LOG_ENTRIES)
    truncated = False

    def render() -> str:
        parts = ["# Library Seed", "", "## Tree", *tree_lines, "", "## Concepts"]
        parts.extend(concept_lines or ["(none)"])
        parts.append("")
        parts.append("## Recent activity")
        parts.extend(log_entries or ["(none)"])
        if truncated:
            parts.extend(["", _TRUNCATION_NOTE])
        return "\n".join(parts) + "\n"

    text = render()
    for lines in (log_entries, concept_lines, tree_lines):
        while len(text.encode("utf-8")) > MAX_SEED_BYTES and lines:
            lines.pop()  # drop oldest/trailing content first
            truncated = True
            text = render()
    if len(text.encode("utf-8")) > MAX_SEED_BYTES:
        marker = ("\n" + _TRUNCATION_NOTE + "\n").encode("utf-8")
        text = text.encode("utf-8")[: MAX_SEED_BYTES - len(marker)].decode(
            "utf-8", errors="ignore"
        ) + marker.decode("utf-8")
    return text


def _tree(root: Path) -> list[str]:
    lines = ["/"]

    def walk(directory: Path, depth: int) -> None:
        for child in sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
            if child.name.startswith("."):
                continue
            if child.is_dir():
                lines.append("  " * depth + child.name + "/")
                walk(child, depth + 1)
            elif child.suffix == ".md" and child.name not in RESERVED_NAMES:
                lines.append("  " * depth + child.name)

    walk(root, 1)
    return lines


def _concepts(root: Path) -> list[str]:
    out = []
    for bundle_path, abs_path in iter_concept_files(root):
        try:
            fm, _ = fm_mod.split_document(abs_path.read_text(encoding="utf-8"))
        except (fm_mod.FrontmatterError, OSError, UnicodeDecodeError):
            fm = {}
        line = f"- {bundle_path} (type: {fm.get('type', '?')})"
        description = fm.get("description")
        if isinstance(description, str) and description:
            line += f" - {description}"
        out.append(line)
    return out
