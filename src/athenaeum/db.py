"""SQLite persistence: users, librarian configs, MCP tokens.

Schema pinned in plan §3.5 (idempotent DDL). One app DB at
``<ATHENAEUM_DATA_ROOT>/app.db``; per-user OKF bundles live at
``<ATHENAEUM_DATA_ROOT>/users/<user_id>/library/``.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# Canonical schema definition (A13): one ordered column list per table is the
# single source of truth for BOTH fresh CREATE TABLE and the migration of
# older databases — adding a column is a single edit here.
#
# Each column entry is (name, create DDL, migrate DDL):
# - create DDL: column definition used when init_db creates the table fresh;
#   None for dead legacy columns (never created fresh; kept migratable so
#   pre-existing databases still gain them and keep working).
# - migrate DDL: ALTER TABLE ADD COLUMN definition applied when the table
#   pre-existed and lacks the column; None when the column is never ALTER-
#   added (SQLite cannot add PRIMARY KEY / UNIQUE / NOT NULL-without-DEFAULT
#   columns; every pre-existing database already carries those). It may
#   differ from the create DDL when an existing install must keep its
#   historic default (retention columns: 0 = keep all, A12).
_SCHEMA: list[tuple[str, list[tuple[str, str | None, str | None]], list[str]]] = [
    (
        "users",
        [
            ("id", "id TEXT PRIMARY KEY", None),  # UUID4, opaque, used in paths
            ("username", "username TEXT UNIQUE NOT NULL", None),
            ("password_hash", "password_hash TEXT NOT NULL", None),  # argon2
            ("is_admin", "is_admin INTEGER NOT NULL DEFAULT 0", None),
            ("created_at", "created_at TEXT NOT NULL", None),  # ISO 8601 UTC
        ],
        [],
    ),
    (
        "librarian_configs",
        [
            ("user_id", "user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE", None),
            ("llm_model", "llm_model TEXT", None),
            ("versioning", "versioning INTEGER NOT NULL DEFAULT 1", None),
            ("snapshot_keep", "snapshot_keep INTEGER NOT NULL DEFAULT 50", None),  # 0 = keep all
            (
                "trace_keep",  # 0 = keep all
                "trace_keep INTEGER NOT NULL DEFAULT 50",
                "trace_keep INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "activity_keep",  # 0 = keep all
                "activity_keep INTEGER NOT NULL DEFAULT 1000",
                "activity_keep INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "payload_keep",  # 0 = keep all
                "payload_keep INTEGER NOT NULL DEFAULT 100",
                "payload_keep INTEGER NOT NULL DEFAULT 0",
            ),
            ("library_name", "library_name TEXT", None),
            ("library_description", "library_description TEXT", None),
            (
                "curate_prompt_addendum",
                "curate_prompt_addendum TEXT",
                "curate_prompt_addendum TEXT",
            ),
            ("curate_last_run_at", "curate_last_run_at TEXT", "curate_last_run_at TEXT"),
            (
                "curate_schedule_enabled",
                "curate_schedule_enabled INTEGER NOT NULL DEFAULT 0",
                "curate_schedule_enabled INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "curate_schedule_time",  # UTC 'HH:MM'; NULL = no scheduled run
                "curate_schedule_time TEXT",
                "curate_schedule_time TEXT",
            ),
            ("prompt_addendum", "prompt_addendum TEXT", "prompt_addendum TEXT"),
            (
                "librarian_connection_id",
                "librarian_connection_id TEXT",
                "librarian_connection_id TEXT",
            ),
            (
                "curator_connection_id",
                "curator_connection_id TEXT",
                "curator_connection_id TEXT",
            ),
            ("curator_model", "curator_model TEXT", "curator_model TEXT"),
            ("embedding_source", "embedding_source TEXT", "embedding_source TEXT"),
            ("embedding_model", "embedding_model TEXT", "embedding_model TEXT"),
            (
                "embedding_connection_id",
                "embedding_connection_id TEXT",
                "embedding_connection_id TEXT",
            ),
            ("semantic_threshold", "semantic_threshold REAL", "semantic_threshold REAL"),
            # Hybrid search toggles (0.19.0): NOT NULL DEFAULT 1 backfills
            # existing rows, so pre-existing deployments upgrade to hybrid-on.
            (
                "hybrid_search",
                "hybrid_search INTEGER NOT NULL DEFAULT 1",
                "hybrid_search INTEGER NOT NULL DEFAULT 1",
            ),
            (
                "hybrid_rerank",
                "hybrid_rerank INTEGER NOT NULL DEFAULT 1",
                "hybrid_rerank INTEGER NOT NULL DEFAULT 1",
            ),
            # Dead pre-provider-connections legacy columns: never created
            # fresh, still added to pre-existing databases.
            ("curate_provider", None, "curate_provider TEXT"),
            ("curate_model", None, "curate_model TEXT"),
            # The pre-0.8.0 llm_* columns (llm_provider, llm_api_key_enc,
            # llm_base_url, llm_max_iterations, llm_temperature,
            # llm_max_tokens), system_prompt, auto_index, and log_writes are
            # dead legacy too: they exist in every pre-0.8.0 database
            # already, need no migration, and are dropped from fresh creates.
            # _migrate_llm_to_provider_configs reads the llm_* columns.
        ],
        [],
    ),
    (
        "mcp_tokens",
        [
            ("id", "id TEXT PRIMARY KEY", None),  # UUID4
            ("user_id", "user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE", None),
            ("label", "label TEXT NOT NULL", None),  # per-agent attribution
            ("token_hash", "token_hash TEXT UNIQUE NOT NULL", None),  # SHA-256 hex of token
            ("created_at", "created_at TEXT NOT NULL", None),
            ("last_used_at", "last_used_at TEXT", None),
            ("revoked_at", "revoked_at TEXT", None),
        ],
        [],
    ),
    (
        "activity",
        [
            ("id", "id INTEGER PRIMARY KEY AUTOINCREMENT", None),
            ("trace_id", "trace_id TEXT NOT NULL", None),
            ("user_id", "user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE", None),
            ("token_label", "token_label TEXT", None),
            ("tool", "tool TEXT NOT NULL", None),
            ("arguments", "arguments TEXT", None),
            ("started_at", "started_at TEXT NOT NULL", None),
            ("duration_ms", "duration_ms REAL", None),
            ("outcome", "outcome TEXT NOT NULL", None),
            ("error", "error TEXT", None),
            ("iterations", "iterations INTEGER", None),
            ("prompt_tokens", "prompt_tokens INTEGER", None),
            ("completion_tokens", "completion_tokens INTEGER", None),
            ("total_tokens", "total_tokens INTEGER", None),
        ],
        [],
    ),
    (
        "provider_configs",
        [
            ("id", "id TEXT PRIMARY KEY", None),  # UUID4
            ("user_id", "user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE", None),
            ("label", "label TEXT NOT NULL", None),
            # 'openai'|'anthropic'|'gemini'|'openrouter'|'openai-compatible'
            ("provider", "provider TEXT NOT NULL", None),
            ("api_key_enc", "api_key_enc TEXT", None),  # Fernet ciphertext, never rendered to HTML
            ("base_url", "base_url TEXT", None),
            ("max_iterations", "max_iterations INTEGER NOT NULL DEFAULT 10", None),
            ("temperature", "temperature REAL", None),
            ("max_tokens", "max_tokens INTEGER", None),
            ("is_default", "is_default INTEGER NOT NULL DEFAULT 0", None),
            ("created_at", "created_at TEXT NOT NULL", None),  # ISO 8601 UTC via utcnow()
        ],
        [],
    ),
    (
        # Admin-managed SHARED execution connections for Attested Computations
        # (0.21.0): deliberately NO user_id column — every user's computations
        # may reference them. 'runtime' is 'postgres'|'sqlite' (validated in
        # CRUD/routes, matching provider_configs style); 'dbname' carries the
        # postgres database name or the sqlite ABSOLUTE file path.
        "runtime_connections",
        [
            ("id", "id TEXT PRIMARY KEY", None),  # UUID4
            ("label", "label TEXT NOT NULL", None),
            ("runtime", "runtime TEXT NOT NULL", None),  # 'postgres'|'sqlite'
            ("host", "host TEXT", None),
            ("port", "port INTEGER", None),
            ("dbname", "dbname TEXT", None),
            ("username", "username TEXT", None),
            # Fernet ciphertext, write-only, never rendered to HTML
            ("password_enc", "password_enc TEXT", None),
            ("created_at", "created_at TEXT NOT NULL", None),  # ISO 8601 UTC via utcnow()
        ],
        [],
    ),
    (
        "embeddings",
        [
            ("user_id", "user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE", None),
            ("concept_path", "concept_path TEXT NOT NULL", None),
            ("model", "model TEXT NOT NULL", None),
            ("dims", "dims INTEGER NOT NULL", None),
            ("vector", "vector BLOB NOT NULL", None),
            ("content_hash", "content_hash TEXT NOT NULL", None),
            ("updated_at", "updated_at TEXT NOT NULL", None),
        ],
        ["PRIMARY KEY (user_id, concept_path)"],
    ),
    (
        "embed_reconcile_claims",
        [
            ("user_id", "user_id TEXT PRIMARY KEY", None),
            ("owner", "owner TEXT NOT NULL", None),  # hostname:pid:uuid of the claim holder
            # ISO 8601 UTC; older than TTL = reclaimable
            ("claimed_at", "claimed_at TEXT NOT NULL", None),
        ],
        [],
    ),
    (
        "app_settings",
        [
            ("key", "key TEXT PRIMARY KEY", None),
            ("value", "value TEXT NOT NULL", None),
        ],
        [],
    ),
    (
        "login_attempts",
        [
            ("key", "key TEXT PRIMARY KEY", None),  # 'user:<username>' or 'ip:<client ip>'
            # consecutive failures (reset on success)
            ("failures", "failures INTEGER NOT NULL DEFAULT 0", None),
            ("locked_until", "locked_until TEXT", None),  # ISO 8601 UTC; NULL = not locked
        ],
        [],
    ),
]

_SCHEMA_INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_activity_user_started ON activity(user_id, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_activity_trace ON activity(trace_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_configs_default"
    " ON provider_configs(user_id) WHERE is_default = 1",
]

# FTS5 virtual tables (hybrid search, 0.19.0). Kept separate from _SCHEMA:
# CREATE VIRTUAL TABLE fails on SQLite builds without FTS5, so init_db wraps
# each statement and degrades to the legacy pure-semantic path instead of
# failing. Plain-content mode (the text column lives in the table); only
# `text` is indexed, tenancy is a plain user_id filter. The shadow tables
# concepts_fts_{data,idx,content,docsize,config} share app.db — keep the
# concepts_fts name prefix exclusive to this feature.
_SCHEMA_VIRTUAL_TABLES: list[str] = [
    "CREATE VIRTUAL TABLE IF NOT EXISTS concepts_fts USING fts5("
    "user_id UNINDEXED, concept_path UNINDEXED, text, content_hash UNINDEXED,"
    " tokenize = 'porter unicode61')",
]


DEFAULT_SCHEDULE_TIME = "03:00"  # UTC HH:MM for newly created users
HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# A12: bounded retention defaults for NEW installs/config rows only — existing
# config rows are never migrated to these (no silent pruning of user history
# on upgrade), and 0 (= keep all) stays an explicit Admin-UI choice.
DEFAULT_SNAPSHOT_KEEP = 50
DEFAULT_TRACE_KEEP = 50
DEFAULT_ACTIVITY_KEEP = 1000
DEFAULT_PAYLOAD_KEEP = 100


def utcnow() -> str:
    """Current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat()


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with row factory and foreign keys enabled.

    ``check_same_thread=False``: FastAPI runs sync dependencies and async
    endpoints on different threads; each request still gets its own
    connection, so there is no cross-request sharing. WAL journal mode, a
    5-second busy timeout, and NORMAL synchronous let concurrent
    connections (MCP server, WebUI, scheduler) share one database file.
    """
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    # Switching into WAL needs a brief exclusive lock that busy_timeout does
    # not cover; retry while a peer connection finishes its initialization.
    # Steady state skips the switch entirely (WAL persists in the DB file):
    # the blocking retry loop then only runs during genuine conversion at
    # startup, which happens off the event loop (A1).
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    if str(mode).lower() != "wal":
        for attempt in range(50):
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or attempt == 49:
                    raise
                time.sleep(0.1)
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """Add ``column`` to ``table`` when missing (idempotent ALTER TABLE)."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        except sqlite3.OperationalError as exc:
            # A concurrent init_db added the column first (R17).
            if "duplicate column name" not in str(exc):
                raise


