"""Unit tests for athenaeum.db and athenaeum.security (self-contained)."""

import sqlite3
import threading

import pytest
from cryptography.fernet import InvalidToken

from athenaeum import db, security

# --- security ----------------------------------------------------------------


def test_password_hash_verify_roundtrip():
    hashed = security.hash_password("correct horse battery staple")
    assert hashed != "correct horse battery staple"
    assert security.verify_password(hashed, "correct horse battery staple")


def test_password_wrong_rejected():
    hashed = security.hash_password("right")
    assert not security.verify_password(hashed, "wrong")


def test_password_malformed_hash_rejected():
    assert not security.verify_password("not-a-hash", "anything")


def test_generate_token_plaintext_and_stable_hash():
    plaintext, digest = security.generate_token()
    assert plaintext
    assert len(digest) == 64
    assert security.hash_token(plaintext) == digest  # stable
    _, digest2 = security.generate_token()
    assert digest2 != digest  # unique per call


def test_fernet_roundtrip():
    secret = "server-secret-key"
    ciphertext = security.encrypt_secret("sk-live-123", secret)
    assert ciphertext != "sk-live-123"
    assert "sk-live-123" not in ciphertext
    assert security.decrypt_secret(ciphertext, secret) == "sk-live-123"


def test_fernet_wrong_key_fails():
    ciphertext = security.encrypt_secret("sk-live-123", "key-a")
    with pytest.raises(InvalidToken):
        security.decrypt_secret(ciphertext, "key-b")


# --- db ----------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "app.db"
    db.init_db(db_path)
    connection = db.connect(db_path)
    yield connection
    connection.close()


def test_init_db_idempotent(tmp_path):
    db_path = tmp_path / "app.db"
    db.init_db(db_path)
    db.init_db(db_path)  # second run must not raise


def test_connect_enables_wal_busy_timeout_and_normal_synchronous(tmp_path):
    db_path = tmp_path / "app.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    finally:
        conn.close()


def test_concurrent_init_db_from_two_connections(tmp_path):
    """Two threads racing init_db both succeed (BEGIN IMMEDIATE, R17)."""
    db_path = tmp_path / "app.db"
    errors = []

    def init():
        try:
            db.init_db(db_path)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=init) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    conn = db.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0
    finally:
        conn.close()


def test_threaded_activity_inserts_no_operational_error(tmp_path):
    """Concurrent writers on separate connections never hit 'database is locked'."""
    db_path = tmp_path / "app.db"
    db.init_db(db_path)
    setup = db.connect(db_path)
    try:
        setup.execute(
            "INSERT INTO users (id, username, password_hash, created_at)"
            " VALUES ('u1', 'alice', 'h', ?)",
            (db.utcnow(),),
        )
        setup.commit()
    finally:
        setup.close()
    errors = []

    def writer(tag: int) -> None:
        conn = db.connect(db_path)
        try:
            for i in range(50):
                db.insert_activity(
                    conn,
                    trace_id=f"t{tag}-{i}",
                    user_id="u1",
                    token_label=None,
                    tool="request_knowledge",
                    arguments=None,
                    started_at=db.utcnow(),
                    duration_ms=None,
                    outcome="ok",
                    error=None,
                    iterations=None,
                    prompt_tokens=None,
                    completion_tokens=None,
                    total_tokens=None,
                )
        except Exception as exc:
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []
    conn = db.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM activity").fetchone()["n"] == 200
    finally:
        conn.close()


def test_foreign_keys_enabled(conn):
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO mcp_tokens (id, user_id, label, token_hash, created_at)"
                " VALUES ('t1', 'no-such-user', 'x', 'h', 'now')"
            )


