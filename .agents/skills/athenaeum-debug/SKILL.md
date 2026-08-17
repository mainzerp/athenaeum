---
name: athenaeum-debug
description: Fetch activity journal entries and LLM traces from the live Athenaeum instance (athenaeum.mzrsvr.net) for debugging. Use when the user asks to debug the live instance, inspect recent tool calls, check for errors, or retrieve a trace by id.
---

# Athenaeum Live Debugging

Fetch logs/traces from the running Athenaeum instance via its WebUI endpoints
(session-cookie auth, handled by the script). Credentials live in
`.secrets/athenaeum-live.json` (gitignored) — never print the password, never
copy it into chat, docs, or commits.

## Usage

All commands run from the repo root (`f:/Github/athenaeum`), stdlib-only:

```bash
# Which version is live? (unauthenticated)
python scripts/athenaeum_debug.py version

# Recent tool calls: in-flight + journal (tool, outcome, duration, tokens, trace id)
python scripts/athenaeum_debug.py activity --limit 20

# Only failures
python scripts/athenaeum_debug.py activity --errors

# Full trace JSON for one call (trace_id from the activity listing)
python scripts/athenaeum_debug.py trace <TRACE_ID> --out /tmp/trace.json
```

## Notes

- Only agent-backed tools produce traces: `request_knowledge`, `store_knowledge`,
  `update_knowledge`, `library_maintain`, `library_curate`. No-op runs journal a
  row but write no trace file (`trace_id` shows `-` then).
- There is no server-log endpoint; app/uvicorn logs need deployment access
  (`docker logs`). This script covers the per-call journal + traces only.
- Trace JSON shape: `{path, score}`-style retrieval hops plus LLM metadata
  (iterations, tokens); inspect `events` for the step-by-step agent loop.
- Login failures re-render the login page — the script reports that as
  "login failed (bad credentials or lockout)"; repeated failures trigger a
  server-side lockout, so do not brute-force retry.