def _create_table_ddl(
    table: str, columns: list[tuple[str, str | None, str | None]], extra: list[str]
) -> str:
    """Fresh CREATE TABLE statement from one _SCHEMA entry (dead legacy
    columns with create DDL None are skipped)."""
    lines = [create_ddl for _, create_ddl, _ in columns if create_ddl is not None]
    return f"CREATE TABLE IF NOT EXISTS {table} (\n  " + ",\n  ".join(lines + extra) + "\n)"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def init_db(db_path: str | Path) -> None:
    """Create the schema (idempotent); migrate pre-existing databases."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        # BEGIN IMMEDIATE serializes concurrent startup (R17); executescript
        # would implicitly commit, so the canonical _SCHEMA statements run
        # one by one inside the transaction (A13).
        conn.execute("BEGIN IMMEDIATE")
        for table, columns, extra in _SCHEMA:
            existed = _table_exists(conn, table)
            conn.execute(_create_table_ddl(table, columns, extra))
            if not existed:
                continue  # fresh CREATE TABLE already carries the full column set
            # Pre-existing database: ALTER-add missing migratable columns.
            for name, _, migrate_ddl in columns:
                if migrate_ddl is not None:
                    _ensure_column(conn, table, name, migrate_ddl)
        for statement in _SCHEMA_INDEXES:
            conn.execute(statement)
        for statement in _SCHEMA_VIRTUAL_TABLES:
            try:
                conn.execute(statement)
            except sqlite3.OperationalError as exc:
                # FTS5-less SQLite build: lexical search is disabled, the
                # backend stays on the legacy pure-semantic path. init_db
                # must never fail over an optional index.
                logger.warning("FTS5 unavailable; lexical search disabled: %s", exc)
        _migrate_llm_to_provider_configs(conn)


def _migrate_llm_to_provider_configs(conn: sqlite3.Connection) -> None:
    """One-time data migration: each user's llm_* row -> first provider_configs
    row (label "Default", is_default=1). Skips users who already have any
    provider_configs row and users with llm_provider IS NULL (unconfigured).

    Runs inside the caller's BEGIN IMMEDIATE transaction (A19): no inner
    ``with conn:`` block, which would commit that transaction early."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(librarian_configs)")}
    if "llm_provider" not in columns:
        return  # fresh/ancient DB without the llm_* columns: nothing to migrate
    rows = conn.execute(
        "SELECT user_id, llm_provider, llm_api_key_enc, llm_base_url,"
        " llm_max_iterations, llm_temperature, llm_max_tokens"
        " FROM librarian_configs WHERE llm_provider IS NOT NULL"
    ).fetchall()
    for row in rows:
        exists = conn.execute(
            "SELECT 1 FROM provider_configs WHERE user_id = ? LIMIT 1",
            (row["user_id"],),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO provider_configs"
            " (id, user_id, label, provider, api_key_enc, base_url,"
            "  max_iterations, temperature, max_tokens, is_default, created_at)"
            " VALUES (?, ?, 'Default', ?, ?, ?, ?, ?, ?, 1, ?)",
            (
                str(uuid.uuid4()),
                row["user_id"],
                row["llm_provider"],
                row["llm_api_key_enc"],
                row["llm_base_url"],
                row["llm_max_iterations"],
                row["llm_temperature"],
                row["llm_max_tokens"],
                utcnow(),
            ),
        )


