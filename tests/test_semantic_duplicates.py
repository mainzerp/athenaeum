"""Tests for F2: semantic duplicate detection (VECTOR_SEARCH plan §8.2).

``semantic_duplicate_candidates`` is pure over cached vectors; gate coverage
mirrors the structural pass (same-type, changed-set, series guard) plus the
Jaccard dedup and the 0.85 cosine threshold. The handle_curate merge is
covered with a fake embedding service.
"""

import importlib.util
import math

import pytest

from athenaeum.librarian.agent import Librarian, LibrarianConfig
from athenaeum.librarian.embed import EmbeddingConfig
from athenaeum.librarian.llm import LLMConfig, LLMResponse
from athenaeum.library import frontmatter as fm_mod
from athenaeum.library import organize as organize_mod
from athenaeum.library import semantic as semantic_mod
from athenaeum.library.organize import (
    FINDING_KEYS,
    findings_empty,
    organization_findings,
)
from athenaeum.library.semantic import (
    SEMANTIC_DUPLICATE_COSINE,
    SEMANTIC_DUPLICATE_MAX_PAIRS,
    SEMANTIC_DUPLICATE_THRESHOLDS,
    semantic_duplicate_candidates,
    semantic_threshold_for_model,
)

BODY = "x" * 200  # at the thin-concept boundary: not thin


def write_concept(root, path, fm, body=BODY):
    file = root / path.lstrip("/")
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(fm_mod.dump_document(fm, body), encoding="utf-8")


def unit_vector(cos_to_base):
    """Unit 2-d vector with the given cosine to ``[1.0, 0.0]``."""
    return [cos_to_base, math.sqrt(1 - cos_to_base**2)]


# --- gates --------------------------------------------------------------------


def test_same_type_gate(tmp_path):
    write_concept(tmp_path, "/a.md", {"type": "Note", "title": "Alpha"})
    write_concept(tmp_path, "/b.md", {"type": "Project", "title": "Beta"})
    vectors = {"a.md": [1.0, 0.0], "b.md": [1.0, 0.0]}
    assert semantic_duplicate_candidates(tmp_path, vectors) == []


def test_since_gate_requires_one_changed_member(tmp_path):
    old = {"type": "Note", "generated": {"at": "2026-01-01T00:00:00+00:00"}}
    write_concept(tmp_path, "/a.md", dict(old, title="Alpha"))
    write_concept(tmp_path, "/b.md", dict(old, title="Beta"))
    vectors = {"a.md": [1.0, 0.0], "b.md": [1.0, 0.0]}
    since = "2026-06-01T00:00:00+00:00"
    assert semantic_duplicate_candidates(tmp_path, vectors, since=since) == []

    write_concept(
        tmp_path,
        "/b.md",
        {"type": "Note", "title": "Beta", "generated": {"at": "2026-07-01T00:00:00+00:00"}},
    )
    assert semantic_duplicate_candidates(tmp_path, vectors, since=since) == [
        {"ids": ["/a", "/b"], "similarity": 1.0}
    ]


def test_series_pair_excluded_despite_identical_vectors(tmp_path):
    write_concept(tmp_path, "/p1.md", {"type": "Lesson", "title": "Athenaeum Phase 1 Lessons"})
    write_concept(tmp_path, "/p4c.md", {"type": "Lesson", "title": "Athenaeum Phase 4c Lessons"})
    # F14 regression shape: "4c" letter-suffixed number token, cosine 1.0
    vectors = {"p1.md": [1.0, 0.0], "p4c.md": [1.0, 0.0]}
    assert semantic_duplicate_candidates(tmp_path, vectors) == []


