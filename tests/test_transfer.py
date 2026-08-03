"""Storage-level tests for whole-bundle export/import (library/transfer.py).

Bare tmp_path roots per the test_concurrency.py pattern; the busy-gate and
evict tests use the shared conftest app/client/admin_user fixtures (the real
create_app assembly with a real LibrarianManager).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import zipfile
from pathlib import Path

import pytest

from athenaeum.librarian.agent import KIND_LIBRARIAN
from athenaeum.library import transfer
from athenaeum.library.backend import LibraryBackend


def _init_root(tmp_path: Path) -> Path:
    root = tmp_path / "library"
    LibraryBackend(root, actor="test-transfer").init_bundle()
    return root


def _snapshot(root: Path) -> tuple[dict[str, str], set[str]]:
    """(relpath -> sha256) for files plus the set of directory relpaths."""
    files: dict[str, str] = {}
    dirs: set[str] = set()
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        if path.is_dir():
            dirs.add(rel)
        else:
            files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files, dirs


def test_round_trip_tree_equality(tmp_path):
    root = _init_root(tmp_path)
    backend = LibraryBackend(root, actor="test-transfer")
    backend.create_concept("/concepts/alpha.md", {"type": "Note", "title": "Alpha"}, "body\n")
    (root / ".traces").mkdir(exist_ok=True)
    (root / ".traces" / "t.json").write_text("{}\n", encoding="utf-8")
    (root / ".athenaeum" / "payloads").mkdir(parents=True, exist_ok=True)  # empty dir
    original_files, original_dirs = _snapshot(root)
    original_log = (root / "log.md").read_text(encoding="utf-8")

    export_zip = tmp_path / "export.zip"
    count = transfer.export_bundle(root, export_zip)
    assert count == len(original_files) + len(original_dirs)

    # destroy + modify the tree, then restore from the archive
    (root / "concepts" / "alpha.md").unlink()
    (root / "index.md").write_text("corrupted\n", encoding="utf-8")
    transfer.import_bundle(root, export_zip, tmp_path / transfer.BACKUP_ZIP_NAME)

    restored_files, restored_dirs = _snapshot(root)
    assert restored_dirs == original_dirs  # incl. dot-dirs and the empty dir
    assert restored_files.keys() == original_files.keys()
    for rel, digest in original_files.items():
        if rel != "log.md":  # log.md gained the restore entry
            assert restored_files[rel] == digest, rel
    restored_log = (root / "log.md").read_text(encoding="utf-8")
    assert original_log in restored_log
    assert "**Update**: Library restored from uploaded archive." in restored_log


def test_backup_zip_created_before_replace(tmp_path):
    root = _init_root(tmp_path)
    marker = root / "marker.md"
    marker.write_bytes(b"v1\n")
    export_zip = tmp_path / "export.zip"
    transfer.export_bundle(root, export_zip)
    backup_zip = tmp_path / transfer.BACKUP_ZIP_NAME

    # the pre-import tree carries v2; the archive restores v1
    marker.write_bytes(b"v2\n")
    transfer.import_bundle(root, export_zip, backup_zip)
    assert marker.read_bytes() == b"v1\n"
    assert backup_zip.is_file()
    with zipfile.ZipFile(backup_zip) as zf:
        assert zf.read("marker.md") == b"v2\n"  # the PRE-import tree

    # a second import overwrites the same single backup file (1 generation)
    marker.write_bytes(b"v3\n")
    transfer.import_bundle(root, export_zip, backup_zip)
    with zipfile.ZipFile(backup_zip) as zf:
        assert zf.read("marker.md") == b"v3\n"
    assert [p.name for p in tmp_path.glob("import-backup*")] == [transfer.BACKUP_ZIP_NAME]


def test_rollback_on_swap_failure(tmp_path, monkeypatch):
    root = _init_root(tmp_path)
    (root / "marker.md").write_text("original\n", encoding="utf-8")
    export_zip = tmp_path / "export.zip"
    transfer.export_bundle(root, export_zip)
    (root / "marker.md").write_text("changed\n", encoding="utf-8")
    before = _snapshot(root)
    backup_zip = tmp_path / transfer.BACKUP_ZIP_NAME

    real_rename = os.rename
    calls = 0

    def flaky_rename(src, dst):
        nonlocal calls
        calls += 1
        if calls == 2:  # fail the staging -> root rename, mid-swap
            raise OSError("simulated rename failure")
        return real_rename(src, dst)

    monkeypatch.setattr(transfer.os, "rename", flaky_rename)
    with pytest.raises(transfer.TransferError) as excinfo:
        transfer.import_bundle(root, export_zip, backup_zip)
    assert str(backup_zip) in str(excinfo.value)
    # best-effort rollback restored the pre-import tree byte-identically
    assert _snapshot(root) == before
    assert not (tmp_path / transfer.STAGING_DIRNAME).exists()
    assert not (tmp_path / transfer.OLD_DIRNAME).exists()


def test_backup_failure_leaves_bundle_untouched(tmp_path, monkeypatch):
    root = _init_root(tmp_path)
    (root / "marker.md").write_text("original\n", encoding="utf-8")
    export_zip = tmp_path / "export.zip"
    transfer.export_bundle(root, export_zip)
    (root / "marker.md").write_text("changed\n", encoding="utf-8")
    before = _snapshot(root)

    def boom(root_, dest_zip):
        raise OSError("disk full")

    monkeypatch.setattr(transfer, "_build_zip", boom)
    with pytest.raises(OSError, match="disk full"):
        transfer.import_bundle(root, export_zip, tmp_path / transfer.BACKUP_ZIP_NAME)
    assert _snapshot(root) == before  # nothing destructive happened
    assert not (tmp_path / transfer.STAGING_DIRNAME).exists()
    assert not (tmp_path / transfer.OLD_DIRNAME).exists()
    assert not (tmp_path / transfer.BACKUP_ZIP_NAME).exists()


def _evil_zip(tmp_path: Path, member: str | zipfile.ZipInfo) -> Path:
    path = tmp_path / "evil.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("index.md", "# I\n")
        zf.writestr("log.md", "# L\n")
        zf.writestr(member, "x")
    return path


@pytest.mark.parametrize("member", ["../x.md", "/abs.md", "C:/x.md", "//unc/x.md"])
def test_zip_slip_members_rejected(tmp_path, member):
    root = _init_root(tmp_path)
    with pytest.raises(transfer.UnsafeMemberError):
        transfer.stage_import(root, _evil_zip(tmp_path, member))
    assert not (tmp_path / transfer.STAGING_DIRNAME).exists()  # nothing extracted


def test_zip_symlink_member_rejected(tmp_path):
    root = _init_root(tmp_path)
    info = zipfile.ZipInfo("link.md")
    info.external_attr = 0o120777 << 16  # S_IFLNK | 0777
    with pytest.raises(transfer.UnsafeMemberError):
        transfer.stage_import(root, _evil_zip(tmp_path, info))
    assert not (tmp_path / transfer.STAGING_DIRNAME).exists()


def test_corrupt_and_missing_required(tmp_path):
    root = _init_root(tmp_path)
    before = _snapshot(root)

    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(transfer.CorruptArchiveError):
        transfer.stage_import(root, bad)

    for missing in ("index.md", "log.md"):
        path = tmp_path / f"missing-{missing}.zip"
        members = {"index.md": "# I\n", "log.md": "# L\n"}
        del members[missing]
        with zipfile.ZipFile(path, "w") as zf:
            for name, content in members.items():
                zf.writestr(name, content)
        with pytest.raises(transfer.MissingBundleFileError):
            transfer.stage_import(root, path)

    assert _snapshot(root) == before  # bundle untouched
    assert not (tmp_path / transfer.STAGING_DIRNAME).exists()


def test_import_busy_gate_409(client, admin_user, test_settings, tmp_path):
    """A held run gate rejects the import with 409 (scheduler semantics)."""
    response = client.post("/login", data={"username": "owner", "password": "owner-pw"})
    assert response.status_code == 303
    uid = admin_user["id"]
    manager = client.app.state.librarian_manager

    async def hold_gate():
        lock = await manager.run_gate._lock_for(uid, KIND_LIBRARIAN)
        await lock.acquire()

    # The lock state persists after the loop closes; the route's wait=False
    # peek (locked()) never touches the loop (CPython 3.12 asyncio.Lock).
    asyncio.run(hold_gate())

    root = Path(test_settings.data_root) / "users" / uid / "library"
    export_zip = tmp_path / "export.zip"
    transfer.export_bundle(root, export_zip)
    response = client.post(
        "/library/import",
        files={"file": ("lib.zip", export_zip.read_bytes(), "application/zip")},
    )
    assert response.status_code == 409
    assert "in progress" in response.text


def test_import_evicts_and_marks_reconcile_pending(client, admin_user, test_settings, tmp_path):
    """Import evicts the cached librarian; the embedding/FTS reconcile rides
    the existing evict -> pending path, never a synchronous re-embed."""
    response = client.post("/login", data={"username": "owner", "password": "owner-pw"})
    assert response.status_code == 303
    uid = admin_user["id"]
    manager = client.app.state.librarian_manager
    manager.get(uid)  # warm the cache (an unconfigured librarian builds fine)
    assert uid in manager.cached_user_ids()

    root = Path(test_settings.data_root) / "users" / uid / "library"
    export_zip = tmp_path / "export.zip"
    transfer.export_bundle(root, export_zip)
    response = client.post(
        "/library/import",
        files={"file": ("lib.zip", export_zip.read_bytes(), "application/zip")},
    )
    assert response.status_code == 303
    assert uid not in manager.cached_user_ids()  # evict ran
