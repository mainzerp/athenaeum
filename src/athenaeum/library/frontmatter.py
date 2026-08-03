"""YAML frontmatter split/serialize for OKF concept documents.

Round-trip guarantees (OKF spec section 4.1 extension rule):
- unknown frontmatter keys are preserved (plain dict round-trip, key order
  kept via ``sort_keys=False``),
- the body is preserved byte-for-byte (never parsed or re-emitted),
- a ``verified`` bare mapping is normalized to a one-element list on read
  (spec section 5.2: consumers MUST accept both forms).
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import yaml


class FrontmatterError(ValueError):
    """Raised when a document's frontmatter block is malformed."""


def split_document(text: str, *, normalize_verified: bool = True) -> tuple[dict, str]:
    """Split ``text`` into ``(frontmatter, body)``.

    Returns ``({}, text)`` when the document has no frontmatter block at all.
    Raises ``FrontmatterError`` when a block is opened but not closed, is not
    parseable YAML, or is not a mapping.
    """
    fm_text, body = _split_raw(text)
    if fm_text is None:
        return {}, text
    try:
        data = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        raise FrontmatterError(f"frontmatter is not parseable YAML: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise FrontmatterError("frontmatter must be a YAML mapping")
    if normalize_verified and isinstance(data.get("verified"), dict):
        data["verified"] = [data["verified"]]
    return data, body


def dump_document(frontmatter: dict, body: str) -> str:
    """Serialize ``(frontmatter, body)`` back to document text."""
    if not frontmatter:
        return body
    return (
        "---\n" + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True) + "---\n" + body
    )


def write_text_atomic(path: str | Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically: unique tmp sibling + fsync + rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)  # L13: never leak the tmp sibling
        raise


def write_bytes_atomic(path: str | Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically: unique tmp sibling + fsync + rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{uuid4().hex}.tmp")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)  # L13: never leak the tmp sibling
        raise


def _split_raw(text: str) -> tuple[str | None, str]:
    """Split into ``(frontmatter_text, body)``; frontmatter_text None if absent."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1 :])
    raise FrontmatterError("frontmatter block is not closed by a '---' line")
