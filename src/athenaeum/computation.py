"""Attested Computations v1: sandboxed execution of ``type: Attested Computation``.

One execution path (``ComputationRunner.run``), two front doors: the external
MCP tool ``run_computation`` and the internal librarian-loop tool of the same
name. v1 scope: runtimes ``postgres`` (via psycopg 3, driven sync) and
``sqlite`` (stdlib), the body ``# Computation`` fenced SQL block only.

Sandbox policy (all enforced here, per execution):
- the admin execution toggle (``app_settings.computation_execution_enabled``,
  default off) is read LIVE on every run;
- SQL must be a single read-only statement: first keyword SELECT or WITH, at
  most one trailing semicolon (a semicolon inside a string literal is also
  rejected — documented v1 limitation);
- connection-level read-only enforcement is the REAL boundary (postgres
  READ ONLY transaction; sqlite ``mode=ro`` + ``PRAGMA query_only``) — the
  textual check is defense-in-depth plus clear errors. No SQL-parser
  dependency: ``sqlparse`` is a formatter without reliable write-detection
  and ``sqlglot`` is a heavy dependency for one check;
- statement timeout (server-side on postgres, thread-interrupt on sqlite)
  and a fixed row cap;
- credentials are Fernet-encrypted at rest, decrypted only in process memory
  at execution time, and never appear in error messages or receipts.

Placeholder conventions: postgres uses psycopg pyformat ``%(name)s``; sqlite
uses native named ``:name``. Parameters are ALWAYS bound via driver
placeholders — never string interpolation.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from athenaeum import db

DEFAULT_TIMEOUT_S = 10.0  # fixed in v1
DEFAULT_ROW_CAP = 500  # fixed in v1

SUPPORTED_RUNTIMES = ("postgres", "sqlite")

_HEADING_RE = re.compile(r"^#\s+Computation\s*$")
_FENCE_RE = re.compile(r"^```\s*(sql)?\s*$", re.IGNORECASE)


class ComputationError(Exception):
    """A recoverable, client-safe computation failure.

    Messages must never contain credentials or conninfo — they are shown to
    MCP clients and fed back to the LLM in the librarian loop.
    """


def extract_sql(frontmatter: dict, body: str) -> str:
    """The SQL of the body ``# Computation`` section's first fenced block."""
    if frontmatter.get("computation") is not None:
        # A URL fetch would be an SSRF primitive; bundle-relative resolution
        # is future work (out of scope for v1).
        raise ComputationError("external computation paths are not supported in v1")
    in_section = False
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if _HEADING_RE.match(line):
            in_section = True
            continue
        if not in_section:
            continue
        if line.startswith("#"):
            break  # the next heading ends the Computation section
        if _FENCE_RE.match(line.strip()):
            block: list[str] = []
            for follow in lines[i + 1 :]:
                if follow.strip().startswith("```"):
                    sql = "\n".join(block).strip()
                    if sql:
                        return sql
                    break
                block.append(follow)
            break  # unterminated or empty fence: fall through to the error
    raise ComputationError("no '# Computation' fenced SQL block found in the concept body")


def _strip_leading_comments(sql: str) -> str:
    text = sql
    while True:
        text = text.lstrip()
        if text.startswith("--"):
            _, _, text = text.partition("\n")
            continue
        if text.startswith("/*"):
            end = text.find("*/")
            if end == -1:
                return text  # unterminated comment: the keyword check fails it
            text = text[end + 2 :]
            continue
        return text


def check_read_only(sql: str) -> str:
    """Validate a single read-only statement; return the cleaned SQL."""
    cleaned = _strip_leading_comments(sql)
    if not cleaned:
        raise ComputationError("empty SQL statement")
    first = cleaned.split(None, 1)[0].upper() if cleaned.split(None, 1) else ""
    if first not in ("SELECT", "WITH"):
        raise ComputationError("only read-only SELECT/WITH statements are allowed")
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].rstrip()
    if ";" in cleaned:
        raise ComputationError(
            "only a single statement is allowed (semicolons, even inside string "
            "literals, are rejected in v1)"
        )
    return cleaned


