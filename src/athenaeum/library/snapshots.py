"""Shadow-copy snapshot versioning under ``.athenaeum/versions/<NNNNNN>/``.

Before every mutating operation the backend snapshots the pre-image of every
file the operation touches (concept files, affected index.md files, log.md)
plus a ``meta.json`` (timestamp, actor, operation, affected paths). Rollback
copies pre-images back (and removes files that did not exist yet); diffs are
computed on demand with ``difflib``. Numbering is monotonically increasing;
retention is keep-all unless ``keep`` > 0 (``prune`` hook, plan Decision 6).
"""

from __future__ import annotations

import difflib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from ..isolation import resolve_under

STORE_DIR = ".athenaeum/versions"


class VersionStore:
    """Per-operation pre-image snapshots for one library root."""

    def __init__(self, root: str | Path, keep: int = 0) -> None:
        self.root = Path(root)
        self.store = self.root / STORE_DIR
        self.keep = keep
        # Next snapshot number, claimed lazily on the first create and bumped
        # per success instead of re-probing the store directory per write
        # (A12); the mkdir collision retry below still guards cross-instance
        # races on the same root.
        self._next: int | None = None

    def create(self, operation: str, paths: list[str], actor: str) -> int:
        """Snapshot the pre-image of ``paths`` (bundle-relative). Returns n."""
        if self._next is None:
            self._next = self._next_n()
        for attempt in range(100):
            n = self._next + attempt
            snap = self.store / f"{n:06d}"
            try:
                snap.mkdir(parents=True)
                break
            except FileExistsError:
                continue
        else:
            raise RuntimeError("could not claim a snapshot directory after 100 attempts")
        self._next = n + 1
        normalized = [_normalize(p) for p in dict.fromkeys(paths)]
        for rel in normalized:
            src = self.root / rel
            if src.is_file():
                dst = snap / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        meta = {
            "n": n,
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
            "actor": actor,
            "operation": operation,
            "paths": normalized,
        }
        (snap / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        if self.keep > 0:
            self.prune(self.keep)
        return n

    def list(self) -> list[dict]:
        """Return snapshot metadata, newest first."""
        out = []
        if self.store.is_dir():
            for meta_path in sorted(self.store.glob("*/meta.json"), reverse=True):
                try:
                    out.append(json.loads(meta_path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    continue
        return sorted(out, key=lambda m: m["n"], reverse=True)

    def diff(self, n: int, path: str) -> str:
        """Unified diff between snapshot ``n``'s pre-image and the current file.

        ``path`` is caller-supplied; it is resolved under the library root
        (``PathEscapeError`` on traversal/absolute/escape) before any read.
        """
        rel = _resolve_rel(self.root, path)
        snap_file = self.store / f"{n:06d}" / rel
        old = (
            snap_file.read_text(encoding="utf-8").splitlines(keepends=True)
            if snap_file.is_file()
            else []
        )
        cur_file = self.root / rel
        new = (
            cur_file.read_text(encoding="utf-8").splitlines(keepends=True)
            if cur_file.is_file()
            else []
        )
        return "".join(difflib.unified_diff(old, new, fromfile=f"a/{rel}", tofile=f"b/{rel}"))

    def rollback(self, n: int) -> None:
        """Restore the LATEST snapshot's pre-images; remove files it created.

        Only the newest snapshot may be rolled back (L12): restoring an older
        one would mix its pre-images with later operations' state (an old
        log.md alongside newer concept changes) — only "undo the most recent
        operation" is state-consistent. Raises ``FileNotFoundError`` for a
        missing snapshot and ``ValueError`` for a non-latest one. Directories
        left empty by removed files are pruned (L13).
        """
        snap = self.store / f"{n:06d}"
        if not snap.is_dir():
            raise FileNotFoundError(f"no such snapshot: {n}")
        latest = self._latest_n()
        if n != latest:
            raise ValueError(f"rollback only supports the latest snapshot ({latest}); got {n}")
        meta = json.loads((snap / "meta.json").read_text(encoding="utf-8"))
        removed: list[Path] = []
        for rel in (_resolve_rel(self.root, p) for p in meta["paths"]):
            src = snap / rel
            dst = self.root / rel
            if src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            elif dst.exists():
                dst.unlink()
                removed.append(dst)
        for dst in removed:
            _prune_empty_dirs(self.root, dst.parent)

    def prune(self, keep_last: int) -> None:
        """Delete all but the newest ``keep_last`` snapshots."""
        if not self.store.is_dir():
            return
        snaps = sorted(
            (d for d in self.store.iterdir() if d.is_dir() and d.name.isdigit()),
            key=lambda d: d.name,
        )
        for old in snaps[: max(0, len(snaps) - keep_last)]:
            shutil.rmtree(old)

    def _next_n(self) -> int:
        if not self.store.is_dir():
            return 1
        nums = [int(d.name) for d in self.store.iterdir() if d.is_dir() and d.name.isdigit()]
        return (max(nums) + 1) if nums else 1

    def _latest_n(self) -> int | None:
        if not self.store.is_dir():
            return None
        nums = [int(d.name) for d in self.store.iterdir() if d.is_dir() and d.name.isdigit()]
        return max(nums) if nums else None


def _prune_empty_dirs(root: Path, start: Path) -> None:
    """Remove ``start`` and its ancestors while completely empty, up to root."""
    current = start
    while current != root and current.is_dir() and not any(current.iterdir()):
        current.rmdir()
        current = current.parent


def _normalize(path: str) -> str:
    return str(path).replace("\\", "/").lstrip("/")


def _resolve_rel(root: Path, path: str) -> str:
    """Normalize ``path`` to a root-relative POSIX form, rejecting escapes.

    Mirrors the strict guard in ``TraceStore.read``: ``..`` segments,
    OS-absolute paths, and post-resolution escapes (incl. symlinks) raise
    ``PathEscapeError`` (a ``ValueError``).
    """
    resolved = resolve_under(root, path)
    return resolved.relative_to(Path(root).resolve()).as_posix()
