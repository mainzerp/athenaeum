"""Unit tests for library.organize: deterministic organization findings."""

from athenaeum.library import frontmatter as fm_mod
from athenaeum.library.organize import (
    FINDING_KEYS,
    NEAR_DUPLICATE_MAX_PAIRS,
    findings_empty,
    organization_findings,
)

BODY = "x" * 200


def write_concept(root, path, fm, body=BODY):
    file = root / path.lstrip("/")
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(fm_mod.dump_document(fm, body), encoding="utf-8")


def test_report_shape_and_empty_library(tmp_path):
    report = organization_findings(tmp_path)
    assert set(report) == set(FINDING_KEYS) | {"concepts_scanned", "since"}
    assert report["concepts_scanned"] == 0
    assert report["since"] is None
    assert findings_empty(report)


def test_findings_empty_predicate(tmp_path):
    report = organization_findings(tmp_path)
    assert findings_empty(report)
    report["thin_concepts"] = [{"id": "/x", "title": "X", "body_chars": 1}]
    assert not findings_empty(report)


def test_store_payload_reviews_is_caller_filled_placeholder(tmp_path):
    """The 7th key ships as an empty placeholder; handle_curate fills it
    from the payload archive (one-shot events, not structural state)."""
    report = organization_findings(tmp_path)
    assert "store_payload_reviews" in FINDING_KEYS
    assert report["store_payload_reviews"] == []


def test_type_named_folder_top_level_flagged_nested_not(tmp_path):
    write_concept(tmp_path, "/versions/v1.md", {"type": "Version", "title": "V1"})
    write_concept(tmp_path, "/athenaeum/versions/v2.md", {"type": "Version", "title": "V2"})
    write_concept(tmp_path, "/athenaeum/overview.md", {"type": "Project", "title": "Overview"})
    report = organization_findings(tmp_path)
    # top-level versions/ is a taxonomy smell; the nested one is allowed
    assert report["type_named_folders"] == [{"path": "versions", "concepts": 1}]


def test_type_named_folder_matches_type_in_use(tmp_path):
    write_concept(tmp_path, "/notes/a.md", {"type": "Note", "title": "Alpha Note"})
    write_concept(tmp_path, "/topics/b.md", {"type": "Note", "title": "Beta Note"})
    report = organization_findings(tmp_path)
    # "notes" matches GENERIC_TYPE_WORDS and the plural of the in-use type
    assert report["type_named_folders"] == [{"path": "notes", "concepts": 1}]


def test_oversized_folder_threshold_boundary(tmp_path):
    for i in range(13):
        write_concept(tmp_path, f"/big/c{i}.md", {"type": "Entry", "title": f"Big {i}"})
    for i in range(12):
        write_concept(tmp_path, f"/small/c{i}.md", {"type": "Entry", "title": f"Small {i}"})
    report = organization_findings(tmp_path)
    # 13 direct children exceeds 12; exactly 12 does not
    assert report["oversized_folders"] == [{"path": "big", "concepts": 13, "threshold": 12}]


def test_thin_concepts_boundary_and_deprecated_excluded(tmp_path):
    write_concept(tmp_path, "/thin.md", {"type": "Note", "title": "Thin"}, body="x" * 199)
    write_concept(tmp_path, "/full.md", {"type": "Note", "title": "Full"}, body="x" * 200)
    write_concept(
        tmp_path,
        "/old.md",
        {"type": "Note", "title": "Old", "status": "deprecated"},
        body="x",
    )
    report = organization_findings(tmp_path)
    assert report["thin_concepts"] == [{"id": "/thin", "title": "Thin", "body_chars": 199}]


def test_near_duplicate_jaccard_and_same_type_gate(tmp_path):
    write_concept(tmp_path, "/a.md", {"type": "Project", "title": "Phase 1 MVP"})
    write_concept(tmp_path, "/b.md", {"type": "Project", "title": "Phase 1 MVP Overview"})
    write_concept(tmp_path, "/c.md", {"type": "Note", "title": "Phase 1 MVP"})
    write_concept(tmp_path, "/d.md", {"type": "Note", "title": "Alpha Beta Gamma"})
    write_concept(tmp_path, "/e.md", {"type": "Note", "title": "Alpha Delta Epsilon"})
    report = organization_findings(tmp_path)
    # 3 shared / 4 union = 0.75; the Note-typed twin is gated out; 0.2 pair below
    assert report["near_duplicate_candidates"] == [
        {"ids": ["/a", "/b"], "similarity": 0.75, "shared": ["1", "mvp", "phase"]}
    ]


def test_near_duplicate_pair_cap(tmp_path):
    for i in range(7):
        write_concept(tmp_path, f"/c{i}.md", {"type": "Note", "title": "Same Title"})
    report = organization_findings(tmp_path)
    # 7 choose 2 = 21 candidate pairs, capped at the report bound
    assert len(report["near_duplicate_candidates"]) == NEAR_DUPLICATE_MAX_PAIRS


