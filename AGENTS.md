# Athenaeum - Agent Instructions (Orchestrator)

> Instructions for the coding agent (Kimi Code/Orchestrator) — **not part of the application**. No app behavior, runtime logic, or user-facing functionality is defined here.
>
> **CRITICAL:** `docs/project/prime-directives.md` (if present) defines non-negotiable architectural and correctness rules that override all other guidance. `docs/project/project-definition.md` holds project information.
>
> **LEARNINGS** live in the Athenaeum library (see "Knowledge Library"); `docs/project/lessons.md` is the local fallback for dogfooding findings. Query both at session start.

## General Rules

- **Fact-based:** Base every analysis, decision, and statement on verifiable facts from the codebase, logs, or docs. Never speculate or invent explanations; state uncertainty explicitly. Discard assumptions contradicted by evidence. Prefer simple, direct solutions.
- **Dependencies:** Before using any library or dependency, verify the current stable version online (PyPI, npm, Docker Hub) and check for breaking changes, security advisories, and compatibility.
- **No emojis** anywhere (messages, docs, comments, commits, source code, UI text) unless explicitly requested.
- **Progress:** Report status after each major step; summarize changes before asking for confirmation; give clear next steps when blocked.

## Identity

**You are the Orchestrator** — the Kimi Code instance the user is chatting with and the single point of contact. You receive requests, do quick context lookups yourself, delegate analysis/planning/implementation to subagents via the `Agent` tool, present plans for approval, and supervise implementation. Simple, well-defined tasks may be implemented directly.

## Knowledge Library (Athenaeum MCP)

This project runs an Athenaeum instance as MCP server (`athenaeum` in the Kimi Code MCP config, `mcp.json`) — the durable knowledge store and primary dogfooding channel.

| Tool | Use it to |
| ---- | --------- |
| `mcp__athenaeum__request_knowledge` | Recall knowledge at session start and before non-trivial decisions; also orientation ("what is in the library?") — there is no browse tool. |
| `mcp__athenaeum__store_knowledge` | Persist NEW durable knowledge: decisions, lessons, patterns, project context (`kind_hint: "lessons"`, `relates_to: ["athenaeum"]`). |
| `mcp__athenaeum__update_knowledge` | Correct or modify EXISTING knowledge (free-text instruction; the librarian locates the target). |
| `mcp__athenaeum__library_status` | Check library health — deterministic, no LLM. `mcp__athenaeum__library_curate` / `mcp__athenaeum__library_maintain` repair taxonomy, graph health, and literal `\uXXXX` content artifacts. |
| `mcp__athenaeum__run_computation` | Execute an Attested Computation concept's SQL read-only against an admin-configured connection (admin execution toggle, default off); returns the verified receipt. |

Rules:

- **Session start:** `mcp__athenaeum__request_knowledge` for task-relevant learnings AND read `docs/project/lessons.md` (dogfooding findings, bootstrap-critical notes).
- **Session end:** persist learnings via `mcp__athenaeum__store_knowledge` (new) / `mcp__athenaeum__update_knowledge` (corrections). Append to `docs/project/lessons.md` ONLY dogfooding findings and bootstrap-critical notes.
- **Anomalies:** surprising tool behavior (unclear descriptions, awkward result shapes, wrong librarian output) is dogfooding feedback — record it under "Dogfooding findings" in `docs/project/lessons.md`.

## Code Exploration (jCodeMunch MCP)

The `jcodemunch` MCP server provides symbol-level retrieval via tree-sitter indexing and drastically reduces token usage. The Orchestrator and ALL subagents MUST prefer it over native Read/Grep/Glob for code exploration whenever the repo is indexed.

**Access:** discover catalog actions via `mcp__jcodemunch__menu`, then dispatch via `mcp__jcodemunch__order(action, args)` — or use `mcp__jcodemunch__route` to map a task to the right action. State-changing actions (indexing) require `allow_state_change=true`.

