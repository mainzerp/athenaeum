"""Tests for the curator agent (athenaeum.curator): maintain/curate runs.

Moved out of test_librarian_loop.py with the curator split; the fakes and
helpers (FakeBackend, ScriptedProvider, FailAfterProvider, make_librarian,
make_real_backend, tc) are reused via import (test_scheduler.py precedent).
"""

import dataclasses
import tempfile

import pytest

from athenaeum.curator.agent import (
    CURATOR_VERIFIER,
    MAX_PAYLOAD_EXCERPT,
    MAX_STORE_PAYLOAD_REVIEWS,
    Curator,
)
from athenaeum.curator.prompts import CURATOR_SYSTEM_PROMPT
from athenaeum.curator.tools import CURATOR_TOOL_SCHEMAS
from athenaeum.librarian.agent import LibrarianConfig, LibrarianNoWriteError
from athenaeum.librarian.llm import LLMConfig, LLMResponse, ToolCall
from athenaeum.library.payloads import PayloadStore
from test_librarian_loop import (
    FailAfterProvider,
    FakeBackend,
    ScriptedProvider,
    make_librarian,
    make_real_backend,
    tc,
)


def make_curator(
    backend, provider, max_iterations=3, *, root=None, computation_runner=None
) -> Curator:
    config = LibrarianConfig(
        user_id="user-1",
        llm=LLMConfig(provider="openai", model="m", api_key="k", max_iterations=max_iterations),
    )
    # The root defaults to a fresh temp dir: handle_store's payload archive
    # (0.20.0) writes under <root>/.athenaeum/payloads — never a fixed path.
    return Curator(
        root or tempfile.mkdtemp(prefix="athenaeum-curator-"),
        config,
        backend=backend,
        provider=provider,
        computation_runner=computation_runner,
    )


# --- curator tool surface + system prompt pins (D4/D5) -----------------------


def test_curator_tool_schemas_exclude_run_computation():
    names = [t["name"] for t in CURATOR_TOOL_SCHEMAS]
    assert names == [
        "list_dir",
        "read_document",
        "search_metadata",
        "search_semantic",
        "write_concept",
        "edit_concept",
        "move_concept",
        "deprecate_concept",
        "delete_concept",
        "link_check",
    ]
    assert "run_computation" not in names


def test_curator_system_prompt_sections_and_no_run_computation():
    # inherited section markers (placement/retrieval/write discipline)
    assert "NAME THE SUBJECT FIRST" in CURATOR_SYSTEM_PROMPT
    assert "DUPLICATE CALLS ARE REJECTED" in CURATOR_SYSTEM_PROMPT
    assert "CREATE vs. ENRICH" in CURATOR_SYSTEM_PROMPT
    assert "ANSWER HYGIENE" in CURATOR_SYSTEM_PROMPT
    # the librarian-only answering bullet is omitted
    assert "run_computation" not in CURATOR_SYSTEM_PROMPT
    assert "Attested Computation" not in CURATOR_SYSTEM_PROMPT


async def test_curator_first_message_is_curator_system_prompt(tmp_path):
    """A curate run opens with CURATOR_SYSTEM_PROMPT and the narrowed toolset."""
    root = tmp_path / "lib"
    root.mkdir()
    (root / "thin.md").write_text("---\ntype: Note\ntitle: Thin\n---\nstub\n", encoding="utf-8")
    backend = FakeBackend(
        docs={"/thin.md": {"frontmatter": {"title": "Thin", "type": "Note"}, "body": "stub"}}
    )
    provider = ScriptedProvider([LLMResponse(text="done")])
    curator = make_curator(backend, provider)
    backend.scan_root = root

    await curator.handle_curate()

    assert provider.calls
    assert provider.calls[0][0][0] == {"role": "system", "content": CURATOR_SYSTEM_PROMPT}
    # every completion call receives the narrowed curator toolset
    for _, tools, _ in provider.calls:
        assert tools == CURATOR_TOOL_SCHEMAS


async def test_maintain_noop_when_healthy_without_llm_call():
    backend = FakeBackend(healthy=True)
    provider = ScriptedProvider([LLMResponse(text="should not be used")])
    curator = make_curator(backend, provider)

    result = await curator.handle_maintain()

    assert result == {
        "actions": [],
        "summary": "Library is healthy; no maintenance needed.",
        "healthy": True,
        "verified": [],
    }
    assert provider.calls == []  # no LLM call on the no-op path


