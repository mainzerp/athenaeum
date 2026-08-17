"""Tests for hybrid search (HYBRID_SEARCH plan): FTS5 schema, query
sanitizing, RRF fusion, FtsIndex CRUD/lifecycle, EmbeddingService wiring,
the backend hybrid path, and the CrossEncoderReranker seam.

Conventions mirror tests/test_embeddings.py: real tmp app.db + tmp library
roots, asyncio_mode=auto, fake providers/rerankers. DB-backed tests skip when
the runtime SQLite lacks FTS5 (degraded mode is the contract there).
"""

import sqlite3
import sys
import types
from contextlib import closing

import pytest

from athenaeum import db
from athenaeum.embeddings import EmbeddingService, concept_text, content_hash
from athenaeum.fts import FtsIndex
from athenaeum.librarian.embed import KIND_DOCUMENT, EmbeddingConfig
from athenaeum.library.backend import LibraryBackend
from athenaeum.library.hybrid import (
    HYBRID_FTS_TABLE,
    HYBRID_QUERY_TOKEN_CAP,
    HYBRID_RERANK_CANDIDATES,
    HYBRID_RERANK_MODEL,
    HYBRID_RERANK_TEXT_CHARS,
    CrossEncoderReranker,
    rrf_merge,
    sanitize_match_query,
)


def _probe_fts5() -> bool:
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='porter unicode61')")
        conn.close()
        return True
    except sqlite3.OperationalError:
        return False


FTS5_AVAILABLE = _probe_fts5()
requires_fts5 = pytest.mark.skipif(not FTS5_AVAILABLE, reason="FTS5 unavailable in this runtime")


def make_db(tmp_path):
    db_path = tmp_path / "app.db"
    db.init_db(db_path)
    with closing(db.connect(db_path)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO users (id, username, password_hash, created_at)"
                " VALUES ('user-1', 'alice', 'hash', '2026-01-01T00:00:00Z')"
            )
            conn.execute(
                "INSERT INTO users (id, username, password_hash, created_at)"
                " VALUES ('user-2', 'bob', 'hash', '2026-01-01T00:00:00Z')"
            )
            conn.execute("INSERT INTO librarian_configs (user_id) VALUES ('user-1')")
    return db_path


def make_backend(root):
    return LibraryBackend(root, actor="test-hybrid", git_enabled=False)


def write_concept(root, rel: str, title: str, body: str, description: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    desc = f"description: {description}\n" if description else ""
    path.write_text(f"---\ntitle: {title}\n{desc}---\n{body}\n", encoding="utf-8")


# --- schema / probe ----------------------------------------------------------


@requires_fts5
def test_init_db_creates_concepts_fts_and_stays_idempotent(tmp_path):
    db_path = make_db(tmp_path)
    db.init_db(db_path)  # second run must not raise
    with closing(db.connect(db_path)) as conn:
        names = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'concepts_fts%'"
            )
        }
        assert "concepts_fts" in names
        assert "concepts_fts_data" in names  # shadow tables share app.db
        # fresh librarian_configs rows default to hybrid-on, rerank off (0.23.0)
        row = db.get_config(conn, "user-1")
        assert row["hybrid_search"] == 1
        assert row["hybrid_rerank"] == 0


@requires_fts5
def test_bm25_lower_is_better(tmp_path):
    """Probe result pinned (analysis §9.3): ORDER BY bm25 ASC ranks the
    better match first; scores are negative."""
    db_path = make_db(tmp_path)
    fts = FtsIndex(db_path, "user-1")
    fts.upsert("a.md", "hybrid search fusion retrieval rank", "h1")
    fts.upsert("b.md", "hybrid banana apple orange grape melon fruit salad bowl", "h2")
    hits = fts.search("hybrid search", 10)
    assert [path for path, _ in hits] == ["a.md", "b.md"]
    assert hits[0][1] < hits[1][1] < 0


# --- sanitize_match_query -----------------------------------------------------


def test_sanitize_match_query_neutralizes_operators():
    match = sanitize_match_query('hello AND world OR (NOT x): ^ * "quoted" NEAR/2')
    assert match == (
        '"hello" OR "AND" OR "world" OR "OR" OR "NOT" OR "x" OR "quoted" OR "NEAR" OR "2"'
    )


