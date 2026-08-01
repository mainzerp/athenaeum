"""Deterministic index.md generator — a pure function of the directory tree.

Structure (OKF spec section 8): ``# Section`` headings grouping
``* [Title](relative-url) - description`` entries. Concepts of the directory
are listed under ``# Documents`` (alphabetical by filename, description-backed
from frontmatter); each subdirectory gets its own section with the ``subdir/``
entry form. Only the bundle-root index carries frontmatter: ``okf_version``.
The generator doubles as a repair tool: any drift is fixed by regenerating.
"""

from __future__ import annotations

from pathlib import Path

from ..isolation import resolve_under
from . import frontmatter as fm_mod
from .links import RESERVED_NAMES

ROOT_OKF_VERSION = "0.2"


def generate_index(root: str | Path, directory: str = "/") -> str:
    """Return the deterministic index.md content for ``directory``."""
    root = Path(root)
    dir_path = resolve_under(root, directory)
    concepts: list[str] = []
    subdirs: list[Path] = []
    if dir_path.is_dir():
        for child in sorted(dir_path.iterdir(), key=lambda p: p.name):
            if child.name.startswith("."):
                continue
            if child.is_dir():
                subdirs.append(child)
            elif child.suffix == ".md" and child.name not in RESERVED_NAMES:
                concepts.append(_entry(child))
    sections: list[str] = []
    if concepts or not subdirs:
        section = "# Documents"
        if concepts:
            section += "\n" + "\n".join(concepts)
        sections.append(section)
    for sub in subdirs:
        sections.append(f"# {sub.name}\n* [{sub.name}]({sub.name}/)")
    body = "\n\n".join(sections) + "\n"
    if dir_path == root:
        return fm_mod.dump_document({"okf_version": ROOT_OKF_VERSION}, body)
    return body


def _entry(path: Path) -> str:
    title = path.stem
    description = None
    try:
        fm, _body = fm_mod.split_document(path.read_text(encoding="utf-8"))
    except (fm_mod.FrontmatterError, OSError, UnicodeDecodeError):
        fm = {}
    if isinstance(fm.get("title"), str) and fm["title"]:
        title = fm["title"]
    if isinstance(fm.get("description"), str) and fm["description"]:
        description = fm["description"]
    line = f"* [{title}]({path.name})"
    if description:
        line += f" - {description}"
    return line
