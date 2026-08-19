"""Curator prompts: system prompt + task preambles for maintain/curate runs.

The curator system prompt is composed from the SAME section text as the
librarian's ``DEFAULT_SYSTEM_PROMPT`` (athenaeum.librarian.prompts): a
curator-specific role section + the shared structure/placement/retrieval/
write-discipline sections verbatim + the answering section MINUS the
``run_computation`` bullet (that tool is not in the curator tool surface).
The task templates carry the per-run health/organization reports; the
"create no new concepts during curation" rule stays prompt-level (in
``CURATE_TASK_TEMPLATE``), exactly as before the curator split.
"""

from __future__ import annotations

from athenaeum.librarian.prompts import (
    _ANSWERING_POST_SECTION,
    _ANSWERING_PRE_SECTION,
    _LIBRARY_STRUCTURE_SECTION,
    _PLACEMENT_SECTION,
    _RETRIEVAL_SECTION,
    _WRITE_DISCIPLINE_SECTION,
    _system_addendum_section,
)
from athenaeum.library.organize import OVERSIZED_FOLDER_THRESHOLD, THIN_CONCEPT_BODY_CHARS

# Curator role: derived from the MAINTAIN/CURATE task identity (repair graph
# health, fix taxonomy, merge duplicates, deprecated cleanup) — no new rules.
_CURATOR_ROLE_SECTION = """\
You are the curator of an athenaeum knowledge library: a curated collection \
of markdown concept documents with YAML frontmatter. You run maintenance \
and curation passes over the whole library on behalf of its owner: you \
repair graph health (orphans, broken links), fix taxonomy, merge duplicates, \
and clean up deprecated concepts, keeping the library coherent, \
deduplicated, and well-linked.
"""

CURATOR_SYSTEM_PROMPT = (
    _CURATOR_ROLE_SECTION
    + "\n"
    + _LIBRARY_STRUCTURE_SECTION
    + "\n"
    + _PLACEMENT_SECTION
    + "\n"
    + _RETRIEVAL_SECTION
    + "\n"
    + _WRITE_DISCIPLINE_SECTION
    + "\n"
    + _ANSWERING_PRE_SECTION
    + _ANSWERING_POST_SECTION
)


def build_curator_system_prompt(addendum: str | None = None) -> str:
    """Effective curator system prompt: built-in default plus owner addendum.

    The owner addendum is the same ``config.prompt_addendum`` the librarian
    uses (pre-split behavior: it applied to curator runs too); the
    curator-specific ``curate_prompt_addendum`` stays in the task preamble
    via ``build_curate_preamble``.
    """
    return CURATOR_SYSTEM_PROMPT + _system_addendum_section(addendum)


MAINTAIN_TASK_TEMPLATE = """\
MAINTENANCE TASK. The library health report below was produced deterministically \
by the validator. Repair the graph: wire orphan concepts into related concepts \
(edit_concept on the related concept's body to add a markdown link \
`[text](/absolute/path.md)` (a bare path is invisible to the link graph), and \
link back where appropriate), and fix or remove dangling links \
(edit_concept the source body; drop links that have no valid target). \
Prefer enriching existing structure over creating new concepts. Concepts you \
repair via edit_concept are machine-confirmed automatically after the run — \
you cannot and must not write `verified` yourself; never claim manual \
verification in your summary. When done, \
summarize every repair you made.

Current health report:
- healthy: {healthy}
- orphans (no inbound AND no outbound links):
{orphans}
- broken links (source -> target):
{broken_links}
- validator warnings: {warnings}, errors: {errors}
{instructions}"""


