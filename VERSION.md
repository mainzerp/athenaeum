# Athenaeum — Version

Current version: **0.30.0**

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

### 0.30.0

Trace replay request/answer visibility:

- Trace files gain a top-level `request` field recording the original tool
  arguments (strings truncated to 2 KB via `MAX_REQUEST`; `store_knowledge`
  images as content-addressed refs, never raw bytes; `null` when never set).
  Recorded by the librarian handlers (`request_knowledge`, `store_knowledge`,
  `update_knowledge`) and the curator handlers (`library_maintain`,
  `library_curate`), including scheduler runs.
- The WebUI trace replay page renders the original request and the generated
  answer as guarded cards; the answer is markdown-rendered server-side (same
  renderer as the document view). Old traces without `request`/`answer`
  render unchanged; the `/api/traces/<id>` JSON keeps the raw markdown
  (`50f76f9`).

### 0.29.0

Document-view UI overhaul (`/library/tree` page), implemented as three
packages from the UI_REVIEW analysis:

- Interaction fixes (`35120b0`): Back/Forward now works for in-page document
  navigation (pushState/popstate; URL updates only after a successful load);
  Ctrl/Cmd/Shift+click on tree entries opens a native new tab again; failed
  folder expansions, document loads, and diff previews surface as toasts
  instead of failing silently (failed expansions are retryable without
  reload); loading indicators on the document card and expanders, with
  AbortController cancellation of superseded fetches; unsaved-edit guard
  (confirm on navigate-away); tab title follows the selected document;
  current tree entry gets a distinct background pill; client-side caches for
  document and diff payloads; valid list nesting in the tree (htmx
  `outerHTML` swap); empty folders show an `(empty)` placeholder.
- Tree rework (`4d5264a`): deep-linked documents are revealed in the tree —
  ancestor folders render expanded server-side and the selection scrolls
  into view (no programmatic expander clicks, per the ui-tree-rework
  lesson); folders collapse/re-expand on chevron or folder-name click with
  zero network requests after first load; accessibility pass
  (`aria-expanded`, labeled history slider, `aria-live` history label,
  labeled ticks with enlarged hit areas); sticky, independently scrollable
  tree pane; inline-diff strikethrough scoped to flow text (code/tables stay
  readable); code blocks use a light palette in light mode (Monokai stays in
  dark mode).
- Performance + hardening (`c04380c`): `list_dir` caches parsed frontmatter
  keyed by (mtime, size) — repeated tree loads/expansions no longer re-read
  every markdown file; inline-diff renders cached per (path, sha, HEAD);
  timeline payloads capped at 200 commits (`timeline_truncated` flag) and
  the tick row capped at ~60 dots; the doc-view minimap is clickable
  (GraphSunburst `onSelect`); relative `.md` links inside documents resolve
  in-page; htmx 2.0.4 is vendored (byte-verified against the previously
  pinned SRI hash) — no CDN dependency; `/api/graph/universe` supports
  ETag/`Cache-Control: no-cache` revalidation (304 on repeat loads);
  unexpected exceptions in edit/restore/delete redirect with a flash instead
  of a raw 500; the renderer escape invariant is pinned by a contract test.

### 0.28.0

OKF spec alignment (upstream 2026-08 change): every timestamp-valued key is
now an ISO 8601 datetime with an explicit UTC offset, no longer a date.

- `athenaeum/okf.py` `is_stale`: instant semantics (`now >= stale_after`);
  offset-less datetimes are ignored (they name a different instant per
  timezone); legacy date-only `stale_after` from pre-change bundles is read
  as midnight UTC so existing libraries keep working.
- `library/validate.py`: `stale_after`, `usage_window.{from,to}`,
  `generated.at`, `verified[].at` all require datetime-with-offset
  (`malformed-date` warning class, code name unchanged).
- `library/frontmatter.py`: custom SafeLoader drops the YAML 1.1 timestamp
  resolver — `2026-06-30T14:00:00Z` survives a parse/dump round-trip
  verbatim instead of being rewritten to PyYAML's `+00:00` form (same fix
  the OKF reference agent adopted).
