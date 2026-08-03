"""Tests for the embedding subsystem (VECTOR_SEARCH plan §8.1, Phases 1-2).

Store CRUD against a real tmp app.db; API providers mocked with respx; the
local provider tested against a monkeypatched fake ``fastembed`` module (the
real dependency is an optional extra and absent here); config resolution via
``LibrarianManager._load_config``; ``sync_writes``/``reconcile`` against tmp
library roots with a deterministic fake provider.
"""

import asyncio
import hashlib
import json
import math
import sys
import types
from contextlib import closing

import httpx
import pytest
import respx

from athenaeum import db
from athenaeum.embeddings import (
    EmbeddingService,
    EmbedStatusRegistry,
    concept_text,
    content_hash,
    cosine,
)
from athenaeum.librarian.agent import Librarian, LibrarianConfig
from athenaeum.librarian.embed import (
    KIND_DOCUMENT,
    KIND_QUERY,
    EmbeddingConfig,
    EmbeddingProviderError,
    create_embedding_provider,
)
from athenaeum.librarian.embed.local import LOCAL_MODEL_SHORTLIST, LocalFastembedProvider
from athenaeum.librarian.manager import LibrarianManager
from athenaeum.library.backend import LibraryBackend


def make_backend(root):
    """Read-side LibraryBackend over a bare tmp root (A10: scans go through it)."""
    return LibraryBackend(root, actor="test-embed")


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


def make_connections_db(tmp_path):
    """app.db with an openai default connection and a gemini second connection."""
    db_path = make_db(tmp_path)
    with closing(db.connect(db_path)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO provider_configs"
                " (id, user_id, label, provider, api_key_enc, base_url, is_default, created_at)"
                " VALUES ('conn-a', 'user-1', 'Default', 'openai', 'enc-a', NULL, 1,"
                " '2026-01-01T00:00:00Z')"
            )
            conn.execute(
                "INSERT INTO provider_configs"
                " (id, user_id, label, provider, api_key_enc, base_url, is_default, created_at)"
                " VALUES ('conn-g', 'user-1', 'Gemini', 'gemini', 'enc-g', NULL, 0,"
                " '2026-01-02T00:00:00Z')"
            )
    return db_path


def set_embedding_row(db_path, source, model, connection_id=None) -> None:
    with closing(db.connect(db_path)) as conn:
        with conn:
            conn.execute(
                "UPDATE librarian_configs SET embedding_source = ?, embedding_model = ?,"
                " embedding_connection_id = ? WHERE user_id = 'user-1'",
                (source, model, connection_id),
            )


