# Athenaeum — Architecture

> Companion to `project-definition.md` (what and why) and `reference.md` (the OKF format). All decisions here originate from the MVP planning phase — an analysis document recording the agreed architecture (its §0) and an approved plan with resolved decisions and pinned contracts; those working files were deleted before the initial push. This document records decisions; it does not introduce new ones.

## 1. System overview

Athenaeum phase 1 is **one Python process**. A single FastAPI application hosts both the MCP server and the WebUI:

- **MCP server** — FastMCP 3.x (`fastmcp>=3.4,<4`), **Streamable HTTP** transport, served at `/mcp`: the ASGI app is a root catch-all mount bounded by a guard (`_MCPCatchAll`) that passes only `/mcp` paths (and lifespan) through and answers everything else with a plain 404, so route ordering is not load-bearing for the error shape. The FastMCP session manager must be driven by the FastAPI `lifespan=` context manager (documented integration caveat — do not use startup/shutdown decorators for it). stdio is out (cannot serve remote agents); SSE is deprecated by the MCP spec and must not be implemented.
- **WebUI** — FastAPI + Jinja2 templates + htmx (+ Alpine.js for small interactions), with **3d-force-graph** (vendored under `webui/static/vendor`, WebGL/three.js) for the 3D relations universe. No Node toolchain; htmx loads from CDN, the graph stack is vendored.
- **Storage** — SQLite (`data/app.db`) for users, per-user librarian configs, and hashed MCP tokens; plain directories for the OKF bundles.
- **LLM access** — a thin `LLMProvider` protocol with hand-rolled httpx adapters (`openai`, `anthropic`, `gemini`, plus `openrouter` and `openai-compatible` which both reuse the OpenAI adapter — `openrouter` defaults its base URL to `https://openrouter.ai/api/v1`, `openai-compatible` requires an explicit base URL). No LangChain, no LiteLLM, no streaming.
- **Library engine** — `LibraryBackend`: the only writer to the bundle, implementing the 9-operation OKF catalog (plus a semantic-search extension, 0.12.0) with compound writes and shadow-copy snapshots.

### Component diagram

```
                        external agent
                              |
                 MCP / Streamable HTTP
                 Authorization: Bearer <token>
                              |
        +---------------------v----------------------+
        |              FastAPI app (uvicorn)          |
        |                                             |
        |  /mcp        FastMCP app                    |
        |               └ auth middleware:            |
        |                 resolve_user_id(request)    |
        |                 (SHA-256 token -> user_id   |
        |                  + token label)             |
        |               └ seed middleware (tool desc) |
        |               └ activity middleware:        |
        |                 mints request/trace id,     |
        |                 journals calls to app.db    |
        |               └ 6 external tools            |
        |                                             |
        |  WebUI pages FastAPI + Jinja2 + htmx        |
        |               (SessionMiddleware cookie)    |
        |  /api/graph  nodes/folders/edges JSON (3D)  |
        |  /library/traces  trace list + graph replay |
        |  /activity  request journal + in-flight     |
        |  /config/provider  connections CRUD + test  |
        |  /config/agents/*  per-agent conn + model   |
        |                                             |
        |  CurateScheduler (0.11.0): asyncio task in  |
        |  the app lifespan; nightly maintain+curate  |
        |  per user, single worker, minute tick       |
        |                                             |
         |  SQLite data/app.db                         |
         |   users / librarian_configs / mcp_tokens    |
         |   provider_configs (named connections)      |
         |   embeddings (semantic vectors)             |
         |   activity (MCP request journal)            |
         |   app_settings / embed_reconcile_claims     |
        +------------------|--------------------------+
                           |
                 LibrarianManager.get(user_id)
                 (lazy init, cached, idle-evict)
                           |
                 +---------v----------+
                 |  Librarian agent   |     LLMProvider protocol
                 |  loop (max N iter) | --> openai / anthropic / gemini
                 |  + system prompt   |     (hand-rolled httpx adapters)
                 +---------|----------+
                            |  10 LLM-facing tools (curated view)
                 +---------v----------+
                 |  LibraryBackend    |  compound writes:
                 |  (9-op OKF catalog)|  snapshot -> file -> index -> log
                 +---------|----------+
                           |
            data/users/<user_id>/library/     (OKF v0.2 bundle)
              index.md, log.md, concepts...
              .athenaeum/versions/<NNNNNN>/   (shadow-copy snapshots)
              .traces/<request_id>.json       (query-path traces, 0.4.0)
```

