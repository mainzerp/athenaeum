"""Server-side markdown and diff rendering for the WebUI.

Document bodies are rendered with markdown-it-py (``default`` preset:
CommonMark + tables + strikethrough, ``html=False`` so raw HTML in the
source is escaped, not passed through) plus the tasklists plugin; fenced
code blocks are highlighted with Pygments. Unified diffs are rendered as
per-line colored spans or as an in-flow document diff. All renderers escape
all raw input, so their output is safe to emit with ``| safe`` in templates
and to assign to ``innerHTML`` in document_view.js (the F31 escape
invariant: new producers MUST escape or use textContent).
"""

from __future__ import annotations

import html

from markdown_it import MarkdownIt
from mdit_py_plugins.tasklists import tasklists_plugin
from pygments import highlight as pygments_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

_md_instance: MarkdownIt | None = None

# Rendered inline-diff cache (V14): (path, sha, head_sha) -> rendered HTML.
# head_sha in the key gives automatic invalidation on every commit — no
# explicit invalidation needed. FIFO eviction at 128 entries (insertion-
# ordered dict, oldest first). Consulted by routes_library.document_diff so a
# repeated request skips BOTH the git call and the render pass.
_inline_diff_cache: dict[tuple[str, str, str], str] = {}
_INLINE_DIFF_CACHE_MAX = 128


def cached_inline_diff(path: str, sha: str, head_sha: str, render) -> str:
    """Return the cached inline-diff HTML for (path, sha, head_sha), calling
    ``render()`` (git diff + render pass) only on a miss."""
    key = (path, sha, head_sha)
    cached = _inline_diff_cache.get(key)
    if cached is not None:
        return cached
    rendered = render()
    if len(_inline_diff_cache) >= _INLINE_DIFF_CACHE_MAX:
        # FIFO: evict the oldest inserted entry (insertion-ordered dict).
        _inline_diff_cache.pop(next(iter(_inline_diff_cache)))
    _inline_diff_cache[key] = rendered
    return rendered


def _highlight(code: str, lang: str, attrs: str) -> str:
    """Pygments highlight callback for fenced code blocks.

    Unknown languages fall back to a plain escaped ``<pre>`` block.
    """
    try:
        lexer = get_lexer_by_name(lang)
    except ClassNotFound:
        return f'<pre class="highlight"><code>{html.escape(code)}</code></pre>\n'
    return pygments_highlight(code, lexer, HtmlFormatter(nowrap=False))


def _md() -> MarkdownIt:
    """Lazily built shared MarkdownIt instance (thread-safe to reuse)."""
    global _md_instance
    if _md_instance is None:
        _md_instance = MarkdownIt("default", {"highlight": _highlight}).use(tasklists_plugin)
    return _md_instance


def render_markdown(body: str) -> str:
    """Render a markdown body to HTML (raw HTML escaped — see module docstring)."""
    return _md().render(body)


def render_diff_html(patch: str) -> str:
    """Render a unified diff patch as per-line colored spans.

    Every line is HTML-escaped and wrapped in a ``diff-add`` / ``diff-del`` /
    ``diff-hunk`` / ``diff-meta`` / ``diff-ctx`` span inside a
    ``<div class="diff-view">``. Empty patch returns ``""``.
    """
    if not patch:
        return ""
    lines = []
    for line in patch.splitlines():
        escaped = html.escape(line)
        if line.startswith("@@"):
            cls = "diff-hunk"
        elif line.startswith(("+++", "---", "diff ", "index ")):
            cls = "diff-meta"
        elif line.startswith("+"):
            cls = "diff-add"
        elif line.startswith("-"):
            cls = "diff-del"
        else:
            cls = "diff-ctx"
        lines.append(f'<span class="{cls}">{escaped}</span>')
    return '<div class="diff-view">' + "\n".join(lines) + "</div>"


def render_inline_diff_html(patch: str) -> str:
    """Render a unified diff patch as an in-flow document diff.

    Hunk and meta headers are dropped; consecutive content lines of the same
    kind (context / deletion / insertion) are grouped and rendered through
    the shared MarkdownIt instance's BLOCK renderer, so document structure
    (headings, lists, tables, code fences) survives — per-line
    ``renderInline`` would flatten all of it to plain text. Deletion groups
    are wrapped in ``<div class="diff-del-block">``, insertion groups in
    ``<div class="diff-add-block">``; context groups render as plain
    markdown. Empty patch returns ``""``.
    """
    if not patch:
        return ""
    md = _md()
    groups: list[tuple[str, list[str]]] = []
    for line in patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            kind, text = "add", line[1:]
        elif line.startswith("-") and not line.startswith("---"):
            kind, text = "del", line[1:]
        elif line.startswith(" "):
            kind, text = "ctx", line[1:]
        else:
            # Hunk headers, diff/index/rename meta, "\ No newline" markers.
            continue
        if groups and groups[-1][0] == kind:
            groups[-1][1].append(text)
        else:
            groups.append((kind, [text]))
    out = []
    for kind, texts in groups:
        rendered = md.render("\n".join(texts))
        if kind == "add":
            out.append(f'<div class="diff-add-block">{rendered}</div>')
        elif kind == "del":
            out.append(f'<div class="diff-del-block">{rendered}</div>')
        else:
            out.append(rendered)
    return '<div class="diff-inline">' + "\n".join(out) + "</div>"
