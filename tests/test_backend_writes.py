"""Tests for athenaeum.library.backend.LibraryBackend compound writes."""

import json
import os
import shutil
import subprocess

import pytest

from athenaeum.librarian.tools import dispatch
from athenaeum.library import escape_guard as escape_guard_mod
from athenaeum.library import frontmatter as fm_mod
from athenaeum.library.backend import LibraryBackend, provision_library
from athenaeum.library.frontmatter import split_document
from athenaeum.library.gittool import GitError

ACTOR = "athenaeum-librarian/0.1.0"

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git binary required")


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def make_backend(tmp_path, **kwargs):
    backend = LibraryBackend(tmp_path / "lib", actor=ACTOR, **kwargs)
    backend.init_bundle()
    return backend


def read_log(root):
    return (root / "log.md").read_text(encoding="utf-8")


def test_provision_library_creates_okf_bundle(tmp_path):
    """A7: user filesystem provisioning lives in the library layer."""
    library_root = provision_library(tmp_path, "user-1")
    assert library_root == tmp_path / "users" / "user-1" / "library"
    fm, _ = split_document((library_root / "index.md").read_text(encoding="utf-8"))
    assert fm == {"okf_version": "0.2"}
    assert "**Initialization**" in (library_root / "log.md").read_text(encoding="utf-8")
    provision_library(tmp_path, "user-1")  # idempotent: existing bundle untouched


def test_init_bundle(tmp_path):
    backend = LibraryBackend(tmp_path / "lib", actor=ACTOR)
    backend.init_bundle()
    root = tmp_path / "lib"
    fm, _ = split_document((root / "index.md").read_text(encoding="utf-8"))
    assert fm == {"okf_version": "0.2"}
    assert "**Initialization**" in (root / "log.md").read_text(encoding="utf-8")


@requires_git
def test_compound_write_produces_concept_index_log_commit(tmp_path):
    backend = make_backend(tmp_path)
    result = backend.create_concept(
        "/tables/customers.md",
        {"type": "Concept", "title": "Customers", "description": "Customer data"},
        "# Customers\n",
    )
    assert result == {"id": "/tables/customers", "action": "created"}
    root = tmp_path / "lib"
    # concept file with injected generated
    doc = backend.read_document("/tables/customers.md")
    assert doc["frontmatter"]["generated"]["by"] == ACTOR
    assert doc["frontmatter"]["generated"]["at"]
    assert doc["body"] == "# Customers\n"
    # regenerated indexes (target dir and root chain)
    assert "customers.md" in (root / "tables" / "index.md").read_text(encoding="utf-8")
    assert "tables/" in (root / "index.md").read_text(encoding="utf-8")
    # log entry
    assert "**Creation**" in read_log(root)
    # one git commit per compound write; the message mirrors the log entry
    commits = backend.list_commits()
    assert [c["subject"] for c in commits] == [
        "Creation: Created [Customers](/tables/customers.md).",
        "Initialization: Initialized the library bundle.",
    ]
    assert backend.status()["stats"]["versions"] == 2
    # no leftover .tmp files
    assert not list(root.rglob("*.tmp"))


def test_reserved_names_refused(tmp_path):
    backend = make_backend(tmp_path)
    for path in ("/index.md", "/log.md", "/sub/index.md", "/sub/log.md"):
        with pytest.raises(ValueError, match="reserved"):
            backend.create_concept(path, {"type": "Concept"}, "x\n")


def test_reserved_name_path_components_refused(tmp_path):
    """LIBRARY-11: reserved names are refused in ANY path component, not
    just the basename (a mid-path index.md would shadow a directory index)."""
    backend = make_backend(tmp_path)
    for path in ("/a/index.md/x.md", "/log.md/x.md", "/index.md/x.md"):
        with pytest.raises(ValueError, match="reserved"):
            backend.create_concept(path, {"type": "Concept"}, "x\n")


def test_non_md_and_existing_and_typeless_refused(tmp_path):
    backend = make_backend(tmp_path)
    with pytest.raises(ValueError, match=".md"):
        backend.create_concept("/notes.txt", {"type": "Concept"}, "x\n")
    with pytest.raises(ValueError, match="type"):
        backend.create_concept("/a.md", {"title": "No type"}, "x\n")
    backend.create_concept("/a.md", {"type": "Concept"}, "x\n")
    with pytest.raises(FileExistsError):
        backend.create_concept("/a.md", {"type": "Concept"}, "y\n")


