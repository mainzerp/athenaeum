"""Tests for F1: the search_semantic internal tool (VECTOR_SEARCH plan §8.3).

The backend method is driven against a real LibraryBackend on tmp_path with
a fake embedding service; loop integration goes through a ScriptedProvider
and a minimal fake backend exposing the async semantic seam; the tracing
shape covers capping and the fallback flag.
"""

import pytest

from athenaeum.librarian.agent import Librarian, LibrarianConfig
from athenaeum.librarian.llm import LLMConfig, LLMResponse, ToolCall
from athenaeum.librarian.tools import TOOL_SCHEMAS, dispatch
from athenaeum.librarian.tracing import MAX_ITEMS, _shape_result
from athenaeum.library.backend import LibraryBackend


class FakeSearchService:
    """Fake EmbeddingService.search_ids seam (ranked list or error)."""

    def __init__(self, ranked=None, error=None):
        self.ranked = ranked or []
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def search_ids(self, query, limit):
        self.calls.append((query, limit))
        if self.error is not None:
            raise self.error
        return list(self.ranked)


def make_backend(tmp_path, service=None) -> LibraryBackend:
    root = tmp_path / "lib"
    root.mkdir()
    return LibraryBackend(root, actor="test/0", versioning=False, embedding_service=service)


def write_concept(root, name, frontmatter_yaml, body):
    (root / name).write_text(f"---\n{frontmatter_yaml}---\n{body}\n", encoding="utf-8")


# --- backend method ---------------------------------------------------------


async def test_search_semantic_ranks_and_shapes_hits(tmp_path):
    service = FakeSearchService(ranked=[("/b", 0.913), ("/a", 0.487)])
    backend = make_backend(tmp_path, service)
    write_concept(
        backend.root, "a.md", "title: Alpha\ntype: Note\ndescription: about a\n", "body a"
    )
    write_concept(backend.root, "b.md", "title: Beta\ntype: Project\n", "body b")

    hits = await backend.search_semantic("intent query", 5)

    assert service.calls == [("intent query", 5)]
    assert [hit["id"] for hit in hits] == ["/b", "/a"]  # service ranking preserved
    assert hits[0] == {
        "id": "/b",
        "path": "/b.md",
        "title": "Beta",
        "type": "Project",
        "description": None,
        "score": 0.91,  # rounded to two decimals
    }
    assert hits[1]["score"] == 0.49


async def test_search_semantic_skips_unreadable_concepts(tmp_path):
    service = FakeSearchService(ranked=[("/gone", 0.9), ("/a", 0.8)])
    backend = make_backend(tmp_path, service)
    write_concept(backend.root, "a.md", "title: Alpha\ntype: Note\n", "body a")

    hits = await backend.search_semantic("q")

    assert [hit["id"] for hit in hits] == ["/a"]  # the missing file is skipped, not fatal


async def test_search_semantic_empty_ranking_returns_empty(tmp_path):
    backend = make_backend(tmp_path, FakeSearchService(ranked=[]))
    assert await backend.search_semantic("q") == []


async def test_search_semantic_unconfigured_is_recoverable_error(tmp_path):
    backend = make_backend(tmp_path, service=None)
    with pytest.raises(RuntimeError, match="search_metadata"):
        await backend.search_semantic("q")


async def test_search_semantic_failure_falls_back_to_metadata(tmp_path):
    service = FakeSearchService(error=RuntimeError("embed down"))
    backend = make_backend(tmp_path, service)
    write_concept(backend.root, "a.md", "title: Alpha\ntype: Note\n", "body a")
    write_concept(backend.root, "b.md", "title: Beta\ntype: Project\n", "body b")

    hits = await backend.search_semantic("alpha")

    # the fallback FILTERS on title/description: only matching concepts, flagged
    assert [hit["id"] for hit in hits] == ["/a"]
    assert all(hit["fallback"] is True for hit in hits)


async def test_search_semantic_fallback_matches_description(tmp_path):
    service = FakeSearchService(error=RuntimeError("embed down"))
    backend = make_backend(tmp_path, service)
    write_concept(backend.root, "a.md", "title: Alpha\ntype: Note\ndescription: about bees\n", "x")
    write_concept(backend.root, "b.md", "title: Beta\ntype: Project\n", "body b")

    hits = await backend.search_semantic("bees")

    assert [hit["id"] for hit in hits] == ["/a"]
    assert hits[0]["fallback"] is True


async def test_search_semantic_fallback_never_dumps_library(tmp_path):
    """L7: no title/description match -> empty result, not the whole library."""
    service = FakeSearchService(error=RuntimeError("embed down"))
    backend = make_backend(tmp_path, service)
    write_concept(backend.root, "a.md", "title: Alpha\ntype: Note\n", "body a")
    write_concept(backend.root, "b.md", "title: Beta\ntype: Project\n", "body b")

    assert await backend.search_semantic("no-such-term-anywhere") == []