- Librarian prompt and demo seed write datetimes with offset; spec links
  point at the new canonical repo `GoogleCloudPlatform/open-knowledge-format`
  (the knowledge-catalog `okf/` copy is a frozen snapshot).

### 0.27.1

F27 follow-up (`0d35696`): the relations graph (`/api/graph/universe`,
Sunburst view) no longer extracts display edges from markdown links inside
inline code spans or fenced code blocks. `_LINK_RE` extraction now runs
through the shared `iter_code_segments` guard (`_body_link_targets`), the
same segmentation contract as `library.links` — example markup in code no
longer draws phantom edges. Display-only change; contract test in
test_graph.py.

### 0.27.0

F28 (`d4db9b1`): `library_status` now exposes validation warning details.
The `health` dict gains two additive keys: `warnings_by_code` (complete
{code: count} breakdown over all warning classes) and `warning_items`
(itemized validate entries {path, code, message}, capped at
`MAX_STATUS_WARNINGS = 50` — counts always complete, only the list
truncates; both keys always present, `{}`/`[]` when empty). Previously
every non-graph warning collapsed into a bare count, so MCP callers could
not diagnose what the warnings were without WebUI or DB access. Four new
contract tests; docs: architecture.md result shape, lessons.md F28
resolved. Backward-compatible additive MCP contract extension.

### 0.26.2

F27 fix (`88f2665`): body-link extraction (`extract_body_links`) and the
`move_concept` rewrite path (`_rewrite_body`) now skip markdown links inside
inline code spans and fenced code blocks. Example markup in code (e.g. a
lessons doc demonstrating `[text](url)`) no longer counts as a concept link,
eliminating false-positive broken links that kept libraries permanently
unhealthy and forced a paid curator LLM run on every maintain schedule.
Extraction and rewrite stay symmetric (strip_link_targets pattern), so
move-driven link rewrites no longer corrupt example markup either. The
fence/span segmentation helpers moved from `escape_guard.py` into a new
shared module `library/md_spans.py` (with a new `iter_code_segments`
generator), breaking the escape_guard → links circular import structurally;
`escape_guard` re-exports the private names so existing consumers keep
working unchanged. Ten new contract tests pin extraction, rewrite symmetry,
warning suppression, and orphan semantics. Docs: lessons.md F27 resolved.

### 0.26.1

Iteration bundling rule (`7455535`): the agent prompts now pin that one
assistant response counts as one iteration and that independent tool calls
(e.g. several moves in a bulk restructure) belong batched in a single
response — closing F9, whose premise ("one tool call per iteration") was
outdated. `max_iterations` stays at its per-connection default of 10; a cap
raise and a batch-move tool were rejected on evidence. Docs corrections in
architecture.md (counting semantics) and lessons.md (F9 resolved).

### 0.26.0

Deterministic relates_to back-linking + answer hygiene guard (`f9d76da`):
store_knowledge/update_knowledge summaries could claim relates_to backlinks
that were never written (F22, six live confirmations) — back-linking is now
done deterministically server-side (`LibraryBackend.add_backlink`, with path
normalization, dedupe, and a Related-concepts append) before `links_after`
scans, and `update_knowledge` gains an optional `relates_to` parameter.
Results carry an additive `backlinked` field; the model is told the server
does the relates_to linking and must not claim it. Also fixes F21: a
deterministic dirty-answer detector (raw tool-call JSON, path blobs, EN/DE
process phrases; fenced code excluded) triggers one bounded no-tools re-ask
for request_knowledge answers, a second dirty answer is returned with a
marker instead of being dropped, and traces now record the final answer
(2 KB cap). `read_document` gains a `.md` fallback and a directory-aware
error. No breaking changes.

### 0.25.5