async def test_maintain_runs_loop_on_orphaned_bundle():
    docs = {
        "/orphan.md": {"frontmatter": {"title": "Orphan", "type": "Note"}, "body": "o"},
        "/hub.md": {"frontmatter": {"title": "Hub", "type": "Note"}, "body": "h"},
    }
    backend = FakeBackend(docs=docs, healthy=False)
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc(
                        "c1",
                        "edit_concept",
                        {"path": "/hub.md", "new_body": "h\n\nSee [/orphan.md]."},
                    )
                ]
            ),
            LLMResponse(text="Wired the orphan into the hub."),
        ]
    )
    curator = make_curator(backend, provider)

    result = await curator.handle_maintain("fix orphans", agent_label="agent-y")

    assert provider.calls, "maintenance loop must run on an unhealthy bundle"
    assert result["summary"] == (
        "Wired the orphan into the hub."
        "\n\nPost-run check: the library still has open health issues."
        "\n\nPost-run verification: machine-confirmed 1 repaired concept(s)."
    )
    assert result["actions"] == [{"id": "/hub", "title": "Hub", "action": "updated"}]
    # the deterministic post-step machine-confirmed exactly the repaired concept
    assert result["verified"] == [{"id": "/hub", "by": CURATOR_VERIFIER}]
    assert ("verify_concept", "/hub.md", CURATOR_VERIFIER, "agent-y") in backend.calls
    assert result["healthy"] is False  # fake backend stays unhealthy
    assert ("edit_concept", "/hub.md", "agent-y") in backend.calls
    # preamble carries the health report + caller instructions
    task_prompt = provider.calls[0][0][1]["content"]
    assert "/orphan" in task_prompt and "fix orphans" in task_prompt


async def test_maintain_verifies_only_updated_writes():
    """Creations, moves, deprecations, deletes are NOT machine-confirmed."""
    docs = {
        "/orphan.md": {"frontmatter": {"title": "Orphan", "type": "Note"}, "body": "o"},
        "/old.md": {"frontmatter": {"title": "Old", "type": "Note"}, "body": "o"},
    }
    backend = FakeBackend(docs=docs, healthy=False)
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc(
                        "c1",
                        "write_concept",
                        {
                            "path": "/new.md",
                            "frontmatter": {"title": "New", "type": "Note"},
                            "body": "n",
                        },
                    ),
                    tc("c2", "deprecate_concept", {"path": "/old.md"}),
                    tc("c3", "move_concept", {"old_path": "/orphan.md", "new_path": "/moved.md"}),
                ]
            ),
            LLMResponse(text="done"),
        ]
    )
    curator = make_curator(backend, provider)

    result = await curator.handle_maintain()

    assert result["verified"] == []
    assert "Post-run verification" not in result["summary"]
    assert not any(call[0] == "verify_concept" for call in backend.calls)


async def test_maintain_verification_failure_never_fails_run(monkeypatch):
    """A verify_concept failure is logged and skipped; the run still succeeds."""
    docs = {"/hub.md": {"frontmatter": {"title": "Hub", "type": "Note"}, "body": "h"}}
    backend = FakeBackend(docs=docs, healthy=False)

    def boom(path, *, by, at=None, agent_label=None):
        raise RuntimeError("verify exploded")

    monkeypatch.setattr(backend, "verify_concept", boom)
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[tc("c1", "edit_concept", {"path": "/hub.md", "new_body": "h2"})]
            ),
            LLMResponse(text="repaired"),
        ]
    )
    curator = make_curator(backend, provider)

    result = await curator.handle_maintain()

    assert result["verified"] == []
    assert result["actions"] == [{"id": "/hub", "title": "Hub", "action": "updated"}]
    assert "Post-run verification" not in result["summary"]


async def test_maintain_provider_error_after_write_returns_partial_success():
    """AGENT-05: a mid-loop provider failure on the maintain path keeps the
    landed writes (partial success); verification + rescan still run."""
    docs = {"/hub.md": {"frontmatter": {"title": "Hub", "type": "Note"}, "body": "h"}}
    backend = FakeBackend(docs=docs, healthy=False)
    provider = FailAfterProvider(
        [
            LLMResponse(
                tool_calls=[tc("c1", "edit_concept", {"path": "/hub.md", "new_body": "h2"})]
            ),
            # second complete() raises RuntimeError -> partial result
        ]
    )
    curator = make_curator(backend, provider)

    result = await curator.handle_maintain()

    assert result["partial"] is True
    assert result["actions"] == [{"id": "/hub", "title": "Hub", "action": "updated"}]
    assert "interrupted" in result["summary"]
    # verification and the post-run rescan still ran over the landed writes
    assert result["verified"] == [{"id": "/hub", "by": CURATOR_VERIFIER}]
    assert ("verify_concept", "/hub.md", CURATOR_VERIFIER, None) in backend.calls
    assert result["healthy"] is False  # fake backend stays unhealthy


