# Athenaeum — Roadmap

> Forward-looking phase plan; lists only work that is still open. A future
> session should be able to pick up work from this document alone. Companion
> to `project-definition.md` (what and why), `architecture.md` (decisions),
> and `prime-directives.md` (non-negotiable rules).

## Phase 6 — LLM fallback provider

**Goal:** A secondary LLM provider takes over when the primary fails,
configured per user.

The structural seam already exists: named provider connections with stable
ids (`provider_configs`), one default per user, per-agent references
(`librarian_connection_id` / `curator_connection_id`), and the `LLMProvider`
protocol + provider factory.

Open items:

- **Per-user fallback configuration** (deferred from understory as
  `LLM_FALLBACK_*`): designate a secondary connection as the fallback.
  **PD-1 applies**: configuration lives in the Admin WebUI + database, not
  in environment variables.
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
  library browsing (tree, graph, activity) and own-token management;
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

## Multi-worker deployment

The concurrency foundation is implemented (details in `architecture.md` §7);
single-worker remains the deployment default. Still deferred, each needing an
explicit go decision:

- Multi-worker deployment itself.
- Cross-process DB-mutex write lock.
- External MCP event store (or sticky sessions as the alternative).
- Sharing the per-worker ActivityRegistry / EmbedStatusRegistry in-flight
  state.

## Later / exploratory

These items are deliberately unphased; each needs an explicit go decision and
its own Research -> Planning cycle before any work starts.

- **WebUI chat with the librarian**: direct chat with
  the per-user librarian from the browser, with tool calls rendered inline so
  the owner can watch it work. Useful for testing prompt/config changes.
- **`sqlite-vec` ANN index for embeddings** (conditional upgrade path): the
  embedding subsystem uses stdlib cosine top-k over float32 BLOBs (`top_k`
  loads all vectors into RAM) — correct at the <1000-concept scale. If
  libraries grow past ~10k concepts, move to `sqlite-vec` behind the same
  `EmbeddingService` seam: an HNSW or flat index queried in SQL
  (`ORDER BY vec_distance_cosine(...) LIMIT k`) keeps RAM flat and scaling
  linear. The table layout already carries model/dims per row for a
  mixed-state migration.
- **Per-user subprocess isolation** (deferred). One shared process with
  in-process per-user librarians today; `LibrarianManager` is the swap seam.
  Motivation would be blast-radius containment of a misbehaving librarian or
  LLM-loop runaway.
- **Streaming LLM responses** (deferred). Not needed for intent-based
  request/response tools; the `LLMProvider` protocol can grow a streaming
  variant if a future tool wants progressive output.
- **Tool-description refresh via `tools/list_changed`** after writes in a
  long-lived MCP session, push a refreshed seed so the client session sees
  its own writes without reconnecting.
- **Ingest / Inbox with batched curation**: today every input immediately
  triggers a librarian LLM run — costly and latent for quick captures made
  outside the chat (reading, meetings, ideas on the go). Proposal: a
  token-protected capture endpoint (and/or WebUI quick-capture) writes raw
  items into a structured inbox with no LLM involved; the curator processes
  the backlog in one batched run, triggered by a threshold (X pending items)
  in addition to the nightly schedule. An e-mail adapter (e.g.
  `save@athenaeum.local`) is a later optional channel on top of the same
  ingest seam, not the entry point. Open design question: inbox as a DB
  table vs. a markdown folder inside the library (visibility in the WebUI,
  git versioning, backup).
- **Human verification path for trust tiers**: machine verification exists —
  `verify_concept` (backend.py) is the sole writer of `verified`, and each
  curator run machine-verifies the concepts it repaired (verifier label
  `athenaeum-curator/<version>`). Open: a dedicated verify path (tool or
  WebUI action) for humans; whether the nightly curate run should gain an
  opt-in verification step; and what verification actually asserts (link
  validity, source freshness, owner sign-off).
- **TOTP second factor for WebUI login**: login is currently password-only
  (signed-cookie session with password marker). Add per-user TOTP: secret in
  the database, enrollment flow with an otpauth QR code, a second login
  step, and a decision on recovery codes. TOTP itself is stdlib-feasible
  (`hmac`/`hashlib`); QR rendering is the only new dependency question.
- **Configurable reasoning effort**: `LLMConfig` today carries only
  `temperature` / `max_tokens`; the three providers map them per API. Add a
  per-agent reasoning-effort setting (PD-1: Admin WebUI + database) and map
  it per provider (OpenAI `reasoning_effort`, Anthropic
  `thinking.budget_tokens`, Gemini `thinkingBudget`). Open design question:
  unified levels (low/medium/high) vs. provider-native token budgets.