# --- users -----------------------------------------------------------------


def users_empty(conn: sqlite3.Connection) -> bool:
    """True when no user account exists yet (drives first-run setup)."""
    row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
    return row["n"] == 0


def list_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, username, is_admin, created_at FROM users ORDER BY username"
    ).fetchall()


def get_user_by_id(conn: sqlite3.Connection, user_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_username(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def create_user(
    conn: sqlite3.Connection,
    username: str,
    password_hash: str,
    *,
    is_admin: bool = False,
) -> sqlite3.Row:
    """Insert the user row + default config row.

    Filesystem provisioning (library dir + OKF bundle) is NOT done here (A7):
    the persistence layer does not import the library layer — callers invoke
    ``library.backend.provision_library`` for the on-disk side.
    """
    user_id = str(uuid.uuid4())
    with conn:
        conn.execute(
            "INSERT INTO users (id, username, password_hash, is_admin, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (user_id, username, password_hash, int(is_admin), utcnow()),
        )
        conn.execute(
            "INSERT INTO librarian_configs"
            " (user_id, curate_schedule_enabled, curate_schedule_time,"
            "  snapshot_keep, trace_keep, activity_keep, payload_keep)"
            " VALUES (?, 1, ?, ?, ?, ?, ?)",
            (
                user_id,
                DEFAULT_SCHEDULE_TIME,
                DEFAULT_SNAPSHOT_KEEP,
                DEFAULT_TRACE_KEEP,
                DEFAULT_ACTIVITY_KEEP,
                DEFAULT_PAYLOAD_KEEP,
            ),
        )
    return get_user_by_id(conn, user_id)


def set_password(conn: sqlite3.Connection, user_id: str, password_hash: str) -> None:
    with conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))