def test_edit_preserves_unknown_keys_and_body(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept(
        "/a.md",
        {"type": "Concept", "title": "A", "custom_key": "keep me"},
        "line1\nline2\n",
    )
    result = backend.edit_concept("/a.md", frontmatter_patch={"description": "new"})
    assert result == {"id": "/a", "action": "updated"}
    doc = backend.read_document("/a.md")
    assert doc["frontmatter"]["custom_key"] == "keep me"
    assert doc["frontmatter"]["description"] == "new"
    assert doc["body"] == "line1\nline2\n"


def test_edit_never_touches_verified(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept("/a.md", {"type": "Concept"}, "x\n")
    with pytest.raises(ValueError, match="verified"):
        backend.edit_concept("/a.md", frontmatter_patch={"verified": []})
    with pytest.raises(ValueError, match="verified"):
        backend.edit_concept("/a.md", remove_keys=["verified"])
    # The guard stays: verify_concept is the ONLY path that writes 'verified'.
    result = backend.verify_concept("/a.md", by="athenaeum-curator/0.1.0")
    assert result == {"id": "/a", "action": "verified"}
    assert backend.read_document("/a.md")["frontmatter"]["verified"]


def test_edit_never_touches_generated(tmp_path):
    """LIBRARY-03: 'generated' provenance is refused like 'verified' —
    _inject_generated stays the sole writer."""
    backend = make_backend(tmp_path)
    backend.create_concept("/a.md", {"type": "Concept"}, "x\n")
    with pytest.raises(ValueError, match="generated"):
        backend.edit_concept("/a.md", frontmatter_patch={"generated": {"by": "mallory"}})
    with pytest.raises(ValueError, match="generated"):
        backend.edit_concept("/a.md", remove_keys=["generated"])
    # Untouched: the creation-time provenance survives the refused edits.
    assert backend.read_document("/a.md")["frontmatter"]["generated"]["by"] == ACTOR


def test_create_concept_strips_caller_supplied_verified(tmp_path):
    """R9: concepts are born unverified; verify_concept is the sole writer."""
    backend = make_backend(tmp_path)
    backend.create_concept(
        "/a.md",
        {"type": "Concept", "verified": [{"by": "human:mallory", "at": "2026-01-01T00:00:00Z"}]},
        "x\n",
    )
    assert "verified" not in backend.read_document("/a.md")["frontmatter"]


def test_verify_concept_appends_without_generated_refresh(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept("/a.md", {"type": "Concept", "title": "A"}, "x\n")
    backend._now = lambda: "2099-01-01T00:00:00+00:00"
    before = backend.read_document("/a.md")["frontmatter"]["generated"]
    result = backend.verify_concept("/a.md", by="athenaeum-curator/0.2.0")
    assert result == {"id": "/a", "action": "verified"}
    fm = backend.read_document("/a.md")["frontmatter"]
    assert fm["verified"] == [{"by": "athenaeum-curator/0.2.0", "at": "2099-01-01T00:00:00+00:00"}]
    # verification is metadata: 'generated' stays byte-identical (no refresh)
    assert fm["generated"] == before


def test_verify_concept_merges_bare_mapping(tmp_path):
    backend = make_backend(tmp_path)
    root = tmp_path / "lib"
    (root / "a.md").write_text(
        "---\ntype: Concept\nverified:\n  by: human:alice\n  at: 2026-01-01T00:00:00Z\n---\nx\n",
        encoding="utf-8",
    )
    backend.verify_concept("/a.md", by="athenaeum-curator/0.2.0", at="2026-02-02T00:00:00+00:00")
    verified = backend.read_document("/a.md")["frontmatter"]["verified"]
    # YAML parses the ISO 'at' of the original entry into a datetime; the
    # appended entry keeps the exact string passed in.
    assert [entry["by"] for entry in verified] == ["human:alice", "athenaeum-curator/0.2.0"]
    assert str(verified[0]["at"]) == "2026-01-01 00:00:00+00:00"
    assert verified[1]["at"] == "2026-02-02T00:00:00+00:00"


def test_verify_concept_appends_to_existing_list(tmp_path):
    backend = make_backend(tmp_path)
    root = tmp_path / "lib"
    (root / "a.md").write_text(
        "---\ntype: Concept\nverified:\n  - by: human:alice\n"
        "    at: 2026-01-01T00:00:00Z\n---\nx\n",
        encoding="utf-8",
    )
    backend.verify_concept("/a.md", by="athenaeum-curator/0.2.0", at="2026-02-02T00:00:00+00:00")
    backend.verify_concept("/a.md", by="athenaeum-curator/0.2.0", at="2026-03-03T00:00:00+00:00")
    verified = backend.read_document("/a.md")["frontmatter"]["verified"]
    assert [entry["by"] for entry in verified] == [
        "human:alice",
        "athenaeum-curator/0.2.0",
        "athenaeum-curator/0.2.0",
    ]


@requires_git
def test_verify_concept_commit_and_log(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept("/a.md", {"type": "Concept", "title": "A"}, "x\n")
    backend.verify_concept("/a.md", by="athenaeum-curator/0.2.0", agent_label="bot-1")
    root = tmp_path / "lib"
    log = read_log(root)
    assert "**Verification**" in log
    assert "(verifier: athenaeum-curator/0.2.0)" in log
    assert "(requested by agent:bot-1)" in log
    # the commit message mirrors the log text, attribution suffix included
    assert backend.list_commits()[0]["subject"] == (
        "Verification: Verified [A](/a.md) (verifier: athenaeum-curator/0.2.0)."
        " (requested by agent:bot-1)"
    )


def test_verify_concept_refusals(tmp_path):
    backend = make_backend(tmp_path)
    with pytest.raises(FileNotFoundError):
        backend.verify_concept("/missing.md", by="athenaeum-curator/0.2.0")
    backend.create_concept("/a.md", {"type": "Concept"}, "x\n")
    for bad in ("", None, 42):
        with pytest.raises(ValueError, match="by"):
            backend.verify_concept("/a.md", by=bad)


def test_update_paths_refresh_generated_at(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept("/a.md", {"type": "Concept"}, "x\n")
    original = backend.read_document("/a.md")["frontmatter"]["generated"]
    backend._now = lambda: "2099-01-01T00:00:00+00:00"
    backend.edit_concept("/a.md", frontmatter_patch={"description": "d"})
    edited = backend.read_document("/a.md")["frontmatter"]["generated"]
    assert edited["at"] == "2099-01-01T00:00:00+00:00" != original["at"]
    assert edited["by"] == original["by"] == backend.actor
    backend.deprecate_concept("/a.md")
    deprecated = backend.read_document("/a.md")["frontmatter"]["generated"]
    assert deprecated["at"] == "2099-01-01T00:00:00+00:00"


def test_move_rewrites_links_and_both_indexes(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept("/b.md", {"type": "Concept", "title": "B"}, "b\n")
    backend.create_concept("/a.md", {"type": "Concept", "title": "A"}, "see [B](/b.md)\n")
    result = backend.move_concept("/b.md", "/moved/b.md")
    assert result["action"] == "moved"
    assert result["id"] == "/moved/b"
    assert result["links_rewritten"] == 1
    root = tmp_path / "lib"
    assert "(/moved/b.md)" in (root / "a.md").read_text(encoding="utf-8")
    assert "b.md" in (root / "moved" / "index.md").read_text(encoding="utf-8")
    assert "moved/" in (root / "index.md").read_text(encoding="utf-8")
    assert "**Move**" in read_log(root)
    assert backend.validate()["errors"] == []


def test_delete_reports_inbound_links(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept("/b.md", {"type": "Concept"}, "b\n")
    backend.create_concept("/a.md", {"type": "Concept"}, "see [B](/b.md)\n")
    result = backend.delete_concept("/b.md")
    assert result == {"id": "/b", "action": "deleted", "inbound_links": ["/a.md"]}
    root = tmp_path / "lib"
    assert not (root / "b.md").exists()
    assert "**Deletion**" in read_log(root)


def test_move_prunes_emptied_directory(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept("/old/only.md", {"type": "Concept"}, "x\n")
    backend.create_concept("/keep.md", {"type": "Concept"}, "y\n")
    backend.move_concept("/old/only.md", "/new/only.md")
    root = tmp_path / "lib"
    assert not (root / "old").exists()
    assert "old/" not in (root / "index.md").read_text(encoding="utf-8")
    assert backend.validate()["errors"] == []


def test_move_keeps_directory_with_remaining_concepts(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept("/d/a.md", {"type": "Concept"}, "a\n")
    backend.create_concept("/d/b.md", {"type": "Concept"}, "b\n")
    backend.move_concept("/d/a.md", "/a.md")
    root = tmp_path / "lib"
    assert (root / "d" / "b.md").is_file()
    assert "b.md" in (root / "d" / "index.md").read_text(encoding="utf-8")


def test_delete_prunes_emptied_directory(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept("/solo/only.md", {"type": "Concept"}, "x\n")
    backend.create_concept("/keep.md", {"type": "Concept"}, "y\n")
    backend.delete_concept("/solo/only.md")
    root = tmp_path / "lib"
    assert not (root / "solo").exists()
    assert backend.validate()["errors"] == []


def test_deprecate_sets_status(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept("/a.md", {"type": "Concept"}, "x\n")
    result = backend.deprecate_concept("/a.md")
    assert result == {"id": "/a", "action": "deprecated"}
    assert backend.read_document("/a.md")["frontmatter"]["status"] == "deprecated"
    assert "**Deprecation**" in read_log(tmp_path / "lib")


# --- write_asset (content-addressed asset store) -------------------------------


@requires_git
def test_write_asset_happy_path(tmp_path):
    backend = make_backend(tmp_path)
    root = tmp_path / "lib"
    log_before = read_log(root)
    data = b"\x89PNG\r\n\x1a\nfake-image-bytes"
    bundle_path = backend.write_asset("scan.png", data)
    assert bundle_path.startswith("/.athenaeum/assets/")
    assert bundle_path.endswith("-scan.png")
    stored = root / bundle_path.lstrip("/")
    assert stored.read_bytes() == data
    # outside the compound write: no log entry, no index drift — but the
    # asset IS committed to git history (assets are linked from concepts)
    assert read_log(root) == log_before
    assert backend.validate()["errors"] == []
    assert backend.list_commits()[0]["subject"].startswith("Asset: Stored asset ")
    assert not list(root.rglob("*.tmp"))


def test_write_asset_rejects_non_bare_names(tmp_path):
    backend = make_backend(tmp_path)
    for bad in ("../evil.png", "sub/dir.png", "/abs.png", "C:\\\\win.png", "..", ""):
        with pytest.raises(ValueError, match="bare name"):
            backend.write_asset(bad, b"x")


def test_write_asset_content_addressed_idempotent(tmp_path):
    backend = make_backend(tmp_path)
    data = b"same bytes"
    first = backend.write_asset("img.png", data)
    second = backend.write_asset("img.png", data)
    assert first == second  # re-store is idempotent
    other = backend.write_asset("img.png", b"different bytes")
    assert other != first  # name is content-addressed
    root = tmp_path / "lib"
    assert len(list((root / ".athenaeum" / "assets").iterdir())) == 2


def test_agent_label_suffix_in_log(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept("/a.md", {"type": "Concept"}, "x\n", agent_label="bot-1")
    assert "(requested by agent:bot-1)" in read_log(tmp_path / "lib")
    # generated.by stays the librarian actor regardless of agent label
    assert backend.read_document("/a.md")["frontmatter"]["generated"]["by"] == ACTOR


# --- generated provenance (requested_by / via) -------------------------------


def test_create_injects_requested_by_and_via(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept(
        "/a.md", {"type": "Concept"}, "x\n", requested_by="human:alice", via="mcp_chat"
    )
    generated = backend.read_document("/a.md")["frontmatter"]["generated"]
    assert generated["requested_by"] == "human:alice"
    assert generated["via"] == "mcp_chat"
    assert generated["by"] == ACTOR


def test_create_replaces_forged_generated_wholesale(tmp_path):
    """A caller-supplied generated mapping (incl. forged sub-keys) never survives."""
    backend = make_backend(tmp_path)
    backend.create_concept(
        "/a.md",
        {"type": "Concept", "generated": {"by": "human:mallory", "requested_by": "human:mallory"}},
        "x\n",
        requested_by="human:alice",
        via="mcp_chat",
    )
    generated = backend.read_document("/a.md")["frontmatter"]["generated"]
    assert generated["by"] == ACTOR
    assert generated["requested_by"] == "human:alice"
    assert set(generated) == {"by", "at", "requested_by", "via"}


def test_edit_preserves_provenance_without_new_requester(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept(
        "/a.md", {"type": "Concept"}, "x\n", requested_by="human:alice", via="mcp_chat"
    )
    backend.edit_concept("/a.md", frontmatter_patch={"description": "d"})  # curator edit
    generated = backend.read_document("/a.md")["frontmatter"]["generated"]
    assert generated["requested_by"] == "human:alice"
    assert generated["via"] == "mcp_chat"


def test_edit_with_new_requester_overwrites_and_refreshes(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept(
        "/a.md", {"type": "Concept"}, "x\n", requested_by="human:alice", via="mcp_chat"
    )
    backend._now = lambda: "2099-01-01T00:00:00+00:00"
    backend.edit_concept(
        "/a.md",
        frontmatter_patch={"description": "d"},
        requested_by="human:bob",
        via="mcp_chat",
    )
    generated = backend.read_document("/a.md")["frontmatter"]["generated"]
    assert generated["requested_by"] == "human:bob"
    assert generated["at"] == "2099-01-01T00:00:00+00:00"


def test_deprecate_preserves_provenance(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept(
        "/a.md", {"type": "Concept"}, "x\n", requested_by="human:alice", via="mcp_chat"
    )
    backend.deprecate_concept("/a.md")
    generated = backend.read_document("/a.md")["frontmatter"]["generated"]
    assert generated["requested_by"] == "human:alice"
    assert generated["via"] == "mcp_chat"


def test_verify_concept_preserves_provenance(tmp_path):
    """The F1 post-step never touches generated: requested_by survives curation."""
    backend = make_backend(tmp_path)
    backend.create_concept(
        "/a.md", {"type": "Concept"}, "x\n", requested_by="human:alice", via="mcp_chat"
    )
    before = backend.read_document("/a.md")["frontmatter"]["generated"]
    backend.verify_concept("/a.md", by="athenaeum-curator/0.2.0")
    assert backend.read_document("/a.md")["frontmatter"]["generated"] == before


def test_reconcile_repairs_stale_index(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept("/a.md", {"type": "Concept", "title": "A"}, "x\n")
    index_path = tmp_path / "lib" / "index.md"
    index_path.write_text("# Documents\n* [Stale](stale.md)\n", encoding="utf-8")
    assert any(w["code"] == "index-drift" for w in backend.validate()["warnings"])
    backend.reconcile()
    assert not any(w["code"] == "index-drift" for w in backend.validate()["warnings"])
    assert "a.md" in index_path.read_text(encoding="utf-8")


def test_reconcile_skips_symlinked_dirs(tmp_path):
    """LIBRARY-02: reconcile never descends into a symlinked directory, so
    no index.md is regenerated inside (or through) the escape."""
    backend = make_backend(tmp_path)
    root = tmp_path / "lib"
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    try:
        os.symlink(outside, root / "escape", target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not permitted on this host")
    backend.reconcile()
    assert not (outside / "index.md").exists()
    assert not (outside / "sub" / "index.md").exists()
    assert not (root / "escape" / "index.md").exists()


def test_status_healthy_bundle(tmp_path):
    backend = make_backend(tmp_path)
    status = backend.status()
    assert status["healthy"] is True
    assert status["stats"]["concepts"] == 0
    assert set(status["stats"]) == {"concepts", "directories", "versions", "last_write"}
    assert set(status["health"]) == {"orphans", "broken_links", "warnings", "errors"}


def test_status_reports_orphans_and_broken(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept("/a.md", {"type": "Concept", "title": "Alpha"}, "x\n")
    backend.create_concept("/b.md", {"type": "Concept"}, "[gone](/missing.md)\n")
    status = backend.status()
    assert status["healthy"] is False
    assert status["health"]["orphans"] == [{"id": "/a", "title": "Alpha"}]
    assert status["health"]["broken_links"] == [{"source": "/b.md", "target": "/missing.md"}]


def test_link_health_counts_inbound_and_outbound(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept("/a.md", {"type": "Concept", "title": "A"}, "[B](/b.md)\n")
    backend.create_concept("/b.md", {"type": "Concept", "title": "B"}, "plain\n")
    assert backend.link_health(["/a.md", "/b.md"]) == {
        "/a.md": {"inbound": 0, "outbound": 1},
        "/b.md": {"inbound": 1, "outbound": 0},
    }


def test_link_health_missing_path_reports_zero(tmp_path):
    backend = make_backend(tmp_path)
    assert backend.link_health(["/gone.md"]) == {"/gone.md": {"inbound": 0, "outbound": 0}}


def test_link_health_ignores_bare_paths(tmp_path):
    # F19 mechanic: a bare path is invisible to the link graph (LINK_RE)
    backend = make_backend(tmp_path)
    backend.create_concept("/a.md", {"type": "Concept", "title": "A"}, "see /b.md\n")
    backend.create_concept("/b.md", {"type": "Concept", "title": "B"}, "plain\n")
    assert backend.link_health(["/b.md"])["/b.md"]["inbound"] == 0


@requires_git
def test_revert_commit_restores_concept_and_logs(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept("/a.md", {"type": "Concept"}, "v1\n")
    backend.edit_concept("/a.md", new_body="v2\n")
    assert backend.read_document("/a.md")["body"] == "v2\n"
    edit_sha = backend.list_commits()[0]["sha"]
    backend.revert_commit(edit_sha)
    assert backend.read_document("/a.md")["body"] == "v1\n"
    assert f"Reverted commit {edit_sha[:7]}" in read_log(tmp_path / "lib")
    commits = backend.list_commits()
    # ONE new commit records the revert (append-only undo)
    assert commits[0]["subject"] == f"Update: Reverted commit {edit_sha[:7]}."
    assert len(commits) == 4  # init + create + edit + revert


@requires_git
def test_reset_to_commit_is_append_only(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept("/a.md", {"type": "Concept"}, "v1\n")
    first_sha = backend.list_commits()[0]["sha"]
    backend.edit_concept("/a.md", new_body="v2\n")
    pre_reset = backend.git_head()
    backend.reset_to_commit(first_sha)
    assert backend.read_document("/a.md")["body"] == "v1\n"
    assert f"Reset library to {first_sha[:7]}" in read_log(tmp_path / "lib")
    commits = backend.list_commits()
    assert commits[0]["subject"] == f"Update: Reset library to {first_sha[:7]}."
    # append-only: the reset commit's parent is the pre-reset commit, so the
    # pre-reset state stays reachable (undo = another reset)
    assert commits[1]["sha"] == pre_reset
    assert len(commits) == 4


@requires_git
def test_history_refusals_leave_clean_worktree(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept("/a.md", {"type": "Concept"}, "v1\n")
    root_sha = backend.list_commits()[-1]["sha"]
    assert backend.list_commits()[-1]["is_root"] is True
    with pytest.raises(GitError, match="cannot revert the initial commit"):
        backend.revert_commit(root_sha)
    head = backend.git_head()
    with pytest.raises(GitError, match="already at this commit"):
        backend.reset_to_commit(head)
    with pytest.raises(GitError, match="invalid commit sha"):
        backend.revert_commit("../evil")
    # the refused ops staged nothing: HEAD and history are untouched
    assert backend.git_head() == head
    assert len(backend.list_commits()) == 2


def test_git_disabled_backend_writes_without_history(tmp_path):
    backend = LibraryBackend(tmp_path / "lib", actor=ACTOR, git_enabled=False)
    backend.init_bundle()
    backend.create_concept("/a.md", {"type": "Concept"}, "x\n")
    assert backend.history_configured is False
    assert backend.history_available is False
    assert backend.status()["stats"]["versions"] == 0
    assert not (tmp_path / "lib" / ".git").exists()
    assert "**Creation**" in read_log(tmp_path / "lib")
    with pytest.raises(GitError, match="disabled"):
        backend.list_commits()


def test_search_and_browse_reads(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept(
        "/tables/customers.md",
        {"type": "Concept", "title": "Customers", "description": "d"},
        "x\n",
    )
    hits = backend.search_metadata(field="type", value="concept")
    assert [h["id"] for h in hits] == ["/tables/customers"]
    listing = backend.list_dir("/")
    assert any(e["is_directory"] and e["path"] == "/tables" for e in listing)
    sub = backend.list_dir("/tables")
    assert sub[0]["title"] == "Customers"
    assert backend.link_check() == []


# --- did-you-mean suggestions on missing paths (LOOP_GUARDS_2) ----------------------


def test_read_document_missing_suggests_close_match(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept("/athenaeum/x.md", {"type": "Note", "title": "X"}, "x\n")

    with pytest.raises(FileNotFoundError) as exc:
        backend.read_document("/athenaum/x.md")

    assert "no such document: '/athenaum/x.md'" in str(exc.value)
    assert "Did you mean: '/athenaeum/x.md'" in str(exc.value)


def test_read_document_missing_without_match_keeps_plain_message(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept("/athenaeum/x.md", {"type": "Note", "title": "X"}, "x\n")

    with pytest.raises(FileNotFoundError) as exc:
        backend.read_document("/zzz/q.md")

    assert str(exc.value) == "no such document: '/zzz/q.md'"  # no suffix


def test_list_dir_missing_suggests_close_match(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept("/tables/customers.md", {"type": "Concept", "title": "C"}, "x\n")

    with pytest.raises(FileNotFoundError) as exc:
        backend.list_dir("/tabels")

    assert "not a directory: '/tabels'" in str(exc.value)
    assert "Did you mean: '/tables'" in str(exc.value)


def test_missing_path_suggestions_never_fire_on_escape(tmp_path):
    """The enrichment sits strictly behind the isolation screen: a traversal
    attempt still raises PathEscapeError (a ValueError), never a suggestion."""
    backend = make_backend(tmp_path)
    with pytest.raises(ValueError, match="parent traversal"):
        backend.read_document("/../outside.md")


# --- per-file history + restore (DOC_TIMELINE, 0.22.0) ---------------------------


@requires_git
def test_restore_file_from_commit_reverts_edit(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept("/concepts/alpha.md", {"type": "Note", "title": "Alpha"}, "v1\n")
    create_sha = backend.list_commits()[0]["sha"]
    backend.edit_concept("/concepts/alpha.md", new_body="v2\n")
    backend.create_concept("/concepts/beta.md", {"type": "Note", "title": "Beta"}, "beta\n")
    count_before = len(backend.list_commits())

    backend.restore_file_from_commit("/concepts/alpha.md", create_sha)

    assert backend.read_document("/concepts/alpha.md")["body"] == "v1\n"
    commits = backend.list_commits()
    assert len(commits) == count_before + 1  # exactly ONE new commit
    short = create_sha[:7]
    message = f"Restored [Alpha](/concepts/alpha.md) from commit {short}."
    assert commits[0]["subject"] == f"Update: {message}"
    assert f"**Update**: {message}" in read_log(tmp_path / "lib")
    # the log entry lands in the SAME commit as the restore
    assert message in _git(tmp_path / "lib", "show", "HEAD:log.md")
    # a file created after the target commit is untouched
    assert backend.read_document("/concepts/beta.md")["body"] == "beta\n"


@requires_git
def test_restore_file_from_commit_regenerates_index(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept("/concepts/alpha.md", {"type": "Note", "title": "Title A"}, "v1\n")
    create_sha = backend.list_commits()[0]["sha"]
    backend.edit_concept("/concepts/alpha.md", frontmatter_patch={"title": "Title B"})
    index_path = tmp_path / "lib" / "concepts" / "index.md"
    assert "Title B" in index_path.read_text(encoding="utf-8")

    backend.restore_file_from_commit("/concepts/alpha.md", create_sha)

    index_text = index_path.read_text(encoding="utf-8")
    assert "Title A" in index_text
    assert "Title B" not in index_text


@requires_git
def test_restore_file_from_commit_noop_refused(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept("/a.md", {"type": "Note"}, "v1\n")
    sha = backend.list_commits()[0]["sha"]
    count = len(backend.list_commits())
    with pytest.raises(GitError, match="already at this state"):
        backend.restore_file_from_commit("/a.md", sha)
    assert len(backend.list_commits()) == count


@requires_git
def test_restore_file_from_commit_absent_at_sha_refused(tmp_path):
    backend = make_backend(tmp_path)
    init_sha = backend.list_commits()[-1]["sha"]  # init commit predates the file
    backend.create_concept("/a.md", {"type": "Note"}, "v1\n")
    count = len(backend.list_commits())
    with pytest.raises(GitError, match="did not exist"):
        backend.restore_file_from_commit("/a.md", init_sha)
    assert backend.read_document("/a.md")["body"] == "v1\n"  # untouched
    assert len(backend.list_commits()) == count


@requires_git
def test_restore_file_from_commit_unknown_sha(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept("/a.md", {"type": "Note"}, "v1\n")
    with pytest.raises(GitError, match="unknown commit"):
        backend.restore_file_from_commit("/a.md", "deadbeef")


@requires_git
def test_restore_file_from_commit_rename_aware(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept("/a.md", {"type": "Note", "title": "Alpha"}, "original\n")
    create_sha = backend.list_commits()[0]["sha"]
    backend.move_concept("/a.md", "/b.md")
    backend.edit_concept("/b.md", new_body="edited\n")

    # restore the CURRENT path from a pre-move commit: the historical bytes
    # (stored under the old name) land at /b.md
    backend.restore_file_from_commit("/b.md", create_sha)

    assert backend.read_document("/b.md")["body"] == "original\n"
    assert not (tmp_path / "lib" / "a.md").exists()


def test_restore_file_from_commit_git_disabled(tmp_path):
    backend = LibraryBackend(tmp_path / "lib", actor=ACTOR, git_enabled=False)
    backend.init_bundle()
    backend.create_concept("/a.md", {"type": "Note"}, "x\n")
    with pytest.raises(GitError, match="disabled"):
        backend.restore_file_from_commit("/a.md", "deadbeef")


def test_file_history_rejects_reserved(tmp_path):
    backend = make_backend(tmp_path)
    with pytest.raises(ValueError, match="reserved"):
        backend.file_history("/log.md")


@requires_git
def test_git_pull_wires_origin_configured_after_last_write(tmp_path):
    """A remote configured after the last auto-commit is wired at pull time."""
    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))

    lib = tmp_path / "library"
    backend = LibraryBackend(lib, actor="athenaeum-test/0.0.0")
    backend.init_bundle()
    backend.create_concept("/concepts/alpha.md", {"title": "Alpha", "type": "Note"}, "v1\n")

    # seed the bare remote with the same history plus one commit, and leave
    # behind the branch tracking a manual `push -u` would have set
    other = tmp_path / "other"
    _git(tmp_path, "clone", str(lib), str(other))
    _git(other, "config", "user.name", "Test")
    _git(other, "config", "user.email", "test@localhost")
    (other / "pulled.md").write_text("from remote\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "remote commit")
    _git(other, "push", str(bare), "main:main")
    _git(lib, "config", "branch.main.remote", "origin")
    _git(lib, "config", "branch.main.merge", "refs/heads/main")

    # a fresh backend sees the newly configured remote; nothing has
    # auto-committed since, so origin is not wired on disk yet
    with_remote = LibraryBackend(lib, actor="athenaeum-test/0.0.0", git_remote_url=str(bare))
    with_remote.git_pull()  # previously failed with git's "no such remote"

    assert _git(lib, "remote", "get-url", "origin").strip() == str(bare)
    assert (lib / "pulled.md").read_text(encoding="utf-8") == "from remote\n"


# --- literal unicode escape guard (F25) ---


def test_create_decodes_escapes_in_prose(tmp_path):
    """Literal \\uXXXX artifacts in prose are decoded and reported."""
    backend = make_backend(tmp_path)
    result = backend.create_concept("/a.md", {"type": "Concept"}, "DP\\u20111 and \\u2014 done\n")
    doc = backend.read_document("/a.md")
    assert doc["body"] == "DP\u20111 and \u2014 done\n"
    assert "\\u" not in doc["body"]
    warnings = result["warnings"]
    assert len(warnings) == 1
    assert "2 literal unicode escape sequence(s)" in warnings[0]
    assert "\\u2011" in warnings[0]
    assert "\\u2014" in warnings[0]


def test_clean_body_omits_warnings_key(tmp_path):
    """A clean body yields the exact legacy result dict (no warnings key)."""
    backend = make_backend(tmp_path)
    result = backend.create_concept("/a.md", {"type": "Concept"}, "plain ASCII body\n")
    assert result == {"id": "/a", "action": "created"}


def test_escapes_inside_fenced_code_untouched(tmp_path):
    """Fenced code blocks are an escape hatch; prose escapes still decode.

    Warn-always: the fenced content stays byte-identical but earns a
    code-span warning naming the exact line/snippet and the
    allow_literal_escapes hint; a mixed body carries BOTH warnings.
    """
    backend = make_backend(tmp_path)
    only_fence = "```text\nDP\\u20111\n```\n"
    result = backend.create_concept("/a.md", {"type": "Concept"}, only_fence)
    assert backend.read_document("/a.md")["body"] == only_fence
    warnings = result["warnings"]
    assert len(warnings) == 1
    assert "inside code spans/fenced blocks" in warnings[0]
    assert "line 2: DP\\u20111" in warnings[0]
    assert "allow_literal_escapes" in warnings[0]

    mixed = "```text\nDP\\u20111\n```\nprose \\u2014 here\n"
    result = backend.create_concept("/b.md", {"type": "Concept"}, mixed)
    body = backend.read_document("/b.md")["body"]
    assert "DP\\u20111" in body  # fenced content untouched
    assert "prose \u2014 here" in body  # prose decoded
    warnings = result["warnings"]
    assert len(warnings) == 2
    assert "Auto-decoded 1 literal unicode escape sequence(s)" in warnings[0]
    assert "inside code spans/fenced blocks" in warnings[1]
    assert "line 2: DP\\u20111" in warnings[1]


def test_escapes_inside_tilde_fence_untouched(tmp_path):
    """Tilde fences are treated like backtick fences (scan included)."""
    backend = make_backend(tmp_path)
    body = "~~~\nDP\\u20111\n~~~\n"
    result = backend.create_concept("/a.md", {"type": "Concept"}, body)
    assert backend.read_document("/a.md")["body"] == body
    warnings = result["warnings"]
    assert len(warnings) == 1
    assert "inside code spans/fenced blocks" in warnings[0]
    assert "line 2: DP\\u20111" in warnings[0]


def test_escapes_inside_inline_code_untouched(tmp_path):
    """Inline code spans (single- and multi-backtick) pass through literal."""
    backend = make_backend(tmp_path)
    only_spans = "use `\\u2011` and `` `\\u2014` `` here\n"
    result = backend.create_concept("/a.md", {"type": "Concept"}, only_spans)
    assert backend.read_document("/a.md")["body"] == only_spans
    warnings = result["warnings"]
    assert len(warnings) == 1
    assert "2 literal unicode escape(s) inside code spans/fenced blocks" in warnings[0]
    assert "line 1: use `\\u2011` and `` `\\u2014` `` here" in warnings[0]

    mixed = "prose \\u2013 then `\\u2011` span\n"
    result = backend.create_concept("/b.md", {"type": "Concept"}, mixed)
    body = backend.read_document("/b.md")["body"]
    assert body == "prose \u2013 then `\\u2011` span\n"
    warnings = result["warnings"]
    assert len(warnings) == 2
    assert "Auto-decoded 1 literal unicode escape sequence(s)" in warnings[0]
    assert "inside code spans/fenced blocks" in warnings[1]


def test_ascii_target_escape_decoded(tmp_path):
    """ALL \\uXXXX escapes decode, even ones targeting ASCII characters."""
    backend = make_backend(tmp_path)
    result = backend.create_concept("/a.md", {"type": "Concept"}, "\\u0041BC\n")
    assert backend.read_document("/a.md")["body"] == "ABC\n"
    assert "warnings" in result


def test_surrogate_escape_left_literal(tmp_path):
    """Surrogate-range escapes (U+D800-DFFF) stay literal and are reported."""
    backend = make_backend(tmp_path)
    result = backend.create_concept("/a.md", {"type": "Concept"}, "x \\ud800 y\n")
    assert backend.read_document("/a.md")["body"] == "x \\ud800 y\n"
    warnings = result["warnings"]
    assert len(warnings) == 1
    assert "Skipped 1 escape(s)" in warnings[0]
    assert "\\ud800" in warnings[0]


def test_escaped_backslash_escape_stays_literal(tmp_path):
    """LIBRARY-06: an escaped backslash must not decode — ``\\u2011`` with an
    EVEN backslash run is an intentional literal, not an artifact."""
    backend = make_backend(tmp_path)
    body = "an escaped \\\\u2011 stays literal\n"  # two literal backslashes
    result = backend.create_concept("/a.md", {"type": "Concept"}, body)
    assert backend.read_document("/a.md")["body"] == body
    assert result == {"id": "/a", "action": "created"}  # no decode, no warnings


def test_odd_backslash_run_decodes_final_escape(tmp_path):
    """LIBRARY-06: pairs collapse, then the ODD run's final ``\\uXXXX``
    decodes — ``\\\\\\u2011`` (3 backslashes) becomes ``\\`` + U+2011."""
    backend = make_backend(tmp_path)
    result = backend.create_concept("/a.md", {"type": "Concept"}, "x \\\\\\u2011 y\n")
    assert backend.read_document("/a.md")["body"] == "x \\" + "\u2011" + " y\n"
    warnings = result["warnings"]
    assert len(warnings) == 1
    assert "Auto-decoded 1 literal unicode escape sequence(s)" in warnings[0]


def test_code_span_scan_parity_even_run_not_reported(tmp_path):
    """LIBRARY-06: the code-span scan applies the same parity — an
    escaped-backslash sequence inside a code span is literal, not a
    candidate (candidate counting agrees with decoding)."""
    backend = make_backend(tmp_path)
    body = "use `\\\\u2011` here\n"  # two literal backslashes in the span
    result = backend.create_concept("/a.md", {"type": "Concept"}, body)
    assert backend.read_document("/a.md")["body"] == body
    assert result == {"id": "/a", "action": "created"}  # no warnings at all


def test_edit_decodes_new_body(tmp_path):
    """edit_concept decodes escapes in a supplied new_body."""
    backend = make_backend(tmp_path)
    backend.create_concept("/a.md", {"type": "Concept"}, "clean\n")
    result = backend.edit_concept("/a.md", new_body="x \\u2013 y\n")
    assert backend.read_document("/a.md")["body"] == "x \u2013 y\n"
    warnings = result["warnings"]
    assert len(warnings) == 1
    assert "1 literal unicode escape sequence(s)" in warnings[0]


def test_edit_without_body_leaves_existing_body_untouched(tmp_path):
    """A body-less edit never rescans the existing on-disk body."""
    backend = make_backend(tmp_path)
    (tmp_path / "lib" / "a.md").write_text(
        fm_mod.dump_document({"type": "Concept"}, "dirty \\u2011 body\n"),
        encoding="utf-8",
    )
    result = backend.edit_concept("/a.md", frontmatter_patch={"title": "T"})
    assert backend.read_document("/a.md")["body"] == "dirty \\u2011 body\n"
    assert result == {"id": "/a", "action": "updated"}


async def test_dispatch_result_carries_warning(tmp_path):
    """The model-facing dispatch result carries the decode warning."""
    result = await dispatch(
        "write_concept",
        {"path": "/b.md", "frontmatter": {"type": "Concept"}, "body": "a \\u2011 b\n"},
        make_backend(tmp_path),
    )
    assert "warnings" in result
    assert "Auto-decoded 1 literal unicode escape sequence(s)" in json.dumps(result)


def test_allow_literal_escapes_suppresses_code_span_warning(tmp_path):
    """allow_literal_escapes=True skips the code-span scan entirely on create
    and edit; prose escapes in a mixed body still decode + warn (the flag
    covers only code spans/fences)."""
    backend = make_backend(tmp_path)
    only_fence = "```text\nDP\\u20111\n```\n"
    result = backend.create_concept(
        "/a.md", {"type": "Concept"}, only_fence, allow_literal_escapes=True
    )
    assert backend.read_document("/a.md")["body"] == only_fence
    assert result == {"id": "/a", "action": "created"}

    backend.create_concept("/b.md", {"type": "Concept"}, "clean\n")
    result = backend.edit_concept("/b.md", new_body=only_fence, allow_literal_escapes=True)
    assert backend.read_document("/b.md")["body"] == only_fence
    assert result == {"id": "/b", "action": "updated"}

    mixed = "```text\nDP\\u20111\n```\nprose \\u2014 here\n"
    result = backend.create_concept("/c.md", {"type": "Concept"}, mixed, allow_literal_escapes=True)
    body = backend.read_document("/c.md")["body"]
    assert "DP\\u20111" in body  # fenced content untouched
    assert "prose \u2014 here" in body  # prose decoded
    warnings = result["warnings"]
    assert len(warnings) == 1
    assert "Auto-decoded 1 literal unicode escape sequence(s)" in warnings[0]


async def test_dispatch_allow_literal_escapes_suppresses_warning(tmp_path):
    """allow_literal_escapes passes schema/dispatch and suppresses the
    code-span warning (pins the tools.py threading)."""
    backend = make_backend(tmp_path)
    only_fence = "```text\nDP\\u20111\n```\n"
    result = await dispatch(
        "write_concept",
        {
            "path": "/b.md",
            "frontmatter": {"type": "Concept"},
            "body": only_fence,
            "allow_literal_escapes": True,
        },
        backend,
    )
    assert result == {"id": "/b", "action": "created"}
    assert backend.read_document("/b.md")["body"] == only_fence


# --- escape artifact stock scan (F25 curator hygiene sweep) ---


def test_escape_artifact_scan_finds_dirty_body(tmp_path):
    """A concept file with a literal escape in prose is reported, RAW body."""
    backend = make_backend(tmp_path)
    dirty = "dirty \\u2011 body\n"
    (tmp_path / "lib" / "a.md").write_text(
        fm_mod.dump_document({"type": "Concept", "title": "A"}, dirty),
        encoding="utf-8",
    )
    entries = backend.escape_artifact_scan()
    assert entries == [{"path": "/a.md", "title": "A", "body": dirty}]
    # the scan is read-only: the on-disk body is still escaped
    assert backend.read_document("/a.md")["body"] == dirty


def test_escape_artifact_scan_skips_fence_and_span_only(tmp_path):
    """Escapes confined to fences/inline code spans are exempt (pre-filter pin:
    decode-compare, not a bare regex)."""
    backend = make_backend(tmp_path)
    (tmp_path / "lib" / "a.md").write_text(
        fm_mod.dump_document({"type": "Concept"}, "```text\nDP\\u20111\n```\n"),
        encoding="utf-8",
    )
    (tmp_path / "lib" / "b.md").write_text(
        fm_mod.dump_document({"type": "Concept"}, "use `\\u2011` span\n"),
        encoding="utf-8",
    )
    assert backend.escape_artifact_scan() == []


def test_escape_artifact_scan_skips_surrogate_only(tmp_path):
    """Surrogate-only escapes stay literal: decoding changes nothing, so the
    file is never reported (prevents repair-noop loops)."""
    backend = make_backend(tmp_path)
    (tmp_path / "lib" / "a.md").write_text(
        fm_mod.dump_document({"type": "Concept"}, "x \\ud800 y\n"),
        encoding="utf-8",
    )
    assert backend.escape_artifact_scan() == []


def test_escape_artifact_scan_clean_library(tmp_path):
    """Clean concepts are never reported."""
    backend = make_backend(tmp_path)
    backend.create_concept("/a.md", {"type": "Concept"}, "plain ASCII body\n")
    backend.create_concept("/b.md", {"type": "Concept"}, "unicode \u2011 direct\n")
    assert backend.escape_artifact_scan() == []


# --- code-span escape candidates scan (LLM-judged curator finding) ---


def test_code_span_candidates_scan_finds_fence_and_span(tmp_path):
    """Fence + inline-span occurrences are reported with correct 1-based
    lines and stripped snippets; the scan is read-only."""
    backend = make_backend(tmp_path)
    (tmp_path / "lib" / "a.md").write_text(
        fm_mod.dump_document(
            {"type": "Concept", "title": "A"},
            "intro prose\n```text\nDP\\u20111\n```\nuse `\\u2014` span\n",
        ),
        encoding="utf-8",
    )
    entries = backend.code_span_escape_candidates()
    assert entries == [
        {
            "path": "/a.md",
            "title": "A",
            "occurrences": [
                {"line": 3, "snippet": "DP\\u20111"},
                {"line": 5, "snippet": "use `\\u2014` span"},
            ],
        }
    ]
    # read-only: the on-disk body still holds the literals
    assert "\\u2011" in backend.read_document("/a.md")["body"]


def test_code_span_candidates_scan_clean_and_prose_only(tmp_path):
    """Clean files and prose-only escapes yield no candidates (prose belongs
    to the deterministic decode path)."""
    backend = make_backend(tmp_path)
    backend.create_concept("/a.md", {"type": "Concept"}, "plain ASCII body\n")
    (tmp_path / "lib" / "b.md").write_text(
        fm_mod.dump_document({"type": "Concept"}, "dirty \\u2011 prose\n"),
        encoding="utf-8",
    )
    assert backend.code_span_escape_candidates() == []


def test_code_span_candidates_scan_skips_unreadable_file(tmp_path):
    """An unparseable concept file is skipped, never fatal."""
    backend = make_backend(tmp_path)
    (tmp_path / "lib" / "a.md").write_text(
        fm_mod.dump_document({"type": "Concept"}, "x `\\u2011` y\n"),
        encoding="utf-8",
    )
    (tmp_path / "lib" / "broken.md").write_bytes(b"\xff\xfe invalid utf-8")
    entries = backend.code_span_escape_candidates()
    assert [e["path"] for e in entries] == ["/a.md"]


def test_code_span_candidates_scan_reports_surrogate_in_span(tmp_path):
    """Surrogate-range escapes inside code spans ARE reported (the LLM
    judges; the scanner does not special-case them)."""
    backend = make_backend(tmp_path)
    (tmp_path / "lib" / "a.md").write_text(
        fm_mod.dump_document({"type": "Concept"}, "x `\\ud800` y\n"),
        encoding="utf-8",
    )
    entries = backend.code_span_escape_candidates()
    assert entries == [
        {
            "path": "/a.md",
            "title": "",
            "occurrences": [{"line": 1, "snippet": "x `\\ud800` y"}],
        }
    ]


def test_scan_code_span_escapes_bounds_occurrences():
    """Occurrences per body are capped at MAX_CODE_SPAN_OCCURRENCES_PER_FILE
    (first-N wins)."""
    body = "\n".join(f"`\\u20{i:02x}`" for i in range(15)) + "\n"
    occurrences = escape_guard_mod.scan_code_span_escapes(body)
    assert len(occurrences) == escape_guard_mod.MAX_CODE_SPAN_OCCURRENCES_PER_FILE
    assert [o["line"] for o in occurrences] == list(range(1, 11))


def test_scan_code_span_escapes_truncates_snippet():
    """Snippets are capped at MAX_CODE_SPAN_SNIPPET chars."""
    body = "`" + "x" * 200 + "\\u2011`\n"
    occurrences = escape_guard_mod.scan_code_span_escapes(body)
    assert len(occurrences) == 1
    assert len(occurrences[0]["snippet"]) == escape_guard_mod.MAX_CODE_SPAN_SNIPPET


def test_code_span_candidates_scan_bounds_files(tmp_path):
    """Candidate files are capped at MAX_CODE_SPAN_CANDIDATE_FILES."""
    backend = make_backend(tmp_path)
    lib = tmp_path / "lib"
    for i in range(25):
        (lib / f"f{i:02d}.md").write_text(
            fm_mod.dump_document({"type": "Concept"}, "x `\\u2011` y\n"),
            encoding="utf-8",
        )
    entries = backend.code_span_escape_candidates()
    assert len(entries) == escape_guard_mod.MAX_CODE_SPAN_CANDIDATE_FILES
