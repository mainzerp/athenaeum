"""Tests for athenaeum.library.frontmatter."""

import datetime
import os
import threading
from pathlib import Path

import pytest

from athenaeum.library.frontmatter import (
    FrontmatterError,
    dump_document,
    split_document,
    write_bytes_atomic,
    write_text_atomic,
)

SAMPLE = (
    "---\n"
    "title: Customers\n"
    "type: Concept\n"
    "custom_key: keep me\n"
    "tags:\n"
    "- a\n"
    "- b\n"
    "---\n"
    "# Body\n"
    "\n"
    "Some *text*.\n"
)
SAMPLE_BODY = "# Body\n\nSome *text*.\n"


def test_round_trip_preserves_unknown_keys():
    fm, body = split_document(SAMPLE)
    assert fm["custom_key"] == "keep me"
    assert fm["tags"] == ["a", "b"]
    assert dump_document(fm, body) == SAMPLE


def test_body_byte_identity():
    fm, body = split_document(SAMPLE)
    assert body == SAMPLE_BODY
    fm["type"] = "Note"
    assert dump_document(fm, body).endswith(SAMPLE_BODY)


def test_verified_bare_mapping_normalized_to_list():
    text = "---\ntype: Concept\nverified:\n  by: human:alice\n  at: 2026-01-01\n---\nbody\n"
    fm, _ = split_document(text)
    assert fm["verified"] == [{"by": "human:alice", "at": datetime.date(2026, 1, 1)}]


def test_verified_normalization_can_be_disabled():
    text = "---\ntype: Concept\nverified:\n  by: human:alice\n---\nbody\n"
    fm, _ = split_document(text, normalize_verified=False)
    assert fm["verified"] == {"by": "human:alice"}


def test_no_frontmatter_returns_empty_dict():
    fm, body = split_document("# Just markdown\n")
    assert fm == {}
    assert body == "# Just markdown\n"


def test_unclosed_block_raises():
    with pytest.raises(FrontmatterError):
        split_document("---\ntype: Concept\nno closing delimiter\n")


def test_indented_delimiter_inside_block_scalar_is_content():
    """LIBRARY-05: an indented ``  ---`` inside a YAML single-quoted scalar
    is content, not the closing delimiter (probe: yaml.safe_dump emits this
    exact shape for multi-line strings, so backend-written files hit it)."""
    text = dump_document({"type": "Note", "description": "first\n\n---\n\nsecond"}, "body\n")
    assert "\n  ---\n" in text  # the dump really carries the indented line
    fm, body = split_document(text)
    assert fm == {"type": "Note", "description": "first\n\n---\n\nsecond"}
    assert body == "body\n"


def test_indented_delimiter_alone_does_not_close_block():
    with pytest.raises(FrontmatterError):
        split_document("---\ntype: Concept\n  ---\nstill frontmatter\n")


def test_bom_prefixed_document_splits():
    """LIBRARY-12: one leading UTF-8 BOM is tolerated before the opening ---."""
    fm, body = split_document("\ufeff---\ntype: Concept\n---\nbody\n")
    assert fm == {"type": "Concept"}
    assert body == "body\n"


def test_bom_without_frontmatter_returns_original_text():
    text = "\ufeff# Just markdown\n"
    fm, body = split_document(text)
    assert fm == {}
    assert body == text


def test_leading_blank_lines_before_frontmatter_tolerated():
    """LIBRARY-12: leading blank lines before the opening --- are skipped."""
    fm, body = split_document("\n\n---\ntype: Concept\n---\nbody\n")
    assert fm == {"type": "Concept"}
    assert body == "body\n"


def test_non_mapping_frontmatter_raises():
    with pytest.raises(FrontmatterError):
        split_document("---\n- just\n- a\n- list\n---\nbody\n")


def test_dump_empty_frontmatter_returns_body():
    assert dump_document({}, "body\n") == "body\n"


def test_write_text_atomic_cleans_tmp_on_replace_failure(tmp_path, monkeypatch):
    """L13: a failed os.replace must not leak the tmp sibling."""
    target = tmp_path / "a.md"

    def failing_replace(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("os.replace", failing_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        write_text_atomic(target, "x\n")
    assert not target.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_bytes_atomic_round_trip(tmp_path):
    target = tmp_path / "sub" / "blob.bin"  # parent dir created on demand
    payload = bytes(range(256)) + b"\x00\xffbinary\n"
    write_bytes_atomic(target, payload)
    assert target.read_bytes() == payload
    assert list(tmp_path.glob("**/*.tmp")) == []


def test_write_bytes_atomic_cleans_tmp_on_replace_failure(tmp_path, monkeypatch):
    """L13: a failed os.replace must not leak the tmp sibling (binary twin)."""
    target = tmp_path / "blob.bin"

    def failing_replace(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("os.replace", failing_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        write_bytes_atomic(target, b"x")
    assert not target.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_text_atomic_parallel_writers_no_shared_tmp(tmp_path, monkeypatch):
    """Concurrent writers to one path never share a tmp file or leave residue."""
    target = tmp_path / "a.md"
    tmp_names = []
    real_replace = os.replace

    def recording_replace(src, dst):
        tmp_names.append(Path(src).name)
        try:
            real_replace(src, dst)
        except PermissionError:
            # Windows refuses concurrent renames onto one destination; the
            # tmp-name property under test is unaffected, so drop the orphan.
            Path(src).unlink(missing_ok=True)

    monkeypatch.setattr("os.replace", recording_replace)
    barrier = threading.Barrier(8)
    errors = []

    def writer(tag: int) -> None:
        try:
            barrier.wait(timeout=10)
            for i in range(20):
                write_text_atomic(target, f"writer {tag} round {i}\n")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    assert len(tmp_names) == 160
    assert len(set(tmp_names)) == 160  # every write used its own tmp file
    assert target.is_file()
    assert list(tmp_path.glob("*.tmp")) == []