## 2. Request flows

### MCP request (external agent)

1. Client calls a tool over Streamable HTTP at `/mcp` with `Authorization: Bearer <token>`.
2. Auth middleware (`resolve_user_id(request) -> str`, the single seam for later OAuth): SHA-256-hash the token, look it up in `mcp_tokens`, reject if unknown or `revoked_at` set; attach `user_id` and `token_label` to the request context (the `athenaeum/identity.py` ContextVar — the shared home both the transport and the activity middleware use, so neither imports the other). `user_id` is **never** accepted from request parameters. The `last_used_at` write is coalesced per token (at most one UPDATE per 5 minutes) so pings and tools/list do not hit the WAL database per message; the lookup itself runs off the event loop (§7).
3. The activity middleware (innermost, added after auth/seed so it runs inside the identity context) mints the request/trace id, registers the call as in-flight, and — around the handler — measures duration and journals the outcome (tool, arguments truncated to 2 KB, duration, outcome/error, LLM iterations and token usage) into the `activity` table. Auth failures raised upstream are never journaled (accepted scope decision). In-flight state lives in a process-local `ActivityRegistry` on `app.state`.
4. The tool handler fetches that user's librarian via `LibrarianManager.get(user_id)` (lazy init from the DB config row, cached, idle-evicted after 30 min) and opens a `TraceSession` (ContextVar, per request — never stored on the librarian instance).
5. The librarian runs its agent loop under the per-user run gate (§7 — a second same-kind run for the same user is rejected with a retryable busy error): system prompt + user intent -> LLM -> tool calls against the internal 9-tool view -> `LibraryBackend` -> repeat until a final text response or the `max_iterations` cap (default 10; on cap exhaustion, request a final answer with no tools). Every internal dispatch runs its backend call on a worker thread (`asyncio.to_thread`, §7), so library I/O never blocks the event loop. Each internal dispatch is recorded into the trace session (operation, summarized arguments/result, duration, errors); LLM token usage is summed across completions.
6. Backend writes are compound (see §4); `agent_label` from the token is threaded into log entries as `(requested by agent:<label>)`.
7. The librarian post-processes affected concepts (trust tier from `verified`/`human:` rule, staleness from `stale_after`) and returns the tool's result shape.
8. On handler exit the trace session closes: the trace file is written under `.traces/` only when events or LLM data exist — `library_status` (no LLM) and a healthy `library_maintain` / well-organized `library_curate` no-op produce no trace file, though the activity journal records them all.
9. An unconfigured librarian (no LLM config row) yields a clear MCP error.

### Scheduled curation (nightly, 0.11.0)

1. A `CurateScheduler` asyncio task starts in the app lifespan (chained inside the FastMCP lifespan block, single worker) and wakes once per minute; shutdown cancels it cleanly.
2. Each tick re-reads the schedule fields from `librarian_configs` (`curate_schedule_enabled`, `curate_schedule_time`, UTC `HH:MM`). The first tick only records the baseline minute — missed runs are never caught up. A user is due when their scheduled absolute minute falls in the window since the previous tick (current or previous day), firing at most once per day even after a multi-day stall; a user whose curator run gate is held (§7, e.g. by an MCP-initiated run) is skipped.
3. Per due user (unconfigured users skipped silently): `library_maintain` then `library_curate` via the same librarian handlers as the MCP tools, each with its own minted `RequestTelemetry` + `TraceSession` (`agent_label` = "scheduler") and an in-flight `ActivityRegistry` entry. A maintain failure never blocks the curate run. Due users run sequentially inside the tick (single-worker model), each bounded by a per-run timeout (default 45 min — above the ~40 min worst case of two tools × `max_iterations` × 120 s provider timeout); a timed-out run is cancelled like a kill (the in-flight tool journals no row) and the tick proceeds to the next user.
4. Each tool journals one activity row (`token_label` = "scheduler", outcome, duration, token usage) and prunes per `activity_keep`; no-op runs journal but write no trace file. A completed curate run (success or no-op) re-baselines `curate_last_run_at`; failed runs do not.
5. When either tool reported actions, the user's cached seed is invalidated so the next tools/list response carries a fresh seed.
6. The WebUI (curator tab, "Scheduled curation" card) edits the schedule; the time input displays browser-local time via client-side conversion and posts UTC (UTC verbatim without JS).

