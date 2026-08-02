# Athenaeum — Version

Current version: **0.19.0**

Versioning follows Semantic Versioning (SemVer): `MAJOR.MINOR.PATCH`.

| Part | When to increment |
| ---- | ----------------- |
| MAJOR | Breaking changes that require user action (e.g. MCP contract changes) |
| MINOR | New, backward-compatible features |
| PATCH | Bug fixes and small improvements |

The version must stay in sync across:

- `VERSION.md` (this file)
- `src/athenaeum/__init__.py` — `__version__`
- `pyproject.toml` — `version`

## Version History

> Entries for 0.1.0–0.2.1 were recorded retroactively; the repository was not
> yet under version control, so no commit hashes are available.

### 0.19.0

Hybrid search: `search_semantic` now fuses the embedding leg with a lexical
FTS5 BM25 leg via reciprocal rank fusion, with an optional local cross-encoder
rerank pass (inspired by github.com/tobi/qmd).

- New `concepts_fts` FTS5 virtual table in `app.db` (porter tokenizer,
  plain-content; shadow tables `concepts_fts_*` share the DB). The new
  `FtsIndex` (`fts.py`) mirrors the embeddings table's keys, text, and content
  hashes, and rides the existing `EmbeddingService` flows: write-through sync
  (FTS rows land even when the embed call fails — the lexical leg is the
  no-provider degradation leg) and the claimed background reconcile, which
  backfills pre-existing libraries on first agent request after upgrade.
- Retrieval: semantic + lexical legs merge via RRF (k=60); when enabled, a
  local fastembed `TextCrossEncoder` (`Xenova/ms-marco-MiniLM-L-6-v2`, ~0.08
  GB, downloads on first use) reranks the top 30 fused candidates off the
  event loop. Reranker missing/failing falls back to RRF order; FTS5
  unavailable falls back to the legacy pure-semantic path; embedding-pipeline
  failure keeps the `fallback: true` metadata path. Hit scores are reranker
  logits (may be negative) or RRF scores — relative ranking aids, never trust
  signals; legacy path keeps cosine.
- Two per-user config toggles `hybrid_search` / `hybrid_rerank`
  (`librarian_configs`, `NOT NULL DEFAULT 1` — upgrades are hybrid-on),
  editable in the WebUI embeddings tab (checkboxes) and stored via
  `db.update_embedding_config` COALESCE semantics (omitted = keep).
- No new MCP tool: name, parameters, 10-tool count, result shape, and the
  unconfigured-error contract are unchanged. Curate duplicate detection and
  related-concept injection stay pure-semantic.

### 0.18.0

Sunburst library view: the orbit/universe graph is fully replaced by a 2D
sunburst (user decision; the 0.16.x–0.17.x orbit lineage and the particle
nebula intermediate step are both gone).

- `/library/graph` is a pure 2D `<canvas>` view (no WebGL on the page;
  the vendored ForceGraph3D stack now serves trace replay only): root-level
  documents fill a ring disc around a glowing center anchor, top-level
  folders dock outward as sectors (angle proportional to file count), every
  file is exactly one dot, and the radial position encodes sqrt-scaled link
  density — the only metric, served by the new
  `GET /api/graph/universe` endpoint (flat payload: clusters, nodes with
  normalized radius, edges).
- Files directly in a top-level folder fill an (undrawn) folder circle at
  the sector bisector; subfolders nest hierarchically — every folder
  subdivides its parent's angular span (direct files by count, child
  folders by recursive total). Each folder gets a cluster-colored bracket
  arc on its own level ring plus a curved name label that stands on the
  line; hovering/selecting a dot lights its entire folder path (brackets,
  folder and sector labels).
- Document links render as subtle center-bowed arcs; clicking a dot zooms
  in with an edge/neighbor highlight and tooltip (second click opens the
  document), sectors and folder circles zoom on click, Esc/Fit view resets,
  `?folder=`/`?focus=` deep links and the minimap (with viewport rectangle)
  work throughout.
- Removed: `computeOrbitLayout`, `computeFolderBodySizes`, the orbit
  CONFIG, the particle nebula (`graph_particles.js`,
  `graph_viewstate.js`), legacy graph controls (Stars/Planets/Moons,
  search, trust legend). Trace replay keeps working on the live force
  layout (`mount`/`buildUniverse` signatures unchanged).
- Demo seed: 80 concepts across 6 clusters with deterministic cross-links
  (hubs up to degree 26, isolates) and a 320-day `generated.at` spread so
  both metric candidates produce visible radial structure.

### 0.17.1

Folder bodies for the 3D relations universe.

