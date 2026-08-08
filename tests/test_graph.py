"""Tests for athenaeum.webui.graph (universe payload containment)."""

from athenaeum.library.backend import LibraryBackend
from athenaeum.webui.graph import build_universe


def test_build_universe_skips_concept_with_broken_frontmatter(tmp_path):
    """SERVER-05: one unreadable concept must not kill the whole payload."""
    backend = LibraryBackend(tmp_path / "lib", actor="test", git_enabled=False)
    backend.init_bundle()
    backend.create_concept(
        "/good.md", {"title": "Good", "type": "Note"}, "See [Broken](/broken.md).\n"
    )
    # split_document raises FrontmatterError on this (unclosed flow mapping)
    (tmp_path / "lib" / "broken.md").write_text(
        "---\ntitle: [unclosed\n---\nbroken body\n", encoding="utf-8"
    )
    payload = build_universe(backend)
    ids = [node["id"] for node in payload["nodes"]]
    assert "/good" in ids
    assert "/broken" not in ids
    # every edge endpoint stays a node id (the skipped node is not linkable)
    for edge in payload["edges"]:
        assert edge["source"] in ids
        assert edge["target"] in ids


def test_build_universe_all_readable_unchanged(tmp_path):
    """The happy path still emits one node per concept, both metrics."""
    backend = LibraryBackend(tmp_path / "lib", actor="test", git_enabled=False)
    backend.init_bundle()
    backend.create_concept("/a.md", {"title": "A", "type": "Note"}, "See [B](/b.md).\n")
    backend.create_concept("/b.md", {"title": "B", "type": "Note"}, "B body.\n")
    for metric in ("link_density", "recency"):
        payload = build_universe(backend, metric)
        assert payload["metric"] == metric
        assert sorted(node["id"] for node in payload["nodes"]) == ["/a", "/b"]
        assert payload["edges"] == [{"source": "/a", "target": "/b"}]