def build_maintain_preamble(status: dict, instructions: str | None = None) -> str:
    """Render the maintenance task preamble from a backend.status() report."""
    health = status.get("health", {})
    orphans = health.get("orphans") or []
    broken = health.get("broken_links") or []
    orphan_lines = (
        "\n".join(f"  - {o.get('id')} ({o.get('title', '')})" for o in orphans) or "  - none"
    )
    broken_lines = (
        "\n".join(f"  - {b.get('source')} -> {b.get('target')}" for b in broken) or "  - none"
    )
    extra = ""
    if instructions:
        extra = f"\nAdditional instructions from the caller:\n{instructions}\n"
    return MAINTAIN_TASK_TEMPLATE.format(
        healthy=status.get("healthy"),
        orphans=orphan_lines,
        broken_links=broken_lines,
        warnings=health.get("warnings", 0),
        errors=health.get("errors", 0),
        instructions=extra,
    )


CURATE_TASK_TEMPLATE = """\
CURATION TASK. The organization report below was produced by a deterministic \
structural scan of the library, plus one embedding-similarity pass (semantic \
duplicate candidates), which is a model judgment, not a structural fact. \
Act ONLY on the reported findings:

- Type-named folders: a top-level folder named after a document type is a \
taxonomy smell. Move its concepts into their proper topic areas \
(move_concept); the emptied folder disappears automatically.
- Oversized folders: the topic may have earned a natural subgroup. Create \
it only when a clear theme exists among the concepts; otherwise leave the \
folder flat.
- Thin concepts: enrich the stub with real content from related concepts \
(edit_concept), or merge it into the concept it belongs to.
- Near-duplicate candidates: merge each pair. Enrich the better-placed, \
better-developed concept (the merge target) via edit_concept with \
everything worth keeping from the other (the source), then supersede the \
source via deprecate_concept. Use delete_concept only when the source has \
no inbound links; after any delete_concept, repair the inbound links it \
reports. Never merge or deprecate documents that differ only in a \
version/phase identifier; numbered series are intentional.
- Semantic duplicate candidates: pairs that read as near-identical in \
meaning though their titles share few tokens. Verify with read_document \
before merging; the similarity score is a relative ranking aid. The series \
rule applies with full force here: never merge or deprecate documents that \
differ only in a version/phase identifier; numbered series are intentional.
- Deprecated concepts pending cleanup: each listed deprecated concept has \
no inbound links from non-deprecated concepts. Delete it via delete_concept \
and repair the inbound links the delete reports. Never delete a deprecated \
concept that is NOT listed: live inbound links keep it in the library until \
their owners remove them. Deleting can orphan the concept's targets — the \
post-run health check and the next maintenance pass are the safety net.
- Store payload reviews: a store_knowledge request ended in an error or \
partial state since the previous curate run. Re-store the archived content \
per your write discipline (search first, enrich in place, back-link); skip \
content the library already covers.
- Code-span escape candidates: literal `\\uXXXX` sequences inside code \
spans/fenced blocks. For each file: read_document, judge every listed \
occurrence — an artifact is repaired via edit_concept with the real \
characters in place of the escapes; intentional documentation (e.g. text \
explaining the escape format itself) is left unchanged. When a repair edit \
keeps OTHER intentional literals inside code spans, pass \
`allow_literal_escapes: true` on that edit.

Convergence rules: respect the existing topic structure; never move \
well-placed concepts; do not touch concepts that appear in no finding; \
prefer enriching over creating; create no new concepts during curation. \
Concepts you repair via edit_concept are machine-confirmed automatically \
after the run — you cannot and must not write `verified` yourself; never \
claim manual verification in your summary. \
When done, summarize every move, enrichment, and merge you made.

Current organization report:
- type-named top-level folders:
{type_named_folders}
- oversized folders (threshold: {oversized_threshold} concepts):
{oversized_folders}
- thin concepts (body under {thin_threshold} characters):
{thin_concepts}
- near-duplicate candidates:
{near_duplicates}
- semantic duplicate candidates (embedding similarity):
{semantic_duplicates}
- deprecated concepts pending cleanup (no live inbound links):
{deprecated_cleanup}
- store payloads pending review (failed or partial since the previous run):
{store_payload_reviews}
- code-span escape candidates (literal escapes inside code spans/fences):
{code_span_escapes}
- scope: findings cover the whole library on every run; an unaddressed \
finding is re-reported until it is actually fixed. Store payload reviews \
are the exception: each is reported once, after the run that recorded it, \
and never re-reported.
{instructions}{addendum}"""


