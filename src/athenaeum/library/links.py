"""Markdown link extraction, resolution, and bundle-wide rewriting.

Bundle links come in two forms (OKF spec section 6): absolute bundle-relative
(``/tables/customers.md``, recommended) and standard relative (``./other.md``).
Concept-to-concept links are extracted from markdown bodies and from the
path-valued frontmatter fields (``resource``, ``sources[].resource``,
``computation``, ``executor.resource``, ``attester.resource``).
Markdown image syntax (``![alt](src)``) is NOT a concept link: it is excluded
from extraction and rewriting, so image assets never become graph citizens.
F27: links inside inline code spans or fenced code blocks are example markup,
not concept links — extraction and rewriting skip them symmetrically via
``md_spans.iter_code_segments``.
"""

from __future__ import annotations

import os
import posixpath
import re
from collections.abc import Iterator
from pathlib import Path

from ..isolation import PathEscapeError, resolve_under
from . import frontmatter as fm_mod
from .frontmatter import write_text_atomic
from .md_spans import iter_code_segments

LINK_RE = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
RESERVED_NAMES = frozenset({"index.md", "log.md"})


def is_bundle_target(target: str) -> bool:
    """True for in-bundle targets (not URLs, mailto:, or in-page anchors)."""
    return bool(target) and not target.startswith("#") and not _SCHEME_RE.match(target)


def extract_body_links(body: str) -> list[str]:
    """Return raw link targets from markdown ``[text](target)`` syntax.

    F27: links inside inline code spans or fenced code blocks are skipped —
    they are example markup, not concept links (see ``_rewrite_body`` for the
    symmetric write side).
    """
    if not LINK_RE.search(body):
        return []
    targets: list[str] = []
    for is_code, text in iter_code_segments(body):
        if is_code:
            continue
        targets.extend(m.group(2) for m in LINK_RE.finditer(text))
    return targets


def extract_frontmatter_links(fm: dict) -> list[str]:
    """Return path-valued frontmatter field values."""
    targets: list[str] = []
    for key in ("resource", "computation"):
        value = fm.get(key)
        if isinstance(value, str):
            targets.append(value)
    sources = fm.get("sources")
    if isinstance(sources, list):
        for entry in sources:
            if isinstance(entry, dict) and isinstance(entry.get("resource"), str):
                targets.append(entry["resource"])
    for key in ("executor", "attester"):
        nested = fm.get(key)
        if isinstance(nested, dict) and isinstance(nested.get("resource"), str):
            targets.append(nested["resource"])
    return targets


def resolve_target(source_path: str, target: str) -> str:
    """Resolve a link target from ``source_path`` to a bundle-absolute path."""
    target = target.split("#", 1)[0].split("?", 1)[0]
    if target.startswith("/"):
        resolved = posixpath.normpath(target)
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source_path), target))
    if not resolved.startswith("/"):
        resolved = "/" + resolved
    return resolved


def iter_concept_files(root: str | Path) -> Iterator[tuple[str, Path]]:
    """Yield ``(bundle_path, absolute_path)`` for every concept document.

    os.walk with ``followlinks=False`` plus explicit symlink screening:
    symlinked directories are never descended into (on any Python) and
    symlinked files are never yielded — a symlink pointing outside the
    root (or a symlink cycle) can neither escape the bundle nor hang the
    scan. Dot-paths and RESERVED_NAMES are filtered as before; the yield
    order stays globally sorted.
    """
    root = Path(root)
    paths: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            d for d in dirnames if not d.startswith(".") and not (Path(dirpath) / d).is_symlink()
        ]
        for name in filenames:
            if name.startswith(".") or not name.endswith(".md") or name in RESERVED_NAMES:
                continue
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            paths.append(path)
    for path in sorted(paths):
        rel = path.relative_to(root)
        yield "/" + rel.as_posix(), path


def iter_bundle_links(
    root: str | Path, source_path: str | None = None
) -> Iterator[tuple[str, str, str]]:
    """Yield ``(source, raw_target, resolved_target)`` for bundle links in concepts."""
    for bundle_path, abs_path in iter_concept_files(root):
        if source_path is not None and bundle_path != source_path:
            continue
        try:
            fm, body = fm_mod.split_document(abs_path.read_text(encoding="utf-8"))
        except (fm_mod.FrontmatterError, OSError, UnicodeDecodeError):
            continue
        for target in extract_body_links(body) + extract_frontmatter_links(fm):
            if is_bundle_target(target):
                yield bundle_path, target, resolve_target(bundle_path, target)


