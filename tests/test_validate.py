"""Tests for athenaeum.library.validate."""

from athenaeum.library.validate import validate_bundle


def write(root, rel, text):
    path = root / rel.lstrip("/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def codes(report, severity):
    return {entry["code"] for entry in report[severity]}


def test_missing_frontmatter_is_error(tmp_path):
    write(tmp_path, "/bad.md", "no frontmatter here\n")
    report = validate_bundle(tmp_path)
    assert "frontmatter-parse" in codes(report, "errors")


def test_unparseable_frontmatter_is_error(tmp_path):
    write(tmp_path, "/bad.md", "---\ntype: Concept\nno closing\n")
    report = validate_bundle(tmp_path)
    assert "frontmatter-parse" in codes(report, "errors")


def test_missing_type_is_error(tmp_path):
    write(tmp_path, "/bad.md", "---\ntitle: No type\n---\nbody\n")
    report = validate_bundle(tmp_path)
    assert "missing-type" in codes(report, "errors")


def test_non_root_index_frontmatter_is_error(tmp_path):
    write(tmp_path, "/ok.md", "---\ntype: Concept\n---\nbody\n")
    write(tmp_path, "/sub/index.md", "---\nokf_version: '0.2'\n---\n# Documents\n")
    report = validate_bundle(tmp_path)
    assert "reserved-structure" in codes(report, "errors")


def test_root_index_extra_frontmatter_key_is_error(tmp_path):
    write(tmp_path, "/ok.md", "---\ntype: Concept\n---\nbody\n")
    write(tmp_path, "/index.md", "---\nokf_version: '0.2'\nextra: nope\n---\n# Documents\n")
    report = validate_bundle(tmp_path)
    assert "reserved-structure" in codes(report, "errors")


def test_log_bad_date_heading_is_error(tmp_path):
    write(tmp_path, "/log.md", "# Directory Update Log\n\n## July 2026\n* **Update**: x.\n")
    report = validate_bundle(tmp_path)
    assert "log-date-format" in codes(report, "errors")


def test_log_valid_date_heading_ok(tmp_path):
    write(tmp_path, "/log.md", "# Directory Update Log\n\n## 2026-07-28\n* **Update**: x.\n")
    report = validate_bundle(tmp_path)
    assert "log-date-format" not in codes(report, "errors")


def test_generated_without_by_is_error(tmp_path):
    write(tmp_path, "/bad.md", "---\ntype: Concept\ngenerated:\n  at: 2026-01-01\n---\nbody\n")
    report = validate_bundle(tmp_path)
    assert "required-within" in codes(report, "errors")


def test_sources_entry_without_resource_is_error(tmp_path):
    write(tmp_path, "/bad.md", "---\ntype: Concept\nsources:\n- id: s1\n---\nbody\n")
    report = validate_bundle(tmp_path)
    assert "required-within" in codes(report, "errors")


def test_attested_computation_without_runtime_is_error(tmp_path):
    write(tmp_path, "/bad.md", "---\ntype: Attested Computation\n---\nbody\n")
    report = validate_bundle(tmp_path)
    assert "required-within" in codes(report, "errors")


def test_bad_status_is_error(tmp_path):
    write(tmp_path, "/bad.md", "---\ntype: Concept\nstatus: archived\n---\nbody\n")
    report = validate_bundle(tmp_path)
    assert "bad-status" in codes(report, "errors")


def test_broken_link_is_warning_not_error(tmp_path):
    write(tmp_path, "/a.md", "---\ntype: Concept\n---\n[gone](/missing.md)\n")
    report = validate_bundle(tmp_path)
    assert "broken-link" in codes(report, "warnings")
    assert not report["errors"]


def test_orphan_detected_linked_concept_not(tmp_path):
    write(tmp_path, "/orphan.md", "---\ntype: Concept\n---\nalone\n")
    write(tmp_path, "/b.md", "---\ntype: Concept\n---\n[c](/c.md)\n")
    write(tmp_path, "/c.md", "---\ntype: Concept\n---\n[b](/b.md)\n")
    report = validate_bundle(tmp_path)
    orphans = {w["path"] for w in report["warnings"] if w["code"] == "orphan"}
    assert "/orphan.md" in orphans
    assert "/b.md" not in orphans
    assert "/c.md" not in orphans


def test_verified_bare_mapping_is_warning_not_error(tmp_path):
    write(tmp_path, "/a.md", "---\ntype: Concept\nverified:\n  by: human:alice\n---\nbody\n")
    report = validate_bundle(tmp_path)
    assert "verified-bare-mapping" in codes(report, "warnings")
    assert not report["errors"]


def test_missing_recommended_and_actor_warnings(tmp_path):
    write(tmp_path, "/a.md", "---\ntype: Concept\ngenerated:\n  by: librarian\n---\nbody\n")
    report = validate_bundle(tmp_path)
    warnings = codes(report, "warnings")
    assert "missing-recommended" in warnings
    assert "non-conventional-actor" in warnings


def test_malformed_date_warning(tmp_path):
    write(tmp_path, "/a.md", "---\ntype: Concept\nstale_after: next week\n---\nbody\n")
    report = validate_bundle(tmp_path)
    assert "malformed-date" in codes(report, "warnings")


def test_index_drift_warning(tmp_path):
    write(tmp_path, "/a.md", "---\ntype: Concept\ntitle: A\n---\nbody\n")
    write(tmp_path, "/index.md", "# Documents\n\n* [Stale](stale.md)\n")
    report = validate_bundle(tmp_path)
    assert "index-drift" in codes(report, "warnings")


def test_footnote_mismatch_warning(tmp_path):
    text = "---\ntype: Concept\nsources:\n- id: s1\n  resource: /r.md\n---\nclaim[^s2]\n"
    write(tmp_path, "/a.md", text)
    report = validate_bundle(tmp_path)
    assert "footnote-mismatch" in codes(report, "warnings")


def test_conformant_concept_has_no_errors(tmp_path):
    write(
        tmp_path,
        "/a.md",
        "---\ntype: Concept\ntitle: A\ndescription: d\n"
        "generated:\n  by: athenaeum-librarian/0.1.0\n  at: 2026-07-28T10:00:00+00:00\n"
        "---\n[b](/b.md)\n",
    )
    write(tmp_path, "/b.md", "---\ntype: Concept\ntitle: B\ndescription: d\n---\n[a](/a.md)\n")
    report = validate_bundle(tmp_path)
    assert report["errors"] == []