def test_threshold_boundary_exact_and_below(tmp_path):
    write_concept(tmp_path, "/a.md", {"type": "Note", "title": "Alpha"})
    write_concept(tmp_path, "/b.md", {"type": "Note", "title": "Beta"})
    write_concept(tmp_path, "/c.md", {"type": "Note", "title": "Gamma"})
    assert SEMANTIC_DUPLICATE_COSINE == 0.85
    vectors = {
        "a.md": [1.0, 0.0],
        "b.md": unit_vector(SEMANTIC_DUPLICATE_COSINE),  # exactly 0.85: passes
        # 0.84 against the base AND far from b (mirrored direction): filtered
        "c.md": [0.84, -math.sqrt(1 - 0.84**2)],
    }
    assert semantic_duplicate_candidates(tmp_path, vectors) == [
        {"ids": ["/a", "/b"], "similarity": 0.85}
    ]


def test_threshold_for_model_mapping():
    assert SEMANTIC_DUPLICATE_THRESHOLDS == {
        "BAAI/bge-small-en-v1.5": 0.85,
        "BAAI/bge-base-en-v1.5": 0.85,
        "sentence-transformers/all-MiniLM-L6-v2": 0.80,
        "intfloat/multilingual-e5-small": 0.82,
    }
    assert semantic_threshold_for_model("BAAI/bge-small-en-v1.5") == 0.85
    assert semantic_threshold_for_model("BAAI/bge-base-en-v1.5") == 0.85
    assert semantic_threshold_for_model("sentence-transformers/all-MiniLM-L6-v2") == 0.80
    assert semantic_threshold_for_model("intfloat/multilingual-e5-small") == 0.82
    assert semantic_threshold_for_model("unknown/model") == 0.85
    assert semantic_threshold_for_model(None) == 0.85
    assert SEMANTIC_DUPLICATE_COSINE == 0.85


def test_explicit_threshold_param_honored(tmp_path):
    write_concept(tmp_path, "/a.md", {"type": "Note", "title": "Alpha"})
    write_concept(tmp_path, "/b.md", {"type": "Note", "title": "Beta"})
    vectors = {"a.md": [1.0, 0.0], "b.md": unit_vector(0.83)}
    assert semantic_duplicate_candidates(tmp_path, vectors) == []
    assert semantic_duplicate_candidates(tmp_path, vectors, threshold=0.80) == [
        {"ids": ["/a", "/b"], "similarity": 0.83}
    ]


def test_jaccard_duplicates_not_double_reported(tmp_path):
    write_concept(tmp_path, "/a.md", {"type": "Project", "title": "Phase 1 MVP"})
    write_concept(tmp_path, "/b.md", {"type": "Project", "title": "Phase 1 MVP Overview"})
    vectors = {"a.md": [1.0, 0.0], "b.md": [1.0, 0.0]}
    # title Jaccard 0.75 >= NEAR_DUPLICATE_JACCARD: the structural pass owns it
    assert semantic_duplicate_candidates(tmp_path, vectors) == []
    report = organization_findings(tmp_path)
    assert [item["ids"] for item in report["near_duplicate_candidates"]] == [["/a", "/b"]]


def test_unembedded_concepts_invisible(tmp_path):
    write_concept(tmp_path, "/a.md", {"type": "Note", "title": "Alpha"})
    write_concept(tmp_path, "/b.md", {"type": "Note", "title": "Beta"})
    assert semantic_duplicate_candidates(tmp_path, {"a.md": [1.0, 0.0]}) == []


def test_leading_slash_vector_keys_tolerated(tmp_path):
    write_concept(tmp_path, "/a.md", {"type": "Note", "title": "Alpha"})
    write_concept(tmp_path, "/b.md", {"type": "Note", "title": "Beta"})
    vectors = {"/a.md": [1.0, 0.0], "/b.md": [1.0, 0.0]}
    assert semantic_duplicate_candidates(tmp_path, vectors) == [
        {"ids": ["/a", "/b"], "similarity": 1.0}
    ]


