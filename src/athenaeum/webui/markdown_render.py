"""Server-side markdown and diff rendering for the WebUI.

Document bodies are rendered with markdown-it-py (``default`` preset:
CommonMark + tables + strikethrough, ``html=False`` so raw HTML in the
source is escaped, not passed through) plus the tasklists plugin; fenced
code blocks are highlighted with Pygments. Unified diffs are rendered as
per-line colored spans. Both renderers escape all raw input, so their
output is safe to emit with ``| safe`` in templates.
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