def test_users_empty_and_create_user(conn, tmp_path):
    assert db.users_empty(conn)
    user = db.create_user(conn, "alice", security.hash_password("pw"), is_admin=True)
    assert not db.users_empty(conn)
    assert user["username"] == "alice"
    assert user["is_admin"] == 1
    # default config row provisioned
    cfg = db.get_config(conn, user["id"])
    assert cfg["librarian_connection_id"] is None
    assert cfg["curator_connection_id"] is None
    assert cfg["curator_model"] is None
    assert cfg["versioning"] == 1
    assert cfg["snapshot_keep"] == db.DEFAULT_SNAPSHOT_KEEP
    assert cfg["trace_keep"] == db.DEFAULT_TRACE_KEEP
    assert cfg["activity_keep"] == db.DEFAULT_ACTIVITY_KEEP
    # A7: db.py does no filesystem provisioning — the on-disk bundle is the
    # caller's job (library.backend.provision_library).
    assert not (tmp_path / "users").exists()


def test_username_unique(conn, tmp_path):
    db.create_user(conn, "alice", "h1")
    with pytest.raises(sqlite3.IntegrityError):
        db.create_user(conn, "alice", "h2")


def test_set_password(conn, tmp_path):
    user = db.create_user(conn, "bob", security.hash_password("old"))
    db.set_password(conn, user["id"], security.hash_password("new"))
    assert security.verify_password(db.get_user_by_id(conn, user["id"])["password_hash"], "new")


def test_update_provider_config_key_only_overwritten_when_provided(conn, tmp_path):
    user = db.create_user(conn, "carol", "h")
    db.update_librarian_config(
        conn, user["id"], connection_id=None, model="gpt-test", prompt_addendum=None
    )
    connection = db.create_provider_config(
        conn,
        user["id"],
        label="Main",
        provider="openai",
        api_key_enc="enc-1",
        max_iterations=5,
        temperature=0.2,
        max_tokens=100,
    )
    row = db.get_provider_config(conn, user["id"], connection["id"])
    assert row["provider"] == "openai"
    assert row["api_key_enc"] == "enc-1"
    assert row["is_default"] == 1  # first connection is auto-default
    # saving again without a key keeps the stored one
    db.update_provider_config(
        conn,
        user["id"],
        connection["id"],
        label="Main",
        provider="anthropic",
        base_url=None,
        max_iterations=5,
        temperature=None,
        max_tokens=None,
    )
    row = db.get_provider_config(conn, user["id"], connection["id"])
    assert row["provider"] == "anthropic"
    assert row["api_key_enc"] == "enc-1"
    assert row["temperature"] is None and row["max_tokens"] is None
    # the connection save never touches the model (owned by the librarian form)
    assert db.get_config(conn, user["id"])["llm_model"] == "gpt-test"


def test_update_librarian_config(conn, tmp_path):
    user = db.create_user(conn, "dan", "h")
    connection = db.create_provider_config(conn, user["id"], label="Main", provider="openai")
    db.update_librarian_config(
        conn,
        user["id"],
        connection_id=connection["id"],
        model="gpt-x",
        prompt_addendum="You are a test.",
    )
    cfg = db.get_config(conn, user["id"])
    assert cfg["librarian_connection_id"] == connection["id"]
    assert cfg["llm_model"] == "gpt-x"
    assert cfg["prompt_addendum"] == "You are a test."
    # empty strings store NULL (no binding, no addendum)
    db.update_librarian_config(conn, user["id"], connection_id="", model="", prompt_addendum="")
    cfg = db.get_config(conn, user["id"])
    assert cfg["librarian_connection_id"] is None
    assert cfg["llm_model"] is None
    assert cfg["prompt_addendum"] is None


def test_update_library_settings(conn, tmp_path):
    user = db.create_user(conn, "erin", "h")
    db.update_library_settings(
        conn,
        user["id"],
        name="My KB",
        description="desc",
        versioning=False,
        snapshot_keep=3,
        trace_keep=7,
        activity_keep=11,
    )
    cfg = db.get_config(conn, user["id"])
    assert cfg["library_name"] == "My KB"
    assert cfg["library_description"] == "desc"
    assert cfg["versioning"] == 0
    assert cfg["snapshot_keep"] == 3
    assert cfg["trace_keep"] == 7 and cfg["activity_keep"] == 11