def write_concept(root, rel: str, title: str, body: str, description: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    desc = f"description: {description}\n" if description else ""
    path.write_text(f"---\ntitle: {title}\n{desc}---\n{body}\n", encoding="utf-8")


class FakeEmbeddingProvider:
    """Deterministic offline provider: vector = [len(text), 1.0, 0.0, ...]."""

    def __init__(self, dims: int = 3) -> None:
        self.dims = dims
        self.calls: list[tuple[list[str], str]] = []

    async def embed(self, texts, config, *, kind=KIND_DOCUMENT):
        self.calls.append((list(texts), kind))
        return [[float(len(text)), 1.0] + [0.0] * (self.dims - 2) for text in texts]


def make_service(db_path, user_id="user-1", provider=None, model="test-model", status=None):
    config = EmbeddingConfig(source="api", model=model, provider="openai", api_key="k")
    provider = provider if provider is not None else FakeEmbeddingProvider()
    return EmbeddingService(db_path, user_id, config, provider, status=status), provider


# --- store CRUD --------------------------------------------------------------


def test_upsert_load_roundtrip_vector_exact(tmp_path):
    service, _ = make_service(make_db(tmp_path))
    vector = [0.125, -2.5, 3.75]  # exactly representable in float32
    service.upsert("a/b.md", "test-model", vector, "hash-1")
    loaded = service.load()
    assert set(loaded) == {"a/b.md"}
    row = loaded["a/b.md"]
    assert row["model"] == "test-model"
    assert row["dims"] == 3
    assert row["vector"] == vector
    assert row["content_hash"] == "hash-1"


def test_upsert_overwrites_and_delete_removes(tmp_path):
    service, _ = make_service(make_db(tmp_path))
    service.upsert("a.md", "test-model", [1.0, 0.0], "hash-1")
    service.upsert("a.md", "test-model", [0.0, 1.0], "hash-2")
    row = service.load()["a.md"]
    assert row["vector"] == [0.0, 1.0]
    assert row["content_hash"] == "hash-2"
    service.delete("a.md")
    assert service.load() == {}


def test_crud_keys_are_canonical_no_leading_slash(tmp_path):
    """Both key shapes in, one canonical shape stored (single normalization)."""
    service, _ = make_service(make_db(tmp_path))
    service.upsert("/a/b.md", "test-model", [1.0, 0.0], "hash-1")
    assert set(service.load()) == {"a/b.md"}
    # upsert with the other shape overwrites the SAME row, not a second one
    service.upsert("a/b.md", "test-model", [0.0, 1.0], "hash-2")
    loaded = service.load()
    assert set(loaded) == {"a/b.md"}
    assert loaded["a/b.md"]["content_hash"] == "hash-2"
    service.delete("/a/b.md")
    assert service.load() == {}


def test_user_scoping_isolation(tmp_path):
    db_path = make_db(tmp_path)
    service1, _ = make_service(db_path, user_id="user-1")
    service2, _ = make_service(db_path, user_id="user-2")
    service1.upsert("a.md", "test-model", [1.0, 0.0], "hash-1")
    assert service2.load() == {}
    service2.upsert("a.md", "test-model", [0.0, 1.0], "hash-2")
    assert service1.load()["a.md"]["content_hash"] == "hash-1"
    assert service2.load()["a.md"]["content_hash"] == "hash-2"


def test_stats(tmp_path):
    service, _ = make_service(make_db(tmp_path))
    assert service.stats() == {"rows": 0, "models": [], "dims": []}
    service.upsert("a.md", "model-a", [1.0, 0.0], "h1")
    service.upsert("b.md", "model-a", [1.0, 0.0], "h2")
    service.upsert("c.md", "model-b", [1.0, 0.0, 0.0], "h3")
    stats = service.stats()
    assert stats["rows"] == 3
    assert sorted(stats["models"]) == ["model-a", "model-b"]
    assert sorted(stats["dims"]) == [2, 3]


# --- text assembly + hashing ---------------------------------------------------


def test_concept_text_assembly_order():
    assert concept_text({"title": "T", "description": "D"}, "B") == "T\nD\n\nB"


def test_concept_text_empty_fields_tolerated():
    assert concept_text({}, "body") == "\n\n\nbody"
    assert concept_text({"title": "T"}, "") == "T\n\n\n"


def test_content_hash_stable():
    assert content_hash("abc") == hashlib.sha256(b"abc").hexdigest()
    assert content_hash("abc") != content_hash("abd")


# --- math ----------------------------------------------------------------------


def test_cosine_known_vectors():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([1.0, 1.0], [1.0, 0.0]) == pytest.approx(math.sqrt(0.5))
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_top_k_ordering_and_truncation(tmp_path):
    service, _ = make_service(make_db(tmp_path))
    service.upsert("a.md", "test-model", [1.0, 0.0], "h")
    service.upsert("b.md", "test-model", [1.0, 1.0], "h")
    service.upsert("c.md", "test-model", [0.0, 1.0], "h")
    ranked = service.top_k([1.0, 0.0], 5)
    assert [path for path, _ in ranked] == ["a.md", "b.md", "c.md"]
    assert ranked[0][1] > ranked[1][1] > ranked[2][1]
    assert [path for path, _ in service.top_k([1.0, 0.0], 1)] == ["a.md"]


def test_top_k_skips_dims_mismatch(tmp_path):
    service, _ = make_service(make_db(tmp_path))
    service.upsert("a.md", "test-model", [1.0, 0.0], "h")  # 2-dim row
    assert service.top_k([1.0, 0.0, 0.0], 5) == []


# --- providers: OpenAI ---------------------------------------------------------


def openai_embed_config(**overrides) -> EmbeddingConfig:
    base = {
        "source": "api",
        "model": "text-embedding-3-small",
        "provider": "openai",
        "api_key": "sk-test",
    }
    return EmbeddingConfig(**(base | overrides))


@respx.mock
async def test_openai_embeddings_request_shape_and_parsing():
    route = respx.post("https://api.openai.com/v1/embeddings").mock(
        return_value=httpx.Response(
            200, json={"data": [{"embedding": [0.1, 0.2]}, {"embedding": [0.3, 0.4]}]}
        )
    )
    config = openai_embed_config()
    vectors = await create_embedding_provider(config).embed(["one", "two"], config)

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer sk-test"
    assert json.loads(request.content) == {
        "model": "text-embedding-3-small",
        "input": ["one", "two"],
    }


@respx.mock
async def test_openai_embeddings_openrouter_base_url():
    route = respx.post("https://openrouter.ai/api/v1/embeddings").mock(
        return_value=httpx.Response(200, json={"data": [{"embedding": [1.0]}]})
    )
    config = openai_embed_config(provider="openrouter", api_key="or-key")
    vectors = await create_embedding_provider(config).embed(["x"], config)
    assert vectors == [[1.0]]
    assert route.calls.last.request.headers["Authorization"] == "Bearer or-key"


@respx.mock
async def test_openai_embeddings_error_payload_on_200_raises():
    respx.post("https://api.openai.com/v1/embeddings").mock(
        return_value=httpx.Response(
            200, json={"error": {"code": 429, "message": "Rate limit exceeded"}}
        )
    )
    config = openai_embed_config()
    with pytest.raises(EmbeddingProviderError, match="Rate limit exceeded"):
        await create_embedding_provider(config).embed(["x"], config)


@respx.mock
async def test_openai_embeddings_missing_data_raises():
    respx.post("https://api.openai.com/v1/embeddings").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    config = openai_embed_config()
    with pytest.raises(EmbeddingProviderError, match="returned 0 embeddings for 1 texts"):
        await create_embedding_provider(config).embed(["x"], config)


# --- providers: Gemini ---------------------------------------------------------


@respx.mock
async def test_gemini_embeddings_request_shape_and_task_type_per_kind():
    route = respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/embed-x:embedContent"
    ).mock(return_value=httpx.Response(200, json={"embedding": {"values": [1.0, 2.0]}}))
    config = EmbeddingConfig(source="api", model="embed-x", provider="gemini", api_key="gk")
    provider = create_embedding_provider(config)

    vectors = await provider.embed(["q"], config, kind=KIND_QUERY)
    assert vectors == [[1.0, 2.0]]
    request = route.calls.last.request
    assert request.headers["x-goog-api-key"] == "gk"
    body = json.loads(request.content)
    assert body["taskType"] == "RETRIEVAL_QUERY"
    assert body["content"] == {"parts": [{"text": "q"}]}

    await provider.embed(["d1", "d2"], config, kind=KIND_DOCUMENT)
    bodies = [json.loads(call.request.content) for call in route.calls[1:]]
    assert [b["taskType"] for b in bodies] == ["RETRIEVAL_DOCUMENT", "RETRIEVAL_DOCUMENT"]


