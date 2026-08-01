# Athenaeum — Project Definition

> **Provenance (historical):** this document was compiled from the original project idea (a pre-repo draft note, `start.md`), the MVP research analysis, and the approved MVP plan (decisions and contracts); where this document and the plan disagreed, the plan won. Those working files were ephemeral and were deleted before the initial push — this document is now the authoritative record.

## What Athenaeum is

Athenaeum is a long-term knowledge and memory container for AI agents, built on the [Open Knowledge Format (OKF) v0.2](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md). Knowledge lives in plain directories of markdown files with YAML frontmatter (an OKF *bundle*) — human-readable, diffable, portable, with no required tooling to inspect.

The defining architectural idea: **external agents never touch the knowledge directly.** They express *intent* ("remember this", "what do you know about X", "what's in the library?") over the Model Context Protocol (MCP) to a **librarian agent** that lives inside the system. The librarian — an LLM agent that knows its own library best — does all the heavy lifting: retrieval, placement, frontmatter, linking, index and log maintenance, and OKF conformance enforcement. External agents need to know nothing about file layouts, frontmatter fields, or index mechanics.

Athenaeum is **multi-user**: every user gets their own library *and* their own librarian, with strict isolation between users.

## Core concepts

- **OKF bundle per user** — one conformant OKF v0.2 bundle directory per user, stored under `data/users/<user_id>/library/`. See `reference.md` for the format and `architecture.md` for the storage model.
- **Librarian agent** — a per-user, in-process LLM agent that is the *only writer* to the bundle. It exposes 6 intent-based MCP tools to the outside and drives a 9-tool internal view over the library backend.
- **MCP server** — Streamable HTTP transport, bearer-token auth, mounted in the same process as the WebUI.
- **WebUI** — server-rendered administration and inspection UI: librarian configuration (named provider connections with a default, per-agent connection select + model and prompt), document tree and relations graph, log viewer, MCP token management.

## Phase 1 scope

Phase 1 ships all of the following together, in a single Python process:

- **MCP server** (FastMCP 3.x, Streamable HTTP) with exactly 6 external tools: `request_knowledge`, `store_knowledge`, `update_knowledge`, `library_status`, `library_maintain`, `library_curate` (0.5.0).
- **Librarian agent** — per-user, lazily created, idle-evicted; hand-rolled tool-calling loop over a pluggable `LLMProvider` (adapters: `openai`, `anthropic`, `gemini`, plus `openrouter` and `openai-compatible` which both reuse the OpenAI adapter).
- **WebUI** (FastAPI + Jinja2 + htmx + Alpine.js + 3d-force-graph): provider-connection/agent/library config screens (named connections, one default, per-agent connection select), document tree, 3D relations universe, activity/log viewer, token management, admin user management.
- **Configuration** — per-user provider connections and librarian config plus accounts in a single SQLite database (`data/app.db`); server settings via environment variables.
- **Multi-user** — account system (first-run bootstrap + admin-created users, no self-registration), per-user libraries, per-user MCP bearer tokens, hard filesystem isolation.
- **Scheduled curation** (0.11.0) — optional per-user nightly `library_maintain` + `library_curate` run at a fixed UTC time, configured in the WebUI (curator tab, per-user opt-out), journaled in Activity.

### Locked user decisions (non-negotiable for phase 1)

1. **OKF-native retrieval first.** Retrieval follows OKF's progressive-disclosure model: `index.md` at every directory, the librarian reads indexes and follows links; a frontmatter/metadata scan covers exact field/value lookups. An optional, additive embedding subsystem (0.12.0) layers semantic similarity search on top without changing the storage or retrieval model — with embeddings off, behavior is unchanged.
2. **Plain-directory storage with git-like structure.** Libraries are ordinary directories (no database, no object store). Versioning uses **shadow-copy snapshots** under `.athenaeum/versions/` — per-operation pre-image copies enabling rollback, audit, and diffs. Real git is a documented upgrade path, not a phase-1 dependency.
3. **Strict per-user isolation.** Each user has their own library and their own librarian. All filesystem access goes through a path-resolution helper rooted at the user's directory that rejects `..` escapes, absolute paths, and symlink escapes. User IDs in paths are opaque UUIDs, never usernames.

### Non-goals for phase 1

Explicitly deferred (full list with rationale and upgrade paths in `roadmap.md`, phase sections and "Later / exploratory"):

- OAuth 2.1 + PKCE for the MCP endpoint (bearer tokens ship first; the auth seam is designed so OAuth can be added without touching tool handlers).
- Real git backend for versioning (shadow snapshots instead; lossless upgrade path).
- Per-user OS processes/subprocesses (one shared process with per-user in-process librarians; the manager interface allows swapping later).
- Multi-worker deployment (single-worker remains the deployment default). No longer fully deferred: the in-process concurrency foundation — agent run gate, per-root write lock, SQLite WAL + busy timeout, DB-backed embed-reconcile claims, middleware-composed seeds, optional stateless MCP HTTP — is implemented (`architecture.md` §7); the deployment itself and its remaining pieces are tracked in `roadmap.md`.
- Streaming LLM responses.
- Migration to the official `mcp` Python SDK v2 (staying on FastMCP 3.x; re-evaluate when v2 is stable).
- SSE transport (deprecated by the MCP spec — must **not** be implemented).
- Open self-registration of accounts.

## Target users and use cases

- **Personal agent memory** — an individual runs one or more AI agents (coding assistants, personal copilots) that accumulate durable knowledge: preferences, project context, decisions, learned procedures. Agents store and recall through the librarian instead of ad-hoc files or opaque vector stores, and the owner can inspect, diff, and roll back everything in the WebUI.
- **Team knowledge bases** — a small team runs a shared athenaeum instance; each member (or each team agent) gets an isolated library with its own librarian, curated over time into a navigable, trust-annotated OKF corpus. Trust tiers (unverified / machine-confirmed / human-reviewed) and staleness markers make agent-written knowledge auditable.
- **Agent interoperability** — because the external surface is plain MCP with intent-based tools, any MCP-capable agent can use athenaeum without learning OKF; because the storage is plain OKF, the knowledge outlives any single tool and can be read with `cat`.

## Success criteria for phase 1

- An external MCP client can authenticate with a bearer token and use all 6 tools against a configured librarian.
- Every librarian write leaves the bundle OKF-conformant: concept file + regenerated `index.md` + `log.md` entry + shadow-copy snapshot, validated by the built-in validator.
- A user can configure their librarian (named provider connections, API keys, per-agent connection select + model and prompt, library retention), browse their tree and graph, view logs and versions, and manage MCP tokens entirely from the WebUI.
- No code path lets one user's session or token reach another user's library (enforced by tests).