- Folder nodes now render as real bodies instead of bare labels: a star is a
  large additive glow sprite, a planet is a matte lit sphere mesh
  (`MeshLambertMaterial`). The SpriteText label sits offset above the body
  inside a shared `THREE.Group`, so hover tooltips, click-to-focus, and the
  Stars/Planets/Moons toggles work on the whole node.
- Body sizes are derived deterministically from the loaded universe via the
  new `computeFolderBodySizes` helper (graph3d.js): planet diameter =
  max moon scale + `folderBodyPlanetGap`, star diameter = planet diameter ×
  `folderBodyStarRatio` (with absolute minimums). The invariant
  star > planet > max moon scale holds for any universe, including one with
  zero moons.
- Folder bodies now honor `__style.color` (tint) and `__style.alpha`
  (dim) in trace replay; folder `__style.scale` is intentionally ignored —
  scaling a folder body would break the size invariant.

### 0.17.0

Orbit-layout radius budget and star/planet/moon kind remap for the 3D
relations universe.

- `computeOrbitLayout` (graph3d.js) now computes ring radii with a recursive
  two-pass radius budget instead of a pure child-count rule: pass 1 derives
  each folder's subtree extent bottom-up, pass 2 places bodies top-down. A
  child subtree always stays inside its parent's orbit budget (`ringGap: 8`
  clearance, `maxChildRingFraction: 0.75` extent cap), so subtrees no longer
  cross parent rings, collide with siblings, or overshoot the rogue ring —
  the guaranteed invariant is a minimum pairwise distance of 8 units between
  any two bodies, verified numerically.
- Kind vocabulary remapped `galaxy/system/planet/moon` -> `star/planet/moon`:
  top-level folder = star, every deeper folder = planet, every file = moon
  (the depth-<=2-planet / depth->=3-moon document split is gone). The graph
  page now has three filter toggles (Stars/Planets/Moons), and all documents
  are sized uniformly by link degree (`starScaleBase + degree *
  starScalePerDegree`) instead of the two-tier planet/moon sizing.

### 0.16.0

Deterministic orbit layout for the 3D relations universe. The force-directed
simulation on the graph page is replaced by a pure, deterministic
`computeOrbitLayout` (graph3d.js): every body is pinned (`fx`/`fy`/`fz`) to a
ring in a shared ecliptic plane — folders orbit their parent folder,
depth-1 folders orbit the universe center, root-level documents sit on an
outer rogue ring, and wikilinked siblings are placed in adjacent orbit slots
(stable FNV-1a hash ordering, so positions never reshuffle across reloads).
The default d3 forces are removed, node drag is disabled, and the engine
settles immediately (`warmupTicks: 0`, `cooldownTime: 300`). The
depth-2 folder cap in `/api/graph` is lifted: folder nodes are now emitted
for arbitrary depth (depth 1 = galaxy, depth >= 2 = system). No payload
schema changes otherwise; templates and trace replay are untouched.

### 0.15.0

Deep code review 2026-07: 51 findings fixed in four phases (criticals,
highs, mediums, lows). The repository is not under version control, so no
commit hashes are available; the phase-by-phase record lives in
`docs/SubAgent/DEEP_REVIEW/CHANGES.md`.

- **Security:** critical path traversal in `VersionStore.diff`/`rollback`
  closed (root-relative resolution via `resolve_under`, `PathEscapeError`
  -> HTTP 400); login throttling with per-account/per-IP lockout
  (`login_attempts` table, 5 failures -> doubling lockout) plus a 12-char
  minimum password policy and an atomic first-admin setup; CSRF tokens on
  every cookie-authenticated form POST (`csrf_protect` router dependency,
  pinned `same_site="lax"` session cookies); numeric WebUI form inputs
  hardened (non-numeric -> 400, nan/inf rejected, retention keeps clamped
  >= 0); MCP tool catch-alls sanitized to generic errors (internals logged
  server-side only); `base_url` documented as a user-controlled trust
  boundary in the provider UI.
- **Correctness:** store/update runs fail honestly on zero writes
  (summary-only no longer accepted; one nudge retry) and report
  `partial: true` on mid-loop provider failure after landed writes;
  curate findings recomputed over the whole library every run (unaddressed
  findings persist until fixed; `findings` is the post-run report);
  dogfooding findings F13/F16/F18 resolved; `is_stale`/`trust_tier`
  unified in `athenaeum/okf.py` with a spec-pinned boundary contract test;
  rollback is latest-snapshot-only and exposed in the versions WebUI;
  anchored links survive moves (`/b.md#s` -> `/moved/b.md#s`); embedding
  sync collapses writes per concept path (last action wins; moves delete
  the old-path row via `from_id`); local `query:`/`passage:` prefixes
  apply to the E5 family only; Gemini empty candidates and malformed tool
  arguments surface as errors instead of silent success; config loading
  uses named row access with stored `0` values preserved; curator
  inheritance is an explicit `curate_llm is None` marker.
