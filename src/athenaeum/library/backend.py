"""LibraryBackend: the OKF boundary — all bundle reads/writes go through here.

All paths are bundle-relative (``/tables/customers.md`` form); resolution
delegates to ``isolation.resolve_under`` (rejects ``..``, OS-absolute paths,
escapes). Concept IDs are paths minus ``.md``. Every mutating method performs
the compound write in fixed crash-safe order: (1) snapshot pre-image, (2)
concept file via write-then-rename, (3) regenerate affected index.md files,
(4) append the root log.md entry. Index.md is a deterministic pure function
of the tree, so ``reconcile()`` can always regenerate it after a crash —
but that is ALL reconcile repairs (L11): a log.md entry lost between index
regeneration and the log append is not recoverable, and a crash
mid-``rewrite_links`` (move) can leave a mixed link state; both are outside
reconcile's scope. Rollback is constrained to the latest snapshot — only
"undo the most recent operation" is state-consistent (L12).
"""

from __future__ import annotations

import logging
import posixpath
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from athenaeum import __version__

from ..isolation import resolve_under
from . import frontmatter as fm_mod
from . import index as index_mod
from . import links as links_mod
from . import log as log_mod
from . import organize as organize_mod
from . import semantic as semantic_mod
from . import snapshots as snapshots_mod
from . import validate as validate_mod
from .frontmatter import write_text_atomic
from .links import RESERVED_NAMES

logger = logging.getLogger(__name__)

# Per-root write locks shared by every LibraryBackend instance in this
# process: several backends can target one library root (WebUI builds fresh
# backends per request), so the compound write serializes on the resolved
# root path. RLock: _regenerate_chain -> regenerate_index nests.
_WRITE_LOCKS: dict[str, threading.RLock] = {}
_WRITE_LOCKS_GUARD = threading.Lock()


def drop_write_lock(root: str | Path) -> None:
    """Forget the write lock for ``root`` (A12: no per-user registry leak).

    Call only after the root's librarian has been evicted (its writes are
    done); dropping the entry while a write holds the lock would let a new
    backend for the same root start a second, unserialized compound write.
    """
    key = str(Path(root).resolve())
    with _WRITE_LOCKS_GUARD:
        _WRITE_LOCKS.pop(key, None)


def provision_library(data_root: str | Path, user_id: str) -> Path:
    """Create the user's library directory and initialize a fresh OKF bundle.

    Filesystem provisioning lives in the library layer (A7): db.py owns the
    user/config rows, this owns the on-disk bundle layout.
    """
    library_root = Path(data_root) / "users" / user_id / "library"
    library_root.mkdir(parents=True, exist_ok=True)
    LibraryBackend(library_root, actor=f"athenaeum-librarian/{__version__}").init_bundle()
    return library_root