async def test_maintain_verifies_concept_edited_twice_exactly_once():
    """AGENT-06: one concept edited twice in a run yields exactly one
    verify_concept call and one receipt."""
    docs = {"/hub.md": {"frontmatter": {"title": "Hub", "type": "Note"}, "body": "h"}}
    backend = FakeBackend(docs=docs, healthy=False)
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[tc("c1", "edit_concept", {"path": "/hub.md", "new_body": "h2"})]
            ),
            LLMResponse(
                tool_calls=[tc("c2", "edit_concept", {"path": "/hub.md", "new_body": "h3"})]
            ),
            LLMResponse(text="repaired"),
        ]
    )
    curator = make_curator(backend, provider)

    result = await curator.handle_maintain()

    assert result["verified"] == [{"id": "/hub", "by": CURATOR_VERIFIER}]
    verify_calls = [call for call in backend.calls if call[0] == "verify_concept"]
    assert verify_calls == [("verify_concept", "/hub.md", CURATOR_VERIFIER, None)]


async def test_curate_noop_when_well_organized_without_llm_call(tmp_path):
    backend = FakeBackend()
    provider = ScriptedProvider([LLMResponse(text="should not be used")])
    curator = make_curator(backend, provider)
    backend.scan_root = tmp_path  # empty library root: no findings

    result = await curator.handle_curate()

    assert result["actions"] == []
    assert result["summary"] == "Library is well-organized; nothing to curate."
    assert result["organized"] is True
    assert result["verified"] == []
    assert result["findings"]["concepts_scanned"] == 0
    assert result["health_after"] == {"healthy": True, "orphans": 0, "broken_links": 0}
    assert provider.calls == []  # no LLM call on the no-op path


async def test_curate_runs_loop_on_findings(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    (root / "thin.md").write_text("---\ntype: Note\ntitle: Thin\n---\nstub\n", encoding="utf-8")
    docs = {"/thin.md": {"frontmatter": {"title": "Thin", "type": "Note"}, "body": "stub"}}
    backend = FakeBackend(docs=docs)
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[tc("c1", "edit_concept", {"path": "/thin.md", "new_body": "enriched"})]
            ),
            LLMResponse(text="Enriched the thin concept."),
        ]
    )
    curator = make_curator(backend, provider)
    backend.scan_root = root

    result = await curator.handle_curate("tidy up", agent_label="agent-c")

    assert provider.calls, "curate loop must run when findings exist"
    assert result["summary"] == (
        "Enriched the thin concept."
        "\n\nPost-run check: open findings remain (see 'findings'); unaddressed "
        "findings are re-reported on the next run until fixed."
        "\n\nPost-run verification: machine-confirmed 1 repaired concept(s)."
    )
    assert result["actions"] == [{"id": "/thin", "title": "Thin", "action": "updated"}]
    # the same deterministic post-step backs curate runs (happy path)
    assert result["verified"] == [{"id": "/thin", "by": CURATOR_VERIFIER}]
    assert result["organized"] is False  # scanned files unchanged by the fake backend
    # L15: findings are the POST-run report (same epoch as 'organized')
    assert result["findings"]["thin_concepts"] == [
        {"id": "/thin", "title": "Thin", "body_chars": 4}
    ]
    assert result["health_after"] == {"healthy": True, "orphans": 0, "broken_links": 0}
    assert ("edit_concept", "/thin.md", "agent-c") in backend.calls
    # preamble carries the findings report + caller instructions
    task_prompt = provider.calls[0][0][1]["content"]
    assert "CURATION TASK" in task_prompt
    assert "/thin" in task_prompt and "tidy up" in task_prompt