### WebUI session

1. First run (empty `users` table): a one-time setup page creates the owner account (admin). Admins create further users in the WebUI. No self-registration.
2. Login sets a signed-cookie session (Starlette `SessionMiddleware`, secret from `ATHENAEUM_SECRET_KEY`; cookie flags pinned: `SameSite=Lax`, no `Secure` — the documented deployment is plain-HTTP self-hosting). argon2 password hashing. Passwords must be at least 12 characters (enforced by every password-accepting handler). Login attempts are throttled per account and per client IP (`login_attempts` table): after 5 consecutive failures the account locks with exponential backoff (30 s doubling, capped at 1 h), reset on a successful login. First-run setup creates the admin atomically (`INSERT ... WHERE NOT EXISTS` in one transaction), so concurrent setup POSTs cannot create two admins. Every mutating form POST carries a per-session CSRF token (hidden input minted per session, validated by a router-level dependency; CS-8); the MCP endpoint is bearer-token authenticated and exempt.
3. Pages are server-rendered; htmx handles lazy tree expansion and log polling; Alpine.js covers small interactions. Every page and API route resolves the current user from the session and only ever touches that user's data — cross-user paths return 404.
4. `/api/graph` scans markdown links across the user's bundle into nodes/folders/edges JSON for the 3D relations universe (`color` from trust/staleness, `group` from `type`, `folder`/`depth`/`kind` from the path, tooltip from tags); the WebUI renders it with the vendored 3d-force-graph (galaxies = depth-1 folders, systems = depth-2, planets = concepts, moons = depth-3+).
5. Config screens write to `provider_configs` (named connections, 0.8.0) and to the per-agent binding columns on `librarian_configs` (`librarian_connection_id`, `curator_connection_id`, models, addenda); LLM API keys are Fernet-encrypted at rest (key derived from `ATHENAEUM_SECRET_KEY`) and never rendered back into HTML.

## 3. Pinned interface boundaries

Two contracts are pinned; parallel work streams build against them.

### 3.1 External MCP surface — 6 intent-based tools

External agents express intent; paths, frontmatter, and index mechanics stay internal. Explicitly **not** exposed: raw file read/write, frontmatter editing, `log.md` access. `browse_library` was removed in 0.3.0 — orientation is the librarian's job (`request_knowledge` plus the per-user seed in the seeded tool descriptions).