Librarian/curator split (`09eed84`): the curator is no longer a mode inside
the single `Librarian` class but a standalone `Curator` agent
(`src/athenaeum/curator/`) subclassing the shared `BaseAgent`
(`librarian/base.py`), with its own system prompt (`curator/prompts.py`) and
a narrowed 10-tool surface (`CURATOR_TOOL_SCHEMAS`, no `run_computation`).
The manager caches (Librarian, Curator) pairs sharing one backend/gate;
`library_curate`/`library_maintain` and the nightly scheduler drive the
curator via `get_curator()`. Pure refactor with no user-facing change: MCP
tool names and result shapes, config keys (`curator_connection_id` /
`curator_model`), RunGate kind strings, `CURATOR_VERIFIER`, and re-baseline
semantics are unchanged; the librarian `DEFAULT_SYSTEM_PROMPT` is
byte-identical.

### 0.25.4

WebUI embedding model switch now downloads the new model and re-embeds the
library in the background immediately on save, instead of deferring both to
the first agent request (which blocked on the ~2.2 GB fastembed download for
local models). The save handler compares the old and new `(source, model)`
binding and, on a real change, spawns a fire-and-forget task that cold-builds
the librarian off the event loop and triggers its deferred reconcile — reusing
the existing claim, status registry, and eviction-cancel machinery. Plain
re-saves (unchanged binding, threshold/hybrid-only changes, disables) only
evict as before.

### 0.25.3

Embedding shortlist correction: `intfloat/multilingual-e5-base` and
`-e5-small` were never loadable via fastembed 0.8.0 (live failure observed);
the shortlist now leads with `intfloat/multilingual-e5-large` (1024 dims, the
only fastembed-supported e5-family model) as the default and drops both
unsupported entries. Stored off-shortlist local models now fail embed calls
with an explicit `EmbeddingProviderError` pointing at WebUI reconfiguration
(Agents > Embeddings) instead of a raw fastembed error. Empty metadata
fallback in `search_semantic` now raises a RuntimeError chaining the
embedding-pipeline cause instead of silently returning `[]` — an empty result
is no longer indistinguishable from a broken embedding pipeline, and the
trace records an error event rather than a misleading `fallback: false`.
Manual step for deployments holding e5-base/e5-small: reconfigure the
embedding model in the WebUI and re-index (e5-large downloads ~2.24 GB on
first use).

### 0.25.2

Link-stripped indexing text (`c8b7cb2`): `concept_text` now reduces inline markdown
links `[text](url)` to their anchor text before embedding/FTS indexing
(`strip_link_targets`), so URLs no longer pollute vectors or the lexical
index. The transform is fence- and code-span-aware (links inside fenced code
blocks or inline code spans pass through untouched); images, reference-style
links, autolinks, and bare URLs are untouched by design. Deploying this
change makes every stored `content_hash` stale: the first librarian
reconcile after restart triggers a one-time full re-embed per user (batched;
API providers incur one corpus-sized token bill) so vectors match the new
text basis.

### 0.25.1

Sidebar version placement (`a174675`): the version badge moved from a stacked
line above the user card into the footer row as its own column
(avatar | user meta | version | theme toggle).

### 0.25.0

Per-step LLM timing in traces (`2092656`): each `provider.complete` wall time
is recorded and attached as `llm_ms` to the first tool event of the hop
(multi-tool responses do not double-count); the aggregate `llm_ms_total` rides
in the trace's `llm` telemetry dict. The trace replay page shows a per-step
`+ LLM x ms` span and a total badge; old traces without the fields render
unchanged. Sidebar version display (`31077ce`): the WebUI sidebar footer now
shows the running app version above the user card, so the deployed build is
visible at a glance.

### 0.24.2

Store-watchdog and path suggestions (`d25c354`), from live-trace analysis of a
store run that burned all iterations on duplicate searches and path typos
without ever writing: write tasks (`store_knowledge`/`update_knowledge`) now
get a one-time imperative user-message nudge to write immediately when only 2
iterations remain and no write has landed yet (retrieval/curator paths never
nudge; the cap-exit final-answer flow is unchanged). `read_document` and
`list_dir` FileNotFoundError messages now include `difflib` close-match
suggestions from the symlink-screened library tree (bounded at 2000
candidates; plain messages stay byte-identical when nothing matches; path
escapes still raise without suggestions).