def create_first_admin(
    conn: sqlite3.Connection,
    username: str,
    password_hash: str,
) -> sqlite3.Row | None:
    """Create the first-run owner iff the users table is still empty.

    The emptiness check and the insert are one statement inside one
    transaction (CS-13): concurrent first-run POSTs cannot both create an
    admin — the loser gets None. Library provisioning happens on the caller
    side (``library.backend.provision_library``), like create_user.
    """
    user_id = str(uuid.uuid4())
    with conn:
        cursor = conn.execute(
            "INSERT INTO users (id, username, password_hash, is_admin, created_at)"
            " SELECT ?, ?, ?, 1, ? WHERE NOT EXISTS (SELECT 1 FROM users)",
            (user_id, username, password_hash, utcnow()),
        )
        if cursor.rowcount == 0:
            return None
        conn.execute(
            "INSERT INTO librarian_configs"
            " (user_id, curate_schedule_enabled, curate_schedule_time,"
            "  snapshot_keep, trace_keep, activity_keep)"
            " VALUES (?, 1, ?, ?, ?, ?)",
            (
                user_id,
                DEFAULT_SCHEDULE_TIME,
                DEFAULT_SNAPSHOT_KEEP,
                DEFAULT_TRACE_KEEP,
                DEFAULT_ACTIVITY_KEEP,
            ),
        )
    return get_user_by_id(conn, user_id)