- `request_knowledge(query, context?) -> {answer, concepts: [{id, title, type, trust_tier, stale}], follow_ups?}` — unchanged; also covers "what is in the library?" orientation. Trust tier (unverified / machine-confirmed / human-reviewed) and staleness are inlined so callers judge reliability without parsing frontmatter.
- `store_knowledge(content, kind_hint?, relates_to?, topic_hint?) -> {stored: [{id, title, action: created|updated|moved|deprecated|deleted, from_id?}], summary, partial?, links_after: {checked, unbacklinked, orphans, healthy}}` — NEW knowledge; the librarian decides placement/frontmatter/linking and may enrich an existing concept in place and back-link (those side-effect writes surface as `updated` entries). A `moved` entry carries `from_id` (the old concept id) so embedding sync deletes the stale old-path row. `topic_hint` (0.14.0) names the target topic area directly and overrides subject inference; the librarian still verifies it does not duplicate an existing area under a different name. A run with zero writes is a failure (strict no-write guard — a non-empty summary no longer rescues it); a provider failure mid-loop after writes landed returns a partial-success result (`partial: true`, landed writes in `stored`, interruption named in `summary`, embeddings still synced). `links_after` is a deterministic post-run link-graph scan of the written concepts: `unbacklinked` lists those that received no inbound link (`orphans` = also no outbound), and the summary ends with a deterministic "Post-run check" verdict (same pattern as maintain/curate).
- `update_knowledge(instruction) -> {stored: [{id, title, action: created|updated|moved|deprecated|deleted, from_id?}], summary, partial?, links_after: {checked, unbacklinked, orphans, healthy}}` — free-text change request (corrections, restructuring, deprecation); the librarian locates the target concepts itself. Same no-write, partial-success, and post-run link-check contract as `store_knowledge`.
- `library_status() -> {stats: {concepts, directories, versions, last_write}, health: {orphans, broken_links, warnings, errors}, healthy}` — deterministic, no LLM, works unconfigured.
- `library_maintain(instructions?) -> {actions: [{id, title, action}], summary, healthy}` — librarian-driven repair; deterministic no-op when healthy. `healthy` requires zero orphans and zero broken links, so a single stray unlinked concept makes the next maintain run a paid LLM run — deliberate (placing an orphan needs librarian judgment; a deterministic threshold would silently defer real repair). The summary ends with a deterministic "Post-run check" verdict from a post-run `status()` rescan, so fixed items are never narrated as remaining.
- `library_curate(instructions?) -> {actions: [{id, title, action}], summary, organized, findings, health_after: {healthy, orphans}}` — librarian-driven curation (0.5.0): a deterministic organization scan (`library/organize.py`) reports type-named top-level folders, oversized folders, thin concepts, near-duplicate candidates (title Jaccard >= 0.6; numbered title series — pairs whose only token difference is a pure number, like phase lessons or dotted version docs — are excluded, 0.11.1), and semantic duplicate candidates (embedding cosine over cached vectors, 0.12.0; per-model threshold with per-user override on the Embeddings tab, 0.13.0 — unknown models fall back to 0.85); both duplicate passes run off the event loop and stay exact over the whole library — the semantic pass is a vectorized matrix cosine when numpy is available (stdlib pairwise fallback otherwise), and the near-duplicate pass evaluates only inverted-index/size-bound candidate pairs instead of all pairs; the LLM acts only on the findings (move misplaced concepts, enrich stubs, merge duplicates enrich-then-deprecate). No-op without an LLM call when the library is well-organized; findings are recomputed over the whole library every run (an unaddressed finding is re-reported until actually fixed — `curate_last_run_at` is run-end bookkeeping, no longer a scoping filter); `findings` in the result is the POST-run report, the same epoch as `organized` and `health_after`, and the summary ends with a deterministic "Post-run check" verdict. The run uses the curator's per-agent connection binding + model (0.8.0; empty binding follows the default connection and the librarian model). `health_after` (0.11.1) is a post-run `status()` snapshot so curator-induced orphans are visible in the result.

Unexpected internal errors surface to the MCP client as a generic `ToolError` ("internal error; details were logged server-side") — the full exception goes to the server log and the trace file, never to the client.

### 3.2 Librarian internal view — 10 tools over `LibraryBackend`

`LibraryBackend` implements the full OKF operation catalog. The LLM sees a curated view of exactly 10 tools, dispatched 1:1:

| LLM tool | Purpose |
|---|---|
| `list_dir` | list a directory |
| `read_document` | read frontmatter + body |
| `search_metadata` | frontmatter scan over all concepts (exact field/value lookups) |
| `search_semantic` | embedding similarity search (0.12.0; ranked hits by meaning; unconfigured → recoverable error naming `search_metadata`, pipeline failure → filtered title/description substring matches marked `fallback: true`, empty when nothing matches — never the unfiltered library) |
| `write_concept` | create a concept |
| `edit_concept` | patch frontmatter (unknown keys and body preserved byte-for-byte) and/or replace body |
| `move_concept` | move + rewrite inbound links bundle-wide |
| `deprecate_concept` | set `status: deprecated` |
| `delete_concept` | remove + report inbound links |
| `link_check` | report broken bundle links (warning-level) |

**Not LLM-callable:** `regenerate_index`, `append_log` (automatic side effects of every write — this prevents the failure mode where the LLM forgets index/log maintenance), `init_bundle`, `reconcile`, `validate`, and the versioning methods (`list_versions`, `diff_version`, `rollback`). `agent_label` is injected by the agent loop from request context, never by the LLM. Tool results are capped at 12,000 characters before re-entering the message history (a `[truncated: …]` marker names the omitted count; the server-side trace record is written before truncation), so large reads cannot blow the context window mid-run. Trust tier and staleness are derived in `athenaeum/okf.py`, the single implementation shared by the librarian and the WebUI (stale when `today >= stale_after`, per OKF §5.5; a `verified` list without parseable verifier mappings counts as unverified).