### 0.24.1

Librarian agent-loop guards (`0bb969f`): deterministic dedupe of repeated
retrieval calls in `_dispatch_tracked` — exact match on (tool, args) for
`list_dir`/`read_document`/`search_metadata`/`search_semantic` plus fuzzy
token-Jaccard (>= 0.8, TUNABLE `SEMANTIC_QUERY_SIMILARITY_THRESHOLD`) on
`search_semantic.query`. Suppressed duplicates return a recoverable
`{"deduplicated": true, ...}` result and stay trace-visible via their own
trace event; failed calls may retry, writes/`link_check`/`run_computation`
are never deduped. Every tool message now carries a
`[budget: N iterations remaining]` prefix so the model can pace itself before
the `max_iterations` cap. Prompt contract extended ("DUPLICATE CALLS ARE
REJECTED" + budget marker bullet, pinned in `tests/test_prompts.py`).
Motivation: live traces showed `request_knowledge` always burning all 10
iterations (~83-96k tokens) on rephrased searches and re-reads. Also adds
live-instance debug fetch tooling (`scripts/athenaeum_debug.py`,
`5a9516e`).

### 0.24.0

Add `intfloat/multilingual-e5-base` (768 dims) to the local embedding model
shortlist as the new default (`47315ea`); existing shortlist entries remain
selectable, E5 prefix handling applies automatically, and the semantic
duplicate threshold falls back to the conservative 0.85 default until live
calibration. Existing per-user configs are unchanged; switching models in the
WebUI triggers the background re-embed as before.

> Entries for 0.1.0–0.2.1 were recorded retroactively; the repository was not
> yet under version control, so no commit hashes are available.

### 0.23.1

Deep-review 2026-08 security and robustness patch (36 findings): import/export
hardening (LIBRARY-01, SERVER-01, SERVER-06); symlink screening of the scan
layer (LIBRARY-02); LLM adapter turn-structure merging (AGENT-01, AGENT-02);
frontmatter tolerance + graph containment (LIBRARY-05, LIBRARY-12, SERVER-05);
async offload + trace/embedding error containment (AGENT-03, AGENT-04,
LIBRARY-07); provenance/sanitization guards (LIBRARY-03, LIBRARY-04,
LIBRARY-06, LIBRARY-10, LIBRARY-11); login/auth hardening (SERVER-02,
SERVER-04, SERVER-08, SERVER-09, SERVER-13); secret-key validation +
decryption error surfacing (SERVER-03, AGENT-10); curator partial-success +
verification/telemetry (AGENT-05, AGENT-06, AGENT-11); provider error
classification (AGENT-07, AGENT-08, AGENT-09); misc robustness (LIBRARY-08,
LIBRARY-09, SERVER-07, SERVER-10, SERVER-11, SERVER-12).

### 0.23.0

Document Time Machine rework: the per-document slider is reversed to
oldest-left/newest-right (rightmost stop = live view) and moving it shows a
reload-free red/green preview diff of the selected commit vs HEAD
(rename-aware `git diff <sha> HEAD`, served by the new read-only JSON
endpoint `GET /library/document/diff`); restore stays the explicit
append-only per-file restore. Document bodies now render server-side
(markdown-it-py + mdit-py-plugins tasklists + Pygments; raw HTML escaped),
replacing the marked/DOMPurify CDN pipeline.

Search performance rework: `search_semantic` no longer reads the whole
embeddings table per query (in-memory vector cache with in-place invalidation
on upsert/delete), ranks with a NumPy-vectorized cosine (stdlib fallback),
and offloads the vector scan and candidate hydration off the event loop;
per-stage DEBUG timings (query embedding, vector scan, fts/semantic legs,
hydration, rerank). The cross-encoder rerank is cheaper when enabled
(10 candidates instead of 30, candidate texts capped at 2000 chars) and now
**defaults to off** (`hybrid_rerank` DB/agent/backend default 0; measured:
the rerank pass alone cost ~3.5-4 s per search on CPU while the RRF-fused
hybrid search answers in ~0.4 s; existing per-user settings are unchanged).
Local ONNX embedding models now live in a process-wide cache (survives the
30-min librarian idle eviction) and are preloaded at app start by a
background task (never blocks startup, logs loaded/failed models), so even
the first search after a container start skips the ~1 s model load.

Curate observability: `library_curate`'s `health_after` now reports
`broken_links` alongside `healthy`/`orphans` (post-run scan, both the no-op
and the LLM path), and the WebUI curator tab shows the scheduled-curation
state (active/inactive, stored UTC time, last run timestamp) so a silent
schedule is visible without DB or log access. The curator tab also gains a
manual "Run now" button that starts a background curate run (incl. the F25
hygiene sweep), journaled on the Activity page as `webui`.

Write-path escape guard: `create_concept`/`edit_concept` auto-decode literal
`\uXXXX` escape artifacts in concept bodies outside code spans/fenced blocks
at write time (F25); the decode is reported as a `warnings` entry in the
write tool result, and a body-less edit never rescans the existing body.
Escapes inside code spans/fenced blocks stay byte-untouched but are no
longer silently exempt: the write result warns with the exact line/snippet
locations, and the new optional `allow_literal_escapes: true` argument on
`write_concept`/`edit_concept` is the stateless resubmit confirmation that
suppresses the warning (prose escapes keep decoding regardless). The
curator repairs the dirty stock deterministically: `library_curate` runs
a content-hygiene sweep before its findings scan that decodes leftover
literal escapes in existing bodies via `edit_concept` (surfaced as `updated`
actions entries; never triggers the LLM by itself). Existing literals inside
code spans/fences surface as the new `code_span_escape_candidates` curate
finding — the curator LLM judges each listed occurrence (artifact → repair
with real characters; intentional documentation → leave unchanged) and
confirmed-intentional literals are re-reported on later runs until fixed.

WebUI graph/activity rework: the library tree and document pages are merged
into a single document view with a minimap for quick navigation and the
inline time-machine diff; the trace replay graph is rebuilt on the sunburst
view (the vendored graph3d bundle, ~5.4k lines, is removed) with index.md
trace hops routed through the sunburst core, and the standalone traces list
is folded into the Activity page. Activity timestamps render in local time,
agent/connection labels identify the originating client, and the page footer
is dropped. Dependency security: cryptography bumped to 50.0.0
(PYSEC-2026-3552).

Commits: `a24712a` fix(ui), `fc19b4b` feat(webui), `13a8738` feat(webui),
`90d2cde` fix(deps), `ab63b04` fix(search), `6614d1e` docs(readme),
`28c4ae2` feat(curate), `19f0eff` feat, `4c05d44` feat(webui), `295047d`
feat(webui).

### 0.22.0

Git Time Machine: the shadow-copy snapshot versioning is replaced by a real
git history in every library bundle — per-write auto-commits, per-document
timelines with per-file restore and a library-wide history section in the
WebUI (per-commit diffs, revert, undoable reset slider), optional remote
push/pull, and Docker/compose hardening. (Commits: `5099f05` feat(library),
`1853600` docs(lessons).)

- **Git history backend (PART 1):** every compound write (and asset store)
  ends in a best-effort `git` auto-commit with a deterministic message
  mirroring the log.md entry (`Creation: Created [X](/x.md). (requested by
  agent:<label>)`); git failures never break a write. History lives in the
  bundle's own repository (`git init -b main`, repo-local identity, library
  `.gitignore` excluding `.athenaeum/versions/`, `.athenaeum/payloads/`,
  `.traces/`, and atomic-write temp files). `snapshots.py` and the
  `versioning`/`snapshot_keep` settings are retired (dead-legacy-column
  pattern); `status()["stats"]["versions"]` now reports the commit count.
