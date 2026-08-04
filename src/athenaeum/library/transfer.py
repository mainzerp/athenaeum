"""Whole-bundle export/import: zip build, validated staging extraction, swap.

Export zips the complete bundle (concepts, indexes, log, and the dot-dir
side stores — except ``.git`` and legacy ``.athenaeum/versions/``) under
the per-root write lock so the walk sees a consistent tree. Import
validates the archive (zip-slip, symlink, and ``.git`` members rejected,
root ``index.md``/``log.md`` required), extracts to a staging sibling, then
replaces the live root via rename-swap under the write lock — a
well-behaved writer class beside the librarian (architecture §7), honoring
``write_lock_for``. After the swap, git history is re-initialized with one
commit (archives carry no ``.git``; best-effort, never breaks the import).
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath

from . import gittool
from . import log as log_mod
from .backend import write_lock_for

STAGING_DIRNAME = "library.import-staging"
OLD_DIRNAME = "library.import-old"
BACKUP_ZIP_NAME = "import-backup.zip"
REQUIRED_ROOT_FILES = ("index.md", "log.md")

_DRIVE_RE = re.compile(r"^[a-zA-Z]:")
_EXTRACT_CHUNK = 1024 * 1024


class TransferError(ValueError):
    """Base for export/import failures; the WebUI surfaces these as HTTP 400."""


class CorruptArchiveError(TransferError):
    """The uploaded file is not a readable, unencrypted zip archive."""


class UnsafeMemberError(TransferError):
    """An archive member is unsafe: escapes the bundle root (zip-slip,
    symlink) or carries a ``.git`` path (git-hook injection)."""


class MissingBundleFileError(TransferError):
    """The archive lacks a file every OKF bundle must carry at the root."""


def _rmtree_onexc(func, path, _exc):
    """chmod-retry for read-only files (git objects are mode 0o444 on Windows)."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _rmtree(path: Path, *, ignore_errors: bool = False) -> None:
    shutil.rmtree(path, ignore_errors=ignore_errors, onexc=_rmtree_onexc)


def export_bundle(root: str | Path, dest_zip: str | Path) -> int:
    """Zip the bundle at ``root`` to ``dest_zip``; returns the member count.

    The per-root write lock is held for the whole build: the walk must see
    a consistent tree, and writers already serialize on this RLock so a
    compound write merely waits briefly.
    """
    root = Path(root).resolve()
    if not (root / "index.md").is_file():
        raise TransferError(f"not an initialized library bundle: {root}")
    with write_lock_for(root):
        return _build_zip(root, Path(dest_zip))