def test_series_pair_phase_lessons_skipped(tmp_path):
    write_concept(tmp_path, "/p1.md", {"type": "Lesson", "title": "Athenaeum Phase 1 Lessons"})
    write_concept(tmp_path, "/p3.md", {"type": "Lesson", "title": "Athenaeum Phase 3 Lessons"})
    report = organization_findings(tmp_path)
    # Jaccard would be 3/5 = 0.6 = threshold; the F14 incident shape
    assert report["near_duplicate_candidates"] == []


def test_series_pair_version_docs_skipped(tmp_path):
    write_concept(tmp_path, "/v080.md", {"type": "Version", "title": "Athenaeum v0.8.0"})
    write_concept(tmp_path, "/v090.md", {"type": "Version", "title": "Athenaeum v0.9.0"})
    report = organization_findings(tmp_path)
    assert report["near_duplicate_candidates"] == []


def test_series_pair_letter_suffixed_numbers_skipped(tmp_path):
    write_concept(tmp_path, "/p1.md", {"type": "Lesson", "title": "Athenaeum Phase 1 Lessons"})
    write_concept(tmp_path, "/p4c.md", {"type": "Lesson", "title": "Athenaeum Phase 4c Lessons"})
    report = organization_findings(tmp_path)
    # "4c" is not pure-numeric; the F14 follow-up gap found live (0.11.2)
    assert report["near_duplicate_candidates"] == []


def test_series_rule_keeps_genuine_duplicates(tmp_path):
    write_concept(tmp_path, "/a.md", {"type": "Note", "title": "Lessons Learned Phase"})
    write_concept(tmp_path, "/b.md", {"type": "Note", "title": "Phase Lessons Learned"})
    report = organization_findings(tmp_path)
    assert report["near_duplicate_candidates"] == [
        {"ids": ["/a", "/b"], "similarity": 1.0, "shared": ["learned", "lessons", "phase"]}
    ]


def test_near_duplicate_excludes_deprecated(tmp_path):
    """Deprecated concepts are hidden: they never join the duplicate grouping."""
    write_concept(tmp_path, "/a.md", {"type": "Note", "title": "Lessons Learned Phase"})
    write_concept(
        tmp_path,
        "/b.md",
        {"type": "Note", "title": "Phase Lessons Learned", "status": "deprecated"},
    )
    report = organization_findings(tmp_path)
    assert report["near_duplicate_candidates"] == []


# --- deprecated_cleanup (6th finding key) --------------------------------------


def test_deprecated_cleanup_zero_inbound_reported(tmp_path):
    write_concept(tmp_path, "/old.md", {"type": "Note", "title": "Old", "status": "deprecated"})
    report = organization_findings(tmp_path)
    assert report["deprecated_cleanup"] == [{"id": "/old", "title": "Old"}]
    assert not findings_empty(report)  # a cleanup finding wakes curation


def test_deprecated_cleanup_live_inbound_not_reported(tmp_path):
    """Convergence pin: a deprecated concept with live inbound links is never
    reported, so it cannot become a permanent re-reported finding."""
    write_concept(tmp_path, "/old.md", {"type": "Note", "title": "Old", "status": "deprecated"})
    write_concept(
        tmp_path, "/live.md", {"type": "Note", "title": "Live"}, body="see [Old](/old.md)\n"
    )
    report = organization_findings(tmp_path)
    assert report["deprecated_cleanup"] == []


def test_deprecated_cleanup_inbound_from_deprecated_counts_as_deletable(tmp_path):
    """Inbound only from another deprecated concept does not keep a deprecated
    concept unlisted — the whole deprecated cluster is pending deletion."""
    write_concept(tmp_path, "/old-a.md", {"type": "Note", "title": "Old A", "status": "deprecated"})
    write_concept(
        tmp_path,
        "/old-b.md",
        {"type": "Note", "title": "Old B", "status": "deprecated"},
        body="see [A](/old-a.md)\n",
    )
    report = organization_findings(tmp_path)
    assert report["deprecated_cleanup"] == [
        {"id": "/old-a", "title": "Old A"},
        {"id": "/old-b", "title": "Old B"},
    ]


def test_identical_titles_with_numbers_still_flagged(tmp_path):
    write_concept(tmp_path, "/a.md", {"type": "Note", "title": "Phase 1"})
    write_concept(tmp_path, "/b.md", {"type": "Note", "title": "Phase 1"})
    report = organization_findings(tmp_path)
    # identical token sets are the strongest duplicate signal;
    # the series rule requires a token difference
    assert report["near_duplicate_candidates"] == [
        {"ids": ["/a", "/b"], "similarity": 1.0, "shared": ["1", "phase"]}
    ]