class LibraryBackend:
    """Internal Python API over one OKF bundle (one per user)."""

    def __init__(
        self,
        root: str | Path,
        *,
        actor: str,
        versioning: bool = True,
        snapshot_keep: int = 0,
        embedding_service=None,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.actor = actor
        self.versioning = versioning
        self.versions = (
            snapshots_mod.VersionStore(self.root, keep=snapshot_keep) if versioning else None
        )
        # (log.md mtime_ns, seed); None forces regeneration (own writes below).
        self.seed_cache: tuple[int | None, str] | None = None
        self._embedding_service = embedding_service

    # ------------------------------------------------------------------ reads

    def list_dir(self, path: str = "/") -> list[dict]:
        dir_path = self._resolve(path)
        if not dir_path.is_dir():
            raise FileNotFoundError(f"not a directory: {path!r}")
        entries = []
        for child in sorted(dir_path.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
            if child.name.startswith("."):
                continue
            rel = "/" + child.relative_to(self.root).as_posix()
            if child.is_dir():
                entries.append({"name": child.name, "path": rel, "is_directory": True})
            elif child.suffix == ".md" and child.name not in RESERVED_NAMES:
                entry: dict = {"name": child.name, "path": rel, "is_directory": False}
                try:
                    fm, _ = fm_mod.split_document(child.read_text(encoding="utf-8"))
                except (fm_mod.FrontmatterError, OSError, UnicodeDecodeError):
                    fm = {}
                for key in ("title", "type", "description"):
                    if fm.get(key):
                        entry[key] = fm[key]
                entries.append(entry)
        return entries

    def read_document(self, path: str) -> dict:
        abs_path = self._resolve(path)
        if not abs_path.is_file():
            raise FileNotFoundError(f"no such document: {path!r}")
        text = abs_path.read_text(encoding="utf-8")
        bundle = self._bundle_path(path)
        if posixpath.basename(bundle) in RESERVED_NAMES:
            return {"path": bundle, "frontmatter": {}, "body": text}
        fm, body = fm_mod.split_document(text)
        return {"path": bundle, "frontmatter": fm, "body": body}

    def search_metadata(self, field: str | None = None, value: str | None = None) -> list[dict]:
        results = []
        for bundle_path, abs_path in links_mod.iter_concept_files(self.root):
            try:
                fm, _ = fm_mod.split_document(abs_path.read_text(encoding="utf-8"))
            except (fm_mod.FrontmatterError, OSError, UnicodeDecodeError):
                continue
            if field is not None:
                if field not in fm:
                    continue
                if value is not None and value.lower() not in str(fm[field]).lower():
                    continue
            results.append(
                {
                    "id": bundle_path[:-3],
                    "path": bundle_path,
                    "title": fm.get("title"),
                    "type": fm.get("type"),
                    "description": fm.get("description"),
                }
            )
        return results

    async def search_semantic(self, query: str, limit: int = 8) -> list[dict]:
        """Embedding-similarity search; falls back to search_metadata on failure."""
        if self._embedding_service is None:
            raise RuntimeError(
                "semantic search is not configured: set an embedding source in the "
                "WebUI (Agents > Embeddings); use search_metadata instead"
            )
        try:
            ranked = await self._embedding_service.search_ids(query, limit)
        except Exception:
            logger.warning("semantic search failed; falling back to search_metadata", exc_info=True)
            return self._metadata_fallback(query, limit)
        hits = []
        for concept_id, score in ranked:
            try:
                doc = self.read_document(f"{concept_id}.md")
            except Exception:
                continue
            fm = doc.get("frontmatter") or {}
            hits.append(
                {
                    "id": concept_id,
                    "path": f"{concept_id}.md",
                    "title": fm.get("title"),
                    "type": fm.get("type"),
                    "description": fm.get("description"),
                    "score": round(score, 2),
                }
            )
        return hits

    def _metadata_fallback(self, query: str, limit: int) -> list[dict]:
        """Degraded semantic search: substring match on title and description.

        Never the unfiltered library (L7): when nothing matches the query the
        result is empty; hits are flagged ``fallback: true`` so callers can
        tell degraded results from ranked ones.
        """
        seen: dict[str, dict] = {}
        for field in ("title", "description"):
            for hit in self.search_metadata(field=field, value=query):
                seen.setdefault(hit["path"], hit)
        return [{**hit, "fallback": True} for hit in list(seen.values())[:limit]]

    def link_check(self, path: str | None = None) -> list[dict]:
        return links_mod.broken_links(
            self.root, self._bundle_path(path) if path is not None else None
        )

    def link_health(self, paths: list[str]) -> dict:
        """Scoped link check: inbound/outbound counts for the given bundle paths."""
        graph = links_mod.link_graph(self.root)
        inbound = {p: 0 for p in graph}
        for targets in graph.values():
            for target in targets:
                if target in inbound:
                    inbound[target] += 1
        report = {}
        for raw in paths:
            bundle = self._bundle_path(raw)
            report[bundle] = {
                "inbound": inbound.get(bundle, 0),
                "outbound": len(graph.get(bundle, ())),
            }
        return report

    def status(self) -> dict:
        report = self.validate()
        errors = report["errors"]
        warnings = report["warnings"]
        orphans = []
        for w in warnings:
            if w["code"] != "orphan":
                continue
            path = w["path"]
            title = ""
            try:
                orphan_fm, _ = fm_mod.split_document(
                    self._resolve(path).read_text(encoding="utf-8")
                )
                title = orphan_fm.get("title") or ""
            except (fm_mod.FrontmatterError, OSError, UnicodeDecodeError):
                pass
            orphans.append({"id": path[:-3], "title": title})
        broken = [
            {"source": w["path"], "target": w.get("target")}
            for w in warnings
            if w["code"] == "broken-link"
        ]
        concepts = list(links_mod.iter_concept_files(self.root))
        directories = [
            d
            for d in self.root.rglob("*")
            if d.is_dir()
            and not any(part.startswith(".") for part in d.relative_to(self.root).parts)
        ]
        mtimes = [p.stat().st_mtime for _, p in links_mod.iter_concept_files(self.root)]
        last_write = (
            datetime.fromtimestamp(max(mtimes), tz=UTC).isoformat(timespec="seconds")
            if mtimes
            else None
        )
        return {
            "stats": {
                "concepts": len(concepts),
                "directories": len(directories),
                "versions": len(self.list_versions()),
                "last_write": last_write,
            },
            "health": {
                "orphans": orphans,
                "broken_links": broken,
                "warnings": len(warnings),
                "errors": len(errors),
            },
            "healthy": not errors and not orphans and not broken,
        }

    # --------------------------------------------- scans (A10: OKF boundary)

    def iter_concept_files(self) -> Iterator[tuple[str, Path]]:
        """All concept files as (bundle_path, abs_path) pairs (read-only scan)."""
        return links_mod.iter_concept_files(self.root)

    def organization_findings(self, *, since: str | None = None) -> dict:
        """Structural organization findings over this bundle."""
        return organize_mod.organization_findings(self.root, since=since)

    @staticmethod
    def findings_empty(report: dict) -> bool:
        return organize_mod.findings_empty(report)

    def semantic_duplicate_candidates(
        self,
        vectors: dict[str, list[float]],
        *,
        since: str | None = None,
        threshold: float | None = None,
        model: str | None = None,
    ) -> list[dict]:
        """Embedding-similarity duplicate pairs over this bundle.

        ``threshold`` None resolves to the per-model default for ``model``
        (then the module fallback), matching the librarian's resolution order.
        """
        if threshold is None:
            threshold = semantic_mod.semantic_threshold_for_model(model)
        return semantic_mod.semantic_duplicate_candidates(
            self.root, vectors, since=since, threshold=threshold
        )

    # ----------------------------------------------------------------- writes

    def create_concept(
        self, path: str, frontmatter: dict, body: str, *, agent_label: str | None = None
    ) -> dict:
        with self._write_lock():
            abs_path = self._guard_concept_path(path)
            if abs_path.exists():
                raise FileExistsError(f"concept already exists: {path!r}")
            fm = dict(frontmatter)
            self._require_type(fm)
            fm["generated"] = {"by": self.actor, "at": self._now()}
            bundle = self._bundle_path(path)
            self._snapshot("create", [bundle, *self._affected_paths(bundle)])
            write_text_atomic(abs_path, fm_mod.dump_document(fm, body))
            self._regenerate_chain(bundle)
            log_mod.append_entry(
                self.root,
                "Creation",
                f"Created [{fm.get('title') or bundle[:-3]}]({bundle}).",
                agent_label=agent_label,
            )
            self.seed_cache = None
            return {"id": bundle[:-3], "action": "created"}

    def edit_concept(
        self,
        path: str,
        *,
        frontmatter_patch: dict | None = None,
        remove_keys: list[str] | None = None,
        new_body: str | None = None,
        agent_label: str | None = None,
    ) -> dict:
        with self._write_lock():
            abs_path = self._guard_concept_path(path)
            if not abs_path.is_file():
                raise FileNotFoundError(f"no such concept: {path!r}")
            if frontmatter_patch and "verified" in frontmatter_patch:
                raise ValueError("'verified' is never modified by edits")
            if remove_keys and "verified" in remove_keys:
                raise ValueError("'verified' is never modified by edits")
            fm, body = fm_mod.split_document(abs_path.read_text(encoding="utf-8"))
            for key, value in (frontmatter_patch or {}).items():
                fm[key] = value
            for key in remove_keys or []:
                fm.pop(key, None)
            self._require_type(fm)
            fm["generated"] = {"by": self.actor, "at": self._now()}
            bundle = self._bundle_path(path)
            self._snapshot("update", [bundle, *self._affected_paths(bundle)])
            write_text_atomic(
                abs_path, fm_mod.dump_document(fm, new_body if new_body is not None else body)
            )
            self._regenerate_chain(bundle)
            log_mod.append_entry(
                self.root,
                "Update",
                f"Updated [{fm.get('title') or bundle[:-3]}]({bundle}).",
                agent_label=agent_label,
            )
            self.seed_cache = None
            return {"id": bundle[:-3], "action": "updated"}

    def move_concept(self, old_path: str, new_path: str, *, agent_label: str | None = None) -> dict:
        with self._write_lock():
            old_abs = self._guard_concept_path(old_path)
            new_abs = self._guard_concept_path(new_path)
            if not old_abs.is_file():
                raise FileNotFoundError(f"no such concept: {old_path!r}")
            if new_abs.exists():
                raise FileExistsError(f"concept already exists: {new_path!r}")
            old_bundle = self._bundle_path(old_path)
            new_bundle = self._bundle_path(new_path)
            affected = sorted(
                set(self._affected_paths(old_bundle)) | set(self._affected_paths(new_bundle))
            )
            self._snapshot("move", [old_bundle, new_bundle, *affected])
            new_abs.parent.mkdir(parents=True, exist_ok=True)
            old_abs.rename(new_abs)
            rewritten = links_mod.rewrite_links(self.root, old_bundle, new_bundle)
            self._prune_empty_dirs(old_abs.parent)
            dirs = set(self._ancestor_dirs(old_bundle)) | set(self._ancestor_dirs(new_bundle))
            for directory in sorted(dirs):
                if self._resolve(directory).is_dir():
                    self.regenerate_index(directory)
            log_mod.append_entry(
                self.root,
                "Move",
                f"Moved {old_bundle} to [{new_bundle[:-3]}]({new_bundle}).",
                agent_label=agent_label,
            )
            self.seed_cache = None
            return {"id": new_bundle[:-3], "action": "moved", "links_rewritten": rewritten}

    def deprecate_concept(self, path: str, *, agent_label: str | None = None) -> dict:
        with self._write_lock():
            abs_path = self._guard_concept_path(path)
            if not abs_path.is_file():
                raise FileNotFoundError(f"no such concept: {path!r}")
            fm, body = fm_mod.split_document(abs_path.read_text(encoding="utf-8"))
            fm["status"] = "deprecated"
            fm["generated"] = {"by": self.actor, "at": self._now()}
            bundle = self._bundle_path(path)
            self._snapshot("deprecate", [bundle, *self._affected_paths(bundle)])
            write_text_atomic(abs_path, fm_mod.dump_document(fm, body))
            self._regenerate_chain(bundle)
            log_mod.append_entry(
                self.root,
                "Deprecation",
                f"Deprecated [{fm.get('title') or bundle[:-3]}]({bundle}).",
                agent_label=agent_label,
            )
            self.seed_cache = None
            return {"id": bundle[:-3], "action": "deprecated"}

    def delete_concept(self, path: str, *, agent_label: str | None = None) -> dict:
        with self._write_lock():
            abs_path = self._guard_concept_path(path)
            if not abs_path.is_file():
                raise FileNotFoundError(f"no such concept: {path!r}")
            bundle = self._bundle_path(path)
            inbound = links_mod.inbound_links(self.root, bundle)
            self._snapshot("delete", [bundle, *self._affected_paths(bundle)])
            abs_path.unlink()
            self._prune_empty_dirs(abs_path.parent)
            self._regenerate_chain(bundle)
            log_mod.append_entry(
                self.root, "Deletion", f"Deleted {bundle}.", agent_label=agent_label
            )
            self.seed_cache = None
            return {"id": bundle[:-3], "action": "deleted", "inbound_links": inbound}

    # ------------------------------------------------------------ maintenance

    def init_bundle(self) -> None:
        """Create the root index.md (okf_version "0.2") and root log.md."""
        with self._write_lock():
            self.root.mkdir(parents=True, exist_ok=True)
            index_path = self.root / "index.md"
            if not index_path.exists():
                write_text_atomic(index_path, index_mod.generate_index(self.root, "/"))
            if not (self.root / "log.md").exists():
                log_mod.append_entry(self.root, "Initialization", "Initialized the library bundle.")

    def regenerate_index(self, directory: str = "/") -> None:
        with self._write_lock():
            dir_path = self._resolve(directory)
            if not dir_path.is_dir():
                raise FileNotFoundError(f"not a directory: {directory!r}")
            rel = "/" if dir_path == self.root else "/" + dir_path.relative_to(self.root).as_posix()
            write_text_atomic(dir_path / "index.md", index_mod.generate_index(self.root, rel))

    def validate(self, scope: str | None = None) -> dict:
        return validate_mod.validate_bundle(self.root, scope)

    def reconcile(self) -> None:
        """Regenerate every index.md (startup crash-safety pass)."""
        with self._write_lock():
            dirs = [self.root] + [
                d
                for d in self.root.rglob("*")
                if d.is_dir()
                and not any(part.startswith(".") for part in d.relative_to(self.root).parts)
            ]
            for directory in dirs:
                rel = (
                    "/"
                    if directory == self.root
                    else "/" + directory.relative_to(self.root).as_posix()
                )
                self.regenerate_index(rel)

    # ------------------------------------------------------------- versioning

    def list_versions(self) -> list[dict]:
        return self.versions.list() if self.versions is not None else []

    def diff_version(self, n: int, path: str) -> str:
        if self.versions is None:
            raise RuntimeError("versioning is disabled for this backend")
        return self.versions.diff(n, path)

    def rollback(self, n: int) -> None:
        """Undo the most recent operation (``n`` must be the latest snapshot).

        ``ValueError`` for a non-latest snapshot (L12),
        ``FileNotFoundError`` for a missing one.
        """
        if self.versions is None:
            raise RuntimeError("versioning is disabled for this backend")
        with self._write_lock():
            self.versions.rollback(n)
            log_mod.append_entry(self.root, "Update", f"Rolled back to version {n:06d}.")
            self.seed_cache = None

    # --------------------------------------------------------------- internal

    def _write_lock(self) -> threading.RLock:
        """The process-wide write lock for this backend's library root."""
        key = str(self.root)
        with _WRITE_LOCKS_GUARD:
            lock = _WRITE_LOCKS.get(key)
            if lock is None:
                lock = threading.RLock()
                _WRITE_LOCKS[key] = lock
            return lock

    def _resolve(self, path: str) -> Path:
        return resolve_under(self.root, path)

    @staticmethod
    def _bundle_path(path: str) -> str:
        normalized = str(path).replace("\\", "/")
        return normalized if normalized.startswith("/") else "/" + normalized

    def _guard_concept_path(self, path: str) -> Path:
        bundle = self._bundle_path(path)
        name = posixpath.basename(bundle)
        if not name.endswith(".md"):
            raise ValueError(f"concept path must end with .md: {path!r}")
        if name in RESERVED_NAMES:
            raise ValueError(f"reserved filename cannot be a concept: {name!r}")
        if any(part.startswith(".") for part in bundle.split("/") if part):
            raise ValueError(f"hidden path components not allowed: {path!r}")
        return self._resolve(bundle)

    @staticmethod
    def _require_type(fm: dict) -> None:
        type_ = fm.get("type")
        if not isinstance(type_, str) or not type_.strip():
            raise ValueError("frontmatter requires a non-empty 'type'")

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")

    @staticmethod
    def _ancestor_dirs(bundle_path: str) -> list[str]:
        dirs = []
        current = posixpath.dirname(bundle_path)
        while current and current != "/":
            dirs.append(current)
            current = posixpath.dirname(current)
        dirs.append("/")
        return dirs

    def _affected_paths(self, bundle_path: str) -> list[str]:
        paths = [
            "/index.md" if d == "/" else d + "/index.md" for d in self._ancestor_dirs(bundle_path)
        ]
        paths.append("/log.md")
        return paths

    def _regenerate_chain(self, bundle_path: str) -> None:
        for directory in self._ancestor_dirs(bundle_path):
            dir_path = self._resolve(directory)
            if dir_path.is_dir():
                self.regenerate_index(directory)

    def _prune_empty_dirs(self, start: Path) -> None:
        """Remove ancestor dirs left empty (besides their index.md), up to the root."""
        current = start
        while current != self.root and current.is_dir():
            children = list(current.iterdir())
            if any(c.name != "index.md" for c in children):
                break
            for child in children:
                child.unlink()
            current.rmdir()
            current = current.parent

    def _snapshot(self, operation: str, paths: list[str]) -> None:
        if self.versions is not None:
            self.versions.create(operation, paths, self.actor)
