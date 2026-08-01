"""Tests for F3: store/update related-concepts injection (VECTOR_SEARCH plan §8.4).

The "Possibly related existing concepts" section is injected into the STORE
and UPDATE task preambles from the embedding store. The unconfigured,
failure, and empty paths leave the task text byte-identical to the
pre-feature shape and cost zero extra loop iterations.
"""

from athenaeum.librarian.agent import STORE_RELATED_TOP_K, Librarian, LibrarianConfig
from athenaeum.librarian.llm import LLMConfig, LLMResponse, ToolCall


class FakeRelatedService:
    """Fake EmbeddingService.related() seam (ranked list or error)."""

    def __init__(self, ranked=None, error=None):
        self.ranked = ranked or []
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def related(self, text, k):
        self.calls.append((text, k))
        if self.error is not None:
            raise self.error
        return list(self.ranked)


class FakeBackend:
    """Minimal Backend-protocol fake: read_document + write_concept."""

    def __init__(self, docs=None):
        self.docs = docs or {}

    def read_document(self, path):
        doc = self.docs[path]
        return {"path": path, "frontmatter": doc["frontmatter"], "body": doc["body"]}

    def create_concept(self, path, frontmatter, body, *, agent_label=None):
        self.docs[path] = {"frontmatter": dict(frontmatter), "body": body}
        return {"id": path[: -len(".md")], "action": "created"}


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


def make_librarian(backend, provider, embedding_service=None) -> Librarian:
    config = LibrarianConfig(
        user_id="user-1",
        llm=LLMConfig(provider="openai", model="m", api_key="k", max_iterations=3),
    )
    return Librarian(
        "/unused-root",
        config,
        backend=backend,
        provider=provider,
        embedding_service=embedding_service,
    )


DOC_ALPHA = {"frontmatter": {"title": "Alpha", "type": "Note"}, "body": "about alpha"}

# L1: a write task only succeeds with a landed write, so every scripted run
# writes one concept before the final text response (2 completions per run).
WRITE = LLMResponse(
    tool_calls=[
        ToolCall(
            id="c1",
            name="write_concept",
            arguments={
                "path": "/n.md",
                "frontmatter": {"title": "N", "type": "Note"},
                "body": "b",
            },
        )
    ]
)


async def test_handle_store_injects_related_section():
    backend = FakeBackend(docs={"/a.md": DOC_ALPHA})
    provider = ScriptedProvider([WRITE, LLMResponse(text="stored")])
    service = FakeRelatedService(ranked=[("/a", 0.91)])
    librarian = make_librarian(backend, provider, service)

    await librarian.handle_store("new knowledge", relates_to=["/a"])

    task = provider.calls[0][0][1]["content"]
    assert "Possibly related existing concepts" in task
    assert "- /a (Alpha) — similarity 0.91" in task
    # the caller-provided relates_to line is unchanged
    assert "Related concept IDs suggested by the caller: /a" in task
    assert service.calls == [("new knowledge", STORE_RELATED_TOP_K)]
    assert len(provider.calls) == 2  # write round + final answer; injection adds none


async def test_handle_store_related_section_sits_after_relates_to_line():
    backend = FakeBackend(docs={"/a.md": DOC_ALPHA})
    provider = ScriptedProvider([WRITE, LLMResponse(text="stored")])
    service = FakeRelatedService(ranked=[("/a", 0.91)])
    librarian = make_librarian(backend, provider, service)

    await librarian.handle_store("new knowledge")

    task = provider.calls[0][0][1]["content"]
    relates_idx = task.index("Related concept IDs suggested by the caller:")
    section_idx = task.index("Possibly related existing concepts")
    discipline_idx = task.index("This is NEW knowledge to add.")
    assert relates_idx < section_idx < discipline_idx


async def test_handle_store_title_fallback_and_missing_docs_skipped():
    backend = FakeBackend(docs={"/notitle.md": {"frontmatter": {"type": "Note"}, "body": "b"}})
    provider = ScriptedProvider([WRITE, LLMResponse(text="stored")])
    service = FakeRelatedService(ranked=[("/notitle", 0.8), ("/missing", 0.7)])
    librarian = make_librarian(backend, provider, service)

    await librarian.handle_store("knowledge")

    task = provider.calls[0][0][1]["content"]
    assert "- /notitle (/notitle) — similarity 0.80" in task  # id stands in for the title
    assert "/missing" not in task  # unreadable concept skipped silently


