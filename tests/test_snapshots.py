"""Tests for athenaeum.library.snapshots.VersionStore."""

import json
import threading

import pytest

from athenaeum.isolation import PathEscapeError
from athenaeum.library.snapshots import VersionStore

ACTOR = "athenaeum-librarian/0.1.0"


def test_pre_image_captured_before_write(tmp_path):
    target = tmp_path / "a.md"
    target.write_text("v1\n", encoding="utf-8")
    store = VersionStore(tmp_path)
    n = store.create("update", ["/a.md"], ACTOR)
    target.write_text("v2\n", encoding="utf-8")
    assert n == 1
    snap = tmp_path / ".athenaeum" / "versions" / "000001" / "a.md"
    assert snap.read_text(encoding="utf-8") == "v1\n"
    assert target.read_text(encoding="utf-8") == "v2\n"


def test_meta_fields(tmp_path):
    (tmp_path / "a.md").write_text("x\n", encoding="utf-8")
    store = VersionStore(tmp_path)
    store.create("update", ["/a.md"], ACTOR)
    meta = json.loads(
        (tmp_path / ".athenaeum" / "versions" / "000001" / "meta.json").read_text(encoding="utf-8")
    )
    assert meta["n"] == 1
    assert meta["actor"] == ACTOR
    assert meta["operation"] == "update"
    assert meta["paths"] == ["a.md"]
    assert meta["timestamp"]


def test_list_newest_first_and_monotonic_numbering(tmp_path):
    store = VersionStore(tmp_path)
    store.create("create", ["/a.md"], ACTOR)
    store.create("update", ["/a.md"], ACTOR)
    versions = store.list()
    assert [v["n"] for v in versions] == [2, 1]


def test_rollback_restores_pre_image(tmp_path):
    target = tmp_path / "a.md"
    target.write_text("v1\n", encoding="utf-8")
    store = VersionStore(tmp_path)
    store.create("update", ["/a.md"], ACTOR)
    target.write_text("v2\n", encoding="utf-8")
    store.rollback(1)
    assert target.read_text(encoding="utf-8") == "v1\n"


def test_rollback_removes_file_created_after_snapshot(tmp_path):
    store = VersionStore(tmp_path)
    store.create("create", ["/new.md"], ACTOR)
    created = tmp_path / "new.md"
    created.write_text("x\n", encoding="utf-8")
    store.rollback(1)
    assert not created.exists()


def test_diff_shows_changes(tmp_path):
    target = tmp_path / "a.md"
    target.write_text("old line\n", encoding="utf-8")
    store = VersionStore(tmp_path)
    store.create("update", ["/a.md"], ACTOR)
    target.write_text("new line\n", encoding="utf-8")
    diff = store.diff(1, "/a.md")
    assert "-old line" in diff
    assert "+new line" in diff


def test_prune_keeps_newest_n(tmp_path):
    store = VersionStore(tmp_path)
    for _ in range(3):
        store.create("update", ["/a.md"], ACTOR)
    store.prune(2)
    remaining = sorted(
        d.name for d in (tmp_path / ".athenaeum" / "versions").iterdir() if d.is_dir()
    )
    assert remaining == ["000002", "000003"]


def test_keep_prunes_on_create(tmp_path):
    store = VersionStore(tmp_path, keep=1)
    store.create("create", ["/a.md"], ACTOR)
    store.create("update", ["/a.md"], ACTOR)
    remaining = [d.name for d in (tmp_path / ".athenaeum" / "versions").iterdir()]
    assert remaining == ["000002"]


def test_concurrent_stores_claim_unique_n(tmp_path):
    """Two VersionStores on one root racing create() never collide on n."""
    stores = (VersionStore(tmp_path), VersionStore(tmp_path))
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def creator(store: VersionStore) -> None:
        try:
            barrier.wait(timeout=10)
            for _ in range(20):
                results.append(store.create("update", ["/a.md"], ACTOR))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=creator, args=(store,)) for store in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    assert len(results) == 40
    assert len(set(results)) == 40  # every claimed n is unique