# --- login throttling ------------------------------------------------------

LOGIN_MAX_FAILURES = 5  # consecutive failures before lockout kicks in
LOGIN_LOCKOUT_BASE_SECONDS = 30  # first lockout; doubles per further failure
LOGIN_LOCKOUT_MAX_SECONDS = 3600  # backoff cap


def login_lockout_seconds(
    conn: sqlite3.Connection, key: str, *, now: datetime | None = None
) -> int:
    """Remaining lockout seconds for ``key`` (0 when not locked)."""
    row = conn.execute("SELECT locked_until FROM login_attempts WHERE key = ?", (key,)).fetchone()
    if row is None or not row["locked_until"]:
        return 0
    now = now or datetime.now(UTC)
    try:
        until = datetime.fromisoformat(row["locked_until"])
    except ValueError:
        return 0
    return max(0, int((until - now).total_seconds()))


def record_login_failure(conn: sqlite3.Connection, key: str, *, now: datetime | None = None) -> int:
    """Count one failed login for ``key``; lock with exponential backoff.

    Returns the applied lockout in seconds (0 below LOGIN_MAX_FAILURES).
    """
    now = now or datetime.now(UTC)
    with conn:
        conn.execute(
            "INSERT INTO login_attempts (key, failures, locked_until) VALUES (?, 0, NULL)"
            " ON CONFLICT(key) DO NOTHING",
            (key,),
        )
        failures = (
            conn.execute("SELECT failures FROM login_attempts WHERE key = ?", (key,)).fetchone()[
                "failures"
            ]
            + 1
        )
        lockout = 0
        locked_until = None
        if failures >= LOGIN_MAX_FAILURES:
            lockout = min(
                LOGIN_LOCKOUT_MAX_SECONDS,
                LOGIN_LOCKOUT_BASE_SECONDS * 2 ** (failures - LOGIN_MAX_FAILURES),
            )
            locked_until = (now + timedelta(seconds=lockout)).isoformat()
        conn.execute(
            "UPDATE login_attempts SET failures = ?, locked_until = ? WHERE key = ?",
            (failures, locked_until, key),
        )
    return lockout


def reset_login_failures(conn: sqlite3.Connection, key: str) -> None:
    """Clear the failure counter for ``key`` (successful login)."""
    with conn:
        conn.execute("DELETE FROM login_attempts WHERE key = ?", (key,))


# --- librarian configs -----------------------------------------------------


def get_config(conn: sqlite3.Connection, user_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM librarian_configs WHERE user_id = ?", (user_id,)).fetchone()


# --- provider configs ----------------------------------------------------------


class ProviderConfigInUseError(Exception):
    """A provider connection cannot be deleted (bound agent or active default)."""


def create_provider_config(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    label: str,
    provider: str,
    api_key_enc: str | None = None,
    base_url: str | None = None,
    max_iterations: int = 10,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> sqlite3.Row:
    """Insert a named provider connection; the first one becomes the default."""
    connection_id = str(uuid.uuid4())
    with conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM provider_configs WHERE user_id = ?", (user_id,)
        ).fetchone()["n"]
        conn.execute(
            "INSERT INTO provider_configs"
            " (id, user_id, label, provider, api_key_enc, base_url,"
            "  max_iterations, temperature, max_tokens, is_default, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                connection_id,
                user_id,
                label,
                provider,
                api_key_enc,
                base_url,
                max_iterations,
                temperature,
                max_tokens,
                1 if count == 0 else 0,
                utcnow(),
            ),
        )
    return get_provider_config(conn, user_id, connection_id)


def list_provider_configs(conn: sqlite3.Connection, user_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM provider_configs WHERE user_id = ? ORDER BY is_default DESC, created_at ASC",
        (user_id,),
    ).fetchall()


def get_provider_config(
    conn: sqlite3.Connection, user_id: str, connection_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM provider_configs WHERE id = ? AND user_id = ?",
        (connection_id, user_id),
    ).fetchone()