# --- providers: factory + local -------------------------------------------------


def test_factory_rejects_anthropic():
    config = EmbeddingConfig(source="api", model="m", provider="anthropic", api_key="k")
    with pytest.raises(EmbeddingProviderError, match="anthropic has no embeddings endpoint"):
        create_embedding_provider(config)


def test_factory_rejects_unknown():
    with pytest.raises(ValueError, match="bogus"):
        create_embedding_provider(EmbeddingConfig(source="api", model="m", provider="bogus"))
    with pytest.raises(ValueError, match="bogus-source"):
        create_embedding_provider(EmbeddingConfig(source="bogus-source", model="m"))


class _FakeTextEmbedding:
    instances: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.embed_calls: list[list[str]] = []
        _FakeTextEmbedding.instances.append(self)

    def embed(self, texts):
        texts = list(texts)
        self.embed_calls.append(texts)
        return [[1.0, 0.0] for _ in texts]


def install_fake_fastembed(monkeypatch):
    _FakeTextEmbedding.instances = []
    module = types.ModuleType("fastembed")
    module.TextEmbedding = _FakeTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", module)


async def test_local_provider_construction_prefixes_and_memoization(monkeypatch, tmp_path):
    install_fake_fastembed(monkeypatch)
    provider = LocalFastembedProvider(cache_dir=tmp_path / "models")
    config = EmbeddingConfig(source="local", model="intfloat/multilingual-e5-small")

    vectors = await provider.embed(["hello"], config, kind=KIND_QUERY)
    assert vectors == [[1.0, 0.0]]
    instance = _FakeTextEmbedding.instances[-1]
    assert instance.kwargs == {
        "model_name": "intfloat/multilingual-e5-small",
        "cache_dir": str(tmp_path / "models"),
    }
    assert instance.embed_calls == [["query: hello"]]

    await provider.embed(["doc"], config, kind=KIND_DOCUMENT)
    assert instance.embed_calls[-1] == ["passage: doc"]
    assert len(_FakeTextEmbedding.instances) == 1  # memoized per model name


async def test_local_provider_prefixes_only_for_e5_models(monkeypatch):
    """L9: query:/passage: prefixes apply to the E5 family only — BGE and
    MiniLM embed the raw text (their model cards define no such convention)."""
    install_fake_fastembed(monkeypatch)
    provider = LocalFastembedProvider()

    for model in (
        "BAAI/bge-small-en-v1.5",
        "BAAI/bge-base-en-v1.5",
        "sentence-transformers/all-MiniLM-L6-v2",
    ):
        config = EmbeddingConfig(source="local", model=model)
        await provider.embed(["hello"], config, kind=KIND_QUERY)
        await provider.embed(["doc"], config, kind=KIND_DOCUMENT)
        instance = _FakeTextEmbedding.instances[-1]
        assert instance.embed_calls == [["hello"], ["doc"]]

    e5 = EmbeddingConfig(source="local", model="intfloat/multilingual-e5-small")
    await provider.embed(["hello"], e5, kind=KIND_QUERY)
    await provider.embed(["doc"], e5, kind=KIND_DOCUMENT)
    instance = _FakeTextEmbedding.instances[-1]
    assert instance.embed_calls == [["query: hello"], ["passage: doc"]]


async def test_local_provider_runs_inference_off_loop(monkeypatch):
    install_fake_fastembed(monkeypatch)
    calls = []
    real_to_thread = asyncio.to_thread

    async def spy(func, *args, **kwargs):
        calls.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy)
    provider = LocalFastembedProvider()
    await provider.embed(["x"], EmbeddingConfig(source="local", model="m"))
    # model construction (A6: first use downloads ONNX weights) AND the
    # blocking ONNX inference both went through asyncio.to_thread
    assert len(calls) == 2
    await provider.embed(["y"], EmbeddingConfig(source="local", model="m"))
    assert len(calls) == 4  # memoized construction still goes through to_thread


async def test_local_provider_import_guard(monkeypatch):
    monkeypatch.setitem(sys.modules, "fastembed", None)  # import raises ImportError
    provider = LocalFastembedProvider()
    with pytest.raises(EmbeddingProviderError, match=r"athenaeum\[local\]"):
        await provider.embed(["x"], EmbeddingConfig(source="local", model="m"))


def test_local_model_shortlist_shape():
    assert LOCAL_MODEL_SHORTLIST[0] == ("BAAI/bge-small-en-v1.5", 384)
    assert len(LOCAL_MODEL_SHORTLIST) == 4
    assert all(isinstance(dims, int) and dims > 0 for _, dims in LOCAL_MODEL_SHORTLIST)


# --- config resolution (manager._load_config) ------------------------------------


def test_config_resolution_local(tmp_path):
    db_path = make_connections_db(tmp_path)
    set_embedding_row(db_path, "local", "BAAI/bge-small-en-v1.5")
    embedding = LibrarianManager(db_path, tmp_path / "data")._load_config("user-1").embedding
    assert embedding == EmbeddingConfig(source="local", model="BAAI/bge-small-en-v1.5")


def test_config_resolution_api_explicit_connection(tmp_path):
    db_path = make_connections_db(tmp_path)
    set_embedding_row(db_path, "api", "text-embedding-004", "conn-g")
    manager = LibrarianManager(db_path, tmp_path / "data", key_decryptor=lambda enc: f"dec:{enc}")
    embedding = manager._load_config("user-1").embedding
    assert embedding is not None
    assert embedding.source == "api"
    assert embedding.provider == "gemini"
    assert embedding.model == "text-embedding-004"
    assert embedding.api_key == "dec:enc-g"  # decrypted from the bound connection