- **Revert / append-only reset:** undo one commit via `git revert
  --no-commit` + one recording commit; reset to any earlier commit via
  `git read-tree --reset -u` + one recording commit, so the pre-reset state
  stays reachable as the reset commit's parent (undoable, fast-forward
  pushable). Reverting the root commit is refused.
- **Config chain:** new `librarian_configs` columns `git_enabled` (default
  on), `git_remote_url`, `git_auto_push` (default off), wired through db.py,
  the librarian manager/agent, `deps.get_library_backend`, and the Library
  settings form. Push runs post-commit (`GIT_TERMINAL_PROMPT=0`); pull is an
  explicit action (`git pull --ff-only`).
- **History WebUI (PART 2 + DOC_TIMELINE):** the old Versions pages are
  replaced by git history in the Library — every document page carries a
  History card (a slider over that file's commits via rename-aware per-file
  git ops, a read-only historical view with its diff, and "Restore this
  version": a per-file restore recorded as one new compound-write commit;
  no-op and file-absent-at-commit restores are refused), and the tree page
  carries the library-wide section (commit list, per-commit diff view at
  `/library/diff`, revert buttons, reset slider, pull button when a remote
  is configured). The standalone Time-Machine page is folded in; legacy
  `/library/time-machine` URLs 301-redirect. Restore and pull evict the
  cached librarian so embeddings/FTS reconcile on the next agent entry.