def bind_parameters(spec: list[dict], given: dict | None) -> dict:
    """Bind caller-given values against the frontmatter ``parameters`` spec.

    Strict contract: a missing required parameter, an unknown given name, or
    a coercion failure is a ComputationError. Values are returned for driver
    placeholder binding — never interpolated into the SQL text.
    """
    remaining = dict(given or {})
    bound: dict[str, Any] = {}
    for entry in spec or []:
        if not isinstance(entry, dict) or not entry.get("name"):
            raise ComputationError("parameters[] entries require a non-empty 'name'")
        name = entry["name"]
        value = remaining.pop(name, None)
        if value is None:
            if entry.get("required"):
                raise ComputationError(f"missing required parameter '{name}'")
            continue
        bound[name] = _coerce(name, value, entry.get("type"))
    if remaining:
        raise ComputationError(f"unknown parameter(s): {', '.join(sorted(remaining))}")
    return bound


def _coerce(name: str, value: Any, declared: str | None) -> Any:
    try:
        if declared == "integer":
            if isinstance(value, bool):
                raise ValueError("a boolean is not an integer")
            return int(value)
        if declared == "number":
            return float(value)
        if declared == "boolean":
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.lower() in ("true", "false"):
                return value.lower() == "true"
            if isinstance(value, (int, float)) and value in (0, 1):
                return bool(value)
            raise ValueError(f"cannot coerce {value!r} to boolean")
        return str(value)  # 'string' and any other declared type
    except (TypeError, ValueError) as exc:
        raise ComputationError(f"parameter '{name}': {exc}") from exc


def _json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return value


def _driver_message(exc: Exception, secret: str | None = None) -> str:
    """Client-safe driver error text: type name + message, password scrubbed."""
    msg = f"{type(exc).__name__}: {exc}"
    if secret:
        msg = msg.replace(secret, "***")
    return msg


