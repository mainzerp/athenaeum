"""WebUI smoke + isolation tests (self-contained: local app factory, fake backend).

Streams A/B are developed in parallel, so the LibraryBackend and the LLM
provider layer are replaced here by fakes honoring the pinned contracts
(plan §3.2 read/versioning surface, §3.4 provider factory).
"""

import asyncio
import io
import json
import re
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from athenaeum import db as db_module
from athenaeum import security
from athenaeum.config import get_settings
from athenaeum.librarian.embed.local import LOCAL_MODEL_SHORTLIST
from athenaeum.librarian.gate import RunGate
from athenaeum.library.backend import provision_library
from athenaeum.library.gittool import GitError
from athenaeum.webui import ROUTERS, deps, routes_auth, routes_library
from conftest import CsrfTestClient

SECRET = "test-secret-key-webui-0123456789ab"  # >= 32 chars (SERVER-03 validator)


class FakeBackend:
    """Minimal stand-in for the plan §3.2 read-only/history surface."""

    def __init__(self, docs, commits=(), *, diff="", configured=True, available=True):
        self.docs = docs
        self.reconciled = False
        self._commits = list(commits)
        self._diff = diff
        self.history_configured = configured
        self.history_available = available
        self.restored: list[tuple[str, str]] = []
        self.edited: list[tuple[str, str]] = []
        self.deleted: list[str] = []

    def list_dir(self, path: str = "/") -> list[dict]:
        prefix = "/" if path == "/" else path.rstrip("/") + "/"
        if path != "/" and not any(p.startswith(prefix) for p in self.docs):
            raise FileNotFoundError(path)
        children: dict[str, bool] = {}
        for doc_path in self.docs:
            if not doc_path.startswith(prefix):
                continue
            head, _, tail = doc_path[len(prefix) :].partition("/")
            children.setdefault(head, bool(tail))
        entries = []
        for name, is_dir in sorted(children.items()):
            child_path = prefix + name
            entry = {"name": name, "path": child_path, "is_directory": is_dir}
            if not is_dir:
                fm = self.docs[child_path]["frontmatter"]
                for key in ("title", "type", "description"):
                    if key in fm:
                        entry[key] = fm[key]
            entries.append(entry)
        return entries

    def read_document(self, path: str) -> dict:
        if path not in self.docs:
            raise FileNotFoundError(path)
        doc = self.docs[path]
        return {"path": path, "frontmatter": doc["frontmatter"], "body": doc["body"]}

    def list_commits(self, limit: int = 200) -> list[dict]:
        return self._commits

    def git_head(self) -> str | None:
        return self._commits[0]["sha"] if self._commits else None

    def commit_diff(self, sha: str) -> str:
        if not any(c["sha"] == sha for c in self._commits):
            raise GitError(f"unknown commit: {sha}")
        return self._diff

    def file_history(self, path: str, limit: int = 100) -> list[dict]:
        return self._commits

    def read_document_at(self, path: str, sha: str) -> dict:
        if not any(c["sha"] == sha or c["sha"].startswith(sha) for c in self._commits):
            raise GitError(f"unknown commit: {sha}")
        if path not in self.docs:
            raise FileNotFoundError(path)
        doc = self.docs[path]
        return {
            "path": path,
            "frontmatter": doc["frontmatter"],
            "body": f"historical body at {sha[:7]}\n",
        }

    def file_diff_at(self, sha: str, path: str) -> str:
        return f"diff for {path} at {sha[:7]}"

    def restore_file_from_commit(self, path: str, sha: str) -> None:
        self.restored.append((path, sha))

    def file_diff_to_head(self, sha: str, path: str, context: int | None = None) -> str:
        if not any(c["sha"] == sha or c["sha"].startswith(sha) for c in self._commits):
            raise GitError(f"unknown commit: {sha}")
        if path not in self.docs:
            raise FileNotFoundError(path)
        self.diff_context = context
        return self._diff

    def edit_concept(self, path: str, *, new_body=None, agent_label=None, **kwargs) -> dict:
        if path not in self.docs:
            raise FileNotFoundError(path)
        self.docs[path]["body"] = new_body
        self.edited.append((path, new_body))
        return {"id": path, "action": "updated"}

    def delete_concept(self, path: str, *, agent_label=None) -> dict:
        if path not in self.docs:
            raise FileNotFoundError(path)
        del self.docs[path]
        self.deleted.append(path)
        return {"id": path, "action": "deleted", "inbound_links": []}

    def revert_commit(self, sha: str) -> None:
        return None

    def reset_to_commit(self, sha: str) -> None:
        return None

    def git_pull(self) -> None:
        return None

    def validate(self, scope: str | None = None) -> dict:
        return {"errors": [], "warnings": ["example warning"]}

    def reconcile(self) -> None:
        self.reconciled = True


def make_docs(username: str) -> dict:
    return {
        "/index.md": {"frontmatter": {}, "body": "# Index\n"},
        "/log.md": {
            "frontmatter": {},
            "body": "# Log\n\n## 2026-01-01\n\n* **Initialization** bundle created\n",
        },
        f"/user-{username}.md": {
            "frontmatter": {"title": f"Private to {username}", "type": "Note"},
            "body": f"Only {username} has this document.\n",
        },
        "/concepts/alpha.md": {
            "frontmatter": {
                "title": "Alpha",
                "type": "Concept",
                "tags": ["x", "y"],
                "verified": [{"by": "human:alice", "at": "2026-01-01"}],
            },
            "body": "See [Beta](/concepts/beta.md).\n",
        },
        "/concepts/beta.md": {
            "frontmatter": {"title": "Beta", "type": "Note", "stale_after": "2000-01-01"},
            "body": "Beta body.\n",
        },
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ATHENAEUM_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("ATHENAEUM_SECRET_KEY", SECRET)

    backends: dict[str, FakeBackend] = {}

    def fake_backend_factory(settings, user, conn):
        user_id = user["id"]
        if user_id not in backends:
            backends[user_id] = FakeBackend(make_docs(user["username"]))
        return backends[user_id]

    monkeypatch.setattr(deps, "get_library_backend", fake_backend_factory)

    class FakeManager:
        def __init__(self):
            self.evicted: list[str] = []
            self.run_gate = RunGate()  # real gate: import acquires it for real

        def evict(self, user_id: str) -> None:
            self.evicted.append(user_id)

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key=SECRET)
    for router in ROUTERS:
        app.include_router(router)
    app.state.librarian_manager = FakeManager()

    client = CsrfTestClient(app, follow_redirects=False)
    return client, backends, tmp_path


def make_user(data_root, username, password, *, admin=False):
    db_path = Path(data_root) / "app.db"
    db_module.init_db(db_path)
    conn = db_module.connect(db_path)
    try:
        user = db_module.create_user(
            conn, username, security.hash_password(password), is_admin=admin
        )
    finally:
        conn.close()
    provision_library(data_root, user["id"])
    return user


def read_config(data_root, user_id):
    conn = db_module.connect(Path(data_root) / "app.db")
    try:
        return db_module.get_config(conn, user_id)
    finally:
        conn.close()


def read_connections(data_root, user_id):
    conn = db_module.connect(Path(data_root) / "app.db")
    try:
        return db_module.list_provider_configs(conn, user_id)
    finally:
        conn.close()


def login(client, username, password):
    response = client.post("/login", data={"username": username, "password": password})
    assert response.status_code == 303
    return response


# --- first-run setup / auth ---------------------------------------------------


def test_first_run_setup_creates_admin(env):
    client, _, data_root = env
    # with an empty users table everything funnels into /setup
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"
    response = client.get("/login")
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"

    assert "First-run setup" in client.get("/setup").text

    # mismatched confirmation re-renders with an error
    response = client.post(
        "/setup",
        data={"username": "owner", "password": "owner-password-1", "confirm": "owner-password-2"},
    )
    assert response.status_code == 200
    assert "do not match" in response.text

    response = client.post(
        "/setup",
        data={"username": "owner", "password": "owner-password-1", "confirm": "owner-password-1"},
    )
    assert response.status_code == 303

    user = db_module.get_user_by_username(db_module.connect(Path(data_root) / "app.db"), "owner")
    assert user is not None and user["is_admin"] == 1

    # setup is gone once a user exists; the new session is already logged in
    response = client.get("/setup")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert client.get("/library/tree").status_code == 200