def test_sanitize_match_query_dedupes_and_caps():
    assert sanitize_match_query("alpha alpha beta") == '"alpha" OR "beta"'
    tokens = " ".join(f"t{i}" for i in range(HYBRID_QUERY_TOKEN_CAP + 5))
    match = sanitize_match_query(tokens)
    assert match.count("OR") + 1 == HYBRID_QUERY_TOKEN_CAP
    assert f'"t{HYBRID_QUERY_TOKEN_CAP}"' not in match


def test_sanitize_match_query_tokenless_returns_none():
    assert sanitize_match_query("") is None
    assert sanitize_match_query("!!! ... ---") is None


@requires_fts5
def test_sanitized_query_executes_literally(tmp_path):
    """FTS5 operators in user input must not error or reprogram the MATCH."""
    db_path = make_db(tmp_path)
    fts = FtsIndex(db_path, "user-1")
    fts.upsert("a.md", "alpha beta gamma", "h1")
    hits = fts.search('alpha OR (NOT beta): "weird" *', 10)
    assert [path for path, _ in hits] == ["a.md"]


# --- rrf_merge -----------------------------------------------------------------


def test_rrf_merge_handcomputed():
    fused = rrf_merge([["a.md", "b.md"], ["b.md", "c.md"]], k=60)
    scores = dict(fused)
    assert scores["a.md"] == pytest.approx(1 / 61)
    assert scores["b.md"] == pytest.approx(1 / 62 + 1 / 61)
    assert scores["c.md"] == pytest.approx(1 / 62)
    assert [path for path, _ in fused] == ["b.md", "a.md", "c.md"]


def test_rrf_merge_single_leg_doc_still_ranks():
    fused = rrf_merge([["a.md"], []])
    assert fused == [("a.md", pytest.approx(1 / 61))]


def test_rrf_merge_deterministic_tiebreak_by_path():
    # both docs at rank 1 in one leg each -> equal scores -> path order
    fused = rrf_merge([["b.md"], ["a.md"]])
    assert [path for path, _ in fused] == ["a.md", "b.md"]
    assert fused[0][1] == fused[1][1]


# --- FtsIndex CRUD + tenancy ----------------------------------------------------


@requires_fts5
def test_fts_index_crud_and_tenancy(tmp_path):
    db_path = make_db(tmp_path)
    fts = FtsIndex(db_path, "user-1")
    other = FtsIndex(db_path, "user-2")
    assert fts.available
    fts.upsert("a.md", "retrieval augmented generation", "h1")
    assert [p for p, _ in fts.search("retrieval", 10)] == ["a.md"]
    assert other.search("retrieval", 10) == []  # tenancy filter
    assert fts.hashes() == {"a.md": "h1"}

    fts.delete("a.md")
    assert fts.search("retrieval", 10) == []
    assert fts.hashes() == {}


@requires_fts5
def test_fts_index_reupsert_replaces_row(tmp_path):
    db_path = make_db(tmp_path)
    fts = FtsIndex(db_path, "user-1")
    fts.upsert("a.md", "old text about apples", "h1")
    fts.upsert("a.md", "new text about oranges", "h2")
    assert fts.search("apples", 10) == []
    assert [p for p, _ in fts.search("oranges", 10)] == ["a.md"]
    assert fts.hashes() == {"a.md": "h2"}  # exactly one row


@requires_fts5
def test_fts_index_canonicalizes_keys(tmp_path):
    db_path = make_db(tmp_path)
    fts = FtsIndex(db_path, "user-1")
    fts.upsert("/x/a.md", "canonical key shapes", "h1")
    fts.delete("x/a.md")  # same row, slash-less spelling
    assert fts.hashes() == {}


@requires_fts5
def test_fts_index_unavailable_returns_empty(tmp_path):
    # no init_db -> no concepts_fts table -> degraded, never an error
    db_path = tmp_path / "app.db"
    fts = FtsIndex(db_path, "user-1")
    assert fts.available is False
    assert fts.search("anything", 10) == []
    fts.sync_writes(make_backend(tmp_path / "lib"), [{"id": "/a", "action": "created"}])
    fts.reconcile(make_backend(tmp_path / "lib"))


# --- FtsIndex.sync_writes -------------------------------------------------------


