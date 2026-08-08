"""Tests for athenaeum.library.log."""

import re
from datetime import date

import pytest

from athenaeum.library.log import append_entry, recent_entries

TODAY = date.today().isoformat()


def read_log(root):
    return (root / "log.md").read_text(encoding="utf-8")


def test_heading_created_on_first_entry(tmp_path):
    append_entry(tmp_path, "Creation", "Created something.")
    content = read_log(tmp_path)
    assert content.startswith("# Directory Update Log\n")
    assert f"## {TODAY}" in content
    assert "* **Creation**: Created something." in content


def test_oldest_first_within_day(tmp_path):
    append_entry(tmp_path, "Creation", "first.")
    append_entry(tmp_path, "Update", "second.")
    content = read_log(tmp_path)
    assert content.index("first.") < content.index("second.")


def test_oldest_date_group_first(tmp_path):
    append_entry(tmp_path, "Creation", "old.", today=date(2026, 1, 1))
    append_entry(tmp_path, "Update", "new.", today=date(2026, 2, 2))
    content = read_log(tmp_path)
    assert content.index("## 2026-01-01") < content.index("## 2026-02-02")


def test_date_format(tmp_path):
    append_entry(tmp_path, "Creation", "x.")
    headings = [line for line in read_log(tmp_path).split("\n") if line.startswith("## ")]
    assert headings
    for heading in headings:
        assert re.fullmatch(r"## \d{4}-\d{2}-\d{2}", heading)


def test_agent_label_suffix(tmp_path):
    append_entry(tmp_path, "Creation", "Created x.", agent_label="bot-1")
    assert "(requested by agent:bot-1)" in read_log(tmp_path)


def test_multiline_text_collapsed_to_one_line(tmp_path):
    """LIBRARY-04: entry text is collapsed at the sink — a forged
    ``## YYYY-MM-DD`` inside the text can neither split the entry nor
    poison the legacy-flip heuristic afterwards."""
    append_entry(tmp_path, "Creation", "first.")
    append_entry(tmp_path, "Update", "line one\n## 1900-01-01\nforged heading")
    content = read_log(tmp_path)
    assert "\n## 1900-01-01\n" not in content
    assert "* **Update**: line one ## 1900-01-01 forged heading" in content
    # a follow-up append must not trigger the legacy flip (no forged heading)
    append_entry(tmp_path, "Move", "after.")
    content = read_log(tmp_path)
    assert content.index("first.") < content.index("line one") < content.index("after.")


def test_multiline_agent_label_collapsed(tmp_path):
    """LIBRARY-04: the agent label is collapsed before the suffix is built."""
    append_entry(tmp_path, "Creation", "x.", agent_label="bot\n## 1900-01-01")
    content = read_log(tmp_path)
    assert "(requested by agent:bot ## 1900-01-01)" in content
    assert "\n## 1900-01-01\n" not in content


def test_unknown_kind_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown log entry kind"):
        append_entry(tmp_path, "Magic", "x.")


def test_recent_entries_newest_first(tmp_path):
    append_entry(tmp_path, "Creation", "one.", today=date(2026, 1, 1))
    append_entry(tmp_path, "Update", "two.")
    entries = recent_entries(tmp_path, limit=10)
    assert len(entries) == 2
    assert "two." in entries[0]
    assert recent_entries(tmp_path, limit=1) == [entries[0]]


LEGACY_LOG = (
    "# Directory Update Log\n"
    "\n"
    "## 2026-02-02\n"
    "* **Update**: feb-newer.\n"
    "* **Creation**: feb-older.\n"
    "\n"
    "## 2026-01-01\n"
    "* **Update**: jan-newer.\n"
    "* **Creation**: jan-older.\n"
)


def test_append_is_append_only_no_full_rewrite(tmp_path, monkeypatch):
    """A12/CS-6: appending to an existing log must not read+rewrite the file."""
    from pathlib import Path

    append_entry(tmp_path, "Creation", "first.")

    def boom(*args, **kwargs):
        raise AssertionError("append must be O(1): no full read/rewrite")

    monkeypatch.setattr("athenaeum.library.log.write_text_atomic", boom)
    monkeypatch.setattr(Path, "read_text", boom)
    append_entry(tmp_path, "Update", "second.")
    content = (tmp_path / "log.md").read_bytes().decode("utf-8")
    assert content.index("first.") < content.index("second.")


def test_legacy_newest_first_file_flipped_on_append(tmp_path):
    """A one-time migration flips legacy newest-first logs to chronological."""
    (tmp_path / "log.md").write_text(LEGACY_LOG, encoding="utf-8")
    append_entry(tmp_path, "Move", "today.", today=date(2026, 3, 3))
    content = read_log(tmp_path)
    assert content.index("## 2026-01-01") < content.index("## 2026-02-02")
    assert content.index("## 2026-02-02") < content.index("## 2026-03-03")
    assert content.index("jan-older.") < content.index("jan-newer.")
    assert content.index("feb-older.") < content.index("feb-newer.")
    assert content.rstrip().endswith("* **Move**: today.")


def test_recent_entries_on_unmigrated_legacy_file(tmp_path):
    """Before the first append migrates it, a legacy file still reads newest-first."""
    (tmp_path / "log.md").write_text(LEGACY_LOG, encoding="utf-8")
    entries = recent_entries(tmp_path, limit=2)
    assert entries == ["* **Update**: feb-newer.", "* **Creation**: feb-older."]
