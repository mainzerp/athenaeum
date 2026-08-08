# Athenaeum — OKF v0.2 Reference

> A practical reference for contributors working on athenaeum. **Authoritative source:** [OKF v0.2 SPEC.md](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md) — when this document and the spec disagree, the spec wins. Section numbers (§) below refer to the spec.

## 1. The format in one paragraph

An OKF **knowledge bundle** is a plain directory tree of UTF-8 markdown files. Each **concept** is one markdown file with a YAML **frontmatter** block (delimited by `---` lines) and a free-form markdown **body**. Two filenames are reserved (`index.md`, `log.md`); every other `.md` file is a concept. The **concept ID** is the file's path within the bundle minus the `.md` suffix (e.g. `/tables/customers.md` -> concept ID `/tables/customers`). That is the entire structural contract — everything else is optional metadata families and conventions.

## 2. Bundle structure

```
library/                      # the bundle root (per user: data/users/<user_id>/library/)
  index.md                    # directory listing (progressive disclosure), optional per spec
  log.md                      # chronological update history, optional per spec
  some-concept.md             # a concept at the root
  subdir/
    index.md
    another-concept.md
  .git/                       # athenaeum-internal commit history (0.22.0); invisible to OKF consumers
  .athenaeum/versions/        # pre-0.22.0 snapshot remnants (inert, gitignored)
```

- Directory organization is up to the producer (in athenaeum: the librarian).
- **Athenaeum policy (stricter than the spec, still conformant):** an `index.md` at *every* directory and a single root `log.md`, both maintained automatically on every write.

### Reserved filenames (§3.1)

| Filename | Purpose |
|---|---|
| `index.md` | Directory listing (§8). MUST NOT be used as a concept document. |
| `log.md` | Update history (§9). MUST NOT be used as a concept document. |

`LibraryBackend.create_concept` refuses both names anywhere in the tree.

## 3. Frontmatter fields

### Required (§4.1)

| Field | Notes |
|---|---|
| `type` | Non-empty string; the only always-required key. Example values: `Reference`, `Playbook`, `Metric`, `API Endpoint`, `Attested Computation`. Not centrally registered; consumers must tolerate unknown values. |

A concept carrying just `type` is fully conformant.

### Recommended (§4.1)

| Field | Notes |
|---|---|
| `title` | Human-readable display name; consumers may derive one from the filename if absent. |
| `description` | One sentence; used by `index.md` generators, search snippets, previews. Athenaeum's index generator requires it for description-backed entries. |
| `resource` | URI uniquely identifying the underlying asset; absent for abstract ideas. |
| `tags` | YAML list of short strings for cross-cutting categorization. |

### Trust family (§5.2, §5.3)

| Field | Notes |
|---|---|
| `generated` | `{ by, at }`. `by` is REQUIRED within `generated` (an actor, §7); `at` is an ISO 8601 datetime of the last meaningful content change. **Athenaeum extension sub-keys** (extension rule §4.1): `requested_by` (`human:<username>` — the local account whose MCP token requested the write) and `via` (`mcp_chat`); both are preserved on later edits/deprecations unless a new requester is supplied. **Athenaeum writer:** only the backend injects/refreshes `generated` — `edit_concept` refuses it in patches/removals (0.23.1, same rule as `verified`). |
| `verified` | List of `{ by, at }` verification events. A **bare mapping MUST be accepted as a one-element list** (athenaeum normalizes on read). Independent of `generated.at`. **Athenaeum writer:** only the automatic post-curation verification (actor `athenaeum-curator/<version>`) appends entries — concepts are born unverified and `edit_concept` never touches `verified`; human review happens outside the loop (`human:` verifier). |

**Trust tiers**, derived from `verified` (advisory, never access control):

- no `verified` key, or no entry with a parseable `{ by, ... }` mapping -> **unverified**
- `verified` by non-`human:` actors only -> **machine-confirmed**
- any `human:` verifier -> **human-reviewed**

### Lifecycle family (§5.4, §5.5)

| Field | Notes |
|---|---|
| `status` | `draft` \| `stable` \| `deprecated`. Absent means `stable`. `deprecated` = kept for links and history, no longer current. |
| `stale_after` | Absolute date `YYYY-MM-DD`; the concept is stale when `today >= stale_after` (stale on the day itself). |

### Provenance family (§5.1)