def test_update_curate_config_and_last_run(conn, tmp_path):
    user = db.create_user(conn, "gail", "h")
    connection = db.create_provider_config(conn, user["id"], label="Main", provider="openai")
    db.update_librarian_config(
        conn, user["id"], connection_id=None, model="gpt-x", prompt_addendum=None
    )
    cfg = db.get_config(conn, user["id"])
    assert cfg["curator_connection_id"] is None
    assert cfg["curator_model"] is None
    assert cfg["curate_prompt_addendum"] is None
    assert cfg["curate_last_run_at"] is None

    db.update_curate_config(
        conn,
        user["id"],
        connection_id=connection["id"],
        curator_model="big",
        curate_prompt_addendum="rule",
    )
    db.set_curate_last_run(conn, user["id"], "2026-07-29T00:00:00+00:00")
    cfg = db.get_config(conn, user["id"])
    assert cfg["curator_connection_id"] == connection["id"]
    assert cfg["curator_model"] == "big"
    assert cfg["curate_prompt_addendum"] == "rule"
    assert cfg["curate_last_run_at"] == "2026-07-29T00:00:00+00:00"
    # the separate curate form does not modify any provider_configs row
    row = db.get_provider_config(conn, user["id"], connection["id"])
    assert row["provider"] == "openai"
    assert db.get_config(conn, user["id"])["llm_model"] == "gpt-x"

    # clearing the binding stores NULLs (default connection / librarian model)
    db.update_curate_config(
        conn,
        user["id"],
        connection_id=None,
        curator_model=None,
        curate_prompt_addendum=None,
    )
    cfg = db.get_config(conn, user["id"])
    assert cfg["curator_connection_id"] is None
    assert cfg["curator_model"] is None
    assert cfg["curate_prompt_addendum"] is None


def test_activity_insert_list_scoping(conn, tmp_path):
    alice = db.create_user(conn, "alice", "h")
    bob = db.create_user(conn, "bob", "h")

    def insert(user_id, trace_id, started_at, tool="request_knowledge"):
        db.insert_activity(
            conn,
            trace_id=trace_id,
            user_id=user_id,
            token_label="agent-1",
            tool=tool,
            arguments='{"query": "x"}',
            started_at=started_at,
            duration_ms=12.5,
            outcome="ok",
            error=None,
            iterations=3,
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
        )

    insert(alice["id"], "t-old", "2026-01-01T00:00:00+00:00")
    insert(alice["id"], "t-new", "2026-01-02T00:00:00+00:00", tool="library_status")
    insert(bob["id"], "t-bob", "2026-01-03T00:00:00+00:00")

    rows = db.list_activity(conn, alice["id"])
    assert [r["trace_id"] for r in rows] == ["t-new", "t-old"]  # newest first
    row = rows[0]
    assert row["tool"] == "library_status"
    assert row["token_label"] == "agent-1"
    assert row["duration_ms"] == 12.5
    assert row["outcome"] == "ok"
    assert row["iterations"] == 3
    assert row["total_tokens"] == 30
    # limit and per-user scoping
    assert [r["trace_id"] for r in db.list_activity(conn, alice["id"], limit=1)] == ["t-new"]
    assert [r["trace_id"] for r in db.list_activity(conn, bob["id"])] == ["t-bob"]


def test_prune_activity_keeps_newest_per_user(conn, tmp_path):
    alice = db.create_user(conn, "alice", "h")
    bob = db.create_user(conn, "bob", "h")
    for i in range(5):
        for user_id, label in ((alice["id"], "a"), (bob["id"], "b")):
            db.insert_activity(
                conn,
                trace_id=f"{label}{i}",
                user_id=user_id,
                token_label=None,
                tool="request_knowledge",
                arguments=None,
                started_at=f"2026-01-0{i + 1}T00:00:00+00:00",
                duration_ms=None,
                outcome="ok",
                error=None,
                iterations=None,
                prompt_tokens=None,
                completion_tokens=None,
                total_tokens=None,
            )
    deleted = db.prune_activity(conn, alice["id"], 2)
    assert deleted == 3
    assert [r["trace_id"] for r in db.list_activity(conn, alice["id"])] == ["a4", "a3"]
    assert len(db.list_activity(conn, bob["id"])) == 5  # other user untouched