async def test_curate_provider_error_after_write_returns_partial_success(tmp_path):
    """AGENT-05: the same partial-success recovery backs curate runs."""
    root = tmp_path / "lib"
    root.mkdir()
    (root / "thin.md").write_text("---\ntype: Note\ntitle: Thin\n---\nstub\n", encoding="utf-8")
    docs = {"/thin.md": {"frontmatter": {"title": "Thin", "type": "Note"}, "body": "stub"}}
    backend = FakeBackend(docs=docs)
    provider = FailAfterProvider(
        [
            LLMResponse(
                tool_calls=[tc("c1", "edit_concept", {"path": "/thin.md", "new_body": "enriched"})]
            ),
            # second complete() raises RuntimeError -> partial result
        ]
    )
    curator = make_curator(backend, provider)
    backend.scan_root = root

    result = await curator.handle_curate()

    assert result["partial"] is True
    assert result["actions"] == [{"id": "/thin", "title": "Thin", "action": "updated"}]
    assert "interrupted" in result["summary"]
    # verification and the post-run rescan still ran over the landed writes
    assert result["verified"] == [{"id": "/thin", "by": CURATOR_VERIFIER}]
    assert result["organized"] is False  # scanned files unchanged by the fake backend
    assert result["findings"]["thin_concepts"] == [
        {"id": "/thin", "title": "Thin", "body_chars": 4}
    ]
    assert result["health_after"] == {"healthy": True, "orphans": 0, "broken_links": 0}


async def test_curate_preamble_includes_addendum(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    (root / "thin.md").write_text("---\ntype: Note\ntitle: Thin\n---\nstub\n", encoding="utf-8")
    backend = FakeBackend()
    provider = ScriptedProvider([LLMResponse(text="done")])
    config = LibrarianConfig(
        user_id="user-1",
        llm=LLMConfig(provider="openai", model="m", api_key="k"),
        curate_prompt_addendum="never create concepts",
    )
    curator = Curator(root, config, backend=backend, provider=provider)
    backend.scan_root = root

    await curator.handle_curate()

    task_prompt = provider.calls[0][0][1]["content"]
    assert "Standing curation rules from the library owner:" in task_prompt
    assert "never create concepts" in task_prompt


async def test_curate_uses_default_llm_without_override(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    (root / "thin.md").write_text("---\ntype: Note\ntitle: Thin\n---\nstub\n", encoding="utf-8")
    backend = FakeBackend()
    provider = ScriptedProvider([LLMResponse(text="done")])
    curator = make_curator(backend, provider)
    backend.scan_root = root

    await curator.handle_curate()

    # no override: the provider sees the base LLM config, byte-identical to today
    assert provider.calls[0][2] is curator.config.llm
    assert curator._curate_provider is None


async def test_curate_model_override_effective_config(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    (root / "thin.md").write_text("---\ntype: Note\ntitle: Thin\n---\nstub\n", encoding="utf-8")
    backend = FakeBackend()
    base = LLMConfig(provider="openai", model="m", api_key="k")
    curate_llm = LLMConfig(provider="anthropic", model="big-model", api_key="k2")
    config = LibrarianConfig(user_id="user-1", llm=base, curate_llm=curate_llm)
    default_provider = ScriptedProvider([LLMResponse(text="should not be used")])
    curate_provider = ScriptedProvider([LLMResponse(text="curated")])
    curator = Curator(root, config, backend=backend, provider=default_provider)
    curator._curate_provider = curate_provider
    backend.scan_root = root

    result = await curator.handle_curate()

    assert result["summary"].startswith("curated")
    assert "Post-run check:" in result["summary"]
    assert default_provider.calls == []
    effective = curate_provider.calls[0][2]
    assert effective.provider == "anthropic"
    assert effective.model == "big-model"
    assert effective.api_key == "k2"  # the curator's connection has its own credentials


def test_curate_llm_partial_override_and_default():
    base = LLMConfig(provider="openai", model="m", api_key="k")
    curate_llm = LLMConfig(provider="anthropic", model="big-model", api_key="k2")
    curator = Curator(
        "/unused-root",
        LibrarianConfig(user_id="user-1", llm=base, curate_llm=curate_llm),
        backend=FakeBackend(),
    )
    assert curator._curate_llm() is curate_llm

    plain = Curator(
        "/unused-root", LibrarianConfig(user_id="user-1", llm=base), backend=FakeBackend()
    )
    assert plain._curate_llm() is base


async def test_maintain_against_real_backend(tmp_path):
    """handle_maintain against a real LibraryBackend (regression: D1 crash).

    Before the status() orphans-shape fix this crashed with
    AttributeError: 'str' object has no attribute 'get'.
    """
    backend = make_real_backend(tmp_path)
    backend.create_concept("/orphan.md", {"type": "Note", "title": "Orphan"}, "o\n")
    backend.create_concept("/hub.md", {"type": "Note", "title": "Hub"}, "h\n")
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    tc(
                        "c1",
                        "edit_concept",
                        {"path": "/hub.md", "new_body": "h\n\nSee [Orphan](/orphan.md).\n"},
                    )
                ]
            ),
            LLMResponse(text="Wired the orphan into the hub."),
        ]
    )
    curator = make_curator(backend, provider)

    result = await curator.handle_maintain("fix orphans")

    assert result["summary"] == (
        "Wired the orphan into the hub.\n\nPost-run check: the library is now healthy."
        "\n\nPost-run verification: machine-confirmed 1 repaired concept(s)."
    )
    assert result["actions"] == [{"id": "/hub", "title": "Hub", "action": "updated"}]
    # real backend: the repaired concept's frontmatter gained the verified entry
    assert result["verified"] == [{"id": "/hub", "by": CURATOR_VERIFIER}]
    verified = backend.read_document("/hub.md")["frontmatter"]["verified"]
    assert [entry["by"] for entry in verified] == [CURATOR_VERIFIER]
    # real backend re-validates after the edit: /orphan gained an inbound
    # link, /hub an outbound one, and no broken links remain
    assert result["healthy"] is True
    # the dict-shaped orphan entry rendered into the preamble
    preamble = provider.calls[0][0][1]["content"]
    assert "/orphan" in preamble and "Orphan" in preamble