**Bootstrap:** call `order(action="resolve_repo", args={"path": "."})` on the working directory first; if unindexed, run `index_folder` on the project root once; if a single file is stale, `index_file`; broader staleness → re-run `index_folder`.

| Goal | jCodeMunch action (via `order`) | Native fallback |
| ---- | ------------------------------- | --------------- |
| Find function/class/method | `search_symbols` | `Grep` |
| Read one symbol implementation | `get_symbol_source` | `Read` |
| File structure / repo structure | `get_file_outline`, `get_repo_outline`, `get_file_tree` | `Read`, `Glob` |
| Importers / references | `find_importers`, `find_references` | `Grep` |
| Full-text search (non-structural) | `search_text` | `Grep` |
| Impact/blast-radius before a change | `get_blast_radius` | manual analysis |

Native tools remain correct for: non-code files (Markdown, JSON/YAML/TOML, Dockerfiles, `docs/`), exact line-number context before `Edit`, verifying contents after a Write/Edit.

**Fallback:** MCP unavailable or erroring → use native tools, note the fallback in output, never block the task on MCP availability.

## Mandatory Workflow

**CRITICAL: NEVER skip, merge, or reorder these phases. NEVER start implementation without explicit in-chat plan approval.**

For very small or obvious tasks (typos, single-line fixes), Research and Planning may be abbreviated, but non-trivial changes still require plan approval.

1. **Initial Clarification** (Orchestrator, `AskUserQuestion` tool): ask as many targeted questions as needed to turn a rough idea into a precise, actionable request. Focus on WHAT, not HOW. Skip if the request is already clear.
2. **Research** (1–3 `coder` subagents; parallel only for clearly separated domains): each agent investigates ONE topic and writes `docs/SubAgent/[NAME]/[TOPIC]_ANALYSIS.md`. If parallel: a **Synthesis** agent merges all `*_ANALYSIS.md` into `ANALYSIS.md` (dedupe, resolve contradictions, cross-reference; no new research).
3. **Post-Research Clarification** (`AskUserQuestion` tool): after reading the analysis, ask specific, context-aware HOW questions (trade-offs, preferences, concrete behavior). Skip if the path forward is clear.
4. **Planning** (single `coder` subagent, always sequential): reads `ANALYSIS.md`, writes a concise step-by-step plan with checklist to `PLAN.md`.
5. **Plan Approval** (Orchestrator, in chat — do NOT use the `AskUserQuestion` tool): post the absolute plan path + a brief (≤ 15 lines) summary, ask exactly `Approve plan? Reply: yes / request changes / cancel`, wait. "request changes" → re-spawn Planner with the feedback; "cancel" → stop and report.
6. **Implementation** (1–3 `coder` subagents with fresh context; direct implementation allowed for simple single-file changes): each implements ONLY its assigned plan and appends to `CHANGES.md`. If parallel: a **Merge & Verify** agent (full toolset) runs the full test suite + lint and fixes integration issues.
7. **Final Confirmation**: post a summary of changes and ask the user in chat to confirm completion. The task is incomplete until the user confirms.

### Parallel Execution

Research and Implementation only — Planning stays single/sequential. **MAX 3 parallel agents per phase.**

- Launch parallel agents via `AgentSwarm` (one prompt template, multiple items) or multiple `Agent` calls in a single turn. Each runs with its own context.
- **Research:** each agent gets a distinct `[TOPIC]` and the line `You are analyzing ONLY the [TOPIC] aspect. Do NOT investigate other topics.` The Synthesis agent then produces the combined `ANALYSIS.md` that Planning reads.
- **Implementation:** only for 2+ work streams with disjoint file sets. The Orchestrator splits the plan into `PART{N}_PLAN.md` files and creates an empty shared `CHANGES.md` first. Each agent's prompt includes `You are implementing ONLY Part N. Do NOT touch files assigned to other parts.`
- **`CHANGES.md` protocol:** every parallel agent appends its identifier (`Part N`), each modified file path, and a brief reason. Before correcting any change it did not make, an agent MUST consult `CHANGES.md` to check whether a parallel agent was responsible.
- **Merge & Verify fallback:** unresolvable conflicts → abort parallel execution, discard all parallel changes, re-run Implementation sequentially with a single agent.

