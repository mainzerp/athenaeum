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