async def test_curate_against_real_backend(tmp_path):
    """handle_curate against a real LibraryBackend: preamble + post-run organized."""
    backend = make_real_backend(tmp_path)
    backend.create_concept("/stub.md", {"type": "Note", "title": "Stub"}, "s\n")
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[tc("c1", "edit_concept", {"path": "/stub.md", "new_body": "x" * 250})]
            ),
            LLMResponse(text="Enriched the stub."),
        ]
    )
    curator = make_curator(backend, provider)

    result = await curator.handle_curate()

    assert result["summary"] == (
        "Enriched the stub."
        "\n\nPost-run check: no open findings remain; the library is well-organized."
        "\n\nPost-run verification: machine-confirmed 1 repaired concept(s)."
    )
    assert result["actions"] == [{"id": "/stub", "title": "Stub", "action": "updated"}]
    assert result["verified"] == [{"id": "/stub", "by": CURATOR_VERIFIER}]
    # the enrichment cleared the only finding: converged on the post-run scan
    assert result["organized"] is True
    # L15: findings are the POST-run report — the fixed thin concept is gone
    assert result["findings"]["thin_concepts"] == []
    # the enriched /stub has no bundle links, so the real validator reports it
    # as an orphan (validate.py) — post-curate observability via health_after
    assert result["health_after"] == {"healthy": False, "orphans": 1, "broken_links": 0}
    preamble = provider.calls[0][0][1]["content"]
    assert "CURATION TASK" in preamble
    assert "/stub" in preamble and "Stub" in preamble


async def test_curate_hygiene_repairs_dirty_concept_without_llm(tmp_path):
    """The deterministic sweep repairs a dirty on-disk body before the
    findings scan: no LLM call, one 'updated' action, no verify receipts."""
    backend = make_real_backend(tmp_path)
    dirty_body = "prose with escape \\u2011 here. " * 10 + "\n"
    (tmp_path / "lib" / "a.md").write_text(
        "---\ntype: Note\ntitle: Alpha\n---\n" + dirty_body,
        encoding="utf-8",
    )
    provider = ScriptedProvider([LLMResponse(text="should not be used")])
    curator = make_curator(backend, provider)

    result = await curator.handle_curate()

    body = backend.read_document("/a.md")["body"]
    assert "\\u" not in body
    assert "‑" in body
    assert result["actions"] == [{"id": "/a", "title": "Alpha", "action": "updated"}]
    assert provider.calls == []  # D6: hygiene repair alone never wakes the LLM
    assert result["verified"] == []  # no receipts for deterministic repairs
    assert result["summary"] == (
        "Library is well-organized; nothing to curate."
        "\n\nContent hygiene: decoded literal unicode escape artifacts in "
        "1 existing concept(s) (F25 stock repair)."
    )
    assert "hygiene_repairs" not in result  # repairs merge into 'actions'
    assert result["organized"] is True