def _curate_addendum_section(addendum: str | None) -> str:
    if not addendum:
        return ""
    return f"\nStanding curation rules from the library owner:\n{addendum}\n"


def build_curate_preamble(
    findings: dict, instructions: str | None = None, addendum: str | None = None
) -> str:
    """Render the curation task preamble from an organization_findings report."""
    type_named = findings.get("type_named_folders") or []
    oversized = findings.get("oversized_folders") or []
    thin = findings.get("thin_concepts") or []
    duplicates = findings.get("near_duplicate_candidates") or []
    semantic = findings.get("semantic_duplicate_candidates") or []
    type_named_lines = (
        "\n".join(f"  - {f.get('path')} ({f.get('concepts')} concepts)" for f in type_named)
        or "  - none"
    )
    oversized_lines = (
        "\n".join(f"  - {f.get('path')} ({f.get('concepts')} concepts)" for f in oversized)
        or "  - none"
    )
    thin_lines = (
        "\n".join(
            f"  - {c.get('id')} ({c.get('title', '')}, {c.get('body_chars')} chars)" for c in thin
        )
        or "  - none"
    )
    duplicate_lines = (
        "\n".join(
            f"  - {d.get('ids', ['?', '?'])[0]} <-> {d.get('ids', ['?', '?'])[1]}"
            f" (similarity {d.get('similarity')}; shared: {', '.join(d.get('shared') or [])})"
            for d in duplicates
        )
        or "  - none"
    )
    semantic_lines = (
        "\n".join(
            f"  - {d.get('ids', ['?', '?'])[0]} <-> {d.get('ids', ['?', '?'])[1]}"
            f" (similarity {d.get('similarity')}; semantic)"
            for d in semantic
        )
        or "  - none"
    )
    cleanup = findings.get("deprecated_cleanup") or []
    cleanup_lines = (
        "\n".join(f"  - {c.get('id')} ({c.get('title', '')})" for c in cleanup) or "  - none"
    )
    reviews = findings.get("store_payload_reviews") or []
    review_lines = (
        "\n".join(
            f"  - {r.get('request_id')} ({r.get('outcome')}"
            f"{', ' + r['error'] if r.get('error') else ''}): {r.get('excerpt', '')}"
            for r in reviews
        )
        or "  - none"
    )
    escapes = findings.get("code_span_escape_candidates") or []
    escape_lines = (
        "\n".join(
            f"  - {c.get('path')} ({c.get('title', '')}): "
            + "; ".join(
                f"line {o.get('line')}: {o.get('snippet', '')}"
                for o in (c.get("occurrences") or [])
            )
            for c in escapes
        )
        or "  - none"
    )
    extra = ""
    if instructions:
        extra = f"\nAdditional instructions from the caller:\n{instructions}\n"
    return CURATE_TASK_TEMPLATE.format(
        type_named_folders=type_named_lines,
        oversized_threshold=OVERSIZED_FOLDER_THRESHOLD,
        oversized_folders=oversized_lines,
        thin_threshold=THIN_CONCEPT_BODY_CHARS,
        thin_concepts=thin_lines,
        near_duplicates=duplicate_lines,
        semantic_duplicates=semantic_lines,
        deprecated_cleanup=cleanup_lines,
        store_payload_reviews=review_lines,
        code_span_escapes=escape_lines,
        instructions=extra,
        addendum=_curate_addendum_section(addendum),
    )


def render_curate_prompt_display(addendum: str | None) -> str:
    """Effective curator prompt for display: template text + addendum section."""
    return CURATE_TASK_TEMPLATE.replace("{addendum}", _curate_addendum_section(addendum))
