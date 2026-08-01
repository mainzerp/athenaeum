"""Tests for the librarian prompt contracts (plan section 3.4a, §3.1).

Pins the write-discipline section markers of DEFAULT_SYSTEM_PROMPT and the
consumer side of the status() orphan-dict contract in
build_maintain_preamble, independently of the agent loop.
"""

from athenaeum.librarian.prompts import (
    DEFAULT_SYSTEM_PROMPT,
    build_curate_preamble,
    build_maintain_preamble,
    build_system_prompt,
    render_curate_prompt_display,
)


def test_default_prompt_has_write_discipline_markers():
    assert "## Write discipline" in DEFAULT_SYSTEM_PROMPT
    assert "CREATE vs. ENRICH" in DEFAULT_SYSTEM_PROMPT
    assert "BACK-LINK AT CREATION" in DEFAULT_SYSTEM_PROMPT
    assert "SUPERSEDE CONTRADICTIONS IN PLACE" in DEFAULT_SYSTEM_PROMPT
    assert "INDEX AND LOG MAINTENANCE IS AUTOMATIC" in DEFAULT_SYSTEM_PROMPT
    assert "NO EMPTY SECTIONS" in DEFAULT_SYSTEM_PROMPT
    assert "A STORE ENDS IN WRITES" in DEFAULT_SYSTEM_PROMPT
    assert "NEVER RE-READ" in DEFAULT_SYSTEM_PROMPT
    assert "ANSWER HYGIENE" in DEFAULT_SYSTEM_PROMPT


def test_default_prompt_pins_semantic_requery_discipline():
    assert "per distinct information need" in DEFAULT_SYSTEM_PROMPT


def test_default_prompt_pins_subject_match_placement():
    assert "NAME THE SUBJECT FIRST" in DEFAULT_SYSTEM_PROMPT
    assert "is NOT a topic" in DEFAULT_SYSTEM_PROMPT


def test_default_prompt_pins_markdown_link_syntax():
    assert "[text](/absolute/path.md)" in DEFAULT_SYSTEM_PROMPT
    assert "invisible to the link graph" in DEFAULT_SYSTEM_PROMPT


def test_maintain_preamble_pins_markdown_link_syntax():
    status = {
        "healthy": False,
        "health": {"orphans": [], "broken_links": [], "warnings": 0, "errors": 0},
    }
    assert "[text](/absolute/path.md)" in build_maintain_preamble(status)


def test_maintain_preamble_renders_orphan_dicts():
    status = {
        "healthy": False,
        "health": {
            "orphans": [{"id": "/a", "title": "A"}],
            "broken_links": [],
            "warnings": 1,
            "errors": 0,
        },
    }
    preamble = build_maintain_preamble(status)
    assert "/a" in preamble
    assert "A" in preamble

    with_instructions = build_maintain_preamble(status, instructions="be careful")
    assert "be careful" in with_instructions


def test_curate_preamble_renders_findings():
    findings = {
        "type_named_folders": [{"path": "versions", "concepts": 4}],
        "oversized_folders": [{"path": "athenaeum", "concepts": 17, "threshold": 12}],
        "thin_concepts": [{"id": "/a", "title": "A", "body_chars": 83}],
        "near_duplicate_candidates": [
            {"ids": ["/a", "/b"], "similarity": 0.75, "shared": ["1", "phase"]}
        ],
        "semantic_duplicate_candidates": [{"ids": ["/c", "/d"], "similarity": 0.91}],
        "concepts_scanned": 22,
        "since": "2026-07-01T00:00:00+00:00",
    }
    preamble = build_curate_preamble(findings)
    assert "CURATION TASK" in preamble
    assert "versions (4 concepts)" in preamble
    assert "athenaeum (17 concepts)" in preamble
    assert "/a (A, 83 chars)" in preamble
    assert "/a <-> /b" in preamble and "0.75" in preamble
    # L14: findings cover the whole library every run (no changed-set scoping)
    assert "re-reported until it is actually fixed" in preamble
    # semantic lines are score-only: similarity marker, no shared-token list
    assert "/c <-> /d (similarity 0.91; semantic)" in preamble
    semantic_line = next(line for line in preamble.splitlines() if "; semantic" in line)
    assert "shared:" not in semantic_line

    empty = build_curate_preamble(
        {
            "type_named_folders": [],
            "oversized_folders": [],
            "thin_concepts": [],
            "near_duplicate_candidates": [],
            "semantic_duplicate_candidates": [],
            "concepts_scanned": 0,
            "since": None,
        },
        instructions="be careful",
    )
    assert empty.count("- none") == 5
    assert "be careful" in empty


def test_curate_preamble_pins_series_rule():
    findings = {
        "type_named_folders": [],
        "oversized_folders": [],
        "thin_concepts": [],
        "near_duplicate_candidates": [],
        "concepts_scanned": 0,
        "since": None,
    }
    assert "differ only in a version/phase identifier" in build_curate_preamble(findings)
    assert "differ only in a version/phase identifier" in render_curate_prompt_display(None)


def test_curate_preamble_addendum():
    findings = {
        "type_named_folders": [],
        "oversized_folders": [],
        "thin_concepts": [{"id": "/a", "title": "A", "body_chars": 83}],
        "near_duplicate_candidates": [],
        "concepts_scanned": 1,
        "since": None,
    }
    with_addendum = build_curate_preamble(findings, addendum="always prefer merge")
    assert "Standing curation rules from the library owner:" in with_addendum
    assert "always prefer merge" in with_addendum

    without_addendum = build_curate_preamble(findings)
    assert "Standing curation rules from the library owner:" not in without_addendum

    # braces inside the addendum value are never re-parsed by str.format
    braced = build_curate_preamble(findings, addendum="{not_a_placeholder}")
    assert "{not_a_placeholder}" in braced


def test_render_curate_prompt_display():
    plain = render_curate_prompt_display(None)
    assert "CURATION TASK" in plain
    assert "{instructions}" in plain  # placeholders stay visible
    assert "{semantic_duplicates}" in plain
    assert "{addendum}" not in plain
    assert "Standing curation rules from the library owner:" not in plain

    with_addendum = render_curate_prompt_display("never create concepts")
    assert with_addendum.endswith("never create concepts\n")
    assert "Standing curation rules from the library owner:" in with_addendum
    assert "{instructions}" in with_addendum

    # an addendum with braces cannot crash the renderer (no .format() call)
    braced = render_curate_prompt_display("keep {braces} intact")
    assert "keep {braces} intact" in braced


def test_build_system_prompt_default_only():
    assert build_system_prompt() == DEFAULT_SYSTEM_PROMPT
    assert build_system_prompt(None) == DEFAULT_SYSTEM_PROMPT
    assert "Standing rules from the library owner:" not in build_system_prompt("")


def test_build_system_prompt_with_addendum():
    prompt = build_system_prompt("Answer in German.")
    assert prompt.startswith(DEFAULT_SYSTEM_PROMPT)
    assert "Standing rules from the library owner:" in prompt
    assert prompt.rstrip().endswith("Answer in German.")
    braced = build_system_prompt("keep {braces} intact")
    assert "keep {braces} intact" in braced