| Field | Notes |
|---|---|
| `sources` | List of entries. `resource` is REQUIRED within each entry (URL, bundle-relative path, path into `references/`, or a scope descriptor). `id` is a stable key for per-claim attribution (SHOULD be present when the body cites the source). Optional credibility signals per entry: `author` (actor), `usage_count`, `last_modified` (`YYYY-MM-DD`). |
| `usage_window` | `{ from, to }` date range, sibling of `sources`, framing every `usage_count`. A single entry may carry its own override. |

**Per-claim attribution:** a markdown footnote whose label is a `sources[].id`:

```markdown
The table is sharded daily.[^ga4-schema]

[^ga4-schema]: GA4 BigQuery Export schema
```

Consumers resolve attribution through the matching `sources` entry, not by parsing footnote prose. Keyed (not positional) labels survive reordering by rewriting agents.

### Computation family (§10) — only for `type: Attested Computation`

| Field | Notes |
|---|---|
| `runtime` | REQUIRED for this type (e.g. `bigquery`, `postgres`, `dbt`, `python`). Defines what `parameters` mean. |
| `parameters` | List of `{ name, type, required }`. |
| `computation` | Optional path to a file holding the computation; absent means the body `# Computation` fence is the computation. |
| `executor` | `resource` (run instructions/code) + `receipt` (fields a run must return). |
| `attester` | `resource`: deterministic (no-LLM) code that inspects a receipt and returns a verdict. |

**Athenaeum v1 execution semantics** (sandbox): runtimes `postgres` and `sqlite` only — every other runtime is refused with an explicit v1 error; the body `# Computation` fenced SQL block only (a frontmatter `computation` path is refused); a single read-only statement (first keyword `SELECT` or `WITH`, at most one trailing semicolon — a semicolon inside a string literal is also rejected in v1); placeholder conventions `%(name)s` (postgres) / `:name` (sqlite), always driver-bound; connection-level read-only enforcement (postgres READ ONLY transaction + server-side statement timeout; sqlite `mode=ro` + `PRAGMA query_only` + interrupt timeout) plus a fixed row cap (500). Execution requires the admin toggle (`computation_execution_enabled`, default off) and runs against admin-managed shared connections (Fernet-encrypted credentials, decrypted only in process memory). The receipt `{ runtime, connection_id, columns, rows, row_count, truncated, duration_ms, executed_at }` is returned to the caller only — v1 never writes receipts back into frontmatter.

### Extension rule (§4.1)

Producers MAY add any other keys. Consumers MUST NOT reject unknown keys and SHOULD preserve them on round-trip. **Consequence for athenaeum:** every edit path that parses and rewrites frontmatter (`edit_concept`) must preserve unknown keys and the body byte-for-byte.

## 4. Actor convention (§7)

Identity fields (`generated.by`, `verified[].by`) use one convention:

- `<producer>/<version>` — agents and tools. **Athenaeum writes `athenaeum-librarian/<version>`.**
- `human:<id>` — a person. Trust-tier classification keys off the `human:` prefix, so it MUST be used for human-authored or human-confirmed content.
- `process:<id>` — an automated process (e.g. `process:finance-nightly`).

## 5. Links (§6)

- **Absolute (bundle-relative), recommended:** `[customers table](/tables/customers.md)` — stable when documents move within their subdirectory. **Athenaeum always writes this form**, which makes `move_concept` link-rewriting trivial.
- **Relative:** `[neighboring concept](./other.md)`.
- A link asserts an untyped, directed relationship; the kind is conveyed by surrounding prose.
- Consumers MUST tolerate broken links — they may represent not-yet-written knowledge. In athenaeum, broken links are validator **warnings, never errors** (`link_check` reports them to the librarian).
- Path-valued fields (`resource`, `sources[].resource`, `computation`, `executor.resource`, `attester.resource`) accept absolute URLs, bundle-relative paths, or relative paths (§6.2).
- `references/` is a conventional subdirectory mirroring external material/code as first-class concepts (§6.3) — a naming convention, not a requirement.

## 6. index.md format (§8)

- MAY appear in any directory; missing `index.md` is never an error (consumers may synthesize one).
- **No frontmatter**, with one exception: the bundle-root `index.md` MAY carry `okf_version: "0.2"` — the only place frontmatter is permitted in an index file (§12).
- Body: `# Section` headings grouping entries of the form `* [Title](relative-url) - description`; subdirectory entries use the `subdir/` form. Entries SHOULD carry the linked concept's `description`.