def test_config_resolution_api_default_connection(tmp_path):
    db_path = make_connections_db(tmp_path)
    set_embedding_row(db_path, "api", "text-embedding-3-small", None)
    embedding = LibrarianManager(db_path, tmp_path / "data")._load_config("user-1").embedding
    assert embedding is not None
    assert embedding.provider == "openai"  # default connection resolved
    assert embedding.api_key == "enc-a"  # no decryptor: value used as-is


def test_config_resolution_anthropic_connection_means_unconfigured(tmp_path):
    db_path = make_connections_db(tmp_path)
    with closing(db.connect(db_path)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO provider_configs"
                " (id, user_id, label, provider, is_default, created_at)"
                " VALUES ('conn-claude', 'user-1', 'Claude', 'anthropic', 0,"
                " '2026-01-03T00:00:00Z')"
            )
    set_embedding_row(db_path, "api", "m", "conn-claude")
    embedding = LibrarianManager(db_path, tmp_path / "data")._load_config("user-1").embedding
    assert embedding is None


def test_config_resolution_dangling_connection_means_unconfigured(tmp_path):
    db_path = make_connections_db(tmp_path)
    set_embedding_row(db_path, "api", "m", "conn-missing")
    embedding = LibrarianManager(db_path, tmp_path / "data")._load_config("user-1").embedding
    assert embedding is None


def test_config_resolution_source_without_model_means_unconfigured(tmp_path):
    db_path = make_connections_db(tmp_path)
    set_embedding_row(db_path, "api", None, "conn-a")
    manager = LibrarianManager(db_path, tmp_path / "data")
    assert manager._load_config("user-1").embedding is None
    set_embedding_row(db_path, "local", None)
    assert manager._load_config("user-1").embedding is None


def test_config_resolution_unset_means_unconfigured(tmp_path):
    db_path = make_connections_db(tmp_path)
    embedding = LibrarianManager(db_path, tmp_path / "data")._load_config("user-1").embedding
    assert embedding is None


# --- bindings + db helpers -------------------------------------------------------


def test_embedding_binding_counts_and_protects_connection_delete(tmp_path):
    db_path = make_connections_db(tmp_path)
    with closing(db.connect(db_path)) as conn:
        assert db.count_provider_config_bindings(conn, "conn-g") == 0
    set_embedding_row(db_path, "api", "m", "conn-g")
    with closing(db.connect(db_path)) as conn:
        assert db.count_provider_config_bindings(conn, "conn-g") == 1
        with pytest.raises(db.ProviderConfigInUseError):
            db.delete_provider_config(conn, "user-1", "conn-g")


def test_update_embedding_config_roundtrip_and_clear(tmp_path):
    db_path = make_connections_db(tmp_path)
    with closing(db.connect(db_path)) as conn:
        db.update_embedding_config(
            conn,
            "user-1",
            source="api",
            model="m",
            connection_id="conn-a",
            semantic_threshold=0.82,
        )
        row = db.get_config(conn, "user-1")
        assert row["embedding_source"] == "api"
        assert row["embedding_model"] == "m"
        assert row["embedding_connection_id"] == "conn-a"
        assert row["semantic_threshold"] == 0.82
        db.update_embedding_config(
            conn, "user-1", source="local", model="local-m", connection_id=None
        )
        row = db.get_config(conn, "user-1")
        assert row["embedding_source"] == "local"
        assert row["embedding_model"] == "local-m"
        assert row["embedding_connection_id"] is None
        db.update_embedding_config(conn, "user-1", source=None, model="m", connection_id="x")
        row = db.get_config(conn, "user-1")
        assert row["embedding_source"] is None
        assert row["embedding_model"] is None
        assert row["embedding_connection_id"] is None
        # param omitted -> None written
        assert row["semantic_threshold"] is None


def test_update_embedding_config_hybrid_toggles_write_and_keep(tmp_path):
    """COALESCE semantics: explicit bools write, None keeps the stored value."""
    db_path = make_connections_db(tmp_path)
    with closing(db.connect(db_path)) as conn:
        row = db.get_config(conn, "user-1")
        assert row["hybrid_search"] == 1 and row["hybrid_rerank"] == 1  # defaults
        db.update_embedding_config(
            conn,
            "user-1",
            source="api",
            model="m",
            connection_id="conn-a",
            hybrid_search=False,
            hybrid_rerank=False,
        )
        row = db.get_config(conn, "user-1")
        assert row["hybrid_search"] == 0 and row["hybrid_rerank"] == 0
        # omitted -> kept, even across an Off save
        db.update_embedding_config(conn, "user-1", source=None, model=None, connection_id=None)
        row = db.get_config(conn, "user-1")
        assert row["hybrid_search"] == 0 and row["hybrid_rerank"] == 0
        db.update_embedding_config(
            conn, "user-1", source="local", model="m", connection_id=None, hybrid_rerank=True
        )
        row = db.get_config(conn, "user-1")
        assert row["hybrid_search"] == 0 and row["hybrid_rerank"] == 1


# --- search_ids / related ---------------------------------------------------------


async def test_search_ids_strips_md_and_ranks(tmp_path):
    service, _ = make_service(make_db(tmp_path))
    service.upsert("x/a.md", "test-model", [1.0, 0.0, 0.0], "h")
    service.upsert("y/b.md", "test-model", [0.0, 1.0, 0.0], "h")
    ranked = await service.search_ids("query", 5)
    # "query" embeds to [5.0, 1.0, 0.0]: closer to [1, 0, 0] than to [0, 1, 0].
    assert [concept_id for concept_id, _ in ranked] == ["x/a", "y/b"]
    assert ranked[0][1] > ranked[1][1]
    assert len(await service.search_ids("query", 1)) == 1