def broken_links(root: str | Path, source_path: str | None = None) -> list[dict]:
    """Return ``[{"source", "target"}]`` for bundle links with missing targets."""
    root = Path(root)
    broken = []
    for source, target, resolved in iter_bundle_links(root, source_path):
        try:
            exists = resolve_under(root, resolved).exists()
        except PathEscapeError:
            exists = False  # escapes the root (e.g. symlink): broken by definition
        if not exists:
            broken.append({"source": source, "target": target})
    return broken


def inbound_links(root: str | Path, target_path: str) -> list[str]:
    """Return bundle paths of concepts linking to ``target_path``."""
    target = target_path if target_path.startswith("/") else "/" + target_path
    return [source for source, _raw, resolved in iter_bundle_links(root) if resolved == target]


def link_graph(root: str | Path) -> dict[str, set[str]]:
    """Return ``{concept_path: {resolved outbound targets}}`` for all concepts."""
    graph: dict[str, set[str]] = {p: set() for p, _ in iter_concept_files(root)}
    for source, _raw, resolved in iter_bundle_links(root):
        graph.setdefault(source, set()).add(resolved)
    return graph


def rewrite_links(root: str | Path, old: str, new: str) -> int:
    """Rewrite absolute bundle links from ``old`` to ``new``. Returns count.

    Only absolute bundle-relative links are rewritten; relative links are left
    untouched by design (the librarian emits absolute links by default).
    Targets are matched on their path portion only (the same ``#``/``?``
    stripping ``resolve_target`` applies), and any anchor/query suffix is
    preserved across the rewrite (L18).
    """
    root = Path(root)
    old = old if old.startswith("/") else "/" + old
    new = new if new.startswith("/") else "/" + new
    count = 0
    for _bundle_path, abs_path in iter_concept_files(root):
        try:
            fm, body = fm_mod.split_document(abs_path.read_text(encoding="utf-8"))
        except (fm_mod.FrontmatterError, OSError, UnicodeDecodeError):
            continue
        new_body, n = _rewrite_body(body, old, new)
        n += _rewrite_frontmatter(fm, old, new)
        if n:
            write_text_atomic(abs_path, fm_mod.dump_document(fm, new_body))
            count += n
    return count


def _split_anchor(target: str) -> tuple[str, str]:
    """Split ``target`` into ``(path, anchor)`` at the first ``#`` or ``?``."""
    cuts = [i for i in (target.find("#"), target.find("?")) if i != -1]
    if not cuts:
        return target, ""
    cut = min(cuts)
    return target[:cut], target[cut:]


def _rewrite_body(body: str, old: str, new: str) -> tuple[str, int]:
    count = 0

    def repl(match: re.Match) -> str:
        nonlocal count
        path, anchor = _split_anchor(match.group(2))
        if path == old:
            count += 1
            return f"[{match.group(1)}]({new}{anchor})"
        return match.group(0)

    if not LINK_RE.search(body):
        return body, 0
    # F27: symmetric with extract_body_links — links inside inline code spans
    # or fenced code blocks are example markup and pass through byte-untouched.
    parts: list[str] = []
    for is_code, text in iter_code_segments(body):
        parts.append(text if is_code else LINK_RE.sub(repl, text))
    return "".join(parts), count


def _rewrite_frontmatter(fm: dict, old: str, new: str) -> int:
    count = 0

    def swap(holder: dict, key: str) -> None:
        nonlocal count
        value = holder.get(key)
        if isinstance(value, str):
            path, anchor = _split_anchor(value)
            if path == old:
                holder[key] = new + anchor
                count += 1

    for key in ("resource", "computation"):
        swap(fm, key)
    sources = fm.get("sources")
    if isinstance(sources, list):
        for entry in sources:
            if isinstance(entry, dict):
                swap(entry, "resource")
    for key in ("executor", "attester"):
        if isinstance(fm.get(key), dict):
            swap(fm[key], "resource")
    return count
