"""Tests for athenaeum.computation (Attested Computations v1 sandbox)."""

import sqlite3
from types import SimpleNamespace

import pytest

import athenaeum.computation as computation
from athenaeum import db as db_module
from athenaeum.computation import (
    ComputationError,
    ComputationRunner,
    bind_parameters,
    check_read_only,
    execute,
    extract_sql,
    run_sqlite,
)
from athenaeum.library.backend import LibraryBackend

# --- extract_sql --------------------------------------------------------------


def test_extract_sql_finds_fence_with_and_without_info_string():
    fm = {"type": "Attested Computation", "runtime": "sqlite"}
    for fence in ("```sql", "```"):
        body = f"Some intro.\n\n# Computation\n\n{fence}\nSELECT 1\n```\n\n# Other\n"
        assert extract_sql(fm, body) == "SELECT 1"


def test_extract_sql_first_fence_wins():
    fm = {"type": "Attested Computation", "runtime": "sqlite"}
    body = "# Computation\n\n```sql\nSELECT 1\n```\n\n```sql\nSELECT 2\n```\n"
    assert extract_sql(fm, body) == "SELECT 1"


def test_extract_sql_missing_fence_refused():
    fm = {"type": "Attested Computation", "runtime": "sqlite"}
    with pytest.raises(ComputationError, match="Computation"):
        extract_sql(fm, "no section here\n")
    with pytest.raises(ComputationError, match="Computation"):
        extract_sql(fm, "# Computation\n\nno fence at all\n")


def test_extract_sql_external_computation_refused_in_v1():
    fm = {"type": "Attested Computation", "runtime": "sqlite", "computation": "q.sql"}
    with pytest.raises(ComputationError, match="not supported in v1"):
        extract_sql(fm, "# Computation\n\n```sql\nSELECT 1\n```\n")


# --- check_read_only ----------------------------------------------------------


def test_check_read_only_accepts_select_and_with():
    assert check_read_only("SELECT 1") == "SELECT 1"
    assert check_read_only("with t as (select 1) select * from t").startswith("with")
    assert check_read_only("-- a comment\nSELECT 1") == "SELECT 1"
    assert check_read_only("/* block */ SELECT 1") == "SELECT 1"
    assert check_read_only("SELECT 1;") == "SELECT 1"  # one trailing semicolon OK


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET x = 1",
        "DELETE FROM t",
        "DROP TABLE t",
        "PRAGMA journal_mode=WAL",
        "ATTACH DATABASE 'x' AS x",
        "SELECT 1; SELECT 2",
        "SELECT ';'",
        "",
        "   \n-- only a comment\n",
    ],
)
def test_check_read_only_refuses(sql):
    with pytest.raises(ComputationError):
        check_read_only(sql)


# --- bind_parameters ----------------------------------------------------------

SPEC = [
    {"name": "uid", "type": "integer", "required": True},
    {"name": "limit", "type": "number"},
    {"name": "active", "type": "boolean"},
    {"name": "tag", "type": "string"},
]


def test_bind_parameters_coercions():
    bound = bind_parameters(SPEC, {"uid": "42", "limit": "1.5", "active": "true", "tag": 7})
    assert bound == {"uid": 42, "limit": 1.5, "active": True, "tag": "7"}


def test_bind_parameters_optional_absent_is_dropped():
    assert bind_parameters(SPEC, {"uid": 1}) == {"uid": 1}


def test_bind_parameters_missing_required_refused():
    with pytest.raises(ComputationError, match="missing required parameter 'uid'"):
        bind_parameters(SPEC, {})


def test_bind_parameters_unknown_name_refused():
    with pytest.raises(ComputationError, match="unknown parameter"):
        bind_parameters(SPEC, {"uid": 1, "bogus": 2})


def test_bind_parameters_bad_coercion_refused():
    with pytest.raises(ComputationError, match="uid"):
        bind_parameters(SPEC, {"uid": "not-an-int"})


# --- sqlite execution -----------------------------------------------------------


def seed_sqlite(tmp_path, rows=("a", "b")):
    path = tmp_path / "data.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE items (name TEXT, n INTEGER)")
    conn.executemany("INSERT INTO items VALUES (?, ?)", [(name, i) for i, name in enumerate(rows)])
    conn.commit()
    conn.close()
    return path


FM_SQLITE = {
    "type": "Attested Computation",
    "runtime": "sqlite",
    "parameters": [{"name": "target", "type": "string", "required": True}],
}
BODY = "# Computation\n\n```sql\nSELECT name, n FROM items WHERE name = :target\n```\n"


def conn_row(target, **overrides):
    row = {"id": "conn-1", "runtime": "sqlite", "dbname": str(target)}
    row.update(overrides)
    return row