async def test_curate_hygiene_prefilter_leaves_fence_only_file_untouched(tmp_path):
    """Escapes confined to a fenced block skip the deterministic sweep but
    become code-span escape candidates: the D6 gate opens and the curator LLM
    IS called. A curator judging "intentional" (text-only response, no tool
    calls) leaves the file byte-identical; the structural finding is
    re-reported post-run (L14)."""
    backend = make_real_backend(tmp_path)
    body = "```text\n" + "DP\\u20111 " * 30 + "\n```\n"
    target = tmp_path / "lib" / "a.md"
    target.write_text("---\ntype: Note\ntitle: Alpha\n---\n" + body, encoding="utf-8")
    before = target.read_bytes()
    provider = ScriptedProvider(
        [LLMResponse(text="Intentional documentation of the escape format; left unchanged.")]
    )
    curator = make_curator(backend, provider)

    result = await curator.handle_curate()

    assert provider.calls  # a code-span candidate wakes the curator (D6 gate)
    preamble = provider.calls[0][0][1]["content"]
    assert "code-span escape candidates" in preamble
    assert "/a.md" in preamble
    assert result["actions"] == []  # no sweep repair, no LLM write
    assert target.read_bytes() == before  # no rewrite, no commit
    # L14 re-report: the confirmed-intentional literals stay on the findings
    candidates = result["findings"]["code_span_escape_candidates"]
    assert [c["path"] for c in candidates] == ["/a.md"]
    assert result["organized"] is False


async def test_curate_repairs_code_span_escape_candidate(tmp_path):
    """A curator judging "artifact" repairs the candidate via edit_concept
    with the real characters: the post-run rescan (L15) finds nothing left
    and the repair is machine-confirmed."""
    backend = make_real_backend(tmp_path)
    body = "```text\n" + "DP\\u20111 " * 30 + "\n```\n"
    (tmp_path / "lib" / "a.md").write_text(
        "---\ntype: Note\ntitle: Alpha\n---\n" + body, encoding="utf-8"
    )
    fixed_body = "```text\n" + "DP\u20111 " * 60 + "\n```\n"  # decoded, stays >200 chars
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[tc("c1", "edit_concept", {"path": "/a.md", "new_body": fixed_body})]
            ),
            LLMResponse(text="Replaced the escape artifacts with real characters."),
        ]
    )
    curator = make_curator(backend, provider)

    result = await curator.handle_curate()

    assert provider.calls
    assert backend.read_document("/a.md")["body"] == fixed_body
    assert result["findings"]["code_span_escape_candidates"] == []
    assert result["organized"] is True
    assert {"id": "/a", "by": CURATOR_VERIFIER} in result["verified"]


async def test_curate_hygiene_multiple_dirty_files(tmp_path):
    """Every dirty concept is repaired in one run (N files = N commits)."""
    backend = make_real_backend(tmp_path)
    dirty_body = "prose with escape \\u2011 here. " * 10 + "\n"
    (tmp_path / "lib" / "a.md").write_text(
        "---\ntype: Note\ntitle: Alpha\n---\n" + dirty_body,
        encoding="utf-8",
    )
    (tmp_path / "lib" / "b.md").write_text(
        "---\ntype: Note\ntitle: Beta\n---\n" + dirty_body,
        encoding="utf-8",
    )
    provider = ScriptedProvider([LLMResponse(text="should not be used")])
    curator = make_curator(backend, provider)

    result = await curator.handle_curate()

    for path in ("/a.md", "/b.md"):
        body = backend.read_document(path)["body"]
        assert "\\u" not in body
        assert "‑" in body
    assert result["actions"] == [
        {"id": "/a", "title": "Alpha", "action": "updated"},
        {"id": "/b", "title": "Beta", "action": "updated"},
    ]
    assert provider.calls == []


async def test_curate_deprecated_cleanup_finding_reaches_curator(tmp_path):
    """A deprecated concept with no live inbound links is a cleanup finding:
    it wakes the curator (scripted delete_concept) and converges post-run."""
    backend = make_real_backend(tmp_path)
    backend.create_concept("/old.md", {"type": "Note", "title": "Old"}, "stale\n")
    backend.deprecate_concept("/old.md")
    provider = ScriptedProvider(
        [
            LLMResponse(tool_calls=[tc("c1", "delete_concept", {"path": "/old.md"})]),
            LLMResponse(text="Deleted the deprecated concept."),
        ]
    )
    curator = make_curator(backend, provider)

    result = await curator.handle_curate()

    assert provider.calls, "a cleanup finding must wake the curator"
    preamble = provider.calls[0][0][1]["content"]
    assert "deprecated concepts pending cleanup" in preamble
    assert "- /old (Old)" in preamble
    assert [a["id"] for a in result["actions"]] == ["/old"]
    assert result["actions"][0]["action"] == "deleted"
    # post-run scan: the deprecated concept is gone, nothing left to clean
    assert result["organized"] is True
    assert result["findings"]["deprecated_cleanup"] == []