async def test_handle_update_injects_related_section():
    backend = FakeBackend(docs={"/a.md": DOC_ALPHA})
    provider = ScriptedProvider([WRITE, LLMResponse(text="updated")])
    service = FakeRelatedService(ranked=[("/a", 0.87)])
    librarian = make_librarian(backend, provider, service)

    await librarian.handle_update("fix the alpha note")

    task = provider.calls[0][0][1]["content"]
    assert "Possibly related existing concepts" in task
    assert "- /a (Alpha) — similarity 0.87" in task
    assert "Instruction:\nfix the alpha note" in task
    assert service.calls == [("fix the alpha note", STORE_RELATED_TOP_K)]
    assert len(provider.calls) == 2


async def test_unconfigured_failure_and_empty_leave_tasks_byte_identical():
    backend = FakeBackend(docs={"/a.md": DOC_ALPHA})
    variants = [
        None,  # unconfigured
        FakeRelatedService(ranked=[]),  # empty ranking
        FakeRelatedService(error=RuntimeError("embed down")),  # pipeline failure
    ]
    store_tasks = []
    update_tasks = []
    for service in variants:
        store_provider = ScriptedProvider([WRITE, LLMResponse(text="done")])
        update_provider = ScriptedProvider([WRITE, LLMResponse(text="done")])
        librarian = make_librarian(backend, store_provider, service)
        await librarian.handle_store("knowledge", kind_hint="note", relates_to=["/a"])
        store_tasks.append(store_provider.calls[0][0][1]["content"])
        assert len(store_provider.calls) == 2  # same baseline as no injection
        update_librarian = make_librarian(backend, update_provider, service)
        await update_librarian.handle_update("fix something")
        update_tasks.append(update_provider.calls[0][0][1]["content"])
        assert len(update_provider.calls) == 2

    assert store_tasks[0] == store_tasks[1] == store_tasks[2]
    assert update_tasks[0] == update_tasks[1] == update_tasks[2]
    for task in store_tasks + update_tasks:
        assert "Possibly related existing concepts" not in task
    # pre-feature byte shape: the relates_to line leads straight into the
    # write-discipline paragraph, the instruction line into the locate guidance
    assert "caller: /a\n\nThis is NEW knowledge to add." in store_tasks[0]
    assert "Instruction:\nfix something\n\nLocate the target concept(s)" in update_tasks[0]


async def test_handle_store_injects_topic_hint_line():
    backend = FakeBackend()
    provider = ScriptedProvider([WRITE, LLMResponse(text="stored")])
    librarian = make_librarian(backend, provider)

    await librarian.handle_store("new knowledge", topic_hint="home-automation")

    task = provider.calls[0][0][1]["content"]
    assert "Topic hint from the caller: home-automation" in task
    # the topic-hint line sits between the kind-hint and relates_to lines
    assert (
        task.index("Kind hint from the caller:")
        < task.index("Topic hint from the caller:")
        < task.index("Related concept IDs suggested by the caller:")
    )


async def test_handle_store_topic_hint_defaults_to_none_provided():
    backend = FakeBackend()
    provider = ScriptedProvider([WRITE, LLMResponse(text="stored")])
    librarian = make_librarian(backend, provider)

    await librarian.handle_store("new knowledge")

    task = provider.calls[0][0][1]["content"]
    assert "Topic hint from the caller: none provided" in task


async def test_handle_store_task_clarifies_backlinks_not_placement():
    backend = FakeBackend()
    provider = ScriptedProvider([WRITE, LLMResponse(text="stored")])
    librarian = make_librarian(backend, provider)

    await librarian.handle_store("knowledge", relates_to=["/a"])

    task = provider.calls[0][0][1]["content"]
    assert "back-link candidates, NOT placement hints" in task
    assert "target topic area" in task