- **Concurrency / event loop:** all blocking filesystem/SQLite work at
  async boundaries offloaded via `asyncio.to_thread` (tool dispatch, token
  lookup, seed regen, maintain/curate scans, scheduler, activity
  journaling, local embedding model construction); `LibrarianManager`
  cache fully lock-guarded; embed-reconcile task retained and cancelled on
  eviction/shutdown; `last_used_at` token writes coalesced (max one UPDATE
  per token per 300 s); duplicate scans bounded (vectorized block cosine
  with numpy, inverted-index Jaccard candidates) while staying exact over
  the whole library; scheduler per-run timeout (default 45 min) so a hung
  run no longer stalls later users.
- **Mediums:** CSRF (above); shared wiring deduplicated (`_agent_run`
  context manager, `journal_activity`); 12,000-char tool-result context
  budget with truncation marker; `mcp_server <-> activity` import cycle
  broken via `athenaeum/identity.py`; user provisioning moved out of db.py
  (`provision_library`); tree scans routed through `LibraryBackend`
  pass-throughs; bounded retention defaults for new installs (snapshots
  50, traces 50, activity 1000 — existing rows never migrated); O(1)
  append-only log.md (chronological on disk, legacy newest-first files
  flipped once); snapshot version probing eliminated; LLM adapters share
  one `AsyncClient` per provider and map HTTP errors to
  `LLMProviderError`; `api_key=None` normalized to `""`.
- **Lows:** single `_SCHEMA` source of truth (fresh tables drop dead
  legacy columns, pre-existing DBs gain columns via migrate DDL);
  llm-config migration stays in init_db's transaction; one cached
  `Settings` per app (`app.state.settings` via `settings_dep`); MCP
  catch-all mount bounded to `/mcp` (plain 404 elsewhere); graph walk
  iterative and depth-bounded (400 instead of RecursionError 500); logging
  at previously silent exception sites (seed fallback signals staleness);
  `list_dir`/`search_semantic` null/limit coercions made explicit.
- **Upgrade note:** users on local BGE/MiniLM embeddings should force a
  re-embed (re-save the embedding config) — rows embedded before the
  E5-prefix fix are not re-embedded automatically.

### 0.14.0

- `store_knowledge` gains an optional `topic_hint` parameter: a
  caller-supplied topic that names the target topic area directly and
  overrides subject inference (the librarian still checks the root
  index.md so the hint does not duplicate an existing area under a
  different name). Threaded MCP tool -> `Librarian.handle_store` ->
  STORE TASK template; covered by an MCP-level passthrough test.