def test_login_logout(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")

    response = client.post("/login", data={"username": "alice", "password": "wrong"})
    assert response.status_code == 200
    assert "Invalid username or password" in response.text

    login(client, "alice", "pw")
    response = client.get("/library/tree")
    assert response.status_code == 200
    assert "concepts/" in response.text

    response = client.post("/logout")
    assert response.status_code == 303
    response = client.get("/library/tree")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# --- CSRF protection (CS-8) ----------------------------------------------------


def test_csrf_post_without_token_rejected(env):
    """Mutating form POSTs without the session CSRF token get 403."""
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    response = client.post("/tokens", data={"label": "agent"}, csrf=False)
    assert response.status_code == 403
    # the login form is protected too (tokenless POST even with valid creds)
    response = client.post("/login", data={"username": "alice", "password": "pw"}, csrf=False)
    assert response.status_code == 403


def test_csrf_post_with_wrong_token_rejected(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    response = client.post("/tokens", data={"label": "agent", "csrf_token": "forged"}, csrf=False)
    assert response.status_code == 403


def test_csrf_post_with_token_accepted(env):
    """The token rendered into forms (and auto-attached by the test client)
    passes validation."""
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    # the token is rendered into every mutating form
    response = client.get("/tokens")
    assert response.status_code == 200
    assert 'name="csrf_token"' in response.text

    response = client.post("/tokens", data={"label": "agent"})
    assert response.status_code == 200

    # htmx partial POSTs are protected the same way
    response = client.post("/config/provider/some-id/test", data={"api_key": ""}, csrf=False)
    assert response.status_code == 403


def test_login_throttling_locks_out_after_failures(env):
    """CS-2: per-account lockout with backoff after repeated failures."""
    client, _, data_root = env
    make_user(data_root, "alice", "alice-password-1")

    for _ in range(db_module.LOGIN_MAX_FAILURES):
        response = client.post("/login", data={"username": "alice", "password": "wrong"})
        assert response.status_code == 200
        assert "Invalid username or password" in response.text

    # the lockout is active: even the CORRECT password is refused
    response = client.post("/login", data={"username": "alice", "password": "alice-password-1"})
    assert response.status_code == 200
    assert "Too many failed attempts" in response.text

    # exponential backoff: more failures -> longer lockout
    conn = db_module.connect(Path(data_root) / "app.db")
    try:
        first = db_module.record_login_failure(conn, "user:alice")
        second = db_module.record_login_failure(conn, "user:alice")
        assert second > first
        # the route resets both the per-account and per-IP keys on success
        db_module.reset_login_failures(conn, "user:alice")
        db_module.reset_login_failures(conn, "ip:testclient")
        assert db_module.login_lockout_seconds(conn, "user:alice") == 0
        assert db_module.login_lockout_seconds(conn, "ip:testclient") == 0
    finally:
        conn.close()

    # after a reset the account logs in normally
    login(client, "alice", "alice-password-1")


def test_login_throttling_resets_on_success(env):
    """A successful login clears the failure counter (no lockout later)."""
    client, _, data_root = env
    make_user(data_root, "alice", "alice-password-1")
    for _ in range(db_module.LOGIN_MAX_FAILURES - 1):
        client.post("/login", data={"username": "alice", "password": "wrong"})
    login(client, "alice", "alice-password-1")
    client.post("/logout")
    # the counter was reset: the same number of failures stays below lockout
    for _ in range(db_module.LOGIN_MAX_FAILURES - 1):
        response = client.post("/login", data={"username": "alice", "password": "wrong"})
        assert "Too many failed attempts" not in response.text


def test_password_policy_min_length(env):
    """CS-2: every password-accepting handler enforces the minimum length."""
    client, _, data_root = env

    # first-run setup
    response = client.post(
        "/setup", data={"username": "owner", "password": "short", "confirm": "short"}
    )
    assert response.status_code == 200
    assert "at least 12 characters" in response.text
    assert db_module.users_empty(db_module.connect(Path(data_root) / "app.db"))

    make_user(data_root, "owner", "owner-password-1", admin=True)
    login(client, "owner", "owner-password-1")

    # admin creates a user
    response = client.post("/admin/users", data={"username": "newbie", "password": "short"})
    assert response.status_code == 400
    assert "at least 12 characters" in response.text
    conn = db_module.connect(Path(data_root) / "app.db")
    assert db_module.get_user_by_username(conn, "newbie") is None
    conn.close()

    # admin resets a password
    owner = db_module.get_user_by_username(db_module.connect(Path(data_root) / "app.db"), "owner")
    response = client.post(
        f"/admin/users/{owner['id']}/reset-password", data={"new_password": "short"}
    )
    assert response.status_code == 400


# --- config screens ------------------------------------------------------------


def test_config_provider_roundtrip_key_never_rendered(env):
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    response = client.post(
        "/config/provider/new",
        data={
            "label": "Main",
            "provider": "openai",
            "base_url": "https://example.test/v1",
            "api_key": "sk-secret-123",
            "max_iterations": "7",
            "temperature": "0.5",
            "max_tokens": "256",
        },
    )
    assert response.status_code == 303

    connections = read_connections(data_root, user["id"])
    assert len(connections) == 1
    connection = connections[0]
    assert connection["label"] == "Main"
    assert connection["provider"] == "openai"
    assert connection["is_default"] == 1  # first connection is auto-default
    assert connection["max_iterations"] == 7
    assert connection["temperature"] == 0.5
    assert connection["max_tokens"] == 256
    assert connection["api_key_enc"] != "sk-secret-123"
    assert security.decrypt_secret(connection["api_key_enc"], SECRET) == "sk-secret-123"
    # the connection save must not touch the model (owned by the librarian form)
    assert read_config(data_root, user["id"])["llm_model"] is None

    page = client.get("/config/provider").text
    assert "sk-secret-123" not in page

    # saving again with an empty key field keeps the stored key
    client.post(
        f"/config/provider/{connection['id']}",
        data={
            "label": "Main",
            "provider": "openai",
            "api_key": "",
            "max_iterations": "7",
        },
    )
    connection = read_connections(data_root, user["id"])[0]
    assert security.decrypt_secret(connection["api_key_enc"], SECRET) == "sk-secret-123"


def test_config_agents_librarian_model_roundtrip(env):
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    response = client.post(
        "/config/agents/librarian",
        data={"model": "gpt-x", "prompt_addendum": ""},
    )
    assert response.status_code == 303

    cfg = read_config(data_root, user["id"])
    assert cfg["llm_model"] == "gpt-x"

    page = client.get("/config/agents/librarian").text
    assert 'value="gpt-x"' in page
    # connection select: empty option ("Default") selected by default
    assert '<option value="" selected>Default</option>' in page


def test_config_llm_openrouter_roundtrip(env):
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    # openrouter with no base_url: provider persists, base_url stays empty
    response = client.post(
        "/config/provider/new",
        data={
            "label": "OR",
            "provider": "openrouter",
            "api_key": "or-secret",
            "max_iterations": "10",
        },
    )
    assert response.status_code == 303

    connection = read_connections(data_root, user["id"])[0]
    assert connection["provider"] == "openrouter"
    assert not connection["base_url"]

    page = client.get(f"/config/provider/{connection['id']}").text
    assert '<option value="openrouter" selected>' in page

    # the model lives on the librarian tab now
    client.post(
        "/config/agents/librarian",
        data={"model": "anthropic/claude-3.5-sonnet", "prompt_addendum": ""},
    )
    page = client.get("/config/agents/librarian").text
    assert 'value="anthropic/claude-3.5-sonnet"' in page

    # second pass with an explicit base_url override (edit keeps the key)
    client.post(
        f"/config/provider/{connection['id']}",
        data={
            "label": "OR",
            "provider": "openrouter",
            "base_url": "http://localhost:9000/v1",
            "api_key": "",
            "max_iterations": "10",
        },
    )
    connection = read_connections(data_root, user["id"])[0]
    assert connection["provider"] == "openrouter"
    assert connection["base_url"] == "http://localhost:9000/v1"
    assert security.decrypt_secret(connection["api_key_enc"], SECRET) == "or-secret"


def test_config_behavior_and_library_settings(env):
    client, backends, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    response = client.post(
        "/config/agents/librarian",
        data={"model": "", "prompt_addendum": "Custom prompt"},
    )
    assert response.status_code == 303
    cfg = read_config(data_root, user["id"])
    assert cfg["prompt_addendum"] == "Custom prompt"

    # retention knobs, git history, and library identity share one form
    client.post(
        "/config/library",
        data={
            "name": "My KB",
            "description": "desc",
            "git_enabled": "1",
            "git_remote_url": "  https://example.test/lib.git  ",
            "git_auto_push": "1",
            "trace_keep": "7",
            "activity_keep": "11",
            "payload_keep": "13",
        },
    )
    cfg = read_config(data_root, user["id"])
    assert cfg["git_enabled"] == 1
    assert cfg["git_remote_url"] == "https://example.test/lib.git"  # stripped
    assert cfg["git_auto_push"] == 1
    assert cfg["trace_keep"] == 7
    assert cfg["activity_keep"] == 11
    assert cfg["payload_keep"] == 13
    assert cfg["library_name"] == "My KB"
    assert cfg["library_description"] == "desc"

    # checkboxes absent -> off; blank URL -> NULL
    client.post(
        "/config/library",
        data={"git_remote_url": "", "trace_keep": "7", "activity_keep": "11"},
    )
    cfg = read_config(data_root, user["id"])
    assert cfg["git_enabled"] == 0
    assert cfg["git_auto_push"] == 0
    assert cfg["git_remote_url"] is None

    response = client.post("/config/library/reconcile")
    assert response.status_code == 303
    assert backends[user["id"]].reconciled

    response = client.post("/config/library/validate")
    assert response.status_code == 200
    assert "example warning" in response.text


def test_config_library_retention_edge_values(env):
    """CS-3: negative keeps are clamped to 0 (keep-all); huge keeps are stored."""
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    response = client.post(
        "/config/library",
        data={"trace_keep": "-100", "activity_keep": "-5"},
    )
    assert response.status_code == 303
    cfg = read_config(data_root, user["id"])
    assert cfg["trace_keep"] == 0
    assert cfg["activity_keep"] == 0

    response = client.post(
        "/config/library",
        data={"trace_keep": "99999", "activity_keep": "123456"},
    )
    assert response.status_code == 303
    cfg = read_config(data_root, user["id"])
    assert cfg["trace_keep"] == 99999
    assert cfg["activity_keep"] == 123456


def test_config_numeric_fields_invalid_input_400(env):
    """CS-4: non-numeric form input yields HTTP 400, not a 500."""
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    response = client.post(
        "/config/library",
        data={"trace_keep": "abc", "activity_keep": "1"},
    )
    assert response.status_code == 400

    for field, value in (
        ("max_iterations", "ten"),
        ("temperature", "hot"),
        ("max_tokens", "many"),
        ("temperature", "nan"),
        ("temperature", "inf"),
    ):
        response = client.post(
            "/config/provider/new",
            data={"label": "x", "provider": "openai", field: value},
        )
        assert response.status_code == 400, (field, response.status_code)


def test_config_agents_librarian_effective_prompt(env):
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    # no addendum: the built-in default is shown
    response = client.get("/config/agents/librarian")
    assert response.status_code == 200
    assert "Built-in default" in response.text
    assert "CREATE vs. ENRICH" in response.text

    # an addendum is appended to the always-present default
    client.post(
        "/config/agents/librarian",
        data={"model": "", "prompt_addendum": "My custom prompt"},
    )
    response = client.get("/config/agents/librarian")
    assert response.status_code == 200
    assert "With addendum" in response.text
    assert "My custom prompt" in response.text
    assert "CREATE vs. ENRICH" in response.text

    # empty addendum -> NULL -> default-only round trip
    client.post("/config/agents/librarian", data={"model": "", "prompt_addendum": ""})
    cfg = read_config(data_root, user["id"])
    assert cfg["prompt_addendum"] is None
    response = client.get("/config/agents/librarian")
    assert response.status_code == 200
    assert "Built-in default" in response.text
    assert "CREATE vs. ENRICH" in response.text


def test_config_agents_curator_roundtrip(env):
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    client.post(
        "/config/provider/new",
        data={"label": "Main", "provider": "anthropic", "api_key": "k"},
    )
    connection = read_connections(data_root, user["id"])[0]

    # defaults: no binding, "Default" selected
    page = client.get("/config/agents/curator").text
    assert 'id="connection_id"' in page
    assert '<option value="" selected>Default</option>' in page
    # connection options show the connection label
    assert "Main</option>" in page

    response = client.post(
        "/config/agents/curator",
        data={
            "connection_id": connection["id"],
            "curator_model": "claude-big",
            "curate_prompt_addendum": "never create concepts",
        },
    )
    assert response.status_code == 303
    cfg = read_config(data_root, user["id"])
    assert cfg["curator_connection_id"] == connection["id"]
    assert cfg["curator_model"] == "claude-big"
    assert cfg["curate_prompt_addendum"] == "never create concepts"
    # the separate curator form does not modify any provider_configs row
    after = read_connections(data_root, user["id"])
    assert len(after) == 1
    assert after[0]["provider"] == "anthropic"

    page = client.get("/config/agents/curator").text
    assert f'<option value="{connection["id"]}" selected>' in page
    assert 'value="claude-big"' in page

    # empty fields clear the binding back to "Default connection"
    client.post(
        "/config/agents/curator",
        data={"connection_id": "", "curator_model": "", "curate_prompt_addendum": ""},
    )
    cfg = read_config(data_root, user["id"])
    assert cfg["curator_connection_id"] is None
    assert cfg["curator_model"] is None
    assert cfg["curate_prompt_addendum"] is None


def test_config_agents_curator_schedule_roundtrip(env):
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    # new users default to enabled at 03:00 UTC
    page = client.get("/config/agents/curator").text
    assert 'name="curate_schedule_enabled"' in page
    assert "checked" in page
    assert 'value="03:00"' in page
    # schedule status line: enabled, stored UTC time, last run (never yet)
    assert "active, daily at 03:00 UTC" in page
    assert "last run: never" in page

    response = client.post(
        "/config/agents/curator/schedule",
        data={"curate_schedule_enabled": "1", "curate_schedule_time": "22:30"},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/config/agents/curator?saved=1"
    cfg = read_config(data_root, user["id"])
    assert cfg["curate_schedule_enabled"] == 1
    assert cfg["curate_schedule_time"] == "22:30"
    page = client.get("/config/agents/curator").text
    assert 'value="22:30"' in page

    # checkbox absent -> off; empty time -> default
    response = client.post("/config/agents/curator/schedule", data={"curate_schedule_time": ""})
    assert response.status_code == 303
    cfg = read_config(data_root, user["id"])
    assert cfg["curate_schedule_enabled"] == 0
    assert cfg["curate_schedule_time"] == "03:00"
    page = client.get("/config/agents/curator").text
    assert "inactive" in page
    assert "last run: never" in page


def test_config_agents_curator_schedule_invalid_time(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    response = client.post(
        "/config/agents/curator/schedule",
        data={"curate_schedule_enabled": "1", "curate_schedule_time": "25:99"},
    )
    assert response.status_code == 400


# --- agents: curator "Run now" ---------------------------------------------------


class FakeScheduler:
    """Stand-in for CurateScheduler: records start_run_now calls."""

    def __init__(self, *, busy=False):
        self._busy = busy
        self.started: list[str] = []

    def curator_busy(self, user_id: str) -> bool:
        return self._busy

    def start_run_now(self, user_id: str, *, token_label: str = "webui") -> None:
        self.started.append(user_id)


def test_curator_run_now_requires_login(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    response = client.post("/config/agents/curator/run")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_curator_run_now_csrf_rejected(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    response = client.post("/config/agents/curator/run", csrf=False)
    assert response.status_code == 403


def test_curator_run_now_starts_background_run(env):
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    fake = FakeScheduler()
    client.app.state.curate_scheduler = fake
    response = client.post("/config/agents/curator/run")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/config/agents/curator?msg=")
    assert fake.started == [user["id"]]


def test_curator_run_now_busy_guard(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    fake = FakeScheduler(busy=True)
    client.app.state.curate_scheduler = fake
    response = client.post("/config/agents/curator/run")
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert fake.started == []


def test_curator_run_now_button_present(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    page = client.get("/config/agents/curator").text
    assert 'action="/config/agents/curator/run"' in page
    assert "Run now" in page


# --- agents: embeddings tab ----------------------------------------------------


def test_config_agents_embeddings_tab_renders(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    client.post("/config/provider/new", data={"label": "Claude", "provider": "anthropic"})
    client.post("/config/provider/new", data={"label": "GPT", "provider": "openai"})

    page = client.get("/config/agents/embeddings").text
    assert 'href="/config/agents/embeddings"' in page  # third tab present
    # shortlist options render with dims in the labels
    for name, dims in LOCAL_MODEL_SHORTLIST:
        assert f"{name} ({dims} dims)" in page
    # anthropic connections are never offered for embeddings (options show labels)
    assert "Claude</option>" not in page
    assert "GPT</option>" in page
    # index status card renders with zero stored vectors
    assert "Stored vectors: 0" in page


def test_config_agents_embeddings_local_roundtrip(env):
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    model = LOCAL_MODEL_SHORTLIST[1][0]

    response = client.post(
        "/config/agents/embeddings", data={"source": "local", "local_model": model}
    )
    assert response.status_code == 303
    cfg = read_config(data_root, user["id"])
    assert cfg["embedding_source"] == "local"
    assert cfg["embedding_model"] == model
    assert cfg["embedding_connection_id"] is None

    page = client.get("/config/agents/embeddings").text
    assert '<option value="local" selected>' in page
    assert f'<option value="{model}" selected>' in page
    assert f"Configured model: {model} (local)" in page

    # empty source clears all three columns
    client.post("/config/agents/embeddings", data={"source": "", "local_model": model})
    cfg = read_config(data_root, user["id"])
    assert cfg["embedding_source"] is None
    assert cfg["embedding_model"] is None
    assert cfg["embedding_connection_id"] is None


def test_config_agents_embeddings_api_roundtrip(env):
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    client.post("/config/provider/new", data={"label": "GPT", "provider": "openai"})
    connection = read_connections(data_root, user["id"])[0]

    response = client.post(
        "/config/agents/embeddings",
        data={
            "source": "api",
            "api_model": "text-embedding-3-small",
            "connection_id": connection["id"],
        },
    )
    assert response.status_code == 303
    cfg = read_config(data_root, user["id"])
    assert cfg["embedding_source"] == "api"
    assert cfg["embedding_model"] == "text-embedding-3-small"
    assert cfg["embedding_connection_id"] == connection["id"]

    # empty connection follows the default connection
    response = client.post(
        "/config/agents/embeddings",
        data={"source": "api", "api_model": "text-embedding-3-small", "connection_id": ""},
    )
    assert response.status_code == 303
    cfg = read_config(data_root, user["id"])
    assert cfg["embedding_connection_id"] is None


def test_config_agents_embeddings_threshold_roundtrip(env):
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    model = LOCAL_MODEL_SHORTLIST[0][0]

    response = client.post(
        "/config/agents/embeddings",
        data={"source": "local", "local_model": model, "semantic_threshold": "0.82"},
    )
    assert response.status_code == 303
    assert read_config(data_root, user["id"])["semantic_threshold"] == 0.82
    page = client.get("/config/agents/embeddings").text
    assert 'value="0.82"' in page

    response = client.post(
        "/config/agents/embeddings",
        data={"source": "local", "local_model": model, "semantic_threshold": ""},
    )
    assert response.status_code == 303
    assert read_config(data_root, user["id"])["semantic_threshold"] is None


def test_config_agents_embeddings_hybrid_toggles_roundtrip(env):
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    model = LOCAL_MODEL_SHORTLIST[0][0]

    # defaults: hybrid on, rerank off (0.23.0); both boxes present
    page = client.get("/config/agents/embeddings").text
    assert 'name="hybrid_search"' in page and 'name="hybrid_rerank"' in page
    assert re.search(r'name="hybrid_search"\s+value="1"\s+checked', page)
    assert not re.search(r'name="hybrid_rerank"\s+value="1"\s+checked', page)

    # unchecked boxes submit nothing -> both off
    response = client.post(
        "/config/agents/embeddings", data={"source": "local", "local_model": model}
    )
    assert response.status_code == 303
    cfg = read_config(data_root, user["id"])
    assert cfg["hybrid_search"] == 0
    assert cfg["hybrid_rerank"] == 0

    # checked boxes submit "1" -> both on, reflected in the form
    response = client.post(
        "/config/agents/embeddings",
        data={
            "source": "local",
            "local_model": model,
            "hybrid_search": "1",
            "hybrid_rerank": "1",
        },
    )
    assert response.status_code == 303
    cfg = read_config(data_root, user["id"])
    assert cfg["hybrid_search"] == 1
    assert cfg["hybrid_rerank"] == 1
    page = client.get("/config/agents/embeddings").text
    assert re.search(r'name="hybrid_search"\s+value="1"\s+checked', page)
    assert re.search(r'name="hybrid_rerank"\s+value="1"\s+checked', page)


def test_config_agents_embeddings_threshold_validation(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    base = {"source": "local", "local_model": LOCAL_MODEL_SHORTLIST[0][0]}

    for bad in ("abc", "1.5", "-0.1", "nan"):
        response = client.post(
            "/config/agents/embeddings", data={**base, "semantic_threshold": bad}
        )
        assert response.status_code == 400
    for good in ("0.0", "1.0"):
        response = client.post(
            "/config/agents/embeddings", data={**base, "semantic_threshold": good}
        )
        assert response.status_code == 303


def test_config_agents_embeddings_invalid_combos_rejected(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    # unknown source
    response = client.post("/config/agents/embeddings", data={"source": "magic"})
    assert response.status_code == 400
    # local with a model outside the shortlist
    response = client.post(
        "/config/agents/embeddings", data={"source": "local", "local_model": "made-up/model"}
    )
    assert response.status_code == 400
    # api without a model
    response = client.post("/config/agents/embeddings", data={"source": "api", "api_model": ""})
    assert response.status_code == 400


def test_config_agents_embeddings_anthropic_connection_rejected(env):
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    client.post("/config/provider/new", data={"label": "Claude", "provider": "anthropic"})
    connection = read_connections(data_root, user["id"])[0]

    # explicit anthropic connection: rejected defensively
    response = client.post(
        "/config/agents/embeddings",
        data={"source": "api", "api_model": "m", "connection_id": connection["id"]},
    )
    assert response.status_code == 400
    # anthropic as the (implicit) default connection: also rejected
    response = client.post(
        "/config/agents/embeddings",
        data={"source": "api", "api_model": "m", "connection_id": ""},
    )
    assert response.status_code == 400
    cfg = read_config(data_root, user["id"])
    assert cfg["embedding_source"] is None


def test_config_agents_embeddings_test_button(env, monkeypatch):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    # nothing saved: clear message, no provider call
    response = client.post("/config/agents/embeddings/test")
    assert response.status_code == 200
    assert "Save an embedding source and model first" in response.text

    client.post(
        "/config/agents/embeddings",
        data={"source": "local", "local_model": LOCAL_MODEL_SHORTLIST[0][0]},
    )

    class FakeEmbedProvider:
        async def embed(self, texts, config, *, kind="document"):
            return [[0.1, 0.2, 0.3] for _ in texts]

    monkeypatch.setattr(
        deps, "create_embedding_provider", lambda config, *, cache_dir=None: FakeEmbedProvider()
    )
    response = client.post("/config/agents/embeddings/test")
    assert response.status_code == 200
    assert "Embedding OK (3 dims" in response.text

    class DownEmbedProvider:
        async def embed(self, texts, config, *, kind="document"):
            raise RuntimeError("embed down")

    monkeypatch.setattr(
        deps, "create_embedding_provider", lambda config, *, cache_dir=None: DownEmbedProvider()
    )
    response = client.post("/config/agents/embeddings/test")
    assert response.status_code == 200
    assert "Embedding failed: embed down" in response.text


def test_config_agents_embeddings_status_card_renders_run(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    manager = client.app.state.librarian_manager
    manager.embed_status_for = lambda user_id: {
        "state": "running",
        "done": 3,
        "total": 10,
        "model": "m",
        "error": None,
    }

    page = client.get("/config/agents/embeddings").text
    assert "State: running" in page
    assert "3/10" in page
    assert "Run model: m" in page


def test_config_save_evicts_cached_librarian(env):
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    manager = client.app.state.librarian_manager

    response = client.get("/config/provider")
    assert response.status_code == 200
    assert manager.evicted == []  # GETs do not evict

    client.post("/config/provider/new", data={"label": "Main", "provider": "openai"})
    assert manager.evicted == [user["id"]]

    client.post("/config/agents/librarian", data={"model": "gpt-x"})
    client.post("/config/library", data={"name": "KB"})
    client.post("/config/agents/curator", data={"curator_model": "big"})
    client.post("/config/agents/curator/schedule", data={"curate_schedule_enabled": "1"})
    client.post("/config/agents/embeddings", data={"source": "", "local_model": ""})
    assert manager.evicted == [user["id"]] * 6


def test_llm_test_connection(env, monkeypatch):
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    # a connection exists but no model yet: clear message, no provider call
    client.post(
        "/config/provider/new",
        data={"label": "Main", "provider": "openai", "api_key": "sk-secret-123"},
    )
    connection = read_connections(data_root, user["id"])[0]
    response = client.post(f"/config/provider/{connection['id']}/test", data={"api_key": ""})
    assert response.status_code == 200
    assert "Save a provider and model first" in response.text

    # provider on the Provider page, model on the Librarian tab (cross-page)
    client.post("/config/agents/librarian", data={"model": "gpt-x"})

    class FakeProvider:
        async def complete(self, messages, tools, config):
            return object()

    monkeypatch.setattr(deps, "build_llm_config", lambda conn_row, key, *, model: object())
    monkeypatch.setattr(deps, "create_llm_provider", lambda config: FakeProvider())

    response = client.post(f"/config/provider/{connection['id']}/test", data={"api_key": ""})
    assert response.status_code == 200
    assert "Connection OK" in response.text


def test_provider_delete_blocked_while_bound_shows_error(env):
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    client.post("/config/provider/new", data={"label": "A", "provider": "openai"})
    client.post("/config/provider/new", data={"label": "B", "provider": "gemini"})
    connections = {c["label"]: c for c in read_connections(data_root, user["id"])}
    bound = connections["B"]

    # bind the librarian to connection B, then try to delete it
    client.post(
        "/config/agents/librarian",
        data={"connection_id": bound["id"], "model": "m"},
    )
    response = client.post(f"/config/provider/{bound['id']}/delete")
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert len(read_connections(data_root, user["id"])) == 2  # not deleted

    # the follow-up GET renders the danger flash
    page = client.get(response.headers["location"]).text
    assert 'data-type="danger"' in page
    assert "rebind the agent first" in page

    # deleting the default while others exist is blocked too
    default = connections["A"]
    response = client.post(f"/config/provider/{default['id']}/delete")
    assert response.status_code == 303
    page = client.get(response.headers["location"]).text
    assert "Set another connection as default first" in page


def test_provider_set_default_roundtrip(env):
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    client.post("/config/provider/new", data={"label": "A", "provider": "openai"})
    client.post("/config/provider/new", data={"label": "B", "provider": "gemini"})
    connections = {c["label"]: c for c in read_connections(data_root, user["id"])}
    assert connections["A"]["is_default"] == 1

    response = client.post(f"/config/provider/{connections['B']['id']}/default")
    assert response.status_code == 303

    connections = {c["label"]: c for c in read_connections(data_root, user["id"])}
    assert connections["A"]["is_default"] == 0
    assert connections["B"]["is_default"] == 1

    page = client.get("/config/provider").text
    assert '<span class="badge badge-info">Default</span>' in page
    # only one Default badge rendered (for B)
    assert page.count('<span class="badge badge-info">Default</span>') == 1


def test_config_old_routes_redirect(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    response = client.get("/config/llm")
    assert response.status_code == 303
    assert response.headers["location"] == "/config/provider"

    response = client.get("/config/behavior")
    assert response.status_code == 303
    assert response.headers["location"] == "/config/agents/librarian"

    response = client.get("/config/prompt")
    assert response.status_code == 303
    assert response.headers["location"] == "/config/agents/librarian"


def test_config_agents_curator_effective_prompt_display(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    # no addendum: built-in template with visible placeholders, no owner label
    response = client.get("/config/agents/curator")
    assert response.status_code == 200
    assert "CURATION TASK" in response.text
    assert "{instructions}" in response.text
    assert "Built-in template" in response.text
    assert "Standing curation rules from the library owner:" not in response.text

    # an addendum (even one containing braces) renders verbatim, no crash
    client.post(
        "/config/agents/curator",
        data={
            "curate_provider": "",
            "curate_model": "",
            "curate_prompt_addendum": "always prefer merge {braces}",
        },
    )
    response = client.get("/config/agents/curator")
    assert response.status_code == 200
    assert "With addendum" in response.text
    assert "Standing curation rules from the library owner:" in response.text
    assert "always prefer merge {braces}" in response.text


# --- tokens --------------------------------------------------------------------


def _extract_token(html: str) -> str:
    match = re.search(r'class="token-value">([^<]+)<', html)
    assert match, "plaintext token not rendered at creation"
    return match.group(1).strip()


def test_tokens_create_revoke(env):
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    response = client.post("/tokens", data={"label": "agent-1"})
    assert response.status_code == 200
    plaintext = _extract_token(response.text)

    conn = db_module.connect(Path(data_root) / "app.db")
    try:
        tokens = db_module.list_tokens(conn, user["id"])
        assert len(tokens) == 1
        token = db_module.get_token(conn, tokens[0]["id"])
        # only the hash is persisted; the plaintext is shown once
        assert (
            security.hash_token(plaintext)
            == db_module.lookup_token(conn, security.hash_token(plaintext))["token_hash"]
        )
        assert token["token_hash"] != plaintext
        assert token["label"] == "agent-1"

        # the plaintext is not rendered again on the plain list view
        assert plaintext not in client.get("/tokens").text

        response = client.post(f"/tokens/{token['id']}/revoke")
        assert response.status_code == 303
        assert db_module.get_token(conn, token["id"])["revoked_at"] is not None
    finally:
        conn.close()


# --- library browsing ----------------------------------------------------------


def test_tree_document_log_pages(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    response = client.get("/library/tree")
    assert response.status_code == 200
    assert "concepts/" in response.text
    assert "/library/graph?folder=" in response.text  # tree -> graph jump (0.10.2)
    # nothing selected: the center pane shows the empty state
    assert "Choose a note from the tree." in response.text
    # tree file links target the document view and carry the JS selection hook
    response = client.get("/library/tree/children", params={"path": "/concepts"})
    assert response.status_code == 200
    assert "alpha.md" in response.text
    assert 'data-doc-path="/concepts/alpha.md"' in response.text
    assert "/library/graph?focus=" in response.text

    response = client.get("/library/tree", params={"path": "/concepts/alpha.md"})
    assert response.status_code == 200
    assert "Alpha" in response.text
    assert "Concept" in response.text
    assert "human-reviewed" in response.text
    # per-document history empty state (the fake backend has no commits)
    assert "No history yet — the first write touching this document" in response.text

    response = client.get("/library/tree", params={"path": "/concepts/beta.md"})
    assert "stale" in response.text

    # the legacy document URL 301s into the new view
    response = client.get("/library/document", params={"path": "/concepts/alpha.md"})
    assert response.status_code == 301
    assert response.headers["location"] == "/library/tree?path=%2Fconcepts%2Falpha.md"

    assert client.get("/library/log").status_code == 200
    response = client.get("/library/log/content")
    assert response.status_code == 200
    assert "Initialization" in response.text


# --- document view: per-document timeline, inline diff, restore -----------------

HISTORY_COMMITS = [
    {
        "sha": "c" * 40,
        "short": "ccccccc",
        "timestamp": "2026-08-03T10:00:00+00:00",
        "subject": "Update: Edited [Alpha](/concepts/alpha.md).",
        "is_root": False,
        "path": "concepts/alpha.md",
    },
    {
        "sha": "b" * 40,
        "short": "bbbbbbb",
        "timestamp": "2026-08-02T10:00:00+00:00",
        "subject": "Creation: Created [Alpha](/concepts/alpha.md).",
        "is_root": False,
        "path": "concepts/alpha.md",
    },
    {
        "sha": "a" * 40,
        "short": "aaaaaaa",
        "timestamp": "2026-08-01T10:00:00+00:00",
        "subject": "Initialization: Initialized the library bundle.",
        "is_root": True,
        "path": "concepts/alpha.md",
    },
]


def test_document_page_history_card(env):
    client, backends, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    backends[user["id"]] = FakeBackend(make_docs("alice"), commits=HISTORY_COMMITS)

    response = client.get("/library/tree", params={"path": "/concepts/alpha.md"})
    assert response.status_code == 200
    assert 'id="history-slider"' in response.text
    assert 'max="2"' in response.text  # 3 file commits -> slider 0..2
    # one visible snap-point dot per timeline stop; the live stop is active
    assert response.text.count('data-index="') == 3
    assert 'class="history-tick active" data-index="2"' in response.text
    assert '<a href="/concepts/beta.md">Beta</a>' in response.text  # live body, server-rendered
    # live view: no banner; the restore form renders but stays hidden for JS
    assert "not the current version" not in response.text
    assert 'id="restore-form"' in response.text
    assert "data-loading hidden>" in response.text


def test_document_page_historical_view(env):
    client, backends, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    backends[user["id"]] = FakeBackend(
        make_docs("alice"), commits=HISTORY_COMMITS, diff=INLINE_DIFF_PATCH
    )

    sha = HISTORY_COMMITS[1]["sha"]  # an older commit, not HEAD
    response = client.get("/library/tree", params={"path": "/concepts/alpha.md", "sha": sha})
    assert response.status_code == 200
    # banner with the viewed commit
    assert "not the current version" in response.text
    assert "2026-08-02 10:00 UTC" in response.text
    assert "Back to current" in response.text
    # the viewed stop's snap point is highlighted
    assert 'class="history-tick active" data-index="1"' in response.text
    # the inline vs-HEAD diff replaces the document body in place — no patch
    # transcript, and the historical body itself is not rendered
    assert "historical body at bbbbbbb" not in response.text
    assert '<div class="diff-del-block"><p>old body</p>\n</div>' in response.text
    assert '<div class="diff-add-block"><p>new body</p>\n</div>' in response.text
    assert "diff-view" not in response.text
    assert response.text.index("diff-inline") > response.text.index('id="md-rendered"')
    # the restore form posts path + sha to the restore route
    assert "/library/document/restore" in response.text
    assert "Restore this version" in response.text
    assert f'value="{sha}"' in response.text


def test_document_page_sha_equal_head_is_live(env):
    client, backends, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    backends[user["id"]] = FakeBackend(make_docs("alice"), commits=HISTORY_COMMITS)

    response = client.get(
        "/library/tree",
        params={"path": "/concepts/alpha.md", "sha": HISTORY_COMMITS[0]["sha"]},
    )
    assert response.status_code == 200
    assert '<a href="/concepts/beta.md">Beta</a>' in response.text  # live body, server-rendered
    assert "not the current version" not in response.text
    assert "data-loading hidden>" in response.text  # restore form hidden in live view


def test_document_page_unknown_sha_404(env):
    client, backends, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    backends[user["id"]] = FakeBackend(make_docs("alice"), commits=HISTORY_COMMITS)

    response = client.get("/library/tree", params={"path": "/concepts/alpha.md", "sha": "f" * 40})
    assert response.status_code == 404


def test_document_page_history_unavailable_hint(env):
    client, backends, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    backends[user["id"]] = FakeBackend(make_docs("alice"), configured=False, available=False)
    response = client.get("/library/tree", params={"path": "/concepts/alpha.md"})
    assert response.status_code == 200
    assert "Git history is unavailable." in response.text
    assert "Library settings" in response.text

    backends[user["id"]] = FakeBackend(make_docs("alice"), configured=True, available=False)
    response = client.get("/library/tree", params={"path": "/concepts/alpha.md"})
    assert response.status_code == 200
    assert "git binary not found on this server." in response.text


def test_document_restore_route(env):
    client, backends, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    backend = FakeBackend(make_docs("alice"), commits=HISTORY_COMMITS)
    backends[user["id"]] = backend
    manager = client.app.state.librarian_manager

    sha = HISTORY_COMMITS[1]["sha"]
    response = client.post(
        "/library/document/restore", data={"path": "/concepts/alpha.md", "sha": sha}
    )
    assert response.status_code == 303
    assert response.headers["location"] == (
        "/library/tree?path=%2Fconcepts%2Falpha.md&msg=Restored+to+commit+bbbbbbb."
    )
    assert backend.restored == [("/concepts/alpha.md", sha)]
    # the cached librarian is evicted so embeddings/FTS reconcile on next use
    assert manager.evicted == [user["id"]]


def test_document_restore_route_csrf_and_login(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    sha = HISTORY_COMMITS[1]["sha"]
    response = client.post(
        "/library/document/restore",
        data={"path": "/concepts/alpha.md", "sha": sha},
        csrf=False,
    )
    assert response.status_code == 403

    client.post("/logout")
    response = client.post(
        "/library/document/restore", data={"path": "/concepts/alpha.md", "sha": sha}
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


INLINE_DIFF_PATCH = (
    "diff --git a/concepts/alpha.md b/concepts/alpha.md\n"
    "index 111..222 333\n"
    "--- a/concepts/alpha.md\n"
    "+++ b/concepts/alpha.md\n"
    "@@ -1,1 +1,1 @@\n"
    "-old body\n"
    "+new body\n"
)


def test_document_diff_endpoint_inline_mode(env):
    client, backends, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    fake = FakeBackend(make_docs("alice"), commits=HISTORY_COMMITS, diff=INLINE_DIFF_PATCH)
    backends[user["id"]] = fake
    sha = HISTORY_COMMITS[1]["sha"]

    # inline mode: in-flow document diff, no hunk chrome; requested with a
    # whole-file context window so the accumulated vs-HEAD diff is complete
    response = client.get(
        "/library/document/diff",
        params={"path": "/concepts/alpha.md", "sha": sha, "mode": "inline"},
    )
    assert response.status_code == 200
    assert fake.diff_context is not None and fake.diff_context >= 1_000_000
    diff_html = response.json()["diff_html"]
    assert '<div class="diff-del-block"><p>old body</p>\n</div>' in diff_html
    assert '<div class="diff-add-block"><p>new body</p>\n</div>' in diff_html
    assert "@@" not in diff_html
    assert "diff --git" not in diff_html
    assert "diff-view" not in diff_html

    # default mode keeps the per-line patch transcript unchanged (default
    # 3-line hunk context, no whole-file expansion)
    response = client.get(
        "/library/document/diff", params={"path": "/concepts/alpha.md", "sha": sha}
    )
    assert response.status_code == 200
    assert fake.diff_context is None
    diff_html = response.json()["diff_html"]
    assert '<span class="diff-del">-old body</span>' in diff_html
    assert '<span class="diff-add">+new body</span>' in diff_html

    # HEAD diffs to nothing in inline mode too
    response = client.get(
        "/library/document/diff",
        params={"path": "/concepts/alpha.md", "sha": HISTORY_COMMITS[0]["sha"], "mode": "inline"},
    )
    assert response.status_code == 200
    assert response.json()["diff_html"] == ""

    # unknown mode is rejected
    response = client.get(
        "/library/document/diff",
        params={"path": "/concepts/alpha.md", "sha": sha, "mode": "bogus"},
    )
    assert response.status_code == 400


def test_document_edit_route(env):
    client, backends, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    backend = FakeBackend(make_docs("alice"), commits=HISTORY_COMMITS)
    backends[user["id"]] = backend
    manager = client.app.state.librarian_manager

    response = client.post(
        "/library/document/edit",
        data={"path": "/concepts/alpha.md", "body": "Rewritten body.\n"},
    )
    assert response.status_code == 303
    assert response.headers["location"] == ("/library/tree?path=%2Fconcepts%2Falpha.md&msg=Saved.")
    assert backend.edited == [("/concepts/alpha.md", "Rewritten body.\n")]
    assert backend.docs["/concepts/alpha.md"]["body"] == "Rewritten body.\n"
    assert manager.evicted == [user["id"]]


def test_document_edit_route_csrf_and_login(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    response = client.post(
        "/library/document/edit",
        data={"path": "/concepts/alpha.md", "body": "x\n"},
        csrf=False,
    )
    assert response.status_code == 403

    client.post("/logout")
    response = client.post(
        "/library/document/edit", data={"path": "/concepts/alpha.md", "body": "x\n"}
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_document_edit_route_unknown_document_flashes_error(env):
    # cross-user analog: a path outside the user's own bundle surfaces as a
    # backend FileNotFoundError and must not succeed
    client, backends, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    backend = FakeBackend(make_docs("alice"), commits=HISTORY_COMMITS)
    backends[user["id"]] = backend

    response = client.post("/library/document/edit", data={"path": "/user-bob.md", "body": "x\n"})
    assert response.status_code == 303
    assert response.headers["location"] == (
        "/library/tree?path=%2Fuser-bob.md&error=%2Fuser-bob.md"
    )
    assert backend.edited == []


def test_document_delete_route(env):
    client, backends, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    backend = FakeBackend(make_docs("alice"), commits=HISTORY_COMMITS)
    backends[user["id"]] = backend
    manager = client.app.state.librarian_manager

    response = client.post("/library/document/delete", data={"path": "/concepts/beta.md"})
    assert response.status_code == 303
    # no path on the redirect: the document is gone
    assert response.headers["location"] == "/library/tree?msg=Deleted+beta.md."
    assert backend.deleted == ["/concepts/beta.md"]
    assert "/concepts/beta.md" not in backend.docs
    assert manager.evicted == [user["id"]]


def test_document_delete_route_csrf_and_login(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    response = client.post(
        "/library/document/delete", data={"path": "/concepts/beta.md"}, csrf=False
    )
    assert response.status_code == 403

    client.post("/logout")
    response = client.post("/library/document/delete", data={"path": "/concepts/beta.md"})
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_document_delete_route_unknown_document_flashes_error(env):
    client, backends, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    backend = FakeBackend(make_docs("alice"), commits=HISTORY_COMMITS)
    backends[user["id"]] = backend

    response = client.post("/library/document/delete", data={"path": "/user-bob.md"})
    assert response.status_code == 303
    assert response.headers["location"] == "/library/tree?error=%2Fuser-bob.md"
    assert backend.deleted == []


def test_graph_pages_script_stack(env):
    client, backends, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    backends[user["id"]] = FakeBackend({})
    make_trace(data_root, user["id"], trace_id="t1")

    graph_page = client.get("/library/graph")
    assert graph_page.status_code == 200
    # sunburst-only graph page (SUNBURST-ONLY rework): pure 2D canvas, no
    # vendored 3D stack (removed entirely with the trace replay rebuild)
    assert "/static/graph_sunburst.js" in graph_page.text
    assert "/static/minimap.js" in graph_page.text
    assert "/static/vendor/graph3d-vendor.min.js" not in graph_page.text
    assert "/static/graph3d.js" not in graph_page.text
    assert "/static/graph_particles.js" not in graph_page.text
    assert "/static/graph_viewstate.js" not in graph_page.text
    assert "vis-network" not in graph_page.text
    assert "cdn.jsdelivr.net/npm/vis-network" not in graph_page.text
    # link_density is fixed (no metric select), the nebula/sunburst mode
    # toggle is gone; Fit view (zoom reset) remains
    assert 'id="graph-metric"' not in graph_page.text
    assert 'id="graph-mode-nebula"' not in graph_page.text
    assert 'id="graph-mode-sunburst"' not in graph_page.text
    assert 'id="graph-fit"' in graph_page.text
    assert 'id="graph-search"' not in graph_page.text
    for toggle in ("stars", "planets", "moons", "galaxies", "systems"):
        assert f'id="graph-show-{toggle}"' not in graph_page.text
    assert "graph-type-filters" not in graph_page.text
    assert 'id="graph-zoom"' not in graph_page.text  # zoom select removed (0.10.2)

    # trace replay renders through the same sunburst module (no 3D stack)
    trace_page = client.get("/library/traces/t1")
    assert trace_page.status_code == 200
    assert "/static/graph_sunburst.js" in trace_page.text
    assert "/static/trace_replay.js" in trace_page.text
    assert "/static/vendor/graph3d-vendor.min.js" not in trace_page.text
    assert "/static/graph3d.js" not in trace_page.text
    assert "vis-network" not in trace_page.text

    # the document view mounts a second sunburst as its minimap (no
    # minimap.js — that satellite stays on the graph page)
    doc_view = client.get("/library/tree")
    assert doc_view.status_code == 200
    assert "/static/graph_sunburst.js" in doc_view.text
    assert "/static/document_view.js" in doc_view.text
    assert "/static/minimap.js" not in doc_view.text


def test_graph_universe_deep_hierarchy(env):
    """Deep docs map to their top-level cluster and full parent folder path."""
    client, backends, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    backends[user["id"]] = FakeBackend(
        {
            "/concepts/alpha.md": {
                "frontmatter": {"title": "Alpha", "type": "Concept"},
                "body": "See [Deep](/a/b/c/deep.md).\n",
            },
            "/a/b/c/deep.md": {
                "frontmatter": {"title": "Deep", "type": "Concept"},
                "body": "Deep body.\n",
            },
            "/a/b/c/d/deeper.md": {
                "frontmatter": {"title": "Deeper", "type": "Concept"},
                "body": "body\n",
            },
        }
    )

    response = client.get("/api/graph/universe")
    assert response.status_code == 200
    data = response.json()

    nodes = {node["id"]: node for node in data["nodes"]}
    deep = nodes["/a/b/c/deep"]
    assert deep["cluster"] == "a"
    assert deep["parent_folder"] == "/a/b/c"

    deeper = nodes["/a/b/c/d/deeper"]
    assert deeper["cluster"] == "a"
    assert deeper["parent_folder"] == "/a/b/c/d"

    edges = {(e["source"], e["target"]) for e in data["edges"]}
    assert ("/concepts/alpha", "/a/b/c/deep") in edges


def test_graph_walk_depth_bounded(env, monkeypatch):
    """CS-17: a pathologically deep tree gets a clear 400, not RecursionError -> 500."""
    from athenaeum.webui import graph

    client, backends, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    monkeypatch.setattr(graph, "MAX_WALK_DEPTH", 3)
    backends[user["id"]] = FakeBackend(
        {"/a/b/c/d/deep.md": {"frontmatter": {"title": "Deep"}, "body": "body\n"}}
    )
    response = client.get("/api/graph/universe")
    assert response.status_code == 400
    assert "depth" in response.json()["detail"].lower()

    # a tree exactly at the limit still walks fine
    backends[user["id"]] = FakeBackend(
        {"/a/b/c/deep.md": {"frontmatter": {"title": "Deep"}, "body": "body\n"}}
    )
    assert client.get("/api/graph/universe").status_code == 200


def _universe_docs():
    return {
        "/atlas/one.md": {
            "frontmatter": {
                "title": "One",
                "type": "Concept",
                "generated": {"at": "2026-07-01T00:00:00+00:00"},
            },
            "body": "See [Two](/atlas/two.md).\n",
        },
        "/atlas/two.md": {
            "frontmatter": {
                "title": "Two",
                "type": "Concept",
                "generated": {"at": "2026-07-30T10:12:00+00:00"},
            },
            "body": "Two body.\n",
        },
        "/helix/three.md": {
            "frontmatter": {"title": "Three", "type": "Concept"},  # no generated -> oldest
            "body": "Three body.\n",
        },
        "/root-doc.md": {
            "frontmatter": {"title": "Root Doc", "type": "Note", "generated": {"at": "2026-07-15"}},
            "body": "Root body.\n",
        },
    }


def test_graph_universe_endpoint(env):
    client, backends, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    backends[user["id"]] = FakeBackend(_universe_docs())

    for metric in ("recency", "link_density"):
        response = client.get("/api/graph/universe", params={"metric": metric})
        assert response.status_code == 200
        data = response.json()
        assert data["metric"] == metric
        counts = {c["id"]: c["count"] for c in data["clusters"]}
        assert counts == {"atlas": 2, "helix": 1, "root": 1}
        for node in data["nodes"]:
            for key in (
                "id",
                "label",
                "cluster",
                "parent_folder",
                "radius",
                "size",
                "metric_value",
                "trust_tier",
                "stale",
            ):
                assert key in node
            assert 0.0 <= node["radius"] <= 1.0
            assert node["size"] > 0
        nodes = {n["id"]: n for n in data["nodes"]}
        assert nodes["/atlas/one"]["cluster"] == "atlas"  # first path segment
        assert nodes["/atlas/one"]["parent_folder"] == "/atlas"
        assert nodes["/root-doc"]["cluster"] == "root"
        assert nodes["/root-doc"]["parent_folder"] == "/"

    # recency: newest doc at radius 1, missing timestamp treated as oldest
    data = client.get("/api/graph/universe", params={"metric": "recency"}).json()
    nodes = {n["id"]: n for n in data["nodes"]}
    assert nodes["/atlas/two"]["radius"] == 1.0
    assert nodes["/atlas/two"]["metric_value"] == "2026-07-30T10:12:00+00:00"
    assert nodes["/helix/three"]["radius"] == 0.0
    assert nodes["/helix/three"]["metric_value"] is None

    # link_density: the linked pair scores highest, unlinked docs lowest
    data = client.get("/api/graph/universe", params={"metric": "link_density"}).json()
    nodes = {n["id"]: n for n in data["nodes"]}
    assert nodes["/atlas/one"]["metric_value"] == 1  # out-degree
    assert nodes["/atlas/two"]["metric_value"] == 1  # in-degree
    assert nodes["/helix/three"]["metric_value"] == 0
    assert nodes["/atlas/one"]["radius"] == 1.0
    assert nodes["/helix/three"]["radius"] == 0.0

    # edges: document-to-document links, every endpoint a node id,
    # deterministic order
    node_ids = set(nodes)
    assert data["edges"] == [{"source": "/atlas/one", "target": "/atlas/two"}]
    assert all(e["source"] in node_ids and e["target"] in node_ids for e in data["edges"])


def test_graph_universe_link_density_sqrt_scale(env):
    """sqrt scaling before normalization spreads the skewed degree distribution."""
    client, backends, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    # hub links to four targets (degree 4), each target has in-degree 1,
    # the isolate has degree 0 -> sqrt values 2 / 1 / 0 -> radii 1 / 0.5 / 0
    # (raw min-max would put the targets at 0.25).
    docs = {
        "/atlas/hub.md": {
            "frontmatter": {"title": "Hub"},
            "body": "".join(f"[T{i}](/atlas/t{i}.md)\n" for i in range(4)),
        },
        "/atlas/isolate.md": {"frontmatter": {"title": "Isolate"}, "body": "No links.\n"},
    }
    for i in range(4):
        docs[f"/atlas/t{i}.md"] = {"frontmatter": {"title": f"T{i}"}, "body": f"Target {i}.\n"}
    backends[user["id"]] = FakeBackend(docs)

    data = client.get("/api/graph/universe", params={"metric": "link_density"}).json()
    nodes = {n["id"]: n for n in data["nodes"]}
    assert nodes["/atlas/hub"]["metric_value"] == 4
    assert nodes["/atlas/hub"]["radius"] == 1.0
    for i in range(4):
        assert nodes[f"/atlas/t{i}"]["metric_value"] == 1
        assert nodes[f"/atlas/t{i}"]["radius"] == 0.5  # sqrt(1)/sqrt(4), not 1/4
    assert nodes["/atlas/isolate"]["radius"] == 0.0
    edges = {(e["source"], e["target"]) for e in data["edges"]}
    assert edges == {("/atlas/hub", f"/atlas/t{i}") for i in range(4)}


def test_graph_universe_metric_param(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    # default metric is link_density (the graph page always uses it)
    response = client.get("/api/graph/universe")
    assert response.status_code == 200
    assert response.json()["metric"] == "link_density"

    # explicit recency echoes in the payload
    response = client.get("/api/graph/universe", params={"metric": "recency"})
    assert response.status_code == 200
    assert response.json()["metric"] == "recency"

    # unknown metric is a clear 400
    response = client.get("/api/graph/universe", params={"metric": "bogus"})
    assert response.status_code == 400
    assert "bogus" in response.json()["detail"]


def test_graph_universe_requires_login(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")

    response = client.get("/api/graph/universe")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# --- traces / activity (phase 4) -----------------------------------------------


def make_trace(data_root, user_id, trace_id="20260728T180036Z-a1b2c3d4", **overrides):
    """Persist one trace file under the user's own library root (.traces/)."""
    trace = {
        "trace_id": trace_id,
        "tool": "request_knowledge",
        "agent_label": "agent-a",
        "started_at": "2026-07-28T18:00:36+00:00",
        "ended_at": "2026-07-28T18:00:38+00:00",
        "duration_ms": 2000.0,
        "outcome": "ok",
        "error": None,
        "llm": {
            "provider": "openai",
            "model": "m",
            "iterations": 1,
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
        "events": [
            {
                "seq": 1,
                "ts": "2026-07-28T18:00:37+00:00",
                "tool": "read_document",
                "args": {"path": "/concepts/alpha.md"},
                "duration_ms": 0.5,
                "result": {"path": "/concepts/alpha.md", "title": "Alpha", "type": "Concept"},
                "error": None,
            },
            {
                "seq": 2,
                "ts": "2026-07-28T18:00:37+00:00",
                "tool": "list_dir",
                "args": {"path": "/concepts"},
                "duration_ms": 0.3,
                "result": {"path": "/concepts", "entries": ["alpha.md"], "count": 1},
                "error": None,
            },
        ],
    }
    trace.update(overrides)
    store = Path(data_root) / "users" / user_id / "library" / ".traces"
    store.mkdir(parents=True, exist_ok=True)
    (store / f"{trace_id}.json").write_text(json.dumps(trace), encoding="utf-8")
    return trace


def test_trace_replay_page_and_api(env):
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    make_trace(data_root, user["id"])
    login(client, "alice", "pw")

    # the trace list page and its rows endpoint were removed; replay lives on
    assert client.get("/library/traces").status_code == 404
    assert client.get("/library/traces/rows").status_code == 404

    response = client.get("/library/traces/20260728T180036Z-a1b2c3d4")
    assert response.status_code == 200
    assert "Trace replay" in response.text
    assert "read_document" in response.text  # timeline events
    assert "list_dir" in response.text
    assert "openai / m" in response.text  # llm badge
    assert "15 tokens" in response.text

    response = client.get("/api/traces/20260728T180036Z-a1b2c3d4")
    assert response.status_code == 200
    data = response.json()
    assert data["trace_id"] == "20260728T180036Z-a1b2c3d4"
    assert data["tool"] == "request_knowledge"
    assert data["outcome"] == "ok"
    assert data["llm"]["total_tokens"] == 15
    assert [event["tool"] for event in data["events"]] == ["read_document", "list_dir"]


def test_trace_replay_page_shows_llm_timing(env):
    """New-shape traces render per-step LLM time and the total badge; the
    old-shape test above (no llm_ms fields) is the backward-compat pin."""
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    trace = make_trace(data_root, user["id"])
    trace["llm"]["llm_ms_total"] = 6300.0
    trace["events"][0]["llm_ms"] = 6123.4
    store = Path(data_root) / "users" / user["id"] / "library" / ".traces"
    (store / f"{trace['trace_id']}.json").write_text(json.dumps(trace), encoding="utf-8")
    login(client, "alice", "pw")

    response = client.get("/library/traces/20260728T180036Z-a1b2c3d4")
    assert response.status_code == 200
    assert "+ LLM 6123.4 ms" in response.text  # per-step span on the first event
    assert "LLM 6300 ms" in response.text  # aggregate badge

    response = client.get("/api/traces/20260728T180036Z-a1b2c3d4")
    assert response.status_code == 200
    data = response.json()
    assert data["llm"]["llm_ms_total"] == 6300.0
    assert data["events"][0]["llm_ms"] == 6123.4


def test_traces_missing_and_invalid_id_404(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    assert client.get("/library/traces/no-such-trace").status_code == 404
    assert client.get("/api/traces/no-such-trace").status_code == 404
    # invalid id characters are rejected by the traversal guard, same 404
    assert client.get("/api/traces/bad%20id").status_code == 404


def test_cross_user_trace_access_404(env):
    client, _, data_root = env
    alice = make_user(data_root, "alice", "pw")
    make_user(data_root, "bob", "pw")
    make_trace(data_root, alice["id"])
    login(client, "bob", "pw")

    # alice's trace id does not exist in bob's own .traces store
    assert client.get("/library/traces/20260728T180036Z-a1b2c3d4").status_code == 404
    assert client.get("/api/traces/20260728T180036Z-a1b2c3d4").status_code == 404


def insert_journal_row(data_root, user_id, *, tool, trace_id, outcome="ok"):
    db_path = Path(data_root) / "app.db"
    conn = db_module.connect(db_path)
    try:
        db_module.insert_activity(
            conn,
            trace_id=trace_id,
            user_id=user_id,
            token_label="agent-a",
            tool=tool,
            arguments='{"query": "hi"}',
            started_at="2026-07-28T18:00:36+00:00",
            duration_ms=12.5,
            outcome=outcome,
            error=None if outcome == "ok" else "boom",
            iterations=1,
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
        )
    finally:
        conn.close()


def write_trace_file(data_root, user_id, trace_id, tool):
    """Minimal trace JSON on disk so the journal row earns a Replay link."""
    store = Path(data_root) / "users" / user_id / "library" / ".traces"
    store.mkdir(parents=True, exist_ok=True)
    (store / f"{trace_id}.json").write_text(
        json.dumps({"trace_id": trace_id, "tool": tool, "outcome": "ok", "events": []}) + "\n",
        encoding="utf-8",
    )


def test_activity_page_and_rows(env):
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    assert client.get("/activity").status_code == 200

    write_trace_file(data_root, user["id"], "t-ok", "request_knowledge")
    write_trace_file(data_root, user["id"], "t-curate", "library_curate")
    insert_journal_row(data_root, user["id"], tool="request_knowledge", trace_id="t-ok")
    insert_journal_row(data_root, user["id"], tool="library_curate", trace_id="t-curate")
    insert_journal_row(data_root, user["id"], tool="library_maintain", trace_id="t-noop")
    insert_journal_row(
        data_root, user["id"], tool="library_status", trace_id="t-status", outcome="error"
    )
    response = client.get("/activity/rows")
    assert response.status_code == 200
    assert "request_knowledge" in response.text
    assert "library_curate" in response.text
    assert "library_status" in response.text
    # trace links only for agent-backed tools with an existing trace file
    assert "/library/traces/t-ok" in response.text
    assert "/library/traces/t-curate" in response.text
    # traced tool but no trace file on disk (no-op run): no dead Replay link
    assert "/library/traces/t-noop" not in response.text
    assert "/library/traces/t-status" not in response.text
    assert "boom" in response.text
    # timestamps carry the raw UTC value for the client-side local-time render
    assert 'data-utc="' in response.text
    # registry absent on the test app: empty in-flight section, no crash
    assert "No calls in flight." in response.text


def test_activity_rows_render_in_flight_registry(env):
    client, _, data_root = env
    alice = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    class FakeRegistry:
        def snapshot(self):
            return [
                {
                    "trace_id": "t-live",
                    "user_id": alice["id"],
                    "token_label": "agent-live",
                    "tool": "store_knowledge",
                    "arguments": '{"content": "x"}',
                    "started_at": "2026-07-28T18:00:36+00:00",
                },
                {
                    "trace_id": "t-other",
                    "user_id": "someone-else",
                    "token_label": "agent-other",
                    "tool": "store_knowledge",
                    "arguments": '{"content": "secret"}',
                    "started_at": "2026-07-28T18:00:37+00:00",
                },
            ]

    client.app.state.activity_registry = FakeRegistry()
    response = client.get("/activity/rows")
    assert response.status_code == 200
    assert "agent-live" in response.text
    assert "store_knowledge" in response.text
    assert "No calls in flight." not in response.text
    # in-flight rows are owner-scoped: the other user's call never renders
    assert "agent-other" not in response.text
    assert "secret" not in response.text


# --- multi-user isolation -------------------------------------------------------


def test_cross_user_document_access_404(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    make_user(data_root, "bob", "pw")
    login(client, "bob", "pw")

    # bob's own library works
    response = client.get("/library/tree", params={"path": "/user-bob.md"})
    assert response.status_code == 200
    assert "Private to bob" in response.text

    # alice's document path does not exist in bob's bundle
    response = client.get("/library/tree", params={"path": "/user-alice.md"})
    assert response.status_code == 404

    # ...and never appears in bob's tree
    assert "user-alice.md" not in client.get("/library/tree").text


def test_cross_user_token_revoke_404(env):
    client, _, data_root = env
    alice = make_user(data_root, "alice", "pw")
    make_user(data_root, "bob", "pw")
    conn = db_module.connect(Path(data_root) / "app.db")
    try:
        _, token_hash = security.generate_token()
        token = db_module.create_token(conn, alice["id"], "alice-agent", token_hash)
    finally:
        conn.close()

    login(client, "bob", "pw")
    response = client.post(f"/tokens/{token['id']}/revoke")
    assert response.status_code == 404


# --- admin ----------------------------------------------------------------------


def test_admin_pages_require_admin(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    assert client.get("/admin/users").status_code == 403
    assert client.get("/admin/server").status_code == 403
    assert client.post("/admin/server", data={}).status_code == 403
    assert client.get("/admin/connections").status_code == 403
    assert client.post("/admin/connections", data={}).status_code == 403
    assert client.get("/admin/connections/some-id/edit").status_code == 403
    assert client.post("/admin/connections/some-id/delete", data={}).status_code == 403
    assert client.post("/admin/connections/some-id/test", data={}).status_code == 403


def test_admin_server_stateless_http_roundtrip(env):
    client, _, data_root = env
    make_user(data_root, "owner", "pw", admin=True)
    login(client, "owner", "pw")

    page = client.get("/admin/server")
    assert page.status_code == 200
    assert "Stateless MCP HTTP" in page.text

    db_path = Path(data_root) / "app.db"
    conn = db_module.connect(db_path)
    try:
        assert db_module.get_app_setting(conn, "mcp_stateless_http", "0") == "0"
    finally:
        conn.close()

    response = client.post("/admin/server", data={"mcp_stateless_http": "1"})
    assert response.status_code == 303
    conn = db_module.connect(db_path)
    try:
        assert db_module.get_app_setting(conn, "mcp_stateless_http") == "1"
    finally:
        conn.close()
    assert "checked" in client.get("/admin/server").text

    # an unchecked checkbox submits no field: back to off
    response = client.post("/admin/server", data={})
    assert response.status_code == 303
    conn = db_module.connect(db_path)
    try:
        assert db_module.get_app_setting(conn, "mcp_stateless_http") == "0"
    finally:
        conn.close()


def test_admin_server_computation_toggle_roundtrip(env):
    client, _, data_root = env
    make_user(data_root, "owner", "pw", admin=True)
    login(client, "owner", "pw")
    db_path = Path(data_root) / "app.db"

    page = client.get("/admin/server")
    assert page.status_code == 200
    assert "computation_execution_enabled" in page.text
    conn = db_module.connect(db_path)
    try:
        assert db_module.get_app_setting(conn, "computation_execution_enabled", "0") == "0"
    finally:
        conn.close()

    response = client.post("/admin/server", data={"computation_execution_enabled": "1"})
    assert response.status_code == 303
    conn = db_module.connect(db_path)
    try:
        assert db_module.get_app_setting(conn, "computation_execution_enabled") == "1"
    finally:
        conn.close()


def test_admin_connections_crud_and_write_only_password(env):
    client, _, data_root = env
    make_user(data_root, "owner", "pw", admin=True)
    login(client, "owner", "pw")
    db_path = Path(data_root) / "app.db"

    page = client.get("/admin/connections")
    assert page.status_code == 200
    assert "No connections yet" in page.text

    # create a postgres connection with a password
    response = client.post(
        "/admin/connections",
        data={
            "label": "Analytics",
            "runtime": "postgres",
            "host": "pg.internal",
            "port": "5432",
            "dbname": "analytics",
            "username": "ro_user",
            "password": "pg-secret",
        },
    )
    assert response.status_code == 303
    conn = db_module.connect(db_path)
    try:
        rows = db_module.list_runtime_connections(conn)
        assert len(rows) == 1
        connection_id = rows[0]["id"]
        stored = db_module.get_runtime_connection(conn, connection_id)
    finally:
        conn.close()
    assert stored["password_enc"]  # stored encrypted...
    assert stored["password_enc"] != "pg-secret"
    assert "pg-secret" not in client.get("/admin/connections").text  # ...never rendered

    # edit with an empty password field keeps the stored ciphertext
    response = client.post(
        f"/admin/connections/{connection_id}/edit",
        data={
            "label": "Analytics RO",
            "runtime": "postgres",
            "host": "pg2.internal",
            "port": "5433",
            "dbname": "analytics",
            "username": "ro_user",
            "password": "",
        },
    )
    assert response.status_code == 303
    conn = db_module.connect(db_path)
    try:
        updated = db_module.get_runtime_connection(conn, connection_id)
    finally:
        conn.close()
    assert updated["label"] == "Analytics RO"
    assert updated["password_enc"] == stored["password_enc"]

    # validation: postgres requires host/port/dbname/username
    response = client.post(
        "/admin/connections",
        data={"label": "Broken", "runtime": "postgres", "host": "", "port": "", "dbname": ""},
    )
    assert response.status_code == 400

    # delete
    response = client.post(f"/admin/connections/{connection_id}/delete")
    assert response.status_code == 303
    conn = db_module.connect(db_path)
    try:
        assert db_module.list_runtime_connections(conn) == []
    finally:
        conn.close()


def test_admin_connections_sqlite_create_and_test_probe(env, tmp_path):
    client, _, data_root = env
    make_user(data_root, "owner", "pw", admin=True)
    login(client, "owner", "pw")
    db_path = Path(data_root) / "app.db"

    import sqlite3 as sqlite3_mod

    target = tmp_path / "probe.db"
    conn = sqlite3_mod.connect(target)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()

    response = client.post(
        "/admin/connections",
        data={"label": "Local", "runtime": "sqlite", "dbname": str(target)},
    )
    assert response.status_code == 303
    conn = db_module.connect(db_path)
    try:
        connection_id = db_module.list_runtime_connections(conn)[0]["id"]
    finally:
        conn.close()

    response = client.post(f"/admin/connections/{connection_id}/test")
    assert response.status_code == 200
    assert "Connection OK" in response.text

    # sqlite requires the file path
    response = client.post(
        "/admin/connections", data={"label": "Broken", "runtime": "sqlite", "dbname": ""}
    )
    assert response.status_code == 400


def test_create_app_passes_stateless_http_flag(env, monkeypatch):
    from fastmcp import FastMCP

    from athenaeum.app import create_app

    _, _, data_root = env
    captured: list[dict] = []
    real_http_app = FastMCP.http_app

    def spy(self, **kwargs):
        captured.append(kwargs)
        return real_http_app(self, **kwargs)

    monkeypatch.setattr(FastMCP, "http_app", spy)
    create_app()
    assert captured[-1]["stateless_http"] is False

    conn = db_module.connect(Path(data_root) / "app.db")
    try:
        db_module.set_app_setting(conn, "mcp_stateless_http", "1")
    finally:
        conn.close()
    create_app()
    assert captured[-1]["stateless_http"] is True


def test_settings_dep_serves_one_cached_settings(app):
    """A15: create_app caches one Settings on app.state; the dependency serves
    that instance instead of re-parsing the environment on every request."""
    from starlette.requests import Request

    scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "app": app}
    assert deps.settings_dep(Request(scope)) is app.state.settings


def test_admin_create_and_reset_user(env):
    client, _, data_root = env
    make_user(data_root, "owner", "pw", admin=True)
    login(client, "owner", "pw")

    assert "User management" in client.get("/admin/users").text

    response = client.post(
        "/admin/users", data={"username": "newbie", "password": "newbie-password-1"}
    )
    assert response.status_code == 303

    # the new user can log in
    client.post("/logout")
    login(client, "newbie", "newbie-password-1")

    # admin resets the password
    client.post("/logout")
    login(client, "owner", "pw")
    newbie = db_module.get_user_by_username(db_module.connect(Path(data_root) / "app.db"), "newbie")
    response = client.post(
        f"/admin/users/{newbie['id']}/reset-password", data={"new_password": "newbie-password-2"}
    )
    assert response.status_code == 303
    client.post("/logout")
    login(client, "newbie", "newbie-password-2")


# --- bootstrap env pre-seed ------------------------------------------------------


def test_bootstrap_admin_if_configured(env, monkeypatch):
    client, _, data_root = env
    monkeypatch.setenv("ATHENAEUM_BOOTSTRAP_ADMIN_USERNAME", "owner")
    monkeypatch.setenv("ATHENAEUM_BOOTSTRAP_ADMIN_PASSWORD", "boot-password-1")

    settings = get_settings()
    assert routes_auth.bootstrap_admin_if_configured(settings)
    # ignored once a user exists
    assert not routes_auth.bootstrap_admin_if_configured(settings)

    # setup page is gone; the pre-seeded owner can log in
    response = client.get("/setup")
    assert response.status_code == 303
    login(client, "owner", "boot-password-1")


def test_bootstrap_admin_short_password_refused(env, monkeypatch, caplog):
    """SERVER-13: an env password below MIN_PASSWORD_LENGTH seeds NO account."""
    client, _, data_root = env
    monkeypatch.setenv("ATHENAEUM_BOOTSTRAP_ADMIN_USERNAME", "owner")
    monkeypatch.setenv("ATHENAEUM_BOOTSTRAP_ADMIN_PASSWORD", "short")

    settings = get_settings()
    with caplog.at_level("WARNING", logger="athenaeum.webui.routes_auth"):
        assert not routes_auth.bootstrap_admin_if_configured(settings)
    assert "refusing ATHENAEUM_BOOTSTRAP_ADMIN_* pre-seed" in caplog.text
    # the refusal happens before any DB write; no account was seeded
    db_path = Path(data_root) / "app.db"
    db_module.init_db(db_path)
    conn = db_module.connect(db_path)
    try:
        assert db_module.users_empty(conn)
    finally:
        conn.close()
    # no account seeded: the app still funnels into first-run setup
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"


# --- credential/session hardening (SERVER-04, SERVER-08) ------------------------


def test_password_reset_invalidates_existing_session(env):
    """SERVER-04: sessions carry a marker of the credential they were issued
    against; an admin reset flips the hash, so the old session is dropped."""
    client, _, data_root = env
    make_user(data_root, "owner", "owner-password-1", admin=True)
    make_user(data_root, "alice", "alice-password-1")

    login(client, "alice", "alice-password-1")
    assert client.get("/library/tree").status_code == 200
    alice_session = client.cookies.get("session")

    # the admin resets alice's password from a separate session
    client.post("/logout")
    login(client, "owner", "owner-password-1")
    alice = db_module.get_user_by_username(db_module.connect(Path(data_root) / "app.db"), "alice")
    response = client.post(
        f"/admin/users/{alice['id']}/reset-password", data={"new_password": "alice-password-2"}
    )
    assert response.status_code == 303
    # the admin's own session survives (their credential did not change)
    assert client.get("/admin/users").status_code == 200

    # alice's pre-reset session now redirects to /login (marker mismatch)
    client.cookies.set("session", alice_session)
    response = client.get("/library/tree")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    # she can log in with the new password immediately
    login(client, "alice", "alice-password-2")


def test_login_unknown_user_still_verifies_password(env, monkeypatch):
    """SERVER-08: the unknown-username branch pays one argon2 verification
    against a dummy hash (monkeypatched counter, no wall-clock assertion)."""
    client, _, data_root = env
    make_user(data_root, "alice", "alice-password-1")

    calls: list[str] = []
    real_verify = security.verify_password

    def counting_verify(password_hash, password):
        calls.append(password_hash)
        return real_verify(password_hash, password)

    monkeypatch.setattr(security, "verify_password", counting_verify)
    response = client.post("/login", data={"username": "ghost", "password": "whatever-pass"})
    assert response.status_code == 200
    assert "Invalid username or password" in response.text
    assert len(calls) == 1
    # verified against the lazily built dummy hash, not a user row
    assert calls[0] == routes_auth._DUMMY_HASH
    # known user, wrong password: exactly one verification against the row
    calls.clear()
    alice = db_module.get_user_by_username(db_module.connect(Path(data_root) / "app.db"), "alice")
    client.post("/login", data={"username": "alice", "password": "wrong"})
    assert calls == [alice["password_hash"]]


# --- template / MCP mount hardening (SERVER-11, SERVER-12) ----------------------


def test_base_html_pins_htmx_with_sri(env):
    """SERVER-11: the htmx CDN script carries a sha384 SRI pin."""
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    response = client.get("/tokens")
    assert response.status_code == 200
    assert 'src="https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js"' in response.text
    assert 'integrity="sha384-' in response.text
    assert 'crossorigin="anonymous"' in response.text


def test_mcp_catch_all_matches_exact_path_only(client):
    """SERVER-12: /mcpfoo is NOT the MCP endpoint — plain 404 JSON, never a
    FastMCP/JSON-RPC error."""
    response = client.get("/mcpfoo")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    assert "jsonrpc" not in response.text.lower()


def test_format_datetime():
    assert deps.format_datetime(None) == ""
    assert deps.format_datetime("") == ""
    assert deps.format_datetime("2026-01-01T00:00:00+00:00") == "2026-01-01 00:00 UTC"
    assert deps.format_datetime("2026-07-28T12:01:19.149973+00:00") == "2026-07-28 12:01 UTC"
    assert deps.format_datetime("not-a-date") == "not-a-date"


# --- library export / import -----------------------------------------------------


def _zip_bytes(members: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buffer.getvalue()


IMPORT_MEMBERS = {"index.md": "# Index\n", "log.md": "# Log\n", "restored.md": "back\n"}


def _tree_files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_library_export_requires_login(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    response = client.get("/library/export")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_library_import_requires_login(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    response = client.post(
        "/library/import",
        files={"file": ("lib.zip", _zip_bytes(IMPORT_MEMBERS), "application/zip")},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_library_import_csrf_rejected(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    response = client.post(
        "/library/import",
        files={"file": ("lib.zip", _zip_bytes(IMPORT_MEMBERS), "application/zip")},
        csrf=False,
    )
    assert response.status_code == 403


def test_library_import_csrf_multipart_success(env):
    """csrf_protect consumes request.form() before the handler's UploadFile
    binding; Starlette caches the parsed form so both coexist."""
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    root = Path(data_root) / "users" / user["id"] / "library"
    assert not (root / "restored.md").exists()

    response = client.post(
        "/library/import",
        data={},
        files={"file": ("lib.zip", _zip_bytes(IMPORT_MEMBERS), "application/zip")},
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/config/library?msg=")
    assert (root / "restored.md").read_text(encoding="utf-8") == "back\n"


def test_library_import_413_over_limit(env, monkeypatch):
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    monkeypatch.setattr(routes_library, "MAX_UPLOAD_BYTES", 64)
    root = Path(data_root) / "users" / user["id"] / "library"
    before = _tree_files(root)

    response = client.post(
        "/library/import",
        files={"file": ("lib.zip", _zip_bytes(IMPORT_MEMBERS), "application/zip")},
    )
    assert response.status_code == 413
    assert "512 MB" in response.text
    assert _tree_files(root) == before  # live bundle untouched


def test_library_import_409_already_in_progress(env):
    """SERVER-06: a second import while the per-user lock is held is a 409;
    the live bundle stays untouched (route-level complement to the
    _import_lock_for unit test in test_transfer.py)."""
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    root = Path(data_root) / "users" / user["id"] / "library"
    before = _tree_files(root)

    lock = routes_library._import_lock_for(user["id"])

    async def acquire() -> None:
        await lock.acquire()

    asyncio.run(acquire())  # held state persists after the loop closes
    try:
        response = client.post(
            "/library/import",
            files={"file": ("lib.zip", _zip_bytes(IMPORT_MEMBERS), "application/zip")},
        )
        assert response.status_code == 409
        assert "already in progress" in response.text
        assert _tree_files(root) == before  # live bundle untouched
    finally:
        lock.release()


def test_library_import_400_corrupt(env):
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    root = Path(data_root) / "users" / user["id"] / "library"
    before = _tree_files(root)

    response = client.post(
        "/library/import",
        files={"file": ("lib.zip", b"not a zip", "application/zip")},
    )
    assert response.status_code == 400
    assert _tree_files(root) == before  # live bundle byte-identical


def test_library_import_400_zip_slip(env):
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    user_dir = Path(data_root) / "users" / user["id"]
    members = {"index.md": "# I\n", "log.md": "# L\n", "../evil.md": "x"}

    response = client.post(
        "/library/import",
        files={"file": ("lib.zip", _zip_bytes(members), "application/zip")},
    )
    assert response.status_code == 400
    assert not (user_dir / "evil.md").exists()  # nothing outside the bundle
    assert not (user_dir / "library.import-staging").exists()


@pytest.mark.parametrize("missing", ["index.md", "log.md"])
def test_library_import_400_missing_required(env, missing):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    members = {"index.md": "# I\n", "log.md": "# L\n"}
    del members[missing]

    response = client.post(
        "/library/import",
        files={"file": ("lib.zip", _zip_bytes(members), "application/zip")},
    )
    assert response.status_code == 400
    assert missing in response.text


def test_library_export_headers_and_contents(env):
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    root = Path(data_root) / "users" / user["id"] / "library"
    (root / "notes.md").write_text("hello export\n", encoding="utf-8")

    response = client.get("/library/export")
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert "attachment" in disposition
    assert "athenaeum-library-" in disposition
    assert ".zip" in disposition
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        names = set(zf.namelist())
    assert {"index.md", "log.md", "notes.md"} <= names