@requires_fts5
async def test_fts_sync_writes_created_and_updated(tmp_path):
    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    root.mkdir()
    backend = make_backend(root)
    fts = FtsIndex(db_path, "user-1")
    write_concept(root, "a.md", "Alpha", "first body")

    fts.sync_writes(backend, [{"id": "/a", "action": "created"}])
    assert [p for p, _ in fts.search("first", 10)] == ["a.md"]

    write_concept(root, "a.md", "Alpha", "second body")
    fts.sync_writes(backend, [{"id": "/a", "action": "updated"}])
    assert fts.search("first", 10) == []
    assert [p for p, _ in fts.search("second", 10)] == ["a.md"]


@requires_fts5
async def test_fts_sync_writes_deleted_and_moved(tmp_path):
    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    root.mkdir()
    backend = make_backend(root)
    fts = FtsIndex(db_path, "user-1")
    write_concept(root, "a.md", "Alpha", "movable body")
    fts.sync_writes(backend, [{"id": "/a", "action": "created"}])

    # move: the OLD path row must go (L8), the new path is indexed
    (root / "a.md").rename(root / "b.md")
    fts.sync_writes(backend, [{"id": "/b", "action": "moved", "from_id": "/a"}])
    assert fts.hashes().keys() == {"b.md"}
    assert [p for p, _ in fts.search("movable", 10)] == ["b.md"]

    fts.sync_writes(backend, [{"id": "/b", "action": "deleted"}])
    assert fts.hashes() == {}


@requires_fts5
async def test_fts_sync_writes_create_then_delete_leaves_no_row(tmp_path):
    """L8: a create-then-delete in one run must not resurrect a row."""
    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    root.mkdir()
    backend = make_backend(root)
    fts = FtsIndex(db_path, "user-1")
    write_concept(root, "a.md", "Alpha", "ephemeral body")
    (root / "a.md").unlink()
    fts.sync_writes(
        backend,
        [{"id": "/a", "action": "created"}, {"id": "/a", "action": "deleted"}],
    )
    assert fts.hashes() == {}


@requires_fts5
async def test_fts_sync_writes_skips_unreadable_and_never_raises(tmp_path):
    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    root.mkdir()
    backend = make_backend(root)
    fts = FtsIndex(db_path, "user-1")
    write_concept(root, "ok.md", "Okay", "readable body")
    fts.sync_writes(
        backend,
        [{"id": "/gone", "action": "created"}, {"id": "/ok", "action": "created"}],
    )
    assert fts.hashes().keys() == {"ok.md"}


# --- FtsIndex.reconcile ---------------------------------------------------------


@requires_fts5
def test_fts_reconcile_backfills_and_drops_vanished(tmp_path):
    """The 0.18 -> 0.19 upgrade path: pre-existing docs get FTS rows."""
    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    root.mkdir()
    backend = make_backend(root)
    write_concept(root, "a.md", "Alpha", "pre-existing body")
    write_concept(root, "sub/b.md", "Beta", "nested body")
    fts = FtsIndex(db_path, "user-1")
    fts.upsert("ghost.md", "vanished concept text", "h-ghost")

    fts.reconcile(backend)

    assert set(fts.hashes()) == {"a.md", "sub/b.md"}
    assert [p for p, _ in fts.search("pre-existing", 10)] == ["a.md"]
    assert fts.search("vanished", 10) == []


@requires_fts5
def test_fts_reconcile_skips_hash_clean_rows(tmp_path):
    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    root.mkdir()
    backend = make_backend(root)
    write_concept(root, "a.md", "Alpha", "stable body")
    fts = FtsIndex(db_path, "user-1")
    doc = backend.read_document("a.md")
    text = concept_text(doc["frontmatter"], doc["body"])
    # correct hash but sentinel text: a rewrite would replace the sentinel
    fts.upsert("a.md", "sentinel text not in the document", content_hash(text))

    fts.reconcile(backend)

    assert [p for p, _ in fts.search("sentinel", 10)] == ["a.md"]  # untouched
    assert fts.search("stable", 10) == []


@requires_fts5
async def test_fts_and_embeddings_store_identical_link_stripped_text(tmp_path):
    """Lockstep pin: both legs index the same link-stripped concept_text."""
    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    root.mkdir()
    backend = make_backend(root)
    body = "see the [caching strategy](/athenaeum/lessons/caching.md) note"
    write_concept(root, "a.md", "Alpha", body)
    fts = FtsIndex(db_path, "user-1")
    service = make_service(db_path, fts=fts)

    await service.sync_writes(backend, [{"id": "/a", "action": "created"}])

    doc = backend.read_document("a.md")
    expected = concept_text(doc["frontmatter"], doc["body"])
    assert "/athenaeum/lessons/caching.md" not in expected  # link stripped
    assert "caching strategy" in expected  # anchor text kept
    stored = service.load()
    assert stored["a.md"]["content_hash"] == content_hash(expected)
    with closing(db.connect(db_path)) as conn:
        row = conn.execute(
            f"SELECT text FROM {HYBRID_FTS_TABLE}"
            " WHERE user_id = 'user-1' AND concept_path = 'a.md'"
        ).fetchone()
    assert row["text"] == expected


