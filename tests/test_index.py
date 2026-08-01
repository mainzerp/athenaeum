"""Tests for athenaeum.library.index."""

from athenaeum.library.frontmatter import split_document
from athenaeum.library.index import generate_index

CONCEPT = "---\ntype: Concept\ntitle: Customers\ndescription: Customer data docs\n---\nbody\n"


def build_bundle(root):
    (root / "customers.md").write_text(CONCEPT, encoding="utf-8")
    (root / "plain.md").write_text("---\ntype: Note\n---\nbody\n", encoding="utf-8")
    sub = root / "tables"
    sub.mkdir()
    (sub / "orders.md").write_text(CONCEPT, encoding="utf-8")


def test_determinism(tmp_path):
    build_bundle(tmp_path)
    assert generate_index(tmp_path, "/") == generate_index(tmp_path, "/")


def test_description_backed_entries(tmp_path):
    build_bundle(tmp_path)
    index = generate_index(tmp_path, "/")
    assert "* [Customers](customers.md) - Customer data docs" in index
    # no description -> plain entry without " - " suffix
    assert "* [plain](plain.md)\n" in index


def test_subdir_entry_form(tmp_path):
    build_bundle(tmp_path)
    index = generate_index(tmp_path, "/")
    assert "# tables\n* [tables](tables/)" in index


def test_root_carries_okf_version_only(tmp_path):
    build_bundle(tmp_path)
    root_index = generate_index(tmp_path, "/")
    fm, _ = split_document(root_index)
    assert fm == {"okf_version": "0.2"}
    sub_index = generate_index(tmp_path, "/tables")
    fm_sub, _ = split_document(sub_index)
    assert fm_sub == {}
    assert "* [Customers](orders.md) - Customer data docs" in sub_index


def test_stable_alphabetical_order(tmp_path):
    for name in ("zebra.md", "alpha.md", "mid.md"):
        (tmp_path / name).write_text("---\ntype: Note\n---\nbody\n", encoding="utf-8")
    index = generate_index(tmp_path, "/")
    lines = [line for line in index.split("\n") if line.startswith("* [")]
    assert lines == ["* [alpha](alpha.md)", "* [mid](mid.md)", "* [zebra](zebra.md)"]