Athenaeum's generator is deterministic (children scan, stable alphabetical ordering) and treats index files as a pure function of the tree — never hand-edited, regenerated on every relevant write and by the startup reconciliation pass.

## 7. log.md format (§9)

Flat, date-grouped, newest-first:

```markdown
# Directory Update Log

## 2026-07-27
* **Creation**: Established the [Customer Orders](/tables/customer-orders.md) concept (requested by agent:coding-assistant).

## 2026-07-20
* **Initialization**: Created foundational directory structure.
```

- Date headings MUST be ISO `YYYY-MM-DD`.
- The leading bold word (`**Creation**`, `**Update**`, `**Deprecation**`, `**Move**`, `**Deletion**`, `**Initialization**`, `**Verification**`) is a convention, not a requirement.
- Athenaeum keeps a **single root log.md**; entries carry the optional `(requested by agent:<label>)` suffix for per-agent attribution. `log.md` (spec-level, human/agent-readable history) and the bundle's git history (system-level audit/revert/reset, 0.22.0) coexist — they answer different questions; commit messages mirror the log entry text.
- **Implementation deviation:** athenaeum's `log.md` is chronological (oldest first, newest appended at EOF) so appends stay O(1) — a deliberate deviation from the newest-first layout shown above, which is a spec §9 convention, not a conformance rule (the validator never enforced entry order and §8 does not cover it). Legacy newest-first files are flipped once on the first append; readers still get newest-first via `recent_entries`.

## 8. Conformance rules (§11)

A bundle is conformant with OKF v0.2 if:

1. Every non-reserved `.md` file has a parseable YAML frontmatter block.
2. Every frontmatter block has a non-empty `type` field.
3. Reserved filenames follow §8/§9 structure when present.

Consumers MUST NOT reject a bundle for: missing optional fields, unknown `type` values, unknown frontmatter keys, broken links, or missing `index.md` files. Consumers MUST treat a bare `verified` mapping as a one-element list and MUST NOT reject a concept for missing any optional family.

Athenaeum's validator maps these to **6 error classes** (the MUSTs plus REQUIRED-within fields and enum checks) and **9 warning classes** (conventions) — see `architecture.md` §5.

## 9. Complete example: a concept as athenaeum writes it

`projects/athenaeum/decision-git-versioning.md`, created by the librarian on behalf of an external agent (token label `coding-assistant`):

```markdown
---
type: Decision
title: "Decision: git history for library versioning"
description: Why athenaeum versions user libraries with a real git history instead of shadow-copy snapshots.
tags: [athenaeum, versioning, architecture]
status: stable
stale_after: 2027-07-27
generated: { by: athenaeum-librarian/0.1.0, at: 2026-07-27T20:54:42Z }
sources:
  - id: mvp-plan
    resource: /decisions/mvp-plan.md
    title: ATHENAEUM_MVP implementation plan
---

# Context

Athenaeum needed undo, an audit trail, and WebUI diffs for the per-user
OKF bundles. Shadow-copy snapshots under `.athenaeum/versions/` provided
that through 0.21.0, at the cost of storing every pre-image twice.

# Decision

Keep the full history in a real git repository inside each bundle
(0.22.0): one auto-commit per compound write with a deterministic
message, per-commit diffs, revert, and an append-only reset — replacing
the snapshot store, per the [implementation plan](/decisions/mvp-plan.md).[^mvp-plan]

# Consequences

Diffs, revert, and reset delegate to the git binary — no double storage,
no difflib. Pre-0.22.0 `.athenaeum/versions/` data stays on disk, inert
and gitignored. See also [compound writes](/architecture/compound-writes.md).

[^mvp-plan]: ATHENAEUM_MVP implementation plan
```

Notes on the example:

- `generated.by` follows the tool actor convention: `athenaeum-librarian/<version>` — never the requesting agent's identity. The requesting HUMAN is recorded durably in the concept itself: `generated.requested_by` (`human:<username>`, the local Athenaeum account that issued the token) + `generated.via` (`mcp_chat`) — that pair is the authoritative in-concept provenance. The `log.md` `(requested by agent:<label>)` suffix remains the audit record of the requesting INTEGRATION (the MCP token label). Neither replaces the other.
- No `verified` key, so this concept's trust tier is **unverified**; adding `verified: { by: human:someone, at: ... }` would make it human-reviewed.
- Links are absolute bundle-relative; the footnote label joins to `sources[].id`.
- Unknown extra keys would be preserved byte-intact by any athenaeum edit.