def test_ensure_column_idempotent_on_preexisting_db(tmp_path):
    db_path = tmp_path / "app.db"
    with sqlite3.connect(db_path) as raw:
        raw.executescript(
            """
            CREATE TABLE users (
              id TEXT PRIMARY KEY,
              username TEXT UNIQUE NOT NULL,
              password_hash TEXT NOT NULL,
              is_admin INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL
            );
            CREATE TABLE librarian_configs (
              user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
              system_prompt TEXT,
              versioning INTEGER NOT NULL DEFAULT 1,
              snapshot_keep INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO users (id, username, password_hash, created_at)
            VALUES ('u1', 'alice', 'h', '2026-01-01T00:00:00Z');
            INSERT INTO librarian_configs (user_id) VALUES ('u1');
            """
        )
    db.init_db(db_path)  # adds the missing columns
    db.init_db(db_path)  # second run must not raise
    conn = db.connect(db_path)
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(librarian_configs)")}
        assert "trace_keep" in columns and "activity_keep" in columns
        assert "curate_provider" in columns
        assert "curate_model" in columns
        assert "curate_prompt_addendum" in columns
        assert "curate_last_run_at" in columns
        assert "prompt_addendum" in columns
        assert "librarian_connection_id" in columns
        assert "curator_connection_id" in columns
        assert "curator_model" in columns
        assert "curate_schedule_enabled" in columns
        assert "curate_schedule_time" in columns
        assert "semantic_threshold" in columns
        assert "hybrid_search" in columns
        assert "hybrid_rerank" in columns
        cfg = db.get_config(conn, "u1")
        assert cfg["trace_keep"] == 0 and cfg["activity_keep"] == 0
        assert cfg["curate_provider"] is None
        assert cfg["curate_model"] is None
        assert cfg["curate_prompt_addendum"] is None
        assert cfg["curate_last_run_at"] is None
        assert cfg["prompt_addendum"] is None
        assert cfg["librarian_connection_id"] is None
        assert cfg["curator_connection_id"] is None
        assert cfg["curator_model"] is None
        # pre-existing rows keep the DDL defaults: scheduled curation stays off
        assert cfg["curate_schedule_enabled"] == 0
        assert cfg["curate_schedule_time"] is None
        assert cfg["semantic_threshold"] is None
        # pre-existing rows backfill to hybrid-on (NOT NULL DEFAULT 1)
        assert cfg["hybrid_search"] == 1
        assert cfg["hybrid_rerank"] == 1
    finally:
        conn.close()


PRE_0_8_SCHEMA = """
CREATE TABLE users (
  id TEXT PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE librarian_configs (
  user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  llm_provider TEXT,
  llm_model TEXT,
  llm_api_key_enc TEXT,
  llm_base_url TEXT,
  llm_max_iterations INTEGER NOT NULL DEFAULT 10,
  llm_temperature REAL,
  llm_max_tokens INTEGER,
  system_prompt TEXT,
  versioning INTEGER NOT NULL DEFAULT 1,
  snapshot_keep INTEGER NOT NULL DEFAULT 0,
  curate_provider TEXT,
  curate_model TEXT
);
"""


