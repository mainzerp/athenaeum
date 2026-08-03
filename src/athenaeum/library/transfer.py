"""Whole-bundle export/import: zip build, validated staging extraction, swap.

Export zips the complete bundle (concepts, indexes, log, and the dot-dir
side stores) under the per-root write lock so the walk sees a consistent
tree. Import validates the archive (zip-slip and symlink members rejected,
root ``index.md``/``log.md`` required), extracts to a staging sibling, then
replaces the live root via rename-swap under the write lock — a
well-behaved writer class beside the librarian (architecture §7), honoring
``write_lock_for``.
"""

from __future__ import annotations

import os
import re
import shutil
import zipfile
from pathlib import Path, PurePosixPath

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
    """An archive member would escape the bundle root (zip-slip, symlink)."""


class MissingBundleFileError(TransferError):
    """The archive lacks a file every OKF bundle must carry at the root."""


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
    shutil.rmtree(staging, ignore_errors=True)
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
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
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
            shutil.rmtree(staging, ignore_errors=True)
            raise
        renamed = False
        try:
            if old.exists():
                shutil.rmtree(old)
            os.rename(root, old)
            renamed = True
            os.rename(staging, root)
            shutil.rmtree(old)
        except Exception as exc:
            if renamed and not root.exists() and old.exists():
                os.rename(old, root)  # best-effort rollback
            shutil.rmtree(staging, ignore_errors=True)  # failed mid-swap
            raise TransferError(
                f"Import failed during restore; the previous library is backed up at {backup_zip}"
            ) from exc
        log_mod.append_entry(root, "Update", "Library restored from uploaded archive.")


def import_bundle(root: str | Path, src_zip: str | Path, backup_zip: str | Path) -> None:
    """Convenience wrapper: stage_import + replace_staged_bundle.

    Used by storage tests; the route calls the two phases separately so
    validation/extraction happen before the run gates are taken.
    """
    staging = stage_import(root, src_zip)
    replace_staged_bundle(root, staging, backup_zip)


def _build_zip(root: Path, dest_zip: Path) -> int:
    """Zip the whole bundle (dot-dirs included) deterministically.

    Entries are sorted by POSIX relpath; directories get explicit entries
    (name + ``/``) so empty dirs (e.g. an empty ``.athenaeum/payloads/``)
    round-trip. Returns the member count.
    """
    entries = sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix())
    count = 0
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for entry in entries:
            arcname = entry.relative_to(root).as_posix()
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