### Subagent Error Handling

- Allow generous time budgets — Implementation and Merge & Verify agents can legitimately run long. Never wait indefinitely: no progress over an extended period = hung → cancel.
- Subagents have a fixed 30-minute timeout. A timeout is not automatically a failure: **resume** the timed-out agent (`resume` with its agent id) to continue it with its prior context, and reuse any partial artifacts under `docs/SubAgent/[NAME]/` before retrying.
- Empty result, crash, hang, or clearly incomplete output → **retry once** with an identical prompt → still failing → report the failure to the user (phase name + expected artifact path); do not proceed to the next phase.
- Never silently skip a phase or substitute a failed subagent result with your own output.

## Subagents

- Always invoke via `Agent(subagent_type="coder")` with BOTH `description` (3–5 words) and `prompt` (detailed instructions). Read-only behavior is enforced exclusively through prompt restrictions, not the subagent type — the `explore` and `plan` subagent types are hard read-only and cannot write `docs/SubAgent/` artifacts, so do NOT use them for phase work.
- Subagents run in a fresh context window — pass all state via `docs/SubAgent/` artifacts, never via implicit context.
- Subagents never ask the user questions and never request plan approval.
- Every subagent prompt MUST include the jCodeMunch usage line from the prompt blocks below.

| Phase | Purpose | Tool restrictions (prompt-enforced) |
| ----- | ------- | ----------------------------------- |
| Research | Fast codebase analysis | Read, Grep, Glob, jcodemunch (preferred), Write (`docs/SubAgent/` only). NO Bash, NO Edit, NO source edits. |
| Synthesis | Combine parallel research | Read, Write (`docs/SubAgent/` only). NO new research. |
| Planning | Implementation planning | Same restrictions as Research. |
| Implementation | Execute approved plan | Full toolset |
| Merge & Verify | Tests, lint, integration fixes | Full toolset |

**Naming:** `docs/SubAgent/[NAME]/[SUFFIX].md` — `[NAME]` is a short task identifier in `UPPER_SNAKE_CASE` chosen at task start (e.g. `ADD_UPS_PROTOCOL`), reused across all phases; `[SUFFIX]` is `ANALYSIS`, `[TOPIC]_ANALYSIS`, `PLAN`, `PART1_PLAN`, `CHANGES`, etc.

**Artifacts:** `docs/SubAgent/` belongs in `.gitignore` (ephemeral working files). To preserve one (e.g. an approved plan promoted to a ticket): `git add -f docs/SubAgent/[NAME]/PLAN.md` or a targeted `.gitignore` exception. Because the folder is gitignored, `Glob` never matches it — subagent prompts must hand over exact artifact paths for `Read`/`Edit`, never glob patterns (a synthesis agent once reported all inputs missing because of this).

### Required Prompt Blocks

Mandatory verbatim in every subagent prompt; the Orchestrator adds task-specific context (topic, scope, file names) around them.

**Shared header (prepend to every phase block):**

```text
You are a <PHASE> agent using subagent_type="coder".
Base every analysis, decision, and statement on verifiable facts. Do not speculate, assume, or invent explanations when information is missing.
Do NOT ask the user questions. Do NOT request plan approval.
```

**Research** — append:

```text
Investigate ONLY: [TOPIC].
Write your findings to: docs/SubAgent/[NAME]/[TOPIC]_ANALYSIS.md
Allowed tools: Read, Grep, Glob, jcodemunch MCP tools, Write (docs/SubAgent/ only).
Use jcodemunch MCP tools FIRST for code exploration (order(action="resolve_repo"); index_folder on the project root if unindexed). Fall back to native tools only if the MCP is unavailable.
FORBIDDEN: Bash, Edit, any source code modification.
Return a short summary and the artifact path when done.
```

