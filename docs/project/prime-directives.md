# Prime Directives

This document contains the non-negotiable architectural and correctness rules
for the athenaeum project. Every implementation — whether written by a human
or an agent — must comply with these directives. They override all other
guidance, conventions, and ad-hoc decisions.

Entries are numbered (`PD-1`, `PD-2`, ...) so they can be referenced in
discussions, plans, code reviews, and other documentation. New directives are
always appended; existing entries are not renumbered.

## PD-1: Environment variables are only for boot-critical configuration

**Only absolutely necessary things may be configured via Docker environment
variables; everything else is configured via the Admin UI.**

### Allowed via environment variables

Environment variables are restricted to what the container needs to boot and
locate its state. The complete, closed list (all with the `ATHENAEUM_`
prefix, defined in `src/athenaeum/config.py`) is:

- `ATHENAEUM_DATA_ROOT` — persistence root for the app database and user
  libraries.
- `ATHENAEUM_HOST` — bind address of the server.
- `ATHENAEUM_PORT` — bind port of the server.
- `ATHENAEUM_SECRET_KEY` — required; signs session cookies and derives the
  encryption key for secrets at rest.
- `ATHENAEUM_LOG_LEVEL` — logging verbosity.
- `ATHENAEUM_BOOTSTRAP_ADMIN_USERNAME` / `ATHENAEUM_BOOTSTRAP_ADMIN_PASSWORD`
  — optional, one-time pre-seed of the owner account; consumed only when the
  users table is empty and ignored afterwards.

### Everything else goes through the Admin WebUI

All other configuration is set exclusively through the Admin WebUI and stored
in the application database (SQLite under `ATHENAEUM_DATA_ROOT`). This
includes, without limitation:

- LLM provider selection, credentials (API keys), and model settings.
- Librarian behavior and tuning.
- Library settings.
- User accounts and permissions.
- MCP access tokens.

### Rule for future work

- Adding a new environment variable requires explicit justification of why it
  cannot be WebUI/DB configuration — i.e. why the application cannot boot or
  locate its state without it.
- Adding any new setting must default to WebUI + database storage, not to an
  environment variable.

---

*Further directives are appended below as PD-2, PD-3, ...*
