# Athenaeum — Roadmap

> Forward-looking phase plan. A future session should be able to pick up work
> from this document alone. Companion to `project-definition.md` (what and
> why), `architecture.md` (decisions), and
> `prime-directives.md` (non-negotiable rules). Deferred items live in this
> document (phase sections and "Later / exploratory"); they originate from
> the deferred-items section (§7) of the MVP-phase plan (a working file
> deleted before the initial push) and the understory comparison.

## Phase 6 — LLM fallback provider

**Goal:** A secondary LLM provider takes over when the primary fails,
configured per user.

> **Structural prerequisite shipped in 0.8.0**: named provider connections
> with stable ids (`provider_configs`), one default per user, and per-agent
> references (`librarian_connection_id` / `curator_connection_id`) already
> exist. What remains is the failure semantics and switching logic below.

Items:

- **Per-user fallback configuration** (deferred from understory as
  `LLM_FALLBACK_*`): designate a secondary connection as the fallback.
  **PD-1 applies**: configuration lives in the Admin WebUI + database, not
  in environment variables. The `LLMProvider` protocol and provider factory
  are the seam.
- **Failure semantics**: define what triggers fallback (transport errors,
  rate limits, timeouts) and what does not (model refusals, malformed tool
  calls), plus surfacing fallback events in the WebUI activity view.

## Phase 7 — MCP auth upgrade

**Goal:** Replace shared bearer tokens with spec-grade OAuth where
deployments need it, without touching tool handlers.

Candidate items:

- **OAuth 2.1 + PKCE for the MCP endpoint** (deferred). The seam is the
  single function `resolve_user_id(request) -> str` in the auth middleware;
  bearer tokens remain as the fallback/simpler mode.
- **`mcp` SDK v2 migration watch** (deferred). No action item — re-evaluate
  the official SDK v2 vs. FastMCP 3.x when v2 is stable; FastMCP's
  auth/middleware may still justify staying.

**Decision needed:**

- **OAuth timing**: implement in phase 7, or defer until a concrete
  multi-user / internet-facing deployment requires it. Bearer tokens are
  sufficient for the current single-host, admin-provisioned model.

## Phase 8 — Admin config mode

**Goal:** A deployment mode for centrally managed instances: an
administrative user presets providers, models, and agent configuration for
everyone; a regular user manages only their own library and MCP tokens.

Items:

- **Mode flag** (PD-1: Admin WebUI + database, not env): a server-wide
  setting toggling admin config mode.
- **Global configuration scope**: connections (`provider_configs`), per-agent
  bindings and models (librarian / curator / embeddings), and agent prompt
  addenda are defined once by an admin and apply to all users; regular users
  cannot create, edit, or delete connections or agent settings.
- **Restricted self-service**: for non-admin users the WebUI reduces to
  library browsing (tree, graph, traces, activity) and own-token management;
  config screens are hidden and their write routes reject non-admin requests
  server-side (403) — hiding the UI alone is not the enforcement.
- **Resolution change**: the per-user config resolution (`LibrarianManager`)
  reads the admin-defined global rows while the mode is on, ignoring any
  per-user rows.

**Decision needed:**

- **Storage model for the global preset**: dedicated global rows (reserved
  owner scope) vs. marking existing rows as admin-managed.
- **Dormancy semantics**: per-user configs stay dormant and re-activate when
  the mode is switched off, vs. hard-overridden.

## Multi-worker foundation

The concurrency foundation for running several Athenaeum workers against one
data volume is implemented (details in `architecture.md` §7); single-worker
remains the deployment default.

- `RunGate` keyed `(user_id, agent_kind)`: at most one librarian and one
  curator run per user; MCP calls get a retryable busy error, the scheduler
  skips due runs.
- Per-root write lock serializes compound writes across backend instances;
  SQLite runs WAL + `busy_timeout=5000`; the embed reconcile is claimed in
  the DB.
- Seeds are composed per request by middleware (the registry is never
  mutated) and self-validate via the log.md mtime; the `mcp_stateless_http`
  admin setting selects the stateless MCP transport (takes effect on
  restart).

Still deferred (each needs an explicit go decision): multi-worker deployment
itself, the cross-process DB-mutex write lock, an external MCP event store
(or sticky sessions as the alternative), and sharing the per-worker
ActivityRegistry / EmbedStatusRegistry in-flight state.

## Later / exploratory

These items are deliberately unphased; each needs an explicit go decision and
its own Research -> Planning cycle before any work starts.

- **WebUI chat with the librarian** (understory feature): direct chat with
  the per-user librarian from the browser, with tool calls rendered inline so
  the owner can watch it work. Useful for testing prompt/config changes.
- **`sqlite-vec` ANN index for embeddings** (conditional upgrade path): the
  embedding subsystem (0.12.0) uses stdlib cosine top-k over float32 BLOBs —
  correct at the <1000-concept scale. If libraries grow past ~10k concepts,
  move to `sqlite-vec` behind the same `EmbeddingService` seam; the table
  layout already carries model/dims per row for a mixed-state migration.
- **Real git backend** for versioning (deferred). Shadow-copy snapshots
  already cover undo/audit/diff; upgrade is lossless (`git init &&
  git add -A && git commit` on the plain directory). Only worth doing if
  external git tooling integration becomes a real requirement.
- **Per-user subprocess isolation** (deferred). One shared process with
  in-process per-user librarians today; `LibrarianManager` is the swap seam.
  Motivation would be blast-radius containment of a misbehaving librarian or
  LLM-loop runaway.
- **Streaming LLM responses** (deferred). Not needed for intent-based
  request/response tools; the `LLMProvider` protocol can grow a streaming
  variant if a future tool wants progressive output.
- **Tool-description refresh via `tools/list_changed`** (understory
  feature): after writes in a long-lived MCP session, push a refreshed seed
  so the client session sees its own writes without reconnecting.
- **Snapshot pruning policy beyond `snapshot_keep`** — the hook
  (`VersionStore.prune(keep_last)`) already exists; only extend if keep-all
  proves wasteful in practice.
- **Verification workflow for trust tiers** (gap found 2026-07-30): trust
  tiers are derived from the `verified` frontmatter list, but nothing in the
  product ever sets it — `edit_concept` explicitly refuses to touch
  `verified` (backend.py), and neither `library_curate` nor
  `library_maintain` perform content verification. Every concept stays
  `unverified` unless someone hand-edits the markdown. Open design
  questions: a dedicated verify path (tool or WebUI action) for humans; a
  machine verifier actor (e.g. `process:nightly-validator`) with defined
  checks; whether the nightly curate run should gain an opt-in verification
  step; and what verification actually asserts (link validity, source
  freshness, owner sign-off).


beim anlegen von neuem wissen auf wiedersprüche prüfen
löschen von wissen erlauben
rohdaten store_knowledge anfrage mit speichern um beim curatieren fehler ausbesern zu können
bilder in markdown unterstützen (bei store_knowledge mit übergebbar)

ui tree rework, bessere usability und möglichkeit für den user dateien anzulegen, editieren und löschen

agents rework, librarian und curator split, als eigenstädige agents


reasoning effort einstellbar