def test_pure_numeric_titles_edge(tmp_path):
    write_concept(tmp_path, "/y1.md", {"type": "Note", "title": "2024"})
    write_concept(tmp_path, "/y2.md", {"type": "Note", "title": "2024"})
    write_concept(tmp_path, "/y3.md", {"type": "Note", "title": "2025"})
    report = organization_findings(tmp_path)
    # the identical pair is a duplicate; the 2024/2025 pairs are series-skipped
    # (also below threshold anyway — behavior-neutral, documents intent)
    assert [item["ids"] for item in report["near_duplicate_candidates"]] == [["/y1", "/y2"]]


def test_since_none_scans_everything(tmp_path):
    write_concept(
        tmp_path,
        "/thin.md",
        {"type": "Note", "title": "Thin", "generated": {"at": "2020-01-01T00:00:00+00:00"}},
        body="x",
    )
    report = organization_findings(tmp_path, since=None)
    assert [c["id"] for c in report["thin_concepts"]] == ["/thin"]
    assert report["concepts_scanned"] == 1


def test_since_filters_per_concept_but_not_structural(tmp_path):
    old = {"type": "Note", "title": "Old Thin", "generated": {"at": "2026-01-01T00:00:00+00:00"}}
    new = {"type": "Note", "title": "New Thin", "generated": {"at": "2026-07-01T00:00:00+00:00"}}
    write_concept(tmp_path, "/old.md", old, body="x")
    write_concept(tmp_path, "/new.md", new, body="x")
    write_concept(tmp_path, "/versions/v1.md", {"type": "Version", "title": "V1"})
    since = "2026-06-01T00:00:00+00:00"
    report = organization_findings(tmp_path, since=since)
    assert [c["id"] for c in report["thin_concepts"]] == ["/new"]
    # structural findings are folder-scoped: never filtered by the changed-set
    assert report["type_named_folders"] == [{"path": "versions", "concepts": 1}]
    assert report["since"] == since


def test_since_filters_duplicate_pairs_unless_one_member_changed(tmp_path):
    old_fm = {"type": "Project", "generated": {"at": "2026-01-01T00:00:00+00:00"}}
    write_concept(tmp_path, "/old-a.md", dict(old_fm, title="Phase 1 MVP"))
    write_concept(tmp_path, "/old-b.md", dict(old_fm, title="Phase 1 MVP Overview"))
    write_concept(
        tmp_path,
        "/new-c.md",
        {"type": "Project", "title": "Phase 1 MVP", "generated": {"at": "2026-07-01"}},
    )
    since = "2026-06-01T00:00:00+00:00"
    report = organization_findings(tmp_path, since=since)
    pairs = {tuple(item["ids"]) for item in report["near_duplicate_candidates"]}
    # old/old pair suppressed (already reported last run); new/old pair reported
    assert ("/old-a", "/old-b") not in pairs
    assert ("/new-c", "/old-a") in pairs


def test_date_only_generated_at_padded_for_comparison(tmp_path):
    write_concept(
        tmp_path,
        "/thin.md",
        {"type": "Note", "title": "Thin", "generated": {"at": "2026-06-01"}},
        body="x",
    )
    # date-only value pads to start-of-day: equal to the boundary, hence changed
    report = organization_findings(tmp_path, since="2026-06-01T00:00:00+00:00")
    assert [c["id"] for c in report["thin_concepts"]] == ["/thin"]
    later = organization_findings(tmp_path, since="2026-06-02T00:00:00+00:00")
    assert later["thin_concepts"] == []


def test_concepts_scanned_counts_all_regardless_of_since(tmp_path):
    write_concept(tmp_path, "/a.md", {"type": "Note", "title": "Alpha"})
    write_concept(tmp_path, "/dir/b.md", {"type": "Note", "title": "Beta"})
    report = organization_findings(tmp_path, since="2999-01-01T00:00:00+00:00")
    assert report["concepts_scanned"] == 2
    assert findings_empty(report)


def test_unscoped_finding_persists_until_fixed(tmp_path):
    """L14: an unscoped scan re-reports an unaddressed finding every run;
    it drops out only when actually fixed (no changed-set amnesia)."""
    write_concept(tmp_path, "/thin.md", {"type": "Note", "title": "Thin"}, body="x")
    first = organization_findings(tmp_path)
    second = organization_findings(tmp_path)  # unaddressed: reported again
    for report in (first, second):
        assert [c["id"] for c in report["thin_concepts"]] == ["/thin"]

    write_concept(tmp_path, "/thin.md", {"type": "Note", "title": "Thin"}, body="x" * 250)
    assert organization_findings(tmp_path)["thin_concepts"] == []  # fixed: drops out
