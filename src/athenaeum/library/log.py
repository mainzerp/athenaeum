"""Root log.md append: flat, date-grouped (``## YYYY-MM-DD``), chronological.

OKF spec section 9: date headings MUST be ISO ``YYYY-MM-DD``; the leading bold
kind word (``**Update**`` etc.) is convention. Phase-1 simplification: a
single root log.md serves as the global history. Entries carry an optional
``(requested by agent:<label>)`` suffix for per-agent attribution.

Appends are O(1) (A12/CS-6): the newest entries live at the END of the file,
so an append writes only the new lines instead of reading and rewriting the
whole history on every compound write. Legacy log.md files were newest-first;
the first append to such a file flips it once (detected via descending date
groups, provable with >= 2 groups — a legacy file whose entries all share one
date group keeps its within-group order; undetectable, cosmetic only).
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .frontmatter import write_text_atomic

LOG_TITLE = "# Directory Update Log"
KINDS = frozenset(
    {"Initialization", "Creation", "Update", "Deprecation", "Move", "Deletion", "Verification"}
)

_HEADING_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})\s*$", re.M)
_HEAD_WINDOW = 4096  # the title + first heading always fit well within this
_TAIL_WINDOW = 65536  # same-day detection scans only this tail slice


def append_entry(
    root: str | Path,
    kind: str,
    text: str,
    *,
    agent_label: str | None = None,
    today: date | None = None,
) -> None:
    """Append ``* **{kind}**: {text}`` under today's heading (created if absent)."""
    if kind not in KINDS:
        raise ValueError(f"unknown log entry kind: {kind!r}")
    day = (today or date.today()).isoformat()
    # Single-line sink (mirrors gittool._sanitize): a multi-line text or
    # label must never forge a fake ``## YYYY-MM-DD`` heading or split one
    # entry across lines.
    text = " ".join(str(text).split())
    if agent_label:
        agent_label = " ".join(str(agent_label).split())
        text = f"{text} (requested by agent:{agent_label})"
    entry = f"* **{kind}**: {text}"
    path = Path(root) / "log.md"
    if not path.exists():
        write_text_atomic(path, f"{LOG_TITLE}\n\n## {day}\n{entry}\n")
        return
    _migrate_legacy(path)
    with open(path, "a+b") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        if size == 0:
            handle.write(f"{LOG_TITLE}\n\n## {day}\n{entry}\n".encode())
            return
        tail = _read_tail(handle, size)
        last_heading = None
        for line in tail.split("\n"):
            if line.startswith("## "):
                last_heading = line.rstrip()
        out = "" if tail.endswith("\n") else "\n"
        if last_heading == f"## {day}":
            out += f"{entry}\n"
        else:
            out += f"\n## {day}\n{entry}\n"
        handle.seek(0, 2)
        handle.write(out.encode())


def recent_entries(root: str | Path, limit: int = 10) -> list[str]:
    """Return the newest ``limit`` entry lines from the root log.md, newest first."""
    path = Path(root) / "log.md"
    if not path.exists():
        return []
    entries = [
        line for line in path.read_text(encoding="utf-8").split("\n") if line.startswith("* ")
    ]
    first, last = _heading_days(path)
    if first is not None and last is not None and first > last:
        # unmigrated legacy file: entries are already newest-first on disk
        return entries[:limit]
    return entries[::-1][:limit]


def _read_tail(handle, size: int) -> str:
    """Decode the last ``_TAIL_WINDOW`` bytes (whole file if it lacks a heading)."""
    handle.seek(max(0, size - _TAIL_WINDOW))
    tail = handle.read().decode("utf-8", errors="ignore")
    if size > _TAIL_WINDOW and "## " not in tail:
        handle.seek(0)
        tail = handle.read().decode("utf-8", errors="ignore")
    return tail


def _heading_days(path: Path) -> tuple[str | None, str | None]:
    """First and last ``## YYYY-MM-DD`` heading dates via bounded head/tail reads."""
    size = path.stat().st_size
    with open(path, "rb") as handle:
        head = handle.read(_HEAD_WINDOW).decode("utf-8", errors="ignore")
        tail = _read_tail(handle, size) if size else ""
    first = _HEADING_RE.search(head)
    last = None
    for match in _HEADING_RE.finditer(tail):
        last = match
    return (first.group(1) if first else None, last.group(1) if last else None)


def _migrate_legacy(path: Path) -> None:
    """Flip a legacy newest-first log.md to chronological order (one-time)."""
    first, last = _heading_days(path)
    if first is not None and last is not None and first > last:
        write_text_atomic(path, _flip(path.read_text(encoding="utf-8")))


def _flip(content: str) -> str:
    """Reverse date-group order and within-group entry order (legacy -> chrono)."""
    title_lines: list[str] = []
    groups: list[list[str]] = []  # each: [heading, *entries]
    for line in content.split("\n"):
        if _HEADING_RE.match(line):
            groups.append([line])
        elif groups:
            if line.startswith("* "):
                groups[-1].append(line)
        else:
            title_lines.append(line)
    title = "\n".join(title_lines).strip() or LOG_TITLE
    out = [title, ""]
    for group in reversed(groups):
        out.append(group[0])
        out.extend(reversed(group[1:]))
        out.append("")
    return "\n".join(out)