def update_provider_config(
    conn: sqlite3.Connection,
    user_id: str,
    connection_id: str,
    *,
    label: str,
    provider: str,
    base_url: str | None,
    max_iterations: int,
    temperature: float | None,
    max_tokens: int | None,
    api_key_enc: str | None = None,
) -> None:
    """Save one provider connection.

    ``api_key_enc=None`` keeps the existing encrypted key; the WebUI never
    renders the key back, so an empty form field means "unchanged".
    """
    with conn:
        conn.execute(
            "UPDATE provider_configs SET label = ?, provider = ?, base_url = ?,"
            " max_iterations = ?, temperature = ?, max_tokens = ?"
            " WHERE id = ? AND user_id = ?",
            (
                label,
                provider,
                base_url,
                max_iterations,
                temperature,
                max_tokens,
                connection_id,
                user_id,
            ),
        )
        if api_key_enc is not None:
            conn.execute(
                "UPDATE provider_configs SET api_key_enc = ? WHERE id = ? AND user_id = ?",
                (api_key_enc, connection_id, user_id),
            )


def count_provider_config_bindings(conn: sqlite3.Connection, connection_id: str) -> int:
    """Number of agent bindings (librarian, curator, or embedding) referencing
    a connection."""
    return conn.execute(
        "SELECT COUNT(*) AS n FROM librarian_configs"
        " WHERE librarian_connection_id = ?1 OR curator_connection_id = ?1"
        " OR embedding_connection_id = ?1",
        (connection_id,),
    ).fetchone()["n"]


def delete_provider_config(conn: sqlite3.Connection, user_id: str, connection_id: str) -> None:
    """Delete a connection; refuses bound agents and the default-with-siblings."""
    if count_provider_config_bindings(conn, connection_id) > 0:
        raise ProviderConfigInUseError(
            "Connection is still assigned to an agent; rebind the agent first."
        )
    row = get_provider_config(conn, user_id, connection_id)
    if row is not None and row["is_default"]:
        others = conn.execute(
            "SELECT COUNT(*) AS n FROM provider_configs WHERE user_id = ? AND id != ?",
            (user_id, connection_id),
        ).fetchone()["n"]
        if others > 0:
            raise ProviderConfigInUseError("Set another connection as default first.")
    with conn:
        conn.execute(
            "DELETE FROM provider_configs WHERE id = ? AND user_id = ?",
            (connection_id, user_id),
        )


def set_default_provider_config(conn: sqlite3.Connection, user_id: str, connection_id: str) -> None:
    """Atomically move the default flag to one of the user's connections."""
    with conn:
        conn.execute("UPDATE provider_configs SET is_default = 0 WHERE user_id = ?", (user_id,))
        conn.execute(
            "UPDATE provider_configs SET is_default = 1 WHERE id = ? AND user_id = ?",
            (connection_id, user_id),
        )


# --- runtime connections (Attested Computations, admin-managed shared) -------


def create_runtime_connection(
    conn: sqlite3.Connection,
    *,
    label: str,
    runtime: str,
    host: str | None = None,
    port: int | None = None,
    dbname: str | None = None,
    username: str | None = None,
    password_enc: str | None = None,
) -> sqlite3.Row:
    """Insert one shared execution connection (admin-managed; no user scope)."""
    connection_id = str(uuid.uuid4())
    with conn:
        conn.execute(
            "INSERT INTO runtime_connections"
            " (id, label, runtime, host, port, dbname, username, password_enc, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (connection_id, label, runtime, host, port, dbname, username, password_enc, utcnow()),
        )
    return get_runtime_connection(conn, connection_id)


