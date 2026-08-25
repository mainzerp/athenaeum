"""Tests for athenaeum.library.links."""

import os

import pytest

from athenaeum.library.links import (
    broken_links,
    extract_body_links,
    extract_frontmatter_links,
    inbound_links,
    iter_concept_files,
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


# --- image syntax is not a concept link (LINK_RE lookbehind) -------------------


def test_image_syntax_produces_no_extracted_link():
    assert extract_body_links("![alt](/x.png)\n") == []
    # concept links around an image are unaffected
    assert extract_body_links("see [B](/b.md) and ![img](/i.png)\n") == ["/b.md"]


def test_image_not_rewritten_on_move(tmp_path):
    write(tmp_path, "/a.md", FM + "![alt](/x.png) and [B](/b.md).\n")
    write(tmp_path, "/b.md", FM + "b\n")
    count = rewrite_links(tmp_path, "/b.md", "/moved/b.md")
    assert count == 1
    body = (tmp_path / "a.md").read_text(encoding="utf-8")
    assert "![alt](/x.png)" in body  # image untouched
    assert "(/moved/b.md)" in body


def test_image_target_move_leaves_image_untouched(tmp_path):
    """A move of the image's own 'target' rewrites nothing: assets are not
    graph citizens, so there is no move-rewrite for them."""
    write(tmp_path, "/a.md", FM + "![alt](/x.png)\n")
    count = rewrite_links(tmp_path, "/x.png", "/y.png")
    assert count == 0
    assert "![alt](/x.png)" in (tmp_path / "a.md").read_text(encoding="utf-8")


def test_linked_image_construct_yields_no_page_link():
    """v1 accepted edge: ``[![a](/i.png)](/a.md)`` — the outer ``[...](...)``
    match consumes the image part, so no link to the page target is extracted."""
    assert extract_body_links("[![a](/i.png)](/a.md)") == ["/i.png"]


# --- symlink escape probes (resolve_under confinement) -------------------------


def test_broken_links_symlink_escape_reported_broken(tmp_path):
    """A link pointing outside the root via a symlinked directory is reported
    broken — the probe is confined by resolve_under, never follows the escape,
    and never crashes (test_isolation.py pattern)."""
    outside = tmp_path.parent / (tmp_path.name + "_outside")
    outside.mkdir(exist_ok=True)
    secret = outside / "secret.md"
    secret.write_text(FM + "secret\n", encoding="utf-8")
    try:
        os.symlink(outside, tmp_path / "link", target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not permitted on this host")
    write(tmp_path, "/a.md", FM + "[s](/link/secret.md)\n")
    assert broken_links(tmp_path) == [{"source": "/a.md", "target": "/link/secret.md"}]
    assert secret.read_text(encoding="utf-8").endswith("secret\n")  # untouched


def test_iter_concept_files_skips_symlinked_dir(tmp_path):
    """LIBRARY-02: a symlinked dir pointing outside the root is neither
    yielded nor recursed — the escape's content never becomes a concept."""
    outside = tmp_path.parent / (tmp_path.name + "_outside")
    outside.mkdir(exist_ok=True)
    (outside / "secret.md").write_text(FM + "secret\n", encoding="utf-8")
    try:
        os.symlink(outside, tmp_path / "escape", target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not permitted on this host")
    write(tmp_path, "/a.md", FM + "a\n")
    assert [bundle for bundle, _ in iter_concept_files(tmp_path)] == ["/a.md"]


def test_iter_concept_files_skips_symlinked_file(tmp_path):
    """LIBRARY-02: a symlinked .md file (even one resolving outside the
    root) is never yielded."""
    outside = tmp_path.parent / (tmp_path.name + "_outside")
    outside.mkdir(exist_ok=True)
    secret = outside / "secret.md"
    secret.write_text(FM + "secret\n", encoding="utf-8")
    try:
        os.symlink(secret, tmp_path / "linked.md")
    except OSError:
        pytest.skip("symlink creation not permitted on this host")
    write(tmp_path, "/a.md", FM + "a\n")
    assert [bundle for bundle, _ in iter_concept_files(tmp_path)] == ["/a.md"]


def test_iter_concept_files_symlink_cycle_does_not_raise(tmp_path):
    """LIBRARY-02: a symlink cycle inside the root can neither hang nor
    crash the scan (os.walk followlinks=False)."""
    (tmp_path / "sub").mkdir()
    try:
        os.symlink(tmp_path, tmp_path / "sub" / "cycle", target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not permitted on this host")
    write(tmp_path, "/sub/a.md", FM + "a\n")
    assert [bundle for bundle, _ in iter_concept_files(tmp_path)] == ["/sub/a.md"]


# --- F27: links in inline code spans / fenced blocks are not concept links ---


def test_extract_body_links_skips_inline_code_spans():
    """F27: a link inside an inline code span is example markup; prose links
    around it are still extracted."""
    body = "see [A](/a.md) and `[B](/b.md)` and [C](/c.md).\n"
    assert extract_body_links(body) == ["/a.md", "/c.md"]


def test_extract_body_links_skips_fenced_blocks():
    """F27: a link inside a backtick-fenced block is example markup."""
    body = "prose [A](/a.md)\n```\n[B](/b.md)\n```\nmore [C](/c.md)\n"
    assert extract_body_links(body) == ["/a.md", "/c.md"]


def test_extract_body_links_skips_tilde_fences():
    """F27: tilde fences are code blocks too."""
    body = "~~~python\nx = '[B](/b.md)'\n~~~\n[A](/a.md)\n"
    assert extract_body_links(body) == ["/a.md"]


def test_extract_body_links_unclosed_fence_swallows_rest():
    """F27: an unclosed fence swallows the rest of the body — links after it
    are code, matching the fence state machine contract."""
    body = "[A](/a.md)\n```\n[B](/b.md)\n[C](/c.md)\n"
    assert extract_body_links(body) == ["/a.md"]


def test_extract_body_links_multi_backtick_spans():
    """F27: a code span opened by a run of length N closes only at the next
    run of EXACTLY length N; single backticks inside are span content."""
    body = "`` `[B](/b.md)` `` and [A](/a.md)\n"
    assert extract_body_links(body) == ["/a.md"]


def test_rewrite_skips_code_spans_and_fences_round_trip(tmp_path):
    """F27 symmetry pin: move rewrites prose links but leaves code-span and
    fenced example markup byte-untouched; a body without matching links
    round-trips byte-identical."""
    write(
        tmp_path,
        "/a.md",
        FM + "see [B](/b.md)\n`[B](/b.md)`\n```\n[B](/b.md)\n```\n",
    )
    write(tmp_path, "/b.md", FM + "b\n")
    count = rewrite_links(tmp_path, "/b.md", "/moved/b.md")
    assert count == 1
    body = (tmp_path / "a.md").read_text(encoding="utf-8")
    assert "see [B](/moved/b.md)" in body
    assert body.count("`[B](/b.md)`") == 1
    assert "```\n[B](/b.md)\n```" in body
    # no match anywhere: byte-identical round-trip, no rewrite reported
    count = rewrite_links(tmp_path, "/unrelated.md", "/x.md")
    assert count == 0
    assert (tmp_path / "a.md").read_text(encoding="utf-8") == body
