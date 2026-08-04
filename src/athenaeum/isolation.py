"""Per-user filesystem isolation: resolve bundle-relative paths under a root.

This is the hard multi-user isolation boundary: every filesystem access the
librarian (or any other component) makes against a user's library goes through
``resolve_under`` so that ``..`` traversal, OS-absolute paths, and symlink
escapes are rejected before any file is touched.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

_DRIVE_RE = re.compile(r"^[a-zA-Z]:")


class PathEscapeError(ValueError):
    """Raised when a bundle-relative path would escape its root."""


def validate_user_id(user_id: str) -> None:
    """Reject a ``user_id`` that is unsafe as a single path segment.

    The id becomes ``users/<user_id>/library`` on disk, so empty/blank ids,
    path separators, NUL, and any ``..`` raise ``ValueError``. Deliberately
    NOT a UUID regex: tests and tooling use slugs like ``user-1``; the risk
    closed here is path traversal, not id shape.
    """
    if (
        not user_id
        or not user_id.strip()
        or "/" in user_id
        or "\\" in user_id
        or "\x00" in user_id
        or ".." in user_id
    ):
        raise ValueError(f"invalid user_id: {user_id!r}")


def resolve_under(root: str | Path, rel: str | Path) -> Path:
    """Resolve a bundle-relative path ``rel`` under ``root``.

    Accepts the OKF bundle-absolute form (``/tables/customers.md``, leading
    ``/`` means the bundle root) and the plain relative form. Rejects ``..``
    traversal, OS-absolute paths (drive letters, UNC shares), and
    post-resolution escapes (including symlink escapes, via ``Path.resolve()``
    comparison against the resolved root).
    """
    root_path = Path(root).resolve()
    normalized = str(rel).replace("\\", "/")
    if _DRIVE_RE.match(normalized) or normalized.startswith("//"):
        raise PathEscapeError(f"absolute OS path not allowed: {rel!r}")
    parts = PurePosixPath(normalized.lstrip("/")).parts
    if any(part == ".." for part in parts):
        raise PathEscapeError(f"parent traversal not allowed: {rel!r}")
    candidate = root_path.joinpath(*parts).resolve() if parts else root_path
    if candidate != root_path and root_path not in candidate.parents:
        raise PathEscapeError(f"path escapes root: {rel!r}")
    return candidate


class UserPaths:
    """Minimal per-user root resolution convenience wrapper."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def resolve(self, rel: str | Path) -> Path:
        """Resolve ``rel`` under this user's root; see ``resolve_under``."""
        return resolve_under(self.root, rel)