def test_sort_by_similarity_then_ids(tmp_path):
    write_concept(tmp_path, "/a.md", {"type": "Note", "title": "Alpha"})
    write_concept(tmp_path, "/b.md", {"type": "Note", "title": "Beta"})
    write_concept(tmp_path, "/c.md", {"type": "Note", "title": "Gamma"})
    vectors = {"a.md": [1.0, 0.0], "b.md": [1.0, 0.0], "c.md": [1.0, 0.3]}
    result = semantic_duplicate_candidates(tmp_path, vectors)
    assert result == [
        {"ids": ["/a", "/b"], "similarity": 1.0},
        {"ids": ["/a", "/c"], "similarity": 0.96},
        {"ids": ["/b", "/c"], "similarity": 0.96},
    ]


def test_pair_cap(tmp_path):
    titles = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta", "Eta", "Theta", "Iota"]
    vectors = {}
    for i, title in enumerate(titles):
        write_concept(tmp_path, f"/c{i}.md", {"type": "Note", "title": title})
        vectors[f"c{i}.md"] = [1.0, 0.0]
    result = semantic_duplicate_candidates(tmp_path, vectors)
    assert SEMANTIC_DUPLICATE_MAX_PAIRS == 20
    assert len(result) == SEMANTIC_DUPLICATE_MAX_PAIRS  # C(9,2) = 36 candidates, capped


# --- organize contract ----------------------------------------------------------


def test_organization_findings_emits_empty_semantic_key(tmp_path):
    write_concept(tmp_path, "/a.md", {"type": "Note", "title": "Alpha"})
    report = organization_findings(tmp_path)
    assert report["semantic_duplicate_candidates"] == []
    assert set(report) == set(FINDING_KEYS) | {"concepts_scanned", "since"}


def test_findings_empty_covers_semantic_key(tmp_path):
    report = organization_findings(tmp_path)
    assert findings_empty(report)
    report["semantic_duplicate_candidates"] = [{"ids": ["/a", "/b"], "similarity": 0.9}]
    assert not findings_empty(report)


# --- handle_curate merge ---------------------------------------------------------


class FakeDupEmbedService:
    """Fake EmbeddingService.load() seam for the curate merge."""

    def __init__(self, vectors):
        self._vectors = vectors

    def load(self):
        return {
            path: {"vector": vector, "model": "m", "dims": len(vector), "content_hash": "h"}
            for path, vector in self._vectors.items()
        }


class FakeBackend:
    """Status stub + A10 scan pass-throughs delegating over the tmp root."""

    def __init__(self, root):
        self._root = root

    def status(self):
        return {
            "stats": {"concepts": 0, "directories": 0, "versions": 0, "last_write": None},
            "health": {"orphans": [], "broken_links": [], "warnings": 0, "errors": 0},
            "healthy": True,
        }

    def link_health(self, paths: list[str]) -> dict:
        return {}

    def organization_findings(self, *, since=None):
        return organization_findings(self._root, since=since)

    def findings_empty(self, report):
        return findings_empty(report)

    def semantic_duplicate_candidates(self, vectors, *, since=None, threshold=None, model=None):
        if threshold is None:
            threshold = semantic_threshold_for_model(model)
        return semantic_duplicate_candidates(self._root, vectors, since=since, threshold=threshold)