def list_runtime_connections(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """List view WITHOUT password_enc (write-only; never rendered to HTML)."""
    return conn.execute(
        "SELECT id, label, runtime, host, port, dbname, username, created_at,"
        " password_enc IS NOT NULL AS password_set"
        " FROM runtime_connections ORDER BY label"
    ).fetchall()


def get_runtime_connection(conn: sqlite3.Connection, connection_id: str) -> sqlite3.Row | None:
    """Full row incl. password_enc — the execution path needs the ciphertext."""
    return conn.execute(
        "SELECT * FROM runtime_connections WHERE id = ?", (connection_id,)
    ).fetchone()


def update_runtime_connection(
    conn: sqlite3.Connection,
    connection_id: str,
    *,
    label: str,
    runtime: str,
    host: str | None,
    port: int | None,
    dbname: str | None,
    username: str | None,
    password_enc: str | None = None,
) -> None:
    """Save one connection; ``password_enc=None`` keeps the stored ciphertext."""
    with conn:
        conn.execute(
            "UPDATE runtime_connections SET label = ?, runtime = ?, host = ?, port = ?,"
            " dbname = ?, username = ? WHERE id = ?",
            (label, runtime, host, port, dbname, username, connection_id),
        )
        if password_enc is not None:
            conn.execute(
                "UPDATE runtime_connections SET password_enc = ? WHERE id = ?",
                (password_enc, connection_id),
            )


def delete_runtime_connection(conn: sqlite3.Connection, connection_id: str) -> None:
    with conn:
        conn.execute("DELETE FROM runtime_connections WHERE id = ?", (connection_id,))


def update_librarian_config(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    connection_id: str | None,
    model: str | None,
    prompt_addendum: str | None,
) -> None:
    with conn:
        conn.execute(
            "UPDATE librarian_configs SET librarian_connection_id = ?, llm_model = ?,"
            " prompt_addendum = ? WHERE user_id = ?",
            (connection_id or None, model or None, prompt_addendum or None, user_id),
        )


def update_library_settings(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    name: str | None,
    description: str | None,
    versioning: bool,
    snapshot_keep: int,
    trace_keep: int,
    activity_keep: int,
    payload_keep: int | None = None,
) -> None:
    # payload_keep uses COALESCE semantics: None keeps the stored value, so
    # existing callers that predate the column leave it untouched.
    with conn:
        conn.execute(
            "UPDATE librarian_configs SET library_name = ?, library_description = ?,"
            " versioning = ?, snapshot_keep = ?, trace_keep = ?, activity_keep = ?,"
            " payload_keep = COALESCE(?, payload_keep)"
            " WHERE user_id = ?",
            (
                name or None,
                description or None,
                int(versioning),
                snapshot_keep,
                trace_keep,
                activity_keep,
                payload_keep,
                user_id,
            ),
        )


def update_curate_config(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    connection_id: str | None,
    curator_model: str | None,
    curate_prompt_addendum: str | None = None,
) -> None:
    """Save the curator binding; NULLs mean "default connection" / librarian model."""
    with conn:
        conn.execute(
            "UPDATE librarian_configs SET curator_connection_id = ?, curator_model = ?,"
            " curate_prompt_addendum = ? WHERE user_id = ?",
            (connection_id or None, curator_model or None, curate_prompt_addendum, user_id),
        )


def update_embedding_config(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    source: str | None,
    model: str | None,
    connection_id: str | None,
    semantic_threshold: float | None = None,
    hybrid_search: bool | None = None,
    hybrid_rerank: bool | None = None,
) -> None:
    """Save the embedding binding; ``source=None`` clears all three columns.

    Unlike model/connection_id, the threshold is NOT force-cleared when
    ``source=None`` — it is written as passed, so a temporary Off does not
    silently discard an explicit override; the WebUI always submits the form
    field. None = model default.

    The hybrid toggles use COALESCE semantics: None keeps the stored value
    (overrides survive a save that does not submit them), an explicit bool
    overwrites. The WebUI always submits explicit values.
    """
    if source is None:
        model = None
        connection_id = None
    with conn:
        conn.execute(
            "UPDATE librarian_configs SET embedding_source = ?, embedding_model = ?,"
            " embedding_connection_id = ?, semantic_threshold = ?,"
            " hybrid_search = COALESCE(?, hybrid_search),"
            " hybrid_rerank = COALESCE(?, hybrid_rerank) WHERE user_id = ?",
            (
                source or None,
                model or None,
                connection_id or None,
                semantic_threshold,
                None if hybrid_search is None else int(hybrid_search),
                None if hybrid_rerank is None else int(hybrid_rerank),
                user_id,
            ),
        )


def embedding_stats(conn: sqlite3.Connection, user_id: str) -> dict:
    """Embedding index size for the WebUI status card (no service needed).

    Same shape as ``EmbeddingService.stats()``: ``{"rows", "models", "dims"}``.
    """
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM embeddings WHERE user_id = ?", (user_id,)
    ).fetchone()["n"]
    models = [
        row["model"]
        for row in conn.execute(
            "SELECT DISTINCT model FROM embeddings WHERE user_id = ?", (user_id,)
        ).fetchall()
    ]
    dims = [
        row["dims"]
        for row in conn.execute(
            "SELECT DISTINCT dims FROM embeddings WHERE user_id = ?", (user_id,)
        ).fetchall()
    ]
    return {"rows": rows, "models": models, "dims": dims}


# --- embed reconcile claims ----------------------------------------------------


def try_claim_embed_reconcile(
    conn: sqlite3.Connection, user_id: str, owner: str, ttl_seconds: float
) -> bool:
    """Claim the user's embed-reconcile slot; False when already claimed.

    Conditional upsert in one atomic statement: the claim succeeds when no
    row exists or the existing ``claimed_at`` is older than the TTL
    (crashed-owner recovery). ``claimed_at`` values are utcnow() ISO strings,
    so lexicographic comparison is chronological.
    """
    now = datetime.now(UTC)
    oldest_live = (now - timedelta(seconds=ttl_seconds)).isoformat()
    with conn:
        cur = conn.execute(
            "INSERT INTO embed_reconcile_claims (user_id, owner, claimed_at)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT (user_id) DO UPDATE SET owner = excluded.owner,"
            " claimed_at = excluded.claimed_at"
            " WHERE embed_reconcile_claims.claimed_at < ?",
            (user_id, owner, now.isoformat(), oldest_live),
        )
    return cur.rowcount > 0