def test_sqlite_happy_path_receipt(tmp_path):
    dbname = seed_sqlite(tmp_path)
    receipt = execute(conn_row(dbname), None, FM_SQLITE, BODY, {"target": "a"})
    assert receipt["runtime"] == "sqlite"
    assert receipt["connection_id"] == "conn-1"
    assert receipt["columns"] == ["name", "n"]
    assert receipt["rows"] == [["a", 0]]
    assert receipt["row_count"] == 1
    assert receipt["truncated"] is False
    assert receipt["duration_ms"] >= 0
    assert receipt["executed_at"]


def test_sqlite_row_cap_truncates(tmp_path):
    dbname = seed_sqlite(tmp_path, rows=("a", "b", "c"))
    body = "# Computation\n\n```\nSELECT name FROM items ORDER BY name\n```\n"
    receipt = execute(conn_row(dbname), None, FM_SQLITE, body, {"target": "a"}, row_cap=1)
    assert receipt["rows"] == [["a"]]
    assert receipt["row_count"] == 1
    assert receipt["truncated"] is True


def test_sqlite_timeout(tmp_path):
    dbname = seed_sqlite(tmp_path)
    body = (
        "# Computation\n\n```sql\nWITH RECURSIVE cnt(x) AS"
        " (SELECT 1 UNION ALL SELECT x+1 FROM cnt) SELECT count(*) FROM cnt\n```\n"
    )
    with pytest.raises(ComputationError, match="timed out"):
        execute(conn_row(dbname), None, FM_SQLITE, body, {"target": "a"}, timeout_s=1)


def test_sqlite_write_refused_before_connect(tmp_path):
    dbname = seed_sqlite(tmp_path)
    body = "# Computation\n\n```sql\nDELETE FROM items\n```\n"
    with pytest.raises(ComputationError, match="SELECT/WITH"):
        execute(conn_row(dbname), None, FM_SQLITE, body, {"target": "a"})


def test_sqlite_missing_file_clean_error(tmp_path):
    with pytest.raises(ComputationError, match="read-only"):
        execute(
            conn_row(tmp_path / "nope.db"),
            None,
            {"type": "Attested Computation", "runtime": "sqlite"},
            "# Computation\n\n```sql\nSELECT 1\n```\n",
            None,
        )


# --- sqlite dbname validation (URI metacharacter guard) -------------------------


@pytest.mark.parametrize("dbname", ["x.db?mode=rw", "x#y", "", "x\x00y"])
def test_run_sqlite_rejects_uri_metacharacters(dbname):
    """`?`/`#`/NUL could re-parameterize the file:...?mode=ro URI; empty is useless."""
    with pytest.raises(ComputationError, match="invalid sqlite database path"):
        run_sqlite(dbname, "SELECT 1", {}, timeout_s=1, row_cap=10)


def test_run_sqlite_rejection_happens_before_connect(tmp_path):
    """A poisoned dbname must not even attempt the connect (defense in depth)."""
    poisoned = f"{tmp_path / 'data.db'}?mode=rw"
    with pytest.raises(ComputationError, match="invalid sqlite database path"):
        run_sqlite(poisoned, "SELECT 1", {}, timeout_s=1, row_cap=10)


def test_run_sqlite_valid_file_still_runs(tmp_path):
    dbname = seed_sqlite(tmp_path)
    result = run_sqlite(
        str(dbname), "SELECT name FROM items WHERE n = :n", {"n": 0}, timeout_s=5, row_cap=10
    )
    assert result["rows"] == [["a"]]
    assert result["truncated"] is False


# --- postgres execution (fake psycopg) ------------------------------------------


class FakePgCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = [SimpleNamespace(name="n")]

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))

    def fetchmany(self, n):
        self.conn.fetchmany_calls.append(n)
        return [(1,)]


class FakePgConnection:
    def __init__(self):
        self.read_only = False
        self.executed = []
        self.fetchmany_calls = []
        self.closed = False

    def cursor(self):
        return FakePgCursor(self)

    def close(self):
        self.closed = True


class FakePsycopg:
    """Drop-in for the psycopg module: records connect kwargs, serves a fake."""

    def __init__(self):
        self.connect_calls = []
        self.connection = FakePgConnection()

    def connect(self, **kwargs):
        self.connect_calls.append(kwargs)
        return self.connection


