"""Write-path guard against literal ``\\uXXXX`` escape artifacts (F25).

Models occasionally emit literal 6-char ASCII sequences like ``\\u2011``
inside concept bodies instead of the actual Unicode characters. This module
detects those sequences OUTSIDE fenced code blocks and inline code spans and
decodes them deterministically to the real characters. Escapes inside fenced
blocks or inline code spans are an explicit escape hatch for intentional
documentation and pass through byte-untouched — but they are no longer
silently exempt: they are collected as candidates for LLM judgment.
``scan_code_span_escapes`` scans one body, ``code_span_escape_warning``
backs the write-path warn-always message, and
``scan_code_span_escape_candidates`` is the read-only stock scan behind the
curator's ``code_span_escape_candidates`` finding (alongside the prose-only
``scan_escape_artifacts``). ``\\U########`` and ``\\xNN`` forms are
intentionally not covered (F25 observed only ``\\uXXXX``).

The public entry point never raises and returns a byte-identical body (and
no warning) for clean input.

The module also owns the read-only stock scan (``scan_escape_artifacts``)
backing the curator's deterministic content-hygiene sweep over existing
concept bodies.
"""

import re
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

from . import frontmatter as fm_mod
from . import links as links_mod

ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")

MAX_CODE_SPAN_OCCURRENCES_PER_FILE = 10
MAX_CODE_SPAN_CANDIDATE_FILES = 20
MAX_CODE_SPAN_SNIPPET = 120

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


def _decode_plain(text: str, stats: Counter) -> str:
    """Decode ``\\uXXXX`` escapes in plain (non-code) text, updating *stats*."""

    def repl(match: re.Match) -> str:
        codepoint = int(match.group(1), 16)
        if 0xD800 <= codepoint <= 0xDFFF:
            # Surrogates cannot be UTF-8 encoded; leave the literal and
            # record the skip instead of crashing on chr()/write.
            stats["skipped"] += 1
            stats[f"skipped:{match.group(0)}"] += 1
            return match.group(0)
        stats["decoded"] += 1
        stats[f"decoded:{match.group(0)}"] += 1
        return chr(codepoint)

    return ESCAPE_RE.sub(repl, text)


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


def _decode_outside_code_spans(segment: str, stats: Counter) -> str:
    """Decode escapes in a non-fenced segment, skipping inline code spans.

    Everything outside the matched spans of ``_iter_code_spans`` (prose and
    unmatched backtick runs alike) is scanned for escapes; matched spans
    pass through untouched.
    """
    out: list[str] = []
    pos = 0
    for start, end in _iter_code_spans(segment):
        out.append(_decode_plain(segment[pos:start], stats))
        out.append(segment[start:end])
        pos = end
    out.append(_decode_plain(segment[pos:], stats))
    return "".join(out)


def decode_unicode_escapes(body: str) -> tuple[str, str | None]:
    """Decode literal ``\\uXXXX`` artifacts outside code spans/fenced blocks.

    Returns ``(new_body, warning_message_or_None)``. A clean body round-trips
    byte-identically with no warning. Never raises.
    """
    if not ESCAPE_RE.search(body):
        return body, None
    stats: Counter = Counter()
    parts: list[str] = []
    for is_fenced, text in _split_fence_segments(body):
        if is_fenced:
            parts.append(text)
        else:
            parts.append(_decode_outside_code_spans(text, stats))
    new_body = "".join(parts)
    decoded = stats["decoded"]
    skipped = stats["skipped"]
    if not decoded and not skipped:
        return body, None

    def detail(prefix: str) -> str:
        return ", ".join(
            f"{key.split(':', 1)[1]} (x{value})"
            for key, value in sorted(stats.items())
            if key.startswith(prefix)
        )

    message = ""
    if decoded:
        message += (
            f"Auto-decoded {decoded} literal unicode escape sequence(s) in the body: "
            f"{detail('decoded:')}. "
        )
    if skipped:
        message += (
            f"Skipped {skipped} escape(s) in the surrogate range "
            f"(U+D800-U+DFFF), left literal: {detail('skipped:')}. "
        )
    message += (
        "Write the actual Unicode characters directly; escapes inside code "
        "spans/fenced blocks were left untouched."
    )
    return new_body, message