@requires_fts5
def test_fts_reconcile_never_raises_on_unreadable_doc(tmp_path):
    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    root.mkdir()
    backend = make_backend(root)
    (root / "bad.md").write_bytes(b"\xff\xfe invalid utf-8 \x00")
    write_concept(root, "ok.md", "Okay", "good body")
    fts = FtsIndex(db_path, "user-1")
    fts.reconcile(backend)
    assert fts.hashes().keys() == {"ok.md"}


@requires_fts5
async def test_fts_sync_writes_deprecated_deletes_row(tmp_path):
    """A deprecated write removes the FTS row, exactly like deleted."""
    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    root.mkdir()
    backend = make_backend(root)
    fts = FtsIndex(db_path, "user-1")
    write_concept(root, "a.md", "Alpha", "searchable body")
    fts.sync_writes(backend, [{"id": "/a", "action": "created"}])
    assert fts.hashes().keys() == {"a.md"}

    fts.sync_writes(backend, [{"id": "/a", "action": "deprecated"}])
    assert fts.hashes() == {}
    assert fts.search("searchable", 10) == []


@requires_fts5
def test_fts_reconcile_skips_deprecated_on_disk(tmp_path):
    """Deprecated on-disk concepts are not indexed; a stale row drops out."""
    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    root.mkdir()
    backend = make_backend(root)
    write_concept(root, "a.md", "Alpha", "live body")
    (root / "old.md").write_text(
        "---\ntitle: Old\nstatus: deprecated\n---\nold body\n", encoding="utf-8"
    )
    fts = FtsIndex(db_path, "user-1")
    fts.upsert("old.md", "stale deprecated text", "h-stale")

    fts.reconcile(backend)

    assert set(fts.hashes()) == {"a.md"}
    assert fts.search("deprecated", 10) == []


# --- EmbeddingService wiring -----------------------------------------------------


class FakeEmbeddingProvider:
    async def embed(self, texts, config, *, kind=KIND_DOCUMENT):
        return [[float(len(text)), 1.0, 0.0] for text in texts]


class RecordingFts:
    """Duck-typed FtsIndex stand-in recording the lifecycle calls."""

    def __init__(self):
        self.synced: list[tuple[object, list[dict]]] = []
        self.reconciled: list[object] = []
        self.search_result: list[tuple[str, float]] = []

    @property
    def available(self):
        return True

    def sync_writes(self, backend, writes):
        self.synced.append((backend, list(writes)))

    def reconcile(self, backend):
        self.reconciled.append(backend)

    def search(self, query, limit):
        return list(self.search_result)


def make_service(db_path, fts=None, provider=None):
    config = EmbeddingConfig(source="api", model="test-model", provider="openai", api_key="k")
    return EmbeddingService(db_path, "user-1", config, provider or FakeEmbeddingProvider(), fts=fts)


async def test_service_drives_fts_sync_writes(tmp_path):
    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    root.mkdir()
    backend = make_backend(root)
    write_concept(root, "a.md", "Alpha", "body")
    fts = RecordingFts()
    service = make_service(db_path, fts=fts)
    writes = [{"id": "/a", "action": "created"}]
    await service.sync_writes(backend, writes)
    assert fts.synced == [(backend, writes)]


async def test_service_drives_fts_reconcile_inside_claim(tmp_path):
    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    root.mkdir()
    backend = make_backend(root)
    write_concept(root, "a.md", "Alpha", "body")
    fts = RecordingFts()
    service = make_service(db_path, fts=fts)
    await service.reconcile(backend)
    assert fts.reconciled == [backend]
    assert service.load().keys() == {"a.md"}  # the embed reconcile still ran


class RaisingProvider:
    async def embed(self, texts, config, *, kind=KIND_DOCUMENT):
        raise RuntimeError("provider down")