def test_migrate_llm_to_provider_configs(tmp_path):
    db_path = tmp_path / "app.db"
    with sqlite3.connect(db_path) as raw:
        raw.executescript(PRE_0_8_SCHEMA)
        raw.execute(
            "INSERT INTO users (id, username, password_hash, created_at)"
            " VALUES ('u1', 'alice', 'h', '2026-01-01T00:00:00Z')"
        )
        raw.execute(
            "INSERT INTO users (id, username, password_hash, created_at)"
            " VALUES ('u2', 'bob', 'h', '2026-01-01T00:00:00Z')"
        )
        # u1: configured pre-0.8.0 style; u2: NULL provider (unconfigured)
        raw.execute(
            "INSERT INTO librarian_configs"
            " (user_id, llm_provider, llm_model, llm_api_key_enc, llm_base_url,"
            "  llm_max_iterations, llm_temperature, llm_max_tokens)"
            " VALUES ('u1', 'openrouter', 'gpt-x', 'enc-k', 'https://or.test/v1', 7, 0.3, 512)"
        )
        raw.execute("INSERT INTO librarian_configs (user_id) VALUES ('u2')")
    db.init_db(db_path)  # runs the migration
    db.init_db(db_path)  # second run must be a no-op (idempotent)
    conn = db.connect(db_path)
    try:
        rows = db.list_provider_configs(conn, "u1")
        assert len(rows) == 1
        row = rows[0]
        assert row["label"] == "Default"
        assert row["provider"] == "openrouter"
        assert row["api_key_enc"] == "enc-k"
        assert row["base_url"] == "https://or.test/v1"
        assert row["max_iterations"] == 7
        assert row["temperature"] == 0.3
        assert row["max_tokens"] == 512
        assert row["is_default"] == 1
        # the unconfigured user gets no connection
        assert db.list_provider_configs(conn, "u2") == []
        # binding columns stay NULL (migrated connection is the default)
        cfg = db.get_config(conn, "u1")
        assert cfg["librarian_connection_id"] is None
        assert cfg["curator_connection_id"] is None
    finally:
        conn.close()


def test_create_user_defaults_schedule_enabled(conn, tmp_path):
    user = db.create_user(conn, "alice", "h")
    cfg = db.get_config(conn, user["id"])
    assert cfg["curate_schedule_enabled"] == 1
    assert cfg["curate_schedule_time"] == db.DEFAULT_SCHEDULE_TIME


def test_retention_defaults_bounded_for_new_users_only(conn, tmp_path):
    """A12: new config rows get bounded keeps; existing rows are never migrated."""
    assert db.DEFAULT_SNAPSHOT_KEEP > 0
    assert db.DEFAULT_TRACE_KEEP > 0
    assert db.DEFAULT_ACTIVITY_KEEP > 0
    user = db.create_user(conn, "alice", "h")
    cfg = db.get_config(conn, user["id"])
    assert cfg["snapshot_keep"] == db.DEFAULT_SNAPSHOT_KEEP
    assert cfg["trace_keep"] == db.DEFAULT_TRACE_KEEP
    assert cfg["activity_keep"] == db.DEFAULT_ACTIVITY_KEEP
    # an explicit keep-all (0) choice survives a re-init untouched
    db.update_library_settings(
        conn,
        user["id"],
        name=None,
        description=None,
        versioning=True,
        snapshot_keep=0,
        trace_keep=0,
        activity_keep=0,
    )
    db.init_db(tmp_path / "app.db")
    cfg = db.get_config(conn, user["id"])
    assert cfg["snapshot_keep"] == 0
    assert cfg["trace_keep"] == 0
    assert cfg["activity_keep"] == 0


def test_payload_keep_bounded_default_explicit_zero_survives(conn, tmp_path):
    """A12 pattern for payload_keep: bounded default for new rows; an explicit
    keep-all (0) survives a re-init; None in update_library_settings leaves
    the column untouched (existing callers predate the column)."""
    assert db.DEFAULT_PAYLOAD_KEEP > 0
    user = db.create_user(conn, "alice", "h")
    cfg = db.get_config(conn, user["id"])
    assert cfg["payload_keep"] == db.DEFAULT_PAYLOAD_KEEP
    # omitted payload_keep (None) = column untouched
    db.update_library_settings(
        conn,
        user["id"],
        name=None,
        description=None,
        versioning=True,
        snapshot_keep=0,
        trace_keep=0,
        activity_keep=0,
    )
    cfg = db.get_config(conn, user["id"])
    assert cfg["payload_keep"] == db.DEFAULT_PAYLOAD_KEEP
    # an explicit keep-all (0) choice survives a re-init untouched
    db.update_library_settings(
        conn,
        user["id"],
        name=None,
        description=None,
        versioning=True,
        snapshot_keep=0,
        trace_keep=0,
        activity_keep=0,
        payload_keep=0,
    )
    db.init_db(tmp_path / "app.db")
    cfg = db.get_config(conn, user["id"])
    assert cfg["payload_keep"] == 0