def test_diff_rejects_traversal(tmp_path):
    """CS-1: diff must never resolve paths outside the library root."""
    (tmp_path / "a.md").write_text("x\n", encoding="utf-8")
    store = VersionStore(tmp_path)
    store.create("update", ["/a.md"], ACTOR)
    for evil in ("../../a.md", "..\\..\\a.md", "/../../etc/passwd", "a/../../b.md"):
        with pytest.raises(PathEscapeError):
            store.diff(1, evil)


def test_diff_rejects_os_absolute_path(tmp_path):
    store = VersionStore(tmp_path)
    store.create("update", ["/a.md"], ACTOR)
    with pytest.raises(PathEscapeError):
        store.diff(1, "C:/Windows/win.ini")
    with pytest.raises(PathEscapeError):
        store.diff(1, "//server/share/secret")


def test_diff_cannot_read_env_escape(tmp_path):
    """A ``.env`` file above the root is unreachable via diff (CS-1 chain)."""
    secret = tmp_path / "secret.env"
    secret.write_text("ATHENAEUM_SECRET_KEY=leak\n", encoding="utf-8")
    root = tmp_path / "library"
    root.mkdir()
    (root / "a.md").write_text("x\n", encoding="utf-8")
    store = VersionStore(root)
    n = store.create("update", ["/a.md"], ACTOR)
    with pytest.raises(PathEscapeError):
        store.diff(n, "../secret.env")


def test_rollback_rejects_tampered_meta_paths(tmp_path):
    """Rollback guards meta.json paths too (defense against a tampered store)."""
    store = VersionStore(tmp_path)
    n = store.create("update", ["/a.md"], ACTOR)
    meta_path = tmp_path / ".athenaeum" / "versions" / f"{n:06d}" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["paths"] = ["../../evil.md"]
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(PathEscapeError):
        store.rollback(n)


def test_rollback_rejects_non_latest(tmp_path):
    """L12: only the newest snapshot may be rolled back (mixed states else)."""
    (tmp_path / "a.md").write_text("v1\n", encoding="utf-8")
    store = VersionStore(tmp_path)
    store.create("update", ["/a.md"], ACTOR)
    (tmp_path / "a.md").write_text("v2\n", encoding="utf-8")
    store.create("update", ["/a.md"], ACTOR)
    with pytest.raises(ValueError, match="latest snapshot"):
        store.rollback(1)
    # the latest one still works
    store.rollback(2)
    assert (tmp_path / "a.md").read_text(encoding="utf-8") == "v2\n"


def test_rollback_unknown_snapshot_raises(tmp_path):
    store = VersionStore(tmp_path)
    store.create("update", ["/a.md"], ACTOR)
    with pytest.raises(FileNotFoundError):
        store.rollback(999)


def test_rollback_prunes_empty_dirs(tmp_path):
    """L13: dirs left empty by rolling back a create are removed."""
    store = VersionStore(tmp_path)
    store.create("create", ["/sub/deep/new.md"], ACTOR)
    created = tmp_path / "sub" / "deep" / "new.md"
    created.parent.mkdir(parents=True)
    created.write_text("x\n", encoding="utf-8")
    store.rollback(1)
    assert not created.exists()
    assert not (tmp_path / "sub").exists()


def test_rollback_keeps_nonempty_dirs(tmp_path):
    store = VersionStore(tmp_path)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "keep.md").write_text("k\n", encoding="utf-8")
    store.create("create", ["/sub/new.md"], ACTOR)
    (tmp_path / "sub" / "new.md").write_text("x\n", encoding="utf-8")
    store.rollback(1)
    assert not (tmp_path / "sub" / "new.md").exists()
    assert (tmp_path / "sub" / "keep.md").is_file()