async def test_service_fts_sync_lands_even_when_embed_fails(tmp_path):
    """FTS rows are written first: the lexical leg survives provider outages."""
    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    root.mkdir()
    backend = make_backend(root)
    write_concept(root, "a.md", "Alpha", "body")
    fts = RecordingFts()
    service = make_service(db_path, fts=fts, provider=RaisingProvider())
    writes = [{"id": "/a", "action": "created"}]
    await service.sync_writes(backend, writes)  # never raises
    assert fts.synced == [(backend, writes)]
    assert service.load() == {}  # embed failed, no vectors


async def test_service_fts_search_delegates_and_defaults_empty(tmp_path):
    db_path = make_db(tmp_path)
    fts = RecordingFts()
    fts.search_result = [("a.md", -1.5)]
    assert make_service(db_path, fts=fts).fts_search("q", 5) == [("a.md", -1.5)]
    assert make_service(db_path).fts_search("q", 5) == []  # no collaborator


# --- backend hybrid path ----------------------------------------------------------


class FakeHybridService:
    """Fake embedding service with an FTS collaborator (the hybrid gate)."""

    def __init__(self, fts, ranked=None, error=None):
        self.fts = fts
        self.ranked = ranked or []
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def search_ids(self, query, limit):
        self.calls.append((query, limit))
        if self.error is not None:
            raise self.error
        return list(self.ranked)[:limit]

    def fts_search(self, query, limit):
        return self.fts.search(query, limit)


class FakeReranker:
    def __init__(self, scores):
        self.scores = scores
        self.calls: list[tuple[str, list[str]]] = []

    async def rerank(self, query, texts):
        self.calls.append((query, list(texts)))
        return self.scores


@requires_fts5
def make_hybrid_backend(tmp_path, service, **kwargs):
    root = tmp_path / "lib"
    root.mkdir(exist_ok=True)
    return LibraryBackend(
        root, actor="test/0", git_enabled=False, embedding_service=service, **kwargs
    )


@requires_fts5
async def test_hybrid_fusion_order_and_hit_shape(tmp_path):
    db_path = make_db(tmp_path)
    fts = FtsIndex(db_path, "user-1")
    service = FakeHybridService(fts, ranked=[("/a", 0.9), ("/b", 0.8)])
    backend = make_hybrid_backend(tmp_path, service, reranker=None)
    write_concept(backend.root, "a.md", "Alpha", "semantic only")
    write_concept(backend.root, "b.md", "Beta", "lexical term plus semantic")
    write_concept(backend.root, "c.md", "Gamma", "lexical term only")
    fts.upsert("b.md", "lexical term plus semantic", "h")
    fts.upsert("c.md", "lexical term only", "h")

    hits = await backend.search_semantic("lexical term", 8)

    # leg_k = max(8, HYBRID_RERANK_CANDIDATES) for the semantic leg
    assert service.calls == [("lexical term", max(8, HYBRID_RERANK_CANDIDATES))]
    # b: sem rank 2 + lex rank 1; a: sem rank 1; c: lex rank 2 -> b, a, c
    assert [hit["id"] for hit in hits] == ["/b", "/a", "/c"]
    assert hits[0]["path"] == "/b.md"
    assert hits[0]["title"] == "Beta"
    assert hits[0]["type"] is None
    # RRF score: 1/62 + 1/61, rounded to two decimals
    assert hits[0]["score"] == round(1 / 62 + 1 / 61, 2)


@requires_fts5
async def test_hybrid_disabled_keeps_legacy_cosine_contract(tmp_path):
    db_path = make_db(tmp_path)
    fts = FtsIndex(db_path, "user-1")
    service = FakeHybridService(fts, ranked=[("/a", 0.913)])
    backend = make_hybrid_backend(tmp_path, service, hybrid_search=False)
    write_concept(backend.root, "a.md", "Alpha", "body")

    hits = await backend.search_semantic("q", 5)

    assert service.calls == [("q", 5)]  # legacy leg size, not leg_k
    assert hits[0]["score"] == 0.91  # cosine score passed through


@requires_fts5
async def test_hybrid_unavailable_fts_keeps_legacy_path(tmp_path):
    class UnavailableFts(RecordingFts):
        @property
        def available(self):
            return False

    service = FakeHybridService(UnavailableFts(), ranked=[("/a", 0.5)])
    backend = make_hybrid_backend(tmp_path, service)
    write_concept(backend.root, "a.md", "Alpha", "body")
    hits = await backend.search_semantic("q", 4)
    assert service.calls == [("q", 4)]
    assert hits[0]["score"] == 0.5


