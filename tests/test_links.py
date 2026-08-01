"""Tests for athenaeum.library.links."""

from athenaeum.library.links import (
    broken_links,
    extract_frontmatter_links,
    inbound_links,
    resolve_target,
    rewrite_links,
)

FM = "---\ntype: Concept\n---\n"


def write(root, rel, text):
    path = root / rel.lstrip("/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_absolute_link_rewrite(tmp_path):
    write(tmp_path, "/a.md", FM + "see [B](/b.md) and [B again](/b.md).\n")
    write(tmp_path, "/b.md", FM + "b\n")
    count = rewrite_links(tmp_path, "/b.md", "/moved/b.md")
    assert count == 2
    body = (tmp_path / "a.md").read_text(encoding="utf-8")
    assert "(/moved/b.md)" in body
    assert "(/b.md)" not in body


def test_relative_links_untouched_by_move(tmp_path):
    write(tmp_path, "/a.md", FM + "see [B](./b.md).\n")
    write(tmp_path, "/b.md", FM + "b\n")
    count = rewrite_links(tmp_path, "/b.md", "/x/b.md")
    assert count == 0
    assert "(./b.md)" in (tmp_path / "a.md").read_text(encoding="utf-8")


def test_frontmatter_resource_rewrite(tmp_path):
    text = "---\ntype: Concept\nresource: /b.md\ncustom_key: keep\n---\nbody\n"
    write(tmp_path, "/a.md", text)
    count = rewrite_links(tmp_path, "/b.md", "/c.md")
    assert count == 1
    out = (tmp_path / "a.md").read_text(encoding="utf-8")
    assert "resource: /c.md" in out
    assert "custom_key: keep" in out


def test_anchored_body_link_rewrite(tmp_path):
    """L18: anchored/query targets match on their path part, anchor preserved."""
    write(
        tmp_path,
        "/a.md",
        FM + "see [B](/b.md#section) and [B raw](/b.md?raw=1#top).\n",
    )
    write(tmp_path, "/b.md", FM + "b\n")
    count = rewrite_links(tmp_path, "/b.md", "/moved/b.md")
    assert count == 2
    body = (tmp_path / "a.md").read_text(encoding="utf-8")
    assert "(/moved/b.md#section)" in body
    assert "(/moved/b.md?raw=1#top)" in body
    assert "(/b.md" not in body


def test_anchored_frontmatter_resource_rewrite(tmp_path):
    text = "---\ntype: Concept\nresource: /b.md#frag\n---\nbody\n"
    write(tmp_path, "/a.md", text)
    count = rewrite_links(tmp_path, "/b.md", "/c.md")
    assert count == 1
    assert "resource: /c.md#frag" in (tmp_path / "a.md").read_text(encoding="utf-8")


def test_non_matching_anchors_untouched(tmp_path):
    write(tmp_path, "/a.md", FM + "[other](/other.md#b.md) [prefix](/b.mdx#s)\n")
    write(tmp_path, "/b.md", FM + "b\n")
    count = rewrite_links(tmp_path, "/b.md", "/moved/b.md")
    assert count == 0
    body = (tmp_path / "a.md").read_text(encoding="utf-8")
    assert "(/other.md#b.md)" in body
    assert "(/b.mdx#s)" in body


def test_broken_link_detection(tmp_path):
    write(
        tmp_path,
        "/a.md",
        FM + "[missing](/missing.md) [ok](/b.md) [ext](https://example.com/x)\n",
    )
    write(tmp_path, "/b.md", FM + "b\n")
    assert broken_links(tmp_path) == [{"source": "/a.md", "target": "/missing.md"}]


def test_resolve_target_relative_and_absolute():
    assert resolve_target("/tables/a.md", "/x.md") == "/x.md"
    assert resolve_target("/tables/a.md", "./b.md") == "/tables/b.md"
    assert resolve_target("/tables/a.md", "../c.md") == "/c.md"
    assert resolve_target("/tables/a.md", "/x.md#frag") == "/x.md"


def test_inbound_links(tmp_path):
    write(tmp_path, "/a.md", FM + "[t](/t.md)\n")
    write(tmp_path, "/sub/b.md", FM + "[t](/t.md)\n")
    write(tmp_path, "/t.md", FM + "t\n")
    assert sorted(inbound_links(tmp_path, "/t.md")) == ["/a.md", "/sub/b.md"]


def test_extract_frontmatter_links():
    fm = {
        "resource": "/r.md",
        "sources": [{"resource": "/s1.md"}, {"id": "x"}, {"resource": "https://e.com"}],
        "executor": {"resource": "/ex.md"},
        "attester": {"resource": "/at.md"},
    }
    targets = extract_frontmatter_links(fm)
    assert targets == ["/r.md", "/s1.md", "https://e.com", "/ex.md", "/at.md"]
