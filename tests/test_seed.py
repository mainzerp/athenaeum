"""Tests for athenaeum.library.seed."""

import logging

from athenaeum.library.backend import LibraryBackend
from athenaeum.library.seed import MAX_SEED_BYTES, generate_seed
from athenaeum.mcp_server import SeedCache

ACTOR = "athenaeum-librarian/0.1.0"


def make_backend(tmp_path, **kwargs):
    backend = LibraryBackend(tmp_path / "lib", actor=ACTOR, **kwargs)
    backend.init_bundle()
    return backend


def test_seed_includes_tree_concepts_and_activity(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept(
        "/tables/customers.md",
        {"type": "Concept", "title": "Customers", "description": "Customer data"},
        "# Customers\n",
    )
    seed = generate_seed(backend)
    assert "tables/" in seed
    assert "customers.md" in seed
    assert "type: Concept" in seed
    assert "Customer data" in seed
    assert "## Recent activity" in seed
    assert "**Creation**" in seed


def test_seed_size_cap_and_truncation_note(tmp_path):
    backend = make_backend(tmp_path)
    for i in range(120):
        backend.create_concept(
            f"/concept-{i:03d}.md",
            {
                "type": "Concept",
                "title": f"Concept {i}",
                "description": "A fairly long one-line description for padding.",
            },
            "body\n",
        )
    seed = generate_seed(backend)
    assert len(seed.encode("utf-8")) <= MAX_SEED_BYTES
    assert "(truncated" in seed


def test_seed_cached_and_invalidated_on_write(tmp_path):
    backend = make_backend(tmp_path)
    backend.create_concept("/a.md", {"type": "Concept"}, "a\n")
    first = generate_seed(backend)
    assert generate_seed(backend) == first  # cached
    backend.create_concept("/b.md", {"type": "Note"}, "b\n")
    second = generate_seed(backend)
    assert second != first
    assert "/b.md" in second


def test_seed_same_mtime_serves_cached(tmp_path, monkeypatch):
    backend = make_backend(tmp_path)
    first = generate_seed(backend)
    monkeypatch.setattr(
        "athenaeum.library.seed._build_seed",
        lambda root: (_ for _ in ()).throw(AssertionError("rebuilt despite unchanged mtime")),
    )
    assert generate_seed(backend) == first


def test_seed_rebuilds_on_log_mtime_change(tmp_path):
    """A write through ANOTHER backend instance on the same root is seen:
    every compound write appends log.md, so its mtime is the version counter."""
    backend_a = make_backend(tmp_path)
    backend_a.create_concept("/a.md", {"type": "Concept"}, "a\n")
    first = generate_seed(backend_a)
    backend_b = LibraryBackend(tmp_path / "lib", actor=ACTOR)
    backend_b.create_concept("/b.md", {"type": "Note"}, "b\n")
    second = generate_seed(backend_a)
    assert second != first
    assert "/b.md" in second


# --- SeedCache fallback (CS-11) ----------------------------------------------


def test_seed_cache_fallback_signals_staleness(tmp_path, caplog):
    """Serving the last-known-good seed after a generator failure logs that
    it may be stale."""
    backend = make_backend(tmp_path)
    calls = {"n": 0}

    def generator(_backend):
        calls["n"] += 1
        if calls["n"] == 1:
            return "GOOD SEED"
        raise RuntimeError("generator down")

    cache = SeedCache(seed_generator=generator)
    assert cache.get("user-1", backend) == "GOOD SEED"
    with caplog.at_level(logging.WARNING):
        assert cache.get("user-1", backend) == "GOOD SEED"  # last-known-good
    assert "may be stale" in caplog.text


def test_seed_cache_without_fallback_returns_empty(tmp_path, caplog):
    backend = make_backend(tmp_path)

    def generator(_backend):
        raise RuntimeError("generator down")

    cache = SeedCache(seed_generator=generator)
    with caplog.at_level(logging.WARNING):
        assert cache.get("user-1", backend) == ""
    assert "may be stale" not in caplog.text