async def test_related_matches_search_ids(tmp_path):
    service, _ = make_service(make_db(tmp_path))
    service.upsert("a.md", "test-model", [1.0, 0.0, 0.0], "h")
    assert await service.related("some text", 3) == await service.search_ids("some text", 3)


# --- sync_writes -------------------------------------------------------------------


async def test_sync_writes_created_updated_deleted(tmp_path):
    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    write_concept(root, "a.md", "Alpha", "body a")
    write_concept(root, "sub/b.md", "Beta", "body b", description="desc b")
    service, provider = make_service(db_path)
    service.upsert("gone.md", "test-model", [1.0, 0.0, 0.0], "old")

    await service.sync_writes(
        make_backend(root),
        [
            {"id": "a", "title": "Alpha", "action": "created"},
            {"id": "sub/b", "title": "Beta", "action": "updated"},
            {"id": "gone", "title": "Gone", "action": "deleted"},
        ],
    )

    loaded = service.load()
    assert set(loaded) == {"a.md", "sub/b.md"}
    assert len(provider.calls) == 1  # one batched embed call for both survivors
    texts, kind = provider.calls[0]
    assert kind == KIND_DOCUMENT
    assert texts == [
        concept_text({"title": "Alpha"}, "body a\n"),
        concept_text({"title": "Beta", "description": "desc b"}, "body b\n"),
    ]
    assert loaded["a.md"]["model"] == "test-model"
    assert loaded["a.md"]["content_hash"] == content_hash(texts[0])
    assert loaded["sub/b.md"]["content_hash"] == content_hash(texts[1])


async def test_sync_writes_read_failure_skipped_without_raising(tmp_path):
    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    root.mkdir()
    write_concept(root, "ok.md", "Ok", "body ok")
    service, provider = make_service(db_path)

    await service.sync_writes(
        make_backend(root),
        [
            {"id": "missing", "title": "M", "action": "updated"},  # file absent
            {"id": "ok", "title": "Ok", "action": "updated"},
        ],
    )
    assert set(service.load()) == {"ok.md"}
    assert len(provider.calls) == 1
    assert len(provider.calls[0][0]) == 1  # only the surviving write embedded


async def test_sync_writes_embed_failure_never_raises(tmp_path):
    class DownProvider:
        async def embed(self, texts, config, *, kind=KIND_DOCUMENT):
            raise RuntimeError("embed down")

    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    write_concept(root, "a.md", "Alpha", "body a")
    service, _ = make_service(db_path, provider=DownProvider())
    await service.sync_writes(make_backend(root), [{"id": "a", "title": "A", "action": "created"}])
    assert service.load() == {}


async def test_sync_writes_canonical_key_shape(tmp_path):
    """Backend result ids are "/x"-shaped; stored keys are "x.md" (canonical),
    so the reconcile writer and the sync writer address the same rows."""
    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    write_concept(root, "a.md", "Alpha", "body a")
    write_concept(root, "sub/b.md", "Beta", "body b")
    service, provider = make_service(db_path)
    service.upsert("gone.md", "test-model", [1.0, 0.0, 0.0], "old")

    await service.sync_writes(
        make_backend(root),
        [
            {"id": "/a", "title": "Alpha", "action": "created"},
            {"id": "/sub/b", "title": "Beta", "action": "updated"},
            {"id": "/gone", "title": "Gone", "action": "deleted"},
        ],
    )

    assert set(service.load()) == {"a.md", "sub/b.md"}  # no "/a.md" rows

    # cross-writer coherence: reconcile sees the sync-written rows as current
    # (same canonical keys + matching hashes) and embeds nothing.
    await service.reconcile(make_backend(root))
    assert sum(len(texts) for texts, _ in provider.calls) == 2  # sync batch only


async def test_sync_writes_move_deletes_old_path_row(tmp_path):
    """L8: a moved write carries from_id; the OLD path's row must not leak."""
    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    write_concept(root, "new/b.md", "Beta", "body b")
    service, provider = make_service(db_path)
    service.upsert("old/a.md", "test-model", [1.0, 0.0, 0.0], "old")

    await service.sync_writes(
        make_backend(root),
        [{"id": "new/b", "title": "Beta", "action": "moved", "from_id": "old/a"}],
    )

    loaded = service.load()
    assert set(loaded) == {"new/b.md"}  # no stale old-path row
    assert len(provider.calls) == 1  # the new path embedded in one batch


async def test_sync_writes_deprecated_deletes_row(tmp_path):
    """A deprecated write removes the vector row, exactly like deleted
    (deprecated concepts are hidden pending cleanup)."""
    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    write_concept(root, "a.md", "Alpha", "body a")
    service, provider = make_service(db_path)
    service.upsert("a.md", "test-model", [1.0, 0.0, 0.0], "old")

    await service.sync_writes(
        make_backend(root),
        [{"id": "a", "title": "Alpha", "action": "deprecated"}],
    )

    assert service.load() == {}
    assert provider.calls == []  # nothing re-embedded


async def test_sync_writes_create_then_delete_leaves_no_row(tmp_path):
    """L8: a concept created and deleted within one run keeps no row."""
    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    root.mkdir()  # no a.md on disk: created, then deleted during the run
    service, provider = make_service(db_path)

    await service.sync_writes(
        make_backend(root),
        [
            {"id": "a", "title": "A", "action": "created"},
            {"id": "a", "title": "A", "action": "deleted"},
        ],
    )

    assert service.load() == {}  # no resurrected row for the deleted concept
    assert provider.calls == []  # nothing left to embed


