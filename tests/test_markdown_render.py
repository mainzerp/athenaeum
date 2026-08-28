"""Unit tests for the server-side markdown/diff renderer (webui.markdown_render)."""

from athenaeum.webui import markdown_render


def test_render_markdown_table():
    html = markdown_render.render_markdown("| a | b |\n|---|---|\n| 1 | 2 |\n")
    assert "<table>" in html
    assert "<td>1</td>" in html


def test_render_markdown_strikethrough():
    html = markdown_render.render_markdown("~~gone~~")
    assert "<s>gone</s>" in html


def test_render_markdown_tasklist():
    html = markdown_render.render_markdown("- [ ] todo\n- [x] done\n")
    assert 'class="task-list-item"' in html
    assert "disabled" in html
    assert 'type="checkbox"' in html


def test_render_markdown_fenced_code_highlighted():
    html = markdown_render.render_markdown("```python\nprint('hi')\n```\n")
    assert 'class="highlight"' in html
    assert "print" in html


def test_render_markdown_unknown_language_falls_back():
    html = markdown_render.render_markdown("```nosuchlang\nx < y\n```\n")
    assert 'class="highlight"' in html
    assert "x &lt; y" in html


def test_render_markdown_escapes_raw_html():
    html = markdown_render.render_markdown("before <script>alert(1)</script> after")
    assert "&lt;script&gt;" in html
    assert "<script>" not in html


def test_render_diff_html_line_classes():
    patch = (
        "diff --git a/x.md b/x.md\n"
        "index 111..222 333\n"
        "--- a/x.md\n"
        "+++ b/x.md\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
        " ctx\n"
    )
    html = markdown_render.render_diff_html(patch)
    assert '<div class="diff-view">' in html
    assert '<span class="diff-meta">diff --git a/x.md b/x.md</span>' in html
    assert '<span class="diff-meta">--- a/x.md</span>' in html
    assert '<span class="diff-meta">+++ b/x.md</span>' in html
    assert '<span class="diff-hunk">@@ -1,1 +1,1 @@</span>' in html
    assert '<span class="diff-del">-old</span>' in html
    assert '<span class="diff-add">+new</span>' in html
    assert '<span class="diff-ctx"> ctx</span>' in html


def test_render_diff_html_escapes_html():
    html = markdown_render.render_diff_html("+<b>bold</b>\n")
    assert "&lt;b&gt;" in html
    assert "<b>" not in html


def test_render_diff_html_empty():
    assert markdown_render.render_diff_html("") == ""


def test_render_inline_diff_html_empty():
    assert markdown_render.render_inline_diff_html("") == ""


def test_render_inline_diff_html_line_classification():
    patch = (
        "diff --git a/x.md b/x.md\n"
        "index 111..222 333\n"
        "--- a/x.md\n"
        "+++ b/x.md\n"
        "@@ -1,2 +1,2 @@\n"
        " intro **text**\n"
        "-old line\n"
        "+new line\n"
        "\\ No newline at end of file\n"
    )
    html = markdown_render.render_inline_diff_html(patch)
    assert '<div class="diff-inline">' in html
    assert "<p>intro <strong>text</strong></p>" in html
    assert '<div class="diff-del-block"><p>old line</p>\n</div>' in html
    assert '<div class="diff-add-block"><p>new line</p>\n</div>' in html


def test_render_inline_diff_html_drops_hunk_and_meta_lines():
    patch = (
        "diff --git a/x.md b/y.md\n"
        "similarity index 90%\n"
        "rename from a/x.md\n"
        "rename to b/y.md\n"
        "index 111..222 333\n"
        "--- a/x.md\n"
        "+++ b/y.md\n"
        "@@ -1 +1 @@\n"
        "+content\n"
    )
    html = markdown_render.render_inline_diff_html(patch)
    assert "@@" not in html
    assert "diff --git" not in html
    assert "index" not in html
    assert "rename" not in html
    assert "a/x.md" not in html
    assert "b/y.md" not in html
    assert '<div class="diff-add-block"><p>content</p>\n</div>' in html


def test_render_inline_diff_html_escapes_html():
    html = markdown_render.render_inline_diff_html("+<script>alert(1)</script>\n")
    assert "&lt;script&gt;" in html
    assert "<script>" not in html


def test_all_renderers_escape_contract():
    """F31 invariant: every server producer of body_html / diff_html escapes
    raw input (document_view.js assigns both to innerHTML)."""
    payload = "<script>alert(1)</script>"
    # body path (render_markdown)
    body_html = markdown_render.render_markdown(f"before {payload} after")
    assert "<script>" not in body_html
    assert "&lt;script&gt;" in body_html
    # per-line diff path (render_diff_html: add / del / ctx lines)
    per_line = markdown_render.render_diff_html(f"+{payload}\n-{payload}\n {payload}\n")
    assert "<script>" not in per_line
    assert "&lt;script&gt;" in per_line
    # grouped-block inline diff path (render_inline_diff_html: add / del / ctx)
    inline = markdown_render.render_inline_diff_html(f"+{payload}\n-{payload}\n {payload}\n")
    assert "<script>" not in inline
    assert "&lt;script&gt;" in inline


def test_cached_inline_diff_renders_once_on_repeat():
    """V14: a repeated (path, sha, head_sha) lookup returns identical bytes
    without a second git+render pass; a new head is a new cache entry."""
    calls = []

    def render():
        calls.append(1)
        return "<div>diff</div>"

    key = ("/cache-test.md", "s" * 40, "h" * 40)
    try:
        first = markdown_render.cached_inline_diff(*key, render)
        second = markdown_render.cached_inline_diff(*key, render)
        assert first == second == "<div>diff</div>"
        assert len(calls) == 1
        # head_sha in the key: every commit invalidates automatically
        markdown_render.cached_inline_diff("/cache-test.md", "s" * 40, "i" * 40, render)
        assert len(calls) == 2
    finally:
        markdown_render._inline_diff_cache.pop(key, None)
        markdown_render._inline_diff_cache.pop(("/cache-test.md", "s" * 40, "i" * 40), None)