def test_postgres_sandbox_enforced(tmp_path, monkeypatch):
    fake = FakePsycopg()
    monkeypatch.setattr(computation, "psycopg", fake)
    row = conn_row(
        "unused",
        runtime="postgres",
        host="pg.internal",
        port=5432,
        dbname="analytics",
        username="ro_user",
    )
    fm = {"type": "Attested Computation", "runtime": "postgres"}
    body = "# Computation\n\n```sql\nSELECT 1 AS n\n```\n"
    receipt = execute(row, "s3cret", fm, body, None)
    (connect_kwargs,) = fake.connect_calls
    assert connect_kwargs["host"] == "pg.internal"
    assert connect_kwargs["port"] == 5432
    assert connect_kwargs["dbname"] == "analytics"
    assert connect_kwargs["user"] == "ro_user"
    assert connect_kwargs["password"] == "s3cret"
    assert connect_kwargs["connect_timeout"] >= 1
    assert fake.connection.read_only is True  # READ ONLY transaction enforced
    set_config, query = fake.connection.executed
    assert set_config[0] == "SELECT set_config('statement_timeout', %s, true)"
    assert set_config[1] == (str(10_000),)
    assert query == ("SELECT 1 AS n", {})  # mapping-bound, never interpolated
    assert receipt["columns"] == ["n"]
    assert receipt["rows"] == [[1]]
    assert receipt["row_count"] == 1
    assert receipt["truncated"] is False
    assert receipt["runtime"] == "postgres"
    assert fake.connection.closed is True


def test_postgres_connect_failure_sanitized(tmp_path, monkeypatch):
    class DownPsycopg:
        def connect(self, **kwargs):
            raise RuntimeError("connection refused for user=ro_user password=s3cret")

    monkeypatch.setattr(computation, "psycopg", DownPsycopg())
    row = conn_row("unused", runtime="postgres", host="h", port=1, dbname="d", username="ro_user")
    with pytest.raises(ComputationError) as excinfo:
        execute(
            row,
            "s3cret",
            {"runtime": "postgres"},
            "# Computation\n```sql\nSELECT 1\n```\n",
            None,
        )
    assert "s3cret" not in str(excinfo.value)


def test_postgres_connect_failure_scrubs_conninfo(tmp_path, monkeypatch):
    """SERVER-10: driver errors carrying conninfo leak neither host, port,
    username, nor password — each is replaced with ***."""
    conninfo_message = (
        'connection to server at "pg.internal" (10.0.0.5), port 5432 failed: '
        'FATAL: password authentication failed for user "ro_user"'
    )

    class DownPsycopg:
        def connect(self, **kwargs):
            raise RuntimeError(conninfo_message)

    monkeypatch.setattr(computation, "psycopg", DownPsycopg())
    row = conn_row(
        "unused", runtime="postgres", host="pg.internal", port=5432, dbname="d", username="ro_user"
    )
    with pytest.raises(ComputationError) as excinfo:
        execute(
            row,
            "s3cret",
            {"runtime": "postgres"},
            "# Computation\n```sql\nSELECT 1\n```\n",
            None,
        )
    message = str(excinfo.value)
    for leaked in ("pg.internal", "5432", "ro_user", "s3cret"):
        assert leaked not in message
    assert "***" in message


def test_postgres_query_failure_scrubs_conninfo(tmp_path, monkeypatch):
    """The query error path scrubs the same values as the connect path."""

    class FailOnQueryPsycopg:
        class _Conn:
            read_only = False

            def cursor(self):
                return self

            def execute(self, sql, params=None):
                if sql.startswith("SELECT set_config"):
                    return
                raise RuntimeError('relation "secrets" of ro_user@pg.internal does not exist')

            def close(self):
                return None

        def connect(self, **kwargs):
            return self._Conn()

    monkeypatch.setattr(computation, "psycopg", FailOnQueryPsycopg())
    row = conn_row(
        "unused", runtime="postgres", host="pg.internal", port=5432, dbname="d", username="ro_user"
    )
    with pytest.raises(ComputationError, match="postgres query failed") as excinfo:
        execute(
            row,
            "s3cret",
            {"runtime": "postgres"},
            "# Computation\n```sql\nSELECT 1\n```\n",
            None,
        )
    message = str(excinfo.value)
    assert "pg.internal" not in message
    assert "ro_user" not in message


# --- execute: runtime gating ----------------------------------------------------


@pytest.mark.parametrize("runtime", ["python", "dbt", "bigquery"])
def test_execute_refuses_unsupported_runtimes(runtime):
    fm = {"type": "Attested Computation", "runtime": runtime}
    with pytest.raises(ComputationError, match="not supported in v1"):
        execute(conn_row("db"), None, fm, "# Computation\n```sql\nSELECT 1\n```\n", None)


# --- ComputationRunner gating ---------------------------------------------------


@pytest.fixture
def stack(tmp_path):
    """Real app db + real backend with one Attested Computation concept."""
    db_path = tmp_path / "app.db"
    db_module.init_db(db_path)
    backend = LibraryBackend(tmp_path / "lib", actor="test")
    backend.init_bundle()
    backend.create_concept(
        "/q.md",
        {"type": "Attested Computation", "runtime": "sqlite", "title": "Q"},
        "# Computation\n\n```sql\nSELECT 1 AS n\n```\n",
    )
    runner = ComputationRunner(db_path)
    return SimpleNamespace(db_path=db_path, backend=backend, runner=runner)


