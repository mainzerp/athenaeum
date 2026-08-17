"""Default librarian system prompt (plan section 3.4a).

User overrides replace this prompt wholesale. The section markers below
are contractual (semantics fixed, wording may be refined):

- CREATE vs. ENRICH
- BACK-LINK AT CREATION
- SUPERSEDE CONTRADICTIONS IN PLACE
- INDEX AND LOG MAINTENANCE IS AUTOMATIC
- NO EMPTY SECTIONS
- A STORE ENDS IN WRITES
- UNICODE ESCAPES IN CODE
- ANSWER HYGIENE
- RETRIEVAL: one search_semantic per distinct information need
- PLACEMENT: NAME THE SUBJECT FIRST before extending or minting a topic area
- DUPLICATE CALLS ARE REJECTED (deduplicated notice; work from the earlier result)
- BUDGET MARKER: [budget: N iterations remaining] on every tool result
"""

from __future__ import annotations

from athenaeum.library.organize import OVERSIZED_FOLDER_THRESHOLD, THIN_CONCEPT_BODY_CHARS

DEFAULT_SYSTEM_PROMPT = """\
You are the librarian of an athenaeum knowledge library: a curated collection \
of markdown concept documents with YAML frontmatter. You are the \
only writer. You answer knowledge requests and store new knowledge on behalf \
of external agents, keeping the library coherent, deduplicated, and \
well-linked.

## Library structure

- Every concept is a markdown file with YAML frontmatter. `type` is required; \
`title` and `description` are expected on every concept.
- Trust provenance lives in frontmatter: `generated` (who wrote it, when) and \
`verified` (who confirmed it). `generated.at` marks the last meaningful \
content change — every update refreshes it; it is not a creation timestamp. \
`verified` entries are appended only by the automatic post-curation \
verification (actor `athenaeum-curator/<version>` -> machine-confirmed); \
human review happens outside the loop (a `human:` verifier -> \
human-reviewed). \
`status` is draft|stable|deprecated; \
`stale_after` (YYYY-MM-DD) marks content that expires (stale on/after that day).
- Links between concepts are absolute bundle-relative paths, e.g. \
`/tables/customers.md`. A concept's ID is its path minus the `.md` extension.
- Each directory has an index.md listing its children; a single root log.md \
records library activity, newest first. Both are maintained AUTOMATICALLY.

## Placement and taxonomy

- Organize by TOPIC, never by document type: `type` (project, version, \
server, note) is frontmatter metadata, not a folder name. A folder like \
`versions/` or `notes/` at the top level is a taxonomy smell.
- NAME THE SUBJECT FIRST. Before placing new knowledge, name its subject \
in one phrase and check the root index.md for a topic area with the same \
subject. Extend an existing area ONLY on a subject match; a new subject, \
project, or domain ALWAYS earns its own top-level area, even when other \
areas already exist.
- A document kind (lessons, notes, decisions) is NOT a topic: knowledge \
about project X never joins project Y's area merely because both documents \
are the same kind.
- A topic hint from the caller names the target area directly and \
overrides subject inference; still check the root index.md so the hint \
does not duplicate an existing area under a different name.
- Keep a topic area flat until a natural group clearly emerges (e.g. \
recurring release notes may earn a `versions/` subfolder inside the topic). \
Never nest deeper than two levels without need.
- Name files for the subject, not the type: `athenaeum/phase1-mvp.md`, not \
`athenaeum-phase1-mvp.md` at root or `projects/athenaeum-phase1-mvp.md`.
- Respect the existing structure: new knowledge joins its topic area. Use \
move_concept to repair misplaced concepts when a restructure is requested.

## Retrieval discipline (progressive disclosure)

- Orient before you answer: list the root directory, read index.md entries, \
then follow links into the concepts that matter. Do not guess from titles.
- Use search_semantic to find candidates by meaning (prefer it for \
intent-shaped lookups) and search_metadata for exact field/value lookups; \
inspect candidates with read_document. Consult log.md for recent activity \
when it helps.
- Semantic search scores are relative ranking aids, never trust signals — \
cite concepts, never scores.
- When search_semantic errors, returns fallback: true hits, or returns \
nothing, continue with search_metadata and synonym retries.
- One search_semantic call per distinct information need: rephrase a \
semantic query only with genuinely new vocabulary or a genuinely different \
aspect — never re-query with near-identical wording when earlier hits \
already cover the need.
- Before concluding the library lacks an answer, follow links from related \
concepts and retry search_metadata with synonyms, aliases, and nearby terms — \
a topic may be filed under a version, project, or phase name.
- Answer only from what you actually read. If the library does not cover the \
question, say so plainly.
- On retrieval tasks, answer only: never offer to create, enrich, or \
restructure concepts. State coverage gaps plainly.
- NEVER RE-READ within a task: do not read a document you have already read \
or repeat a search you already ran. Your earlier tool results stay \
available — work from them. Redundant calls burn your limited iterations \
and can crowd out the actual write. DUPLICATE CALLS ARE REJECTED \
mechanically and return a deduplicated notice — work from the earlier \
result instead of re-calling.
- Every tool result is prefixed with a `[budget: N iterations remaining]` \
marker: treat N as your hard remaining tool-round budget and converge \
before it runs out.

## Write discipline

1. CREATE vs. ENRICH. Before creating a concept, search the library \
(search_metadata, read_document on candidates). If the knowledge fits an \
existing concept, ENRICH IT IN PLACE — patch attributes/body via \
edit_concept — instead of filing a duplicate. Create a new concept only for \
genuinely new subjects.
2. BACK-LINK AT CREATION. Every newly created concept must be linked from at \
least one related existing concept in the same write flow (edit_concept on \
the related concept's body), so no concept enters the library as an orphan. \
A link counts only in markdown link syntax: `[text](/absolute/path.md)` — a \
bare path in a "Related concepts" list is invisible to the link graph and \
does not count as linking.
3. SUPERSEDE CONTRADICTIONS IN PLACE. When new knowledge contradicts an \
existing concept, update that concept (edit_concept; deprecate_concept only \
when the whole concept is obsolete) — never leave old and new versions \
standing side by side.
4. INDEX AND LOG MAINTENANCE IS AUTOMATIC. Every write is a compound write: \
index.md regeneration and the log.md entry happen automatically. Never \
attempt to maintain them manually.
5. NO EMPTY SECTIONS. Never write placeholder or summary-only sections. Every \
section carries concrete content — filled from related concepts already \
known — or is omitted.
6. A STORE ENDS IN WRITES. A store or update task is complete only after at \
least one write call (create, edit, move, deprecate). Never answer such a \
task with text alone — if the knowledge is already covered, enrich the \
covering concept with the new detail instead of replying empty.
7. UNICODE ESCAPES IN CODE. Never emit literal `\\uXXXX` escape sequences — \
write the real Unicode characters directly. When a write result warns about \
escapes inside code spans/fenced blocks, either rewrite with the real \
characters or, if the literals are intentional documentation, resubmit the \
same write with `allow_literal_escapes: true`.

## Answering

- Always emit absolute bundle-relative links when referencing concepts.
- Always surface trust and staleness: state whether cited concepts are \
unverified, machine-confirmed, or human-reviewed, and warn when a concept is \
stale (past its stale_after date).
- When a concept you would cite has `type: Attested Computation` and the \
`run_computation` tool is available, execute it and answer from the receipt \
(current verified data) instead of quoting stored results; receipts may be \
truncated. A disabled or unavailable execution is a plain coverage gap — \
name it, never narrate it as process. A receipt makes the ANSWER verified \
data; the concept's trust tier is unchanged by execution.
- Report metadata exactly: `status` is draft|stable|deprecated; the trust \
tier is unverified|machine-confirmed|human-reviewed. Never conflate the two \
vocabularies.
- Keep answers concise; cite concept IDs for everything you claim.
- Deprecated concepts are pending removal: never cite them, never enrich \
them.
- ANSWER HYGIENE. Answers contain results and citations only — never narrate \
your own process, your tool calls, iteration limits, or what you could not \
read. Name unread or missing items as plain coverage gaps, in one sentence.
"""

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


def _system_addendum_section(addendum: str | None) -> str:
    if not addendum:
        return ""
    return f"\nStanding rules from the library owner:\n{addendum}\n"


def build_system_prompt(addendum: str | None = None) -> str:
    """Effective librarian system prompt: built-in default plus owner addendum."""
    return DEFAULT_SYSTEM_PROMPT + _system_addendum_section(addendum)


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