@requires_fts5
async def test_hybrid_reranker_reorders_and_scores_by_logit(tmp_path):
    db_path = make_db(tmp_path)
    fts = FtsIndex(db_path, "user-1")
    service = FakeHybridService(fts, ranked=[("/a", 0.9), ("/b", 0.8)])
    reranker = FakeReranker([0.25, 3.14159])
    backend = make_hybrid_backend(tmp_path, service, hybrid_rerank=True, reranker=reranker)
    write_concept(backend.root, "a.md", "Alpha", "body a")
    write_concept(backend.root, "b.md", "Beta", "body b")

    hits = await backend.search_semantic("q", 8)

    assert len(reranker.calls) == 1
    query, texts = reranker.calls[0]
    assert query == "q"
    doc_a = backend.read_document("a.md")
    doc_b = backend.read_document("b.md")
    assert texts == [
        concept_text(doc_a["frontmatter"], doc_a["body"]),
        concept_text(doc_b["frontmatter"], doc_b["body"]),
    ]
    assert [hit["id"] for hit in hits] == ["/b", "/a"]  # logit order
    assert hits[0]["score"] == 3.14  # rounded logit
    assert hits[1]["score"] == 0.25


@requires_fts5
async def test_hybrid_reranker_none_keeps_rrf_order(tmp_path):
    db_path = make_db(tmp_path)
    fts = FtsIndex(db_path, "user-1")
    service = FakeHybridService(fts, ranked=[("/a", 0.9), ("/b", 0.8)])
    backend = make_hybrid_backend(tmp_path, service, reranker=FakeReranker(None))
    write_concept(backend.root, "a.md", "Alpha", "body a")
    write_concept(backend.root, "b.md", "Beta", "body b")

    hits = await backend.search_semantic("q", 8)

    assert [hit["id"] for hit in hits] == ["/a", "/b"]  # fused order kept


@requires_fts5
async def test_hybrid_rerank_disabled_by_config(tmp_path):
    db_path = make_db(tmp_path)
    fts = FtsIndex(db_path, "user-1")
    service = FakeHybridService(fts, ranked=[("/a", 0.9), ("/b", 0.8)])
    reranker = FakeReranker([0.1, 9.9])
    backend = make_hybrid_backend(tmp_path, service, hybrid_rerank=False, reranker=reranker)
    write_concept(backend.root, "a.md", "Alpha", "body a")
    write_concept(backend.root, "b.md", "Beta", "body b")

    hits = await backend.search_semantic("q", 8)

    assert reranker.calls == []  # never invoked
    assert [hit["id"] for hit in hits] == ["/a", "/b"]


@requires_fts5
async def test_hybrid_skips_unreadable_candidates(tmp_path):
    db_path = make_db(tmp_path)
    fts = FtsIndex(db_path, "user-1")
    service = FakeHybridService(fts, ranked=[("/gone", 0.9), ("/a", 0.8)])
    reranker = FakeReranker([1.0])
    backend = make_hybrid_backend(tmp_path, service, hybrid_rerank=True, reranker=reranker)
    write_concept(backend.root, "a.md", "Alpha", "body a")

    hits = await backend.search_semantic("q", 8)

    assert [hit["id"] for hit in hits] == ["/a"]
    # the unreadable candidate never reaches the reranker
    assert len(reranker.calls[0][1]) == 1


@requires_fts5
async def test_hybrid_rerank_truncates_long_documents(tmp_path):
    """Rerank texts are capped at HYBRID_RERANK_TEXT_CHARS; the
    title/description prefix survives the truncation."""
    db_path = make_db(tmp_path)
    fts = FtsIndex(db_path, "user-1")
    service = FakeHybridService(fts, ranked=[("/a", 0.9)])
    reranker = FakeReranker([1.0])
    backend = make_hybrid_backend(tmp_path, service, hybrid_rerank=True, reranker=reranker)
    write_concept(backend.root, "a.md", "Alpha", "body " + "x" * 5000)

    hits = await backend.search_semantic("q", 8)

    assert [hit["id"] for hit in hits] == ["/a"]
    query, texts = reranker.calls[0]
    assert len(texts) == 1
    assert len(texts[0]) <= HYBRID_RERANK_TEXT_CHARS
    assert texts[0].startswith("Alpha")