def set_execution_enabled(db_path, enabled: bool) -> None:
    with db_module.connect(db_path) as conn:
        db_module.set_app_setting(conn, "computation_execution_enabled", "1" if enabled else "0")


def create_connection(db_path, **kwargs) -> str:
    with db_module.connect(db_path) as conn:
        row = db_module.create_runtime_connection(conn, **kwargs)
        return row["id"]


def test_runner_toggle_off_refused(stack, tmp_path):
    dbname = seed_sqlite(tmp_path)
    cid = create_connection(stack.db_path, label="L", runtime="sqlite", dbname=str(dbname))
    with pytest.raises(ComputationError, match="disabled"):
        stack.runner.run(stack.backend, "/q.md", cid, None)


def test_runner_wrong_concept_type_refused(stack, tmp_path):
    dbname = seed_sqlite(tmp_path)
    cid = create_connection(stack.db_path, label="L", runtime="sqlite", dbname=str(dbname))
    set_execution_enabled(stack.db_path, True)
    stack.backend.create_concept("/note.md", {"type": "Note", "title": "N"}, "x\n")
    with pytest.raises(ComputationError, match="not an Attested Computation"):
        stack.runner.run(stack.backend, "/note.md", cid, None)


def test_runner_unknown_connection_refused(stack):
    set_execution_enabled(stack.db_path, True)
    with pytest.raises(ComputationError, match="unknown connection id"):
        stack.runner.run(stack.backend, "/q.md", "no-such-id", None)


def test_runner_runtime_mismatch_refused(stack, tmp_path):
    cid = create_connection(
        stack.db_path, label="L", runtime="postgres", host="h", port=1, dbname="d", username="u"
    )
    set_execution_enabled(stack.db_path, True)
    with pytest.raises(ComputationError, match="does not match"):
        stack.runner.run(stack.backend, "/q.md", cid, None)


def test_runner_missing_concept_refused(stack):
    set_execution_enabled(stack.db_path, True)
    with pytest.raises(ComputationError, match="no such concept"):
        stack.runner.run(stack.backend, "/gone.md", "any", None)


def test_runner_happy_path_and_decryption(stack, tmp_path, monkeypatch):
    """End to end through the runner: toggle on, encrypted password decrypted."""
    dbname = seed_sqlite(tmp_path)
    cid = create_connection(
        stack.db_path, label="L", runtime="sqlite", dbname=str(dbname), password_enc="enc:pw"
    )
    set_execution_enabled(stack.db_path, True)
    decrypts = []
    runner = ComputationRunner(
        stack.db_path, key_decryptor=lambda value: decrypts.append(value) or value[4:]
    )
    stack.backend.edit_concept(
        "/q.md",
        frontmatter_patch={"parameters": [{"name": "target", "type": "string", "required": True}]},
        new_body="# Computation\n\n```sql\nSELECT name FROM items WHERE name = :target\n```\n",
    )
    receipt = runner.run(stack.backend, "/q.md", cid, {"target": "a"})
    assert decrypts == ["enc:pw"]  # the injected decryptor was applied
    assert receipt["rows"] == [["a"]]
    assert receipt["connection_id"] == cid


# --- internal librarian-loop tool -----------------------------------------------


def test_run_computation_is_not_a_write_action():
    """CRITICAL: never a tracker write — no no-write/embedding-sync interference."""
    from athenaeum.librarian.tools import WRITE_ACTIONS

    assert "run_computation" not in WRITE_ACTIONS


async def test_dispatch_run_computation_receipt_and_clean_tracker(stack, tmp_path):
    """Dispatch routes to the runner; the receipt carries no 'id' key, so even
    the generic tracker guard cannot record a write."""
    from athenaeum.librarian.tools import dispatch

    dbname = seed_sqlite(tmp_path)
    cid = create_connection(stack.db_path, label="L", runtime="sqlite", dbname=str(dbname))
    set_execution_enabled(stack.db_path, True)
    receipt = await dispatch(
        "run_computation",
        {"path": "/q.md", "connection_id": cid},
        stack.backend,
        computation_runner=stack.runner,
    )
    assert receipt["columns"] == ["n"]
    assert receipt["rows"] == [[1]]
    assert "id" not in receipt  # the tracker guard keys on 'id'


async def test_dispatch_run_computation_unavailable_without_runner(stack):
    from athenaeum.librarian.tools import dispatch

    with pytest.raises(ValueError, match="not available"):
        await dispatch(
            "run_computation",
            {"path": "/q.md", "connection_id": "any"},
            stack.backend,
            computation_runner=None,
        )