def test_update_curate_schedule_roundtrip(conn, tmp_path):
    user = db.create_user(conn, "alice", "h")
    db.update_curate_schedule(conn, user["id"], enabled=False, time_hhmm="22:30")
    cfg = db.get_config(conn, user["id"])
    assert cfg["curate_schedule_enabled"] == 0
    assert cfg["curate_schedule_time"] == "22:30"
    # empty time normalizes to the default
    db.update_curate_schedule(conn, user["id"], enabled=True, time_hhmm="")
    cfg = db.get_config(conn, user["id"])
    assert cfg["curate_schedule_enabled"] == 1
    assert cfg["curate_schedule_time"] == db.DEFAULT_SCHEDULE_TIME


def test_provider_config_first_is_auto_default(conn, tmp_path):
    user = db.create_user(conn, "hank", "h")
    first = db.create_provider_config(conn, user["id"], label="A", provider="openai")
    second = db.create_provider_config(conn, user["id"], label="B", provider="gemini")
    assert first["is_default"] == 1
    assert second["is_default"] == 0
    # list order: default first, then by creation time
    assert [r["label"] for r in db.list_provider_configs(conn, user["id"])] == ["A", "B"]


def test_set_default_provider_config_switches_atomically(conn, tmp_path):
    user = db.create_user(conn, "ida", "h")
    first = db.create_provider_config(conn, user["id"], label="A", provider="openai")
    second = db.create_provider_config(conn, user["id"], label="B", provider="gemini")
    db.set_default_provider_config(conn, user["id"], second["id"])
    assert db.get_provider_config(conn, user["id"], first["id"])["is_default"] == 0
    assert db.get_provider_config(conn, user["id"], second["id"])["is_default"] == 1
    # the partial unique index rejects a second raw default insert (P2)
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO provider_configs"
                " (id, user_id, label, provider, is_default, created_at)"
                " VALUES ('raw-1', ?, 'C', 'openai', 1, '2026-01-01T00:00:00Z')",
                (user["id"],),
            )


def test_delete_provider_config_blocked_while_bound(conn, tmp_path):
    user = db.create_user(conn, "jane", "h")
    first = db.create_provider_config(conn, user["id"], label="A", provider="openai")
    second = db.create_provider_config(conn, user["id"], label="B", provider="gemini")
    # bound to the librarian: deletion refused (D3)
    db.update_librarian_config(
        conn, user["id"], connection_id=second["id"], model="m", prompt_addendum=None
    )
    assert db.count_provider_config_bindings(conn, second["id"]) == 1
    with pytest.raises(db.ProviderConfigInUseError):
        db.delete_provider_config(conn, user["id"], second["id"])
    # the default with siblings is also refused (P5)
    with pytest.raises(db.ProviderConfigInUseError, match="default"):
        db.delete_provider_config(conn, user["id"], first["id"])
    # rebinding to NULL unblocks the delete
    db.update_librarian_config(
        conn, user["id"], connection_id=None, model="m", prompt_addendum=None
    )
    db.delete_provider_config(conn, user["id"], second["id"])
    assert db.get_provider_config(conn, user["id"], second["id"]) is None
    # deleting the last (default) connection is allowed: unconfigured state
    db.delete_provider_config(conn, user["id"], first["id"])
    assert db.list_provider_configs(conn, user["id"]) == []