async def test_sync_writes_delete_then_create_keeps_row(tmp_path):
    """Collapse order matters: delete followed by a re-create keeps the row."""
    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    write_concept(root, "a.md", "Alpha", "body a")
    service, provider = make_service(db_path)

    await service.sync_writes(
        make_backend(root),
        [
            {"id": "a", "title": "A", "action": "deleted"},
            {"id": "a", "title": "A", "action": "created"},
        ],
    )

    assert set(service.load()) == {"a.md"}
    assert len(provider.calls) == 1


# --- reconcile ---------------------------------------------------------------------


async def test_reconcile_embeds_missing_and_reports_status(tmp_path):
    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    write_concept(root, "a.md", "Alpha", "body a")
    write_concept(root, "sub/b.md", "Beta", "body b")
    status = EmbedStatusRegistry()
    service, provider = make_service(db_path, status=status)

    await service.reconcile(make_backend(root))

    loaded = service.load()
    assert set(loaded) == {"a.md", "sub/b.md"}
    assert all(row["model"] == "test-model" for row in loaded.values())
    state = status.get("user-1")
    assert state["state"] == "idle"
    assert state["done"] == state["total"] == 2
    assert state["error"] is None
    assert state["model"] == "test-model"

    await service.reconcile(make_backend(root))  # converged: nothing left to embed
    assert sum(len(texts) for texts, _ in provider.calls) == 2


async def test_reconcile_reembeds_on_hash_drift(tmp_path):
    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    write_concept(root, "a.md", "Alpha", "body a")
    service, provider = make_service(db_path)
    await service.reconcile(make_backend(root))
    first_hash = service.load()["a.md"]["content_hash"]

    write_concept(root, "a.md", "Alpha", "body a changed")
    await service.reconcile(make_backend(root))
    row = service.load()["a.md"]
    assert row["content_hash"] != first_hash
    assert len(provider.calls) == 2  # one embed batch per reconcile


async def test_reconcile_model_mismatch_forces_full_reembed(tmp_path):
    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    write_concept(root, "a.md", "Alpha", "body a")
    service, provider = make_service(db_path)
    text = concept_text({"title": "Alpha"}, "body a\n")
    service.upsert("a.md", "old-model", [1.0, 0.0, 0.0], content_hash(text))  # hash matches

    await service.reconcile(make_backend(root))
    row = service.load()["a.md"]
    assert row["model"] == "test-model"  # re-embedded under the current model
    assert len(provider.calls) == 1


async def test_reconcile_deletes_vanished_paths(tmp_path):
    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    write_concept(root, "a.md", "Alpha", "body a")
    service, _ = make_service(db_path)
    service.upsert("ghost.md", "test-model", [1.0, 0.0, 0.0], "stale")

    await service.reconcile(make_backend(root))
    assert set(service.load()) == {"a.md"}


async def test_reconcile_skips_deprecated_on_disk(tmp_path):
    """Deprecated on-disk concepts are not embedded, and a stale stored row
    drops out (deprecated concepts are hidden pending cleanup)."""
    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    write_concept(root, "a.md", "Alpha", "body a")
    (root / "old.md").write_text(
        "---\ntitle: Old\nstatus: deprecated\n---\nbody old\n", encoding="utf-8"
    )
    service, provider = make_service(db_path)
    service.upsert("old.md", "test-model", [1.0, 0.0, 0.0], "stale")

    await service.reconcile(make_backend(root))

    assert set(service.load()) == {"a.md"}
    assert sum(len(texts) for texts, _ in provider.calls) == 1  # only a.md embedded


async def test_reconcile_failure_marks_failed_and_reraises(tmp_path):
    class DownProvider:
        async def embed(self, texts, config, *, kind=KIND_DOCUMENT):
            raise RuntimeError("embed down")

    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    write_concept(root, "a.md", "Alpha", "body a")
    status = EmbedStatusRegistry()
    service, _ = make_service(db_path, provider=DownProvider(), status=status)

    with pytest.raises(RuntimeError, match="embed down"):
        await service.reconcile(make_backend(root))
    state = status.get("user-1")
    assert state["state"] == "failed"
    assert "embed down" in state["error"]


async def test_reconcile_without_registry_never_raises(tmp_path):
    class DownProvider:
        async def embed(self, texts, config, *, kind=KIND_DOCUMENT):
            raise RuntimeError("embed down")

    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    write_concept(root, "a.md", "Alpha", "body a")
    service, _ = make_service(db_path, provider=DownProvider())  # status=None
    await service.reconcile(make_backend(root))  # logs instead of raising


async def test_reconcile_concurrent_guard_returns_immediately(tmp_path):
    """A live DB claim owned by another instance blocks the reconcile; the
    status registry only reports, it is no longer the guard (S7)."""
    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    write_concept(root, "a.md", "Alpha", "body a")
    status = EmbedStatusRegistry()
    service, provider = make_service(db_path, status=status)
    with closing(db.connect(db_path)) as conn:
        assert db.try_claim_embed_reconcile(conn, "user-1", "other-host:1:abc", 3600)
    status.begin("user-1", 5, "test-model")  # a run is already in flight

    await service.reconcile(make_backend(root))
    assert provider.calls == []
    assert status.get("user-1")["state"] == "running"  # untouched