### 3.3 LLMProvider protocol

```python
class LLMProvider(Protocol):
    async def complete(
        self, messages: list[dict], tools: list[dict], config: LLMConfig
    ) -> LLMResponse: ...


# LLMConfig: provider ('openai'|'anthropic'|'gemini'|'openrouter'|'openai-compatible'),
#   model, api_key, base_url?, max_iterations=10, temperature?, max_tokens?
#   resolved by the LibrarianManager from `provider_configs` + the per-agent
#   binding columns (0.8.0); an unbound agent follows the default connection
# 'openrouter' and 'openai-compatible' reuse the OpenAI adapter (openrouter defaults
#   base_url to https://openrouter.ai/api/v1; openai-compatible requires base_url)
# base_url is user-controlled and a trust boundary (CS-9): the server POSTs the
#   configured API key to it and the agent loop consumes the endpoint's output —
#   acceptable for the documented single-user self-hosting deployment
# LLMResponse: content blocks; either text (final) or tool_calls [{id, name, arguments}];
#   optional usage {prompt_tokens, completion_tokens, total_tokens} — populated by all
#   adapters (0.4.0), summed by the agent loop into traces and the activity journal
# LLMProviderError (0.4.1): raised by adapters when the endpoint returns an error
#   payload (incl. HTTP 200 with an error body, seen from OpenRouter) or an unusable
#   response — callers never see raw KeyErrors from response parsing.
#   Non-2xx HTTP responses map to LLMProviderError via the shared http_status_error
#   helper in every adapter; Gemini empty candidates without blockReason also raise.
#   Each provider instance holds ONE shared httpx.AsyncClient across completions.
#   Malformed tool-call argument JSON never kills a run: it surfaces as a
#   model-recoverable tool error (MalformedToolArguments) at dispatch time.
#   LLMConfig coerces api_key=None -> "" — "Bearer None" is unconstructable.
```

## 4. Storage model

### Data layout

```
data/
  app.db                          # users, librarian_configs, provider_configs, mcp_tokens,
                                  # embeddings, activity, app_settings, embed_reconcile_claims
  embedding-models/               # ONNX model cache (0.12.0, local embeddings)
  users/<user_id>/                # <user_id> = opaque UUID, never a username
    library/                      # OKF v0.2 bundle
      index.md                    # root index, frontmatter: okf_version: "0.2"
      log.md                      # single root log (phase 1)
      <concepts and subdirs>/
        index.md                  # index at every directory (progressive disclosure)
      .athenaeum/
        versions/<NNNNNN>/        # shadow-copy snapshots + meta.json
      .traces/
        <request_id>.json         # query-path traces (0.4.0)
```

### Query-path traces and the activity journal

Two transparency artifacts record what the librarian does (both 0.4.0):

- **Trace files** — one JSON per MCP request under `.traces/<request_id>.json`: ordered events (internal tool, summarized args/result, duration, error), LLM metadata (provider, model, iterations, summed token usage), outcome. Dot-prefixed, so invisible to all OKF traversal (index generator, validator, seed, link scan); never snapshotted — traces survive content rollback intentionally ("what the librarian did" is history). The WebUI replays a trace on the relations graph (numbered hops for reads/writes, dotted nodes for search hits, everything else faded) and lists all events in a textual timeline. Retention: hook `TraceStore.prune(keep_last)` behind per-user `trace_keep` (`0` = keep all; bounded default 50 for new installs/new config rows — existing rows are never migrated).
- **Activity journal** — `activity` table in `app.db`: one row per MCP tool call (request/trace id, user, token label, tool, truncated arguments, timestamps, duration, outcome/error, iterations, token usage). The trace id correlates a journal row to its replay. Retention: `prune_activity(keep_last)` behind per-user `activity_keep` (`0` = keep all; bounded default 1000 for new installs/new config rows — existing rows are never migrated). In-flight calls live only in process memory (`ActivityRegistry`) and are never persisted.

### Compound writes

Every mutation is a fixed-order compound write:

1. **Snapshot pre-image** (if versioning enabled) — copy of files about to be touched + `meta.json` (timestamp, actor, operation, affected paths).
2. **Concept file** — atomic write-then-rename (`<file>.tmp` + fsync + atomic rename).
3. **Regenerate affected `index.md`(s)** — index files are a pure function of the tree (deterministic scan + frontmatter parse, stable alphabetical order); never hand-edited. The generator doubles as a repair tool.
4. **Append root `log.md` entry** — kind (`Creation`/`Update`/`Deprecation`/`Move`/`Deletion`) + optional `(requested by agent:<label>)`.

Compound operations are not transactional; mitigations are the fixed write order, a startup/lazy `reconcile()` pass that regenerates all index files, and the snapshot layer for rollback. Compound writes are serialized per library root by the write lock (§7), so concurrency reduces to per-operation crash safety.

`actor` is always `athenaeum-librarian/<__version__>` per the OKF actor convention; `generated.by` semantics stay spec-clean (per-agent attribution lives in the log entry, not in `generated.by`).

### Versioning: shadow-copy snapshots

Per-operation pre-image copies under `.athenaeum/versions/<NNNNNN>/` with monotonically increasing numbering and `meta.json`. Provides rollback (copy back + log entry), audit trail, and WebUI diffs (difflib unified diffs computed on demand). Rollback is **latest-snapshot-only** (older snapshots rejected; mixed log/concept states otherwise) and is exposed in the versions WebUI — a Rollback button on the latest row only. Honors "plain directory" literally — OKF conformance quantifies over `.md` files only, so the auxiliary directory is invisible to consumers. Retention: keep-all in phase 1; the pruning hook is `VersionStore.prune(keep_last)`, invoked at the end of snapshot creation only when per-user config `snapshot_keep` (`0` = keep all; bounded default 50 for new installs/new config rows — existing rows are never migrated) is > 0. Upgrade path to real git is lossless (`git init && git add -A && git commit`).

### Isolation helper

All librarian filesystem access goes through a path-resolution helper rooted at the user's library: `resolve_under(root, rel) -> Path` rejects `..`, OS-absolute paths, and post-resolution escapes including symlinks (`Path.resolve()` comparison). This is the hard multi-user boundary, enforced by dedicated tests.

## 5. OKF conformance enforcement

Strategy: **enforce the tiny MUST surface, default to all conventions** (see `reference.md` for the format itself).

- **Single writer** — the librarian, through `LibraryBackend`, is the only writer — and the backend is also the only tree-scan boundary: the curate organization/semantic scans and the embedding sync/reconcile scans run through thin `LibraryBackend` pass-throughs (`organization_findings`, `semantic_duplicate_candidates`, `iter_concept_files`), never through direct `library/organize.py`/`library/semantic.py`/`links.py` calls with a raw root. `create_concept` refuses reserved names (`index.md`/`log.md`), non-`.md` paths, and existing files, and injects `generated: {by, at}`. `edit_concept` refreshes `generated.{by,at}` on every update — SPEC §5.2 defines it as the last meaningful change (a consumer freshness signal), not creation provenance; OKF v0.2 has no creation-time field. `edit_concept` never auto-verifies and preserves unknown frontmatter keys byte-intact (spec extension rule). User provisioning mirrors the same layering: db.py owns the user/config rows, `library.backend.provision_library` owns the on-disk bundle (library dir + `init_bundle`).
- **Automatic maintenance** — index regeneration and log append are side effects of every write op, not LLM-callable tools.
- **Validator** — tiered, sharing the frontmatter/link parse core with the index generator; run post-write (changed scope) and full-bundle (startup/on demand):
  - **Errors (6):** parseable frontmatter; non-empty `type`; reserved-name discipline + index frontmatter limited to root `okf_version`; `log.md` date heading format; REQUIRED-within fields (`generated.by`, `sources[].resource`, `runtime` for Attested Computation); `status` in {draft, stable, deprecated}.
  - **Warnings (8):** broken links (never errors per spec §6.1); missing `title`/`description`; non-conventional actor strings; `verified` bare mapping (normalized to a one-element list on read); malformed dates; index drift (auto-repairable); footnote labels not matching any `sources[].id`.

## 6. Configuration