def test_token_lifecycle(conn, tmp_path):
    user = db.create_user(conn, "erin", "h")
    plaintext, token_hash = security.generate_token()
    token = db.create_token(conn, user["id"], "agent-1", token_hash)
    assert token["label"] == "agent-1"
    assert token["revoked_at"] is None
    # lookup by hash (MCP auth path)
    found = db.lookup_token(conn, token_hash)
    assert found["id"] == token["id"]
    assert db.lookup_token(conn, security.hash_token(plaintext))["id"] == token["id"]
    # touch last_used
    db.touch_token(conn, token["id"])
    assert db.get_token(conn, token["id"])["last_used_at"] is not None
    # revoke only succeeds for the owner
    other = db.create_user(conn, "fred", "h")
    assert not db.revoke_token(conn, other["id"], token["id"])
    assert db.revoke_token(conn, user["id"], token["id"])
    assert db.get_token(conn, token["id"])["revoked_at"] is not None
    # second revoke is a no-op
    assert not db.revoke_token(conn, user["id"], token["id"])
    assert [t["id"] for t in db.list_tokens(conn, user["id"])] == [token["id"]]


# --- runtime connections (Attested Computations) -------------------------------


def test_runtime_connections_table_created(conn):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'runtime_connections'"
    ).fetchone()
    assert row is not None


def test_runtime_connection_crud_and_write_only_password(conn):
    password_enc = security.encrypt_secret("pg-password", "server-secret-key")
    created = db.create_runtime_connection(
        conn,
        label="Analytics",
        runtime="postgres",
        host="pg.internal",
        port=5432,
        dbname="analytics",
        username="ro_user",
        password_enc=password_enc,
    )
    assert created["id"]
    # get returns the full row (execution path needs the ciphertext)
    fetched = db.get_runtime_connection(conn, created["id"])
    assert fetched["label"] == "Analytics"
    assert fetched["password_enc"] == password_enc
    assert security.decrypt_secret(fetched["password_enc"], "server-secret-key") == "pg-password"
    # the list view never carries the ciphertext (write-only)
    listed = db.list_runtime_connections(conn)
    assert [row["id"] for row in listed] == [created["id"]]
    assert "password_enc" not in listed[0].keys()
    assert listed[0]["password_set"] == 1
    # update with password_enc=None keeps the stored ciphertext
    db.update_runtime_connection(
        conn,
        created["id"],
        label="Analytics RO",
        runtime="postgres",
        host="pg2.internal",
        port=5433,
        dbname="analytics",
        username="ro_user2",
    )
    updated = db.get_runtime_connection(conn, created["id"])
    assert updated["label"] == "Analytics RO"
    assert updated["host"] == "pg2.internal" and updated["port"] == 5433
    assert updated["password_enc"] == password_enc
    db.delete_runtime_connection(conn, created["id"])
    assert db.get_runtime_connection(conn, created["id"]) is None
    assert db.list_runtime_connections(conn) == []


def test_runtime_connection_sqlite_shape(conn):
    created = db.create_runtime_connection(
        conn, label="Local file", runtime="sqlite", dbname="/abs/path/data.db"
    )
    assert created["host"] is None and created["port"] is None
    assert created["username"] is None and created["password_enc"] is None
    assert db.list_runtime_connections(conn)[0]["password_set"] == 0


def test_runtime_connections_migration_on_preexisting_db(tmp_path):
    """init_db adds the NEW table to a database that predates it (A13)."""
    db_path = tmp_path / "app.db"
    with sqlite3.connect(db_path) as raw:
        raw.executescript(
            """
            CREATE TABLE users (
              id TEXT PRIMARY KEY,
              username TEXT UNIQUE NOT NULL,
              password_hash TEXT NOT NULL,
              is_admin INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL
            );
            INSERT INTO users (id, username, password_hash, created_at)
            VALUES ('u1', 'alice', 'h', '2026-01-01T00:00:00Z');
            """
        )
    db.init_db(db_path)
    db.init_db(db_path)  # idempotent
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'runtime_connections'"
        ).fetchone()
        assert row is not None
        created = db.create_runtime_connection(conn, label="L", runtime="sqlite", dbname="/x.db")
        assert created["id"]
    finally:
        conn.close()


def test_computation_execution_toggle_defaults_off(conn):
    assert db.get_app_setting(conn, "computation_execution_enabled", "0") == "0"
    db.set_app_setting(conn, "computation_execution_enabled", "1")
    assert db.get_app_setting(conn, "computation_execution_enabled", "0") == "1"