def test_embed_reconcile_claim_helpers(tmp_path):
    db_path = make_db(tmp_path)
    with closing(db.connect(db_path)) as conn:
        assert db.try_claim_embed_reconcile(conn, "user-1", "owner-a", 3600)
        # a live claim refuses a second owner
        assert not db.try_claim_embed_reconcile(conn, "user-1", "owner-b", 3600)
        # owner-mismatched release is a no-op
        db.release_embed_reclaim(conn, "user-1", "owner-b")
        assert not db.try_claim_embed_reconcile(conn, "user-1", "owner-b", 3600)
        # the owner's release frees the slot
        db.release_embed_reclaim(conn, "user-1", "owner-a")
        assert db.try_claim_embed_reconcile(conn, "user-1", "owner-b", 3600)


async def test_reconcile_claim_blocks_second_service(tmp_path):
    """Two EmbeddingService instances for one user never reconcile at once:
    while service1's provider is parked, service2's reconcile returns
    immediately without embedding (S7)."""
    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    write_concept(root, "a.md", "Alpha", "body a")
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingProvider:
        def __init__(self):
            self.calls: list = []

        async def embed(self, texts, config, *, kind=KIND_DOCUMENT):
            self.calls.append(list(texts))
            started.set()
            await release.wait()
            return [[1.0, 0.0] for _ in texts]

    service1, _ = make_service(db_path, provider=BlockingProvider())
    service2, provider2 = make_service(db_path)  # FakeEmbeddingProvider

    run = asyncio.create_task(service1.reconcile(make_backend(root)))
    await asyncio.wait_for(started.wait(), timeout=5)
    await service2.reconcile(make_backend(root))  # claim held by service1: immediate skip
    assert provider2.calls == []
    release.set()
    await run
    assert set(service2.load()) == {"a.md"}  # service1's pass landed
    # claim released in finally: no row left behind
    with closing(db.connect(db_path)) as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM embed_reconcile_claims").fetchone()
    assert row["n"] == 0


async def test_reconcile_expired_claim_is_reclaimable(tmp_path):
    """A claim older than the TTL belongs to a presumed-crashed owner: the
    next reconcile reclaims the slot and runs."""
    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    write_concept(root, "a.md", "Alpha", "body a")
    service, provider = make_service(db_path)
    with closing(db.connect(db_path)) as conn:
        assert db.try_claim_embed_reconcile(conn, "user-1", "dead-owner", 3600)
        with conn:
            conn.execute(
                "UPDATE embed_reconcile_claims SET claimed_at = ? WHERE user_id = ?",
                ("2020-01-01T00:00:00+00:00", "user-1"),
            )
    await service.reconcile(make_backend(root))
    assert len(provider.calls) == 1
    assert set(service.load()) == {"a.md"}


async def test_reconcile_failure_releases_claim(tmp_path):
    """A failed reconcile releases the claim (finally): a retry is not blocked."""

    class DownProvider:
        async def embed(self, texts, config, *, kind=KIND_DOCUMENT):
            raise RuntimeError("embed down")

    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    write_concept(root, "a.md", "Alpha", "body a")
    status = EmbedStatusRegistry()
    service, _ = make_service(db_path, provider=DownProvider(), status=status)
    with pytest.raises(RuntimeError, match="embed down"):
        await service.reconcile(make_backend(root))
    with closing(db.connect(db_path)) as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM embed_reconcile_claims").fetchone()
    assert row["n"] == 0


async def test_reconcile_cancel_releases_claim(tmp_path):
    """A cancelled reconcile releases the claim in its finally (A5): the next
    reconcile is not blocked for the claim TTL."""
    started = asyncio.Event()
    release = asyncio.Event()

    class ParkingProvider:
        async def embed(self, texts, config, *, kind=KIND_DOCUMENT):
            started.set()
            await release.wait()
            return [[1.0, 0.0] for _ in texts]

    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    write_concept(root, "a.md", "Alpha", "body a")
    service, _ = make_service(db_path, provider=ParkingProvider())
    run = asyncio.create_task(service.reconcile(make_backend(root)))
    await asyncio.wait_for(started.wait(), timeout=5)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    with closing(db.connect(db_path)) as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM embed_reconcile_claims").fetchone()
    assert row["n"] == 0
    # retry is not blocked by the cancelled run's claim
    service2, provider2 = make_service(db_path)
    await service2.reconcile(make_backend(root))
    assert len(provider2.calls) == 1


# --- Phase 2: librarian lifecycle seams ---------------------------------------------


class FakeEmbedService:
    def __init__(self) -> None:
        self.reconcile_calls: list = []
        self.sync_calls: list = []

    async def reconcile(self, backend):
        self.reconcile_calls.append(backend)

    async def sync_writes(self, backend, writes):
        self.sync_calls.append((backend, writes))


async def test_maybe_embed_reconcile_schedules_on_first_async_entry(tmp_path):
    service = FakeEmbedService()
    librarian = Librarian(
        tmp_path / "lib", LibrarianConfig(user_id="user-1"), embedding_service=service
    )
    assert librarian._embed_reconcile_pending  # set by _build_backend
    try:
        await librarian.handle_request("q")
    except Exception:
        pass  # unconfigured LLM raises after the reconcile was scheduled
    for _ in range(10):
        if service.reconcile_calls:
            break
        await asyncio.sleep(0)
    assert service.reconcile_calls == [librarian.backend]
    assert not librarian._embed_reconcile_pending  # fires once