- **SQLite** (`data/app.db`, idempotent DDL from one `_SCHEMA` column list per table; pre-existing tables gain missing columns via that list's migrate DDL) — nine tables: `users` (UUID id, username, argon2 password hash, admin flag), `librarian_configs` (per-agent binding columns `librarian_connection_id`/`curator_connection_id` + `llm_model`/`curator_model` (0.8.0), `prompt_addendum` (0.7.0), `versioning`, `snapshot_keep`, `trace_keep`, `activity_keep`, library name/description, curator prompt addendum (`curate_prompt_addendum`, 0.6.0), `curate_last_run_at`, the nightly-schedule columns `curate_schedule_enabled`/`curate_schedule_time` (0.11.0, UTC `HH:MM`; existing rows default to disabled, new users to enabled at 03:00), and the embedding binding columns `embedding_source`/`embedding_model`/`embedding_connection_id` (0.12.0) and `semantic_threshold` (0.13.0, nullable per-user override for the semantic-duplicate threshold); the legacy `auto_index`/`log_writes`/`system_prompt` columns plus the 0.8.0-retired `llm_provider`/`llm_api_key_enc`/`llm_base_url`/`llm_max_iterations`/`llm_temperature`/`llm_max_tokens`/`curate_provider`/`curate_model` columns are unread and unwritten; fresh tables no longer carry them — they survive only as migrate entries so pre-existing DBs keep their columns (SQLite column drop would need a table rebuild)), `provider_configs` (0.8.0: named per-user connections — label, provider, Fernet-encrypted key, base_url, generation defaults, `is_default` flag guarded by a partial unique index), `embeddings` (0.12.0: per-user float32 vector BLOBs keyed by `(user_id, concept_path)` with model, dims, content SHA-256 for drift detection, and updated-at; written by `EmbeddingService` sync-on-write + background reconcile; the local provider applies `query:`/`passage:` prefixes only for the E5 model family — every other local model embeds raw text), `mcp_tokens` (label, SHA-256 token hash, created/last-used/revoked timestamps), `activity` (MCP request journal, see §4), `embed_reconcile_claims` (background embed-reconcile ownership, §7), `app_settings` (server-wide key/value flags, e.g. `mcp_stateless_http`), `login_attempts` (per-account/per-IP login failure counters with `locked_until`, §2). SQLite is the single source of truth — no YAML config files. Sessions are signed cookies (no sessions table). Effective prompts are shown read-only in the Agents tabs (`/config/agents/librarian` and `/config/agents/curator`); both agents use the addendum model (0.7.0): the built-in default always applies and the owner's addendum is appended — the librarian's via `build_system_prompt` (shared by loop and display), the curator's to the built-in `CURATE_TASK_TEMPLATE` (placeholder contract intact).
- **Server env** (`config.py`, pydantic-settings): `ATHENAEUM_HOST` (default `127.0.0.1`), `ATHENAEUM_PORT` (`8000`), `ATHENAEUM_DATA_ROOT` (`./data`), `ATHENAEUM_SECRET_KEY` (required; also derives the Fernet key), `ATHENAEUM_LOG_LEVEL` (default `INFO`), `ATHENAEUM_BOOTSTRAP_ADMIN_USERNAME`/`ATHENAEUM_BOOTSTRAP_ADMIN_PASSWORD` (optional first-run owner pre-seed; consumed only while the users table is empty, never logged). One parsed `Settings` per app, cached on `app.state.settings` and served to handlers via the `settings_dep` dependency (no per-request re-parse).

## 7. Concurrency model

Single-worker remains the deployment default; the mechanisms below are the foundation for a later multi-worker deployment (see `roadmap.md`). Within one process, several entry points can target the same user's library at once: parallel MCP calls from the user's agents, the nightly scheduler, and WebUI requests (fresh `LibraryBackend` per request, on the threadpool).

- **Event-loop offload** — all synchronous filesystem/SQLite work at async boundaries runs via `asyncio.to_thread`: internal tool dispatch (every backend call), the seed middleware (seed regen + cold `manager.get`), token lookup, activity journaling (MCP and scheduler), the maintain/curate status and findings scans, the scheduler's cold `manager.get`, and local embedding model construction (first use downloads the ONNX weights). One librarian run, seed regen, or cold build no longer stalls MCP calls, the WebUI, or `/healthz` for any user.
- **Agent run gate** (`librarian/gate.py`) — keyed `(user_id, agent_kind)`; kinds today are `"librarian"` (`request_knowledge`/`store_knowledge`/`update_knowledge`) and `"curator"` (`library_maintain`/`library_curate`). `agent_kind` is an open lowercase string declared at the handler — future agents reuse the same gate with no schema change. Contention **rejects, never blocks**: MCP handlers raise `AgentRunBusyError`, converted to a retryable tool error ("another <kind> run is in progress; retry shortly"); a scheduler pre-check that finds the gate held skips silently (info log, no journal row, no re-baseline); contention surfacing after the pre-check journals an error row, matching the MCP tools. Different kinds and different users run in parallel.
- **Per-root write lock** (`library/backend.py`) — a process-wide `threading.RLock` per resolved library root wraps every compound write (WebUI builds fresh backends with no user context, so the key is the root path, not the user). The compound-write order itself is unchanged (snapshot -> concept file -> index.md -> log.md). RLock because `_regenerate_chain` nests into `regenerate_index`. With the event-loop offload above, lock waits only ever block a worker thread (async boundaries and the WebUI threadpool), never the loop thread.
- **LibrarianManager cache** — all cache reads/mutations (`get`/`evict`/`evict_idle`) hold the manager lock: callers run on the event loop and the WebUI threadpool, so an unguarded evict could race a concurrent `get`. Eviction (explicit, idle, or `close()` at app shutdown) cancels the librarian's pending embed-reconcile task.
- **SQLite hardening** (`db.connect`) — WAL journal mode, `busy_timeout=5000`, `synchronous=NORMAL` on every connection; `init_db` serializes concurrent startup inside `BEGIN IMMEDIATE`, and `_ensure_column` tolerates a concurrent ALTER. The WAL pragma is skipped when the file is already in WAL (it persists), so the exclusive-lock retry loop only runs during genuine conversion at startup — off the event loop. `busy_timeout` is a code constant, not a setting: it is a correctness parameter, and reading a DB-backed value at connect time would be a bootstrap paradox.
- **Embed reconcile claim** (`embed_reconcile_claims` table) — the background embedding reconcile is guarded by a conditional upsert (owner `hostname:pid:uuid`, 1 h TTL for crashed-owner recovery) instead of an in-memory flag, so two services — and later two workers — never reconcile one user concurrently. The task is strongly referenced on the `Librarian` (a fire-and-forget task could be garbage-collected mid-run) and cancelled on eviction/shutdown; the claim row is released in a `finally`, so a cancelled or failed reconcile never blocks the next one for the TTL.
- **Curate run bookkeeping** — `curate_last_run_at` is written at every completed curate run (MCP or scheduler; failed runs skip it). It is bookkeeping only: curate findings are recomputed over the whole library on every run, so unaddressed findings persist until actually fixed (no changed-set scoping).
- **Seed cache** — `generate_seed` caches `(log.md mtime, seed)` on the backend and revalidates by `stat` on every call: every compound write appends log.md, so its mtime is an exact, process-agnostic version counter. Tool descriptions are composed per request by the seed middleware (base description + the caller's current seed); the shared FastMCP registry keeps the base descriptions forever, so one user's seed can never leak into another user's tools/list response.
- **Process-local registries** — `ActivityRegistry` (in-flight calls) and `EmbedStatusRegistry` (embed progress) stay per-worker and are never persisted. The durable `activity` table is the cross-worker audit view; the WebUI in-flight page shows the local worker only. Revisit only with an actual multi-worker deployment.

**MCP transport and multi-worker.** Multi-worker is a redesign, not a flag: the agent run gate, the per-root write locks, the `LibrarianManager` cache, the seed caches, the process-local registries, and the FastMCP Streamable HTTP sessions all live inside one process, so no single toggle makes them safe to share. Enabling `stateless_http` removes session affinity only (no session resumability; any worker can serve any request) — it does not share or replace any of the process-local stores above. Scaling out therefore needs the cross-process replacements first: the documented DB write-lock mutex, per-worker-safe or shared registries, and either sticky sessions or a shared external event store for FastMCP. `stateless_http` is a DB-backed `app_settings` flag toggled in the Admin WebUI (Admin > Server), read once at startup; default off (sessions kept).

**Documented, not implemented:** the cross-process write lock (a DB mutex row via an IMMEDIATE transaction) is the designed mechanism for serializing compound writes across workers; it ships only with multi-worker deployment itself.