- Placement fix (dogfooding finding F17): the taxonomy rule now requires
  naming the subject in one phrase and matching it against the root
  index.md BEFORE placing — extend an existing area only on a subject
  match; a new subject/project/domain always earns its own top-level
  area; a document kind (lessons, notes, decisions) is not a topic. Root
  cause of the HA-AgentHub misplacement: the old rule ("prefer extending
  an existing area") had no subject test. Contract-pinned in
  tests/test_prompts.py.
- STORE TASK clarifications: caller-suggested related concepts and
  similarity-ranked candidates are back-link candidates, NOT placement
  hints; the librarian must state the subject and target topic area
  before its first write. The byte-shape pins in
  tests/test_related_injection.py stay green unchanged.

### 0.13.0

- Fix (dogfooding finding F15, Befund 1): the semantic-duplicate cosine
  threshold is now model-dependent. The fixed 0.85 was calibrated for BGE and
  never fired with all-MiniLM-L6-v2 (live measurement 2026-07-30: max pair
  cosine 0.83, p99 0.786, zero pairs >= 0.85 across 19 concepts/171 pairs).
  New per-model default map in `library/semantic.py` (initial calibration,
  TUNABLE; unknown models keep the conservative 0.85 fallback) plus an
  optional per-user override on the Embeddings tab (nullable
  `semantic_threshold` column on `librarian_configs`; empty = model default;
  override wins, PD-1: DB + WebUI, no env).
- Fix (dogfooding finding F15, Befund 3): the librarian system prompt gains a
  contract-pinned retrieval rule — one `search_semantic` call per distinct
  information need; rephrase only with genuinely new vocabulary or a
  different aspect; never re-query when earlier hits already cover the need
  (near-identical reformulations burned loop iterations in live runs).

### 0.12.0

- New optional embedding subsystem: per-user binding (off / local model / API
  connection) stored on `librarian_configs`; local models run via fastembed
  (ONNX, new `local` optional extra — pip installs stay dependency-free
  without it, the Docker image includes it), API embeddings via OpenAI,
  OpenRouter, or Gemini connections (anthropic has no embeddings endpoint and
  is rejected). Vectors are float32 BLOBs in a new `embeddings` SQLite table
  keyed by `(user_id, concept_path)` with a content SHA-256 for drift
  detection; indexing is sync-on-write plus a guarded background reconcile
  (model switch = full re-embed) with an in-memory per-user status registry.
- New internal librarian tool `search_semantic` (10th): ranked similarity
  hits over title/description/body embeddings; unconfigured yields a
  model-recoverable error naming `search_metadata`, pipeline failures degrade
  to `search_metadata` hits marked `fallback: true`. Scores stay internal —
  external MCP response schemas are unchanged.
- `library_curate` gains a fifth finding kind `semantic_duplicate_candidates`:
  embedding-similarity pairs (cosine >= 0.85, TUNABLE) computed purely over
  cached vectors with the same-type/changed-set gates, the numbered-series
  guard, and Jaccard dedup against the structural near-duplicate pass.
- `store_knowledge`/`update_knowledge` inject "possibly related existing
  concepts" (top-5 semantic) into the librarian task preamble — one embed
  call outside the agent loop, zero extra iterations; unconfigured/failure/
  empty leaves the task text byte-identical.
- WebUI: new Agents > Embeddings tab — source/model/connection settings, a
  Test button (one trivial embed, renders dims + duration), and an index
  status card (run state + stored vectors/models). Connections bound as the
  embedding connection are deletion-protected like the other agent bindings.

### 0.11.2

- Fix (F14 follow-up, found in the live curate re-test): `_is_series_pair`
  now treats letter-suffixed number tokens (e.g. `4c`, `4b`) as series
  identifiers too — `_NUMERIC_RE` widened from `\d+` to `\d+[a-z]*`. The
  0.11.1 pattern missed `phase4c`-style titles, leaving false near-duplicate
  findings that would have resurfaced on every curate run.

### 0.11.1

- Fix (dogfooding finding F14): the near-duplicate scan no longer flags
  numbered title series — pairs whose title tokens differ only in pure
  numbers (phase lessons, dotted version docs) are excluded via
  `_is_series_pair`; identical titles remain duplicate candidates.
- The curator prompt now pins the rule: never merge or deprecate documents
  that differ only in a version/phase identifier.
- `library_curate` result gains `health_after: {healthy, orphans}` (post-run
  status snapshot) so curator-merge-induced orphans are visible in the
  result/journal.

### 0.11.0

- Per-user nightly scheduled curation: a `CurateScheduler` asyncio task in the
  app lifespan runs `library_maintain` + `library_curate` for each opted-in
  user at a fixed UTC time of day. New columns
  `curate_schedule_enabled`/`curate_schedule_time` on `librarian_configs`
  (existing rows default to disabled; new users to enabled at 03:00 UTC).
- Scheduled runs reuse the librarian handlers with MCP-equivalent wiring: one
  trace session + one activity row per tool (label `scheduler`), in-flight
  registry entries, curate re-baseline on completed runs, and seed-cache
  invalidation after mutating runs. Missed runs are skipped, never caught up;
  a maintain failure never blocks the curate run.
- New "Scheduled curation" card on the curator tab (WebUI): enable checkbox +
  time of day, stored as UTC `HH:MM`, displayed in browser-local time via
  client-side conversion (UTC verbatim without JS).
- Fix: the Activity journal no longer renders dead Replay links — the link
  now appears only when the trace file exists on disk (no-op agent runs
  journal a row but write no trace).

### 0.10.6

- Fix (layout): the graph canvas really stays inside the viewport now —
  the `.card` wrapping it was a block parent, so `flex: 1` on the canvas
  was a no-op and the canvas grew to its WebGL content height (measured
  1307px against a 1305px viewport). The card is now a flex column that
  grows with the page, and the canvas has `min-height: 0` so flex can
  shrink it. Verified by measurement: canvas bottom 1197px < 1305px
  viewport.

### 0.10.5

- Fix (layout, supersedes 0.10.4): the viewport pinning moved from the
  page to `.main-col` — the 0.10.4 rule forgot the footer outside
  `.page`, so the canvas still overflowed by the footer height.

### 0.10.4

- Fix (layout): graph pages are pinned to the viewport height
  (`.page:has(.graph-canvas)` with `overflow: hidden`) so the canvas fills
  the remaining space instead of overflowing below the fold; other pages
  keep normal scrolling.

### 0.10.3

- Layout/space pass (user feedback):
  - 3D universe background is now pure black (`#000000`).
  - All pages use the full browser width (the 1100px content max-width is
    removed; the page is a full-width flex column).
  - The relations graph canvas fills the remaining viewport height
    (flex-grow) instead of a fixed 70vh; the obsolete CSS starfield behind
    the canvas was removed (the real 3D starfield lives in the scene).

### 0.10.2

- 3D universe polish (user feedback):
  - Background is now near-black (`#050505`) instead of dark blue — closer
    to real space.
  - Idle auto-rotation: the universe slowly orbits when the user is not
    interacting (orbit controls, re-arms 8 s after the last interaction;
    disabled on the trace replay so camera flights stay in charge).
  - The "Zoom to galaxy/system" select was removed — folder clicks and
    search cover the same navigation.
  - Tree-to-graph jump: every tree row (folder or concept) has a small
    target icon linking to the corresponding spot in the 3D universe
    (`/library/graph?folder=` / `?focus=`; the graph flies there once the
    engine settles).
  - Demo data: the drifting-memo orphan is now truly unlinked (it had a
    deliberate link before, which contradicted its purpose as an
    isolation test).

### 0.10.1

- 3D universe depth + framing pass (user feedback: flat look, no starfield,
  loose grouping):
  - Real 3D starfield: 2200 soft-point stars in a shell around the graph
    (THREE.Points, parallax on rotation) plus exponential scene fog for
    depth falloff.
  - Framing replaced `zoomToFit` with a custom `fitContent`: bounding-box
    center (not the force origin) + aspect-aware distance — the universe
    now opens centered and frame-filling; "Fit view" and folder focus use
    the same math; folderless root planets no longer blow up the frame.
  - Grouping tightened: containment forces dominate more (2.5 vs 0.25),
    shorter link distance (45).
  - Demo library expanded to 50 concepts across 5 galaxies with more
    systems, moons, cross-links and one folderless orphan planet
    (`scripts/seed_demo_library.py`, idempotent).

### 0.10.0

- 3D universe visual rework (user feedback: haze clouds occluded content,
  visuals below par):
  - New vendored bundle `graph3d-vendor.min.js` (esbuild-built once at
    vendor time via `scripts/build_graph_vendor.mjs`): three 0.185.1 +
    3d-force-graph 1.80.0 + three-spritetext 1.10.0 + UnrealBloomPass as
    ONE three instance — unlocking custom `nodeThreeObject` visuals,
    sprite labels, and bloom post-processing (the old UMD bundle exposed
    no THREE global; it is removed).
  - Planets/moons render as soft glowing stars (canvas radial-gradient
    sprites, additive blending, depthWrite off) instead of hard spheres;
    sized by link degree; health colors.
  - UnrealBloomPass added (strength 0.7, radius 0.4, threshold 0.3) for a
    controlled deep-space glow.
  - Enclosing haze spheres removed entirely — no more occlusion. Galaxy
    and system membership now reads from cluster physics plus persistent
    sprite-text labels at the folder centers (galaxies large/bright,
    systems small/dim).
  - Links kept deliberately quiet (thin, dim blue-gray, small directional
    particles) so the health-colored stars carry the picture.
  - New search-to-fly: the "Find a planet..." field locates a concept by
    label/path and flies the camera to it with neighbor highlight.
  - Trace replay rides the same renderer (overlay restyles now mutate the
    sprite objects directly).

### 0.9.2

- 3D universe physics tuning (user feedback: clouds had no visible
  content): containment forces now dominate (strength 2 vs 0.35 for real
  links) with short distances (planet 10, system 18, moon 4), so planets
  and moons stay visibly inside their system cloud and systems inside
  their galaxy cloud; cloud proportions fixed (galaxy ~4x system volume);
  system haze slightly more opaque than galaxy haze for nesting contrast;
  initial fit and "Fit view" frame the content (planets/moons), not the
  cloud spheres; reduced inter-galaxy charge.

### 0.9.1

- 3D universe refinement (user feedback): type-based filter checkboxes
  replaced by structural toggles (Galaxies, Systems, Planets, Moons);
  galaxy/system folder nodes are now rendered as translucent haze clouds
  (much larger, ~0.10 opacity) instead of discrete nodes, and containment
  links are no longer drawn at all — folder belonging reads from the
  cloud + clustering, only real markdown links render as lines (folders
  have no relations). Brighter link edges for the deep-space look.
- New `scripts/seed_demo_library.py`: idempotent generator for a "demo"
  user with a rich demo library (3 galaxies with systems, planets, moons,
  cross-galaxy links, mixed trust/stale states) for graph testing;
  generator covered by `tests/test_demo_seed.py`.

### 0.9.0

- Phase 5 — relations graph rework as a **3D relational universe** (user
  decision, replaces the planned 2D Obsidian-style rework): library =
  universe, depth-1 folders = galaxies, depth-2 folders = systems,
  concepts = planets, depth-3+ = moons. Rendered with the vendored
  3d-force-graph 1.80.0 (self-contained UMD, three.js bundled, MIT) under
  `webui/static/vendor/` — the project's first vendored frontend asset
  (vis-network was CDN-loaded and is fully removed).
- `/api/graph` extended: concept nodes gain `folder`/`depth`/`kind`
  (planet|moon)/`trust_tier`/`stale`; new `folders` list (galaxy|system);
  `edges` stays link-edges-only (containment synthesized client-side);
  the vis-specific `arrows` key was dropped.
- Universe view (`/library/graph`): sizing by link degree, health colors
  (server remains the source of truth), glowing directional-particle link
  edges, zoom-to-galaxy/system, type filter, moon toggle, neighbor
  highlight on select, click-to-open the document page, CSS starfield.
- Trace replay ported to 3D: camera journey through the query path with
  new play/pause/step controls, scale+color halos for visited planets,
  pulsing search hits, faded others, numbered hop badges, timeline sync
  (`data-seq`).
- Deferred: the graph-health overlay (orphans/broken-link marking) — the
  health data ships in `/api/graph`, the overlay is a follow-up.

### 0.8.4

- Fix (UI): added spacing below the Agents tab navigation — the tab switch
  was visually docked to the settings cards.

### 0.8.3

- Fix (UI, supersedes 0.8.2): connection options in the agent selects now
  show only the provider name (e.g. "openrouter") — the parenthesized
  connection label was removed on user request. Labels remain editable on
  the Provider page.

### 0.8.2

- Fix (UI, supersedes the 0.8.1 rendering): agent connection selects now
  show the inherit option as plain "Default" and connections as
  `provider (label)` — provider name first. The 0.8.1 `label (provider)`
  rendering still read as duplicated for a migrated connection labeled
  "Default".

### 0.8.1

- Fix (UI): agent connection selects now render options as
  `label (provider)` with a `— default` marker on the default connection.
  Previously a migrated connection labeled "Default" was
  indistinguishable from the "Default connection" inherit option.

### 0.8.0

- Multi-provider connections (roadmap phase-6 groundwork, decisions D1-D6):
  new `provider_configs` table holds any number of named provider connections
  per user (label, provider type, Fernet-encrypted API key, base URL,
  generation defaults). One connection is the default, enforced by a partial
  unique index plus an atomic clear-and-set transaction; the first created
  connection becomes the default automatically.
- The Provider page becomes a management screen: connection list with
  key-set/default badges, create/edit/delete, per-connection test button
  (htmx partial). The test gate message is unchanged ("Save a provider and
  model first.") — the model still lives on the Librarian tab.
- Per-agent binding: the Librarian and Curator tabs each get a connection
  select plus a model field. Empty selection follows the default connection;
  the manager resolves the effective `LLMConfig` per agent (an inheriting
  curator shares the librarian's config object). The 0.5.0
  `curate_provider`/`curate_model` overrides are retired — the curator's
  connection now always uses its own credentials.
- Delete protection (D3): deleting a connection is blocked while an agent is
  bound to it, and the default connection cannot be deleted while others
  exist; both surface via a 303 `?error=` redirect rendered as a danger
  toast. Deleting the last connection is allowed (unconfigured state).
- Migration: each user's existing `llm_*` settings become the first
  connection (label "Default", `is_default=1`); the per-agent bindings stay
  NULL (inherit the default). Idempotent per-user guard; safe on re-run.
  The old `llm_*`/`curate_provider`/`curate_model` columns stay in the DDL
  as dead columns (no table rebuild).

### 0.7.3

- Fix: the librarian tool dispatch now validates schema-required arguments
  up front and returns a model-recoverable error ("missing required
  argument(s): body") instead of crashing with a raw `KeyError`. Root cause
  of the failed 0.7.0 store attempts: gpt-oss repeatedly called
  `write_concept` without the `body` argument; the opaque crash made the
  model abandon the write and end with an empty summary.

### 0.7.2

- New contract-pinned retrieval rule "NEVER RE-READ": no duplicate document
  reads or repeated searches within one task — earlier tool results stay
  available. Root cause of the failed 0.7.0 stores: the model re-read the
  same concept repeatedly, burned the 10-iteration budget on exploration,
  and hit `max_iterations` before the actual write (the cap-path final
  answer then came back empty).

### 0.7.1

- Fix (dogfooding finding F11): store/update requests could complete
  "successfully" with zero writes and an empty summary — a silent no-op
  (observed with gpt-oss returning empty `content` after read-only
  exploration). `handle_store`/`handle_update` now detect the F11 signature
  (no writes + empty summary), retry once with an explicit nudge, and
  otherwise raise `LibrarianNoWriteError`, which the MCP handlers surface as
  a `ToolError` ("completed without writing; retry or rephrase"). A
  substantive no-write explanation (e.g. "already covered") stays a valid
  outcome. New contract-pinned write-discipline rule "A STORE ENDS IN
  WRITES".

### 0.7.0

- Librarian prompt config switches from wholesale override to **addendum
  only** (user decision, aligning the librarian with the curator model):
  the effective system prompt is now always the built-in default plus an
  optional per-user `prompt_addendum` ("Standing rules from the library
  owner:"). Revised defaults (taxonomy, answer hygiene, future revisions)
  reach every user; the escape-hatch wholesale override is removed. The
  legacy `system_prompt` column remains in the DDL but is no longer read or
  written (dead-column precedent; no overrides existed in the wild).
  Config-semantics change: a previously set override would stop applying —
  verified none exist.
- New shared `build_system_prompt(addendum)` used by both the agent loop
  and the Librarian-tab display, so runtime and display cannot drift.

### 0.6.0

- WebUI configuration restructure: the old LLM/Behavior/Prompt pages become a
  **Provider** page (connection + generation defaults for both agents) and an
  **Agents** page with Librarian/Curator tabs that own the per-agent model and
  prompt, each with a read-only effective-prompt display (librarian:
  default-vs-override badge; curator: built-in template + addendum badge).
  The retention knobs (`versioning`, `snapshot_keep`, `trace_keep`,
  `activity_keep`) move from the Behavior page to the Library page.
- Curator prompt addendum: new per-user `curate_prompt_addendum` column,
  appended to the built-in `CURATE_TASK_TEMPLATE` under the label "Standing
  curation rules from the library owner:" via a brace-safe format value
  (addenda containing `{...}` can neither crash rendering nor be parsed as
  placeholders); editable on the Curator tab.
- Legacy GET redirects: `/config/llm` -> `/config/provider`,
  `/config/behavior` and `/config/prompt` -> `/config/agents/librarian`;
  the old POST endpoints are removed (405).
- DB helper split: `db.update_llm_config` becomes `update_provider_config`
  (no `model` param) + `update_librarian_config` (`model` + `system_prompt`);
  `update_behavior_config` is absorbed by `update_librarian_config` and the
  extended `update_library_settings` (gains versioning/retention params);
  `update_curate_config` gains `curate_prompt_addendum`.

### 0.5.0

- New MCP tool `library_curate` (6th external tool): the librarian tidies,
  reorganizes, and consolidates the library. A deterministic organization
  scan (`library/organize.py`) reports findings — type-named top-level
  folders, oversized folders (>12 direct concepts), thin concepts (<200 body
  chars), near-duplicate candidates (same type, title Jaccard >= 0.6) — and
  the LLM acts only on the reported findings (move misplaced concepts,
  enrich stubs, merge duplicates via enrich-then-deprecate, delete only at
  zero inbound links). No-op without an LLM call when the library is
  well-organized (anti-churn by construction).
- Per-user optional curate model/provider override (D5): `curate_provider` /
  `curate_model` columns on `librarian_configs` and a "Curate model
  (optional)" card on the LLM connection page; empty fields run curate on
  the default connection (credentials inherited). PD-1 conformant: DB +
  Admin WebUI, no env vars.
- Incremental scope: `curate_last_run_at` records each completed run's
  end timestamp; per-concept findings cover only concepts changed since the
  last run, while structural folder checks always scan the whole tree.
- Observability: curate runs appear in traces and the activity journal like
  any other MCP call; journal rows for `library_curate` get replay links.

### 0.4.4

- New "ANSWER HYGIENE" rule (contract-pinned): final answers contain results
  and citations only — no narration of process, tool calls, or iteration
  limits; unread items are stated as plain coverage gaps. Also applied to
  `FINAL_ANSWER_REQUEST` (cap-exhaustion path), mitigating dogfooding
  finding F10 (process narration leaked into final answers after the cap).
- "OKF v0.2" references removed from the system prompt: the structure rules
  are self-contained, and models need only follow them — namedropping the
  spec invites hallucinated "spec knowledge" instead of rule-following.

### 0.4.3

- System prompt gains a "Placement and taxonomy" section: organize by topic,
  never by document type; small set of top-level topic areas; flat within a
  topic until a natural group emerges; subject-named files; `move_concept`
  for repairs. Addresses ad-hoc type-based folder growth (`projects/`,
  `servers/`, `versions/`) observed in dogfooding — the spec leaves taxonomy
  to the producer and the prompt previously said nothing about it.
- `move_concept`/`delete_concept` now prune directories left empty (removing
  their orphaned `index.md`) so restructures don't leave ghost folders in
  the tree; rollback still recreates parent directories on restore.

### 0.4.2

- Spec-conformance fixes after re-review against OKF v0.2 SPEC.md:
  - Reverted the 0.4.0 `generated.at` change: `edit_concept` and
    `deprecate_concept` again refresh `generated.{by,at}` on every update.
    SPEC §5.2 defines `generated.at` as the last *meaningful change* (a
    freshness signal for consumers), not as creation provenance — the 0.4.0
    "fix" was based on a misreading and broke that signal.
  - `is_stale` boundary corrected to SPEC §5.5: a concept is stale
    *on/after* `stale_after` (`today >= stale_after`); previously the
    boundary day itself was reported as not stale.
  - System prompt text corrected to the same spec semantics.

### 0.4.1

- Fix (dogfooding finding F7): LLM adapters no longer crash or silently
  misbehave on provider error payloads. New `LLMProviderError`; the OpenAI
  adapter raises it with the provider's `error.message` when an HTTP 200
  body contains an `error` object (observed from OpenRouter) or no
  `choices`; the Anthropic adapter raises on `type: "error"` bodies; the
  Gemini adapter raises when the prompt was blocked
  (`promptFeedback.blockReason`) instead of returning an empty answer.

### 0.4.0

- Librarian transparency in the WebUI (roadmap phase 4):
  - Query-path traces: one JSON trace per MCP request under
    `<library>/.traces/` (searches, reads, writes, durations, outcomes),
    recorded via a ContextVar seam in the librarian's tool dispatch.
  - Trace replay on the relations graph (`/library/traces`): numbered
    directed hops, visited concepts ringed, search hits dotted, others faded.
  - Activity view (`/activity`): passive journal of MCP tool calls (tool,
    intent, agent label, duration, outcome, token usage) backed by the new
    `activity` table in `app.db`, plus live in-flight requests; journal rows
    link to the matching trace replay.
  - System prompt display (`/config/prompt`): read-only view of the
    effective librarian prompt with default/override badge.
- `DEFAULT_SYSTEM_PROMPT` revised from dogfooding findings: link-following
  and synonym retry on retrieval, new "NO EMPTY SECTIONS" write rule,
  `generated` = creation provenance, answer-only retrieval, exact
  status/trust-tier vocabulary.
- `LLMResponse.usage`: token usage (prompt/completion/total) captured from
  all providers (openai, anthropic, gemini + aliases) and surfaced in
  traces and the activity journal together with tool-loop iterations.
- Behavior page: inert `auto_index`/`log_writes` toggles removed; new
  `trace_keep`/`activity_keep` retention knobs (0 = keep all; pruning hooks
  wired, policy later).
- Fix: `edit_concept`/`deprecate_concept` no longer overwrite the
  frontmatter `generated.at` creation timestamp.
- Fix: in-flight activity rows are scoped to the owning user (no cross-user
  leak).

### 0.3.0

- Breaking MCP surface change (roadmap phase 3): `browse_library`
  removed — orientation is covered by `request_knowledge` plus the
  per-user library seed.
- `store_knowledge` narrowed to add-intent (new knowledge; the
  librarian may still enrich/back-link existing concepts internally).
- New tool `update_knowledge(instruction)`: free-text change requests
  (corrections, restructuring, deprecations); the librarian locates
  the target concepts itself. Returns `{stored: [...], summary}` like
  `store_knowledge`.
- Seed injection unchanged (`request_knowledge`/`store_knowledge`
  descriptions).

### 0.2.1

- Fix: librarian configuration caching — stale config no longer served after WebUI edits.

### 0.2.0

- MVP gap fixes from the post-implementation audit: `library_status` orphan
  shape brought in line with the pinned contract, lazy per-librarian
  `reconcile()` wired, expanded end-to-end and integration test coverage.

### 0.1.0

- Phase 1 MVP: one Python process serving the MCP server (FastMCP 3.x,
  Streamable HTTP at `/mcp`, bearer auth, seed injection), the per-user
  librarian agent (openai / anthropic / gemini / openrouter /
  openai-compatible), and the WebUI (FastAPI + Jinja2 + htmx + vis-network).
- Library core: OKF v0.2 bundle handling, `LibraryBackend` with compound
  writes (snapshot -> file -> index -> log), validator, shadow-copy
  versioning with rollback/diff.
- Multi-user with strict per-user isolation; per-user MCP bearer tokens.
- Single-stage Docker image; all durable state under one
  `ATHENAEUM_DATA_ROOT` volume.