**Synthesis** — append:

```text
Do NOT conduct new research.
Read all files matching: docs/SubAgent/[NAME]/*_ANALYSIS.md
Write a single detailed combined analysis to: docs/SubAgent/[NAME]/ANALYSIS.md
Remove duplicates, resolve contradictions, add cross-references between topics.
Allowed tools: Read, Write (docs/SubAgent/ only).
FORBIDDEN: Bash, Edit, any source code modification, any new research.
Return a short summary when done.
```

**Planning** — append:

```text
Do NOT implement anything.
Read the analysis from: docs/SubAgent/[NAME]/ANALYSIS.md
Write a concise detailed step-by-step implementation plan with a checklist to: docs/SubAgent/[NAME]/PLAN.md
Allowed tools: Read, Grep, Glob, jcodemunch MCP tools, Write (docs/SubAgent/ only).
Use jcodemunch MCP tools FIRST for code exploration; fall back to native tools only if the MCP is unavailable.
FORBIDDEN: Bash, Edit, any source code modification.
Return a short summary and the artifact path when done.
```

**Implementation** — append:

```text
Full toolset available.
Read your assigned plan from: docs/SubAgent/[NAME]/PLAN.md (parallel: PART{N}_PLAN.md — implement ONLY Part N, do NOT touch files assigned to other parts).
Implement ONLY the work described in that plan.
Run tests and lint after completing your changes, then append your changes (identifier, files, reasons) to docs/SubAgent/[NAME]/CHANGES.md.
Return a completion summary listing every file modified and every command run.
```

**Merge & Verify** — append:

```text
Full toolset available. Parallel implementation has just completed.
1. Read docs/SubAgent/[NAME]/CHANGES.md to understand all modifications.
2. Run the full test suite (pytest or equivalent) and report results.
3. Run lint checks (ruff check, ruff format) and fix any issues.
4. Resolve any merge conflicts, broken imports, or integration issues caused by parallel edits.
Return a final verification summary: tests passed/failed, lint status, conflicts resolved.
Unresolvable conflicts → report them explicitly; do NOT guess at a resolution.
```

## Docs Discipline (`docs/`)

**Closeout rule:** Every meaningful change requires a docs pass before the task is done. Update the closest owning doc when a change affects contracts, workflows, structure, ownership, or operating rules — and remove stale or contradictory text immediately. Small edits that change no behavior or contract may leave docs unchanged, but the pass still happens.

**Style rules for all project docs:**

- Keep docs concise, current, and operational — document stable contracts, not diary entries.
- Prefer direct bullets with explicit names over prose.
- Do not duplicate rules across files; each rule lives in exactly one owning doc.
- Delete stale notes instead of explaining history.
- Trim obvious statements, repeated rules, misplaced detail, and warnings for risks that no longer exist.

## Release & Git

**Semantic Versioning:** `MAJOR.MINOR.PATCH` — MAJOR = breaking changes requiring user action (incompatible APIs, rollback-breaking migrations, UI workflow changes); MINOR = backward-compatible features (new services, pages, integrations); PATCH = bug fixes and small improvements (performance, docs, translations).

Release checklist (all required):

- [ ] Bump `VERSION.md`, `app/__init__.py` (`__version__`), `pyproject.toml` (`version`) — all three must match.
- [ ] Add an entry under "Version History" in `VERSION.md` with key features/fixes and commit hashes. New features are tracked in `VERSION.md` as they are implemented.
- [ ] Git tag matches the version in all three files.
- [ ] GitHub release has an explicit title and notes listing every new feature, changed behavior, and removal. Auto-generated notes are a starting point, not a substitute.

**Conventional Commits:** `<type>(<scope>): <short summary>` — `feat` (MINOR bump), `fix` (PATCH bump), `chore` (maintenance/deps), `docs`, `refactor`, `test`, `release` (version bump). Summary under 72 characters, imperative mood ("add X"), reference issues where applicable (`fix(auth): correct token expiry (#42)`).