async def test_embed_reconcile_task_retained_and_shutdown_cancels(tmp_path):
    """A5: the reconcile task is strongly referenced; shutdown() cancels it."""
    started = asyncio.Event()
    release = asyncio.Event()

    class ParkingEmbedService(FakeEmbedService):
        async def reconcile(self, backend):
            self.reconcile_calls.append(backend)
            started.set()
            await release.wait()

    service = ParkingEmbedService()
    librarian = Librarian(
        tmp_path / "lib", LibrarianConfig(user_id="user-1"), embedding_service=service
    )
    try:
        await librarian.handle_request("q")
    except Exception:
        pass  # unconfigured LLM raises after the reconcile was scheduled
    await asyncio.wait_for(started.wait(), timeout=5)
    task = librarian._embed_reconcile_task
    assert task is not None and not task.done()  # strong reference retained
    librarian.shutdown()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
    await asyncio.sleep(0)  # let the done callback run
    assert librarian._embed_reconcile_task is None
    release.set()


async def test_manager_evict_cancels_pending_reconcile(tmp_path):
    """A5: evicting a cached librarian cancels its in-flight reconcile."""
    db_path = make_connections_db(tmp_path)
    set_embedding_row(db_path, "local", "BAAI/bge-small-en-v1.5")
    started = asyncio.Event()
    release = asyncio.Event()

    class ParkingProvider(FakeEmbeddingProvider):
        async def embed(self, texts, config, *, kind=KIND_DOCUMENT):
            started.set()
            await release.wait()
            return await super().embed(texts, config, kind=kind)

    class DownLLM:
        async def complete(self, messages, tools, config):
            raise RuntimeError("llm down")

    # a concept on disk so the reconcile has something to embed
    write_concept(tmp_path / "data" / "users" / "user-1" / "library", "a.md", "Alpha", "body a")
    manager = LibrarianManager(
        db_path,
        tmp_path / "data",
        # no backend_factory: the real backend runs _build_backend, which
        # arms the deferred reconcile flag
        provider_factory=lambda user_id, llm: DownLLM(),
        embedding_provider_factory=lambda cfg: ParkingProvider(),
    )
    librarian = manager.get("user-1")
    try:
        await librarian.handle_request("q")
    except Exception:
        pass  # unconfigured LLM raises after the reconcile was scheduled
    await asyncio.wait_for(started.wait(), timeout=5)
    task = librarian._embed_reconcile_task
    assert task is not None and not task.done()
    manager.evict("user-1")
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
    release.set()


async def test_sync_embeddings_delegates_and_noops(tmp_path):
    writes = [{"id": "a", "title": "A", "action": "created"}]
    bare = Librarian(tmp_path / "lib", LibrarianConfig(user_id="user-1"), backend=object())
    await bare.sync_embeddings(writes)  # no service: no-op

    service = FakeEmbedService()
    librarian = Librarian(
        tmp_path / "lib",
        LibrarianConfig(user_id="user-1"),
        backend=object(),
        embedding_service=service,
    )
    await librarian.sync_embeddings([])  # empty: no-op
    assert service.sync_calls == []
    await librarian.sync_embeddings(writes)
    assert service.sync_calls == [(librarian.backend, writes)]


async def test_agent_move_deletes_old_embedding_row_end_to_end(tmp_path):
    """L8 activation: a move_concept through the agent loop surfaces
    action=moved + from_id in the result, and the embedding sync deletes the
    OLD path's row (previously the stale row survived until the next reconcile)."""
    from athenaeum.librarian.llm import LLMConfig, LLMResponse, ToolCall

    class MoveScript:
        async def complete(self, messages, tools, config):
            if not any(m.get("role") == "tool" for m in messages):
                return LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="move_concept",
                            arguments={"old_path": "/old/a.md", "new_path": "/new/b.md"},
                        )
                    ]
                )
            return LLMResponse(text="Moved it.")

    db_path = make_db(tmp_path)
    root = tmp_path / "lib"
    write_concept(root, "old/a.md", "Alpha", "body a")
    service, _ = make_service(db_path)
    service.upsert("old/a.md", "test-model", [1.0, 0.0, 0.0], "old")
    librarian = Librarian(
        root,
        LibrarianConfig(user_id="user-1", llm=LLMConfig(provider="openai", model="m", api_key="k")),
        backend=make_backend(root),
        provider=MoveScript(),
        embedding_service=service,
    )

    result = await librarian.handle_update("relocate alpha")
    assert result["stored"] == [
        {"id": "/new/b", "title": "Alpha", "action": "moved", "from_id": "/old/a"}
    ]

    await librarian.sync_embeddings(result["stored"])
    assert set(service.load()) == {"new/b.md"}  # no stale old-path row


def test_manager_build_injects_embedding_service(tmp_path):
    db_path = make_connections_db(tmp_path)
    set_embedding_row(db_path, "local", "BAAI/bge-small-en-v1.5")
    manager = LibrarianManager(
        db_path,
        tmp_path / "data",
        backend_factory=lambda user_id, root, config: object(),
        embedding_provider_factory=lambda cfg: FakeEmbeddingProvider(),
    )
    librarian = manager.get("user-1")
    assert librarian._embed is not None
    assert librarian._embed.config.model == "BAAI/bge-small-en-v1.5"
    assert manager.embed_status_for("user-1") is None  # no reconcile ran yet


def test_manager_build_survives_broken_embedding_factory(tmp_path):
    db_path = make_connections_db(tmp_path)
    set_embedding_row(db_path, "api", "m", "conn-a")

    def broken_factory(cfg):
        raise EmbeddingProviderError("no endpoint")

    manager = LibrarianManager(
        db_path,
        tmp_path / "data",
        backend_factory=lambda user_id, root, config: object(),
        embedding_provider_factory=broken_factory,
    )
    librarian = manager.get("user-1")  # broken embeddings never break the librarian
    assert librarian._embed is None