def release_embed_reclaim(conn: sqlite3.Connection, user_id: str, owner: str) -> None:
    """Release the claim only when the caller still owns it."""
    with conn:
        conn.execute(
            "DELETE FROM embed_reconcile_claims WHERE user_id = ? AND owner = ?",
            (user_id, owner),
        )


# --- app settings ------------------------------------------------------------


def get_app_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    """Read one server-wide setting; ``default`` when unset."""
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else default


def set_app_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert one server-wide setting."""
    with conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def set_curate_last_run(conn: sqlite3.Connection, user_id: str, ts: str) -> None:
    """Record the run-end timestamp of the last completed curate run."""
    with conn:
        conn.execute(
            "UPDATE librarian_configs SET curate_last_run_at = ? WHERE user_id = ?",
            (ts, user_id),
        )


def update_curate_schedule(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    enabled: bool,
    time_hhmm: str,
) -> None:
    """Save the nightly maintain+curate schedule (UTC 'HH:MM')."""
    with conn:
        conn.execute(
            "UPDATE librarian_configs SET curate_schedule_enabled = ?,"
            " curate_schedule_time = ? WHERE user_id = ?",
            (int(enabled), time_hhmm or DEFAULT_SCHEDULE_TIME, user_id),
        )


# --- activity journal --------------------------------------------------------


def insert_activity(
    conn: sqlite3.Connection,
    *,
    trace_id: str,
    user_id: str,
    token_label: str | None,
    tool: str,
    arguments: str | None,
    started_at: str,
    duration_ms: float | None,
    outcome: str,
    error: str | None,
    iterations: int | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    total_tokens: int | None,
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO activity (trace_id, user_id, token_label, tool, arguments,"
            " started_at, duration_ms, outcome, error, iterations, prompt_tokens,"
            " completion_tokens, total_tokens)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trace_id,
                user_id,
                token_label,
                tool,
                arguments,
                started_at,
                duration_ms,
                outcome,
                error,
                iterations,
                prompt_tokens,
                completion_tokens,
                total_tokens,
            ),
        )


def list_activity(conn: sqlite3.Connection, user_id: str, limit: int = 50) -> list[sqlite3.Row]:
    """Newest journal rows first, scoped to one user."""
    return conn.execute(
        "SELECT * FROM activity WHERE user_id = ? ORDER BY started_at DESC, id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()


def prune_activity(conn: sqlite3.Connection, user_id: str, keep_last: int) -> int:
    """Delete all but the newest ``keep_last`` rows for one user."""
    with conn:
        cur = conn.execute(
            "DELETE FROM activity WHERE user_id = ? AND id NOT IN ("
            " SELECT id FROM activity WHERE user_id = ?"
            " ORDER BY started_at DESC, id DESC LIMIT ?)",
            (user_id, user_id, keep_last),
        )
    return cur.rowcount


# --- MCP tokens ------------------------------------------------------------


def create_token(
    conn: sqlite3.Connection, user_id: str, label: str, token_hash: str
) -> sqlite3.Row:
    token_id = str(uuid.uuid4())
    with conn:
        conn.execute(
            "INSERT INTO mcp_tokens (id, user_id, label, token_hash, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (token_id, user_id, label, token_hash, utcnow()),
        )
    return conn.execute("SELECT * FROM mcp_tokens WHERE id = ?", (token_id,)).fetchone()


def list_tokens(conn: sqlite3.Connection, user_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, user_id, label, created_at, last_used_at, revoked_at"
        " FROM mcp_tokens WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()


def get_token(conn: sqlite3.Connection, token_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM mcp_tokens WHERE id = ?", (token_id,)).fetchone()


def revoke_token(conn: sqlite3.Connection, user_id: str, token_id: str) -> bool:
    """Revoke one of the user's own tokens. False when not found / not owned."""
    with conn:
        cur = conn.execute(
            "UPDATE mcp_tokens SET revoked_at = ? WHERE id = ? AND user_id = ?"
            " AND revoked_at IS NULL",
            (utcnow(), token_id, user_id),
        )
    return cur.rowcount > 0


def lookup_token(conn: sqlite3.Connection, token_hash: str) -> sqlite3.Row | None:
    """Resolve a bearer token hash to its row (MCP auth; stream B consumes)."""
    return conn.execute("SELECT * FROM mcp_tokens WHERE token_hash = ?", (token_hash,)).fetchone()


def touch_token(conn: sqlite3.Connection, token_id: str) -> None:
    with conn:
        conn.execute("UPDATE mcp_tokens SET last_used_at = ? WHERE id = ?", (utcnow(), token_id))
