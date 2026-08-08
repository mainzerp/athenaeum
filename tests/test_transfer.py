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
from pathlib import Path, PurePosixPath

import pytest

from athenaeum.librarian.agent import KIND_LIBRARIAN
from athenaeum.library import gittool, transfer
from athenaeum.library.backend import LibraryBackend


def _init_root(tmp_path: Path) -> Path:
    root = tmp_path / "library"
    LibraryBackend(root, actor="test-transfer").init_bundle()
    return root


def _snapshot(root: Path) -> tuple[dict[str, str], set[str]]:
    """(relpath -> sha256) for files plus the set of directory relpaths.

    Excludes ``.git``: archives never carry it (export exclusion) and the
    post-import repo is a fresh re-init, so history cannot compare equal.
    """
    files: dict[str, str] = {}
    dirs: set[str] = set()
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        if ".git" in path.relative_to(root).parts:
            continue
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


def test_export_excludes_git_and_legacy_versions(tmp_path):
    """Archives are content bundles: no .git, no .athenaeum/versions/ (0.22.0)."""
    root = _init_root(tmp_path)
    # .git exists on hosts with git (init_bundle); hand-made entries keep the
    # exclusion exercised on git-less hosts either way.
    (root / ".git" / "objects").mkdir(parents=True, exist_ok=True)
    (root / ".git" / "objects" / "blob").write_bytes(b"x")
    (root / ".athenaeum" / "versions").mkdir(parents=True, exist_ok=True)
    (root / ".athenaeum" / "versions" / "v1.json").write_text("{}\n", encoding="utf-8")

    export_zip = tmp_path / "export.zip"
    transfer.export_bundle(root, export_zip)
    with zipfile.ZipFile(export_zip) as zf:
        names = zf.namelist()
    assert "index.md" in names  # content IS archived
    assert not any(".git" in PurePosixPath(n).parts for n in names)
    assert not any(PurePosixPath(n).parts[:2] == (".athenaeum", "versions") for n in names)


def test_import_rejects_git_members(tmp_path):
    """Hook-injection guard: exports never carry .git, so an archive that
    does is hostile (supply-chain via git hooks)."""
    root = _init_root(tmp_path)
    with pytest.raises(transfer.UnsafeMemberError, match=r"\.git members are not allowed"):
        transfer.stage_import(root, _evil_zip(tmp_path, ".git/hooks/x"))
    assert not (tmp_path / transfer.STAGING_DIRNAME).exists()


def test_import_accepts_legacy_versions_members(tmp_path):
    """Pre-0.22 archives carry .athenaeum/versions/ — accepted but inert
    (gitignored, unread by 0.22.0+)."""
    root = _init_root(tmp_path)
    archive = tmp_path / "old.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("index.md", "# I\n")
        zf.writestr("log.md", "# L\n")
        zf.writestr(".athenaeum/versions/v1.json", "{}\n")
        zf.writestr("restored.md", "back\n")
    transfer.import_bundle(root, archive, tmp_path / transfer.BACKUP_ZIP_NAME)
    assert (root / "restored.md").read_text(encoding="utf-8") == "back\n"
    assert (root / ".athenaeum" / "versions" / "v1.json").is_file()


def test_import_decompressed_size_cap_aborts(tmp_path, monkeypatch):
    """LIBRARY-01: a zip bomb aborts on the running decompressed total across
    ALL members; the staging tree is removed and the live bundle untouched."""
    root = _init_root(tmp_path)
    before = _snapshot(root)
    archive = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.md", "# I\n")
        zf.writestr("log.md", "# L\n")
        zf.writestr("payload.md", "x" * 10_000)
    monkeypatch.setattr(transfer, "MAX_EXTRACT_BYTES", 5_000)
    with pytest.raises(transfer.CorruptArchiveError, match="decompressed size cap"):
        transfer.stage_import(root, archive)
    assert not (tmp_path / transfer.STAGING_DIRNAME).exists()
    assert _snapshot(root) == before


def test_import_member_count_cap_aborts(tmp_path, monkeypatch):
    """LIBRARY-01: the member-count cap aborts hostile archives too."""
    root = _init_root(tmp_path)
    archive = tmp_path / "many.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("index.md", "# I\n")
        zf.writestr("log.md", "# L\n")
        for i in range(5):
            zf.writestr(f"m{i}.md", "x")
    monkeypatch.setattr(transfer, "MAX_EXTRACT_MEMBERS", 3)
    with pytest.raises(transfer.CorruptArchiveError, match="member cap"):
        transfer.stage_import(root, archive)
    assert not (tmp_path / transfer.STAGING_DIRNAME).exists()


@pytest.mark.parametrize("member", [".GIT/hooks/post-commit", ".git./x", ".git /x"])
def test_import_rejects_git_member_variants(tmp_path, member):
    """SERVER-01: .git variants that resolve to the same directory on
    case/quote-insensitive filesystems are rejected like exact .git."""
    root = _init_root(tmp_path)
    with pytest.raises(transfer.UnsafeMemberError, match=r"\.git members are not allowed"):
        transfer.stage_import(root, _evil_zip(tmp_path, member))
    assert not (tmp_path / transfer.STAGING_DIRNAME).exists()


def test_export_excludes_symlink_members(tmp_path):
    """LIBRARY-02: a symlinked dir pointing outside the root is never
    descended or archived; a symlinked file is skipped."""
    root = _init_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("secret\n", encoding="utf-8")
    try:
        os.symlink(outside, root / "escape", target_is_directory=True)
        os.symlink(root / "index.md", root / "linked.md")
    except OSError:
        pytest.skip("symlink creation not permitted on this host")
    export_zip = tmp_path / "export.zip"
    transfer.export_bundle(root, export_zip)
    with zipfile.ZipFile(export_zip) as zf:
        names = zf.namelist()
    assert not any(name.startswith("escape") for name in names)
    assert "linked.md" not in names
    assert "index.md" in names  # real content still archived


def test_import_lock_helper_per_user_and_peek():
    """SERVER-06: the per-user import lock is stable per uid and the route's
    409 path peeks it with wait=False semantics (test_webui.py route-level
    coverage belongs to Part 3)."""
    from athenaeum.webui.routes_library import _import_lock_for

    lock = _import_lock_for("lock-peek-user")
    assert _import_lock_for("lock-peek-user") is lock
    assert _import_lock_for("other-user") is not lock
    assert not lock.locked()

    async def acquire() -> None:
        await lock.acquire()

    asyncio.run(acquire())
    assert lock.locked()  # state persists after the loop closes (CPython 3.12+)
    lock.release()
    assert not lock.locked()


@pytest.mark.skipif(not gittool.git_available(), reason="git binary required")
def test_import_reinitializes_git_history(tmp_path):
    """The swapped-in tree gets a fresh repo with one restore commit."""
    root = _init_root(tmp_path)
    export_zip = tmp_path / "export.zip"
    transfer.export_bundle(root, export_zip)
    transfer.import_bundle(root, export_zip, tmp_path / transfer.BACKUP_ZIP_NAME)
    commits = gittool.GitRepo(root).list_commits()
    assert [c["subject"] for c in commits] == ["Update: Library restored from uploaded archive."]
    assert commits[0]["is_root"]


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
