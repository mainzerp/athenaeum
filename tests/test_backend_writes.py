"""Tests for athenaeum.library.backend.LibraryBackend compound writes."""

import pytest

from athenaeum.library.backend import LibraryBackend, provision_library
from athenaeum.library.frontmatter import split_document

ACTOR = "athenaeum-librarian/0.1.0"


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


def test_compound_write_produces_concept_index_log_snapshot(tmp_path):
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
    # snapshot pre-image recorded
    assert (root / ".athenaeum" / "versions" / "000001" / "meta.json").exists()
    # no leftover .tmp files
    assert not list(root.rglob("*.tmp"))


def test_reserved_names_refused(tmp_path):
    backend = make_backend(tmp_path)
    for path in ("/index.md", "/log.md", "/sub/index.md", "/sub/log.md"):
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


def test_verify_concept_snapshot_and_log(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept("/a.md", {"type": "Concept", "title": "A"}, "x\n")
    backend.verify_concept("/a.md", by="athenaeum-curator/0.2.0", agent_label="bot-1")
    root = tmp_path / "lib"
    assert (root / ".athenaeum" / "versions" / "000002" / "meta.json").exists()
    log = read_log(root)
    assert "**Verification**" in log
    assert "(verifier: athenaeum-curator/0.2.0)" in log
    assert "(requested by agent:bot-1)" in log


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
    # outside the compound write: no log entry, no snapshot, no index drift
    assert read_log(root) == log_before
    assert backend.validate()["errors"] == []
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


def test_rollback_restores_concept_and_logs(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept("/a.md", {"type": "Concept"}, "v1\n")
    backend.edit_concept("/a.md", new_body="v2\n")
    assert backend.read_document("/a.md")["body"] == "v2\n"
    backend.rollback(2)
    assert backend.read_document("/a.md")["body"] == "v1\n"
    assert "Rolled back to version 000002" in read_log(tmp_path / "lib")


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