- **ZIP coexistence (PART 2):** exports exclude `.git` and legacy
  `.athenaeum/versions/`; imports reject archives containing `.git` members
  (hook-injection guard) and re-initialize git history after the swap.
- **Docker/compose hardening + path confinement (PART 3):** git binary in
  the container image; `resolve_under` confinement in links/validate;
  `user_id` path-segment validation; sqlite `dbname` URI-metacharacter
  rejection for runtime connections. Compose: read-only rootfs + `tmpfs /tmp`,
  `cap_drop: ALL`, `no-new-privileges`, CPU/RAM/PID limits; base image pinned
  (`python:3.12.13-slim`), container user pinned to UID/GID 10001.
- **Upgrade note (existing volumes):** the pinned UID 10001 replaces the old
  unpinned UID 1000, so an existing data volume is not writable by the new
  container (sqlite "attempt to write a readonly database", restart loop).
  One-time fix: `docker run --rm --user root -v <project>_athenaeum-data:/data
  athenaeum-athenaeum chown -R 10001:10001 /data`, then `docker compose up -d`.
  Fresh volumes are unaffected.

### 0.21.0

Trust surface: deterministic curator verification (trust tiers), durable
librarian provenance, and sandboxed Attested Computation execution behind an
admin toggle. (Commits: `05273c9` feat(okf), `f4207d1` docs(lessons) —
dogfooding findings F21/F22.)

- **Curator trust tiers:** after every maintain/curate run (interactive or
  nightly scheduler), a deterministic post-step machine-confirms exactly the
  concepts the curator repaired (`updated` writes only) by appending
  `verified: [{by: athenaeum-curator/<version>, at}]` via the new
  `LibraryBackend.verify_concept` — the sole writer of `verified`
  (append-only, snapshot-covered, no `generated.at` refresh; `create_concept`
  strips a caller-supplied `verified`). Maintain/curate responses gain a
  `verified` receipt list and the summary a "Post-run verification" line.
  New validator warning `verified-missing-by` (9 warning classes now).
- **Librarian provenance:** MCP-chat `store_knowledge`/`update_knowledge`
  writes record durable `generated.requested_by: human:<username>` +
  `generated.via: mcp_chat` (the local account that issued the token);
  `generated.by` stays `athenaeum-librarian/<version>`. Edits/deprecations
  preserve the sub-keys unless a new requester is supplied; a caller-forged
  `generated` mapping is replaced wholesale at creation. The log.md
  `(requested by agent:<label>)` suffix remains the integration audit record.