@requires_fts5
async def test_hybrid_limit_zero_returns_empty(tmp_path):
    db_path = make_db(tmp_path)
    fts = FtsIndex(db_path, "user-1")
    service = FakeHybridService(fts, ranked=[("/a", 0.9)])
    backend = make_hybrid_backend(tmp_path, service)
    write_concept(backend.root, "a.md", "Alpha", "body a")
    assert await backend.search_semantic("q", 0) == []


@requires_fts5
async def test_hybrid_empty_lexical_leg_keeps_semantic_order(tmp_path):
    db_path = make_db(tmp_path)
    fts = FtsIndex(db_path, "user-1")  # empty index: no lexical hits
    service = FakeHybridService(fts, ranked=[("/a", 0.9), ("/b", 0.8)])
    backend = make_hybrid_backend(tmp_path, service)
    write_concept(backend.root, "a.md", "Alpha", "body a")
    write_concept(backend.root, "b.md", "Beta", "body b")

    hits = await backend.search_semantic("no lexical match here", 8)

    assert [hit["id"] for hit in hits] == ["/a", "/b"]


@requires_fts5
async def test_hybrid_semantic_failure_falls_back_to_metadata(tmp_path):
    db_path = make_db(tmp_path)
    fts = FtsIndex(db_path, "user-1")
    service = FakeHybridService(fts, error=RuntimeError("embed down"))
    backend = make_hybrid_backend(tmp_path, service)
    write_concept(backend.root, "a.md", "Alpha", "body a")
    write_concept(backend.root, "b.md", "Beta", "body b")

    hits = await backend.search_semantic("alpha")

    assert [hit["id"] for hit in hits] == ["/a"]
    assert all(hit["fallback"] is True for hit in hits)


# --- CrossEncoderReranker -----------------------------------------------------------


class _FakeTextCrossEncoder:
    instances: list["_FakeTextCrossEncoder"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.rerank_calls = []
        _FakeTextCrossEncoder.instances.append(self)

    def rerank(self, query, documents):
        documents = list(documents)
        self.rerank_calls.append((query, documents))
        return [float(i) for i, _ in enumerate(documents)]


def install_fake_cross_encoder(monkeypatch):
    _FakeTextCrossEncoder.instances = []
    for name in ("fastembed", "fastembed.rerank", "fastembed.rerank.cross_encoder"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    sys.modules["fastembed.rerank.cross_encoder"].TextCrossEncoder = _FakeTextCrossEncoder


async def test_reranker_constructs_model_lazily_and_maps_scores(monkeypatch, tmp_path):
    install_fake_cross_encoder(monkeypatch)
    reranker = CrossEncoderReranker(cache_dir=tmp_path / "models")
    assert _FakeTextCrossEncoder.instances == []  # no load at construction

    scores = await reranker.rerank("q", ["doc a", "doc b"])

    assert scores == [0.0, 1.0]
    assert len(_FakeTextCrossEncoder.instances) == 1
    assert _FakeTextCrossEncoder.instances[0].kwargs == {
        "model_name": HYBRID_RERANK_MODEL,
        "cache_dir": str(tmp_path / "models"),
    }
    # memoized: a second rerank constructs nothing new
    await reranker.rerank("q", ["doc a"])
    assert len(_FakeTextCrossEncoder.instances) == 1


async def test_reranker_empty_texts_short_circuits(monkeypatch):
    install_fake_cross_encoder(monkeypatch)
    reranker = CrossEncoderReranker()
    assert await reranker.rerank("q", []) == []
    assert _FakeTextCrossEncoder.instances == []


async def test_reranker_import_error_returns_none_and_sticks(monkeypatch):
    monkeypatch.setitem(sys.modules, "fastembed", None)  # import raises ImportError
    reranker = CrossEncoderReranker()
    assert await reranker.rerank("q", ["doc"]) is None
    assert reranker._unavailable
    assert await reranker.rerank("q", ["doc"]) is None  # no retry storm


async def test_reranker_model_failure_returns_none(monkeypatch):
    install_fake_cross_encoder(monkeypatch)

    def boom(self, query, documents):
        raise RuntimeError("onnx exploded")

    _FakeTextCrossEncoder.rerank = boom
    reranker = CrossEncoderReranker()
    assert await reranker.rerank("q", ["doc"]) is None