class ScriptedProvider:
    """Returns a fixed queue of LLMResponses; records every complete() call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[list[dict], list[dict], LLMConfig]] = []

    async def complete(self, messages, tools, config) -> LLMResponse:
        self.calls.append((list(messages), list(tools), config))
        if not self.responses:
            return LLMResponse(text="(script exhausted)")
        return self.responses.pop(0)


def make_curate_librarian(root, provider, embedding_service=None) -> Librarian:
    config = LibrarianConfig(
        user_id="user-1",
        llm=LLMConfig(provider="openai", model="m", api_key="k"),
    )
    return Librarian(
        root,
        config,
        backend=FakeBackend(root),
        provider=provider,
        embedding_service=embedding_service,
    )


def make_threshold_librarian(root, embedding_service, config) -> Librarian:
    """Curate-seam Librarian with an explicit config (threshold resolution)."""
    return Librarian(
        root,
        config,
        backend=FakeBackend(root),
        provider=None,
        embedding_service=embedding_service,
    )


def test_semantic_duplicates_honors_config_override(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    write_concept(root, "/a.md", {"type": "Note", "title": "Alpha"})
    write_concept(root, "/b.md", {"type": "Note", "title": "Beta"})
    service = FakeDupEmbedService({"a.md": [1.0, 0.0], "b.md": unit_vector(0.9)})

    config = LibrarianConfig(
        user_id="user-1",
        embedding=EmbeddingConfig(source="local", model="m"),
        semantic_threshold=0.95,
    )
    assert make_threshold_librarian(root, service, config)._semantic_duplicates(None) == []

    config = LibrarianConfig(
        user_id="user-1",
        embedding=EmbeddingConfig(source="local", model="m"),
        semantic_threshold=0.85,
    )
    assert make_threshold_librarian(root, service, config)._semantic_duplicates(None) == [
        {"ids": ["/a", "/b"], "similarity": 0.9}
    ]


def test_semantic_duplicates_model_default_resolution(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    write_concept(root, "/a.md", {"type": "Note", "title": "Alpha"})
    write_concept(root, "/b.md", {"type": "Note", "title": "Beta"})
    service = FakeDupEmbedService({"a.md": [1.0, 0.0], "b.md": unit_vector(0.83)})

    config = LibrarianConfig(
        user_id="user-1",
        embedding=EmbeddingConfig(source="local", model="sentence-transformers/all-MiniLM-L6-v2"),
    )
    assert make_threshold_librarian(root, service, config)._semantic_duplicates(None) == [
        {"ids": ["/a", "/b"], "similarity": 0.83}
    ]

    config = LibrarianConfig(
        user_id="user-1",
        embedding=EmbeddingConfig(source="local", model="BAAI/bge-small-en-v1.5"),
    )
    assert make_threshold_librarian(root, service, config)._semantic_duplicates(None) == []


async def test_handle_curate_merges_semantic_findings(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    write_concept(root, "/a.md", {"type": "Note", "title": "Alpha"})
    write_concept(root, "/b.md", {"type": "Note", "title": "Beta"})
    provider = ScriptedProvider([LLMResponse(text="curated")])
    librarian = make_curate_librarian(
        root, provider, FakeDupEmbedService({"a.md": [1.0, 0.0], "b.md": [1.0, 0.0]})
    )

    result = await librarian.handle_curate()

    # a semantic-only finding triggers the curate run
    assert provider.calls
    assert result["findings"]["semantic_duplicate_candidates"] == [
        {"ids": ["/a", "/b"], "similarity": 1.0}
    ]
    assert result["findings"]["concepts_scanned"] == 2
    task = provider.calls[0][0][1]["content"]
    assert "/a <-> /b (similarity 1.0; semantic)" in task
    # unresolved by the scripted no-op run: still un-converged on the re-scan
    assert result["organized"] is False


async def test_handle_curate_noop_zero_llm_when_all_five_empty(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    provider = ScriptedProvider([LLMResponse(text="should not be used")])
    librarian = make_curate_librarian(root, provider, FakeDupEmbedService({}))

    result = await librarian.handle_curate()

    assert provider.calls == []  # no LLM call on the no-op path
    assert result["findings"]["semantic_duplicate_candidates"] == []
    assert findings_empty(result["findings"])
    assert result["organized"] is True


async def test_handle_curate_without_embeddings_keeps_empty_semantic_key(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    provider = ScriptedProvider([LLMResponse(text="should not be used")])
    librarian = make_curate_librarian(root, provider, embedding_service=None)

    result = await librarian.handle_curate()

    assert result["findings"]["semantic_duplicate_candidates"] == []
    assert provider.calls == []


async def test_handle_curate_semantic_failure_continues_without_it(tmp_path):
    class BrokenEmbedService:
        def load(self):
            raise RuntimeError("db down")

    root = tmp_path / "lib"
    root.mkdir()
    write_concept(root, "/thin.md", {"type": "Note", "title": "Thin"}, body="x")
    provider = ScriptedProvider([LLMResponse(text="curated")])
    librarian = make_curate_librarian(root, provider, BrokenEmbedService())

    result = await librarian.handle_curate()

    # the thin-concept finding still drives the run; the semantic key degrades to []
    assert result["findings"]["semantic_duplicate_candidates"] == []
    assert provider.calls
    assert "CURATION TASK" in provider.calls[0][0][1]["content"]


def test_unscoped_pair_persists_until_fixed(tmp_path):
    """L14: an unscoped scan re-reports an unaddressed duplicate pair every
    run; it drops out only when one member is actually fixed."""
    write_concept(tmp_path, "/a.md", {"type": "Note", "title": "Alpha Topic"})
    write_concept(tmp_path, "/b.md", {"type": "Note", "title": "Beta Subject"})
    vectors = {"a.md": [1.0, 0.0], "b.md": [1.0, 0.0]}
    expected = [{"ids": ["/a", "/b"], "similarity": 1.0}]
    assert semantic_duplicate_candidates(tmp_path, vectors) == expected
    assert semantic_duplicate_candidates(tmp_path, vectors) == expected  # unaddressed

    # fixed: /b rewritten with a different vector -> the pair drops out
    vectors["b.md"] = unit_vector(0.1)
    assert semantic_duplicate_candidates(tmp_path, vectors) == []


# --- A2: bounded scans (sub-quadratic pair work) ------------------------------------


def test_semantic_scan_uses_no_pure_python_pairwise_cosine(tmp_path, monkeypatch):
    """A2: with numpy available the similarity pass is vectorized — zero
    pure-Python pairwise cosine calls over all pairs."""
    if importlib.util.find_spec("numpy") is None:
        pytest.skip("numpy not installed (stdlib pairwise fallback)")
    for i in range(40):
        write_concept(tmp_path, f"/c{i}.md", {"type": "Note", "title": f"Unique{i} Solo{i}"})
    # 2-d unit vectors spread by angle; nearest neighbors exceed 0.85
    vectors = {f"c{i}.md": [math.cos(i * 0.5), math.sin(i * 0.5)] for i in range(40)}
    calls = []
    real_cosine = semantic_mod.cosine

    def counting(a, b):
        calls.append(1)
        return real_cosine(a, b)

    monkeypatch.setattr(semantic_mod, "cosine", counting)
    result = semantic_duplicate_candidates(tmp_path, vectors)
    assert calls == []  # vectorized: no stdlib pairwise cosine at all
    assert result  # adjacent-angle pairs are still found (whole-library scan)


def test_near_duplicate_scan_is_subquadratic(tmp_path, monkeypatch):
    """A2: inverted-index + size-bound candidate generation keeps full
    Jaccard evaluations sub-quadratic in the concept count."""
    n_distinct = 45
    for i in range(n_distinct):
        write_concept(tmp_path, f"/u{i}.md", {"type": "Note", "title": f"Unique{i} Solo{i}"})
    clusters = 5
    for k in range(clusters):
        write_concept(tmp_path, f"/k{k}a.md", {"type": "Note", "title": f"Common{k} Alpha Beta"})
        write_concept(tmp_path, f"/k{k}b.md", {"type": "Note", "title": f"Common{k} Alpha Gamma"})
        write_concept(tmp_path, f"/k{k}c.md", {"type": "Note", "title": f"Common{k} Beta Gamma"})
    n = n_distinct + 3 * clusters
    calls = []
    real_jaccard = organize_mod._jaccard

    def counting(a, b):
        calls.append(1)
        return real_jaccard(a, b)

    monkeypatch.setattr(organize_mod, "_jaccard", counting)
    organize_mod.organization_findings(tmp_path)
    total_pairs = n * (n - 1) // 2
    # candidate-driven: only token-sharing, size-compatible pairs are evaluated
    assert 0 < len(calls) <= 3 * n
    assert len(calls) < total_pairs // 4