- **Attested Computations (v1):** `type: Attested Computation` concepts with
  `runtime: postgres|sqlite` are executable — new external MCP tool
  `run_computation` (7 external tools now) and a same-named internal
  librarian-loop tool (11 internal tools; deliberately not a write action).
  Sandbox: admin execution toggle (default off, read live per execution),
  admin-managed shared `runtime_connections` (Fernet-encrypted write-only
  passwords, new Admin WebUI page), single read-only `SELECT`/`WITH`
  statement with driver-bound parameters, READ ONLY transaction / `mode=ro`
  enforcement, statement timeout, fixed row cap (500). Receipts
  (`columns`/`rows`/`row_count`/`truncated`/`duration_ms`/`executed_at`) are
  returned to the caller only — never written back into frontmatter.
  New dependency: `psycopg[binary]` 3.x (pinned 3.3.4).

### 0.20.0

Store-surface extensions: contradiction warnings, deletion-via-deprecate
with hidden-deprecated semantics, a raw-payload archive for
`store_knowledge`, and base64 images stored as hidden library assets.
(Commits: `c9c676f` feat(store), `e251560` ci(docker) — GHCR image build +
prebuilt compose.)

- **Contradiction warnings (store-only):** when the librarian supersedes
  contradictions in place (write-discipline rule 3), the STORE task now
  requires a `## Contradictions` summary section (`- <concept id>: <note>`
  per resolved contradiction, or `- none`). `store_knowledge` results gain
  an optional `contradictions: [{id, note}]` field — LLM-reported,
  deterministically cross-checked against the tracked writes (only
  `updated`/`deprecated` writes on the reported id count), present only
  when non-empty, with a deterministic "Post-run check" verdict appended
  after the links verdict. The channel is embeddings-independent and works
  in degraded mode (embeddings only add candidate recall). The
  `update_knowledge` contract is untouched.
- **Deletion via deprecate:** update tasks route delete requests ALWAYS to
  `deprecate_concept`, never `delete_concept` (curator-only, prompt-level
  pin). Deprecated concepts are now hidden from knowledge consumers: the
  library seed's Concepts section, embedding/FTS indexes (a deprecation
  deletes the row like a deletion; reconcile drops stale rows), semantic
  and near-duplicate candidate passes, and the orphan warning (deprecated
  concepts are never orphan-reported, so they no longer force paid
  maintain runs — edges still count and `stats.concepts` still counts the
  files). `library_curate` gains a 6th finding kind `deprecated_cleanup`:
  deprecated concepts with zero inbound links from non-deprecated concepts
  are listed for the curator to `delete_concept` (live inbound links keep
  a deprecated concept unlisted — never a permanent re-reported finding).
  The answering prompt pins: deprecated concepts are pending removal —
  never cite, never enrich.
- **Raw-payload archive:** every `store_knowledge` call is archived
  two-phase under `<library>/.athenaeum/payloads/<request_id>.json`
  (`received` on entry — busy rejections included — rewritten with the
  final `ok`/`partial`/`error`/`busy` outcome and stored entries on exit;
  best-effort, never fails the store). Image params are archived as
  content-addressed refs only. Retention via the new `payload_keep`
  config column (PD-1: bounded default 100 for new installs/config rows,
  existing rows migrate to 0 = keep all; WebUI library page). Failed or
  partial payloads surface as a 7th curate finding kind
  `store_payload_reviews` — a bounded digest (5 newest, 120-char excerpts)
  reported once since the previous curate run and never re-reported; a
  payload-only finding wakes exactly one paid curate run.
- **Base64 images → hidden asset store:** `store_knowledge` gains an
  optional `images` param (`[{filename, media_type, data_base64}]`; max 5
  images, 5 MiB decoded each, png/jpeg/gif/webp). Bytes are written
  server-side to `.athenaeum/assets/<sha256[:12]>-<filename>`
  (content-addressed, idempotent; outside the compound write — no
  snapshot/index/log) and reach the prompt only as absolute markdown
  image links; base64 never enters the prompt or the activity journal
  (sha-ref sanitization). Markdown image syntax is no longer parsed as a
  concept link (`LINK_RE` negative lookbehind): assets are not graph
  citizens — no edges, no broken-link warnings, no move-rewrite.

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
- Fix (graph): the sunburst root ring is now drawn even when the library has
  no root-level documents (previously the center boundary was only rendered
  with `rootCount > 0`, leaving the sector dividers visually unanchored).

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