def run_sqlite(dbname: str, sql: str, params: dict, *, timeout_s: float, row_cap: int) -> dict:
    """Run the statement read-only against a sqlite file (``:name`` placeholders)."""
    try:
        conn = sqlite3.connect(f"file:{dbname}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ComputationError(f"cannot open the sqlite database read-only: {exc}") from exc
    try:
        conn.execute("PRAGMA query_only = ON")  # belt and braces on top of mode=ro
        timer = threading.Timer(timeout_s, conn.interrupt)
        timer.start()
        try:
            cursor = conn.execute(sql, params)
            columns = [d[0] for d in cursor.description or []]
            rows = cursor.fetchmany(row_cap + 1)
        except sqlite3.Error as exc:
            if "interrupted" in str(exc):
                raise ComputationError(f"query timed out after {timeout_s:g} s") from exc
            raise ComputationError(f"sqlite query failed: {_driver_message(exc)}") from exc
        finally:
            timer.cancel()
    finally:
        conn.close()
    truncated = len(rows) > row_cap
    kept = rows[:row_cap]
    return {
        "columns": columns,
        "rows": [[_json_safe(value) for value in row] for row in kept],
        "row_count": len(kept),
        "truncated": truncated,
    }


def run_postgres(
    conn_row: dict, password: str | None, sql: str, params: dict, *, timeout_s: float, row_cap: int
) -> dict:
    """Run the statement read-only against postgres (``%(name)s`` placeholders)."""
    try:
        conn = psycopg.connect(
            host=conn_row.get("host"),
            port=conn_row.get("port"),
            dbname=conn_row.get("dbname"),
            user=conn_row.get("username"),
            password=password,
            connect_timeout=max(1, int(min(timeout_s, DEFAULT_TIMEOUT_S))),
        )
    except Exception as exc:
        raise ComputationError(
            f"cannot connect to postgres: {_driver_message(exc, password)}"
        ) from exc
    try:
        conn.read_only = True  # psycopg3: the transaction is READ ONLY
        try:
            cursor = conn.cursor()
            # Per-transaction server-side timeout (true = LOCAL to this transaction).
            cursor.execute(
                "SELECT set_config('statement_timeout', %s, true)", (str(int(timeout_s * 1000)),)
            )
            cursor.execute(sql, params)
            columns = [d.name for d in cursor.description or []]
            rows = cursor.fetchmany(row_cap + 1)
        except Exception as exc:
            raise ComputationError(
                f"postgres query failed: {_driver_message(exc, password)}"
            ) from exc
    finally:
        conn.close()
    truncated = len(rows) > row_cap
    kept = rows[:row_cap]
    return {
        "columns": columns,
        "rows": [[_json_safe(value) for value in row] for row in kept],
        "row_count": len(kept),
        "truncated": truncated,
    }


def execute(
    conn_row: dict,
    password: str | None,
    frontmatter: dict,
    body: str,
    parameters: dict | None,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    row_cap: int = DEFAULT_ROW_CAP,
) -> dict:
    """Orchestrate runtime check, extract/check/bind, and the sandboxed run.

    The receipt is returned to the caller only — v1 never writes it back into
    the concept's frontmatter. Receipts never contain conninfo/credentials.
    """
    runtime = frontmatter.get("runtime")
    if runtime not in SUPPORTED_RUNTIMES:
        raise ComputationError(
            f"runtime {runtime!r} is not supported in v1 "
            f"(supported: {', '.join(SUPPORTED_RUNTIMES)})"
        )
    sql = check_read_only(extract_sql(frontmatter, body))
    bound = bind_parameters(frontmatter.get("parameters") or [], parameters)
    started = time.perf_counter()
    if runtime == "sqlite":
        result = run_sqlite(conn_row["dbname"], sql, bound, timeout_s=timeout_s, row_cap=row_cap)
    else:
        result = run_postgres(conn_row, password, sql, bound, timeout_s=timeout_s, row_cap=row_cap)
    return {
        "runtime": runtime,
        "connection_id": conn_row["id"],
        "columns": result["columns"],
        "rows": result["rows"],
        "row_count": result["row_count"],
        "truncated": result["truncated"],
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        "executed_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


class ComputationRunner:
    """The single execution path: toggle gate -> concept/connection checks -> run.

    Constructed with the app DB path and the manager's key decryptor (None =
    stored values are plaintext, matching the provider_configs semantics).
    All methods are synchronous — callers wrap them in ``asyncio.to_thread``.
    """

    def __init__(self, db_path: str | Path, key_decryptor=None) -> None:
        self._db_path = Path(db_path)
        self._key_decryptor = key_decryptor

    def run(
        self, backend, concept_path: str, connection_id: str, parameters: dict | None = None
    ) -> dict:
        with db.connect(self._db_path) as conn:
            # The toggle is read LIVE on every execution (default off).
            if db.get_app_setting(conn, "computation_execution_enabled", "0") != "1":
                raise ComputationError("computation execution is disabled by the admin")
            row = db.get_runtime_connection(conn, connection_id)
        try:
            doc = backend.read_document(concept_path)
        except FileNotFoundError as exc:
            raise ComputationError(f"no such concept: {concept_path!r}") from exc
        frontmatter = doc.get("frontmatter") or {}
        if frontmatter.get("type") != "Attested Computation":
            raise ComputationError(f"concept {concept_path!r} is not an Attested Computation")
        if row is None:
            raise ComputationError(f"unknown connection id: {connection_id!r}")
        conn_row = dict(row)
        if conn_row.get("runtime") != frontmatter.get("runtime"):
            raise ComputationError(
                f"connection runtime {conn_row.get('runtime')!r} does not match "
                f"the concept runtime {frontmatter.get('runtime')!r}"
            )
        password = conn_row.get("password_enc")
        if password and self._key_decryptor is not None:
            password = self._key_decryptor(password)
        return execute(conn_row, password, frontmatter, doc.get("body") or "", parameters)