def scan_escape_artifacts(root: str | Path) -> list[dict]:
    """Concept files whose on-disk body still holds repairable literal \\uXXXX artifacts.

    F25 stock scan. Read-only; never raises per-file (unreadable/unparseable
    files are skipped).
    A file is dirty only when decoding would actually change the body — fence/inline-code
    exemptions and surrogate-only files are therefore never reported.
    """
    dirty: list[dict] = []
    for bundle_path, abs_path in links_mod.iter_concept_files(root):
        try:
            fm, body = fm_mod.split_document(abs_path.read_text(encoding="utf-8"))
        except (fm_mod.FrontmatterError, OSError, UnicodeDecodeError):
            continue
        decoded, _ = decode_unicode_escapes(body)
        if decoded != body:
            dirty.append({"path": bundle_path, "title": str(fm.get("title") or ""), "body": body})
    return dirty


def scan_code_span_escapes(body: str) -> list[dict]:
    """Literal ``\\uXXXX`` occurrences inside code spans/fenced blocks of one body.

    Returns ``[{"line": <1-based body line>, "snippet": <source line,
    stripped, capped at MAX_CODE_SPAN_SNIPPET>}]``, bounded to
    MAX_CODE_SPAN_OCCURRENCES_PER_FILE (first-N wins). Fenced segments are
    scanned whole; in non-fenced segments only matched inline code spans are
    scanned (prose matches belong to the deterministic decode path, keeping
    the two semantics aligned). Surrogate-range escapes are collected like
    any other — judgment is the LLM's, not the scanner's. Never raises;
    returns ``[]`` for clean bodies.
    """
    occurrences: list[dict] = []
    body_lines = body.splitlines()
    offset = 0  # 0-based line index of the current segment start
    for is_fenced, text in _split_fence_segments(body):
        if is_fenced:
            positions = [m.start() for m in ESCAPE_RE.finditer(text)]
        else:
            positions = [
                start + m.start()
                for start, end in _iter_code_spans(text)
                for m in ESCAPE_RE.finditer(text[start:end])
            ]
        for pos in positions:
            line_index = offset + text.count("\n", 0, pos)
            snippet = body_lines[line_index].strip()[:MAX_CODE_SPAN_SNIPPET]
            occurrences.append({"line": line_index + 1, "snippet": snippet})
            if len(occurrences) >= MAX_CODE_SPAN_OCCURRENCES_PER_FILE:
                return occurrences
        offset += text.count("\n")
    return occurrences


def code_span_escape_warning(body: str) -> str | None:
    """Warn-always message for literal escapes inside code spans/fences.

    Distinct from the auto-decode warning: code-span content is left
    untouched (the escape hatch), so it needs caller/LLM judgment, not a
    deterministic rewrite. Returns None for clean bodies.
    """
    occurrences = scan_code_span_escapes(body)
    if not occurrences:
        return None
    locations = "; ".join(f"line {o['line']}: {o['snippet']}" for o in occurrences)
    return (
        f"Found {len(occurrences)} literal unicode escape(s) inside code "
        f"spans/fenced blocks (left untouched): {locations}. "
        "If these are artifacts, rewrite with real characters; if "
        "intentional, resubmit with allow_literal_escapes: true."
    )


def scan_code_span_escape_candidates(root: str | Path) -> list[dict]:
    """Concept files holding literal ``\\uXXXX`` inside code spans/fenced blocks.

    Curator finding (LLM-judged): the deterministic sweep stays prose-only,
    so these candidates are reported for the curator to judge artifact vs.
    intentional documentation. Read-only; never raises per-file
    (unreadable/unparseable files are skipped). Bounded to
    MAX_CODE_SPAN_CANDIDATE_FILES.
    """
    candidates: list[dict] = []
    for bundle_path, abs_path in links_mod.iter_concept_files(root):
        try:
            fm, body = fm_mod.split_document(abs_path.read_text(encoding="utf-8"))
        except (fm_mod.FrontmatterError, OSError, UnicodeDecodeError):
            continue
        occurrences = scan_code_span_escapes(body)
        if occurrences:
            candidates.append(
                {
                    "path": bundle_path,
                    "title": str(fm.get("title") or ""),
                    "occurrences": occurrences,
                }
            )
            if len(candidates) >= MAX_CODE_SPAN_CANDIDATE_FILES:
                break
    return candidates