# --- schema + dispatch --------------------------------------------------------


def test_search_semantic_schema_advertised():
    names = [t["name"] for t in TOOL_SCHEMAS]
    assert len(names) == 10
    assert names.count("search_semantic") == 1
    schema = next(t for t in TOOL_SCHEMAS if t["name"] == "search_semantic")
    assert schema["parameters"]["required"] == ["query"]


async def test_dispatch_requires_query_argument():
    with pytest.raises(ValueError, match="missing required argument"):
        await dispatch("search_semantic", {}, backend=None)  # arg check fires first


# --- loop integration ---------------------------------------------------------


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


class SemanticFakeBackend:
    """Minimal Backend-protocol fake exposing only the async semantic seam."""

    def __init__(self, hits):
        self.hits = hits
        self.calls: list[tuple[str, int]] = []

    async def search_semantic(self, query: str, limit: int = 8):
        self.calls.append((query, limit))
        return list(self.hits)


def tc(call_id: str, name: str, arguments: dict | None = None) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments or {})


def make_librarian(backend, provider) -> Librarian:
    config = LibrarianConfig(
        user_id="user-1",
        llm=LLMConfig(provider="openai", model="m", api_key="k", max_iterations=3),
    )
    return Librarian("/unused-root", config, backend=backend, provider=provider)


async def test_search_semantic_dispatches_through_agent_loop():
    backend = SemanticFakeBackend(
        hits=[
            {
                "id": "/a",
                "path": "/a.md",
                "title": "A",
                "type": "Note",
                "description": None,
                "score": 0.9,
            }
        ]
    )
    provider = ScriptedProvider(
        [
            LLMResponse(tool_calls=[tc("c1", "search_semantic", {"query": "intent", "limit": 5})]),
            LLMResponse(text="Found A."),
        ]
    )
    librarian = make_librarian(backend, provider)

    result = await librarian.handle_request("find by meaning")

    assert result["answer"] == "Found A."
    assert backend.calls == [("intent", 5)]
    # the hit list was fed back as a tool message to the second completion
    tool_messages = [m for m in provider.calls[1][0] if m["role"] == "tool"]
    assert tool_messages[0]["tool_call_id"] == "c1"
    assert "/a.md" in tool_messages[0]["content"]
    # read-side tool: writes stay untracked and concepts tracker-driven
    assert result["concepts"] == []


async def test_search_semantic_default_limit_is_eight():
    backend = SemanticFakeBackend(hits=[])
    provider = ScriptedProvider(
        [
            LLMResponse(tool_calls=[tc("c1", "search_semantic", {"query": "q"})]),
            LLMResponse(text="none"),
        ]
    )
    librarian = make_librarian(backend, provider)

    await librarian.handle_request("q")

    assert backend.calls == [("q", 8)]


async def test_search_semantic_error_is_recoverable_tool_error():
    class UnconfiguredBackend:
        async def search_semantic(self, query: str, limit: int = 8):
            raise RuntimeError(
                "semantic search is not configured: set an embedding source in the "
                "WebUI (Agents > Embeddings); use search_metadata instead"
            )

    provider = ScriptedProvider(
        [
            LLMResponse(tool_calls=[tc("c1", "search_semantic", {"query": "q"})]),
            LLMResponse(text="fell back to metadata"),
        ]
    )
    librarian = make_librarian(UnconfiguredBackend(), provider)

    result = await librarian.handle_request("q")

    tool_messages = [m for m in provider.calls[1][0] if m["role"] == "tool"]
    assert "error" in tool_messages[0]["content"]
    assert "search_metadata" in tool_messages[0]["content"]
    assert result["answer"] == "fell back to metadata"


# --- tracing ------------------------------------------------------------------


def test_shape_search_semantic_hits_capped_with_fallback_flag():
    hits = [{"id": f"/c{i}", "path": f"/c{i}.md", "score": 0.9} for i in range(MAX_ITEMS + 10)]

    shaped = _shape_result("search_semantic", {"query": "q"}, hits)

    assert shaped["count"] == MAX_ITEMS + 10
    assert len(shaped["hits"]) == MAX_ITEMS
    assert shaped["hits"][0] == {"path": "/c0.md", "score": 0.9}
    assert shaped["fallback"] is False

    fallback = _shape_result("search_semantic", {}, [{"path": "/a.md", "fallback": True}])
    assert fallback["fallback"] is True
    assert fallback["hits"] == [{"path": "/a.md", "score": None}]