async def test_curate_wakes_on_failed_store_payload_once(tmp_path):
    """D3.6: a failed store payload is a one-shot curate finding — it wakes
    a paid run and is consumed by it (post-run key empty, organized True)."""
    backend = FakeBackend()
    store_provider = ScriptedProvider([LLMResponse(text="no tools")])
    storer = make_librarian(backend, store_provider, root=tmp_path / "lib")
    with pytest.raises(LibrarianNoWriteError):
        await storer.handle_store("unstored knowledge about zeta")

    curator_provider = ScriptedProvider([LLMResponse(text="Reviewed the failed payload.")])
    curator = make_curator(backend, curator_provider, root=tmp_path / "lib")
    # the payload archive lives under a dot-dir: structural scans stay empty
    backend.scan_root = tmp_path / "lib"

    result = await curator.handle_curate()

    assert curator_provider.calls, "a payload-only finding must wake a curate run"
    preamble = curator_provider.calls[0][0][1]["content"]
    assert "store payloads pending review" in preamble
    assert "unstored knowledge about zeta" in preamble
    assert result["organized"] is True
    # one-shot: the post-run report never re-lists the consumed payload
    assert result["findings"]["store_payload_reviews"] == []


async def test_curate_skips_payloads_older_than_last_run(tmp_path):
    """The time filter is payload-store-local (D3.6/R22): payloads received
    before curate_last_run_at are not reported — each payload is reported
    exactly once because curate_last_run_at advances."""
    backend = FakeBackend()
    provider = ScriptedProvider([LLMResponse(text="unused")])
    config = LibrarianConfig(
        user_id="user-1",
        llm=LLMConfig(provider="openai", model="m", api_key="k"),
        curate_last_run_at="2026-08-02T00:00:00+00:00",
    )
    curator = Curator(tmp_path / "lib", config, backend=backend, provider=provider)
    backend.scan_root = tmp_path / "lib"
    PayloadStore(tmp_path / "lib").create(
        payload_record(
            "20260801T000000Z-aaaa1111",
            received_at="2026-08-01T00:00:00+00:00",  # before the baseline
            content="old failed store",
        )
    )

    result = await curator.handle_curate()

    assert provider.calls == []  # nothing wakes the curator
    assert result["organized"] is True
    assert result["summary"] == "Library is well-organized; nothing to curate."


def test_store_payload_reviews_digest_bounds(tmp_path):
    """The digest keeps the MAX_STORE_PAYLOAD_REVIEWS newest error/partial
    records and bounds excerpts at MAX_PAYLOAD_EXCERPT chars."""
    config = LibrarianConfig(
        user_id="user-1",
        llm=LLMConfig(provider="openai", model="m", api_key="k"),
    )
    curator = Curator(
        tmp_path / "lib", config, backend=FakeBackend(), provider=ScriptedProvider([])
    )
    store = PayloadStore(tmp_path / "lib")
    for day in range(1, 8):
        store.create(
            payload_record(
                f"2026080{day}T000000Z-aaaa111{day}",
                received_at=f"2026-08-0{day}T00:00:00+00:00",
                content="x" * 200,
            )
        )
    store.create(payload_record("20260808T000000Z-aaaa1118", outcome="ok"))
    store.create(payload_record("20260809T000000Z-aaaa1119", outcome="busy"))

    reviews = curator._store_payload_reviews()

    # ok/busy outcomes are excluded; newest first, capped at 5
    assert [r["request_id"] for r in reviews] == [
        "20260807T000000Z-aaaa1117",
        "20260806T000000Z-aaaa1116",
        "20260805T000000Z-aaaa1115",
        "20260804T000000Z-aaaa1114",
        "20260803T000000Z-aaaa1113",
    ]
    assert len(reviews) == MAX_STORE_PAYLOAD_REVIEWS
    assert all(r["outcome"] == "error" for r in reviews)
    assert all(len(r["excerpt"]) == MAX_PAYLOAD_EXCERPT for r in reviews)