def stage_import(root: str | Path, src_zip: str | Path) -> Path:
    """Validate ``src_zip`` and extract it to the staging sibling of ``root``.

    Nothing but the staging directory is written; on any failure the
    staging directory is removed again and the live bundle is untouched.
    Returns the staging path.
    """
    staging = Path(root).parent / STAGING_DIRNAME
    _rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        try:
            zf = zipfile.ZipFile(src_zip)
        except (zipfile.BadZipFile, RuntimeError) as exc:
            # BadZipFile: not a zip; RuntimeError: encrypted members.
            raise CorruptArchiveError(f"unreadable archive: {exc}") from exc
        with zf:
            names = {info.filename.replace("\\", "/") for info in zf.infolist()}
            missing = [name for name in REQUIRED_ROOT_FILES if name not in names]
            if missing:
                raise MissingBundleFileError(
                    "archive is not an Athenaeum library export; missing: " + ", ".join(missing)
                )
            for info in zf.infolist():
                if _is_symlink(info):
                    raise UnsafeMemberError(f"symlink member not allowed: {info.filename!r}")
                # Hook-injection guard: exports never carry .git, so an
                # archive that does is hostile (supply-chain via git hooks).
                if ".git" in _member_relpath(info.filename.rstrip("/")).parts:
                    raise UnsafeMemberError(".git members are not allowed")
                if info.is_dir():
                    # Explicit dir entries (empty dirs) round-trip; files
                    # create their parents on demand below.
                    rel = _member_relpath(info.filename.rstrip("/"))
                    staging.joinpath(*rel.parts).mkdir(parents=True, exist_ok=True)
                    continue
                rel = _member_relpath(info.filename)
                target = staging.joinpath(*rel.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with zf.open(info) as source, open(target, "wb") as dest:
                        shutil.copyfileobj(source, dest, _EXTRACT_CHUNK)
                except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
                    raise CorruptArchiveError(
                        f"unreadable archive member {info.filename!r}: {exc}"
                    ) from exc
    except TransferError:
        _rmtree(staging, ignore_errors=True)
        raise
    except Exception as exc:
        _rmtree(staging, ignore_errors=True)
        raise CorruptArchiveError(f"unreadable archive: {exc}") from exc
    return staging


def replace_staged_bundle(root: str | Path, staging: str | Path, backup_zip: str | Path) -> None:
    """Replace the live bundle with the staged one (rename-swap).

    Writes a one-generation backup zip of the CURRENT bundle BEFORE any
    destructive step (atomic overwrite via ``<backup>.tmp`` +
    ``os.replace``), then renames the root aside and the staging dir in —
    all under the per-root write lock. A failure before the first rename
    leaves the bundle byte-identical; a failure mid-swap triggers a
    best-effort rollback and raises ``TransferError`` naming the backup.
    """
    root = Path(root).resolve()
    staging = Path(staging)
    backup_zip = Path(backup_zip)
    old = root.parent / OLD_DIRNAME
    with write_lock_for(root):
        tmp_backup = backup_zip.with_name(backup_zip.name + ".tmp")
        try:
            _build_zip(root, tmp_backup)
            os.replace(tmp_backup, backup_zip)
        except Exception:
            _rmtree(staging, ignore_errors=True)
            raise
        renamed = False
        try:
            if old.exists():
                _rmtree(old)
            os.rename(root, old)
            renamed = True
            os.rename(staging, root)
            _rmtree(old)
        except Exception as exc:
            if renamed and not root.exists() and old.exists():
                os.rename(old, root)  # best-effort rollback
            _rmtree(staging, ignore_errors=True)  # failed mid-swap
            raise TransferError(
                f"Import failed during restore; the previous library is backed up at {backup_zip}"
            ) from exc
        log_mod.append_entry(root, "Update", "Library restored from uploaded archive.")
        # Archives carry no .git (export excludes it, import rejects it), so
        # the restored tree gets a fresh repo with one commit. Best-effort:
        # ensure()/commit_all() never raise — a missing git binary must not
        # fail the import after the swap already happened.
        repo = gittool.GitRepo(root)
        repo.ensure()
        repo.commit_all("Update: Library restored from uploaded archive.")


def import_bundle(root: str | Path, src_zip: str | Path, backup_zip: str | Path) -> None:
    """Convenience wrapper: stage_import + replace_staged_bundle.

    Used by storage tests; the route calls the two phases separately so
    validation/extraction happen before the run gates are taken.
    """
    staging = stage_import(root, src_zip)
    replace_staged_bundle(root, staging, backup_zip)


def _export_skip(rel: Path) -> bool:
    """True for bundle paths excluded from archives (decision: content-only).

    ``.git`` (history stays on the host; import re-initializes it) and the
    legacy ``.athenaeum/versions/`` snapshot store (inert since 0.22.0).
    """
    return ".git" in rel.parts or rel.parts[:2] == (".athenaeum", "versions")


def _build_zip(root: Path, dest_zip: Path) -> int:
    """Zip the whole bundle (dot-dirs included except ``.git`` and the
    legacy ``.athenaeum/versions/`` store) deterministically.

    Entries are sorted by POSIX relpath; directories get explicit entries
    (name + ``/``) so empty dirs (e.g. an empty ``.athenaeum/payloads/``)
    round-trip. Returns the member count.
    """
    entries = sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix())
    count = 0
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for entry in entries:
            rel = entry.relative_to(root)
            if _export_skip(rel):
                continue
            arcname = rel.as_posix()
            if entry.is_dir():
                zf.writestr(arcname + "/", "")
            else:
                zf.write(entry, arcname)
            count += 1
    return count


def _member_relpath(name: str) -> PurePosixPath:
    """Normalize one archive member name to a safe bundle-relative path.

    Mirrors ``resolve_under`` semantics with the additional absolute-path
    rejection (zip members are never OKF-bundle-absolute): rejects empty
    names, NUL, leading ``/``, drive letters, UNC ``//``, and ``..`` parts.
    """
    normalized = name.replace("\\", "/")
    if (
        not normalized
        or "\x00" in normalized
        or normalized.startswith("/")
        or _DRIVE_RE.match(normalized)
        or any(part == ".." for part in PurePosixPath(normalized).parts)
    ):
        raise UnsafeMemberError(f"unsafe archive member: {name!r}")
    return PurePosixPath(normalized)


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    """True for members created with the Unix symlink file type (S_IFLNK)."""
    return (info.external_attr >> 16) & 0o170000 == 0o120000
