"""Shared markdown code-region segmentation (fenced blocks and inline code spans).

The helpers here were extracted from ``escape_guard`` (F27) because three
modules now need the same segmentation: ``escape_guard`` (literal ``\\uXXXX``
hygiene), ``embeddings`` (link-target stripping for indexing), and ``links``
(concept-link extraction and rewrite). Centralizing the segmentation keeps
all three on one CommonMark-flavored contract instead of drifting copies.

- ``_split_fence_segments`` splits a body into fenced/prose line segments.
- ``_iter_code_spans`` yields inline code spans within one non-fenced segment.
- ``iter_code_segments`` is the public, flat stream: ``(is_code, text)``
  chunks covering the whole body, byte-identical on round-trip.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def _split_fence_segments(body: str) -> list[tuple[bool, str]]:
    """Split *body* into (is_fenced, text) segments, preserving newlines.

    Line-based state machine: an opening fence is a line with up to 3 leading
    spaces followed by a run of >= 3 backticks or tildes (info string
    ignored). A closing fence is a line whose first non-space content
    (<= 3 leading spaces) is a run of the SAME fence character with a length
    >= the opening run length. Fence lines and everything between are fenced.
    An unclosed fence swallows the rest of the body.
    """
    segments: list[tuple[bool, str]] = []
    current: list[str] = []
    current_fenced = False
    fence_char: str | None = None
    fence_len = 0

    def flush() -> None:
        if current:
            segments.append((current_fenced, "".join(current)))
            current.clear()

    for line in body.splitlines(keepends=True):
        if fence_char is None:
            m = _FENCE_OPEN_RE.match(line)
            if m:
                run = m.group(1)
                flush()
                current_fenced = True
                fence_char = run[0]
                fence_len = len(run)
                current.append(line)
            else:
                current.append(line)
        else:
            current.append(line)
            stripped = line.lstrip(" ")
            if len(line) - len(stripped) <= 3 and stripped.startswith(fence_char):
                run = 0
                for ch in stripped:
                    if ch == fence_char:
                        run += 1
                    else:
                        break
                if run >= fence_len:
                    fence_char = None
                    flush()
                    current_fenced = False
    flush()
    return segments


def _iter_code_spans(segment: str) -> Iterator[tuple[int, int]]:
    """Yield ``(start, end)`` of matched inline code spans in a non-fenced segment.

    Character scanner over backtick runs: an opening run of length N closes
    at the NEXT run of EXACTLY length N; the span includes both runs. An
    unmatched run is literal/prose text — NOT a span.
    """
    pos = 0
    length = len(segment)
    while pos < length:
        if segment[pos] != "`":
            tick = segment.find("`", pos)
            pos = length if tick == -1 else tick
            continue
        run_end = pos
        while run_end < length and segment[run_end] == "`":
            run_end += 1
        run_len = run_end - pos
        close = pos + run_len
        while True:
            tick = segment.find("`", close)
            if tick == -1:
                close = -1
                break
            close_end = tick
            while close_end < length and segment[close_end] == "`":
                close_end += 1
            if close_end - tick == run_len:
                close = close_end
                break
            close = close_end
        if close == -1:
            # Unmatched run: literal text, not a span.
            pos = run_end
        else:
            yield pos, close
            pos = close


def iter_code_segments(body: str) -> Iterator[tuple[bool, str]]:
    """Yield ``(is_code, text)`` chunks covering *body*, byte-identical on join.

    Fenced code blocks are code; within prose segments, matched inline code
    spans (``_iter_code_spans``) are code. Everything else — including
    unmatched backtick runs — is prose. Empty chunks are never yielded, and
    ``"".join(text for _, text in iter_code_segments(body)) == body``.
    """
    for is_fenced, text in _split_fence_segments(body):
        if is_fenced:
            yield True, text
            continue
        pos = 0
        for start, end in _iter_code_spans(text):
            if start > pos:
                yield False, text[pos:start]
            yield True, text[start:end]
            pos = end
        if pos < len(text):
            yield False, text[pos:]