async def test_curate_unaddressed_findings_persist_across_runs(tmp_path):
    """L14: an unaddressed finding is re-reported on the next curate run.

    Regression shape: the baseline (curate_last_run_at) is AFTER the finding
    was created — the old changed-set scoping made the finding vanish and run
    N+1 claimed "well-organized".
    """
    root = tmp_path / "lib"
    root.mkdir()
    (root / "thin.md").write_text(
        "---\ntype: Note\ntitle: Thin\ngenerated: {at: '2026-01-01T00:00:00+00:00'}\n---\nstub\n",
        encoding="utf-8",
    )
    backend = FakeBackend()
    provider = ScriptedProvider(
        [
            LLMResponse(text="did nothing"),  # run 1: LLM does not fix it
            LLMResponse(text="did nothing again"),  # run 2
        ]
    )
    curator = make_curator(backend, provider)
    backend.scan_root = root
    curator.config.curate_last_run_at = "2999-01-01T00:00:00+00:00"  # the amnesia condition

    first = await curator.handle_curate()
    second = await curator.handle_curate()

    # both runs invoked the LLM (no false "well-organized" no-op)...
    assert len(provider.calls) == 2
    # ...and both report the still-open finding (post-run epoch)
    for result in (first, second):
        assert result["organized"] is False
        assert [c["id"] for c in result["findings"]["thin_concepts"]] == ["/thin"]
        assert "open findings remain" in result["summary"]


async def test_curate_summary_and_findings_share_post_run_epoch(tmp_path):
    """L15 (F18): fixed items are not reported as remaining — one epoch."""
    root = tmp_path / "lib"
    root.mkdir()
    thin = root / "thin.md"
    thin.write_text("---\ntype: Note\ntitle: Thin\n---\nstub\n", encoding="utf-8")

    class FixingProvider:
        """Fixes the finding on disk as part of the scripted tool round."""

        def __init__(self):
            self.calls: list[tuple] = []

        async def complete(self, messages, tools, config) -> LLMResponse:
            self.calls.append((list(messages), list(tools), config))
            if len(self.calls) == 1:
                return LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="edit_concept",
                            arguments={"path": "/thin.md", "new_body": "x" * 250},
                        )
                    ]
                )
            # the edit landed on disk before the final answer
            thin.write_text(
                "---\ntype: Note\ntitle: Thin\n---\n" + "x" * 250 + "\n", encoding="utf-8"
            )
            return LLMResponse(text="Enriched the thin concept.")

    backend = FakeBackend(
        docs={"/thin.md": {"frontmatter": {"title": "Thin", "type": "Note"}, "body": "stub"}}
    )
    curator = make_curator(backend, FixingProvider())
    backend.scan_root = root

    result = await curator.handle_curate()

    assert result["organized"] is True
    assert result["findings"]["thin_concepts"] == []  # fixed finding not "remaining"
    assert result["summary"] == (
        "Enriched the thin concept."
        "\n\nPost-run check: no open findings remain; the library is well-organized."
        "\n\nPost-run verification: machine-confirmed 1 repaired concept(s)."
    )


def test_curate_provider_inheritance_survives_derived_config_copy():
    """Inheritance dispatches on the explicit None marker: deriving a config
    whose llm is a copy cannot silently split off a second provider."""
    base = LLMConfig(provider="openai", model="m", api_key="k")
    provider = ScriptedProvider([LLMResponse(text="x")])
    curator = Curator(
        "/unused-root",
        LibrarianConfig(user_id="user-1", llm=base),
        backend=FakeBackend(),
        provider=provider,
    )
    curator.config = dataclasses.replace(curator.config, llm=dataclasses.replace(base))
    assert curator.config.curate_llm is None
    assert curator._curate_provider_or_default() is provider
    assert curator._curate_provider is None


def payload_record(
    request_id: str,
    *,
    outcome: str = "error",
    received_at: str = "2026-08-01T00:00:00+00:00",
    content: str = "c",
) -> dict:
    return {
        "request_id": request_id,
        "tool": "store_knowledge",
        "user_id": "user-1",
        "agent_label": "agent-a",
        "trace_id": "20260801T000000Z-deadbeef",
        "received_at": received_at,
        "outcome": outcome,
        "error": "LibrarianNoWriteError" if outcome == "error" else None,
        "params": {"content": content},
        "stored": [],
    }
